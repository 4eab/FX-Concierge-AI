"""
Pydantic schemas used as Agent output_schema and for structured data exchange.
Using explicit schemas eliminates hallucination in LLM-generated structured output.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding schemas
# ─────────────────────────────────────────────────────────────────────────────

class RiskTolerance(str, Enum):
    conservative = "conservative"   # 稳健，价格合适即可
    moderate = "moderate"           # 中等，追求合理价位
    aggressive = "aggressive"       # 积极，等待最佳时机


class TimeHorizon(str, Enum):
    short = "short"    # 1个月内
    medium = "medium"  # 1-6个月
    long = "long"      # 6个月以上


class PurchaseIntent(BaseModel):
    """User's FX purchase needs collected during onboarding."""
    purpose: str = Field(description="Purpose: travel, study, investment, remittance, etc.")
    amount: float = Field(description="Amount in target currency to convert (e.g. 5000 for 5000 EUR. 0 if unknown)")
    time_horizon: TimeHorizon = Field(description="When the user needs to complete the purchase")
    risk_tolerance: RiskTolerance = Field(description="User's price sensitivity and risk appetite")
    target_rate: float | None = Field(
        default=None,
        description="User's ideal target rate (e.g. 1 EUR = 7.8 CNY). None if no specific target."
    )
    alert_threshold: int = Field(default=70, ge=50, le=95, description="Minimum composite score to trigger alert")


class ScoringWeights(BaseModel):
    """Dimension weights for composite scoring. Must sum to 1.0."""
    historical_percentile: Annotated[float, Field(ge=0.0, le=1.0)] = 0.30
    short_term_trend: Annotated[float, Field(ge=0.0, le=1.0)] = 0.25
    mean_reversion: Annotated[float, Field(ge=0.0, le=1.0)] = 0.20
    user_goal: Annotated[float, Field(ge=0.0, le=1.0)] = 0.15
    volatility: Annotated[float, Field(ge=0.0, le=1.0)] = 0.10

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ScoringWeights":
        """Ensure weights sum to 1.0 within 1% tolerance."""
        total = (
            self.historical_percentile
            + self.short_term_trend
            + self.mean_reversion
            + self.user_goal
            + self.volatility
        )
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"ScoringWeights must sum to 1.0, got {total:.4f}. "
                "Check: historical_percentile + short_term_trend + mean_reversion + user_goal + volatility = 1.0"
            )
        return self


class OnboardingResult(BaseModel):
    """Output schema for onboarding agent."""
    source_currency: str = Field(description="Base currency user holds, e.g. 'CNY'")
    target_currencies: list[str] = Field(description="List of currencies to track, e.g. ['EUR', 'USD']")
    purchase_intents: dict[str, PurchaseIntent] = Field(
        description="Map from target currency to its purchase intent configuration"
    )
    scoring_weights: dict[str, ScoringWeights] = Field(
        description="Map from target currency to its scoring weights configuration"
    )
    onboarding_summary: str = Field(description="1-2 sentence summary of the user's setup for confirmation")


# ─────────────────────────────────────────────────────────────────────────────
# Historical analysis schemas
# ─────────────────────────────────────────────────────────────────────────────

class CurrencyAnalysisSummary(BaseModel):
    """Per-currency analysis result."""
    source_currency: str
    target_currency: str
    period_days: int
    current_rate: float
    mean_rate: float
    std_rate: float
    min_rate: float
    max_rate: float
    current_percentile: float = Field(description="Where current rate sits in history (0=lowest=best for buyer, 100=highest)")
    trend_30d: str = Field(description="'rising'|'falling'|'flat' over last 30 days")
    trend_90d: str = Field(description="'rising'|'falling'|'flat' over last 90 days")
    annualised_volatility_pct: float = Field(description="Annualised volatility in percent")
    key_insight: str = Field(description="1-2 sentence human-readable insight for this currency pair")


class HistoricalAnalysisReport(BaseModel):
    """Output schema for analysis agent — structured report of historical patterns."""
    analysis_date: str = Field(description="ISO date of analysis")
    currency_summaries: list[CurrencyAnalysisSummary]
    overall_market_context: str = Field(
        description="2-3 sentences summarising the current macro FX environment"
    )
    recommendation_preview: str = Field(
        description="Brief preview of what to watch for before offering a full briefing"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scoring schemas
# ─────────────────────────────────────────────────────────────────────────────

class DimensionScore(BaseModel):
    """Score and rationale for one scoring dimension (0-100)."""
    score: Annotated[float, Field(ge=0.0, le=100.0)]
    rationale: str = Field(description="1 sentence explaining this score")


class CurrencyScoreResult(BaseModel):
    """Scoring result for one currency pair."""
    source_currency: str
    target_currency: str
    current_rate: float
    rate_source: str

    # Five dimensions
    historical_percentile: DimensionScore = Field(
        description="Score based on where current rate sits in historical distribution. "
        "Higher score = better buying opportunity (lower percentile = cheaper)"
    )
    short_term_trend: DimensionScore = Field(
        description="Score based on recent 7-day momentum. "
        "Higher score = trend favours buyer (target currency weakening)"
    )
    mean_reversion: DimensionScore = Field(
        description="Score based on deviation from 30d/90d mean. "
        "Higher score = rate significantly above mean (may revert down = better buy)"
    )
    user_goal: DimensionScore = Field(
        description="Score based on alignment with user's target rate and timeline. "
        "Higher score = rate meets or beats user's target"
    )
    volatility: DimensionScore = Field(
        description="Score based on recent volatility (inverted: lower volatility = higher score). "
        "High volatility increases execution risk"
    )

    composite_score: Annotated[float, Field(ge=0.0, le=100.0)] = Field(
        description="Weighted composite score (0-100). Above threshold triggers alert."
    )
    alert_worthy: bool = Field(description="True if composite_score >= user's threshold")
    summary: str = Field(
        description="2-3 sentence human-readable summary suitable for Telegram message. "
        "Explain WHY this score was reached and what action to consider."
    )


class ScoringReport(BaseModel):
    """Output schema for scoring agent."""
    scored_at: str = Field(description="ISO datetime of scoring")
    scores: list[CurrencyScoreResult]
    top_opportunity: str | None = Field(
        default=None,
        description="ISO currency code of the best opportunity, or None if none are alert-worthy"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Briefing schemas
# ─────────────────────────────────────────────────────────────────────────────

class BriefingReport(BaseModel):
    """Output schema for briefing agent."""
    title: str = Field(description="Report title (e.g. 'EUR/CNY Opportunity Briefing — July 3')")
    executive_summary: str = Field(description="2-3 sentence executive summary")
    currency_sections: list[str] = Field(
        description="One section per currency pair with analysis, score breakdown, and recommendation"
    )
    action_items: list[str] = Field(
        description="Bullet-point action items for the user (max 3)"
    )
    disclaimer: str = Field(
        default="本报告由AI生成，仅供参考，不构成金融建议。汇率存在波动风险，请结合自身情况决策。",
        description="Standard disclaimer"
    )
