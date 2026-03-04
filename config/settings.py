"""
Central configuration for Japan 225 Trading Bot.
All trading rules, constants, and parameters live here.
Change settings here, not scattered across files.
"""
import os
from datetime import datetime, timezone, timedelta
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

# ============================================
# RISK MANAGEMENT - NON-NEGOTIABLE
# ============================================
MAX_MARGIN_PERCENT = 0.50  # Margin must NEVER exceed 50% of balance
MAX_OPEN_POSITIONS = 1  # One trade at a time
MAX_CONSECUTIVE_LOSSES = 2  # 2 losses = 1-hour cooldown
COOLDOWN_HOURS = 1
DAILY_LOSS_LIMIT_PERCENT = 1.0  # Effectively disabled — AI finds the setups, user manages risk
WEEKLY_LOSS_LIMIT_PERCENT = 0.50  # 50% of balance
EXTREME_DAY_RANGE_PTS = 1000     # Intraday range > this = extreme day (crash or rally)
EXTREME_DAY_MIN_CONFIDENCE = 85  # Minimum confidence on extreme days (both directions)
OVERSOLD_SHORT_BLOCK_RSI_4H = 32 # Don't SHORT below this 4H RSI unless breaking support with volume
OVERBOUGHT_LONG_BLOCK_RSI_4H = 68 # Don't LONG above this 4H RSI unless breaking resistance with volume
MIN_CONFIDENCE = 70  # Hard floor - no trades below this
EVENT_BLACKOUT_MINUTES = 60  # No trades within 60 min of high-impact events
TRADE_EXPIRY_MINUTES = 15  # Unconfirmed alerts expire after 15 min

# ============================================
# TRADING PARAMETERS
# ============================================
MIN_LOT_SIZE = 0.01
SPREAD_ESTIMATE = 7  # Points during main hours (live spread used at execution)

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

# RSI gate for BB mid bounce entry
RSI_ENTRY_HIGH_BOUNCE = 55    # Backtest: RSI 55-65 LONG WR=38%, cut off

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
MINUTE_5_CANDLES = 100  # 5M lookback for fallback entry TF (~8h of 5M data, covers BB20 + EMA50)

# ============================================
# AI MODELS
# ============================================
SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL   = "claude-opus-4-6"

# ============================================
# POSITION MONITOR
# ============================================
MONITOR_INTERVAL_SECONDS = 2        # Price check interval (seconds) — get_market_info only, 30 calls/min
POSITION_CHECK_EVERY_N_CYCLES = 15  # Check position existence every N cycles: 15 × 2s = 30s, 2 calls/min
SCAN_INTERVAL_SECONDS = 300         # Entry scan interval when flat (5 min)
OFFHOURS_INTERVAL_SECONDS = 1800    # Off-hours heartbeat (30 min)

# ============================================
# ENTRY SCANNING
# ============================================
AI_COOLDOWN_MINUTES = 15            # Suppress duplicate AI escalations
HAIKU_MIN_SCORE = 60                # Local confidence floor before AI evaluation (legacy name, kept for compat)
                                    # CONFIDENCE_BASE=30, criteria add 10pts each → scores are discrete: 30,40,50,...
                                    # C7/C8 (event/blackout) are always True when AI is reached (hard-blocked before)
                                    # → effective floor is already 50 (30 + 2×10). Setting 50 does nothing.
                                    # 60 = first meaningful threshold: requires ≥1 technical criterion beyond C7/C8
PRICE_DRIFT_ABORT_PTS = 20          # Abort trade if price moved this far during analysis
STALE_DATA_THRESHOLD = 10           # Identical price readings = stale data alert
ADVERSE_LOOKBACK_READINGS = 150     # Readings to look back for adverse_move (150 × 2s = 5min window)
PRE_SCREEN_CANDLES = 220            # Candles for 15M pre-screen — MUST be >200 to compute EMA200
AI_ESCALATION_CANDLES = 220         # Candles for 4H full scan — MUST be >200 to compute EMA200
DAILY_EMA200_CANDLES = 250          # Candles for Daily — MUST be >200 to compute EMA200

# ============================================
# SHORT TRADING
# ============================================
MIN_CONFIDENCE_SHORT = 75           # Higher bar for shorts (BOJ intervention risk)
SHORT_RSI_LOW = 55                  # RSI zone for short entries

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
# DISPLAY TIMEZONE (user-facing times: Telegram, logs, reports)
# ============================================
DISPLAY_TZ = timezone(timedelta(hours=3))  # Kuwait = UTC+3
DISPLAY_TZ_LABEL = "UTC+3"

def display_now() -> datetime:
    """Current time in display timezone (Kuwait UTC+3)."""
    return datetime.now(DISPLAY_TZ)

# ============================================
# LOGGING
# ============================================
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
LOG_LEVEL = "DEBUG" if DEBUG else "INFO"
