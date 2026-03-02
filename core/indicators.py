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
    if any(v > 0 for v in volumes):
        recent_vols = [v for v in volumes[-20:] if v > 0]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else None
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
        # Market structure context — fed directly to Sonnet/Opus
        "volume_signal": tf_15m.get("volume_signal"),
        "volume_ratio": tf_15m.get("volume_ratio"),
        "swing_high_20": tf_15m.get("swing_high_20"),
        "swing_low_20": tf_15m.get("swing_low_20"),
        "dist_to_swing_high": tf_15m.get("dist_to_swing_high"),
        "dist_to_swing_low": tf_15m.get("dist_to_swing_low"),
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
        rsi_ok_long = 35 <= rsi_15m <= RSI_ENTRY_HIGH_BOUNCE
        above_ema50 = tf_15m.get("above_ema50")
        prev_close = tf_15m.get("prev_close")
        bounce_starting = prev_close is not None and price > prev_close

        if near_mid_pts and rsi_ok_long and bounce_starting:
            entry = price
            sl = entry - DEFAULT_SL_DISTANCE
            if ema50_15m:
                sl = max(sl, ema50_15m - 20)
            tp = entry + DEFAULT_TP_DISTANCE

            ema50_note = "above EMA50" if above_ema50 else "below EMA50 (AI to evaluate)"
            result.update({
                "found": True,
                "type": "bollinger_mid_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"LONG: BB mid bounce on 15M. "
                    f"Price {abs(price - bb_mid):.0f}pts from mid ({bb_mid:.0f}). "
                    f"RSI {rsi_15m:.1f} in zone. {ema50_note}. {daily_str}."
                ),
            })
            return result

    # --- LONG Setup 2: Bollinger Lower Band Bounce ---
    # Deeply oversold — strongest mean-reversion signal.
    # No above_ema50 gate: price may be below EMA50 at the lower band (expected).
    if bb_lower and rsi_15m:
        near_lower_pts = abs(price - bb_lower) <= 80
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
            result.update({
                "found": True,
                "type": "bollinger_lower_bounce",
                "direction": "LONG",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"LONG: BB lower band bounce on 15M. "
                    f"Price {abs(price - bb_lower):.0f}pts from lower ({bb_lower:.0f}). "
                    f"RSI {rsi_15m:.1f} deeply oversold. "
                    f"Lower wick {lower_wick_l:.0f}pts rejection. "
                    f"{daily_str}.{macro_note}"
                ),
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

            result.update({
                "found": True,
                "type": "bollinger_upper_rejection",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"SHORT: BB upper rejection on 15M. "
                    f"Price {abs(price - bb_upper):.0f}pts from upper ({bb_upper:.0f}). "
                    f"RSI {rsi_15m:.1f} in zone. Below EMA50. {short_daily_str}."
                ),
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

            result.update({
                "found": True,
                "type": "ema50_rejection",
                "direction": "SHORT",
                "entry": round(entry, 1),
                "sl": round(sl, 1),
                "tp": round(tp, 1),
                "reasoning": (
                    f"SHORT: EMA50 rejection on 15M. Price {dist_ema50:.0f}pts from EMA50 ({ema50_15m:.0f}), "
                    f"testing from below. RSI {rsi_15m:.1f}. {short_daily_str}."
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
