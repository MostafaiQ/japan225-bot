# core/indicators.py — DIGEST
# Purpose: Pure math. No API calls, no side effects. Fully testable.
# Updated 2026-03-04: 4 new LONG momentum setups, 2 new SHORT momentum setups.
#   Fibonacci S/R enhanced in _build_confluence(). indicators_snapshot expanded with ema9_15m, above_ema9, above_ema200, fibonacci dict.

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

heiken_ashi(opens, highs, lows, closes) -> (ha_open, ha_high, ha_low, ha_close)
  # First candle seeded from raw OHLC. Subsequent candles use prior HA values.

analyze_timeframe(candles: list[dict]) -> dict
  # Output keys: price, open, high, low, bollinger_upper/mid/lower,
  #   ema9, ema50, ema200, rsi, vwap,
  #   above_ema9, above_ema50, above_ema200, above_vwap,
  #   fibonacci (dict: fib_236/382/500/618/786), fib_near (str|None),
  #   ha_bullish, ha_streak, fvg_bullish/bearish, fvg_level,
  #   pullback_depth, avg_candle_range, bb_width,
  #   candlestick_pattern/direction/strength, body_trend, consecutive_direction

_build_confluence(tf_15m: dict, direction: str) -> (list[str], list[str])
  # Returns (confluence_list, counter_list) from Phase 1 indicators.
  # NEW: Fibonacci S/R — uses full fibonacci dict (within 100pts) as support/resistance context.
  #   LONG: fib below price = support held. Fib above = overhead resistance.
  #   SHORT: fib above price = resistance holding. Fib below = support below.

detect_setup(tf_daily, tf_4h, tf_15m, tf_5m=None, exclude_direction=None) -> dict
  # Bidirectional — NO daily hard gate. C1 in confidence.py penalizes counter-trend.
  # Returns: found, type, direction, entry, sl, tp, reasoning, indicators_snapshot
  # indicators_snapshot NOW includes: ema9_15m, above_ema9, above_ema200, fibonacci (full dict)
  #
  # LONG mean-reversion (setups 1-5):
  #   bollinger_mid_bounce, bollinger_lower_bounce, ema50_bounce(disabled),
  #   oversold_reversal, extreme_oversold_reversal
  #
  # LONG momentum/trend-following (setups 6-9, NEW 2026-03-04):
  #   breakout_long:              near BB upper(200pts) or swing_high(100pts) + vol≥1.3x + HA bullish + above EMA50 | RSI 55-75
  #   vwap_bounce_long:           near VWAP(120pts) + above EMA50 + bounce confirm (HA/candle/wick) | RSI 40-65
  #   ema9_pullback_long:         near EMA9(100pts) + above EMA50 + HA bullish or turning | RSI 40-65
  #   momentum_continuation_long: above EMA50 + above VWAP + HA streak≥2 + vol not LOW | RSI 45-70 (broadest catch-all)
  #
  # SHORT mean-reversion (13 total): bb_upper_rejection, ema50_rejection, bb_mid_rejection,
  #   overbought_reversal, breakdown_continuation, dead_cat_bounce_short, bear_flag_breakdown,
  #   vwap_rejection_short, high_volume_distribution, multi_tf_bearish, ema200_rejection,
  #   lower_lows_bearish_momentum, pivot_r1_rejection
  #
  # SHORT momentum/trend-following (NEW 2026-03-04):
  #   momentum_continuation_short: below EMA50+VWAP + HA streak≤-2 + vol not LOW | RSI 30-55
  #   vwap_rejection_short_momentum: near VWAP(120pts) from below + below EMA50 + rejection confirm | RSI 35-60
  #
  # SL/TP: DEFAULT_SL_DISTANCE=150, DEFAULT_TP_DISTANCE=400

detect_higher_lows(prices, lookback=5) -> bool
detect_lower_highs(prices, lookback=5) -> bool

_std_dev(values) -> float   # Population std dev (helper)
_last(lst) -> Optional[float]  # Last non-None value, rounded to 2dp (helper)
