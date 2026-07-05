"""
Telegram → ADK Runner bridge.

Maintains one ADK session per Telegram user (keyed by chat_id).
Handles message routing, session creation, and response streaming.

Session management:
  - user_id = str(chat_id)
  - session_id = f"tg_{chat_id}"  (persistent per user)
  - State injected at each turn: {"chat_id": str(chat_id), "trigger": "chat"}
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

logger = logging.getLogger(__name__)

# Shared runner and session service (module-level singletons)
_runner: Runner | None = None
_session_service: InMemorySessionService | None = None
_sessions: dict[str, str] = {}  # chat_id → session_id


def get_runner() -> Runner:
    """Get or initialise the ADK Runner."""
    global _runner, _session_service

    if _runner is None:
        from app.agent import app as adk_app

        _session_service = InMemorySessionService()
        _runner = Runner(
            app=adk_app,
            session_service=_session_service,
        )
        logger.info("ADK Runner initialised")

    return _runner


async def get_or_create_session(chat_id: str) -> str:
    """Get existing session or create a new one for this user."""
    runner = get_runner()
    user_id = str(chat_id)

    if chat_id not in _sessions:
        session = await runner.session_service.create_session(
            app_name="app",
            user_id=user_id,
        )
        _sessions[chat_id] = session.id
        logger.info(f"Created session {session.id} for user {user_id}")

    return _sessions[chat_id]


def _process_response_text(text: str) -> str | None:
    trimmed = text.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        try:
            import json
            data = json.loads(trimmed)
            if isinstance(data, dict):
                # Onboarding summary extraction
                if "onboarding_summary" in data:
                    return data["onboarding_summary"]
                # Suppress other structured system JSONs (ScoringReport, BriefingReport)
                if "scores" in data or "currency_sections" in data or "composite_score" in data:
                    return None
        except Exception:
            pass
    return text


async def send_to_agent(
    chat_id: str,
    message: str,
    trigger: str = "chat",
    extra_state: dict | None = None,
) -> list[str]:
    """Send a message to the ADK agent and collect response parts.

    Args:
        chat_id: Telegram chat_id (as string).
        message: User message text.
        trigger: "chat" | "scheduled" | "briefing" | "new_currency"
        extra_state: Additional state to inject into the session.

    Returns:
        List of response text chunks to send back to Telegram.
    """
    runner = get_runner()
    session_id = await get_or_create_session(chat_id)
    user_id = str(chat_id)

    # Fetch current session to check for any pending interrupts
    session = await runner.session_service.get_session(
        app_name="app", user_id=user_id, session_id=session_id
    )

    responded_ids = set()
    if session and session.events:
        for ev in session.events:
            for attr in ("content", "message", "output"):
                val = getattr(ev, attr, None)
                if val and hasattr(val, "parts") and val.parts:
                    for part in val.parts:
                        if hasattr(part, "function_response") and part.function_response:
                            responded_ids.add(part.function_response.id)

    pending_interrupt = None
    if session and session.events:
        for ev in reversed(session.events):
            lr_ids = getattr(ev, "long_running_tool_ids", None)
            if not lr_ids:
                continue
            content_val = getattr(ev, "content", None)
            if not content_val or not content_val.parts:
                continue
            for part in content_val.parts:
                fc = getattr(part, "function_call", None)
                if fc and fc.id in lr_ids and fc.id not in responded_ids:
                    pending_interrupt = (fc.id, fc.name, ev.invocation_id)
                    break
            if pending_interrupt:
                break

    # Build the user message content
    is_command = message.startswith("/")
    if pending_interrupt and not is_command:
        fc_id, fc_name, invocation_id = pending_interrupt
        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=fc_id,
                        name=fc_name,
                        response={"result": message},
                    )
                )
            ],
        )
        run_invocation_id = invocation_id
    else:
        content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=message)],
        )
        run_invocation_id = None

    # Inject context into state via a synthetic state update
    state_update = {"chat_id": str(chat_id), "trigger": trigger}
    if extra_state:
        state_update.update(extra_state)

    responses = []
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
            state_delta=state_update,
            invocation_id=run_invocation_id,
        ):
            # Collect text parts from model events
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        processed = _process_response_text(part.text)
                        if processed:
                            responses.append(processed)
                    elif hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        if fc.name == "adk_request_input":
                            msg = fc.args.get("message")
                            if msg:
                                responses.append(msg)

    except Exception as e:
        logger.error(f"ADK runner error for {chat_id}: {e}", exc_info=True)
        responses.append("⚠️ 系统出现错误，请稍后再试。")

    return responses


async def trigger_scheduled_run(chat_id: str, window_label: str) -> None:
    """Trigger a scheduled scoring run for a user (called by scheduler)."""
    logger.info(f"[Scheduler] Triggering {window_label} scoring for {chat_id}")
    await send_to_agent(
        chat_id=chat_id,
        message=f"[SCHEDULER] {window_label} 时段自动评分",
        trigger="scheduled",
    )
