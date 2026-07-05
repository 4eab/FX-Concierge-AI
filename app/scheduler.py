"""
Scheduler — APScheduler-based cron jobs for automated FX monitoring.

Schedule (Asia/Shanghai = CST = UTC+8):
  09:00 CST — Morning window (before European open)
  15:30 CST — Afternoon window (London market active)
  21:00 CST — Evening window (Europe/US overlap)
  23:30 CST — EOD ECB supplement (after ECB publishes ~16:00 CET)

All scheduled jobs iterate over all active users and trigger the
appropriate workflow via the ADK bridge.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import SCHEDULER_TIMEZONE

logger = logging.getLogger(__name__)


def get_active_user_chat_ids() -> list[str]:
    """Retrieve all active user chat_ids from the database."""
    from app.database import SessionLocal
    from app.models import UserProfile

    with SessionLocal() as db:
        users = db.query(UserProfile).filter_by(status="active", alerts_enabled=True).all()
    return [u.chat_id for u in users]


async def run_realtime_scoring(window_label: str) -> None:
    """Fetch BOC rates and run AI scoring for all active users."""
    chat_ids = get_active_user_chat_ids()
    logger.info(f"[Scheduler] {window_label}: running for {len(chat_ids)} users")

    from app.telegram_bot.adk_bridge import trigger_scheduled_run

    for chat_id in chat_ids:
        try:
            await trigger_scheduled_run(chat_id, window_label)
        except Exception as e:
            logger.error(f"[Scheduler] Error for {chat_id} at {window_label}: {e}", exc_info=True)


async def run_eod_supplement() -> None:
    """Fetch ECB EOD rates and store in historical DB (no alert)."""
    logger.info("[Scheduler] EOD supplement: fetching ECB daily rates")

    from app.database import SessionLocal
    from app.models import UserProfile
    from app.tools import bulk_save_ecb_history, fetch_ecb_daily

    with SessionLocal() as db:
        # Collect all tracked currency pairs across all users
        users = db.query(UserProfile).filter_by(status="active").all()

    all_targets: set[str] = set()
    for user in users:
        all_targets.update(user.target_currencies)

    if not all_targets:
        logger.info("[Scheduler] EOD: no active users with configured currencies")
        return

    # Fetch ECB daily rates (EUR-based)
    target_list = list(all_targets)
    result = await fetch_ecb_daily(target_list)

    if result.get("status") == "success":
        rates = result.get("rates", {})
        fetch_date = result.get("fetch_date", "unknown")

        records = [
            {
                "date": fetch_date,
                "source_currency": "EUR",
                "target_currency": currency,
                "rate": rate,
            }
            for currency, rate in rates.items()
        ]

        save_result = bulk_save_ecb_history(records)
        logger.info(
            f"[Scheduler] EOD: inserted {save_result.get('inserted')} records "
            f"for date {fetch_date}"
        )
    else:
        logger.error(f"[Scheduler] EOD fetch failed: {result}")


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance."""
    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)

    # ── Realtime scoring windows ──────────────────────────────────────────
    scheduler.add_job(
        run_realtime_scoring,
        trigger=CronTrigger(hour=9, minute=0, timezone=SCHEDULER_TIMEZONE),
        args=["09:00 早盘"],
        id="morning_score",
        name="Morning scoring (09:00 CST)",
        replace_existing=True,
        misfire_grace_time=300,  # 5-minute grace period
    )

    scheduler.add_job(
        run_realtime_scoring,
        trigger=CronTrigger(hour=15, minute=30, timezone=SCHEDULER_TIMEZONE),
        args=["15:30 伦敦盘"],
        id="afternoon_score",
        name="Afternoon scoring (15:30 CST)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        run_realtime_scoring,
        trigger=CronTrigger(hour=21, minute=0, timezone=SCHEDULER_TIMEZONE),
        args=["21:00 晚盘"],
        id="evening_score",
        name="Evening scoring (21:00 CST)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # ── EOD ECB historical supplement ─────────────────────────────────────
    scheduler.add_job(
        run_eod_supplement,
        trigger=CronTrigger(hour=23, minute=30, timezone=SCHEDULER_TIMEZONE),
        id="eod_supplement",
        name="EOD ECB supplement (23:30 CST)",
        replace_existing=True,
        misfire_grace_time=600,  # 10-minute grace for EOD job
    )

    # ── Test scoring running every 5 minutes ──────────────────────────────
    scheduler.add_job(
        run_realtime_scoring,
        trigger=CronTrigger(minute="*/5", timezone=SCHEDULER_TIMEZONE),
        args=["Test 5-min Run"],
        id="test_5min_score",
        name="Test 5-minute scoring and alerts",
        replace_existing=True,
        misfire_grace_time=60,
    )

    logger.info("[Scheduler] Created with 5 jobs: 5-min intervals, 09:00, 15:30, 21:00, 23:30 CST")
    return scheduler
