"""
Daily Score Workflow — autonomous scoring run triggered by scheduler.

Flow:
  START → load_user_configs
    ↓ (for each user)
  fetch_realtime_rates     ← BOC scrape (or other RateSource)
    ↓
  [score_historical] [score_trend] [score_mean_rev] [score_goal] [score_vol]  ← parallel
    ↓
  aggregate_scores         ← weighted composite
    ↓ (conditional)
  ≥ threshold → send_alert → save_score_record
  < threshold → save_score_record

This workflow runs 3× per day (09:00 / 15:30 / 21:00 CST).
It is also triggered when a user adds a new currency.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import JoinNode, Workflow

from app.config import get_model
from app.schemas import ScoringReport
from app.tools import (
    fetch_boc_rate,
    get_historical_stats,
    get_user_profile,
    save_daily_rate,
    save_rate_score,
)


# Monkey patch to bypass structured output EOF loop bug with LiteLLM proxy endpoints.
# Only applied when MODEL_PROVIDER is "litellm" to avoid ImportError on other providers.
try:
    from app.config import MODEL_PROVIDER
    if MODEL_PROVIDER == "litellm":
        import google.adk.models.lite_llm as adk_lite_llm
        from pydantic import BaseModel
        from typing import Any

        original_to_litellm_format = adk_lite_llm._to_litellm_response_format

        def custom_to_litellm_response_format(response_schema: Any, model: str) -> dict[str, Any]:
            res = original_to_litellm_format(response_schema, model)
            schema_name = ""
            if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
                schema_name = response_schema.__name__
            elif isinstance(response_schema, dict):
                schema_name = response_schema.get("title", "")

            if schema_name == "ScoringReport" and isinstance(res, dict) and "json_schema" in res:
                res["json_schema"]["strict"] = False
            return res

        adk_lite_llm._to_litellm_response_format = custom_to_litellm_response_format
except (ImportError, AttributeError):
    pass  # LiteLLM not available or not the active provider — no patch needed


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions for deterministic score calculation
# ─────────────────────────────────────────────────────────────────────────────

import math as _math


def estimate_percentile(rate: float, stats: dict) -> float:
    """Estimate the percentile ranking of current rate using cached percentile values."""
    min_rate = stats.get("min_rate", rate * 0.85)
    max_rate = stats.get("max_rate", rate * 1.15)
    p_map = [
        (0.0, min_rate),
        (10.0, stats.get("percentiles", {}).get("p10", min_rate)),
        (25.0, stats.get("percentiles", {}).get("p25", min_rate)),
        (50.0, stats.get("percentiles", {}).get("p50", (min_rate + max_rate) / 2)),
        (75.0, stats.get("percentiles", {}).get("p75", max_rate)),
        (90.0, stats.get("percentiles", {}).get("p90", max_rate)),
        (100.0, max_rate)
    ]
    p_map.sort(key=lambda x: x[1])

    if rate <= p_map[0][1]:
        return 0.0
    if rate >= p_map[-1][1]:
        return 100.0

    for i in range(len(p_map) - 1):
        p_low, r_low = p_map[i]
        p_high, r_high = p_map[i+1]
        if r_low <= rate <= r_high:
            if abs(r_high - r_low) < 1e-6:
                return p_low
            ratio = (rate - r_low) / (r_high - r_low)
            return p_low + (p_high - p_low) * ratio
    return 50.0


def calc_trend_score(slope: float, std_rate: float) -> float:
    """Calculate trend score as a continuous function of normalised slope.

    Uses a sigmoid-like mapping: falling trend (slope < 0) favours buyers → higher score.
    Score range: [20, 80]. Normalised by std_rate to be currency-agnostic.
    """
    if std_rate <= 0:
        std_rate = 0.1
    # Normalise: slope / std_rate gives a rate-independent signal strength
    # A slope of 1 std_dev per day is an extreme move; realistic daily slope << 0.01
    # Scale factor: map ±0.01 normalised slope to ±30 score change
    normalised = slope / std_rate
    # Clamp to prevent extreme values dominating
    normalised = max(-0.05, min(0.05, normalised))
    # Falling (negative slope) → score above 50; Rising → below 50
    score = 50.0 - (normalised / 0.05) * 30.0
    return round(max(20.0, min(80.0, score)), 1)


def calc_mean_reversion(deviation: float) -> float:
    """Calculate mean reversion score based on standard deviation distance.

    Score range: [0, 100]. Rate well below the mean (cheap) → high score.
    """
    # Clamp deviation to [-3, 3] std devs
    deviation = max(-3.0, min(3.0, deviation))
    # Linear map: deviation -3 → 100, 0 → 50, +3 → 0
    score = 50.0 - (deviation / 3.0) * 50.0
    return round(max(0.0, min(100.0, score)), 1)


def calc_user_goal(rate: float, target_rate: float | None) -> float:
    """Calculate user goal score based on ratio to target price.

    Score range: [0, 100]. Rate at or below target → full score.
    """
    if not target_rate:
        return 55.0
    ratio = rate / target_rate
    if ratio <= 1.0:
        return 100.0
    elif ratio <= 1.02:
        # Linearly interpolate from 100 to 75 as rate rises to 2% above target
        r = (ratio - 1.0) / 0.02
        return round(100.0 - 25.0 * r, 1)
    elif ratio <= 1.05:
        # 75 → 35 as rate rises from 2% to 5% above target
        r = (ratio - 1.02) / 0.03
        return round(75.0 - 40.0 * r, 1)
    else:
        # Beyond 5% above target, sharply declining
        r = min((ratio - 1.05) / 0.05, 1.0)
        return round(35.0 - 35.0 * r, 1)


def calc_volatility(vol: float, std_rate: float = 0.0) -> float:
    """Calculate volatility score (lower volatility = higher score).

    Score range: [0, 100]. Uses smooth continuous interpolation.
    vol: annualised volatility in percent (e.g. 8.0 = 8%).
    """
    # Clamp to [0, 30] percent range
    vol = max(0.0, min(30.0, vol))
    # Linear interpolation: 0% vol → 100 score; 20%+ vol → 10 score
    if vol <= 20.0:
        score = 100.0 - (vol / 20.0) * 90.0
    else:
        score = 10.0 - ((vol - 20.0) / 10.0) * 10.0
    return round(max(0.0, min(100.0, score)), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring LLM Agent — reads raw data, produces structured scores
# ─────────────────────────────────────────────────────────────────────────────

scoring_llm = LlmAgent(
    name="scoring_llm",
    model=get_model(),
    description="Scores FX rates across 5 dimensions using historical stats and user profile.",
    output_schema=ScoringReport,
    output_key="scoring_report",
    instruction="""You are a professional FX opportunity scoring report generator. Assemble a structured scoring report based on pre-calculated scores and write human-friendly analysis.

Input data (injected from session state):
- realtime_rates: {realtime_rates}
- historical_stats: {historical_stats}
- user_profile: {user_profile}
- precalculated_scores: {precalculated_scores}

**Model Output and Assembly Rules**:
1. **Adopt Precalculated Scores**: You MUST adopt the exact scores from precalculated_scores (dimension scores, composite_score, and alert_worthy). Do NOT modify or recalculate them!
2. **Rationale Guidelines**:
   - The rationale for each score must be a concise sentence in professional English (e.g. "Current rate 7.79 is at the 25th historical percentile, presenting an attractive buying opportunity.").
   - Do NOT include any calculation steps or linear interpolation formulas in the rationale. Write the final result directly.
3. **Summary Guidelines**: Write a 2-3 sentence summary in English explaining why the rate is or is not favorable for purchase.
""",
    tools=[get_historical_stats],
)


# ─────────────────────────────────────────────────────────────────────────────
# Workflow nodes
# ─────────────────────────────────────────────────────────────────────────────

def load_score_context(ctx: Context, node_input) -> Event:
    """Load user profile and prepare scoring context."""
    chat_id = ctx.state.get("chat_id", "")

    if not chat_id:
        return Event(output=None, route="skip")

    profile = get_user_profile(chat_id)
    if profile.get("status") == "not_found" or profile.get("onboarding_status") == "new":
        ctx.state["user_profile"] = json.dumps(profile)
        return Event(output=None, route="skip")

    ctx.state["user_profile"] = json.dumps(profile)
    ctx.state["target_currencies"] = json.dumps(profile.get("target_currencies", []))
    ctx.state["source_currency"] = profile.get("source_currency", "CNY")
    ctx.state["scoring_weights"] = json.dumps(profile.get("scoring_weights", {}))
    ctx.state["alert_threshold"] = str(profile.get("alert_threshold", 70))

    return Event(
        output=profile,
        state={
            "user_profile": json.dumps(profile),
            "target_currencies": json.dumps(profile.get("target_currencies", [])),
            "source_currency": profile.get("source_currency", "CNY"),
            "scoring_weights": json.dumps(profile.get("scoring_weights", {})),
            "alert_threshold": profile.get("alert_threshold", 70),
        },
    )


async def fetch_realtime_rates(ctx: Context, node_input) -> Event:
    """Fetch current rates from BOC for all target currencies."""
    targets_json = ctx.state.get("target_currencies", "[]")
    targets = json.loads(targets_json)

    rates = {}
    for currency in targets:
        result = await fetch_boc_rate(currency)
        if result.get("status") == "success":
            rates[currency] = result
            # Persist to DB
            save_daily_rate(
                source_currency=result["source_currency"],
                target_currency=result["target_currency"],
                rate=result["spot_sell"],
                rate_source="BOC",
                rate_type="spot_sell",
            )
        else:
            rates[currency] = {"error": result.get("error", "fetch_failed")}

    return Event(
        output=rates,
        state={"realtime_rates": json.dumps(rates)},
    )


async def load_stats_for_scoring(ctx: Context, node_input) -> Event:
    """Load pre-computed historical stats and pre-calculate deterministic scores."""
    targets_json = ctx.state.get("target_currencies", "[]")
    targets = json.loads(targets_json)
    source_currency = ctx.state.get("source_currency", "CNY")

    weights_json = ctx.state.get("scoring_weights", "{}")
    try:
        weights_dict = json.loads(weights_json)
    except Exception:
        weights_dict = {}

    chat_id = ctx.state.get("chat_id", "")
    user_profile = get_user_profile(chat_id) if chat_id else {}
    purchase_intent_map = user_profile.get("purchase_intent", {})

    realtime_rates_json = ctx.state.get("realtime_rates", "{}")
    realtime_rates = json.loads(realtime_rates_json)

    all_stats = {}
    precalculated = {}
    for currency in targets:
        # Query stats directly for target currency against source currency
        stats = get_historical_stats(currency, source_currency)
        all_stats[currency] = stats

        # Precalculate
        rate_info = realtime_rates.get(currency, {})
        current_rate = rate_info.get("rate") or rate_info.get("spot_sell")

        if stats.get("status") == "found" and current_rate:
            std_rate = stats.get("std_rate") or 0.1

            # Get per-currency weights
            weights = weights_dict.get(currency, {})
            if not weights or not isinstance(weights, dict):
                weights = {
                    "historical_percentile": 0.30,
                    "short_term_trend": 0.25,
                    "mean_reversion": 0.20,
                    "user_goal": 0.15,
                    "volatility": 0.10,
                }

            # Get per-currency purchase intent details
            intent = purchase_intent_map.get(currency, {})
            target_rate = intent.get("target_rate")
            alert_threshold = intent.get("alert_threshold", 70)

            # 1. Historical percentile — inverse: low percentile = cheap = high score
            pct = estimate_percentile(current_rate, stats)
            hist_score = round((1.0 - pct / 100.0) * 100.0, 1)

            # 2. Short-term trend — continuous sigmoid based on normalised slope
            slope = stats.get("trend_30d_slope", 0.0)
            trend_score = calc_trend_score(slope, std_rate)

            # 3. Mean reversion — continuous linear across [-3, +3] std devs
            mean_rate = stats.get("mean_rate") or current_rate
            deviation = (current_rate - mean_rate) / std_rate if std_rate > 0 else 0.0
            mr_score = calc_mean_reversion(deviation)

            # 4. User goal — continuous interpolation around target rate
            goal_score = calc_user_goal(current_rate, target_rate)

            # 5. Volatility — continuous inverse of annualised vol %
            vol = stats.get("annualised_volatility") or 8.0
            vol_score = calc_volatility(vol)

            # Normalise weights so they sum to 1.0 (guard against LLM hallucination)
            w_sum = sum(weights.values())
            if w_sum <= 0:
                w_sum = 1.0

            # Weighted composite — all dimensions now in [0, 100]
            comp_score = round((
                hist_score * weights.get("historical_percentile", 0.30) +
                trend_score * weights.get("short_term_trend", 0.25) +
                mr_score * weights.get("mean_reversion", 0.20) +
                goal_score * weights.get("user_goal", 0.15) +
                vol_score * weights.get("volatility", 0.10)
            ) / w_sum, 1)

            # alert_worthy is determined deterministically here, NOT by the LLM
            is_alert_worthy = comp_score >= alert_threshold

            precalculated[currency] = {
                "current_rate": current_rate,
                "historical_percentile": hist_score,
                "short_term_trend": trend_score,
                "mean_reversion": mr_score,
                "user_goal": goal_score,
                "volatility": vol_score,
                "composite_score": comp_score,
                "alert_worthy": is_alert_worthy,
                # Extra metadata for LLM rationale context (read-only, not to be re-calculated)
                "_meta": {
                    "percentile_rank": round(pct, 1),
                    "slope_30d": round(slope, 6),
                    "deviation_sigma": round(deviation, 2),
                    "vol_pct": round(vol, 2),
                    "target_rate": target_rate,
                    "mean_rate": round(mean_rate, 4),
                }
            }
        else:
            precalculated[currency] = {
                "current_rate": current_rate or 0.0,
                "historical_percentile": 50.0,
                "short_term_trend": 50.0,
                "mean_reversion": 50.0,
                "user_goal": 50.0,
                "volatility": 50.0,
                "composite_score": 50.0,
                "alert_worthy": False
            }

    return Event(
        output=all_stats,
        state={
            "historical_stats": json.dumps(all_stats),
            "precalculated_scores": json.dumps(precalculated),
        },
    )


def check_should_alert(ctx: Context, node_input) -> Event:
    """Check scoring report and determine if alert should be sent.

    IMPORTANT: alert_worthy is re-derived from the deterministic precalculated_scores,
    NOT from the LLM's scoring_report output, to eliminate any possibility of
    alert hallucination or LLM-introduced bias.
    """
    report_raw = ctx.state.get("scoring_report", {})

    if isinstance(report_raw, str):
        try:
            report_raw = json.loads(report_raw)
        except json.JSONDecodeError:
            report_raw = {}

    # Re-derive alert_worthy from deterministic precalculated_scores (source of truth)
    precalc_raw = ctx.state.get("precalculated_scores", "{}")
    if isinstance(precalc_raw, str):
        try:
            precalculated = json.loads(precalc_raw)
        except json.JSONDecodeError:
            precalculated = {}
    else:
        precalculated = precalc_raw

    # Build alert list from deterministic scores, not LLM output
    scores = report_raw.get("scores", [])
    alert_currencies = [
        s for s in scores
        if precalculated.get(s.get("target_currency", ""), {}).get("alert_worthy", False)
    ]

    trigger = ctx.state.get("trigger", "scheduled")
    if trigger == "status_request":
        return Event(
            output=report_raw,
            state={"alert_currencies": json.dumps(alert_currencies)},
            route="status_report",
        )

    return Event(
        output=report_raw,
        state={"alert_currencies": json.dumps(alert_currencies)},
        route="alert" if alert_currencies else "no_alert",
    )


async def send_status_report(ctx: Context, node_input) -> Event:
    """Send real-time score report to the user in the Telegram chat."""
    from app.telegram_bot.sender import send_message_to_user

    chat_id = ctx.state.get("chat_id", "")
    report_raw = ctx.state.get("scoring_report", {})

    if isinstance(report_raw, str):
        try:
            report_raw = json.loads(report_raw)
        except json.JSONDecodeError:
            report_raw = {}

    scores = report_raw.get("scores", [])
    if not chat_id or not scores:
        return Event(output={"sent": False})

    lines = ["📊 **Real-Time FX Opportunity Scores**\n"]
    for score_data in scores:
        currency = score_data.get("target_currency", "")
        rate = score_data.get("current_rate", 0)
        composite = score_data.get("composite_score", 0)
        summary = score_data.get("summary", "")

        emoji = "🟢" if composite >= 70 else ("🟡" if composite >= 55 else "🔴")
        lines.append(f"{emoji} **{score_data.get('source_currency', 'EUR')}/{currency}**")
        lines.append(f"Current Rate: {rate:.4f} | **Composite Score: {composite:.1f}/100**\n")
        
        # Dimensions breakdown
        lines.append("**Dimension Breakdown**:")
        dims = [
            ("historical_percentile", "Historical Percentile"),
            ("short_term_trend", "Short-Term Trend"),
            ("mean_reversion", "Mean Reversion"),
            ("user_goal", "Target Rate Match"),
            ("volatility", "Volatility Safety")
        ]
        for key, label in dims:
            dim_data = score_data.get(key, {})
            if isinstance(dim_data, dict):
                score_val = dim_data.get("score", 0)
                rationale = dim_data.get("rationale", "")
                lines.append(f"• **{label}**: {score_val:.1f}/100 — {rationale}")
            else:
                lines.append(f"• **{label}**: No details available")

        lines.append(f"\n**AI Recommendation**:\n{summary}")
        lines.append("")

    lines.append("_Scores synchronized and saved to database._")
    message = "\n".join(lines)

    try:
        await send_message_to_user(chat_id, message)
        sent = True
    except Exception as e:
        sent = False
        print(f"[Status] Failed to send Telegram status to {chat_id}: {e}")

    return Event(output={"sent": sent, "chat_id": chat_id})


async def send_telegram_alert(ctx: Context, node_input) -> Event:
    """Send Telegram notification for high-scoring opportunities."""
    from app.telegram_bot.sender import send_message_to_user

    chat_id = ctx.state.get("chat_id", "")
    alert_currencies = json.loads(ctx.state.get("alert_currencies", "[]"))

    if not chat_id or not alert_currencies:
        return Event(output={"sent": False})

    # Build alert message
    lines = ["🔔 **FX Purchase Opportunity Alert**\n"]

    for score_data in alert_currencies:
        currency = score_data.get("target_currency", "")
        rate = score_data.get("current_rate", 0)
        composite = score_data.get("composite_score", 0)
        summary = score_data.get("summary", "")

        lines.append(f"**{score_data.get('source_currency', 'EUR')}/{currency}**")
        lines.append(f"Current Rate: {rate:.4f} | **Composite Score: {composite:.1f}/100**\n")

        # Dimensions breakdown
        lines.append("**Dimension Breakdown**:")
        dims = [
            ("historical_percentile", "Historical Percentile"),
            ("short_term_trend", "Short-Term Trend"),
            ("mean_reversion", "Mean Reversion"),
            ("user_goal", "Target Rate Match"),
            ("volatility", "Volatility Safety")
        ]
        for key, label in dims:
            dim_data = score_data.get(key, {})
            if isinstance(dim_data, dict):
                score_val = dim_data.get("score", 0)
                rationale = dim_data.get("rationale", "")
                lines.append(f"• **{label}**: {score_val:.1f}/100 — {rationale}")
            else:
                lines.append(f"• **{label}**: No details available")

        lines.append(f"\n**AI Recommendation**:\n{summary}")
        lines.append("")

    lines.append("⚠️ Generated by AI for reference only. Not financial advice.")

    message = "\n".join(lines)

    try:
        await send_message_to_user(chat_id, message)
        sent = True
    except Exception as e:
        sent = False
        print(f"[Alert] Failed to send Telegram message to {chat_id}: {e}")

    return Event(output={"sent": sent, "chat_id": chat_id})


def persist_scores(ctx: Context, node_input) -> Event:
    """Persist all scores to the database."""
    report_raw = ctx.state.get("scoring_report", {})
    chat_id = ctx.state.get("chat_id", "")
    alert_currencies_json = ctx.state.get("alert_currencies", "[]")
    alert_currency_codes = {
        s["target_currency"] for s in json.loads(alert_currencies_json)
    }

    if isinstance(report_raw, str):
        try:
            report_raw = json.loads(report_raw)
        except json.JSONDecodeError:
            report_raw = {}

    persisted = []
    for score in report_raw.get("scores", []):
        dim_scores = {
            "historical_percentile": score.get("historical_percentile", {}).get("score", 0),
            "short_term_trend": score.get("short_term_trend", {}).get("score", 0),
            "mean_reversion": score.get("mean_reversion", {}).get("score", 0),
            "user_goal": score.get("user_goal", {}).get("score", 0),
            "volatility": score.get("volatility", {}).get("score", 0),
        }
        target = score.get("target_currency", "")
        result = save_rate_score(
            chat_id=chat_id,
            source_currency=score.get("source_currency", "EUR"),
            target_currency=target,
            current_rate=score.get("current_rate", 0),
            rate_source=score.get("rate_source", "BOC"),
            scores=dim_scores,
            composite_score=score.get("composite_score", 0),
            rationale=score.get("summary", ""),
            alert_sent=target in alert_currency_codes,
        )
        persisted.append(result)

    return Event(output={"persisted": len(persisted)})


def skip_inactive_user(ctx: Context, node_input) -> str:
    """No-op for users not yet configured."""
    return "User not configured — skipping score run."


# ─────────────────────────────────────────────────────────────────────────────
# Daily Score Workflow
# ─────────────────────────────────────────────────────────────────────────────

daily_score_workflow = Workflow(
    name="daily_score_workflow",
    description="Autonomous daily scoring: fetch BOC rates → AI score → alert if worthy → persist.",
    edges=[
        ("START", load_score_context),
        # Single conditional edge: 'skip' route goes to skip_inactive_user,
        # all other routes (default) proceed to fetch_realtime_rates.
        # Using __DEFAULT__ avoids the dual-edge routing ambiguity.
        (load_score_context, {"skip": skip_inactive_user, "__DEFAULT__": fetch_realtime_rates}),
        (fetch_realtime_rates, load_stats_for_scoring),
        (load_stats_for_scoring, scoring_llm),
        (scoring_llm, check_should_alert),
        # Conditional routing for alert vs status_report vs no-alert
        (check_should_alert, {
            "alert": send_telegram_alert,
            "status_report": send_status_report,
            "no_alert": persist_scores
        }),
        # After alert or status report is sent, also persist
        (send_telegram_alert, persist_scores),
        (send_status_report, persist_scores),
    ],
)
