"""
Onboarding Workflow — multi-turn HITL setup for new users.

Flow:
  START → greet_and_collect → validate_and_confirm → save_profile → fetch_history → analyse_history

Uses RequestInput (HITL) for multi-turn conversation to collect:
  1. Source currency (e.g. CNY)
  2. Target currencies (e.g. EUR, USD)
  3. Purchase intent (amount, timeline, purpose, risk)
  4. Derived scoring weights + alert threshold

After collection, automatically:
  - Fetches ECB historical data for all target currencies
  - Computes statistical baselines
  - Offers briefing to user
"""

from __future__ import annotations

import json

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow

from app.config import get_model
from app.workflows.report_workflow import report_workflow
from app.schemas import OnboardingResult
from app.tools import (
    get_user_profile,
    save_user_profile,
    compute_scoring_weights,
    compute_alert_threshold,
    ensure_currency_history_and_stats,
)


# ─────────────────────────────────────────────────────────────────────────────
onboarding_llm = LlmAgent(
    name="onboarding_llm",
    model=get_model(),
    description="Collects user FX monitoring preferences through conversation.",
    output_schema=OnboardingResult,
    output_key="onboarding_result",
    instruction="""You are the onboarding guide for the FX Monitor AI assistant. Your task is to collect the user's FX purchase requirements through a friendly conversation and output a structured JSON configuration.

The input text contains the user's "Currency Selection" and "Purchase Needs". Your tasks:

1. **Determine Source Currency**: The base currency the user holds, e.g. "CNY".
2. **Determine Target Currencies List**: The currencies they want to track/buy. Map currency names to standard 3-digit ISO codes:
   - Euro -> "EUR", US Dollar -> "USD", British Pound -> "GBP", Japanese Yen -> "JPY"
   - Hong Kong Dollar -> "HKD", Canadian Dollar -> "CAD", Australian Dollar -> "AUD", Swiss Franc -> "CHF", Singapore Dollar -> "SGD"
   - Output must be a list, e.g. ["EUR"], even if there is only one.
3. **Collect Purchase Needs** (configure independently per target currency):
   - Purpose: (e.g., travel, study, investment, remittance).
   - Budget/Amount: Record directly in target currency units (e.g., 5000 for 5000 EUR). Do NOT convert to CNY! 0 if unknown.
   - Time Horizon mapping (very important):
     * Within 1 month (e.g. "soon", "in two weeks") -> "short"
     * 1 to 6 months (e.g. "mid-term", "3 months") -> "medium"
     * 6 months and above (e.g. "6-12 months", "next year", "long-term") -> "long"
   - Risk Tolerance:
     * Conservative (willing to buy when rate is acceptable) -> "conservative"
     * Moderate (seeking reasonable price) -> "moderate"
     * Aggressive (willing to wait for best rate) -> "aggressive"
   - Target Rate: Target exchange rate (e.g. 7.50). null if not specified.
4. **scoring_weights & alert_threshold**: These will be calculated automatically by Python. Use placeholders:
   - scoring_weights: {"historical_percentile": 0.3, "short_term_trend": 0.25, "mean_reversion": 0.2, "user_goal": 0.15, "volatility": 0.1}
   - alert_threshold: 70
5. **Handle Midway Corrections**:
   - If the user corrects themselves, changes their mind, or specifies a different currency setup in the "Purchase Needs" section compared to the initial "Currency Selection" (e.g. initially said "I hold CNY" but later said "actually buy EUR using USD"), **always prioritize the most recent information in "Purchase Needs" as the source of truth.**
   - Do not output error messages or flags about inconsistency in the summary if the user has updated their choice; silently accept the latest update as the intended setting.

**JSON Output Structure** (OnboardingResult schema):
- source_currency: Base currency code
- target_currencies: Monitored currencies list (e.g., ["EUR"])
- purchase_intents: Map from target currency code (e.g., "EUR") to PurchaseIntent
- scoring_weights: Map from target currency code to the placeholder weights above
- onboarding_summary: A 1-2 sentence English summary of the configuration

If information is incomplete, note what is missing in onboarding_summary, but still output your best estimation.
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# Node: Multi-turn HITL greeting and data collection
# ─────────────────────────────────────────────────────────────────────────────

async def greet_new_user(ctx: Context, node_input):
    """Send welcome message and collect initial user input."""
    # Reset any stale onboarding state to default empty values to prevent session leakage
    ctx.state["initial_response"] = ""
    ctx.state["onboarding_result"] = "{}"
    ctx.state["saved_target_currencies"] = "[]"
    ctx.state["saved_source_currency"] = "CNY"
    ctx.state["history_fetch_results"] = "{}"
    ctx.state["wants_briefing"] = "no"

    yield RequestInput(
        interrupt_id="onboarding_intro",
        message=(
            "👋 Welcome to FX Monitor AI!\n\n"
            "I am your foreign exchange monitoring assistant. I will help you:\n"
            "• Track historical exchange rate trends\n"
            "• Check real-time exchange rates three times a day\n"
            "• Proactively notify you when it's a good buying opportunity\n\n"
            "To get started, please tell me:\n"
            "**What base currency do you hold, and which currencies do you want to track/buy?**\n"
            "(e.g., 'I hold CNY and want to buy EUR and USD')"
        ),
    )


async def collect_purchase_intent(ctx: Context, node_input):
    """Collect purchase intent details after onboarding_intro response is received."""
    # Extract the user's intro response and save to state
    if isinstance(node_input, dict):
        initial = node_input.get("result", "")
    else:
        initial = str(node_input)
    ctx.state["initial_response"] = initial

    yield RequestInput(
        interrupt_id="ask_intent",
        message=(
            "Understood! To tailor the scoring weights for your buying strategy, please tell me:\n\n"
            "1. What is your purchase purpose? (e.g., study, travel, investment)\n"
            "2. How much foreign currency do you plan to buy? (e.g., 5000 EUR, 3000 USD)\n"
            "3. When do you need to complete the purchase?\n"
            "   - Soon (within 1 month)\n"
            "   - Mid-term (1-6 months)\n"
            "   - Long-term (6 months or more)\n"
            "4. What is your price sensitivity / risk appetite?\n"
            "   - Conservative (want to buy soon when rate is decent, low patience)\n"
            "   - Moderate (want to search for a reasonably cheap rate)\n"
            "   - Aggressive (highly patient, willing to wait long for the best price)\n"
            "5. Do you have a specific target rate? (e.g. EUR below 7.50)"
        ),
    )


def prepare_llm_input(ctx: Context, node_input) -> str:
    """Combines user's onboarding_intro response and ask_intent response for the LLM agent."""
    if isinstance(node_input, dict):
        intent_response = node_input.get("result", "")
    else:
        intent_response = str(node_input)
        
    initial = ctx.state.get("initial_response", "")
    combined = f"Currency Selection: {initial}\nPurchase Needs: {intent_response}"
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# Node: Save profile to DB
# ─────────────────────────────────────────────────────────────────────────────

def save_onboarding_result(ctx: Context, node_input) -> Event:
    """Persist the onboarding result to the database.

    ADK 2.0 pattern: LLM collects user data; Python computes all business logic
    (weights, thresholds). This eliminates hallucination risk for critical config.
    """
    chat_id = ctx.state.get("chat_id", "unknown")
    result_raw = ctx.state.get("onboarding_result", {})

    if isinstance(result_raw, str):
        try:
            result_raw = json.loads(result_raw)
        except json.JSONDecodeError:
            result_raw = {}

    source_currency = result_raw.get("source_currency", "CNY")
    target_currencies = result_raw.get("target_currencies", [])
    purchase_intents = result_raw.get("purchase_intents", {})
    summary = result_raw.get("onboarding_summary", "配置已完成")

    # Compute scoring weights and thresholds for each target currency in Python
    scoring_weights = {}
    normalized_intents = {}
    for currency in target_currencies:
        intent = purchase_intents.get(currency, {})
        purpose = intent.get("purpose", "留学/旅游")
        amount = float(intent.get("amount", 0.0))
        time_horizon = intent.get("time_horizon", "medium")
        risk_tolerance = intent.get("risk_tolerance", "moderate")
        target_rate = intent.get("target_rate")
        
        weights = compute_scoring_weights(risk_tolerance, time_horizon)
        threshold = compute_alert_threshold(risk_tolerance)
        
        scoring_weights[currency] = weights
        normalized_intents[currency] = {
            "purpose": purpose,
            "amount": amount,
            "time_horizon": time_horizon,
            "risk_tolerance": risk_tolerance,
            "target_rate": target_rate,
            "alert_threshold": threshold
        }

    alert_threshold = 70
    if target_currencies:
        first_curr = target_currencies[0]
        alert_threshold = normalized_intents[first_curr]["alert_threshold"]

    if target_currencies and chat_id != "unknown":
        save_user_profile(
            chat_id=chat_id,
            source_currency=source_currency,
            target_currencies=target_currencies,
            purchase_intent=normalized_intents,
            scoring_weights=scoring_weights,
            alert_threshold=alert_threshold,
        )

    return Event(
        output={
            "chat_id": chat_id,
            "source_currency": source_currency,
            "target_currencies": target_currencies,
            "summary": summary,
            "scoring_weights": scoring_weights,
            "alert_threshold": alert_threshold,
        },
        state={
            "saved_target_currencies": json.dumps(target_currencies),
            "saved_source_currency": source_currency,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Node: Fetch and store ECB history for configured currencies
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_and_store_history(ctx: Context, node_input) -> Event:
    """Pull ECB history for all target currencies and compute stats.

    Uses ensure_currency_history_and_stats to handle raw rates, cross rates,
    and statistics caching.
    """
    targets_json = ctx.state.get("saved_target_currencies", "[]")
    source = ctx.state.get("saved_source_currency", "CNY")

    try:
        targets = json.loads(targets_json)
    except json.JSONDecodeError:
        targets = []

    results = {}
    for target in targets:
        success = await ensure_currency_history_and_stats(source, target)
        results[target] = {
            "records": "ECB",
            "inserted": "ECB",
            "stats_status": "success" if success else "failed"
        }

    return Event(
        output=results,
        state={"history_fetch_results": json.dumps(results)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Node: Confirm setup and ask if user wants a briefing
# ─────────────────────────────────────────────────────────────────────────────

async def confirm_and_ask_briefing(ctx: Context, node_input) -> Event:
    """Summarize setup completion and offer initial briefing."""
    history_results = json.loads(ctx.state.get("history_fetch_results", "{}"))
    target_currencies = json.loads(ctx.state.get("saved_target_currencies", "[]"))
    source = ctx.state.get("saved_source_currency", "CNY")

    history_summary = []
    for currency, res in history_results.items():
        if "error" in res:
            history_summary.append(f"• {currency}: Failed to load historical data")
        else:
            history_summary.append(f"• {currency}: Loaded historical data successfully")
    
    history_text = "\n".join(history_summary) if history_summary else "(Historical data loading...)"

    yield RequestInput(
        interrupt_id="ask_briefing",
        message=(
            f"✅ **Configuration Completed!**\n\n"
            f"I have configured the following trackers for you:\n"
            f"• Base Currency: {source}\n"
            f"• Tracked Currencies: {', '.join(target_currencies)}\n\n"
            f"Historical Data Status:\n{history_text}\n\n"
            f"Going forward, I will automatically check exchange rates daily at 09:00, 15:30, and 21:00 (Beijing Time), and proactively alert you when scores meet your threshold.\n\n"
            f"**Would you like to view a detailed opportunity briefing based on historical trends?** (Reply 'yes' or 'no')"
        ),
    )
def route_after_briefing_ask(ctx: Context, node_input) -> Event:
    """Helper router node to route and save wants_briefing choice based on resumption input."""
    if isinstance(node_input, dict):
        response = node_input.get("result", "")
    else:
        response = str(node_input)
    response = response.strip().lower()
    wants_briefing = any(kw in response for kw in ["yes", "y", "sure", "ok", "yeah", "briefing", "show", "yes, please", "want"])
    return Event(
        output=response,
        state={"wants_briefing": "yes" if wants_briefing else "no"},
        route="briefing" if wants_briefing else "done",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Node: Done without briefing
# ─────────────────────────────────────────────────────────────────────────────

async def onboarding_done(ctx: Context, node_input) -> Event:
    """Final confirmation message listing all available commands."""
    from app.telegram_bot.sender import send_message_to_user

    chat_id = ctx.state.get("chat_id", "")
    targets = json.loads(ctx.state.get("saved_target_currencies", "[]"))

    done_msg = (
        f"✨ **Onboarding Completed Successfully!**\n\n"
        f"I have enabled real-time exchange rate monitoring and scoring for {', '.join(targets)}. I will proactively alert you when the buying opportunity score exceeds your configured threshold.\n\n"
        f"**🤖 Available commands to interact with me:**\n"
        f"• /status — Get real-time exchange rates and detailed AI scoring breakdowns\n"
        f"• /briefing — Get a comprehensive opportunity briefing & strategy report\n"
        f"• /settings — View and edit your current configuration\n"
        f"• /help — Show help and commands\n"
        f"• Send a message — Tell me modifications directly (e.g., 'Change my EUR target rate to 7.5' or 'Add GBP to track'), and I will update and save it immediately!"
    )

    if chat_id:
        try:
            await send_message_to_user(chat_id, done_msg, parse_mode="Markdown")
        except Exception as e:
            print(f"[Onboarding] Failed to send done message: {e}")

    return Event(output={"status": "done"})


# Onboarding Workflow
# ─────────────────────────────────────────────────────────────────────────────

onboarding_workflow = Workflow(
    name="onboarding_workflow",
    description="Multi-turn HITL onboarding: collect user FX preferences, fetch history, optionally generate briefing.",
    edges=[
        ("START", greet_new_user),
        (greet_new_user, collect_purchase_intent),
        (collect_purchase_intent, prepare_llm_input),
        (prepare_llm_input, onboarding_llm),
        (onboarding_llm, save_onboarding_result),
        (save_onboarding_result, fetch_and_store_history),
        (fetch_and_store_history, confirm_and_ask_briefing),
        (confirm_and_ask_briefing, route_after_briefing_ask),
        (route_after_briefing_ask, {
            "done": onboarding_done,
            "briefing": report_workflow
        }),
        (report_workflow, onboarding_done),
    ],
)
