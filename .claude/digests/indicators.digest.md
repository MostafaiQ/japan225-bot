# core/indicators.py — DIGEST
# Purpose: Pure math. No API calls, no side effects. Fully testable.
# Updated 2026-03-04: 4 new LONG momentum setups, 2 new SHORT momentum setups.
#   Fibonacci S/R enhanced in _build_confluence(). indicators_snapshot expanded with ema9_15m, above_ema9, above_ema200, fibonacci dict.
# Updated 2026-03-05: 4 new market structure functions. analyze_timeframe() extended.
#   indicators_snapshot expanded with anchored VWAPs, volume profile, equal levels, PDH/PDL sweep.

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
  #   pullback_depth, avg_candle_range, bb_width, atr,
  #   candlestick_pattern/direction/strength, body_trend, consecutive_direction,
  #   # NEW (2026-03-05):
  #   anchored_vwap_daily, anchored_vwap_weekly (None if no timestamp/volume data),
  #   volume_poc, volume_vah, volume_val (None if no volume),
  #   equal_highs_zones (list), equal_lows_zones (list)

anchored_vwap(candles: list[dict], anchor_isodate: str) -> float | None
  # VWAP from candles on/after anchor_isodate (YYYY-MM-DD). Requires 'timestamp' + 'volume' fields.
  # Returns None if no matching candles or no volume.

compute_volume_profile(candles: list[dict], lookback: int = 50, bucket_size: int = 25) -> dict
  # Returns {"poc": int|None, "vah": int|None, "val": int|None}
  # POC = price level with most volume. VAH/VAL = 70% value area bounds.
  # bucket_size=25 works well for Japan 225. Returns all None if no volume data.

detect_equal_levels(candles: list[dict], lookback: int = 30, tolerance: float = 20.0) -> dict
  # Returns {"equal_highs_zones": list[float], "equal_lows_zones": list[float]}
  # Zones are clusters of 2+ candle highs/lows within `tolerance` points of each other.

compute_session_context(candles_15m: list[dict], candles_daily: list[dict] | None = None) -> dict
  # Returns: session_open, asia_high, asia_low, pdh, pdl, prev_week_high, prev_week_low, gap_pts
  # session_open: first candle open in current session (Tokyo/London/NY by UTC hour)
  # asia_high/low: 00:00–05:59 UTC range for today
  # pdh/pdl: candles_daily[-2] high/low (yesterday)
  # gap_pts: today open - yesterday close (from daily candles)
  # prev_week_high/low: last calendar week Mon–Sun range
  # Called from monitor.py (standalone, not called from analyze_timeframe)

_build_confluence(tf_15m: dict, direction: str) -> (list[str], list[str])
  # Returns (confluence_list, counter_list) from Phase 1 indicators.
  # Fibonacci S/R — uses full fibonacci dict (within 100pts) as support/resistance context.
  #   LONG: fib below price = support held. Fib above = overhead resistance.
  #   SHORT: fib above price = resistance holding. Fib below = support below.

detect_setup(tf_daily, tf_4h, tf_15m, tf_5m=None, exclude_direction=None) -> dict
  # Bidirectional — NO daily hard gate. C1 in confidence.py penalizes counter-trend.
  # Returns: found, type, direction, entry, sl, tp, reasoning, indicators_snapshot
  # indicators_snapshot includes: ema9_15m, above_ema9, above_ema200, fibonacci (full dict)
  #   PLUS (2026-03-05): anchored_vwap_daily/weekly, volume_poc/vah/val,
  #   equal_highs/lows_zones, pdh_daily, pdl_daily, prev_week_high/low (always None — injected by monitor),
  #   pdh_swept (bool), pdl_swept (bool)
  #
  # LONG mean-reversion (setups 1-5):
  #   bollinger_mid_bounce, bollinger_lower_bounce, ema50_bounce(disabled),
  #   oversold_reversal, extreme_oversold_reversal
  #
  # LONG momentum/trend-following (setups 6-9):
  #   breakout_long:              near BB upper(200pts) or swing_high(100pts) + vol≥1.3x + HA bullish + above EMA50 | RSI 55-75
  #   vwap_bounce_long:           near VWAP(120pts) + above EMA50 + bounce confirm (HA/candle/wick) | RSI 40-65
  #   ema9_pullback_long:         near EMA9(100pts) + above EMA50 + HA bullish or turning | RSI 40-65
  #   momentum_continuation_long: above EMA50 + above VWAP + HA streak≥2 + vol not LOW | RSI 45-70
  #
  # SHORT mean-reversion (13 total): bb_upper_rejection, ema50_rejection, bb_mid_rejection,
  #   overbought_reversal, breakdown_continuation, dead_cat_bounce_short, bear_flag_breakdown,
  #   vwap_rejection_short, high_volume_distribution, multi_tf_bearish, ema200_rejection,
  #   lower_lows_bearish_momentum, pivot_r1_rejection
  #
  # SHORT momentum/trend-following:
  #   momentum_continuation_short: below EMA50+VWAP + HA streak≤-2 + vol not LOW | RSI 30-55
  #   vwap_rejection_short_momentum: near VWAP(120pts) from below + below EMA50 + rejection confirm | RSI 35-60
  #
  # SL/TP: DEFAULT_SL_DISTANCE=150, DEFAULT_TP_DISTANCE=400

detect_higher_lows(prices, lookback=5) -> bool
detect_lower_highs(prices, lookback=5) -> bool

_std_dev(values) -> float   # Population std dev (helper)
_last(lst) -> Optional[float]  # Last non-None value, rounded to 2dp (helper)
