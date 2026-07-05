"""
FX Monitor AI — Root Agent (ADK 2.0 Workflow)

Architecture:
  START → detect_user_state
    ├── "new_user"    → onboarding_agent (HITL multi-turn setup)
    ├── "active"      → handle_chat_agent (day-to-day Q&A)
    └── "scheduled"   → daily_score_workflow (autonomous scoring)

The Workflow uses conditional routing (Event.route) to pick the right
sub-agent or sub-workflow based on session state, making the system
robust and deterministic rather than prompt-driven.
"""

from __future__ import annotations

import json

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.workflow import Workflow

from app.config import get_model
from app.tools import fetch_boc_rate, get_user_profile, save_user_profile, update_currency_intent, add_monitored_currency, remove_monitored_currency, update_base_currency
from app.workflows.onboarding_workflow import onboarding_workflow
from app.workflows.daily_score_workflow import daily_score_workflow
from app.workflows.report_workflow import report_workflow


# ─────────────────────────────────────────────────────────────────────────────
# Router node — reads session state to decide which path to take
# ─────────────────────────────────────────────────────────────────────────────

def detect_user_state(ctx: Context, node_input) -> Event:
    """Route based on context: new user setup, scheduled task, or active chat."""
    chat_id = ctx.state.get("chat_id", "")
    if not chat_id:
        chat_id = "playground_user"
        ctx.state["chat_id"] = chat_id

    trigger = ctx.state.get("trigger", "chat")  # "chat" | "scheduled" | "new_currency" | "briefing" | "status_request"
    ctx.state["trigger"] = trigger

    if trigger == "scheduled" or trigger == "new_currency" or trigger == "status_request":
        return Event(output=str(node_input), route="scheduled")

    if trigger == "briefing":
        return Event(output=str(node_input), route="briefing")

    # Check if user is configured
    if chat_id:
        profile = get_user_profile(chat_id)
        if profile.get("status") == "not_found" or profile.get("onboarding_status") == "new":
            ctx.state["user_profile"] = json.dumps(profile)
            return Event(output=str(node_input), route="onboarding")

        ctx.state["user_profile"] = json.dumps(profile)
        return Event(output=str(node_input), route="chat")

    ctx.state["user_profile"] = json.dumps({"status": "not_configured"})
    return Event(output=str(node_input), route="chat")


# ─────────────────────────────────────────────────────────────────────────────
# Chat agent — handles day-to-day user questions
# ─────────────────────────────────────────────────────────────────────────────

chat_agent = LlmAgent(
    name="chat_agent",
    model=get_model(),
    description="Handles everyday user questions about FX rates, their settings, and scores.",
    instruction="""You are a professional FX assistant. Your task is to help the user understand currency rates and provide buying advice.

User profile configurations (current session):
{user_profile}

Your current chat_id is: {chat_id}

Your abilities and execution rules:

1. **Answer Exchange Rate & Analysis Questions**: Answer questions about currency exchange rates, scoring logic, and purchasing advice. All financial info is for reference only and not investment advice. When stating rate timestamps, explicitly mention it is in "Beijing Time" (CST) to avoid timezone confusion.

2. **Modify/Save Configurations (Core Rules)**:
   Configurations for each currency are stored independently under `{user_profile}`'s `purchase_intent` keyed by currency code.
   - **Update Currency Intent**: When the user wants to update settings for a specific tracked currency (e.g. purpose, amount, time horizon, risk tolerance, target rate), you MUST call the `update_currency_intent` tool.
     * **Budget/Amount**: Directly record in units of the target currency (e.g., "buy 5000 EUR" -> amount=5000). Do NOT convert to CNY!
     * **Target Rate**: Set target exchange rate (e.g., target_rate=7.50). Pass target_rate=0.0 to disable.
     * **Time Horizon Mapping**:
       - Within 1 month (e.g., "soon", "next week") -> "short"
       - 1 to 6 months (e.g., "mid-term", "3 months") -> "medium"
       - 6 months or more (e.g., "long-term", "6-12 months") -> "long"
     * **Adopt Suggestion**: If the user agrees to your suggestion (e.g. "go ahead with that suggestion", "ok, change it"), **you MUST immediately call the corresponding tool (like `update_currency_intent`) to persist settings to the database! Do not just reply verbally.**
   - **Update Base Currency**: When the user wants to change the base currency they hold (e.g. "change base currency to USD", "buy EUR using USD"), you MUST call `update_base_currency`.
   - **Add Monitored Currency**: When the user requests to track/add a new currency (e.g., "track USD"), call `add_monitored_currency`.
   - **Remove Monitored Currency**: When the user requests to stop tracking a currency (e.g., "stop tracking GBP"), call `remove_monitored_currency`.
   - **Confirm Save**: Once successfully saved, confirm to the user that settings are saved.

3. **Query Real-time Rates**: When the user asks for the current rate, or you need to inspect the market price, call the `fetch_boc_rate` tool. Never estimate or guess exchange rates.

4. Maintain a professional and helpful tone. Reply in English. When answering questions about their current settings, **you MUST strictly read values from the `{user_profile}` above, and report honestly if not set. Never hallucinate, guess, or reference any missing fields (like Notes).**
""",
    tools=[
        get_user_profile,
        fetch_boc_rate,
        update_currency_intent,
        update_base_currency,
        add_monitored_currency,
        remove_monitored_currency,
    ],
)


root_agent = Workflow(
    name="fx_monitor_root",
    description="FX Monitor AI root workflow — routes between onboarding, chat, scoring, and reporting.",
    edges=[
        ("START", detect_user_state),
        (detect_user_state, {
            "onboarding": onboarding_workflow,
            # Both "scheduled" and "new_currency" trigger the same scoring workflow
            "scheduled": daily_score_workflow,
            "briefing": report_workflow,
            "chat": chat_agent,
        }),
        # new_currency also triggers scoring (fan-out from routing map not possible for same target)
        # Handled by detect_user_state mapping "new_currency" → "scheduled" internally
    ],
)


app = App(
    name="app",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
