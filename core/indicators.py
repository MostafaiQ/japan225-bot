"""
Technical indicator calculations for Japan 225 Trading Bot.
Pure math - no API calls, no side effects. Fully testable.

Indicators: Bollinger Bands, EMA 50/200, RSI 14, VWAP
Input: lists/arrays of OHLCV data
Output: dicts with calculated values
"""
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional
from config.settings import (
    RSI_ENTRY_HIGH_BOUNCE, ENABLE_EMA50_BOUNCE_SETUP,
    DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE,
    MOMENTUM_RSI_LOW, MOMENTUM_RSI_HIGH,
    BREAKOUT_RSI_LOW, BREAKOUT_RSI_HIGH, BREAKOUT_VOL_RATIO_MIN,
    VWAP_BOUNCE_RSI_LOW, VWAP_BOUNCE_RSI_HIGH,
    EMA9_PULLBACK_RSI_LOW, EMA9_PULLBACK_RSI_HIGH,
    BB_UPPER_PROXIMITY_PTS, SWING_HIGH_PROXIMITY_PTS,
    VWAP_PROXIMITY_PTS, EMA9_PROXIMITY_PTS,
    MOMENTUM_HA_STREAK_MIN, DISABLED_SETUP_TYPES,
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

    # Most recent swing pivot high/low — the last real price reversal point,
    # regardless of how many candles ago. A pivot high = candle whose high is
    # higher than the 3 candles on each side. Same for pivot low.
    # This is what humans actually see as "the last high" or "the last low" on a chart.
    _N = 3  # neighbours on each side required to confirm a pivot
    recent_pivot_high = None
    recent_pivot_high_age = None
    recent_pivot_low = None
    recent_pivot_low_age = None
    if len(highs) > _N * 2 + 1:
        for i in range(len(highs) - _N - 1, _N - 1, -1):
            if recent_pivot_high is None and highs[i] == max(highs[i - _N: i + _N + 1]):
                recent_pivot_high = highs[i]
                recent_pivot_high_age = len(highs) - 1 - i  # candles ago
            if recent_pivot_low is None and lows[i] == min(lows[i - _N: i + _N + 1]):
                recent_pivot_low = lows[i]
                recent_pivot_low_age = len(lows) - 1 - i
            if recent_pivot_high is not None and recent_pivot_low is not None:
                break
    if recent_pivot_high is not None:
        result["pivot_high"] = round(recent_pivot_high, 1)
        result["pivot_high_age"] = recent_pivot_high_age
        result["dist_to_pivot_high"] = round(recent_pivot_high - current_price, 1)
    if recent_pivot_low is not None:
        result["pivot_low"] = round(recent_pivot_low, 1)
        result["pivot_low_age"] = recent_pivot_low_age
        result["dist_to_pivot_low"] = round(current_price - recent_pivot_low, 1)

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
    # Plural list form used by detect_setup() (candlestick_patterns)
    if cp["pattern_name"] and cp["pattern_direction"] != "neutral":
        result["candlestick_patterns"] = [{"direction": cp["pattern_direction"], "name": cp["pattern_name"], "strength": cp["pattern_strength"]}]
    else:
        result["candlestick_patterns"] = []

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

    # ATR(14) — true volatility per candle, used by AI to set appropriate SL/TP width
    result["atr"] = round(compute_atr(candles, period=14), 1)

    # ── Anchored VWAPs (requires timestamp field in candles) ─────────────────
    try:
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        days_since_mon = now_utc.weekday()
        week_start = (now_utc - timedelta(days=days_since_mon)).strftime("%Y-%m-%d")
        result["anchored_vwap_daily"]  = anchored_vwap(candles, today_str)
        result["anchored_vwap_weekly"] = anchored_vwap(candles, week_start)
        # Use daily-anchored VWAP as primary if available (more accurate than cumulative multi-day)
        if result["anchored_vwap_daily"] is not None:
            result["vwap"] = result["anchored_vwap_daily"]
            result["above_vwap"] = current_price > result["vwap"]
    except Exception:
        result["anchored_vwap_daily"]  = None
        result["anchored_vwap_weekly"] = None

    # ── Volume Profile (POC / VAH / VAL) ──────────────────────────────────────
    if any(c.get("volume", 0) > 0 for c in candles):
        vp = compute_volume_profile(candles, lookback=50, bucket_size=25)
        result["volume_poc"] = vp["poc"]
        result["volume_vah"] = vp["vah"]
        result["volume_val"] = vp["val"]
    else:
        result["volume_poc"] = None
        result["volume_vah"] = None
        result["volume_val"] = None

    # ── Equal Highs/Lows Liquidity Zones ──────────────────────────────────────
    eq = detect_equal_levels(candles, lookback=30, tolerance=20.0)
    result["equal_highs_zones"] = eq["equal_highs_zones"]
    result["equal_lows_zones"]  = eq["equal_lows_zones"]

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


def anchored_vwap(candles: list[dict], anchor_isodate: str) -> float | None:
    """
    VWAP anchored to a specific date (YYYY-MM-DD format).
    Returns VWAP computed only from candles on/after that date.
    Requires candles to have 'timestamp' and 'volume' fields.
    """
    try:
        filtered = [c for c in candles if str(c.get("timestamp", ""))[:10] >= anchor_isodate and c.get("volume", 0) > 0]
        if not filtered:
            return None
        total_vol = sum(c["volume"] for c in filtered)
        if total_vol <= 0:
            return None
        tpv = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in filtered)
        return round(tpv / total_vol, 1)
    except Exception:
        return None


def compute_volume_profile(candles: list[dict], lookback: int = 50, bucket_size: int = 25) -> dict:
    """
    Approximate volume profile: distribute each candle's volume uniformly across its price range.
    Returns POC (highest-volume price), VAH (value area high), VAL (value area low).
    Uses last `lookback` candles. bucket_size in points (25 works well for Japan 225).
    """
    recent = candles[-lookback:] if len(candles) > lookback else candles
    vol_by_price: dict[int, float] = {}

    for c in recent:
        vol = c.get("volume", 0)
        if vol <= 0:
            continue
        price_range = c["high"] - c["low"]
        if price_range < bucket_size:
            # Narrow candle: put all volume at midpoint bucket
            bucket = round(((c["high"] + c["low"]) / 2) / bucket_size) * bucket_size
            vol_by_price[bucket] = vol_by_price.get(bucket, 0) + vol
            continue
        n_buckets = max(1, round(price_range / bucket_size))
        vol_per_bucket = vol / n_buckets
        for i in range(n_buckets):
            bucket = round((c["low"] + i * bucket_size) / bucket_size) * bucket_size
            vol_by_price[bucket] = vol_by_price.get(bucket, 0) + vol_per_bucket

    if not vol_by_price:
        return {"poc": None, "vah": None, "val": None}

    sorted_prices = sorted(vol_by_price.keys())
    poc = max(vol_by_price, key=lambda p: vol_by_price[p])
    poc_idx = sorted_prices.index(poc)

    # Value area: expand from POC until 70% of total volume is covered
    total_vol = sum(vol_by_price.values())
    target = total_vol * 0.70
    accumulated = vol_by_price[poc]
    lo_idx = hi_idx = poc_idx

    while accumulated < target and (lo_idx > 0 or hi_idx < len(sorted_prices) - 1):
        lo_add = vol_by_price[sorted_prices[lo_idx - 1]] if lo_idx > 0 else 0.0
        hi_add = vol_by_price[sorted_prices[hi_idx + 1]] if hi_idx < len(sorted_prices) - 1 else 0.0
        if lo_add >= hi_add and lo_idx > 0:
            lo_idx -= 1
            accumulated += lo_add
        elif hi_idx < len(sorted_prices) - 1:
            hi_idx += 1
            accumulated += hi_add
        else:
            lo_idx -= 1
            accumulated += lo_add

    return {
        "poc": sorted_prices[poc_idx],
        "vah": sorted_prices[hi_idx],
        "val": sorted_prices[lo_idx],
    }


def detect_equal_levels(candles: list[dict], lookback: int = 30, tolerance: float = 20.0) -> dict:
    """
    Detect equal highs and equal lows liquidity pools.
    Equal levels = price clusters within `tolerance` points across `lookback` candles.
    Returns lists of zone prices where 2+ candles touched the same level.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    all_highs = [c["high"] for c in recent]
    all_lows  = [c["low"]  for c in recent]

    def find_zones(values: list[float], tol: float) -> list[float]:
        used = [False] * len(values)
        zones = []
        for i in range(len(values)):
            if used[i]:
                continue
            cluster = [values[i]]
            for j in range(i + 1, len(values)):
                if not used[j] and abs(values[j] - values[i]) <= tol:
                    cluster.append(values[j])
                    used[j] = True
            if len(cluster) >= 2:
                zones.append(round(sum(cluster) / len(cluster), 1))
        return zones

    return {
        "equal_highs_zones": find_zones(all_highs, tolerance),
        "equal_lows_zones":  find_zones(all_lows,  tolerance),
    }


def compute_session_context(candles_15m: list[dict], candles_daily: list[dict] | None = None) -> dict:
    """
    Compute session-level price context from 15M and daily candles.
    Returns: session_open, asia_high, asia_low, prev_session_high, prev_session_low,
             pdh (prev day high), pdl (prev day low), prev_week_high, prev_week_low, gap_pts.
    Candle timestamps must be parseable ISO strings (e.g. '2026-03-05 08:00:00').
    """
    result: dict = {
        "session_open": None,
        "asia_high": None,
        "asia_low": None,
        "pdh": None,
        "pdl": None,
        "prev_week_high": None,
        "prev_week_low": None,
        "gap_pts": None,
    }

    def parse_ts(c: dict):
        try:
            return datetime.fromisoformat(str(c.get("timestamp", "")).replace(" ", "T"))
        except Exception:
            return None

    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    current_hour = now_utc.hour

    # ── Session open from 15M candles ─────────────────────────────────────────
    if candles_15m:
        if 0 <= current_hour < 6:
            session_start_h, session_end_h = 0, 6
        elif 8 <= current_hour < 16:
            session_start_h, session_end_h = 8, 16
        elif 16 <= current_hour < 21:
            session_start_h, session_end_h = 16, 21
        else:
            session_start_h, session_end_h = None, None

        if session_start_h is not None:
            sess_candles = [
                c for c in candles_15m
                if (ts := parse_ts(c)) and ts.date() == today
                and session_start_h <= ts.hour < session_end_h
            ]
            if sess_candles:
                result["session_open"] = sess_candles[0]["open"]

        # ── Asia range (Tokyo session: 00:00–05:59 UTC) ───────────────────────
        asia = [
            c for c in candles_15m
            if (ts := parse_ts(c)) and ts.date() == today and 0 <= ts.hour < 6
        ]
        if asia:
            result["asia_high"] = max(c["high"] for c in asia)
            result["asia_low"]  = min(c["low"]  for c in asia)

    # ── PDH/PDL + prev week + gap from daily candles ─────────────────────────
    if candles_daily and len(candles_daily) >= 2:
        result["pdh"] = candles_daily[-2]["high"]
        result["pdl"] = candles_daily[-2]["low"]
        result["gap_pts"] = round(candles_daily[-1]["open"] - candles_daily[-2]["close"], 1)

        # Prev week: find candles from last calendar week (Mon–Sun)
        days_since_mon = now_utc.weekday()  # Mon=0
        this_monday_dt = (now_utc - timedelta(days=days_since_mon)).replace(hour=0, minute=0, second=0, microsecond=0)
        last_monday_dt = this_monday_dt - timedelta(weeks=1)
        prev_week = [
            c for c in candles_daily
            if (ts := parse_ts(c))
            and last_monday_dt.date() <= ts.date() < this_monday_dt.date()
        ]
        if prev_week:
            result["prev_week_high"] = max(c["high"] for c in prev_week)
            result["prev_week_low"]  = min(c["low"]  for c in prev_week)

    return result


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

    # Fibonacci: near a key fib level (support/resistance context)
    fib_near = tf_15m.get("fib_near")
    fibonacci = tf_15m.get("fibonacci", {})
    price = tf_15m.get("price")
    if fib_near:
        conf.append(f"Fib {fib_near}")
    elif fibonacci and price:
        # Check for fib levels acting as S/R (within 100pts)
        for fib_name in ("fib_618", "fib_500", "fib_382", "fib_236", "fib_786"):
            fib_val = fibonacci.get(fib_name)
            if fib_val is None:
                continue
            dist = price - fib_val
            if abs(dist) <= 100:
                pct_label = fib_name.split("_")[1]
                if is_long and dist >= 0:
                    # Price just above fib level = support held
                    conf.append(f"Fib {pct_label} support ({fib_val:.0f}, +{dist:.0f}pts)")
                elif not is_long and dist <= 0:
                    # Price just below fib level = resistance holding
                    conf.append(f"Fib {pct_label} resistance ({fib_val:.0f}, {dist:.0f}pts)")
                elif is_long and dist < 0:
                    # Price below fib = resistance overhead
                    counter.append(f"Fib {pct_label} overhead ({fib_val:.0f}, {dist:.0f}pts)")
                elif not is_long and dist > 0:
                    # Price above fib = support below
                    counter.append(f"Fib {pct_label} support below ({fib_val:.0f}, +{dist:.0f}pts)")
                break  # Only report nearest fib level

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
    exclude_direction: Optional[str] = None,
) -> dict:
    """
    Detect trading setup across multiple timeframes. Bidirectional (LONG + SHORT).

    Args:
        exclude_direction: "LONG" or "SHORT" to skip that direction's setups.
            Used for bidirectional retry after AI rejects the first direction.

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
        "fibonacci": tf_15m.get("fibonacci", {}),
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
        "candlestick_patterns": tf_15m.get("candlestick_patterns", []),
        "body_trend": tf_15m.get("body_trend"),
        "consecutive_direction": tf_15m.get("consecutive_direction"),
        "avg_body_size": tf_15m.get("avg_body_size"),
        "wick_ratio": tf_15m.get("wick_ratio"),
        # Trade quality context
        "pullback_depth": tf_15m.get("pullback_depth"),
        "avg_candle_range": tf_15m.get("avg_candle_range"),
        "bb_width": tf_15m.get("bb_width"),
        # Momentum setup context
        "ema9_15m": tf_15m.get("ema9"),
        "above_ema9": tf_15m.get("above_ema9"),
        "above_ema200": tf_15m.get("above_ema200"),
        # New market structure fields
        "anchored_vwap_daily":  tf_15m.get("anchored_vwap_daily"),
        "anchored_vwap_weekly": tf_15m.get("anchored_vwap_weekly"),
        "volume_poc":           tf_15m.get("volume_poc"),
        "volume_vah":           tf_15m.get("volume_vah"),
        "volume_val":           tf_15m.get("volume_val"),
        "equal_highs_zones":    tf_15m.get("equal_highs_zones", []),
        "equal_lows_zones":     tf_15m.get("equal_lows_zones", []),
        # Daily structure (from tf_daily)
        "pdh_daily":            tf_daily.get("prev_candle_high"),
        "pdl_daily":            tf_daily.get("prev_candle_low"),
        "prev_week_high":       None,
        "prev_week_low":        None,
        # PDH/PDL sweep detection (from tf_15m sweep analysis against daily levels)
        "pdh_swept": (
            tf_15m.get("swept_high") and tf_daily.get("prev_candle_high") is not None
            and abs(tf_15m.get("high", 0) - tf_daily.get("prev_candle_high", 0)) < 100
        ),
        "pdl_swept": (
            tf_15m.get("swept_low") and tf_daily.get("prev_candle_low") is not None
            and abs(tf_15m.get("low", 0) - tf_daily.get("prev_candle_low", 0)) < 100
        ),
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
    # Strong bearish momentum filter — skip LONG bounce setups during freefall.
    # When HA streak <= -2 AND price making new lows, bounces are likely fakeouts.
    # Let SHORT setups handle it instead.
    # ============================================================
    _ha_streak_filter = tf_15m.get("ha_streak")
    _prev_close_filter = tf_15m.get("prev_close")
    # Bearish momentum filter: skip LONG bounce setups during freefall (HA streak ≤-2 + price falling).
    # EXCEPTION: when RSI < 35 (deeply oversold), bounces are valid mean-reversion — let them through.
    # AI still makes the final call on whether the bounce has enough confirmation.
    _strong_bearish_momentum = (
        _ha_streak_filter is not None and _ha_streak_filter <= -2
        and _prev_close_filter is not None and price < _prev_close_filter
        and (rsi_15m is None or rsi_15m >= 35)  # bypass when deeply oversold
    )

    # ============================================================
    # LONG SETUPS (bidirectional — no daily gate, C1 in confidence penalizes counter-trend)
    # Skipped when strong bearish momentum is detected — defers to SHORT setups.
    # ============================================================
    _skip_long = (exclude_direction == "LONG")
    _skip_short = (exclude_direction == "SHORT")

    # --- LONG Setup 1: Bollinger Mid Bounce ---
    if not _skip_long and bb_mid and rsi_15m:
        near_mid_pts = abs(price - bb_mid) <= 80  # tightened from 150: higher WR, fewer marginal entries
        rsi_ok_long = 30 <= rsi_15m <= RSI_ENTRY_HIGH_BOUNCE  # widened from 35 to 30 (captures RSI 30-35 near BB mid)
        above_ema50 = tf_15m.get("above_ema50")
        prev_close = tf_15m.get("prev_close")
        swept_low_bm = tf_15m.get("swept_low", False)  # bullish liquidity sweep: dipped below level, closed back above
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
        # Liquidity sweep counts as strongest bounce confirmation (price swept below BB mid and closed back above)
        bounce_confirmed = bounce_starting or swept_low_bm

        if near_mid_pts and rsi_ok_long and bounce_confirmed and not _strong_bearish_momentum:
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
    # Entry requires that the candle wick actually PENETRATED the BB lower band and closed back above.
    # This is the institutional sweep: push below BB lower to trigger stops, then reverse.
    # The old "within 150pts + 15pt wick" fired on random candles nowhere near the band — too loose.
    if not _skip_long and bb_lower and rsi_15m:
        near_lower_pts = abs(price - bb_lower) <= 80  # tightened: must be genuinely near the band
        rsi_ok_lower = 20 <= rsi_15m <= 40
        candle_open_l = tf_15m.get("open")
        candle_low_l  = tf_15m.get("low")
        swept_low_bl = tf_15m.get("swept_low", False)
        # BB lower sweep: wick actually touched or pierced the band (within 10pts), close back above
        # This is the post-hunt entry — the sweep below the band already happened this candle
        bb_lower_wick_test = candle_low_l is not None and candle_low_l <= bb_lower + 10
        rejection_l = bb_lower_wick_test or swept_low_bl  # band wick OR broader swing-low sweep

        if near_lower_pts and rsi_ok_lower and rejection_l and not _strong_bearish_momentum:
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
                f"{'Swing-low sweep' if swept_low_bl else 'BB lower band wick'} rejection. "
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
    if not _skip_long and ENABLE_EMA50_BOUNCE_SETUP and ema50_15m and rsi_15m:
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
    if not _skip_long and rsi_15m and rsi_15m < 30 and daily_bullish and not _strong_bearish_momentum:
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

    # --- LONG Setup 5: Extreme Oversold Reversal (bear market snap-back) ---
    # RSI < 28 — extreme oversold, even in a bear market a snap-back is likely.
    # No daily trend requirement. Bypasses _strong_bearish_momentum filter intentionally.
    # 4H condition: near BB lower (300pts) OR 4H RSI < 35 OR 4H unavailable (pre-screen, tighter RSI < 25).
    # Reversal confirmation: wick ≥10 OR HA bullish OR bullish candle pattern OR liquidity sweep.
    if not _skip_long and rsi_15m and rsi_15m < 28:
        # 4H oversold confirmation: near 4H BB lower OR 4H RSI < 35
        bb_lower_4h = tf_4h.get("bollinger_lower")
        price_4h = tf_4h.get("price")
        near_4h_bb_lower = (
            bb_lower_4h is not None and price_4h is not None
            and abs(price_4h - bb_lower_4h) <= 300
        )
        rsi_4h_extreme = rsi_4h is not None and rsi_4h < 35
        # When 4H unavailable (pre-screen), allow with tighter RSI < 25 — AI will have 4H later
        _4h_not_available = not tf_4h or rsi_4h is None
        _4h_oversold_ok = near_4h_bb_lower or rsi_4h_extreme or (_4h_not_available and rsi_15m < 25)

        if _4h_oversold_ok:
            candle_open_ext = tf_15m.get("open")
            candle_low_ext  = tf_15m.get("low")
            lower_wick_ext = (min(candle_open_ext, price) - candle_low_ext) if (candle_open_ext is not None and candle_low_ext is not None) else 0
            ha_bull_ext = tf_15m.get("ha_bullish")
            swept_low_ext = tf_15m.get("swept_low", False)
            candle_patterns_ext = tf_15m.get("candlestick_patterns", [])
            bullish_pattern_ext = any(p.get("direction") == "bullish" for p in candle_patterns_ext) if candle_patterns_ext else False
            reversal_confirm_ext = lower_wick_ext >= 10 or ha_bull_ext or swept_low_ext or bullish_pattern_ext

            if reversal_confirm_ext:
                entry = price
                sl = entry - DEFAULT_SL_DISTANCE
                tp = entry + DEFAULT_TP_DISTANCE
                _4h_note_parts = []
                if rsi_4h_extreme:
                    _4h_note_parts.append(f"4H RSI {rsi_4h:.1f}")
                if near_4h_bb_lower:
                    _4h_note_parts.append(f"4H near BB lower ({bb_lower_4h:.0f})")
                rsi_4h_note = f" {' + '.join(_4h_note_parts)} — multi-TF extreme." if _4h_note_parts else ""
                conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
                confirm_ext_str = []
                if lower_wick_ext >= 10:
                    confirm_ext_str.append(f"wick {lower_wick_ext:.0f}pts")
                if ha_bull_ext:
                    confirm_ext_str.append("HA bullish")
                if bullish_pattern_ext:
                    confirm_ext_str.append("bullish candle pattern")
                if swept_low_ext:
                    confirm_ext_str.append("liquidity sweep")
                reasoning = (
                    f"LONG: Extreme oversold reversal on 15M. "
                    f"RSI {rsi_15m:.1f} — extreme freefall snap-back candidate. "
                    f"Reversal: {', '.join(confirm_ext_str)}. "
                    f"{daily_str}.{rsi_4h_note} "
                    f"HIGH-RISK: counter-trend in bear conditions — size down."
                )
                if conf_list:
                    reasoning += f" Confluence: {', '.join(conf_list)}."
                if counter_list:
                    reasoning += f" Caution: {', '.join(counter_list)}."
                result.update({
                    "found": True,
                    "type": "extreme_oversold_reversal",
                    "direction": "LONG",
                    "entry": round(entry, 1),
                    "sl": round(sl, 1),
                    "tp": round(tp, 1),
                    "reasoning": reasoning,
                })
                return result

    # ============================================================
    # MOMENTUM / TREND-FOLLOWING LONG SETUPS
    # These fire when the market is trending strongly upward and mean-reversion
    # setups (above) don't apply because price is NOT near BB lower/mid.
    # Ordered: most specific (breakout) → most general (momentum continuation).
    # ============================================================
    ema9_15m = tf_15m.get("ema9")
    above_ema9 = tf_15m.get("above_ema9")
    above_ema50 = tf_15m.get("above_ema50")
    above_ema200_15m = tf_15m.get("above_ema200")
    above_vwap = tf_15m.get("above_vwap")
    vwap_15m = tf_15m.get("vwap")
    ha_bullish = tf_15m.get("ha_bullish")
    ha_streak = tf_15m.get("ha_streak")
    vol_signal = tf_15m.get("volume_signal", "NORMAL")
    vol_ratio = tf_15m.get("volume_ratio", 1.0) or 1.0
    above_ema50_4h = tf_4h.get("above_ema50")

    # --- LONG Setup 6: Breakout Long ---
    # Price near/above BB upper or swing high with volume conviction.
    # Catches breakout moves with institutional participation.
    if not _skip_long and bb_upper and rsi_15m and ema50_15m and above_ema50:
        near_bb_upper = abs(price - bb_upper) <= BB_UPPER_PROXIMITY_PTS
        swing_high = tf_15m.get("swing_high_20")
        near_swing_high = swing_high is not None and abs(price - swing_high) <= SWING_HIGH_PROXIMITY_PTS
        rsi_ok_breakout = BREAKOUT_RSI_LOW <= rsi_15m <= BREAKOUT_RSI_HIGH
        vol_ok_breakout = vol_ratio >= BREAKOUT_VOL_RATIO_MIN
        ha_ok_breakout = ha_bullish is True

        if (near_bb_upper or near_swing_high) and rsi_ok_breakout and vol_ok_breakout and ha_ok_breakout:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            level_str = []
            if near_bb_upper:
                level_str.append(f"BB upper ({bb_upper:.0f}, {abs(price - bb_upper):.0f}pts)")
            if near_swing_high:
                level_str.append(f"swing high ({swing_high:.0f}, {abs(price - swing_high):.0f}pts)")
            reasoning = (
                f"LONG: Breakout on 15M. "
                f"Near {', '.join(level_str)}. "
                f"RSI {rsi_15m:.1f}, vol {vol_ratio:.1f}x, HA bullish. "
                f"Above EMA50. {daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "breakout_long",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- LONG Setup 7: VWAP Bounce Long ---
    # Price pulled back to VWAP in an uptrend and bouncing — intraday fair value re-entry.
    if "vwap_bounce_long" not in DISABLED_SETUP_TYPES and not _skip_long and vwap_15m and rsi_15m and ema50_15m and above_ema50:
        near_vwap = abs(price - vwap_15m) <= VWAP_PROXIMITY_PTS
        rsi_ok_vwap = VWAP_BOUNCE_RSI_LOW <= rsi_15m <= VWAP_BOUNCE_RSI_HIGH
        # Bounce confirmation: HA bullish/turning, or candle pattern, or lower wick, or liquidity sweep
        swept_low_vb = tf_15m.get("swept_low", False)  # price dipped through VWAP and closed back above
        candle_open_vb = tf_15m.get("open")
        candle_low_vb = tf_15m.get("low")
        lower_wick_vb = (min(candle_open_vb, price) - candle_low_vb) if (candle_open_vb is not None and candle_low_vb is not None) else 0
        candle_patterns_vb = tf_15m.get("candlestick_patterns", [])
        bullish_pattern_vb = any(p.get("direction") == "bullish" for p in candle_patterns_vb) if candle_patterns_vb else False
        bounce_confirm_vb = ha_bullish is True or lower_wick_vb >= 25 or bullish_pattern_vb or swept_low_vb  # wick tightened 15→25pts

        if near_vwap and rsi_ok_vwap and bounce_confirm_vb:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            confirm_str_vb = []
            if ha_bullish:
                confirm_str_vb.append("HA bullish")
            if lower_wick_vb >= 15:
                confirm_str_vb.append(f"wick {lower_wick_vb:.0f}pts")
            if bullish_pattern_vb:
                confirm_str_vb.append("bullish candle")
            reasoning = (
                f"LONG: VWAP bounce on 15M. "
                f"Price {abs(price - vwap_15m):.0f}pts from VWAP ({vwap_15m:.0f}). "
                f"RSI {rsi_15m:.1f}. Bounce: {', '.join(confirm_str_vb)}. "
                f"Above EMA50. {daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "vwap_bounce_long",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- LONG Setup 8: EMA9 Pullback Long ---
    # Price pulled back to fast EMA9 in a strong uptrend — shallow dip re-entry.
    # Entry requires HA bullish confirmation OR a bullish liquidity sweep (price dipped below EMA9, closed back above).
    # The loose ha_streak >= -1 path is removed — it allowed entries mid-reversal before bounce confirmed.
    if not _skip_long and ema9_15m and rsi_15m and ema50_15m and above_ema50:
        near_ema9 = abs(price - ema9_15m) <= EMA9_PROXIMITY_PTS
        rsi_ok_ema9 = EMA9_PULLBACK_RSI_LOW <= rsi_15m <= EMA9_PULLBACK_RSI_HIGH
        swept_low_e9 = tf_15m.get("swept_low", False)  # price swept below EMA9 and closed back above = post-sweep entry
        ha_ok_ema9 = (ha_bullish is True) or swept_low_e9  # tightened: HA must be bullish OR sweep confirmed

        if near_ema9 and rsi_ok_ema9 and ha_ok_ema9:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            reasoning = (
                f"LONG: EMA9 pullback on 15M. "
                f"Price {abs(price - ema9_15m):.0f}pts from EMA9 ({ema9_15m:.0f}). "
                f"RSI {rsi_15m:.1f}. HA {'bullish' if ha_bullish else f'streak {ha_streak}'}. "
                f"Above EMA50. {daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "ema9_pullback_long",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- LONG Setup 9: Momentum Continuation Long ---
    # Broadest catch-all for trending markets. Above EMA50 + VWAP + HA bullish streak.
    # This is what catches "today's scenario" — RSI 60-70, strongly trending, no pullback.
    if not _skip_long and ema50_15m and rsi_15m and above_ema50:
        rsi_ok_mom = MOMENTUM_RSI_LOW <= rsi_15m <= MOMENTUM_RSI_HIGH
        above_vwap_ok = above_vwap is True
        ha_streak_ok = ha_streak is not None and ha_streak >= MOMENTUM_HA_STREAK_MIN
        # Volume: IG CFD volume unreliable (often LOW during strong moves).
        # Lenient when HA streak confirms strong trend (>=4).
        vol_ok_mom = vol_signal != "LOW" or (ha_streak is not None and ha_streak >= 4)

        if rsi_ok_mom and above_vwap_ok and ha_streak_ok and vol_ok_mom:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            tp = entry + DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "LONG", pivots=pivots)
            reasoning = (
                f"LONG: Momentum continuation on 15M. "
                f"RSI {rsi_15m:.1f}, HA streak +{ha_streak}, above VWAP+EMA50. "
                f"Vol={vol_signal}. {daily_str}. "
                f"Trend-following — RSI 45-70 is healthy, not overbought."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "momentum_continuation_long",
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
    if not _skip_short and bb_upper and bb_mid and rsi_15m:
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
    if "ema50_rejection" not in DISABLED_SETUP_TYPES and not _skip_short and ema50_15m and rsi_15m:
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
    if "bb_mid_rejection" not in DISABLED_SETUP_TYPES and not _skip_short and bb_mid and rsi_15m:
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
    if not _skip_short and rsi_15m and rsi_15m > 70 and daily_bullish is False:
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
    if not _skip_short and bb_mid and rsi_15m and ema50_15m:
        dist_below_mid = price - bb_mid  # negative when below mid
        below_mid_significant = dist_below_mid < -100
        rsi_ok_breakdown = 25 <= rsi_15m <= 45
        below_ema50_bd = not tf_15m.get("above_ema50")
        ha_streak_bd = tf_15m.get("ha_streak")
        ha_bearish_momentum = ha_streak_bd is not None and ha_streak_bd <= -2
        vol_signal_bd = tf_15m.get("volume_signal", "NORMAL")
        vol_ok_bd = vol_signal_bd != "LOW"

        # Require bearish liquidity sweep: price spiked above a swing high (fake move) then closed back below.
        # Without this, the setup fires mid-move and gets caught in short-covering rallies.
        swept_high_bd = tf_15m.get("swept_high", False)

        if below_mid_significant and rsi_ok_breakdown and below_ema50_bd and ha_bearish_momentum and vol_ok_bd and swept_high_bd:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE

            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: Breakdown continuation on 15M. "
                f"Price {abs(dist_below_mid):.0f}pts below BB mid ({bb_mid:.0f}). "
                f"RSI {rsi_15m:.1f}, HA streak {ha_streak_bd}. "
                f"Below EMA50, vol={vol_signal_bd}. Bearish sweep confirmed. {short_daily_str}."
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

    # --- SHORT Setup 6: Dead Cat Bounce Short ---
    # Bear market sell-the-rally: price bounced from oversold back up to resistance
    # (BB mid or EMA9), HA turned bearish again. Classic bear trap continuation.
    # Fires when daily is bearish OR when locally bearish structure is clear
    # (below EMA50 + HA streak <= -2) — catches medium-term bears while daily EMA200 still bullish.
    ema9_15m = tf_15m.get("ema9")
    if not _skip_short and bb_mid and rsi_15m:
        rsi_ok_dcb = 43 <= rsi_15m <= 62
        ha_bearish_dcb = tf_15m.get("ha_bullish") is False
        ha_streak_dcb = tf_15m.get("ha_streak")
        ha_turning_bear = ha_streak_dcb is not None and ha_streak_dcb <= -1
        below_ema50_dcb = not tf_15m.get("above_ema50")
        locally_bearish_dcb = below_ema50_dcb and ha_streak_dcb is not None and ha_streak_dcb <= -2
        near_bb_mid_dcb = abs(price - bb_mid) <= 150
        near_ema9_dcb = ema9_15m is not None and abs(price - ema9_15m) <= 100
        at_resistance = near_bb_mid_dcb or near_ema9_dcb
        cp_dir_dcb = tf_15m.get("candlestick_direction")
        bearish_candle_dcb = cp_dir_dcb == "bearish"
        fvg_bear_dcb = tf_15m.get("fvg_bearish", False)
        rejection_confirm_dcb = ha_bearish_dcb or ha_turning_bear or bearish_candle_dcb or fvg_bear_dcb
        bias_allows_dcb = (daily_bullish is False) or locally_bearish_dcb

        if bias_allows_dcb and rsi_ok_dcb and below_ema50_dcb and at_resistance and rejection_confirm_dcb:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            res_str = f"BB mid ({bb_mid:.0f})" if near_bb_mid_dcb else f"EMA9 ({ema9_15m:.0f})"
            confirm_parts_dcb = []
            if ha_bearish_dcb or ha_turning_bear:
                confirm_parts_dcb.append(f"HA streak {ha_streak_dcb}")
            if bearish_candle_dcb:
                confirm_parts_dcb.append("bearish candle")
            if fvg_bear_dcb:
                confirm_parts_dcb.append("bearish FVG overhead")
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: Dead cat bounce rejection at {res_str}. "
                f"RSI {rsi_15m:.1f} (bounced but failing). Below EMA50. "
                f"Rejection: {', '.join(confirm_parts_dcb)}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "dead_cat_bounce_short",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 7: Bear Flag Breakdown ---
    # Low-volume consolidation after a sharp drop, then momentum resumes down.
    # Price coiling between BB lower and BB mid — the "flag". HA turning negative.
    if not _skip_short and bb_mid and bb_lower and rsi_15m:
        rsi_ok_flag = 28 <= rsi_15m <= 52
        ha_streak_flag = tf_15m.get("ha_streak")
        ha_neg_flag = ha_streak_flag is not None and ha_streak_flag <= -1
        vol_signal_flag = tf_15m.get("volume_signal", "NORMAL")
        vol_low_flag = vol_signal_flag in ("LOW", "NORMAL")  # flag = low/normal volume consolidation
        in_flag_zone = bb_lower <= price <= bb_mid  # between lower and mid band
        below_ema50_flag = not tf_15m.get("above_ema50")
        below_vwap_flag = tf_15m.get("above_vwap") is False
        fvg_bear_flag = tf_15m.get("fvg_bearish", False)
        bearish_overhead = below_ema50_flag or fvg_bear_flag or below_vwap_flag

        if rsi_ok_flag and ha_neg_flag and vol_low_flag and in_flag_zone and bearish_overhead:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            overhead_str = []
            if below_ema50_flag:
                overhead_str.append("below EMA50")
            if fvg_bear_flag:
                overhead_str.append("bearish FVG overhead")
            if below_vwap_flag:
                overhead_str.append("below VWAP")
            reasoning = (
                f"SHORT: Bear flag breakdown on 15M. "
                f"Price in flag zone ({bb_lower:.0f}–{bb_mid:.0f}). "
                f"RSI {rsi_15m:.1f}, HA streak {ha_streak_flag}, vol={vol_signal_flag}. "
                f"{', '.join(overhead_str)}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "bear_flag_breakdown",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 8: VWAP Rejection Short ---
    # In a downtrend, price rallies back to VWAP (intraday fair value) and fails.
    # No daily requirement — VWAP is intraday, useful in any bear session.
    vwap_15m = tf_15m.get("vwap")
    if "vwap_rejection_short" not in DISABLED_SETUP_TYPES and not _skip_short and vwap_15m and rsi_15m and ema50_15m:
        near_vwap_short = abs(price - vwap_15m) <= 120
        rsi_ok_vwap = 43 <= rsi_15m <= 60
        below_ema50_vwap = not tf_15m.get("above_ema50")
        below_vwap_short = tf_15m.get("above_vwap") is False  # currently below VWAP
        # Price must have just tested VWAP from below: prev_close < vwap, current near vwap
        prev_close_vwap = tf_15m.get("prev_close")
        tested_vwap = prev_close_vwap is not None and prev_close_vwap < vwap_15m and near_vwap_short
        ha_bear_vwap = tf_15m.get("ha_bullish") is False
        cp_dir_vwap = tf_15m.get("candlestick_direction")
        bearish_candle_vwap = cp_dir_vwap == "bearish"
        candle_high_vwap = tf_15m.get("high")
        candle_open_vwap = tf_15m.get("open")
        wick_vwap = (candle_high_vwap - max(candle_open_vwap or price, price)) if candle_high_vwap else 0
        rejection_vwap = ha_bear_vwap or bearish_candle_vwap or wick_vwap >= 15

        if near_vwap_short and rsi_ok_vwap and below_ema50_vwap and tested_vwap and rejection_vwap:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            confirm_parts_vwap = []
            if ha_bear_vwap:
                confirm_parts_vwap.append("HA bearish")
            if bearish_candle_vwap:
                confirm_parts_vwap.append("bearish candle")
            if wick_vwap >= 15:
                confirm_parts_vwap.append(f"wick {wick_vwap:.0f}pts")
            reasoning = (
                f"SHORT: VWAP rejection on 15M (VWAP={vwap_15m:.0f}). "
                f"Price tested VWAP from below and rejected. "
                f"RSI {rsi_15m:.1f}. Below EMA50. "
                f"Rejection: {', '.join(confirm_parts_vwap) or 'HA/candle'}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "vwap_rejection_short",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 9: High-Volume Distribution Short ---
    # Institutional selling at resistance: heavy volume + upper band rejection.
    # Price near BB upper OR swept a high, RSI 55-75, bearish candle/large wick.
    if not _skip_short and bb_upper and rsi_15m:
        near_upper_hv = abs(price - bb_upper) <= 200
        rsi_ok_hv = 55 <= rsi_15m <= 75
        vol_ratio_hv = tf_15m.get("volume_ratio", 1.0) or 1.0
        vol_high_hv = vol_ratio_hv >= 1.4 or tf_15m.get("volume_signal") in ("HIGH", "VERY_HIGH")
        swept_high_hv = tf_15m.get("swept_high", False)
        at_supply_hv = near_upper_hv or swept_high_hv
        cp_dir_hv = tf_15m.get("candlestick_direction")
        bearish_candle_hv = cp_dir_hv == "bearish"
        candle_open_hv = tf_15m.get("open")
        candle_high_hv = tf_15m.get("high")
        upper_wick_hv = (candle_high_hv - max(candle_open_hv, price)) if (candle_open_hv and candle_high_hv) else 0
        large_wick_hv = upper_wick_hv >= 20
        ha_bear_hv = tf_15m.get("ha_bullish") is False
        rejection_hv = bearish_candle_hv or large_wick_hv or ha_bear_hv

        if at_supply_hv and rsi_ok_hv and vol_high_hv and rejection_hv:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            confirm_hv = []
            if bearish_candle_hv:
                confirm_hv.append("bearish candle")
            if large_wick_hv:
                confirm_hv.append(f"wick {upper_wick_hv:.0f}pts")
            if ha_bear_hv:
                confirm_hv.append("HA bearish")
            if swept_high_hv:
                confirm_hv.append("liquidity sweep")
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: High-volume distribution at BB upper ({bb_upper:.0f}). "
                f"RSI {rsi_15m:.1f}, vol ratio {vol_ratio_hv:.2f}x. "
                f"Rejection: {', '.join(confirm_hv)}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "high_volume_distribution",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 10: Multi-Timeframe Bearish Alignment ---
    # All timeframes pointing down simultaneously — bear market momentum confirmation.
    # No specific level required: it's about full-stack alignment.
    # Works in pre-screen (rsi_4h=None): uses 4 local factors; rsi_4h is bonus when available.
    if "multi_tf_bearish" not in DISABLED_SETUP_TYPES and not _skip_short and rsi_15m:
        rsi_15m_bear = rsi_15m < 48
        rsi_4h_bear = rsi_4h is not None and rsi_4h < 48
        daily_bear_mta = daily_bullish is False
        below_ema50_mta = not tf_15m.get("above_ema50")
        below_vwap_mta = tf_15m.get("above_vwap") is False
        ha_bear_mta = tf_15m.get("ha_bullish") is False
        ha_streak_mta = tf_15m.get("ha_streak")
        # Score local factors (always available): rsi_15m, daily, ema50, vwap
        local_score = sum([rsi_15m_bear, daily_bear_mta, below_ema50_mta, below_vwap_mta])
        alignment_score = local_score + (1 if rsi_4h_bear else 0)
        # Need at least 3/4 local factors + HA bear; if 4H available, need 4/5
        threshold = 4 if rsi_4h is not None else 3
        strong_alignment = alignment_score >= threshold and ha_bear_mta and rsi_15m_bear

        if strong_alignment:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            align_parts = []
            if rsi_15m_bear:
                align_parts.append(f"15M RSI {rsi_15m:.1f}")
            if rsi_4h_bear:
                align_parts.append(f"4H RSI {rsi_4h:.1f}")
            if daily_bear_mta:
                align_parts.append("daily bearish")
            if below_ema50_mta:
                align_parts.append("below EMA50")
            if below_vwap_mta:
                align_parts.append("below VWAP")
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            total_factors = 5 if rsi_4h is not None else 4
            reasoning = (
                f"SHORT: Multi-TF bearish alignment ({alignment_score}/{total_factors} factors). "
                f"HA streak {ha_streak_mta}. "
                f"Aligned: {', '.join(align_parts)}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "multi_tf_bearish",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 11: EMA200 Rejection (major support → resistance) ---
    # Price rallies up to EMA200 from below and gets rejected — turn of tide signal.
    # Bear market intensity: price was able to touch EMA200 but couldn't break above.
    ema200_15m = tf_15m.get("ema200")
    if not _skip_short and ema200_15m and rsi_15m:
        near_ema200 = abs(price - ema200_15m) <= 200
        rsi_ok_e200 = 50 <= rsi_15m <= 70
        approaching_from_below = price < ema200_15m and tf_15m.get("prev_close", price) <= ema200_15m
        candle_open_e200 = tf_15m.get("open")
        candle_high_e200 = tf_15m.get("high")
        wick_e200 = (candle_high_e200 - max(candle_open_e200, price)) if (candle_open_e200 and candle_high_e200) else 0
        ha_bear_e200 = tf_15m.get("ha_bullish") is False
        fvg_bear_e200 = tf_15m.get("fvg_bearish", False)
        rejection_e200 = wick_e200 >= 15 or ha_bear_e200 or fvg_bear_e200

        if near_ema200 and rsi_ok_e200 and approaching_from_below and rejection_e200 and daily_bullish is False:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            confirm_parts_e200 = []
            if wick_e200 >= 15:
                confirm_parts_e200.append(f"wick {wick_e200:.0f}pts")
            if ha_bear_e200:
                confirm_parts_e200.append("HA bearish")
            if fvg_bear_e200:
                confirm_parts_e200.append("bearish FVG")
            reasoning = (
                f"SHORT: EMA200 rejection on 15M ({ema200_15m:.0f}). "
                f"Price {abs(price - ema200_15m):.0f}pts from EMA200 (approached from below). "
                f"RSI {rsi_15m:.1f}. Rejection: {', '.join(confirm_parts_e200)}. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "ema200_rejection",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 12: Lower Lows Bearish Momentum ---
    # Swing deterioration: new swing_low_20 < previous swing_low.
    # Combined with bearish momentum (HA streak, RSI) → trend confirmation.
    swing_low_20_curr = tf_15m.get("swing_low_20")
    swing_low_20_prev = tf_15m.get("swing_low_20_prev")  # will check if available
    if not _skip_short and swing_low_20_curr and rsi_15m:
        # If no prev available, compare to current low vs 20-bar low trend
        prev_swing_low = tf_15m.get("swing_low_20_prev")
        is_lower_low = prev_swing_low is not None and swing_low_20_curr < prev_swing_low
        if not is_lower_low and len(tf_15m.get("lows", [])) >= 20:
            # Fallback: check if current is at least 50pts below BB mid (deep pullback)
            lows_recent = tf_15m.get("lows", [])[-20:]
            if lows_recent:
                is_lower_low = min(lows_recent[-5:]) < min(lows_recent[-20:-10])

        rsi_ok_ll = 20 <= rsi_15m <= 50
        ha_streak_ll = tf_15m.get("ha_streak")
        ha_bearish_ll = ha_streak_ll is not None and ha_streak_ll <= -2
        below_ema50_ll = not tf_15m.get("above_ema50")
        vol_signal_ll = tf_15m.get("volume_signal", "NORMAL")
        vol_ok_ll = vol_signal_ll != "LOW"

        if is_lower_low and rsi_ok_ll and ha_bearish_ll and below_ema50_ll and vol_ok_ll:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: Lower lows bearish momentum on 15M. "
                f"Swing deterioration: new low {swing_low_20_curr:.0f}. "
                f"RSI {rsi_15m:.1f}, HA streak {ha_streak_ll}, vol={vol_signal_ll}. "
                f"Below EMA50. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "lower_lows_bearish",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Setup 13: Pivot Point Resistance Rejection ---
    # Price tests Pivot Resistance (R1) and gets rejected with bearish confirmation.
    # Daily pivots provide institutional support/resistance levels.
    if not _skip_short and pivots and rsi_15m:
        pivot_r1 = pivots.get("r1")
        if pivot_r1:
            near_r1 = abs(price - pivot_r1) <= 150
            rsi_ok_pr = 55 <= rsi_15m <= 75
            candle_open_pr = tf_15m.get("open")
            candle_high_pr = tf_15m.get("high")
            wick_pr = (candle_high_pr - max(candle_open_pr, price)) if (candle_open_pr and candle_high_pr) else 0
            ha_bear_pr = tf_15m.get("ha_bullish") is False
            cp_dir_pr = tf_15m.get("candlestick_direction")
            bearish_candle_pr = cp_dir_pr == "bearish"
            rejection_pr = wick_pr >= 15 or ha_bear_pr or bearish_candle_pr

            if near_r1 and rsi_ok_pr and rejection_pr and daily_bullish is False:
                entry = price
                sl = entry + DEFAULT_SL_DISTANCE
                tp = entry - DEFAULT_TP_DISTANCE
                conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
                confirm_parts_pr = []
                if wick_pr >= 15:
                    confirm_parts_pr.append(f"wick {wick_pr:.0f}pts")
                if ha_bear_pr:
                    confirm_parts_pr.append("HA bearish")
                if bearish_candle_pr:
                    confirm_parts_pr.append("bearish candle")
                reasoning = (
                    f"SHORT: Pivot R1 rejection on 15M ({pivot_r1:.0f}). "
                    f"Price {abs(price - pivot_r1):.0f}pts from R1 (institutional resistance). "
                    f"RSI {rsi_15m:.1f}. Rejection: {', '.join(confirm_parts_pr)}. {short_daily_str}."
                )
                if conf_list:
                    reasoning += f" Confluence: {', '.join(conf_list)}."
                if counter_list:
                    reasoning += f" Caution: {', '.join(counter_list)}."
                result.update({
                    "found": True,
                    "type": "pivot_r1_rejection",
                    "direction": "SHORT",
                    "entry": round(entry, 1),
                    "sl": round(sl, 1),
                    "tp": round(tp, 1),
                    "reasoning": reasoning,
                })
                return result

    # ============================================================
    # MOMENTUM / TREND-FOLLOWING SHORT SETUPS
    # Mirror of LONG momentum setups. Catch strong downtrend continuation.
    # ============================================================

    # --- SHORT Momentum: EMA9 Pullback Short ---
    # Mirror of ema9_pullback_long. Price bounced to EMA9 in a confirmed downtrend — shallow dead-cat rejection.
    # Entry requires HA bearish OR a bearish sweep (price spiked above EMA9, closed back below).
    # Placed BEFORE momentum_continuation_short so near-EMA9 bounces are captured with tight entry.
    below_ema50_s = not tf_15m.get("above_ema50")
    ha_streak_s = tf_15m.get("ha_streak")
    vol_signal_s = tf_15m.get("volume_signal", "NORMAL")

    if "ema9_pullback_short" not in DISABLED_SETUP_TYPES and not _skip_short and ema9_15m and rsi_15m and ema50_15m and below_ema50_s:
        near_ema9_s = abs(price - ema9_15m) <= EMA9_PROXIMITY_PTS
        ema9_below_ema50_s = ema9_15m < ema50_15m  # EMA9 < EMA50 confirms downtrend alignment
        rsi_ok_e9s = 35 <= rsi_15m <= 60  # bearish momentum: wide enough to catch pullback RSI
        swept_high_e9s = tf_15m.get("swept_high", False)  # price spiked above EMA9, closed back below
        ha_ok_e9s = (ha_bullish is False) or swept_high_e9s  # HA must be bearish OR sweep confirmed

        if near_ema9_s and ema9_below_ema50_s and rsi_ok_e9s and ha_ok_e9s:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: EMA9 pullback on 15M. "
                f"Price {abs(price - ema9_15m):.0f}pts from EMA9 ({ema9_15m:.0f}). "
                f"RSI {rsi_15m:.1f}. {'Bearish sweep rejection' if swept_high_e9s else 'HA bearish'}. "
                f"Below EMA50. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "ema9_pullback_short",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Momentum: Momentum Continuation Short ---
    # Broadest catch-all for trending SHORT markets. Below EMA50 + below VWAP + HA bearish streak.
    below_ema50_s = not tf_15m.get("above_ema50")
    below_vwap_s = tf_15m.get("above_vwap") is False
    ha_bearish_s = tf_15m.get("ha_bullish") is False
    ha_streak_s = tf_15m.get("ha_streak")
    vol_signal_s = tf_15m.get("volume_signal", "NORMAL")

    if "momentum_continuation_short" not in DISABLED_SETUP_TYPES and not _skip_short and ema50_15m and rsi_15m and below_ema50_s:
        rsi_ok_mom_s = 30 <= rsi_15m <= 55
        ha_streak_ok_s = ha_streak_s is not None and ha_streak_s <= -MOMENTUM_HA_STREAK_MIN
        # Volume: IG CFD volume unreliable. Lenient when HA streak confirms strong trend (<=−4).
        vol_ok_mom_s = vol_signal_s != "LOW" or (ha_streak_s is not None and ha_streak_s <= -4)

        if rsi_ok_mom_s and below_vwap_s and ha_streak_ok_s and vol_ok_mom_s:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            reasoning = (
                f"SHORT: Momentum continuation on 15M. "
                f"RSI {rsi_15m:.1f}, HA streak {ha_streak_s}, below VWAP+EMA50. "
                f"Vol={vol_signal_s}. {short_daily_str}. "
                f"Trend-following — RSI 30-55 is healthy bearish, not oversold."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "momentum_continuation_short",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": reasoning,
            })
            return result

    # --- SHORT Momentum: VWAP Rejection Short ---
    # Price rallied to VWAP from below in a downtrend and got rejected.
    if "vwap_rejection_short_momentum" not in DISABLED_SETUP_TYPES and not _skip_short and vwap_15m and rsi_15m and ema50_15m and below_ema50_s:
        near_vwap_s = abs(price - vwap_15m) <= VWAP_PROXIMITY_PTS
        rsi_ok_vwap_s = 35 <= rsi_15m <= 60
        # Rejection confirmation: HA bearish/turning, or bearish candle pattern, or upper wick
        candle_open_vs = tf_15m.get("open")
        candle_high_vs = tf_15m.get("high")
        upper_wick_vs = (candle_high_vs - max(candle_open_vs, price)) if (candle_open_vs is not None and candle_high_vs is not None) else 0
        candle_patterns_vs = tf_15m.get("candlestick_patterns", [])
        bearish_pattern_vs = any(p.get("direction") == "bearish" for p in candle_patterns_vs) if candle_patterns_vs else False
        reject_confirm_vs = ha_bearish_s or upper_wick_vs >= 15 or bearish_pattern_vs

        if near_vwap_s and rsi_ok_vwap_s and reject_confirm_vs:
            entry = price
            sl = entry + DEFAULT_SL_DISTANCE
            tp = entry - DEFAULT_TP_DISTANCE
            conf_list, counter_list = _build_confluence(tf_15m, "SHORT", pivots=pivots)
            confirm_str_vs = []
            if ha_bearish_s:
                confirm_str_vs.append("HA bearish")
            if upper_wick_vs >= 15:
                confirm_str_vs.append(f"wick {upper_wick_vs:.0f}pts")
            if bearish_pattern_vs:
                confirm_str_vs.append("bearish candle")
            reasoning = (
                f"SHORT: VWAP rejection on 15M. "
                f"Price {abs(price - vwap_15m):.0f}pts from VWAP ({vwap_15m:.0f}). "
                f"RSI {rsi_15m:.1f}. Rejection: {', '.join(confirm_str_vs)}. "
                f"Below EMA50. {short_daily_str}."
            )
            if conf_list:
                reasoning += f" Confluence: {', '.join(conf_list)}."
            if counter_list:
                reasoning += f" Caution: {', '.join(counter_list)}."
            result.update({
                "found": True,
                "type": "vwap_rejection_short_momentum",
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
    # Momentum diagnostic — shows why momentum setups didn't fire
    mom_flags = []
    _ae50 = tf_15m.get("above_ema50")
    _avw = tf_15m.get("above_vwap")
    _hs = tf_15m.get("ha_streak")
    _vs = tf_15m.get("volume_signal", "?")
    mom_flags.append(f"EMA50={'above' if _ae50 else 'below'}")
    mom_flags.append(f"VWAP={'above' if _avw else 'below'}")
    mom_flags.append(f"HA={_hs}")
    mom_flags.append(f"vol={_vs}")
    diag = " | ".join(diag_parts)
    result["reasoning"] = f"No setup. {diag} | Daily={daily_str} | Mom: {' '.join(mom_flags)}"
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


def compute_atr(candles: list[dict], period: int = 14) -> float:
    """
    Compute Average True Range over the last `period` candles.

    Uses standard Wilder ATR (simple mean of True Ranges for simplicity).
    Candles must have keys: high, low, close.

    Returns 0.0 if there are fewer than period+1 candles (insufficient data).
    Callers should treat 0.0 as "ATR not established — skip entry".
    """
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i].get("high", 0)
        l = candles[i].get("low", 0)
        pc = candles[i - 1].get("close", 0)
        if h == 0 or l == 0 or pc == 0:
            continue
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period

