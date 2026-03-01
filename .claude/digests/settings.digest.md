# config/settings.py — DIGEST
# Purpose: Single source of truth for ALL constants. Never scatter config.

## Credentials (from .env)
IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACC_NUMBER, IG_ENV ("demo"|"live")
ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
TRADING_MODE ("paper"|"live"), DEBUG (bool)

## Instrument
EPIC="IX.D.NIKKEI.IFM.IP"  CURRENCY="USD"  CONTRACT_SIZE=1  MARGIN_FACTOR=0.005
IG_API_URL = {demo: ..., live: ...}

## Risk (non-negotiable)
MAX_MARGIN_PERCENT=0.50  MAX_OPEN_POSITIONS=1  MAX_CONSECUTIVE_LOSSES=2  COOLDOWN_HOURS=4
DAILY_LOSS_LIMIT_PERCENT=0.10  WEEKLY_LOSS_LIMIT_PERCENT=0.20
MIN_CONFIDENCE=70  MIN_CONFIDENCE_SHORT=75  EVENT_BLACKOUT_MINUTES=60  TRADE_EXPIRY_MINUTES=15

## Trading parameters
MIN_LOT_SIZE=0.01  MIN_STOP_DISTANCE=20  SPREAD_ESTIMATE=7
DEFAULT_SL_DISTANCE=150  DEFAULT_TP_DISTANCE=400  MIN_RR_RATIO=1.5
# SL=150 WFO-validated (PF=3.67 vs SL=200 PF=2.56). Updated 2026-02-28 post-backtest.
BREAKEVEN_TRIGGER=150  BREAKEVEN_BUFFER=10
RUNNER_VELOCITY_THRESHOLD=0.75  TRAILING_STOP_DISTANCE=150  TRAILING_STOP_INCREMENT=5

## Indicators
BOLLINGER_PERIOD=20  BOLLINGER_STD=2.0  EMA_FAST=50  EMA_SLOW=200  RSI_PERIOD=14
RSI_OVERSOLD=30  RSI_OVERBOUGHT=70  RSI_ENTRY_LOW=35  RSI_ENTRY_HIGH=55
RSI_ENTRY_HIGH_BOUNCE=55   # RSI upper gate for BB mid bounce (raised from 48; AI gates RSI 48-55 range)

## Confidence
CONFIDENCE_BASE=30  CONFIDENCE_CRITERIA = 8 keys × 10pts each  # max = 110, capped at 100

## Sessions (Kuwait UTC+3 reference — session.py uses UTC internally)
SESSIONS dict: tokyo_open/mid/late/close, london_open/mid/late, ny_open/mid/late, off_hours

## AI models
SONNET_MODEL="claude-sonnet-4-5-20250929"  OPUS_MODEL="claude-opus-4-6"
AI_MAX_TOKENS=2000  AI_TEMPERATURE=0.1

## Monitor timing
MONITOR_INTERVAL_SECONDS=2   SCAN_INTERVAL_SECONDS=300  OFFHOURS_INTERVAL_SECONDS=1800
POSITION_CHECK_EVERY_N_CYCLES=15  # 15 × 2s = 30s position check (2 calls/min to positions endpoint)
AI_COOLDOWN_MINUTES=30  PRICE_DRIFT_ABORT_PTS=20  STALE_DATA_THRESHOLD=10
ADVERSE_LOOKBACK_READINGS=150  # 150 × 2s = 5min adverse window
PRE_SCREEN_CANDLES=50  AI_ESCALATION_CANDLES=100  SAFETY_CONSECUTIVE_EMPTY=2

## Short trading
MIN_CONFIDENCE_SHORT=75  SHORT_RSI_LOW=55  SHORT_RSI_HIGH=75

## Adverse move tiers (recalibrated 2026-02-28 — old values fired inside 1-candle noise)
ADVERSE_MILD_PTS=60    # was 30 — ATR14-15m≈100pts, 30 was inside normal candle range
ADVERSE_MODERATE_PTS=120  # was 50
ADVERSE_SEVERE_PTS=175    # was 80 — 87.5% of 200pt SL = semantically "setup has failed"

## Paper trading safety gates (added 2026-02-28)
PAPER_TRADING_SESSION_GATE=False  # All 3 sessions enabled (Tokyo 49% WR, London 44%, NY 48% — backtest validated)
ENABLE_EMA50_BOUNCE_SETUP=False   # Disabled: median EMA50 dist=325pts, entries unvalidated

## Friday blackout
FRIDAY_BLACKOUT_START_UTC="12:00"  FRIDAY_BLACKOUT_END_UTC="16:00"

## Helper functions (defined here, not in other modules)
get_lot_size(balance, price=59500) -> float   # max lots under 50% margin
calculate_margin(lots, price=59500) -> float
calculate_profit(lots, points) -> float       # = lots * CONTRACT_SIZE * points

## Calendar rules
BLOCKED_DAYS = {4: ["PPI","CPI","NFP","BOJ"]}  MONTHEND_BLACKOUT_DAYS=2
