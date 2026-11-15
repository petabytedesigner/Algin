"""Algin settings (Practice-only by design)."""
import os

# ================================
#   CONFIG V10 FINAL
#   Me CSV logging të plotë (A)
# ================================

# -------------------------
#   OANDA API SETTINGS
# -------------------------
API_KEY = os.getenv("OANDA_API_TOKEN", "")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_URL = "https://api-fxpractice.oanda.com/v3"
ENABLE_ORDER_SUBMISSION = False  # Never auto-submit in development

# -------------------------
#   STRATEGY SETTINGS
# -------------------------
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

# -------------------------
#   RISK SETTINGS
# -------------------------
RISK_PER_TRADE = 1.0
MAX_TRADES_GRADE_A = 2
MAX_TRADES_GRADE_B = 3

# -------------------------
#   NEWS & SESSIONS
# -------------------------
NEWS_LOCKOUT_MINUTES_BEFORE = 30
NEWS_LOCKOUT_MINUTES_AFTER = 30
ENABLE_SESSIONS = True

# -------------------------
#   EXECUTION LIMITS
# -------------------------
MIN_DISTANCE_PIPS = 3
SLIPPAGE_ALLOWED = 2

# -------------------------
#   CSV LOGGING (FULL - 11 EVENTS)
# -------------------------
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

# -------------------------
#   HEARTBEAT
# -------------------------
HEARTBEAT_PATH = "heartbeat_v10.txt"
