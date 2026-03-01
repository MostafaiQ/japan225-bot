# core/indicators.py — DIGEST
# Purpose: Pure math. No API calls, no side effects. Fully testable.

## Functions

ema(prices: list[float], period: int) -> list[float]
  # SMA-seeded EMA. Returns same length as input (first period-1 = None).

sma(prices: list[float], period: int) -> list[float]
  # Same-length output, first period-1 = None.

bollinger_bands(closes, period=20, num_std=2.0) -> {"upper": list, "mid": list, "lower": list}

rsi(closes, period=14) -> list[float]
  # Wilder's smoothing. Returns same length (first period = None).

vwap(highs, lows, closes, volumes) -> list[float]
  # Cumulative VWAP. Assumes same-session candles.

analyze_timeframe(candles: list[dict]) -> dict
  # Input: candles with open/high/low/close/volume/timestamp
  # Output keys: price, open, high, low, bollinger_upper/mid/lower,
  #              ema9, ema50, ema200, rsi, vwap,
  #              above_ema9, above_ema50, above_ema200, above_vwap,
  #              ema200_available, above_ema200_fallback, bollinger_percentile,
  #              prev_close,
  #              volume_ratio, volume_signal ("HIGH"|"NORMAL"|"LOW"),
  #              swing_high_20, swing_low_20,
  #              dist_to_swing_high, dist_to_swing_low
  # NOTE: needs 200 candles for EMA200; <200 logs warning and uses EMA50 fallback

confirm_5m_entry(tf_5m: dict, direction: str) -> bool
  # 5M confirmation check. Returns True if tf_5m is None (pass-through).
  # LONG: price > EMA9, green candle, RSI > 45
  # SHORT: price < EMA9, red candle, RSI < 55
  # Used for AI context / future confidence scoring — NOT a hard gate in detect_setup()

detect_setup(tf_daily, tf_4h, tf_15m, tf_5m=None) -> dict
  # Bidirectional. Returns: found, type, direction, entry, sl, tp, reasoning, indicators_snapshot
  # indicators_snapshot includes: price, rsi_15m, bb_mid/upper/lower, ema50_15m,
  #   daily_bullish, rsi_4h, volume_signal, volume_ratio, swing_high/low_20, dist_to_swing_*
  #
  # LONG paths (if daily_bullish=True):
  #   bollinger_mid_bounce:   near_mid ±150pts, RSI 35-55 (RSI_ENTRY_HIGH_BOUNCE), bounce_starting (price>prev_close)
  #                           above_ema50 gate REMOVED (2026-03-01). EMA50 status in reasoning string for AI.
  #   bollinger_lower_bounce: near_lower ±80pts, RSI 20-40, lower_wick >=15pts
  #                           (NO above_ema50 gate — price expected below EMA50 at lower band)
  #   ema50_bounce:           DISABLED (ENABLE_EMA50_BOUNCE_SETUP=False)
  #
  # SHORT paths (if daily_bullish=False):
  #   bollinger_upper_rejection: near_upper ±150pts, RSI 55-75, below_ema50
  #   ema50_rejection:           price <=ema50+2, dist ≤150, RSI 50-70
  #
  # SL/TP: DEFAULT_SL_DISTANCE=150, DEFAULT_TP_DISTANCE=400 (from settings.py)
  # CRITICAL: daily_bullish=None → both branches skip → found=False always
  # Updated 2026-02-28: all thresholds recalibrated for Nikkei ~55k level
  # Updated 2026-03-01: above_ema50 gate removed from BB mid bounce; RSI upper limit 48→55

detect_higher_lows(prices, lookback=5) -> bool
  # Returns True if last 2-3 swing lows are ascending

detect_lower_highs(prices, lookback=5) -> bool
  # Returns True if last 2-3 swing highs are descending

_std_dev(values) -> float   # Population std dev (helper)
_last(lst) -> Optional[float]  # Last non-None value, rounded to 2dp (helper)
