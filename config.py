from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


BASE_DIR = Path(__file__).resolve().parent
ENV_PATHS = (BASE_DIR / ".env", BASE_DIR / ".env.local")


def refresh_runtime_env(*, override: bool = True) -> None:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
                value = value[1:-1]
            if override or key not in os.environ:
                os.environ[key] = value


refresh_runtime_env()

BINANCE_BASE_URL = "https://api.binance.com"
KLINES_ENDPOINT = "/api/v3/klines"
TICKER_ENDPOINT = "/api/v3/ticker/price"
SYMBOL = "BTCUSDT"

TRADING_CURRENCY = "USDT"
DISPLAY_CURRENCY = "TWD"
FX_RATE_BASE = "USD"
FX_RATE_QUOTE = "TWD"
FRANKFURTER_BASE_URL = "https://api.frankfurter.dev"

SUPPORTED_INTERVALS = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
)

TREND_INTERVAL = "1h"
SIGNAL_INTERVAL = "15m"
TREND_LIMIT = 220
SIGNAL_LIMIT = 100

EMA_TREND_PERIOD = 200
EMA_SIGNAL_PERIOD = 20
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_LINE = 9

RSI_ENTRY_LOW = 50.0
RSI_ENTRY_HIGH = 70.0
RSI_EXIT_HIGH = 75.0
ANTI_CHASE_PCT = 0.02
STOP_LOSS_PCT = 0.02
TRAIL_TRIGGER_PCT = 0.015
TRAIL_STOP_PCT = 0.01

FEE_TAKER = 0.0016
FEE_MAKER = 0.0008

CHECK_INTERVAL_SEC = 10.0
REQUEST_TIMEOUT = 10

LOG_FILE = "autobit.log"
DATABASE_FILE = "autobit.db"
WEB_TITLE = "Autobit Web Dashboard"
OPENROUTER_MODEL_CANDIDATES = (
    "openai/gpt-4.1-mini",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "openai/o4-mini",
    "openai/o3",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.7-sonnet",
    "anthropic/claude-sonnet-4",
    "google/gemini-2.0-flash-001",
    "google/gemini-2.5-flash-preview",
    "google/gemini-2.5-pro-preview",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat-v3",
    "deepseek/deepseek-r1",
    "qwen/qwen-2.5-72b-instruct",
)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "")
OPENROUTER_X_TITLE = os.getenv("OPENROUTER_X_TITLE", WEB_TITLE)


def get_openrouter_settings() -> dict[str, str]:
    refresh_runtime_env()
    return {
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        "model": os.getenv("OPENROUTER_MODEL", "").strip(),
        "api_key": os.getenv("OPENROUTER_API_KEY", "").strip(),
        "http_referer": os.getenv("OPENROUTER_HTTP_REFERER", "").strip(),
        "x_title": os.getenv("OPENROUTER_X_TITLE", WEB_TITLE).strip(),
    }

WEB_DIR = BASE_DIR / "web"
WEB_INDEX_FILE = WEB_DIR / "dashboard.html"
WEB_DATABASE_PATH = BASE_DIR / ".autobit-data" / DATABASE_FILE
HISTORICAL_DATA_DIR = BASE_DIR / ".autobit-data" / "historical"
AI_REPORTS_DIR = BASE_DIR / ".autobit-data" / "ai_reports"
