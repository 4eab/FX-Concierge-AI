"""
Telegram message sender — standalone utility for proactive notifications.

Used by workflows (daily_score_workflow, report_workflow) to push messages
without a user-initiated conversation.
"""

from __future__ import annotations

import logging
import os

import httpx

import html
import re

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def markdown_to_telegram_html(text: str) -> str:
    """Escapes HTML special characters and translates basic markdown tags to safe HTML."""
    text = html.escape(text)
    # Header tags to Bold
    text = re.sub(r'^#{1,6}\s*(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # Double asterisks to Bold
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Single asterisk or underscore to Italic
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
    # Inline code
    text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
    return text


async def send_message_to_user(
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a proactive message to a Telegram user.

    Args:
        chat_id: Target chat_id.
        text: Message text (supports Markdown).
        parse_mode: "Markdown" or "HTML".

    Returns:
        True if sent successfully.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot send message")
        return False

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"

    # Translate Markdown to HTML to avoid Telegram's strict markdown parser crashes
    if parse_mode == "Markdown":
        text = markdown_to_telegram_html(text)
        parse_mode = "HTML"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
            logger.info(f"[Telegram] Sent message to {chat_id}: {text[:60]}...")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"[Telegram] HTTP error sending to {chat_id}: {e.response.text}")
            # Fallback: if rich parsing fails, attempt to send as raw unparsed text
            if e.response.status_code == 400 and parse_mode:
                try:
                    logger.warning(f"[Telegram] Rich parse failed for {chat_id}, falling back to raw text...")
                    resp_fallback = await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": text,  # send as is (HTML-escaped, but still readable)
                            "disable_web_page_preview": True,
                        },
                    )
                    resp_fallback.raise_for_status()
                    logger.info(f"[Telegram] Sent fallback message successfully to {chat_id}")
                    return True
                except Exception as fallback_err:
                    logger.error(f"[Telegram] Fallback send failed to {chat_id}: {fallback_err}")
            return False
        except Exception as e:
            logger.error(f"[Telegram] Error sending to {chat_id}: {e}")
            return False
