"""
Technical indicator calculations for Japan 225 Trading Bot.
Pure math - no API calls, no side effects. Fully testable.

Indicators: Bollinger Bands, EMA 50/200, RSI 14, VWAP
Input: lists/arrays of OHLCV data
Output: dicts with calculated values
"""
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def ema(prices: list[float], period: int) -> list[float]:
    """
    Exponential Moving Average.
    Returns list same length as input (first `period-1` values are SMA-seeded).
    """
    if len(prices) < period:
        return []
    
    multiplier = 2 / (period + 1)
    result = []
    
    # Seed with SMA
    sma = sum(prices[:period]) / period
    result = [None] * (period - 1) + [sma]
    
    for i in range(period, len(prices)):
        val = (prices[i] - result[-1]) * multiplier + result[-1]
        result.append(val)
    
    return result


def sma(prices: list[float], period: int) -> list[float]:
    """Simple Moving Average."""
    if len(prices) < period:
        return []
    result = [None] * (period - 1)
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        result.append(sum(window) / period)
    return result


def bollinger_bands(
    closes: list[float], period: int = 20, num_std: float = 2.0
) -> dict:
    """
    Bollinger Bands: midband (SMA), upper, lower.
    Returns dict with 'upper', 'mid', 'lower' lists.
    """
    mid = sma(closes, period)
    upper = []
    lower = []
    
    for i in range(len(closes)):
        if mid[i] is None:
            upper.append(None)
            lower.append(None)
            continue
        window = closes[i - period + 1 : i + 1]
        std = _std_dev(window)
        upper.append(mid[i] + num_std * std)
        lower.append(mid[i] - num_std * std)
    
    return {"upper": upper, "mid": mid, "lower": lower}


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """
    Relative Strength Index (Wilder's smoothing method).
    Returns list same length as input.
    """
    if len(closes) < period + 1:
        return []
    
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    
    # First average: simple average of first `period` values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    result = [None] * period
    
    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100 - (100 / (1 + rs)))
    
    # Subsequent values: Wilder's smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - (100 / (1 + rs)))
    
    return result


def vwap(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> list[float]:
    """
    Volume Weighted Average Price.
    Assumes all candles are from the same session (daily reset).
    """
    if not highs or len(highs) != len(volumes):
        return []
    
    result = []
    cumulative_tp_vol = 0.0
    cumulative_vol = 0.0
    
    for i in range(len(highs)):
        typical_price = (highs[i] + lows[i] + closes[i]) / 3
        cumulative_tp_vol += typical_price * volumes[i]
        cumulative_vol += volumes[i]
        
        if cumulative_vol == 0:
            result.append(typical_price)
        else:
            result.append(cumulative_tp_vol / cumulative_vol)
    
    return result


def analyze_timeframe(candles: list[dict]) -> dict:
    """
    Full indicator analysis for a single timeframe.
    
    Input: list of candle dicts with keys:
        'open', 'high', 'low', 'close', 'volume', 'timestamp'
    
    Output: dict with all indicator values for the latest candle.
    """
    if len(candles) < 200:
        logger.warning(
            f"analyze_timeframe: only {len(candles)} candles available "
            f"(need 200 for EMA200). EMA200 will be None. "
            f"Using EMA50 as trend fallback where possible."
        )
    
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    
    # Calculate all indicators
    bb = bollinger_bands(closes, 20, 2.0)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi_vals = rsi(closes, 14)
    vwap_vals = vwap(highs, lows, closes, volumes) if any(v > 0 for v in volumes) else []
    
    # Get latest values (last element)
    current_price = closes[-1]
    
    result = {
        "price": current_price,
        "open": candles[-1]["open"],
        "high": candles[-1]["high"],
        "low": candles[-1]["low"],
        "bollinger_upper": _last(bb["upper"]),
        "bollinger_mid": _last(bb["mid"]),
        "bollinger_lower": _last(bb["lower"]),
        "ema50": _last(ema50),
        "ema200": _last(ema200),
        "rsi": _last(rsi_vals),
        "vwap": _last(vwap_vals) if vwap_vals else None,
    }
    
    # Price position analysis
    result["above_ema50"] = current_price > result["ema50"] if result["ema50"] else None
    result["above_ema200"] = current_price > result["ema200"] if result["ema200"] else None
    result["above_vwap"] = current_price > result["vwap"] if result["vwap"] else None

    # EMA200 fallback: if unavailable, use EMA50 as trend proxy and flag it
    result["ema200_available"] = result["ema200"] is not None
    if not result["ema200_available"] and result["ema50"] is not None:
        result["above_ema200_fallback"] = result["above_ema50"]
        logger.debug("EMA200 unavailable — using EMA50 as trend fallback")
    else:
        result["above_ema200_fallback"] = result["above_ema200"]
    
    # Bollinger position: -1 (at lower), 0 (at mid), 1 (at upper)
    if result["bollinger_upper"] and result["bollinger_lower"]:
        bb_range = result["bollinger_upper"] - result["bollinger_lower"]
        if bb_range > 0:
            result["bollinger_percentile"] = (
                (current_price - result["bollinger_lower"]) / bb_range
            )
        else:
            result["bollinger_percentile"] = 0.5
    
    return result


def detect_lower_highs(prices: list[float], lookback: int = 5) -> bool:
    """Check if recent swing highs are making lower highs (bearish structure)."""
    if len(prices) < lookback * 2:
        return False

    highs = []
    for i in range(2, len(prices) - 2):
        if prices[i] > prices[i - 1] and prices[i] > prices[i - 2]:
            if prices[i] > prices[i + 1] and prices[i] > prices[i + 2]:
                highs.append(prices[i])

    if len(highs) < 2:
        return False

    recent_highs = highs[-3:]
    return all(recent_highs[i] < recent_highs[i - 1] for i in range(1, len(recent_highs)))


def detect_higher_lows(prices: list[float], lookback: int = 5) -> bool:
    """Check if recent swing lows are making higher lows."""
    if len(prices) < lookback * 2:
        return False
    
    lows = []
    for i in range(2, len(prices) - 2):
        if prices[i] < prices[i - 1] and prices[i] < prices[i - 2]:
            if prices[i] < prices[i + 1] and prices[i] < prices[i + 2]:
                lows.append(prices[i])
    
    if len(lows) < 2:
        return False
    
    # Check last 2-3 swing lows are ascending
    recent_lows = lows[-3:]
    return all(recent_lows[i] > recent_lows[i - 1] for i in range(1, len(recent_lows)))


def detect_setup(
    tf_daily: dict,
    tf_4h: dict,
    tf_15m: dict,
    tf_5m: Optional[dict] = None,
) -> dict:
    """
    Detect trading setup across multiple timeframes. Bidirectional (LONG + SHORT).

    Returns dict with:
        'found': bool
        'type': str (setup name)
        'direction': 'LONG' or 'SHORT'
        'entry': float
        'sl': float
        'tp': float
        'reasoning': str
        'indicators_snapshot': dict  (key values for logging)
    """
    result = {
        "found": False,
        "type": None,
        "direction": None,
        "entry": None,
        "sl": None,
        "tp": None,
        "reasoning": "",
        "indicators_snapshot": {},
    }

    price = tf_15m.get("price")
    rsi_15m = tf_15m.get("rsi")
    bb_mid = tf_15m.get("bollinger_mid")
    bb_upper = tf_15m.get("bollinger_upper")
    bb_lower = tf_15m.get("bollinger_lower")
    ema50_15m = tf_15m.get("ema50")
    rsi_4h = tf_4h.get("rsi")

    # Use EMA200 with fallback to EMA50 for trend determination
    daily_bullish = tf_daily.get("above_ema200_fallback")

    result["indicators_snapshot"] = {
        "price": price,
        "rsi_15m": rsi_15m,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "ema50_15m": ema50_15m,
        "daily_bullish": daily_bullish,
        "rsi_4h": rsi_4h,
    }

    if not price:
        result["reasoning"] = "No price data available."
        return result

    # ============================================================
    # LONG SETUPS
    # ============================================================
    if daily_bullish:
        # 4H RSI not overbought
        if rsi_4h and rsi_4h > 75:
            # Don't block entirely — still check, but note it
            logger.debug(f"4H RSI overbought ({rsi_4h:.1f}) — LONG setup quality reduced")

        # --- LONG Setup 1: Bollinger Mid Bounce ---
        if bb_mid and rsi_15m:
            near_mid_pts = abs(price - bb_mid) <= 30  # Fixed: point distance, not percentile
            rsi_ok_long = 35 <= rsi_15m <= 60  # expanded: 55→60, valid pullbacks at RSI 56-60
            above_ema50 = tf_15m.get("above_ema50")

            if near_mid_pts and rsi_ok_long and above_ema50:
                entry = price
                sl = entry - 200
                if ema50_15m:
                    sl = max(sl, ema50_15m - 20)
                tp = entry + 400

                result.update({
                    "found": True,
                    "type": "bollinger_mid_bounce",
                    "direction": "LONG",
                    "entry": round(entry, 1),
                    "sl": round(sl, 1),
                    "tp": round(tp, 1),
                    "reasoning": (
                        f"LONG: BB mid bounce on 15M. Price {abs(price - bb_mid):.0f}pts from mid ({bb_mid:.0f}). "
                        f"RSI {rsi_15m:.1f} in zone. Above EMA50. Daily bullish."
                    ),
                })
                return result

        # --- LONG Setup 2: EMA50 Bounce ---
        if ema50_15m and rsi_15m:
            dist_ema50 = abs(price - ema50_15m)
            if dist_ema50 <= 30 and rsi_15m < 55 and price >= ema50_15m - 10:  # < 50 → < 55
                entry = price
                sl = entry - 200
                tp = entry + 400

                result.update({
                    "found": True,
                    "type": "ema50_bounce",
                    "direction": "LONG",
                    "entry": round(entry, 1),
                    "sl": round(sl, 1),
                    "tp": round(tp, 1),
                    "reasoning": (
                        f"LONG: EMA50 bounce on 15M. Price {dist_ema50:.0f}pts from EMA50 ({ema50_15m:.0f}). "
                        f"RSI {rsi_15m:.1f}. Daily trend bullish."
                    ),
                })
                return result

    # ============================================================
    # SHORT SETUPS (daily bearish)
    # ============================================================
    if not daily_bullish and daily_bullish is not None:
        # 4H RSI not oversold
        if rsi_4h and rsi_4h < 25:
            logger.debug(f"4H RSI oversold ({rsi_4h:.1f}) — SHORT setup quality reduced")

        # --- SHORT Setup 1: Bollinger Upper Rejection ---
        if bb_upper and bb_mid and rsi_15m:
            near_upper_pts = abs(price - bb_upper) <= 30
            rsi_ok_short = 55 <= rsi_15m <= 75
            below_ema50 = not tf_15m.get("above_ema50")

            if near_upper_pts and rsi_ok_short and below_ema50:
                entry = price
                sl = entry + 200
                tp = entry - 400

                result.update({
                    "found": True,
                    "type": "bollinger_upper_rejection",
                    "direction": "SHORT",
                    "entry": round(entry, 1),
                    "sl": round(sl, 1),
                    "tp": round(tp, 1),
                    "reasoning": (
                        f"SHORT: BB upper rejection on 15M. Price {abs(price - bb_upper):.0f}pts from upper ({bb_upper:.0f}). "
                        f"RSI {rsi_15m:.1f} in zone. Below EMA50. Daily bearish."
                    ),
                })
                return result

        # --- SHORT Setup 2: EMA50 Rejection (rallied up to EMA50, getting turned away) ---
        if ema50_15m and rsi_15m:
            dist_ema50 = abs(price - ema50_15m)
            # Price must be at or just below EMA50 (came up to test it)
            at_ema50_from_below = price <= ema50_15m + 2 and dist_ema50 <= 30  # +5 → +2 (Finding 5)
            if at_ema50_from_below and 50 <= rsi_15m <= 70:
                entry = price
                sl = entry + 200
                tp = entry - 400

                result.update({
                    "found": True,
                    "type": "ema50_rejection",
                    "direction": "SHORT",
                    "entry": round(entry, 1),
                    "sl": round(sl, 1),
                    "tp": round(tp, 1),
                    "reasoning": (
                        f"SHORT: EMA50 rejection on 15M. Price {dist_ema50:.0f}pts from EMA50 ({ema50_15m:.0f}), "
                        f"testing from below. RSI {rsi_15m:.1f}. Daily trend bearish."
                    ),
                })
                return result

    rsi_str = f"{rsi_15m:.1f}" if rsi_15m is not None else "N/A"
    daily_str = "bullish" if daily_bullish else ("bearish" if daily_bullish is not None else "N/A")
    result["reasoning"] = (
        f"No setup. Price={price:.0f} | RSI={rsi_str} | Daily={daily_str}"
    )
    return result


# ============================================
# HELPER FUNCTIONS
# ============================================

def _std_dev(values: list[float]) -> float:
    """Population standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


def _last(lst: list) -> Optional[float]:
    """Get last non-None value from a list."""
    if not lst:
        return None
    for val in reversed(lst):
        if val is not None:
            return round(val, 2)
    return None
