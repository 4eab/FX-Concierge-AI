"""
Telegram Bot — main bot using python-telegram-bot v21 (async).

Commands:
  /start    — Start onboarding or re-greet active users
  /status   — Show current rate + latest scores
  /briefing — Request a full analysis briefing
  /settings — Show current settings (edit via conversation)
  /help     — Show help

All non-command messages are forwarded to the ADK agent.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import TELEGRAM_BOT_TOKEN
from app.telegram_bot.adk_bridge import send_to_agent
from app.tools import get_user_profile

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: send ADK response back to user
# ─────────────────────────────────────────────────────────────────────────────

async def reply_markdown(update: Update, text: str) -> None:
    """Send markdown text safely by converting it to Telegram HTML, with fallback."""
    from app.telegram_bot.sender import markdown_to_telegram_html
    try:
        html_text = markdown_to_telegram_html(text)
        await update.message.reply_text(html_text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to send HTML reply, falling back to raw text: {e}")
        try:
            await update.message.reply_text(text)
        except Exception as err:
            logger.error(f"Fallback reply failed: {err}")


async def _relay(update: Update, context: ContextTypes.DEFAULT_TYPE, trigger: str = "chat", extra_state: dict | None = None) -> None:
    """Send user's message to ADK and relay responses."""
    chat_id = str(update.effective_chat.id)
    text = update.message.text or ""

    # Send a temporary waiting message
    waiting_msg = await update.message.reply_text("⏳ Processing, please wait...")

    # Show typing indicator
    await update.message.chat.send_action("typing")

    try:
        responses = await send_to_agent(
            chat_id=chat_id,
            message=text,
            trigger=trigger,
            extra_state=extra_state,
        )
    finally:
        try:
            await waiting_msg.delete()
        except Exception:
            pass

    for response in responses:
        if response.strip():
            await reply_markdown(update, response)


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Begin onboarding or re-welcome active users."""
    chat_id = str(update.effective_chat.id)
    profile = get_user_profile(chat_id)

    if profile.get("status") == "not_found" or profile.get("onboarding_status") in ("new", None):
        # New user — trigger onboarding
        responses = await send_to_agent(
            chat_id=chat_id,
            message="/start",
            trigger="chat",
        )
        for resp in responses:
            if resp.strip():
                await reply_markdown(update, resp)
    else:
        # Returning user
        currencies = profile.get("target_currencies", [])
        source = profile.get("source_currency", "CNY")
        await reply_markdown(
            update,
            f"👋 Welcome back!\n\n"
            f"You are tracking: {source} → {', '.join(currencies)}\n\n"
            f"Available Commands:\n"
            f"• /status — View real-time exchange rate scores\n"
            f"• /briefing — Get detailed analysis briefing\n"
            f"• /settings — View and edit settings\n"
            f"• Send a message — Chat with the AI assistant",
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — Show latest scores for tracked currencies calculated in real-time."""
    chat_id = str(update.effective_chat.id)
    profile = get_user_profile(chat_id)

    if profile.get("status") == "not_found":
        await update.message.reply_text("Please send /start to complete initial configuration first.")
        return

    currencies = profile.get("target_currencies", [])
    if not currencies:
        await update.message.reply_text("You haven't set any monitored currencies yet. Please send /start to configure.")
        return

    # Notify user that we are generating fresh scores
    await update.message.reply_text("⏳ Fetching real-time rates and performing AI scoring, please wait (approx. 5-10 seconds)...")

    # Trigger daily_score_workflow with status_request trigger
    responses = await send_to_agent(
        chat_id=chat_id,
        message="/status",
        trigger="status_request",
    )

    for resp in responses:
        if resp.strip() and resp != "（处理中，请稍候）":
            await reply_markdown(update, resp)


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/briefing — Request a full analysis briefing."""
    chat_id = str(update.effective_chat.id)
    profile = get_user_profile(chat_id)

    if profile.get("status") == "not_found":
        await update.message.reply_text("Please send /start to complete initial configuration first.")
        return

    await update.message.reply_text("📋 Generating opportunity briefing, please wait (approx. 15-30 seconds)...")

    responses = await send_to_agent(
        chat_id=chat_id,
        message="/briefing",
        trigger="briefing",
    )

    # Briefing is sent directly by the workflow — these are any additional messages
    for resp in responses:
        if resp.strip() and resp != "（处理中，请稍候）":
            await reply_markdown(update, resp)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings — Show current settings."""
    chat_id = str(update.effective_chat.id)
    profile = get_user_profile(chat_id)

    if profile.get("status") == "not_found":
        await update.message.reply_text("Please send /start to complete initial configuration first.")
        return

    currencies = profile.get("target_currencies", [])
    source = profile.get("source_currency", "CNY")
    intent_map = profile.get("purchase_intent", {})
    weights_map = profile.get("scoring_weights", {})
    alerts = profile.get("alerts_enabled", True)

    lines = [
        f"⚙️ **Current Settings**\n",
        f"**General Config**",
        f"• Base Currency: {source}",
        f"• Tracked Currencies: {', '.join(currencies)}",
        f"• Auto Alerts: {'Enabled ✅' if alerts else 'Disabled ❌'}\n"
    ]

    for currency in currencies:
        intent = intent_map.get(currency, {})
        amount = intent.get("amount", 0)
        amount_str = f"{amount:,.0f} {currency}" if amount > 0 else "Not set"
        target_rate = intent.get("target_rate")
        target_rate_str = f"{target_rate:.4f}" if target_rate else "Not set"
        threshold = intent.get("alert_threshold", 70)

        lines.extend([
            f"**🟡 {currency} Config**",
            f"• Purpose: {intent.get('purpose', 'Not set')}",
            f"• Budget/Amount: {amount_str}",
            f"• Time Horizon: {intent.get('time_horizon', 'Not set')}",
            f"• Risk Appetite: {intent.get('risk_tolerance', 'Not set')}",
            f"• Target Rate: {target_rate_str}",
            f"• Alert Threshold: {threshold}/100\n"
        ])

    lines.append(f"_To update settings, simply tell me (e.g., \"Set EUR target to 7.5\" or \"Add USD to track\")_")
    await reply_markdown(update, "\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — Show help."""
    await reply_markdown(
        update,
        "🤖 **FX Monitor AI Help**\n\n"
        "**Commands**\n"
        "• /start — Start onboarding or restart conversation\n"
        "• /status — View real-time exchange rate scores\n"
        "• /briefing — Get detailed opportunity briefing\n"
        "• /settings — View and edit settings\n"
        "• /help — Show this help message\n\n"
        "**Features**\n"
        "• Automated checks daily at 09:00 / 15:30 / 21:00 (Beijing Time)\n"
        "• Proactive purchase notifications when composite score ≥ threshold\n"
        "• Track multiple currencies independently\n"
        "• Chat with the AI assistant anytime by typing a message\n\n"
        "⚠️ For reference only. Not financial advice.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# General message handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all non-command messages — forward to ADK agent."""
    await _relay(update, context, trigger="chat")


# ─────────────────────────────────────────────────────────────────────────────
# Bot application factory
# ─────────────────────────────────────────────────────────────────────────────

def create_bot_application() -> Application:
    """Create and configure the Telegram bot application."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in environment")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("briefing", cmd_briefing))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    return application
