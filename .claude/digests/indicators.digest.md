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
  # Output keys: price, open, high, low, bollinger_upper/mid/lower, ema50, ema200,
  #              rsi, vwap, above_ema50, above_ema200, above_vwap,
  #              ema200_available, above_ema200_fallback, bollinger_percentile
  # NOTE: needs 200 candles for EMA200; <200 logs warning and sets above_ema200_fallback=above_ema50

detect_setup(tf_daily, tf_4h, tf_15m, tf_5m=None) -> dict
  # Bidirectional. Returns: found, type, direction, entry, sl, tp, reasoning, indicators_snapshot
  # LONG paths (if daily_bullish=True):  bollinger_mid_bounce, ema50_bounce
  # SHORT paths (if daily_bullish=False): bollinger_upper_rejection, ema50_rejection
  # CRITICAL: daily_bullish=None → both branches skip → found=False always
  #   daily_bullish = tf_daily.get("above_ema200_fallback")
  # LONG BB mid bounce: near_mid_pts ±30, rsi_ok_long 35-60, above_ema50
  # LONG EMA50 bounce:  dist_ema50 ≤30, rsi_15m < 55, price >= ema50_15m - 10
  # SHORT BB upper:     near_upper_pts ±30, rsi_ok_short 55-75, below_ema50
  # SHORT EMA50 reject: price <= ema50_15m + 2, dist ≤30, rsi 50-70
  # LONG SL: entry - 200, capped at ema50_15m - 20. TP: entry + 400
  # SHORT SL: entry + 200. TP: entry - 400

detect_higher_lows(prices, lookback=5) -> bool
  # Returns True if last 2-3 swing lows are ascending

detect_lower_highs(prices, lookback=5) -> bool
  # Returns True if last 2-3 swing highs are descending

_std_dev(values) -> float   # Population std dev (helper)
_last(lst) -> Optional[float]  # Last non-None value, rounded to 2dp (helper)
