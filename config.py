"""Algin Practice runtime configuration. Values deliberately default to no orders."""
from __future__ import annotations
import os

API_KEY = os.getenv("OANDA_API_TOKEN", "")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_URL = "https://api-fxpractice.oanda.com/v3"

# Both an explicit CLI switch and this exact opt-in are required for Practice writes.
ENABLE_ORDER_SUBMISSION = os.getenv("ALGIN_PRACTICE_SUBMIT", "") == "I_ACCEPT_PRACTICE_ORDERS"

INSTRUMENTS = tuple(x.strip() for x in os.getenv(
    "ALGIN_INSTRUMENTS", "EUR_USD,GBP_USD,USD_JPY,USD_CAD,AUD_USD").split(",") if x.strip())

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
RISK_PER_TRADE = 0.5
GRADE_A_RISK_PCT = 0.5
GRADE_B_RISK_PCT = 0.25
MAX_TOTAL_RISK_PCT = 2.0
MAX_SHARED_CURRENCY_RISK_PCT = 1.0
MAX_DAILY_LOSS_PCT = 2.0
MAX_ACCOUNT_DRAWDOWN_PCT = 5.0
MAX_TRADES_GRADE_A = 2
MAX_TRADES_GRADE_B = 2
REWARD_RISK = 2.0
MIN_DISTANCE_PIPS = 3
SLIPPAGE_ALLOWED = 1.0
MAX_SPREAD_PIPS = 2.5
COOLDOWN_MINUTES = 30
POLL_SECONDS = 60
ENABLE_SESSIONS = True
# Without a reliable calendar, block entries. To intentionally trade without
# calendar filtering in Practice, set ALGIN_NEWS_MODE=DISABLED_ACKNOWLEDGED.
NEWS_MODE = os.getenv("ALGIN_NEWS_MODE", "UNCONFIGURED_BLOCK")
NEWS_LOCKOUT_MINUTES_BEFORE = 30
NEWS_LOCKOUT_MINUTES_AFTER = 30
NEWS_CACHE_SECONDS = 900
NEWS_PROVIDER_PATH = os.getenv("ALGIN_NEWS_JSON_PATH", "")
CSV_ENABLED = True
CSV_PATH = "csv"
HEARTBEAT_PATH = ".local/heartbeat.json"
