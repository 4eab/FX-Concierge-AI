"""
ADK tool functions that wrap MCP servers and database operations.

These are the building blocks used by ADK Agents as tools=[...] entries.
All tools follow ADK conventions:
  - Clear docstrings (sent to LLM)
  - Type hints on all params, NO default values (ADK requirement)
  - Return dict (JSON-serializable)
  - ToolContext as last optional param for state access
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root so MCP server modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# ECB tools (call MCP server via direct import for prototype, MCP in prod)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_ecb_history(target_currency: str) -> dict:
    """Fetch full ECB historical exchange rates for a currency pair.

    Pulls from the ECB eurofxref-hist.xml (~400KB, ~5000 records going back to 1999).
    All rates are expressed as: 1 EUR = X target_currency.
    This is a slow call — use only during initial setup or when adding a new currency.

    Args:
        target_currency: ISO 4217 code of the target currency (e.g. 'CNY', 'USD').

    Returns:
        dict with 'status', 'currency_pair', 'record_count', and 'records' list.
    """
    import httpx
    import xml.etree.ElementTree as ET
    from decimal import Decimal, InvalidOperation
    from datetime import date as dateclass

    ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
    NS = {
        "gesmes": "http://www.gesmes.org/xml/2002-08-01",
        "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(ECB_HIST_URL)
        response.raise_for_status()

    root = ET.fromstring(response.content)
    records = []

    for cube_time in root.findall(".//ecb:Cube[@time]", NS):
        rate_date = dateclass.fromisoformat(cube_time.attrib["time"])
        for cube_rate in cube_time.findall("ecb:Cube", NS):
            if cube_rate.attrib.get("currency") == target_currency:
                try:
                    rate = float(Decimal(cube_rate.attrib["rate"]))
                    records.append({
                        "date": rate_date.isoformat(),
                        "source_currency": "EUR",
                        "target_currency": target_currency,
                        "rate": rate,
                        "rate_type": "mid",
                        "source": "ECB",
                    })
                except InvalidOperation:
                    continue

    return {
        "status": "success",
        "currency_pair": f"EUR/{target_currency}",
        "record_count": len(records),
        "records": records,
    }


async def fetch_ecb_daily(target_currencies: list) -> dict:
    """Fetch today's ECB reference rates for multiple currencies.

    Pulls from eurofxref.xml, published around 16:00 CET each trading day.
    These are END-OF-DAY rates — use for the nightly supplement task, not intraday.

    Args:
        target_currencies: List of ISO codes to fetch (e.g. ['CNY', 'USD']).

    Returns:
        dict with 'status', 'fetch_date', and 'rates' mapping.
    """
    from app.sources.ecb import ECBRateSource
    try:
        source = ECBRateSource()
        timestamp, rates_decimal = await source.fetch_ecb_raw_rates()
        target_set = set(target_currencies)
        rates = {
            currency: float(rate)
            for currency, rate in rates_decimal.items()
            if currency in target_set
        }
        return {
            "status": "success",
            "fetch_date": timestamp.date().isoformat(),
            "source": "ECB",
            "base_currency": "EUR",
            "rates": rates,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }


async def fetch_boc_rate(target_currency: str, source_currency: str = "CNY") -> dict:
    """Fetch real-time spot rate for a target currency against a source base currency.

    Scrapes rates and returns the price of target_currency in terms of source_currency.

    Args:
        target_currency: ISO 4217 code of the currency to buy (e.g. 'EUR', 'USD').
        source_currency: ISO 4217 code of the currency you hold (e.g. 'CNY', 'USD'). Default is 'CNY'.

    Returns:
        dict with 'status', 'source_currency', 'target_currency', 'spot_sell', 'published_at', and 'explanation'.
    """
    from app.sources import rate_manager
    try:
        quote = await rate_manager.fetch_rate(target_currency, source_currency)
        return {
            "status": "success",
            "source_currency": quote.currency_from,
            "target_currency": quote.currency_to,
            "spot_sell": float(quote.rate),
            "spot_buy": None,
            "rate": float(quote.rate),
            "rate_type": quote.rate_type,
            "published_at": quote.timestamp.isoformat(),
            "published_at_beijing": quote.timestamp.strftime("%Y-%m-%d %H:%M:%S (北京时间)"),
            "source": quote.source,
            "explanation": f"1 {quote.currency_from} = {float(quote.rate):.4f} {quote.currency_to}",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }



# ─────────────────────────────────────────────────────────────────────────────
# Helper functions for config calculations
# ─────────────────────────────────────────────────────────────────────────────

def compute_scoring_weights(risk_tolerance: str, time_horizon: str) -> dict:
    """Compute scoring weights deterministically from risk/horizon profile.

    All weight sets sum to exactly 1.0.
    """
    if risk_tolerance == "aggressive" and time_horizon == "short":
        return {
            "historical_percentile": 0.35,
            "short_term_trend": 0.30,
            "mean_reversion": 0.15,
            "user_goal": 0.10,
            "volatility": 0.10,
        }
    elif risk_tolerance == "conservative" and time_horizon == "long":
        return {
            "historical_percentile": 0.25,
            "short_term_trend": 0.15,
            "mean_reversion": 0.25,
            "user_goal": 0.20,
            "volatility": 0.15,
        }
    elif time_horizon == "long":
        return {
            "historical_percentile": 0.30,
            "short_term_trend": 0.15,
            "mean_reversion": 0.25,
            "user_goal": 0.20,
            "volatility": 0.10,
        }
    else:
        return {
            "historical_percentile": 0.30,
            "short_term_trend": 0.25,
            "mean_reversion": 0.20,
            "user_goal": 0.15,
            "volatility": 0.10,
        }


def compute_alert_threshold(risk_tolerance: str) -> int:
    """Compute alert threshold deterministically from risk tolerance."""
    thresholds = {
        "conservative": 65,
        "moderate": 70,
        "aggressive": 75,
    }
    return thresholds.get(risk_tolerance, 70)


# ─────────────────────────────────────────────────────────────────────────────
# Database tools
# ─────────────────────────────────────────────────────────────────────────────

def get_user_profile(chat_id: str) -> dict:
    """Retrieve a user's profile and configuration from the database.

    Automatically normalizes old formats to per-currency dictionaries.

    Args:
        chat_id: Telegram chat_id (as string).

    Returns:
        dict with user profile data, or status='not_found' if new user.
    """
    from app.database import SessionLocal
    from app.models import UserProfile

    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(chat_id=chat_id).first()
        if not profile:
            return {"status": "not_found", "chat_id": chat_id}

        target_currencies = profile.target_currencies
        raw_intent = profile.purchase_intent
        raw_weights = profile.scoring_weights

        # Normalize purchase_intent to dict[str, dict]
        normalized_intent = {}
        if isinstance(raw_intent, dict):
            if "purpose" in raw_intent:
                # Old single currency format
                single_intent = dict(raw_intent)
                if "amount_cny" in single_intent:
                    single_intent["amount"] = single_intent.pop("amount_cny")
                if "amount" not in single_intent:
                    single_intent["amount"] = 0.0
                single_intent["alert_threshold"] = profile.alert_threshold
                for currency in target_currencies:
                    normalized_intent[currency] = dict(single_intent)
            else:
                # New per-currency format
                for currency in target_currencies:
                    curr_intent = dict(raw_intent.get(currency, {
                        "purpose": "未设置",
                        "amount": 0.0,
                        "time_horizon": "medium",
                        "risk_tolerance": "moderate",
                        "target_rate": None,
                        "alert_threshold": 70
                    }))
                    if "amount_cny" in curr_intent:
                        curr_intent["amount"] = curr_intent.pop("amount_cny")
                    if "amount" not in curr_intent:
                        curr_intent["amount"] = 0.0
                    if "alert_threshold" not in curr_intent:
                        curr_intent["alert_threshold"] = profile.alert_threshold
                    normalized_intent[currency] = curr_intent
        else:
            normalized_intent = {}

        # Normalize scoring_weights to dict[str, dict]
        normalized_weights = {}
        if isinstance(raw_weights, dict):
            if "historical_percentile" in raw_weights:
                # Old single weights format
                for currency in target_currencies:
                    normalized_weights[currency] = raw_weights
            else:
                # New per-currency format
                for currency in target_currencies:
                    normalized_weights[currency] = raw_weights.get(currency, {
                        "historical_percentile": 0.30,
                        "short_term_trend": 0.25,
                        "mean_reversion": 0.20,
                        "user_goal": 0.15,
                        "volatility": 0.10,
                    })
        else:
            normalized_weights = {}

        return {
            "status": "found",
            "chat_id": profile.chat_id,
            "onboarding_status": profile.status,
            "source_currency": profile.source_currency,
            "target_currencies": target_currencies,
            "purchase_intent": normalized_intent,
            "scoring_weights": normalized_weights,
            "alert_threshold": profile.alert_threshold,
            "alerts_enabled": profile.alerts_enabled,
        }


def save_user_profile(
    chat_id: str,
    source_currency: str,
    target_currencies: list,
    purchase_intent: dict,
    scoring_weights: dict,
    alert_threshold: int,
) -> dict:
    """Create or update a user profile in the database.

    Args:
        chat_id: Telegram chat_id (as string).
        source_currency: Currency the user holds (e.g. 'CNY').
        target_currencies: List of currencies to track (e.g. ['EUR', 'USD']).
        purchase_intent: Dict with purchase needs (purpose, amount, horizon, etc.).
        scoring_weights: Dict with 5 scoring dimension weights.
        alert_threshold: Minimum composite score (0-100) to trigger an alert.

    Returns:
        dict with 'status' and 'chat_id'.
    """
    from app.database import SessionLocal
    from app.models import UserProfile

    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(chat_id=chat_id).first()
        if not profile:
            profile = UserProfile(chat_id=chat_id)
            db.add(profile)

        profile.status = "active"
        profile.source_currency = source_currency
        profile.target_currencies = target_currencies
        profile.purchase_intent = purchase_intent
        profile.scoring_weights = scoring_weights
        profile.alert_threshold = alert_threshold
        db.commit()

    return {"status": "saved", "chat_id": chat_id}


def update_currency_intent(
    chat_id: str,
    currency: str,
    purpose: str | None = None,
    amount: float | None = None,
    time_horizon: str | None = None,
    risk_tolerance: str | None = None,
    target_rate: float | None = None,
    alert_threshold: int | None = None,
) -> dict:
    """Granular tool to update a single tracked currency's purchase intent settings.

    Use this when the user wants to adjust their target rate, budget amount, risk,
    or window for a specific currency (e.g. EUR).

    Args:
        chat_id: Telegram chat_id (as string).
        currency: The target currency code to update (e.g. 'EUR', 'USD').
        purpose: Purpose of purchase (e.g., '留学', '旅游'). None to leave unchanged.
        amount: Budget/amount to purchase in units of the target currency. None to leave unchanged.
        time_horizon: Timeline for purchase ('short' | 'medium' | 'long'). None to leave unchanged.
        risk_tolerance: Risk appetite ('conservative' | 'moderate' | 'aggressive'). None to leave unchanged.
        target_rate: User's target exchange rate. None to leave unchanged (or 0.0/None to disable target rate).
        alert_threshold: Manual alert score threshold override (50-95). None to leave unchanged.

    Returns:
        dict with status.
    """
    from app.database import SessionLocal
    from app.models import UserProfile

    currency = currency.upper()

    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(chat_id=chat_id).first()
        if not profile:
            return {"status": "error", "message": f"User {chat_id} not found."}

        p_dict = get_user_profile(chat_id)
        current_intents = p_dict.get("purchase_intent", {})
        current_weights = p_dict.get("scoring_weights", {})

        intent = current_intents.get(currency, {
            "purpose": "未设置",
            "amount": 0.0,
            "time_horizon": "medium",
            "risk_tolerance": "moderate",
            "target_rate": None,
            "alert_threshold": 70
        })

        if purpose is not None:
            intent["purpose"] = purpose
        if amount is not None:
            intent["amount"] = amount
        if time_horizon is not None:
            intent["time_horizon"] = time_horizon
        if risk_tolerance is not None:
            intent["risk_tolerance"] = risk_tolerance
        if target_rate is not None:
            # Handle removing target rate (if passed 0 or None)
            intent["target_rate"] = None if target_rate in (0.0, 0, None) else target_rate
        if alert_threshold is not None:
            intent["alert_threshold"] = alert_threshold

        new_risk = intent.get("risk_tolerance", "moderate")
        new_horizon = intent.get("time_horizon", "medium")
        
        # Determine threshold from risk if not manually overridden
        if alert_threshold is None:
            intent["alert_threshold"] = compute_alert_threshold(new_risk)

        # Recalculate weights and intents
        current_weights[currency] = compute_scoring_weights(new_risk, new_horizon)
        current_intents[currency] = intent

        profile.purchase_intent = current_intents
        profile.scoring_weights = current_weights
        profile.alert_threshold = intent["alert_threshold"]
        db.commit()

    return {
        "status": "success",
        "chat_id": chat_id,
        "currency": currency,
        "updated_intent": intent
    }


async def update_base_currency(chat_id: str, source_currency: str) -> dict:
    """Update the user's base/source currency (the currency they currently hold).

    Use this when the user wants to change their base currency (e.g. from CNY to USD, or "buy EUR using USD").
    This automatically recalculates and fetches historical stats for all tracked currencies
    against the new base currency.

    Args:
        chat_id: Telegram chat_id (as string).
        source_currency: The currency code the user currently holds (e.g., 'USD', 'CNY', 'EUR').

    Returns:
        dict with status.
    """
    from app.database import SessionLocal
    from app.models import UserProfile

    source_currency = source_currency.upper()

    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(chat_id=chat_id).first()
        if not profile:
            return {"status": "error", "message": f"User {chat_id} not found."}

        profile.source_currency = source_currency
        db.commit()

        # Recalculate stats for all currently tracked currencies against the new base currency
        targets = profile.target_currencies
        for target in targets:
            await ensure_currency_history_and_stats(source_currency, target)

    return {
        "status": "success",
        "chat_id": chat_id,
        "source_currency": source_currency,
        "tracked_currencies": targets
    }


async def ensure_currency_history_and_stats(source: str, target: str) -> bool:
    """Fetch ECB history, compute crossed rates, and calculate stats for a target currency against source currency.

    Safe to run repeatedly; it will upsert/refresh historical data.
    """
    source = source.upper()
    target = target.upper()

    try:
        # 1. Fetch source currency against EUR
        source_history = {}
        if source != "EUR":
            res_source = await fetch_ecb_history(source)
            if res_source.get("status") == "success":
                bulk_save_ecb_history(res_source.get("records", []))
                compute_and_save_stats("EUR", source)
                source_history = {r["date"]: float(r["rate"]) for r in res_source.get("records", [])}
            else:
                print(f"[History] Failed to fetch source currency {source}")
                return False

        if target == "EUR":
            return True

        # 2. Fetch target currency against EUR
        target_res = await fetch_ecb_history(target)
        if target_res.get("status") != "success":
            print(f"[History] Failed to fetch target currency {target}")
            return False

        bulk_save_ecb_history(target_res.get("records", []))
        compute_and_save_stats("EUR", target)

        if source == "EUR":
            return True

        # 3. Cross compute target/source (e.g. USD/CNY = (EUR/CNY) / (EUR/USD))
        crossed_records = []
        for r in target_res.get("records", []):
            d = r["date"]
            rate_eur_target = float(r["rate"])
            if source_history and d in source_history and rate_eur_target > 0:
                rate_eur_source = source_history[d]
                crossed_rate = rate_eur_source / rate_eur_target
                crossed_records.append({
                    "date": d,
                    "source_currency": target,
                    "target_currency": source,
                    "rate": crossed_rate,
                    "source": "ECB"
                })

        # Save crossed rates and compute stats
        bulk_save_ecb_history(crossed_records)
        compute_and_save_stats(target, source)
        return True
    except Exception as e:
        print(f"[History] Exception in ensure_history: {e}")
        return False



async def add_monitored_currency(chat_id: str, currency: str) -> dict:
    """Add a new target currency to user's tracked list and initialize its default settings.

    Automatically fetches history and calculates stats for the new currency.

    Args:
        chat_id: Telegram chat_id (as string).
        currency: The target currency code to add (e.g. 'USD', 'GBP').

    Returns:
        dict with status.
    """
    from app.database import SessionLocal
    from app.models import UserProfile

    currency = currency.upper()

    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(chat_id=chat_id).first()
        if not profile:
            return {"status": "error", "message": f"User {chat_id} not found."}

        targets = profile.target_currencies
        if currency not in targets:
            targets.append(currency)
            profile.target_currencies = targets

            p_dict = get_user_profile(chat_id)
            intents = p_dict.get("purchase_intent", {})
            weights = p_dict.get("scoring_weights", {})

            intents[currency] = {
                "purpose": "未设置",
                "amount": 0.0,
                "time_horizon": "medium",
                "risk_tolerance": "moderate",
                "target_rate": None,
                "alert_threshold": 70
            }
            weights[currency] = {
                "historical_percentile": 0.30,
                "short_term_trend": 0.25,
                "mean_reversion": 0.20,
                "user_goal": 0.15,
                "volatility": 0.10
            }
            profile.purchase_intent = intents
            profile.scoring_weights = weights
            db.commit()

            # Async fetch history and compute stats for the new currency
            source = profile.source_currency or "CNY"
            await ensure_currency_history_and_stats(source, currency)

    return {"status": "success", "chat_id": chat_id, "added": currency}


def remove_monitored_currency(chat_id: str, currency: str) -> dict:
    """Remove a target currency from user's tracked list.

    Use this when the user says "删除追踪美元" or "不想监控欧元了".

    Args:
        chat_id: Telegram chat_id (as string).
        currency: The target currency code to remove (e.g. 'USD', 'EUR').

    Returns:
        dict with status.
    """
    from app.database import SessionLocal
    from app.models import UserProfile

    currency = currency.upper()

    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(chat_id=chat_id).first()
        if not profile:
            return {"status": "error", "message": f"User {chat_id} not found."}

        targets = profile.target_currencies
        if currency in targets:
            targets.remove(currency)
            profile.target_currencies = targets

            p_dict = get_user_profile(chat_id)
            intents = p_dict.get("purchase_intent", {})
            weights = p_dict.get("scoring_weights", {})

            intents.pop(currency, None)
            weights.pop(currency, None)

            profile.purchase_intent = intents
            profile.scoring_weights = weights
            db.commit()

    return {"status": "success", "chat_id": chat_id, "removed": currency}


def save_daily_rate(
    source_currency: str,
    target_currency: str,
    rate: float,
    rate_source: str,
    rate_type: str,
) -> dict:
    """Persist a fetched exchange rate to the daily_rates table.

    Args:
        source_currency: From currency (e.g. 'EUR').
        target_currency: To currency (e.g. 'CNY').
        rate: Exchange rate (1 source = X target).
        rate_source: Data provider name ('BOC' or 'ECB').
        rate_type: 'spot_sell', 'spot_buy', or 'mid'.

    Returns:
        dict with 'status' and 'id' of saved record.
    """
    from app.database import SessionLocal
    from app.models import DailyRate

    with SessionLocal() as db:
        record = DailyRate(
            recorded_at=datetime.now(timezone.utc),
            source=rate_source,
            source_currency=source_currency,
            target_currency=target_currency,
            rate=rate,
            rate_type=rate_type,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return {"status": "saved", "id": record.id}


def bulk_save_ecb_history(records: list) -> dict:
    """Bulk-insert ECB historical rate records.

    Skips duplicates (same date + currency pair already in DB).

    Args:
        records: List of dicts with keys: date, source_currency, target_currency, rate.

    Returns:
        dict with 'status', 'inserted', 'skipped'.
    """
    from datetime import datetime as dt
    from app.database import SessionLocal
    from app.models import DailyRate

    with SessionLocal() as db:
        # Get existing (date, source, target) to avoid duplicates
        existing = set(
            (str(r.recorded_at.date()), r.source_currency, r.target_currency)
            for r in db.query(DailyRate).filter_by(source="ECB").all()
        )

        to_insert = []
        for rec in records:
            key = (rec["date"], rec["source_currency"], rec["target_currency"])
            if key not in existing:
                to_insert.append(
                    DailyRate(
                        recorded_at=dt.fromisoformat(rec["date"]).replace(tzinfo=timezone.utc),
                        source="ECB",
                        source_currency=rec["source_currency"],
                        target_currency=rec["target_currency"],
                        rate=rec["rate"],
                        rate_type="mid",
                    )
                )

        db.add_all(to_insert)
        db.commit()

    return {"status": "done", "inserted": len(to_insert), "skipped": len(records) - len(to_insert)}


def get_historical_stats(source_currency: str, target_currency: str) -> dict:
    """Retrieve pre-computed historical statistics for a currency pair.

    Returns statistical summary used for AI scoring dimensions.

    Args:
        source_currency: From currency (e.g. 'EUR').
        target_currency: To currency (e.g. 'CNY').

    Returns:
        dict with statistical summary, or 'not_computed' if not yet analysed.
    """
    from app.database import SessionLocal
    from app.models import HistoricalStats

    with SessionLocal() as db:
        stats = (
            db.query(HistoricalStats)
            .filter_by(source_currency=source_currency, target_currency=target_currency)
            .order_by(HistoricalStats.computed_at.desc())
            .first()
        )
        if not stats:
            return {
                "status": "not_computed",
                "source_currency": source_currency,
                "target_currency": target_currency,
            }

        return {
            "status": "found",
            "source_currency": stats.source_currency,
            "target_currency": stats.target_currency,
            "window_days": stats.window_days,
            "computed_at": stats.computed_at.isoformat(),
            "mean_rate": stats.mean_rate,
            "std_rate": stats.std_rate,
            "min_rate": stats.min_rate,
            "max_rate": stats.max_rate,
            "percentiles": stats.percentiles,
            "trend_30d_slope": stats.trend_30d_slope,
            "trend_90d_slope": stats.trend_90d_slope,
            "annualised_volatility": stats.annualised_volatility,
            "summary": stats.summary,
        }


def compute_and_save_stats(source_currency: str, target_currency: str) -> dict:
    """Compute historical statistics from stored ECB data and cache them.

    Calculates: mean, std, min, max, percentiles, trend slopes, volatility.
    Uses all available ECB historical data for this currency pair.

    Args:
        source_currency: From currency (e.g. 'EUR').
        target_currency: To currency (e.g. 'CNY').

    Returns:
        dict with computed statistics.
    """
    import json
    import statistics
    from app.database import SessionLocal
    from app.models import DailyRate, HistoricalStats

    with SessionLocal() as db:
        rows = (
            db.query(DailyRate)
            .filter_by(source_currency=source_currency, target_currency=target_currency)
            .filter(DailyRate.source == "ECB")
            .order_by(DailyRate.recorded_at.asc())
            .all()
        )

    if len(rows) < 30:
        return {
            "status": "insufficient_data",
            "record_count": len(rows),
            "message": f"Need at least 30 ECB records, got {len(rows)}.",
        }

    rates = [float(r.rate) for r in rows]
    n = len(rates)

    # Basic stats
    mean_rate = statistics.mean(rates)
    std_rate = statistics.stdev(rates)
    min_rate = min(rates)
    max_rate = max(rates)

    # Percentiles
    sorted_rates = sorted(rates)
    def pct(p: float) -> float:
        idx = (p / 100) * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return sorted_rates[lo] + (idx - lo) * (sorted_rates[hi] - sorted_rates[lo])

    percentiles = {
        "p10": pct(10), "p25": pct(25), "p50": pct(50),
        "p75": pct(75), "p90": pct(90),
    }

    # Trend slope (linear regression) for last 30 and 90 days
    def linear_slope(vals: list[float]) -> float:
        n_v = len(vals)
        if n_v < 2:
            return 0.0
        x_mean = (n_v - 1) / 2
        y_mean = sum(vals) / n_v
        num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
        den = sum((i - x_mean) ** 2 for i in range(n_v))
        return num / den if den != 0 else 0.0

    trend_30d_slope = linear_slope(rates[-30:])
    trend_90d_slope = linear_slope(rates[-90:]) if n >= 90 else linear_slope(rates)

    # Annualised volatility (daily log returns × sqrt(252))
    log_returns = [math.log(rates[i] / rates[i - 1]) for i in range(1, n) if rates[i - 1] > 0]
    daily_vol = statistics.stdev(log_returns) if len(log_returns) > 1 else 0.0
    annualised_volatility = daily_vol * math.sqrt(252) * 100  # as percentage

    summary = {
        "currency_pair": f"{source_currency}/{target_currency}",
        "period": f"{rows[0].recorded_at.date()} to {rows[-1].recorded_at.date()}",
        "data_points": n,
        "mean_rate": round(mean_rate, 4),
        "std_rate": round(std_rate, 4),
        "min_rate": round(min_rate, 4),
        "max_rate": round(max_rate, 4),
        "percentiles": {k: round(v, 4) for k, v in percentiles.items()},
        "trend_30d": "rising" if trend_30d_slope > 0.0001 else ("falling" if trend_30d_slope < -0.0001 else "flat"),
        "trend_90d": "rising" if trend_90d_slope > 0.0001 else ("falling" if trend_90d_slope < -0.0001 else "flat"),
        "annualised_volatility_pct": round(annualised_volatility, 2),
    }

    # Persist
    with SessionLocal() as db:
        # Use window_days=0 as a sentinel meaning "all available data"
        # This ensures we always upsert the same row rather than accumulating
        # one row per distinct record count as historical data grows.
        existing = (
            db.query(HistoricalStats)
            .filter_by(source_currency=source_currency, target_currency=target_currency, window_days=0)
            .first()
        )
        if not existing:
            existing = HistoricalStats(
                source_currency=source_currency,
                target_currency=target_currency,
                window_days=0,
            )
            db.add(existing)

        existing.computed_at = datetime.now(timezone.utc)
        existing.mean_rate = mean_rate
        existing.std_rate = std_rate
        existing.min_rate = min_rate
        existing.max_rate = max_rate
        existing.percentiles_json = json.dumps({k: float(v) for k, v in percentiles.items()})
        existing.trend_30d_slope = trend_30d_slope
        existing.trend_90d_slope = trend_90d_slope
        existing.annualised_volatility = annualised_volatility
        existing.summary_json = json.dumps(summary)
        db.commit()

    return {"status": "computed", **summary}


def save_rate_score(
    chat_id: str,
    source_currency: str,
    target_currency: str,
    current_rate: float,
    rate_source: str,
    scores: dict,
    composite_score: float,
    rationale: str,
    alert_sent: bool,
) -> dict:
    """Persist an AI scoring result.

    Args:
        chat_id: Telegram chat_id.
        source_currency: From currency.
        target_currency: To currency.
        current_rate: The rate that was scored.
        rate_source: 'BOC' or 'ECB'.
        scores: Dict with keys: historical_percentile, short_term_trend, mean_reversion, user_goal, volatility.
        composite_score: Weighted composite score (0-100).
        rationale: Short human-readable explanation.
        alert_sent: Whether a Telegram alert was triggered.

    Returns:
        dict with 'status' and 'id'.
    """
    from app.database import SessionLocal
    from app.models import RateScore

    with SessionLocal() as db:
        record = RateScore(
            chat_id=chat_id,
            scored_at=datetime.now(timezone.utc),
            source_currency=source_currency,
            target_currency=target_currency,
            current_rate=current_rate,
            rate_source=rate_source,
            score_historical_percentile=scores.get("historical_percentile", 0),
            score_short_term_trend=scores.get("short_term_trend", 0),
            score_mean_reversion=scores.get("mean_reversion", 0),
            score_user_goal=scores.get("user_goal", 0),
            score_volatility=scores.get("volatility", 0),
            composite_score=composite_score,
            rationale=rationale,
            alert_sent=alert_sent,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return {"status": "saved", "id": record.id}


def get_recent_scores(chat_id: str, source_currency: str, target_currency: str, days: int) -> dict:
    """Get recent scoring history for a currency pair.

    Args:
        chat_id: Telegram chat_id.
        source_currency: From currency.
        target_currency: To currency.
        days: Look back this many days.

    Returns:
        dict with 'scores' list and 'count'.
    """
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import RateScore

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    with SessionLocal() as db:
        rows = (
            db.query(RateScore)
            .filter_by(chat_id=chat_id, source_currency=source_currency, target_currency=target_currency)
            .filter(RateScore.scored_at >= cutoff)
            .order_by(RateScore.scored_at.desc())
            .all()
        )

    scores = [
        {
            "scored_at": r.scored_at.isoformat(),
            "current_rate": float(r.current_rate),
            "composite_score": float(r.composite_score),
            "alert_sent": r.alert_sent,
        }
        for r in rows
    ]

    return {"status": "found", "count": len(scores), "scores": scores}
