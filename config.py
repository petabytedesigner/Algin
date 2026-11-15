"""Algin runtime policy — Category-3 read-only development baseline.

Risk settings are explicitly defined by Category 2. Broker mutation remains
disabled in configuration and by an independent Category-3 release gate.
"""
from __future__ import annotations
import os

API_KEY = os.getenv("OANDA_API_TOKEN", "")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_URL = "https://api-fxpractice.oanda.com/v3"
# No environment override: all real broker mutations remain disabled until
# a separate explicitly reviewed operational release.
ORDER_WRITES_ENABLED = False

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
# Category 2: original 1% cap retained, Grade B conservative.
RISK_PER_TRADE = 1.0
RISK_GRADE_A_PCT = 1.0
RISK_GRADE_B_PCT = 0.5
MAX_TOTAL_RISK_PCT = 1.0
MAX_INSTRUMENT_RISK_PCT = 1.0
MAX_CURRENCY_RISK_PCT = 1.0
MAX_TRADES_GRADE_A = 2
MAX_TRADES_GRADE_B = 3
MAX_ACTIVE_TRADES = 3
MAX_CONSECUTIVE_LOSSES = 3
MAX_DRAWDOWN_PCT = 5.0
MAX_DAILY_LOSS_PCT = 2.0
RISK_REWARD_RATIO = 3.0
ATR_STOP_MULTIPLE = 1.5
MAX_MARGIN_FRACTION = 0.5
MAX_QUOTE_AGE_SECONDS = 20

NEWS_LOCKOUT_MINUTES_BEFORE = 30
NEWS_LOCKOUT_MINUTES_AFTER = 30
NEWS_CACHE_TTL_MINUTES = 15
NEWS_CACHE_MAX_AGE_MINUTES = 30
SIGNAL_CANDLE_MAX_AGE_MINUTES = 45
MAX_RETEST_BARS = 4
ENABLE_SESSIONS = True
MIN_DISTANCE_PIPS = 3
# Read-only preflight; limit from verified historical FX spread scans.
SLIPPAGE_ALLOWED = 2
MAX_SPREAD_PIPS = 2.5

CSV_ENABLED = True
CSV_PATH = "csv"
CSV_LOG_SIGNALS = True
CSV_LOG_TRADES = True
CSV_LOG_PARTIAL = True
CSV_LOG_BE = True
CSV_LOG_NEWS_BLOCK = True
CSV_LOG_SESSION_BLOCK = True
CSV_LOG_COOLDOWN = True
CSV_LOG_SL_HIT = True
CSV_LOG_TP_HIT = True
CSV_LOG_MANUAL_CLOSE = True
CSV_LOG_SYSTEM_CLOSE = True

HEARTBEAT_PATH = ".local/heartbeat.json"
