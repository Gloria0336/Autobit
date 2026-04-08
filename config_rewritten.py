from __future__ import annotations

from pathlib import Path

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

CHECK_INTERVAL_SEC = 300.0
REQUEST_TIMEOUT = 10

LOG_FILE = "autobit.log"
DATABASE_FILE = "autobit.db"
WEB_TITLE = "Autobit Web Dashboard"

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
WEB_INDEX_FILE = WEB_DIR / "index.html"
WEB_DATABASE_PATH = BASE_DIR / ".autobit-data" / DATABASE_FILE
HISTORICAL_DATA_DIR = BASE_DIR / ".autobit-data" / "historical"
