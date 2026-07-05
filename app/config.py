"""
Multi-model configuration for FX Monitor AI.

Supports three modes via MODEL_PROVIDER env var:
  gemini_apikey  - Gemini via AI Studio (GOOGLE_API_KEY)
  vertex         - Gemini via Vertex AI (GOOGLE_CLOUD_PROJECT)
  litellm        - Any LiteLLM-supported model (LITELLM_MODEL)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Project paths ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Database ───────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/fx_monitor_ai.db")

# ── Telegram ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ── Model provider ─────────────────────────────────────────────────────────
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "gemini_apikey").lower()
LITELLM_MODEL = os.getenv("LITELLM_MODEL", "anthropic/claude-sonnet-4-20250514")

# ── Scoring thresholds ─────────────────────────────────────────────────────
# Alert is sent when composite score >= this value (0-100)
ALERT_SCORE_THRESHOLD = int(os.getenv("ALERT_SCORE_THRESHOLD", "70"))

# ── Scheduler timezone ─────────────────────────────────────────────────────
SCHEDULER_TIMEZONE = "Asia/Shanghai"  # CST = UTC+8

# Realtime fetch windows (HH:MM in CST)
REALTIME_WINDOWS = ["09:00", "15:30", "21:00"]

# EOD ECB supplement task (after ECB publishes ~16:00 CET = 23:00 CST)
EOD_SUPPLEMENT_TIME = "23:30"


def get_model():
    """
    Return the appropriate model object/string for ADK Agent based on
    MODEL_PROVIDER environment variable.
    """
    if MODEL_PROVIDER == "litellm":
        from google.adk.models.lite_llm import LiteLlm  # type: ignore[import]
        return LiteLlm(model=LITELLM_MODEL)

    if MODEL_PROVIDER == "vertex":
        # Vertex AI: requires GOOGLE_CLOUD_PROJECT + GOOGLE_GENAI_USE_VERTEXAI=True
        from google.adk.models import Gemini
        return Gemini(model="gemini-flash-latest")

    # Default: gemini_apikey — ADK reads GOOGLE_API_KEY automatically
    # when GOOGLE_GENAI_USE_VERTEXAI is False/unset
    from google.adk.models import Gemini
    return Gemini(model="gemini-flash-latest")
