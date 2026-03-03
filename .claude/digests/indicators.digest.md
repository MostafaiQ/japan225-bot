# core/indicators.py — DIGEST
# Purpose: Pure math. No API calls, no side effects. Fully testable.
# Updated 2026-03-02: Phase 1 indicators (HA, FVG, Fibonacci, PDH/PDL, liquidity sweep)
# Updated 2026-03-02: _build_confluence() wired into all 4 setup paths. indicators_snapshot expanded with Phase 1 keys.
# Updated 2026-03-02: bb_mid_bounce RSI 35→30, relaxed bounce_starting gate, BB_LOWER 80→150pts, new oversold_reversal type.
# Updated 2026-03-02: pullback_depth, avg_candle_range, bb_width added to analyze_timeframe() + indicators_snapshot.
# Updated 2026-03-03: 3 new SHORT setups: bb_mid_rejection, overbought_reversal, breakdown_continuation.
# Updated 2026-03-03: Bear market improvements: dead_cat_bounce_short no longer requires daily bearish
#   (fires on locally_bearish: below EMA50 + HA streak ≤-2); bear_flag_breakdown RSI floor 35→28;
#   new vwap_rejection_short (no daily req); multi_tf_bearish works without rsi_4h (pre-screen safe).
#   Total SHORT setups: 13 (bollinger_upper_rejection, ema50_rejection, bb_mid_rejection,
#   overbought_reversal, breakdown_continuation, dead_cat_bounce_short, bear_flag_breakdown,
#   vwap_rejection_short, high_volume_distribution, multi_tf_bearish, ema200_rejection,
#   lower_lows_bearish_momentum, pivot_resistance_rejection).

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
  # Input: candles with open/high/low/close/volume/timestamp
  # Output keys: price, open, high, low, bollinger_upper/mid/lower,
  #              ema9, ema50, ema200, rsi, vwap,
  #              above_ema9, above_ema50, above_ema200, above_vwap,
  #              ema200_available, above_ema200_fallback, bollinger_percentile,
  #              prev_close,
  #              volume_ratio, volume_signal ("HIGH"|"NORMAL"|"LOW"),
  #              NOTE: volume uses prev completed candle if current < 20% of avg (partial candle guard)
  #              swing_high_20, swing_low_20,
  #              dist_to_swing_high, dist_to_swing_low,
  #   --- Phase 1 indicators ---
  #              ha_bullish (bool), ha_streak (int, +bullish/-bearish),
  #              fvg_bullish (bool), fvg_bearish (bool), fvg_level (float|None),
  #              fibonacci (dict: fib_236/382/500/618/786), fib_near (str|None),
  #              prev_candle_high, prev_candle_low (PDH/PDL),
  #              swept_low (bool), swept_high (bool)
  #   --- Trade quality context ---
  #              pullback_depth (float): closes[-1] - closes[-6], negative = price fell
  #              avg_candle_range (float): mean(high-low) of last 5 candles (volatility proxy)
  #              bb_width (float|None): BB upper - BB lower (market regime)
  # NOTE: needs 200 candles for EMA200; <200 logs warning and uses EMA50 fallback

confirm_5m_entry(tf_5m: dict, direction: str) -> bool
  # 5M confirmation check. Returns True if tf_5m is None (pass-through).
  # LONG: price > EMA9, green candle, RSI > 45
  # SHORT: price < EMA9, red candle, RSI < 55

_build_confluence(tf_15m: dict, direction: str) -> (list[str], list[str])
  # Returns (confluence_list, counter_list) from Phase 1 indicators.
  # Checks: fib_near, swept_low/high, fvg_bullish/bearish, VWAP above/below, ha_streak.
  # Direction-aware: same signal can be confluence for LONG but counter for SHORT.

detect_setup(tf_daily, tf_4h, tf_15m, tf_5m=None) -> dict
  # Bidirectional — NO daily hard gate. C1 in confidence.py penalizes counter-trend.
  # Returns: found, type, direction, entry, sl, tp, reasoning, indicators_snapshot
  # indicators_snapshot includes: price, rsi_15m, bb_mid/upper/lower, ema50_15m,
  #   daily_bullish, rsi_4h, volume_signal, volume_ratio, swing_high/low_20, dist_to_swing_*,
  #   vwap, above_vwap, ha_bullish, ha_streak, fib_near, fvg_bullish/bearish, fvg_level,
  #   swept_low/high, prev_candle_high/low
  # Reasoning now includes "Confluence: ..." and "Caution: ..." from _build_confluence().
  #
  # LONG paths:
  #   bollinger_mid_bounce:   near_mid ±150pts, RSI 30-65, bounce_starting (price>prev_close OR lower_wick>=20 OR HA bullish OR bullish candle pattern)
  #                           above_ema50 gate REMOVED. EMA50 status in reasoning string for AI.
  #   bollinger_lower_bounce: near_lower ±150pts, RSI 20-40, lower_wick >=15pts
  #   extreme_oversold_reversal: RSI<22 + (4H near BB lower ±300pts OR 4H RSI<35) + reversal confirm (wick≥10 OR HA bullish OR candle pattern OR sweep). No daily req.
  #   oversold_reversal:      RSI<30 + daily bullish + reversal confirm (wick≥10 OR HA bullish OR candle pattern OR liquidity sweep)
  #   ema50_bounce:           DISABLED (ENABLE_EMA50_BOUNCE_SETUP=False)
  #
  # SHORT paths (13 total):
  #   bollinger_upper_rejection: near_upper ±150pts, RSI 55-75, below_ema50
  #   ema50_rejection:           price <=ema50+2, dist ≤150, RSI 50-70
  #   bb_mid_rejection:          near_mid ±150pts, RSI 40-65, rejection (price<prev_close OR wick≥20 OR HA bearish OR bearish pattern)
  #   overbought_reversal:       RSI>70 + daily bearish + reversal confirm (wick≥10 OR HA bearish OR bearish pattern OR swept_high)
  #   breakdown_continuation:    price >100pts below BB mid, RSI 25-45, below EMA50, HA streak ≤-2, vol not LOW
  #   dead_cat_bounce_short:     near BB mid/EMA9, RSI 43-62, below EMA50, HA reject.
  #                              FIRES when daily bearish OR locally_bearish (below EMA50 + HA streak ≤-2)
  #   bear_flag_breakdown:       price between BB lower–mid, RSI 28-52 (was 35), HA streak ≤-1, below EMA50/VWAP
  #   vwap_rejection_short:      NEW — price tests VWAP from below + rejects, RSI 43-60, below EMA50. No daily req.
  #   high_volume_distribution:  near BB upper/swept high, vol ratio ≥1.4, RSI 55-75, bearish reject
  #   multi_tf_bearish:          3+/4 local factors (rsi_15m<48, daily_bear, below_ema50, below_vwap) + HA bear.
  #                              FIXED: works in pre-screen (rsi_4h=None). Threshold 3/4 without 4H, 4/5 with.
  #   ema200_rejection:          price near 15M EMA200 from below, daily bearish, RSI 50-70, rejection
  #   lower_lows_bearish_momentum: strong bearish swing structure
  #   pivot_resistance_rejection: price at daily pivot level with rejection
  #
  # SL/TP: DEFAULT_SL_DISTANCE=150, DEFAULT_TP_DISTANCE=400 (from settings.py)

detect_higher_lows(prices, lookback=5) -> bool
detect_lower_highs(prices, lookback=5) -> bool

_std_dev(values) -> float   # Population std dev (helper)
_last(lst) -> Optional[float]  # Last non-None value, rounded to 2dp (helper)
