"""
Technical indicator calculations for Japan 225 Trading Bot.
Pure math - no API calls, no side effects. Fully testable.

Indicators: Bollinger Bands, EMA 50/200, RSI 14, VWAP
Input: lists/arrays of OHLCV data
Output: dicts with calculated values
"""
import math
from typing import Optional


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
        # Pad with available data but warn
        pass
    
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
    Detect trading setup across multiple timeframes.
    
    Returns dict with:
        'found': bool
        'type': str (setup name)
        'direction': 'LONG' or 'SHORT'
        'entry': float
        'sl': float
        'tp': float
        'reasoning': str
    """
    result = {
        "found": False,
        "type": None,
        "direction": None,
        "entry": None,
        "sl": None,
        "tp": None,
        "reasoning": "",
    }
    
    # --- Pre-checks ---
    # Daily must be bullish (price above EMA200)
    if not tf_daily.get("above_ema200"):
        result["reasoning"] = "Daily trend bearish (below EMA200). No long setups."
        return result
    
    # 4H RSI not overbought
    if tf_4h.get("rsi") and tf_4h["rsi"] > 75:
        result["reasoning"] = f"4H RSI overbought at {tf_4h['rsi']:.1f}. Wait for pullback."
        return result
    
    # --- Setup 1: Bollinger Mid Bounce (Primary) ---
    bb_mid = tf_15m.get("bollinger_mid")
    bb_pct = tf_15m.get("bollinger_percentile")
    rsi_15m = tf_15m.get("rsi")
    price = tf_15m.get("price")
    
    if bb_mid and bb_pct is not None and rsi_15m:
        # Price near Bollinger midband (within 30-60% of range)
        near_mid = 0.30 <= bb_pct <= 0.60
        # RSI in sweet spot
        rsi_ok = 35 <= rsi_15m <= 55
        # Price above EMAs on 15M
        above_emas = tf_15m.get("above_ema50") and tf_15m.get("above_ema200")
        
        if near_mid and rsi_ok and above_emas:
            entry = price
            sl = entry - 200  # Default, will be refined by AI
            tp = entry + 400  # 1:2 R:R
            
            # Refine SL: use EMA50 or recent swing low
            if tf_15m.get("ema50"):
                sl_ema = tf_15m["ema50"] - 20  # Buffer below EMA50
                sl = max(sl, sl_ema)  # Don't make SL too tight
            
            result.update({
                "found": True,
                "type": "bollinger_mid_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"Bollinger mid bounce on 15M. Price at {bb_pct:.0%} of BB range. "
                    f"RSI {rsi_15m:.1f} in entry zone. Above EMA50/200. "
                    f"Daily bullish, 4H not overbought."
                ),
            })
            return result
    
    # --- Setup 2: EMA50 Bounce (Secondary) ---
    if tf_15m.get("ema50") and price:
        distance_to_ema50 = abs(price - tf_15m["ema50"])
        # Within 30 points of EMA50
        if distance_to_ema50 <= 30 and rsi_15m and rsi_15m < 50:
            entry = price
            sl = tf_15m.get("ema200", entry - 250) - 20
            tp = entry + 400
            
            result.update({
                "found": True,
                "type": "ema50_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"EMA50 bounce on 15M. Price {distance_to_ema50:.0f}pts from EMA50. "
                    f"RSI {rsi_15m:.1f}. Daily trend bullish."
                ),
            })
            return result
    
    result["reasoning"] = "No clean setup found. Standing aside."
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
