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

# Setup types permanently disabled in both live and backtest.
# Reason for each:
#   breakout_long            — 28% WR, chasing price at extension
#   momentum_continuation_long — 28% WR, same issue as breakout
#   bollinger_lower_bounce   — 35% WR, below breakeven; touching band ≠ reversal
#   vwap_bounce_long         — 38% WR, -$6.26; near-breakeven but consistently below
#   multi_tf_bearish         — condition (bearish alignment), not entry signal; no price level
DISABLED_SETUP_TYPES: set = {
    # --- LONG (cut: below breakeven WR) ---
    "breakout_long",              # 28% WR — chasing extension
    "momentum_continuation_long", # 28% WR — same problem
    "bollinger_lower_bounce",     # 35% WR — touching band ≠ reversal
    "vwap_bounce_long",           # 38% WR — consistently below breakeven
    # --- SHORT condition (not entry) ---
    "multi_tf_bearish",           # condition, not signal; no price level
    # --- SHORT (losing; keep only bear_flag_breakdown + bollinger_upper_rejection) ---
    "momentum_continuation_short",    # 33% WR — too broad, fires on any downtrend
    "bb_mid_rejection",               # 33% WR — false positives
    "ema50_rejection",                # 25% WR — catching falling knife shorts
    "vwap_rejection_short",           # 0% WR — structurally broken
    "vwap_rejection_short_momentum",  # 24% WR — broken momentum short
    "ema9_pullback_short",            # 29% WR — structural loser in non-crash periods
}

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
MAX_MARGIN_PERCENT = 0.05   # Hard ceiling: 5% of balance in margin per position (replaces 50%)
MAX_OPEN_POSITIONS = 3      # Allow up to 3 concurrent positions (up from 1)
MAX_PORTFOLIO_RISK_PERCENT = 0.08  # Total open risk across all positions ≤ 8% of balance
MAX_CONSECUTIVE_LOSSES = 2  # 2 losses = 1-hour cooldown
COOLDOWN_HOURS = 1
DAILY_LOSS_LIMIT_PERCENT = 1.0  # Effectively disabled — AI finds the setups, user manages risk
WEEKLY_LOSS_LIMIT_PERCENT = 0.50  # 50% of balance
EXTREME_DAY_RANGE_PTS = 1000     # Intraday range > this = extreme day (crash or rally)
EXTREME_DAY_MIN_CONFIDENCE = 85  # Minimum confidence on extreme days (both directions)
OVERSOLD_SHORT_BLOCK_RSI_4H = 32 # Don't SHORT below this 4H RSI unless breaking support with volume
OVERBOUGHT_LONG_BLOCK_RSI_4H = 68 # Don't LONG above this 4H RSI unless breaking resistance with volume
MIN_CONFIDENCE = 70        # Hard floor for Sonnet swing trades
MIN_SCALP_CONFIDENCE = 60  # Lower floor for Opus scalp trades (SL=85, smaller risk)
MIN_SCALP_CONFIDENCE_SHORT = 65  # Scalp SHORT floor (maintains conservative SHORT bias)
EVENT_BLACKOUT_MINUTES = 60  # No trades within 60 min of high-impact events
TRADE_EXPIRY_MINUTES = 15  # Unconfirmed alerts expire after 15 min

# ============================================
# POSITION SIZING — RISK-BASED (replaces margin-cap approach)
# ============================================
RISK_PERCENT = 2.0          # % of balance risked per trade (base rate)
MAX_RISK_PERCENT = 3.0      # Hard ceiling on risk per trade
# Drawdown-triggered reductions:
DRAWDOWN_REDUCE_10PCT = 0.5    # Reduce to 0.5% risk when balance 10% below peak
DRAWDOWN_REDUCE_15PCT = 0.25   # Reduce to 0.25% risk when balance 15% below peak
DRAWDOWN_STOP_20PCT = True     # Stop trading when balance 20% below peak

# --- Dynamic SL ATR multipliers (replaces hardcoded 150pt) ---
SL_ATR_MULTIPLIER_MOMENTUM = 1.2      # Momentum setups: tighter SL
SL_ATR_MULTIPLIER_MEAN_REVERSION = 1.8  # Mean-reversion: needs room
SL_ATR_MULTIPLIER_BREAKOUT = 1.5
SL_ATR_MULTIPLIER_VWAP = 1.3
SL_ATR_MULTIPLIER_DEFAULT = 1.5
SL_FLOOR_PTS = 120           # Absolute minimum SL in points
TP_ATR_MULTIPLIER_BASE = 2.5  # Base TP = 2.5× ATR (gives ~1.67:1 R:R)
TP_ATR_MULTIPLIER_MOMENTUM = 3.0  # Momentum setups: wider TP (trends extend)
TP_FLOOR_PTS = 250            # Absolute minimum TP in points

# ============================================
# TRADING PARAMETERS
# ============================================
MIN_LOT_SIZE = 0.01
SPREAD_ESTIMATE = 7  # Points during main hours (live spread used at execution)

# --- Exit Strategy (3-Phase) ---
# Phase 1: Initial protection
DEFAULT_SL_DISTANCE = 150  # Points — fallback only (ATR-based used when ATR available)
DEFAULT_TP_DISTANCE = 400  # Points — fallback only
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

# --- Momentum / Trend-Following LONG Setups ---
MOMENTUM_RSI_LOW = 45         # RSI floor for momentum continuation
MOMENTUM_RSI_HIGH = 75        # RSI ceiling (widened: extreme rally days RSI hits 73-75)
BREAKOUT_RSI_LOW = 55         # RSI floor for breakout
BREAKOUT_RSI_HIGH = 75        # RSI ceiling for breakout
VWAP_BOUNCE_RSI_LOW = 40      # RSI floor for VWAP bounce
VWAP_BOUNCE_RSI_HIGH = 65     # RSI ceiling for VWAP bounce
EMA9_PULLBACK_RSI_LOW = 40    # RSI floor for EMA9 pullback
EMA9_PULLBACK_RSI_HIGH = 65   # RSI ceiling for EMA9 pullback
BREAKOUT_VOL_RATIO_MIN = 1.3  # Minimum volume ratio for breakout setup
BB_UPPER_PROXIMITY_PTS = 200  # Points from BB upper for breakout detection
SWING_HIGH_PROXIMITY_PTS = 100  # Points from swing high for breakout detection
VWAP_PROXIMITY_PTS = 120      # Points from VWAP for VWAP bounce detection
EMA9_PROXIMITY_PTS = 100      # Points from EMA9 for pullback detection
MOMENTUM_HA_STREAK_MIN = 2    # Minimum HA bullish streak for momentum continuation

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
POSITION_CHECK_EVERY_N_CYCLES = 5   # Check position existence every N cycles: 5 × 2s = 10s, 6 calls/min
OPUS_POSITION_EVAL_EVERY_N = 60     # Run Opus position evaluator every N monitor cycles (60 × 2s = 120s = 2min)
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
STREAMING_STALE_SECONDS = 10       # Treat streaming price as stale if no tick for this long → fallback to REST

# ============================================
# ATR-BASED ENTRY GATE
# ============================================
ATR_PERIOD = 14                    # ATR(14) lookback — require this many candles before any entry

# ============================================
# CONTRADICTORY SIGNAL GATE
# ============================================
CONTRADICTORY_SIGNAL_MIN_SCORE = 80   # Both directions must be >= this to be considered contradictory
CONTRADICTORY_SIGNAL_MAX_GAP = 5      # Max gap between LONG/SHORT scores to call it contradictory (no edge)

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
