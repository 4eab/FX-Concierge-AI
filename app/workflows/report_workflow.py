"""
Report Workflow — generates detailed FX briefing report on user request.

Triggered by:
  1. User choice at end of onboarding ("是否需要简报？")
  2. User sending /briefing command
  3. trigger="briefing" in session state

Flow:
  START → load_analysis_data → generate_briefing_llm → format_and_send
"""

from __future__ import annotations

import json

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import Workflow

from app.config import get_model
from app.schemas import BriefingReport
from app.tools import fetch_boc_rate, get_historical_stats, get_recent_scores, get_user_profile


# ─────────────────────────────────────────────────────────────────────────────
# Briefing LLM Agent
# ─────────────────────────────────────────────────────────────────────────────

briefing_llm = LlmAgent(
    name="briefing_llm",
    model=get_model(),
    description="Generates a comprehensive FX opportunity briefing report.",
    output_schema=BriefingReport,
    output_key="briefing_report",
    instruction="""You are a professional FX analyst. Generate a detailed FX briefing report in English for the user.

Input data (injected from session state):
- realtime_rates: {realtime_rates}
- historical_stats_all: {historical_stats_all}
- recent_scores: {recent_scores}
- user_profile: {user_profile}

**Report Structure (BriefingReport schema)**:

1. **title**: Briefing title with date, e.g. "EUR/CNY Purchase Opportunity Briefing — July 3, 2026"
2. **executive_summary**: A 2-3 sentence executive summary:
   - General market environment
   - Best opportunities (if any)
   - Critical risks
3. **currency_sections**: One analysis section per tracked currency. Each section must include:
   - Current rate vs. historical mean and low (using real-time rates from realtime_rates)
   - Historical percentile breakdown (e.g. current rate at 25th percentile, meaning 75% of historical data was more expensive)
   - Recent momentum/trend (30-day and 90-day slopes)
   - Volatility levels
   - AI score interpretation and recommendation
4. **action_items**: Up to 3 concrete action items in English (e.g. "EUR/CNY is at a historical low; consider buying in tranches").
5. **disclaimer**: Use default English disclaimer.

Language: Professional English. Support all assertions with data. Avoid vague statements.
""",
    tools=[get_historical_stats, get_recent_scores],
)


# ─────────────────────────────────────────────────────────────────────────────
# Workflow nodes
# ─────────────────────────────────────────────────────────────────────────────

async def load_briefing_data(ctx: Context, node_input) -> Event:
    """Load all data needed for the briefing including real-time rates and stats."""
    chat_id = ctx.state.get("chat_id", "")
    profile = get_user_profile(chat_id)

    if profile.get("status") == "not_found":
        return Event(
            output={"error": "User not configured"},
            route="error",
        )

    target_currencies = profile.get("target_currencies", [])
    source_currency = profile.get("source_currency", "CNY")

    # Load stats and realtime rates for all target currencies
    all_stats = {}
    realtime_rates = {}
    for currency in target_currencies:
        stats = get_historical_stats(currency, source_currency)
        all_stats[currency] = stats

        # Fetch current real-time rate (from BOC)
        rate_res = await fetch_boc_rate(currency)
        realtime_rates[currency] = rate_res

    # Load recent scoring history
    recent = {}
    for currency in target_currencies:
        scores = get_recent_scores(chat_id, currency, source_currency, days=7)
        recent[currency] = scores

    return Event(
        output={
            "profile": profile,
            "stats": all_stats,
            "realtime_rates": realtime_rates,
            "recent_scores": recent,
        },
        state={
            "user_profile": json.dumps(profile),
            "historical_stats_all": json.dumps(all_stats),
            "realtime_rates": json.dumps(realtime_rates),
            "recent_scores": json.dumps(recent),
        },
    )


async def send_briefing(ctx: Context, node_input) -> Event:
    """Format and send briefing report via Telegram."""
    from app.telegram_bot.sender import send_message_to_user

    chat_id = ctx.state.get("chat_id", "")
    report_raw = ctx.state.get("briefing_report", {})

    if isinstance(report_raw, str):
        try:
            report_raw = json.loads(report_raw)
        except json.JSONDecodeError:
            report_raw = {}

    # Build message
    parts = []
    parts.append(f"📊 **{report_raw.get('title', 'FX Briefing Report')}**\n")
    parts.append(report_raw.get("executive_summary", ""))
    parts.append("")

    for section in report_raw.get("currency_sections", []):
        parts.append(section)
        parts.append("")

    if report_raw.get("action_items"):
        parts.append("**💡 Action Items**")
        for item in report_raw.get("action_items", []):
            parts.append(f"• {item}")
        parts.append("")

    parts.append(f"_{report_raw.get('disclaimer', '')}_")

    message = "\n".join(parts)

    # Telegram has 4096 char limit — split if needed
    if len(message) > 4000:
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    else:
        chunks = [message]

    sent = False
    try:
        for chunk in chunks:
            await send_message_to_user(chat_id, chunk, parse_mode="Markdown")
        sent = True
    except Exception as e:
        print(f"[Report] Failed to send briefing to {chat_id}: {e}")

    return Event(output={"sent": sent, "chunks": len(chunks)})


def handle_briefing_error(ctx: Context, node_input) -> str:
    """Handle case where user is not yet configured."""
    return "Please complete the initial onboarding via /start first before requesting a briefing."


# ─────────────────────────────────────────────────────────────────────────────
# Report Workflow
# ─────────────────────────────────────────────────────────────────────────────

report_workflow = Workflow(
    name="report_workflow",
    description="Generates and delivers a comprehensive FX briefing report to the user.",
    edges=[
        ("START", load_briefing_data),
        (load_briefing_data, {
            "error": handle_briefing_error,
            "__DEFAULT__": briefing_llm,
        }),
        (briefing_llm, send_briefing),
    ],
)
