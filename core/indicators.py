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
from config.settings import (
    RSI_ENTRY_HIGH_BOUNCE, ENABLE_EMA50_BOUNCE_SETUP,
    DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE,
)

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


def heiken_ashi(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> tuple[list, list, list, list]:
    """
    Heiken Ashi candle calculation.
    Returns (ha_open, ha_high, ha_low, ha_close).
    First candle seeded from raw OHLC. Subsequent candles use prior HA values.
    """
    n = len(closes)
    if n < 1:
        return [], [], [], []

    ha_open  = [None] * n
    ha_high  = [None] * n
    ha_low   = [None] * n
    ha_close = [None] * n

    # Seed first candle
    ha_close[0] = (opens[0] + highs[0] + lows[0] + closes[0]) / 4
    ha_open[0]  = (opens[0] + closes[0]) / 2
    ha_high[0]  = max(highs[0], ha_open[0], ha_close[0])
    ha_low[0]   = min(lows[0],  ha_open[0], ha_close[0])

    for i in range(1, n):
        ha_close[i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
        ha_open[i]  = (ha_open[i - 1] + ha_close[i - 1]) / 2
        ha_high[i]  = max(highs[i], ha_open[i], ha_close[i])
        ha_low[i]   = min(lows[i],  ha_open[i], ha_close[i])

    return ha_open, ha_high, ha_low, ha_close


def pivot_points(high: float, low: float, close: float) -> dict:
    """
    Standard floor pivot points from daily OHLC.
    PP = (H+L+C)/3, R1-R3, S1-S3. All rounded to 1dp.
    """
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2 * (pp - low)
    s3 = low - 2 * (high - pp)
    return {
        "pp": round(pp, 1),
        "r1": round(r1, 1), "r2": round(r2, 1), "r3": round(r3, 1),
        "s1": round(s1, 1), "s2": round(s2, 1), "s3": round(s3, 1),
    }


def detect_candlestick_patterns(candles: list[dict]) -> dict:
    """
    Detect classic candlestick patterns from the last 3-5 candles.
    Returns {pattern_name, pattern_direction, pattern_strength}.
    """
    result = {"pattern_name": None, "pattern_direction": None, "pattern_strength": None}
    if not candles or len(candles) < 1:
        return result

    c = candles[-1]
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body = abs(cl - o)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l
    total_range = h - l
    if total_range == 0:
        return result

    body_ratio = body / total_range

    # --- Single candle patterns ---
    # Doji: very small body relative to range
    if body_ratio < 0.1:
        result.update(pattern_name="doji", pattern_direction="neutral", pattern_strength="moderate")
        return result

    # Spinning top: small body, wicks on both sides
    if body_ratio < 0.3 and upper_wick > body * 0.5 and lower_wick > body * 0.5:
        result.update(pattern_name="spinning_top", pattern_direction="neutral", pattern_strength="weak")
        return result

    # Hammer: small body at top, long lower wick (bullish)
    if lower_wick >= body * 2 and upper_wick < body * 0.5 and body_ratio < 0.4:
        result.update(pattern_name="hammer", pattern_direction="bullish", pattern_strength="strong")
        return result

    # Inverted hammer: small body at bottom, long upper wick (bullish reversal)
    if upper_wick >= body * 2 and lower_wick < body * 0.5 and body_ratio < 0.4:
        result.update(pattern_name="inverted_hammer", pattern_direction="bullish", pattern_strength="moderate")
        return result

    # Marubozu: large body, minimal wicks
    if body_ratio > 0.85:
        direction = "bullish" if cl > o else "bearish"
        result.update(pattern_name="marubozu", pattern_direction=direction, pattern_strength="strong")
        return result

    # --- Two candle patterns (need >= 2 candles) ---
    if len(candles) >= 2:
        prev = candles[-2]
        po, ph, pl, pcl = prev["open"], prev["high"], prev["low"], prev["close"]
        prev_body = abs(pcl - po)
        prev_bullish = pcl > po
        curr_bullish = cl > o

        # Bullish engulfing: prev bearish, current bullish body engulfs prev body
        if not prev_bullish and curr_bullish and o <= pcl and cl >= po and body > prev_body:
            result.update(pattern_name="bullish_engulfing", pattern_direction="bullish", pattern_strength="strong")
            return result

        # Bearish engulfing: prev bullish, current bearish body engulfs prev body
        if prev_bullish and not curr_bullish and o >= pcl and cl <= po and body > prev_body:
            result.update(pattern_name="bearish_engulfing", pattern_direction="bearish", pattern_strength="strong")
            return result

        # Piercing line: prev bearish, current opens below prev low, closes above prev midpoint
        prev_mid = (po + pcl) / 2
        if not prev_bullish and curr_bullish and o < pl and cl > prev_mid and cl < po:
            result.update(pattern_name="piercing_line", pattern_direction="bullish", pattern_strength="moderate")
            return result

        # Dark cloud cover: prev bullish, current opens above prev high, closes below prev midpoint
        prev_mid_b = (po + pcl) / 2
        if prev_bullish and not curr_bullish and o > ph and cl < prev_mid_b and cl > po:
            result.update(pattern_name="dark_cloud_cover", pattern_direction="bearish", pattern_strength="moderate")
            return result

    # --- Three candle patterns (need >= 3 candles) ---
    if len(candles) >= 3:
        c1 = candles[-3]
        c2 = candles[-2]
        c3 = candles[-1]
        c1_bull = c1["close"] > c1["open"]
        c2_body = abs(c2["close"] - c2["open"])
        c2_range = c2["high"] - c2["low"]
        c3_bull = c3["close"] > c3["open"]

        # Morning star: bearish + small body + bullish (reversal)
        if (not c1_bull and c2_range > 0 and c2_body / c2_range < 0.3
                and c3_bull and c3["close"] > (c1["open"] + c1["close"]) / 2):
            result.update(pattern_name="morning_star", pattern_direction="bullish", pattern_strength="strong")
            return result

        # Evening star: bullish + small body + bearish (reversal)
        if (c1_bull and c2_range > 0 and c2_body / c2_range < 0.3
                and not c3_bull and c3["close"] < (c1["open"] + c1["close"]) / 2):
            result.update(pattern_name="evening_star", pattern_direction="bearish", pattern_strength="strong")
            return result

        # Three white soldiers: 3 consecutive bullish with increasing closes
        if (c1_bull and c2["close"] > c2["open"] and c3_bull
                and c2["close"] > c1["close"] and c3["close"] > c2["close"]
                and c2["open"] > c1["open"] and c3["open"] > c2["open"]):
            result.update(pattern_name="three_white_soldiers", pattern_direction="bullish", pattern_strength="strong")
            return result

        # Three black crows: 3 consecutive bearish with decreasing closes
        if (not c1_bull and c2["close"] < c2["open"] and not c3_bull
                and c2["close"] < c1["close"] and c3["close"] < c2["close"]
                and c2["open"] < c1["open"] and c3["open"] < c2["open"]):
            result.update(pattern_name="three_black_crows", pattern_direction="bearish", pattern_strength="strong")
            return result

    return result


def analyze_body_trend(candles: list[dict], lookback: int = 5) -> dict:
    """
    Analyze candle body sizes for momentum/exhaustion signals.
    Returns {body_trend, consecutive_direction, avg_body_size, wick_ratio}.
    """
    result = {
        "body_trend": "neutral",
        "consecutive_direction": 0,
        "avg_body_size": 0.0,
        "wick_ratio": 0.0,
    }
    if not candles or len(candles) < 2:
        return result

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    bodies = [abs(c["close"] - c["open"]) for c in recent]
    avg_body = sum(bodies) / len(bodies) if bodies else 0
    result["avg_body_size"] = round(avg_body, 1)

    # Body trend: compare first half vs second half avg body size
    if len(bodies) >= 4:
        mid = len(bodies) // 2
        first_half = sum(bodies[:mid]) / mid
        second_half = sum(bodies[mid:]) / len(bodies[mid:])
        if first_half > 0:
            ratio = second_half / first_half
            if ratio > 1.3:
                result["body_trend"] = "expanding"
            elif ratio < 0.7:
                result["body_trend"] = "contracting"

    # Consecutive direction: count streak from end
    streak = 0
    for c in reversed(recent):
        bullish = c["close"] > c["open"]
        bearish = c["close"] < c["open"]
        if streak == 0:
            if bullish:
                streak = 1
            elif bearish:
                streak = -1
            else:
                break  # doji breaks streak
        elif streak > 0 and bullish:
            streak += 1
        elif streak < 0 and bearish:
            streak -= 1
        else:
            break
    result["consecutive_direction"] = streak

    # Wick ratio: avg (upper + lower wick) / body
    wick_ratios = []
    for c in recent:
        body = abs(c["close"] - c["open"])
        if body > 0:
            upper_wick = c["high"] - max(c["open"], c["close"])
            lower_wick = min(c["open"], c["close"]) - c["low"]
            wick_ratios.append((upper_wick + lower_wick) / body)
    result["wick_ratio"] = round(sum(wick_ratios) / len(wick_ratios), 2) if wick_ratios else 0.0

    return result


def analyze_timeframe(candles: list[dict]) -> dict:
    """
    Full indicator analysis for a single timeframe.
    
    Input: list of candle dicts with keys:
        'open', 'high', 'low', 'close', 'volume', 'timestamp'
    
    Output: dict with all indicator values for the latest candle.
    """
    if len(candles) < 200:
        logger.debug(
            f"analyze_timeframe: {len(candles)} candles (EMA200 needs 200). "
            f"Using EMA50 fallback."
        )
    
    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    opens   = [c["open"]   for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    
    # Calculate all indicators
    bb = bollinger_bands(closes, 20, 2.0)
    ema9 = ema(closes, 9)
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
        "ema9": _last(ema9),
        "ema50": _last(ema50),
        "ema200": _last(ema200),
        "rsi": _last(rsi_vals),
        "vwap": _last(vwap_vals) if vwap_vals else None,
    }
    
    # Price position analysis
    result["above_ema9"]  = current_price > result["ema9"]  if result["ema9"]  else None
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
    
    # Previous close (for bounce confirmation: current > prev = turning up)
    result["prev_close"] = closes[-2] if len(closes) >= 2 else None

    # Bollinger position: -1 (at lower), 0 (at mid), 1 (at upper)
    if result["bollinger_upper"] and result["bollinger_lower"]:
        bb_range = result["bollinger_upper"] - result["bollinger_lower"]
        if bb_range > 0:
            result["bollinger_percentile"] = (
                (current_price - result["bollinger_lower"]) / bb_range
            )
        else:
            result["bollinger_percentile"] = 0.5

    # Volume analysis — is this a high-conviction or thin signal?
    # Always use the LAST COMPLETED candle (volumes[-2]), never the current forming one.
    # The latest candle (volumes[-1]) is almost always partial — its volume is meaningless.
    if any(v > 0 for v in volumes):
        recent_vols = [v for v in volumes[-20:] if v > 0]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        vol_completed = volumes[-2] if len(volumes) >= 2 else volumes[-1]
        vol_ratio = vol_completed / avg_vol if avg_vol > 0 else None
        result["volume_ratio"] = round(vol_ratio, 2) if vol_ratio is not None else None
        result["volume_signal"] = (
            "HIGH"   if vol_ratio and vol_ratio > 1.5 else
            "LOW"    if vol_ratio and vol_ratio < 0.7 else
            "NORMAL"
        ) if vol_ratio is not None else None
    else:
        result["volume_ratio"] = None
        result["volume_signal"] = None

    # Recent swing high/low (20-candle lookback) — nearest resistance and support
    n_sw = min(20, len(highs))
    swing_h = max(highs[-n_sw:])
    swing_l = min(lows[-n_sw:])
    result["swing_high_20"] = round(swing_h, 1)
    result["swing_low_20"]  = round(swing_l, 1)
    result["dist_to_swing_high"] = round(swing_h - current_price, 1)
    result["dist_to_swing_low"]  = round(current_price - swing_l, 1)

    # ── Heiken Ashi ───────────────────────────────────────────────────────────
    ha_open_v, ha_high_v, ha_low_v, ha_close_v = heiken_ashi(opens, highs, lows, closes)
    if ha_close_v and ha_close_v[-1] is not None:
        ha_bull = ha_close_v[-1] > ha_open_v[-1]
        result["ha_bullish"] = ha_bull
        # Count consecutive HA candles in same direction (positive=bullish, negative=bearish)
        streak = 0
        for i in range(len(ha_close_v) - 1, -1, -1):
            if ha_close_v[i] is None:
                break
            if (ha_close_v[i] > ha_open_v[i]) == ha_bull:
                streak += 1
            else:
                break
        result["ha_streak"] = streak if ha_bull else -streak
    else:
        result["ha_bullish"] = None
        result["ha_streak"]  = None

    # ── Fair Value Gap ────────────────────────────────────────────────────────
    # 3-candle imbalance: bullish FVG = candles[i-2].high < candles[i].low
    #                     bearish FVG = candles[i-2].low  > candles[i].high
    result["fvg_bullish"] = False
    result["fvg_bearish"] = False
    result["fvg_level"]   = None
    n_c = len(candles)
    if n_c >= 3:
        for i in range(n_c - 1, max(n_c - 6, 2), -1):
            if highs[i - 2] < lows[i]:
                result["fvg_bullish"] = True
                result["fvg_level"]   = round((highs[i - 2] + lows[i]) / 2, 1)
                break
            if lows[i - 2] > highs[i]:
                result["fvg_bearish"] = True
                result["fvg_level"]   = round((lows[i - 2] + highs[i]) / 2, 1)
                break

    # ── Fibonacci Retracement ─────────────────────────────────────────────────
    if result.get("swing_high_20") and result.get("swing_low_20"):
        sh = result["swing_high_20"]
        sl_f = result["swing_low_20"]
        rng = sh - sl_f
        fib_levels = {
            "fib_236": round(sh - 0.236 * rng, 1),
            "fib_382": round(sh - 0.382 * rng, 1),
            "fib_500": round(sh - 0.500 * rng, 1),
            "fib_618": round(sh - 0.618 * rng, 1),
            "fib_786": round(sh - 0.786 * rng, 1),
        }
        result["fibonacci"] = fib_levels
        # Nearest fib level within 50pts of current price
        fib_near = None
        min_dist = float("inf")
        for name, level in fib_levels.items():
            dist = abs(current_price - level)
            if dist <= 50 and dist < min_dist:
                min_dist = dist
                fib_near = name
        result["fib_near"] = fib_near
    else:
        result["fibonacci"] = {}
        result["fib_near"]  = None

    # ── PDH/PDL (previous candle high/low) ────────────────────────────────────
    if len(candles) >= 2:
        result["prev_candle_high"] = candles[-2]["high"]
        result["prev_candle_low"]  = candles[-2]["low"]
    else:
        result["prev_candle_high"] = None
        result["prev_candle_low"]  = None

    # ── Liquidity Sweep ───────────────────────────────────────────────────────
    # Previous 20-period swing excluding current candle, then check if current candle
    # swept past it intrabar but closed on the other side (reversal signal).
    if len(highs) >= 21:
        prev_swing_high = max(highs[-21:-1])
        prev_swing_low  = min(lows[-21:-1])
        # Bullish sweep: low dips below prev swing low but close is above it
        result["swept_low"]  = lows[-1] < prev_swing_low  and closes[-1] > prev_swing_low
        # Bearish sweep: high pushes above prev swing high but close is below it
        result["swept_high"] = highs[-1] > prev_swing_high and closes[-1] < prev_swing_high
    else:
        result["swept_low"]  = False
        result["swept_high"] = False

    # ── Candlestick Patterns ────────────────────────────────────────────────
    cp = detect_candlestick_patterns(candles[-5:] if len(candles) >= 5 else candles)
    result["candlestick_pattern"]   = cp["pattern_name"]
    result["candlestick_direction"] = cp["pattern_direction"]
    result["candlestick_strength"]  = cp["pattern_strength"]

    # ── Body Trend Analysis ─────────────────────────────────────────────────
    bt = analyze_body_trend(candles, lookback=5)
    result["body_trend"]              = bt["body_trend"]
    result["consecutive_direction"]   = bt["consecutive_direction"]
    result["avg_body_size"]           = bt["avg_body_size"]
    result["wick_ratio"]              = bt["wick_ratio"]

    # ── Pre-entry Context (trade quality filters) ────────────────────────
    # Pullback depth: price change over last 5 candles (negative = price fell)
    if len(closes) >= 6:
        result["pullback_depth"] = round(closes[-1] - closes[-6], 1)
    else:
        result["pullback_depth"] = 0.0

    # Average candle range (volatility proxy) — last 5 completed candles
    n_vol = min(5, len(candles))
    recent_ranges = [candles[-(i+1)]["high"] - candles[-(i+1)]["low"] for i in range(n_vol)]
    result["avg_candle_range"] = round(sum(recent_ranges) / len(recent_ranges), 1) if recent_ranges else 0.0

    # Bollinger Band width (market regime: narrow=squeeze, wide=trending)
    if result["bollinger_upper"] and result["bollinger_lower"]:
        result["bb_width"] = round(result["bollinger_upper"] - result["bollinger_lower"], 1)
    else:
        result["bb_width"] = None

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


def confirm_5m_entry(tf_5m: dict, direction: str) -> bool:
    """
    5M entry confirmation. Called AFTER 15M setup is detected.
    Returns True if 5M chart confirms the entry direction.
    If tf_5m is None or incomplete, returns True (pass-through — don't block).

    LONG confirmation: price > EMA9, green candle body, RSI > 45
    SHORT confirmation: price < EMA9, red candle body, RSI < 55
    """
    if not tf_5m:
        return True  # no 5M data → don't block

    price = tf_5m.get("price")
    ema9 = tf_5m.get("ema9")
    rsi_5m = tf_5m.get("rsi")
    candle_open = tf_5m.get("open")

    if not price or not ema9:
        return True  # insufficient data → pass-through

    if direction == "LONG":
        price_above_ema9 = price > ema9
        rsi_ok = rsi_5m is None or rsi_5m > 45
        body_green = candle_open is None or price > candle_open
        return price_above_ema9 and rsi_ok and body_green
    else:  # SHORT
        price_below_ema9 = price < ema9
        rsi_ok = rsi_5m is None or rsi_5m < 55
        body_red = candle_open is None or price < candle_open
        return price_below_ema9 and rsi_ok and body_red


def _build_confluence(tf_15m: dict, direction: str, pivots: dict = None) -> tuple[list[str], list[str]]:
    """
    Build confluence and counter-signal lists from Phase 1 indicators.
    Returns (confluence_list, counter_list).
    """
    conf = []
    counter = []
    is_long = direction == "LONG"

    # Fibonacci: near a key fib level
    fib_near = tf_15m.get("fib_near")
    if fib_near:
        conf.append(f"Fib {fib_near}")

    # Liquidity sweep
    swept_low = tf_15m.get("swept_low", False)
    swept_high = tf_15m.get("swept_high", False)
    if is_long:
        if swept_low:
            conf.append("swept low (bullish reversal)")
        if swept_high:
            counter.append("swept high (bearish reversal)")
    else:
        if swept_high:
            conf.append("swept high (bearish reversal)")
        if swept_low:
            counter.append("swept low (bullish reversal)")

    # FVG
    fvg_bullish = tf_15m.get("fvg_bullish", False)
    fvg_bearish = tf_15m.get("fvg_bearish", False)
    if is_long:
        if fvg_bullish:
            conf.append("bullish FVG (demand zone)")
        if fvg_bearish:
            counter.append("bearish FVG (supply zone)")
    else:
        if fvg_bearish:
            conf.append("bearish FVG (supply zone)")
        if fvg_bullish:
            counter.append("bullish FVG (demand zone)")

    # VWAP
    vwap_val = tf_15m.get("vwap")
    price = tf_15m.get("price")
    if vwap_val and price:
        above_vwap = tf_15m.get("above_vwap")
        if is_long:
            if above_vwap is False:
                conf.append("below VWAP (discount)")
            elif above_vwap is True:
                counter.append("above VWAP (premium)")
        else:
            if above_vwap is True:
                conf.append("above VWAP (premium)")
            elif above_vwap is False:
                counter.append("below VWAP (discount)")

    # HA streak
    ha_streak = tf_15m.get("ha_streak")
    if ha_streak is not None:
        if is_long:
            if ha_streak >= 2:
                conf.append(f"HA streak {ha_streak}")
            elif ha_streak <= -3:
                counter.append(f"HA streak {ha_streak} (bearish)")
        else:
            if ha_streak <= -2:
                conf.append(f"HA streak {ha_streak}")
            elif ha_streak >= 3:
                counter.append(f"HA streak {ha_streak} (bullish)")

    # Pivot points — institutional S/R levels
    if pivots and price:
        if is_long:
            for lvl in ("s1", "s2", "s3"):
                val = pivots.get(lvl)
                if val and abs(price - val) <= 100:
                    conf.append(f"near pivot {lvl.upper()} ({val:.0f})")
                    break
            for lvl in ("r1", "r2"):
                val = pivots.get(lvl)
                if val and abs(price - val) <= 100:
                    counter.append(f"near pivot {lvl.upper()} resistance ({val:.0f})")
                    break
        else:
            for lvl in ("r1", "r2", "r3"):
                val = pivots.get(lvl)
                if val and abs(price - val) <= 100:
                    conf.append(f"near pivot {lvl.upper()} ({val:.0f})")
                    break
            for lvl in ("s1", "s2"):
                val = pivots.get(lvl)
                if val and abs(price - val) <= 100:
                    counter.append(f"near pivot {lvl.upper()} support ({val:.0f})")
                    break

    # Candlestick pattern
    cp_dir = tf_15m.get("candlestick_direction")
    cp_name = tf_15m.get("candlestick_pattern")
    cp_str = tf_15m.get("candlestick_strength")
    if cp_name and cp_dir and cp_dir != "neutral":
        if is_long:
            if cp_dir == "bullish":
                conf.append(f"{cp_name} ({cp_str})")
            elif cp_dir == "bearish":
                counter.append(f"{cp_name} ({cp_str})")
        else:
            if cp_dir == "bearish":
                conf.append(f"{cp_name} ({cp_str})")
            elif cp_dir == "bullish":
                counter.append(f"{cp_name} ({cp_str})")

    # Body trend — exhaustion/momentum
    body_trend = tf_15m.get("body_trend")
    consec = tf_15m.get("consecutive_direction", 0)
    wick_r = tf_15m.get("wick_ratio", 0)
    if body_trend == "contracting":
        if is_long and consec < 0:
            conf.append("contracting bodies (sell-off exhaustion)")
        elif not is_long and consec > 0:
            conf.append("contracting bodies (rally exhaustion)")
    elif body_trend == "expanding":
        if is_long and consec > 0:
            conf.append("expanding bodies (bullish momentum)")
        elif not is_long and consec < 0:
            conf.append("expanding bodies (bearish momentum)")
    if wick_r and wick_r > 2.0:
        counter.append(f"high wick ratio {wick_r:.1f} (indecision)")

    return conf, counter


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

    # Pivot points from daily data (yesterday's completed candle)
    pivots = {}
    d_high = tf_daily.get("prev_candle_high")
    d_low = tf_daily.get("prev_candle_low")
    d_close = tf_daily.get("prev_close")
    if d_high and d_low and d_close:
        pivots = pivot_points(d_high, d_low, d_close)

    result["indicators_snapshot"] = {
        "price": price,
        "rsi_15m": rsi_15m,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "ema50_15m": ema50_15m,
        "daily_bullish": daily_bullish,
        "rsi_4h": rsi_4h,
        # Market structure context — fed directly to Sonnet/Opus
        "volume_signal": tf_15m.get("volume_signal"),
        "volume_ratio": tf_15m.get("volume_ratio"),
        "swing_high_20": tf_15m.get("swing_high_20"),
        "swing_low_20": tf_15m.get("swing_low_20"),
        "dist_to_swing_high": tf_15m.get("dist_to_swing_high"),
        "dist_to_swing_low": tf_15m.get("dist_to_swing_low"),
        # Phase 1 indicators
        "vwap": tf_15m.get("vwap"),
        "above_vwap": tf_15m.get("above_vwap"),
        "ha_bullish": tf_15m.get("ha_bullish"),
        "ha_streak": tf_15m.get("ha_streak"),
        "fib_near": tf_15m.get("fib_near"),
        "fvg_bullish": tf_15m.get("fvg_bullish"),
        "fvg_bearish": tf_15m.get("fvg_bearish"),
        "fvg_level": tf_15m.get("fvg_level"),
        "swept_low": tf_15m.get("swept_low"),
        "swept_high": tf_15m.get("swept_high"),
        "prev_candle_high": tf_15m.get("prev_candle_high"),
        "prev_candle_low": tf_15m.get("prev_candle_low"),
        # Phase 2 indicators
        "pivots": pivots,
        "candlestick_pattern": tf_15m.get("candlestick_pattern"),
        "candlestick_direction": tf_15m.get("candlestick_direction"),
        "candlestick_strength": tf_15m.get("candlestick_strength"),
        "body_trend": tf_15m.get("body_trend"),
        "consecutive_direction": tf_15m.get("consecutive_direction"),
        "avg_body_size": tf_15m.get("avg_body_size"),
        "wick_ratio": tf_15m.get("wick_ratio"),
        # Trade quality context
        "pullback_depth": tf_15m.get("pullback_depth"),
        "avg_candle_range": tf_15m.get("avg_candle_range"),
        "bb_width": tf_15m.get("bb_width"),
    }

    if not price:
        result["reasoning"] = "No price data available."
        return result

    # Note: daily_bullish informs reasoning/confidence but is no longer a hard gate.
    # Counter-trend setups are penalized by C1 in confidence.py — AI makes the final call.
    if rsi_4h and rsi_4h > 75:
        logger.debug(f"4H RSI overbought ({rsi_4h:.1f}) — LONG setup quality reduced")
    if rsi_4h and rsi_4h < 25:
        logger.debug(f"4H RSI oversold ({rsi_4h:.1f}) — SHORT setup quality reduced")

    daily_str = "Daily bullish" if daily_bullish else ("Daily bearish (counter-trend)" if daily_bullish is False else "Daily N/A")

    # ============================================================
    # LONG SETUPS (bidirectional — no daily gate, C1 in confidence penalizes counter-trend)
    # ============================================================
    # --- LONG Setup 1: Bollinger Mid Bounce ---
    if bb_mid and rsi_15m:
        near_mid_pts = abs(price - bb_mid) <= 150
        rsi_ok_long = 30 <= rsi_15m <= RSI_ENTRY_HIGH_BOUNCE  # widened from 35 to 30 (captures RSI 30-35 near BB mid)
        above_ema50 = tf_15m.get("above_ema50")
        prev_close = tf_15m.get("prev_close")
        bounce_starting = prev_close is not None and price > prev_close
        # Relaxed bounce gate for oversold: if RSI<40, accept alternative reversal signals
        if not bounce_starting and rsi_15m < 40:
            candle_open_b = tf_15m.get("open")
            candle_low_b  = tf_15m.get("low")
            lower_wick_b = (min(candle_open_b, price) - candle_low_b) if (candle_open_b is not None and candle_low_b is not None) else 0
            ha_bull = tf_15m.get("ha_bullish")
            candle_patterns = tf_15m.get("candlestick_patterns", [])
            bullish_pattern = any(p.get("direction") == "bullish" for p in candle_patterns) if candle_patterns else False
            bounce_starting = lower_wick_b >= 20 or ha_bull or bullish_pattern

        if near_mid_pts and rsi_ok_long and bounce_starting:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            if ema50_15m:
                sl = max(sl, ema50_15m - 20)
            tp = entry + DEFAULT_TP_DISTANCE

            ema50_note = "above EMA50" if above_ema50 else "below EMA50 (AI to evaluate)"
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            reasoning = (
                f"LONG: BB mid bounce on 15M. "
                f"Price {abs(price - bb_mid):.0f}pts from mid ({bb_mid:.0f}). "
                f"RSI {rsi_15m:.1f} in zone. {ema50_note}. {daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "bollinger_mid_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- LONG Setup 2: Bollinger Lower Band Bounce ---
    # Deeply oversold — strongest mean-reversion signal.
    # No above_ema50 gate: price may be below EMA50 at the lower band (expected).
    if bb_lower and rsi_15m:
        near_lower_pts = abs(price - bb_lower) <= 150  # widened from 80 to match BB mid threshold
        rsi_ok_lower = 20 <= rsi_15m <= 40
        candle_open_l = tf_15m.get("open")
        candle_low_l  = tf_15m.get("low")
        if candle_open_l is not None and candle_low_l is not None:
            lower_wick_l = min(candle_open_l, price) - candle_low_l
            rejection_l = lower_wick_l >= 15
        else:
            rejection_l = False

        if near_lower_pts and rsi_ok_lower and rejection_l:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE
            macro_note = (
                f" 4H RSI {rsi_4h:.1f} — multi-TF oversold confluence."
                if rsi_4h and rsi_4h < 40 else ""
            )
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            reasoning = (
                f"LONG: BB lower band bounce on 15M. "
                f"Price {abs(price - bb_lower):.0f}pts from lower ({bb_lower:.0f}). "
                f"RSI {rsi_15m:.1f} deeply oversold. "
                f"Lower wick {lower_wick_l:.0f}pts rejection. "
                f"{daily_str}.{macro_note}"
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "bollinger_lower_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- LONG Setup 3: EMA50 Bounce (disabled until validated — see ENABLE_EMA50_BOUNCE_SETUP) ---
    if ENABLE_EMA50_BOUNCE_SETUP and ema50_15m and rsi_15m:
        dist_ema50 = abs(price - ema50_15m)
        if dist_ema50 <= 150 and rsi_15m < 55 and price >= ema50_15m - 10:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE

            result.update({
                "found": True,
                "type": "ema50_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"LONG: EMA50 bounce on 15M. Price {dist_ema50:.0f}pts from EMA50 ({ema50_15m:.0f}). "
                    f"RSI {rsi_15m:.1f}. {daily_str}."
                ),
            })
            return result

    # --- LONG Setup 4: Oversold Reversal (extreme mean-reversion) ---
    # Fires when RSI < 30 and daily is bullish — textbook oversold reversal in uptrend.
    # Weaker reversal confirmation: any wick, HA turn, or candle pattern suffices.
    if rsi_15m and rsi_15m < 30 and daily_bullish:
        candle_open_os = tf_15m.get("open")
        candle_low_os  = tf_15m.get("low")
        lower_wick_os = (min(candle_open_os, price) - candle_low_os) if (candle_open_os is not None and candle_low_os is not None) else 0
        ha_bull_os = tf_15m.get("ha_bullish")
        swept_low = tf_15m.get("swept_low", False)
        candle_patterns_os = tf_15m.get("candlestick_patterns", [])
        bullish_pattern_os = any(p.get("direction") == "bullish" for p in candle_patterns_os) if candle_patterns_os else False
        reversal_confirm = lower_wick_os >= 10 or ha_bull_os or bullish_pattern_os or swept_low

        if reversal_confirm:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE
            rsi_4h_note = f" 4H RSI {rsi_4h:.1f} — multi-TF oversold." if rsi_4h and rsi_4h < 40 else ""
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            confirm_str = []
            if lower_wick_os >= 10:
                confirm_str.append(f"wick {lower_wick_os:.0f}pts")
            if ha_bull_os:
                confirm_str.append("HA bullish")
            if bullish_pattern_os:
                confirm_str.append("bullish candle pattern")
            if swept_low:
                confirm_str.append("liquidity sweep")
            reasoning = (
                f"LONG: Oversold reversal on 15M. "
                f"RSI {rsi_15m:.1f} extremely oversold in daily uptrend. "
                f"Reversal: {', '.join(confirm_str)}. "
                f"{daily_str}.{rsi_4h_note}"
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "oversold_reversal",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # ============================================================
    # SHORT SETUPS (bidirectional — no daily gate, C1 in confidence penalizes counter-trend)
    # ============================================================
    short_daily_str = "Daily bearish" if daily_bullish is False else ("Daily bullish (counter-trend SHORT)" if daily_bullish else "Daily N/A")

    # --- SHORT Setup 1: Bollinger Upper Rejection ---
    if bb_upper and bb_mid and rsi_15m:
        near_upper_pts = abs(price - bb_upper) <= 150
        rsi_ok_short = 55 <= rsi_15m <= 75
        below_ema50 = not tf_15m.get("above_ema50")

        if near_upper_pts and rsi_ok_short and below_ema50:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE

            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: BB upper rejection on 15M. "
                f"Price {abs(price - bb_upper):.0f}pts from upper ({bb_upper:.0f}). "
                f"RSI {rsi_15m:.1f} in zone. Below EMA50. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "bollinger_upper_rejection",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 2: EMA50 Rejection (rallied up to EMA50, getting turned away) ---
    if ema50_15m and rsi_15m:
        dist_ema50 = abs(price - ema50_15m)
        at_ema50_from_below = price <= ema50_15m + 2 and dist_ema50 <= 150
        if at_ema50_from_below and 50 <= rsi_15m <= 70:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE

            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: EMA50 rejection on 15M. Price {dist_ema50:.0f}pts from EMA50 ({ema50_15m:.0f}), "
                f"testing from below. RSI {rsi_15m:.1f}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "ema50_rejection",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 3: BB Mid Rejection (mirror of bb_mid_bounce LONG) ---
    # Price rallied up to BB mid as resistance and got rejected — heading back down.
    if bb_mid and rsi_15m:
        near_mid_pts = abs(price - bb_mid) <= 150
        rsi_ok_short_mid = 40 <= rsi_15m <= 65
        prev_close_s = tf_15m.get("prev_close")
        rejection_starting = prev_close_s is not None and price < prev_close_s
        # Relaxed rejection gate: accept alternative reversal signals
        if not rejection_starting and rsi_15m > 50:
            candle_open_r = tf_15m.get("open")
            candle_high_r = tf_15m.get("high")
            upper_wick_r = (candle_high_r - max(candle_open_r, price)) if (candle_open_r is not None and candle_high_r is not None) else 0
            ha_bear = tf_15m.get("ha_bullish") is False
            cp_dir_r = tf_15m.get("candlestick_direction")
            bearish_pattern_r = cp_dir_r == "bearish"
            rejection_starting = upper_wick_r >= 20 or ha_bear or bearish_pattern_r

        if near_mid_pts and rsi_ok_short_mid and rejection_starting:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE

            below_ema50_s = not tf_15m.get("above_ema50")
            ema50_note_s = "below EMA50" if below_ema50_s else "above EMA50 (AI to evaluate)"
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: BB mid rejection on 15M. "
                f"Price {abs(price - bb_mid):.0f}pts from mid ({bb_mid:.0f}). "
                f"RSI {rsi_15m:.1f} in zone. {ema50_note_s}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "bb_mid_rejection",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 4: Overbought Reversal (mirror of oversold_reversal LONG) ---
    # RSI > 70 = extremely overbought. If daily bearish → textbook mean-reversion short.
    if rsi_15m and rsi_15m > 70 and daily_bullish is False:
        candle_open_ob = tf_15m.get("open")
        candle_high_ob = tf_15m.get("high")
        upper_wick_ob = (candle_high_ob - max(candle_open_ob, price)) if (candle_open_ob is not None and candle_high_ob is not None) else 0
        ha_bear_ob = tf_15m.get("ha_bullish") is False
        swept_high_ob = tf_15m.get("swept_high", False)
        cp_dir_ob = tf_15m.get("candlestick_direction")
        bearish_pattern_ob = cp_dir_ob == "bearish"
        reversal_confirm_ob = upper_wick_ob >= 10 or ha_bear_ob or bearish_pattern_ob or swept_high_ob

        if reversal_confirm_ob:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            rsi_4h_note_ob = f" 4H RSI {rsi_4h:.1f} — multi-TF overbought." if rsi_4h and rsi_4h > 60 else ""
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            confirm_str_ob = []
            if upper_wick_ob >= 10:
                confirm_str_ob.append(f"wick {upper_wick_ob:.0f}pts")
            if ha_bear_ob:
                confirm_str_ob.append("HA bearish")
            if bearish_pattern_ob:
                confirm_str_ob.append("bearish candle pattern")
            if swept_high_ob:
                confirm_str_ob.append("liquidity sweep")
            reasoning = (
                f"SHORT: Overbought reversal on 15M. "
                f"RSI {rsi_15m:.1f} extremely overbought in daily downtrend. "
                f"Reversal: {', '.join(confirm_str_ob)}. "
                f"{short_daily_str}.{rsi_4h_note_ob}"
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "overbought_reversal",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 5: Breakdown Continuation (trend-following short) ---
    # Price already broke below key levels and keeps falling with momentum.
    # Not mean-reversion — this is a trend continuation play.
    if bb_mid and rsi_15m and ema50_15m:
        dist_below_mid = price - bb_mid  # negative when below mid
        below_mid_significant = dist_below_mid < -100
        rsi_ok_breakdown = 25 <= rsi_15m <= 45
        below_ema50_bd = not tf_15m.get("above_ema50")
        ha_streak_bd = tf_15m.get("ha_streak")
        ha_bearish_momentum = ha_streak_bd is not None and ha_streak_bd <= -2
        vol_signal_bd = tf_15m.get("volume_signal", "NORMAL")
        vol_ok_bd = vol_signal_bd != "LOW"

        if below_mid_significant and rsi_ok_breakdown and below_ema50_bd and ha_bearish_momentum and vol_ok_bd:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE

            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: Breakdown continuation on 15M. "
                f"Price {abs(dist_below_mid):.0f}pts below BB mid ({bb_mid:.0f}). "
                f"RSI {rsi_15m:.1f}, HA streak {ha_streak_bd}. "
                f"Below EMA50, vol={vol_signal_bd}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "breakdown_continuation",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    diag_parts = []
    if bb_mid is not None:
        mid_dist = abs(price - bb_mid)
        diag_parts.append(f"BB_mid={mid_dist:.0f}pts({'OK' if mid_dist <= 150 else 'FAR'})")
    if rsi_15m is not None:
        diag_parts.append(f"RSI={rsi_15m:.1f}({'OK' if 30 <= rsi_15m <= RSI_ENTRY_HIGH_BOUNCE else 'OUT'})")
    prev_close = tf_15m.get("prev_close")
    if prev_close is not None:
        diag_parts.append(f"bounce={'OK' if price > prev_close else 'NO'}")
    daily_str = "bullish" if daily_bullish else ("bearish" if daily_bullish is not None else "N/A")
    diag = " | ".join(diag_parts)
    result["reasoning"] = f"No setup. {diag} | Daily={daily_str}"
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
