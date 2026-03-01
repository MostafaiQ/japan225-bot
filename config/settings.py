"""
Central configuration for Japan 225 Trading Bot.
All trading rules, constants, and parameters live here.
Change settings here, not scattered across files.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ============================================
# PATHS
# ============================================
BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage" / "data"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = STORAGE_DIR / "trading.db"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ============================================
# CREDENTIALS (from environment)
# ============================================
IG_API_KEY = os.getenv("IG_API_KEY", "")
IG_USERNAME = os.getenv("IG_USERNAME", "")
IG_PASSWORD = os.getenv("IG_PASSWORD", "")
IG_ACC_NUMBER = os.getenv("IG_ACC_NUMBER", "")
IG_ENV = os.getenv("IG_ENV", "demo")  # "demo" or "live"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TRADING_MODE = os.getenv("TRADING_MODE", "live")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

ENABLE_EMA50_BOUNCE_SETUP = False  # Disabled: median EMA50 dist=325pts, entries unvalidated

# ============================================
# IG MARKETS - INSTRUMENT
# ============================================
EPIC = "IX.D.NIKKEI.IFM.IP"  # Japan 225 Cash (mini, $1/pt)
CURRENCY = "USD"
CONTRACT_SIZE = 1  # $1 per point
EXPIRY = "-"  # Cash = no expiry
MARGIN_FACTOR = 0.005  # 0.5% Tier 1 (0-95 contracts)

# IG API URLs
IG_API_URL = {
    "demo": "https://demo-api.ig.com/gateway/deal",
    "live": "https://api.ig.com/gateway/deal",
}

# ============================================
# RISK MANAGEMENT - NON-NEGOTIABLE
# ============================================
MAX_MARGIN_PERCENT = 0.50  # Margin must NEVER exceed 50% of balance
MAX_OPEN_POSITIONS = 1  # One trade at a time
MAX_CONSECUTIVE_LOSSES = 2  # 2 losses = 4-hour cooldown
COOLDOWN_HOURS = 4
DAILY_LOSS_LIMIT_PERCENT = 0.10  # 10% of balance
WEEKLY_LOSS_LIMIT_PERCENT = 0.20  # 20% of balance
MIN_CONFIDENCE = 70  # Hard floor - no trades below this
EVENT_BLACKOUT_MINUTES = 60  # No trades within 60 min of high-impact events
TRADE_EXPIRY_MINUTES = 15  # Unconfirmed alerts expire after 15 min

# ============================================
# TRADING PARAMETERS
# ============================================
MIN_LOT_SIZE = 0.01
MIN_STOP_DISTANCE = 20  # IG minimum for Japan 225
SPREAD_ESTIMATE = 7  # Points during main hours
GUARANTEED_STOP_PREMIUM = 8  # Points if using guaranteed stop

# --- Exit Strategy (3-Phase) ---
# Phase 1: Initial protection
DEFAULT_SL_DISTANCE = 150  # Points (WFO-validated: PF=3.67 vs SL=200 PF=2.56)
DEFAULT_TP_DISTANCE = 400  # Points (1:2 R:R base)
MIN_RR_RATIO = 1.5  # Minimum acceptable R:R

# Phase 2: Breakeven lock
BREAKEVEN_TRIGGER = 150  # Move SL to BE at +150 pts
BREAKEVEN_BUFFER = 10  # Add buffer above entry for spread

# Phase 3: Runner mode
RUNNER_VELOCITY_THRESHOLD = 0.75  # 75% of TP in first scan period = runner
TRAILING_STOP_DISTANCE = 150  # Points behind price
TRAILING_STOP_INCREMENT = 5  # Step size for trailing stop

# Legacy scalp mode (100pt)
SCALP_TP_DISTANCE = 100
SCALP_SL_DISTANCE = 150  # 1:1.5 minimum even in scalp mode

# ============================================
# INDICATORS
# ============================================
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
VWAP_RESET = "daily"

# RSI thresholds
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_ENTRY_LOW = 35            # Ideal entry RSI range (15M)
RSI_ENTRY_HIGH = 55
RSI_ENTRY_HIGH_BOUNCE = 55    # RSI upper gate for BB mid bounce (AI is quality gate for RSI 48-55 range)

# ============================================
# CONFIDENCE SCORING (8-point system)
# ============================================
CONFIDENCE_BASE = 30  # Starting confidence
CONFIDENCE_CRITERIA = {
    "daily_bullish": 10,       # Daily trend is bullish
    "entry_at_tech_level": 10, # Entry at Bollinger mid / EMA50
    "rsi_15m_in_range": 10,    # RSI 15M between 35-55
    "tp_viable": 10,           # TP distance achievable
    "higher_lows": 10,         # Price making higher lows
    "macro_bullish": 10,       # News/sentiment bullish
    "no_event_1hr": 10,        # No high-impact event within 1hr
    "no_friday_monthend": 10,  # Not Friday w/ data or month-end
}
# Max possible = 30 + 8*10 = 110, but capped at 100

# ============================================
# SESSIONS (Kuwait Time, UTC+3)
# ============================================
SESSIONS = {
    "tokyo_open":  {"start": "03:00", "end": "05:00", "priority": "HIGH"},
    "mid_tokyo":   {"start": "05:00", "end": "07:00", "priority": "HIGH"},
    "late_tokyo":  {"start": "07:00", "end": "09:00", "priority": "MEDIUM"},
    "tokyo_close": {"start": "09:00", "end": "11:00", "priority": "MEDIUM"},
    "london_open": {"start": "11:00", "end": "13:00", "priority": "HIGH"},
    "mid_london":  {"start": "13:00", "end": "15:00", "priority": "MEDIUM"},
    "late_london": {"start": "15:00", "end": "17:00", "priority": "MEDIUM"},
    "ny_open":     {"start": "17:30", "end": "19:30", "priority": "HIGH"},
    "mid_ny":      {"start": "19:30", "end": "21:30", "priority": "MEDIUM"},
    "late_ny":     {"start": "21:30", "end": "23:30", "priority": "LOW"},
    "off_hours":   {"start": "00:00", "end": "03:00", "priority": "SKIP"},
}

# ============================================
# SCAN SCHEDULE (Kuwait Time)
# ============================================
SCAN_TIMES = [
    "03:00", "05:00", "07:00", "09:00",
    "11:00", "13:00", "15:00",
    "17:30", "19:30", "21:30", "23:30",
]

# ============================================
# SESSION HOURS (UTC) — backtest + monitor
# ============================================
SESSION_HOURS_UTC = {
    "Tokyo":    (0,  6),   # 00:00-06:00 UTC (N225 cash market, highest quality)
    "London":   (8, 16),   # 08:00-16:00 UTC (strong directional moves)
    "New York": (16, 21),  # 16:00-21:00 UTC (US-correlated, decent quality)
    # 06-08 UTC: skip — chaotic Tokyo-close / London-open crossover
    # 21-00 UTC: skip — thin volume, avoid
}
MINUTE_5_CANDLES = 30  # 5M lookback for confirm_5m_entry (covers EMA9 + RSI14 + BB20)

# ============================================
# AI MODELS
# ============================================
SONNET_MODEL = "claude-sonnet-4-5-20250929"
OPUS_MODEL   = "claude-opus-4-6"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"
AI_MAX_TOKENS = 2000
AI_TEMPERATURE = 0.1  # Low temp for consistent analysis

# ============================================
# POSITION MONITOR
# ============================================
MONITOR_INTERVAL_SECONDS = 2        # Price check interval (seconds) — get_market_info only, 30 calls/min
POSITION_CHECK_EVERY_N_CYCLES = 15  # Check position existence every N cycles: 15 × 2s = 30s, 2 calls/min
SCAN_INTERVAL_SECONDS = 300         # Entry scan interval when flat (5 min)
OFFHOURS_INTERVAL_SECONDS = 1800    # Off-hours heartbeat (30 min)
MONITOR_USE_STREAMING = False       # Start with REST polling, upgrade later

# ============================================
# ENTRY SCANNING
# ============================================
AI_COOLDOWN_MINUTES = 30            # Suppress duplicate AI escalations (set AFTER Haiku approves)
HAIKU_MIN_SCORE = 35                # Minimum local score to reach Haiku gate (was 50 hard-gate to Sonnet)
                                    # Setups at 35-49%: Haiku evaluates with full macro context
                                    # C7/C8 (event/blackout) are hard-blocked BEFORE Haiku regardless of score
PRICE_DRIFT_ABORT_PTS = 20          # Abort trade if price moved this far during analysis
STALE_DATA_THRESHOLD = 10           # Identical price readings = stale data alert
ADVERSE_LOOKBACK_READINGS = 150     # Readings to look back for adverse_move (150 × 2s = 5min window)
PRE_SCREEN_CANDLES = 50             # Candles for 15M pre-screen (enough for BB20 + EMA50)
AI_ESCALATION_CANDLES = 100         # Candles for 4H when escalating to AI (RSI only, EMA50 ok)
DAILY_EMA200_CANDLES = 250          # Candles for Daily — MUST be >200 to compute EMA200

# ============================================
# SHORT TRADING
# ============================================
MIN_CONFIDENCE_SHORT = 75           # Higher bar for shorts (BOJ intervention risk)
SHORT_RSI_LOW = 55                  # RSI zone for short entries
SHORT_RSI_HIGH = 75

# ============================================
# ADVERSE MOVE TIERS (position monitoring)
# ============================================
ADVERSE_MILD_PTS = 60               # Alert only (was 30 — fired inside 1-candle noise)
ADVERSE_MODERATE_PTS = 120         # Alert + suggest close (was 50)
ADVERSE_SEVERE_PTS = 175           # Auto move SL to breakeven (was 80 — 87.5% of 200pt SL)

# ============================================
# FRIDAY BLACKOUT
# ============================================
FRIDAY_BLACKOUT_START_UTC = "12:00"  # Default Friday no-trade window (covers NFP)
FRIDAY_BLACKOUT_END_UTC = "16:00"

# ============================================
# FREE MARKET DATA APIS (no key required)
# ============================================
USD_JPY_API = "https://api.frankfurter.app/latest?from=USD&to=JPY"
SAFETY_CONSECUTIVE_EMPTY = 2        # Require N consecutive empty position responses before accepting close

# ============================================
# COMPOUND PLAN
# ============================================
def get_lot_size(balance: float, price: float = 59500) -> float:
    """Calculate max lot size that keeps margin under 50% of balance."""
    max_margin = balance * MAX_MARGIN_PERCENT
    margin_per_lot = CONTRACT_SIZE * price * MARGIN_FACTOR
    max_lots = max_margin / margin_per_lot
    # Round down to nearest 0.01
    lots = int(max_lots * 100) / 100
    return max(MIN_LOT_SIZE, lots)


def calculate_margin(lots: float, price: float = 59500) -> float:
    """Calculate required margin for a position."""
    return lots * CONTRACT_SIZE * price * MARGIN_FACTOR


def calculate_profit(lots: float, points: float) -> float:
    """Calculate profit/loss for given lots and point movement."""
    return lots * CONTRACT_SIZE * points


# ============================================
# NO-TRADE RULES
# ============================================
BLOCKED_DAYS = {
    # Day of week (0=Monday, 4=Friday)
    4: ["PPI", "CPI", "NFP", "BOJ"],  # Friday with these events = no trade
}
# No trading last 2 days of month (month-end rebalancing)
MONTHEND_BLACKOUT_DAYS = 2

# ============================================
# LOGGING
# ============================================
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
LOG_LEVEL = "DEBUG" if DEBUG else "INFO"
