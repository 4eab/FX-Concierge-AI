# FX Concierge AI 🤖💱

> **Stop watching exchange rates. Start living.**

FX Concierge AI is an intelligent, multi-agent personal foreign exchange assistant on Telegram, built with **Google Agent Development Kit (ADK) 2.0** and developed inside the **Google Antigravity (AGY)** environment.

Designed for international students, travelers, and investors who need to exchange currency — the concierge replaces stressful daily manual rate-checking with fully automated, goal-oriented alerts and briefings.

---

## ✨ Features

- 🎯 **Stateful Onboarding** — Interactive setup of budget, timeline, target rate, and risk preference per currency pair
- 📊 **5-Dimension Opportunity Scoring** — Deterministic Python scoring model (no LLM hallucination) combining:
  - Historical Percentile
  - Short-term Momentum
  - Mean Reversion Potential
  - User Goal Proximity
  - Volatility Adjustment
- 🔔 **Proactive Telegram Alerts** — Background scheduler pushes alerts only when the score crosses your threshold
- 📋 **Daily Briefing** — LLM-generated multi-currency summary with real-time and historical context
- 💬 **Natural Language Settings** — Change thresholds, add/remove currencies, and update goals via chat
- 🌐 **Multi-Currency Support** — Track USD, EUR, GBP, JPY, and more simultaneously with isolated goals
- 🏦 **Dual Data Sources** — Real-time Bank of China rates + historical ECB rates for comprehensive analysis

---

## 🛠️ Tech Stack

| Component | Technology |
|-----------|------------|
| Agent Framework | Google ADK 2.0 |
| LLM | Google Gemini (model-agnostic via LiteLLM) |
| Bot Platform | Telegram Bot API (python-telegram-bot) |
| Database | SQLite (local) / PostgreSQL (cloud) |
| Scheduler | APScheduler |
| Dev Environment | Google Antigravity (AGY) |

---

## 🚀 Quick Start (Local Development)

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- A Telegram Bot Token (create via [@BotFather](https://t.me/botfather))
- A Google Gemini API Key ([Get one free](https://aistudio.google.com/))

### 1. Clone the repository

```bash
git clone https://github.com/your-username/fx-monitor-ai.git
cd fx-monitor-ai
```

### 2. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
MODEL_PROVIDER=gemini_apikey
```

### 3. Install dependencies and run

```bash
uv sync
uv run python -m app.main
```

The bot will start in polling mode. Open Telegram and send `/start` to your bot to begin!

---

## ☁️ Cloud Deployment (Google Cloud VM)

To keep the bot running 24/7 on a free Google Cloud e2-micro VM:

```bash
# On your Google Cloud VM (SSH)
git clone https://github.com/your-username/fx-monitor-ai.git
cd fx-monitor-ai

# Create your .env file
nano .env

# Install uv and dependencies
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv sync

# Start in background (persists after SSH disconnect)
nohup uv run python -m app.main > bot.log 2>&1 &
tail -f bot.log
```

---

## 📁 Project Structure

```
fx-monitor-ai/
├── app/
│   ├── agent.py                    # Main ADK agent + tool registration
│   ├── tools.py                    # All deterministic tools (scoring, DB, rates)
│   ├── schemas.py                  # Pydantic schemas for all workflows
│   ├── models.py                   # SQLAlchemy ORM models
│   ├── database.py                 # DB engine & session factory
│   ├── config.py                   # Multi-provider model config
│   ├── scheduler.py                # APScheduler cron jobs
│   ├── main.py                     # Entry point (bot + scheduler)
│   ├── workflows/
│   │   ├── onboarding_workflow.py  # Interactive currency setup
│   │   ├── daily_score_workflow.py # 5-dimension scoring engine
│   │   └── report_workflow.py      # Briefing LLM workflow
│   └── telegram_bot/
│       ├── bot.py                  # Telegram bot handlers
│       ├── adk_bridge.py           # ADK <-> Telegram bridge
│       └── sender.py               # Message formatting & sending
├── scripts/
│   └── reset_db.py                 # Database reset utility
├── tests/                          # Unit and integration tests
├── Dockerfile                      # Container deployment
├── pyproject.toml                  # Project dependencies
└── .env.example                    # Environment variable template
```

---

## 🔧 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Your Telegram bot token from @BotFather |
| `GEMINI_API_KEY` | ✅ (for gemini_apikey mode) | Google AI Studio API key |
| `MODEL_PROVIDER` | ✅ | `gemini_apikey` \| `vertex` \| `litellm` |
| `DATABASE_URL` | ❌ | Defaults to local SQLite. Set to `postgresql://...` for cloud DB |
| `GOOGLE_CLOUD_PROJECT` | ❌ | Required only for `vertex` mode |
| `ALERT_SCORE_THRESHOLD` | ❌ | Alert trigger score (0-100, default: 70) |
| `LITELLM_MODEL` | ❌ | Model name for `litellm` mode |

---

## 📱 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Begin onboarding for a new currency |
| `/status` | View current scores for all monitored currencies |
| `/briefing` | Get a full AI-generated market briefing |
| `/settings` | View and modify your current configuration |

You can also chat naturally with the bot to update settings or ask questions!

---

## 🧪 ADK Web Playground

To test the agent without Telegram:

```bash
agents-cli playground
```

Then open `http://localhost:8000` in your browser.

---

## ⚠️ Note on Latency & Model Providers

During the hackathon evaluation, the bot is running on a free-tier trial model host (`agnes-2.0-flash`). While the core scoring engine is written in deterministic Python and executes instantly, LLM-generated responses (such as briefings and conversational configuration updates) might occasionally experience latency or cold starts. Please allow a few seconds for the bot to generate text replies.

---

Built with ❤️ inside [Google Antigravity](https://antigravity.google/) environment using [Google ADK 2.0](https://adk.dev/).
