"""
Main entry point for FX Monitor AI.

Starts:
  1. Database init (creates tables)
  2. APScheduler (background cron jobs)
  3. Telegram Bot (polling or webhook)

Usage:
  uv run python -m app.main

Environment variables (see .env.example):
  TELEGRAM_BOT_TOKEN  — required
  MODEL_PROVIDER      — gemini_apikey | vertex | litellm
  GOOGLE_API_KEY      — for gemini_apikey mode
  DATABASE_URL        — defaults to SQLite
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    from app.config import TELEGRAM_BOT_TOKEN
    from app.database import init_db
    from app.scheduler import create_scheduler
    from app.telegram_bot.bot import create_bot_application

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Please configure .env")
        sys.exit(1)

    # 1. Init database
    logger.info("Initialising database...")
    init_db()
    logger.info("Database ready.")

    # 2. Start scheduler
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Scheduler started.")

    # 3. Start Telegram bot
    bot_app = create_bot_application()

    logger.info("Starting Telegram bot (polling mode)...")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )

    logger.info("✅ FX Monitor AI is running. Press Ctrl+C to stop.")

    # Keep running until interrupted
    stop_event = asyncio.Event()

    def _handle_signal(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    await stop_event.wait()

    # Graceful shutdown
    logger.info("Stopping Telegram bot...")
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

    logger.info("Stopping scheduler...")
    scheduler.shutdown(wait=False)

    logger.info("Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
