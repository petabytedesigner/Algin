"""Algin original configuration; Category 1 executes in observation-only mode.

Numerical strategy/risk values deliberately retained pending Category 2.
"""
from __future__ import annotations
import os

API_KEY = os.getenv("OANDA_API_TOKEN", "")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_URL = "https://api-fxpractice.oanda.com/v3"
# No environment override: broker mutations require categories 2, 3 and 4.
ORDER_WRITES_ENABLED = False

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
RISK_PER_TRADE = 1.0
MAX_TRADES_GRADE_A = 2
MAX_TRADES_GRADE_B = 3

NEWS_LOCKOUT_MINUTES_BEFORE = 30
NEWS_LOCKOUT_MINUTES_AFTER = 30
ENABLE_SESSIONS = True
MIN_DISTANCE_PIPS = 3
SLIPPAGE_ALLOWED = 2

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
