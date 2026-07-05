"""
SQLAlchemy data models for FX Monitor AI.

Tables:
  user_profiles     - Per-user Telegram config, currencies, scoring weights
  historical_stats  - Cached statistical summaries from ECB history
  daily_rates       - EOD ECB rates + intraday BOC rates
  rate_scores       - AI scoring records (per session / alert)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# UserProfile — one row per Telegram user (chat_id)
# ─────────────────────────────────────────────────────────────────────────────
class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Onboarding state: "new" | "onboarding" | "active"
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False)

    # Source currency (e.g. "CNY")
    source_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # JSON list of target currencies, e.g. ["EUR", "USD"]
    target_currencies_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # Purchase intent: JSON with keys amount, horizon, purpose, risk_tolerance
    purchase_intent_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    # Scoring weights: JSON with 5 dimension weights summing to 1.0
    scoring_weights_json: Mapped[str] = mapped_column(
        Text,
        default='{"historical_percentile":0.30,"short_term_trend":0.25,"mean_reversion":0.20,"user_goal":0.15,"volatility":0.10}',
        nullable=False,
    )

    # Alert score threshold (override per-user, default from config)
    alert_threshold: Mapped[int] = mapped_column(Integer, default=70, nullable=False)

    # Alert enabled flag
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def target_currencies(self) -> list[str]:
        return json.loads(self.target_currencies_json)

    @target_currencies.setter
    def target_currencies(self, value: list[str]) -> None:
        self.target_currencies_json = json.dumps(value)

    @property
    def purchase_intent(self) -> dict:
        return json.loads(self.purchase_intent_json)

    @purchase_intent.setter
    def purchase_intent(self, value: dict) -> None:
        self.purchase_intent_json = json.dumps(value)

    @property
    def scoring_weights(self) -> dict:
        return json.loads(self.scoring_weights_json)

    @scoring_weights.setter
    def scoring_weights(self, value: dict) -> None:
        self.scoring_weights_json = json.dumps(value)


# ─────────────────────────────────────────────────────────────────────────────
# DailyRate — one row per (date, source_currency, target_currency, source)
# ─────────────────────────────────────────────────────────────────────────────
class DailyRate(Base):
    __tablename__ = "daily_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # "ECB" for official EOD, "BOC" for realtime scrape
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    # e.g. "EUR", "USD" (from currency)
    source_currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # e.g. "CNY" (to currency)
    target_currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # 1 source_currency = X target_currency
    rate: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)

    # For BOC: spot sell price; for ECB: mid rate
    rate_type: Mapped[str] = mapped_column(String(20), default="mid", nullable=False)

    __table_args__ = (
        Index("idx_daily_rate_lookup", "source_currency", "target_currency", "recorded_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# HistoricalStats — cached stats per currency pair, refreshed on demand
# ─────────────────────────────────────────────────────────────────────────────
class HistoricalStats(Base):
    __tablename__ = "historical_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    target_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # Sample window (days)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)

    mean_rate: Mapped[float] = mapped_column(Float, nullable=False)
    std_rate: Mapped[float] = mapped_column(Float, nullable=False)
    min_rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_rate: Mapped[float] = mapped_column(Float, nullable=False)

    # Percentile breakpoints stored as JSON: {"p10":x, "p25":x, "p50":x, "p75":x, "p90":x}
    percentiles_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Recent trend: slope of linear regression over last 30 days (positive = appreciation)
    trend_30d_slope: Mapped[float] = mapped_column(Float, nullable=True)
    trend_90d_slope: Mapped[float] = mapped_column(Float, nullable=True)

    # Annualised volatility (std of daily returns * sqrt(252))
    annualised_volatility: Mapped[float] = mapped_column(Float, nullable=True)

    # Summary JSON for LLM context (chart-ready)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        UniqueConstraint("source_currency", "target_currency", "window_days"),
    )

    @property
    def percentiles(self) -> dict:
        return json.loads(self.percentiles_json)

    @property
    def summary(self) -> dict:
        return json.loads(self.summary_json)


# ─────────────────────────────────────────────────────────────────────────────
# RateScore — AI scoring record per user/currency/timestamp
# ─────────────────────────────────────────────────────────────────────────────
class RateScore(Base):
    __tablename__ = "rate_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(20), nullable=False)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    source_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    target_currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # The rate that was scored
    current_rate: Mapped[float] = mapped_column(Float, nullable=False)
    rate_source: Mapped[str] = mapped_column(String(20), nullable=False)  # "BOC"/"ECB"

    # Individual dimension scores (0-100)
    score_historical_percentile: Mapped[float] = mapped_column(Float, nullable=False)
    score_short_term_trend: Mapped[float] = mapped_column(Float, nullable=False)
    score_mean_reversion: Mapped[float] = mapped_column(Float, nullable=False)
    score_user_goal: Mapped[float] = mapped_column(Float, nullable=False)
    score_volatility: Mapped[float] = mapped_column(Float, nullable=False)

    # Weighted composite (0-100)
    composite_score: Mapped[float] = mapped_column(Float, nullable=False)

    # AI-generated rationale (short)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)

    # Whether an alert was sent
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("idx_score_lookup", "chat_id", "source_currency", "target_currency", "scored_at"),
    )
