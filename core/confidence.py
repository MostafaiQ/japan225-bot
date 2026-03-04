"""
Local confidence scorer — bidirectional 12-criteria system.

Computes a confidence score LOCALLY from indicator data before any AI call.
Acts as a gate: only escalate to Sonnet/Opus if local score >= HAIKU_MIN_SCORE (60%).

Scoring (proportional):
  score = min(30 + int(passed * 70 / total_criteria), 100)
  12/12=100%, 11/12=94%, 10/12=88%, 9/12=82%, 8/12=76%, 7/12=70%,
  6/12=65% (fails 70 gate), 5/12=59% (below 60% gate)
  LONG needs 7/12 (70%≥70), SHORT needs 8/12 (76%≥75).

Criteria (12 total):
  C1  daily_trend       — daily EMA200 agrees with direction (oversold exempt)
  C2  entry_level       — price at BB/EMA technical level
  C3  rsi_15m           — 15M RSI in valid entry zone
  C4  tp_viable         — price below/above BB mid (room for TP)
  C5  structure         — 15M EMA50 or reversal signals (setup-type-aware)
  C6  macro             — 4H RSI in healthy range
  C7  no_event_1hr      — no HIGH-impact event within 60 min
  C8  no_friday_monthend— not Friday blackout / month-end
  C9  volume            — 15M volume not critically low (signal != LOW)
  C10 trend_4h          — 4H EMA50 or oversold reversal (setup-type-aware)
  C11 ha_aligned        — 15M HA or reversal signals (setup-type-aware)
  C12 entry_quality     — pullback depth + volatility regime (data-backed filter)
"""
import logging
from datetime import datetime, timezone
from config.settings import RSI_ENTRY_HIGH_BOUNCE

logger = logging.getLogger(__name__)

BASE_SCORE = 30
MAX_SCORE = 100

MIN_CONFIDENCE_LONG = 70
MIN_CONFIDENCE_SHORT = 75

# RSI ranges — LONG lower gate widened to 30 (captures RSI 30-35 near BB mid)
LONG_RSI_LOW = 30  # widened from 35 to match detect_setup bb_mid_bounce range
LONG_RSI_HIGH = RSI_ENTRY_HIGH_BOUNCE  # imported from settings — single source of truth
SHORT_RSI_LOW, SHORT_RSI_HIGH = 55, 75

# Bollinger nearness threshold (points from midband)
BB_MID_THRESHOLD_PTS = 150  # calibrated for Nikkei ~50k-60k (was 30, only p11 of candles)

# EMA50 nearness for EMA bounce setup (points)
EMA50_THRESHOLD_PTS = 150  # calibrated for Nikkei ~50k-60k (was 30, only p6 of candles)


def compute_confidence(
    direction: str,
    tf_daily: dict,
    tf_4h: dict,
    tf_15m: dict,
    upcoming_events: list = None,
    web_research: dict = None,
    setup_type: str = None,
) -> dict:
    """
    Compute local confidence score for a potential trade.

    Args:
        direction:      'LONG' or 'SHORT'
        tf_daily:       analyze_timeframe() output for daily candles
        tf_4h:          analyze_timeframe() output for 4H candles
        tf_15m:         analyze_timeframe() output for 15M candles
        upcoming_events: list of event dicts from calendar
        web_research:   dict from WebResearcher.research()

    Returns:
        {
            "score": int (0-100),
            "criteria": {criterion: bool},
            "reasons": [str],  # why each criterion passed/failed
            "direction": str,
            "meets_threshold": bool,
        }
    """
    direction = direction.upper()
    criteria = {}
    reasons = {}

    price = tf_15m.get("price", 0)
    rsi_15m = tf_15m.get("rsi")
    bb_mid = tf_15m.get("bollinger_mid")
    bb_upper = tf_15m.get("bollinger_upper")
    bb_lower = tf_15m.get("bollinger_lower")
    ema50_15m = tf_15m.get("ema50")
    ema200_15m = tf_15m.get("ema200")
    above_ema50_15m = tf_15m.get("above_ema50")
    above_ema200_15m = tf_15m.get("above_ema200")

    above_ema200_daily = tf_daily.get("above_ema200")
    above_ema50_daily = tf_daily.get("above_ema50")
    rsi_4h = tf_4h.get("rsi")
    rsi_daily = tf_daily.get("rsi")

    # Setup-type-aware flags — used by C1, C5, C10, C11, C12
    _oversold_setup = setup_type in ("bollinger_lower_bounce", "oversold_reversal", "extreme_oversold_reversal")
    _overbought_setup = setup_type in ("overbought_reversal",)
    _breakdown_setup = setup_type in (
        "breakdown_continuation", "bear_flag_breakdown", "multi_tf_bearish"
    )
    # ---- Criterion 1: Daily Trend Aligned (oversold-exempt) ----
    # Uses EMA50 as PRIMARY (more responsive to recent trend changes).
    # EMA200 as fallback only. On crash days, EMA200 lags by thousands of points
    # and always reads "bullish" — useless for trend detection.
    # Oversold bounces (bb_lower_bounce, oversold_reversal) pass C1 even when daily is bearish.
    if direction == "LONG":
        if above_ema50_daily is not None:
            c1 = bool(above_ema50_daily)
            if not c1 and _oversold_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA50: below (oversold exempt — counter-trend bounce)"
            else:
                reasons["daily_trend"] = f"Daily EMA50: {'above' if c1 else 'below'} (price={price:.0f})"
        elif above_ema200_daily is not None:
            c1 = bool(above_ema200_daily)
            if not c1 and _oversold_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: below (oversold exempt — counter-trend bounce)"
            else:
                reasons["daily_trend"] = f"Daily EMA50 N/A, using EMA200: {'above' if c1 else 'below'}"
        else:
            c1 = False
            reasons["daily_trend"] = "Daily EMA data unavailable"
    else:  # SHORT
        if above_ema50_daily is not None:
            c1 = not bool(above_ema50_daily)
            if not c1 and _overbought_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA50: above (overbought exempt — counter-trend reversal)"
            elif not c1 and _breakdown_setup:
                # Breakdown/momentum shorts: daily bullish is EXPECTED during transition.
                # 4H/15M already turned bearish but daily EMA lags on big selloff days.
                c1 = True
                reasons["daily_trend"] = f"Daily EMA50: above (breakdown exempt — daily lags in transition)"
            else:
                reasons["daily_trend"] = f"Daily EMA50: {'below (bearish)' if c1 else 'above (not bearish)'}"
        elif above_ema200_daily is not None:
            c1 = not bool(above_ema200_daily)
            if not c1 and _overbought_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: above (overbought exempt — counter-trend reversal)"
            elif not c1 and _breakdown_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: above (breakdown exempt — daily lags in transition)"
            else:
                reasons["daily_trend"] = f"Daily EMA50 N/A, using EMA200: {'below (bearish)' if c1 else 'above (not bearish)'}"
        else:
            c1 = False
            reasons["daily_trend"] = "Daily EMA data unavailable"
    criteria["daily_trend"] = c1

    # ---- Criterion 2: Entry at Technical Level ----
    vwap_15m = tf_15m.get("vwap")
    above_vwap_15m = tf_15m.get("above_vwap")
    if direction == "LONG":
        # Near BB midband OR near EMA50 OR near BB lower band (deeply oversold bounce)
        near_bb_mid = (
            bb_mid is not None and abs(price - bb_mid) <= BB_MID_THRESHOLD_PTS
        )
        near_ema50 = (
            ema50_15m is not None and abs(price - ema50_15m) <= EMA50_THRESHOLD_PTS
        )
        near_bb_lower = (
            bb_lower is not None and abs(price - bb_lower) <= 150  # widened to match detect_setup
        )
        # VWAP fallback: price below VWAP within 150pts (discount zone)
        near_vwap_long = (
            vwap_15m is not None
            and above_vwap_15m is False
            and abs(price - vwap_15m) <= BB_MID_THRESHOLD_PTS
        )
        c2 = near_bb_mid or near_ema50 or near_bb_lower or near_vwap_long
        if near_bb_lower:
            reasons["entry_level"] = f"Price {abs(price - bb_lower):.0f}pts from BB lower ({bb_lower:.0f})"
        elif near_bb_mid:
            reasons["entry_level"] = f"Price {abs(price - bb_mid):.0f}pts from BB mid ({bb_mid:.0f})"
        elif near_ema50:
            reasons["entry_level"] = f"Price {abs(price - ema50_15m):.0f}pts from EMA50 ({ema50_15m:.0f})"
        elif near_vwap_long:
            reasons["entry_level"] = f"Price {abs(price - vwap_15m):.0f}pts below VWAP ({vwap_15m:.0f}, discount)"
        else:
            reasons["entry_level"] = (
                f"Not at tech level. BB mid dist: {abs(price - bb_mid):.0f}pts, "
                f"EMA50 dist: {abs(price - ema50_15m):.0f}pts" if bb_mid and ema50_15m
                else "BB/EMA50 data unavailable"
            )
    else:  # SHORT
        # Near Bollinger upper band OR near EMA50 from below (rejected at EMA50)
        near_bb_upper = (
            bb_upper is not None and abs(price - bb_upper) <= BB_MID_THRESHOLD_PTS
        )
        near_bb_mid = (
            bb_mid is not None and abs(price - bb_mid) <= BB_MID_THRESHOLD_PTS
        )
        # For short EMA50 bounce: price is at or just below EMA50
        near_ema50_short = (
            ema50_15m is not None
            and abs(price - ema50_15m) <= EMA50_THRESHOLD_PTS
            and price <= ema50_15m  # price came up to EMA50 from below = rejection
        )
        # VWAP fallback: price above VWAP within 150pts (premium zone)
        near_vwap_short = (
            vwap_15m is not None
            and above_vwap_15m is True
            and abs(price - vwap_15m) <= BB_MID_THRESHOLD_PTS
        )
        c2 = near_bb_upper or near_bb_mid or near_ema50_short or near_vwap_short
        if near_bb_upper:
            reasons["entry_level"] = f"Price {abs(price - bb_upper):.0f}pts from BB upper ({bb_upper:.0f})"
        elif near_ema50_short:
            reasons["entry_level"] = f"Price {abs(price - ema50_15m):.0f}pts from EMA50 (rejection)"
        elif near_bb_mid:
            reasons["entry_level"] = f"Price {abs(price - bb_mid):.0f}pts from BB mid"
        elif near_vwap_short:
            reasons["entry_level"] = f"Price {abs(price - vwap_15m):.0f}pts above VWAP ({vwap_15m:.0f}, premium)"
        else:
            reasons["entry_level"] = "Not at technical level for short"
    criteria["entry_level"] = c2

    # ---- Criterion 3: RSI 15M in Zone ----
    # Setup-aware: BB lower bounce uses deeply oversold zone (20-40), not the standard 35-48
    if rsi_15m is not None:
        if direction == "LONG":
            at_bb_lower = bb_lower is not None and abs(price - bb_lower) <= 150
            if at_bb_lower:
                c3 = 20 <= rsi_15m <= 40
                reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (BB lower zone 20-40)"
            else:
                c3 = LONG_RSI_LOW <= rsi_15m <= LONG_RSI_HIGH
                reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (zone {LONG_RSI_LOW}-{LONG_RSI_HIGH})"
        else:
            c3 = SHORT_RSI_LOW <= rsi_15m <= SHORT_RSI_HIGH
            reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (zone {SHORT_RSI_LOW}-{SHORT_RSI_HIGH})"
    else:
        c3 = False
        reasons["rsi_15m"] = "RSI 15M unavailable"
    criteria["rsi_15m"] = c3

    # ---- Criterion 4: Entry Below/Above Midline (confirms actual bounce level reached) ----
    # For LONG: price must be at or below BB mid — ensures we're buying a real pullback,
    #   not entering while price is still falling toward the mid from above.
    # For SHORT: price must be at or above BB mid — price has rallied to mid before rejection.
    if direction == "LONG":
        c4 = bb_mid is not None and price <= bb_mid
        reasons["tp_viable"] = (
            f"Price {'at/below' if c4 else 'above'} BB mid ({bb_mid:.0f})" if bb_mid else "BB mid unavailable"
        )
    else:
        if _breakdown_setup:
            # Breakdown continuation: price below BB mid is EXPECTED (that's the setup trigger)
            c4 = True
            reasons["tp_viable"] = f"Breakdown: price below BB mid (expected — targeting BB lower/beyond)"
        else:
            c4 = bb_mid is not None and price >= bb_mid
            reasons["tp_viable"] = (
                f"Price {'at/above' if c4 else 'below'} BB mid ({bb_mid:.0f})" if bb_mid else "BB mid unavailable"
            )
    criteria["tp_viable"] = c4

    # ---- Criterion 5: Price Structure (setup-type-aware) ----
    # For oversold setups (bb_lower_bounce, oversold_reversal): being below EMA50 is EXPECTED.
    # Accept reversal signals (swept_low, bullish candle, lower wick) instead of EMA50 position.
    if direction == "LONG":
        if _oversold_setup:
            # Oversold: check for reversal signals instead of EMA50 position
            swept_low = tf_15m.get("swept_low", False)
            candle_patterns = tf_15m.get("candlestick_patterns", [])
            bullish_candle = any(p.get("direction") == "bullish" for p in candle_patterns) if candle_patterns else False
            c_open = tf_15m.get("open")
            c_low = tf_15m.get("low")
            lower_wick = (min(c_open, price) - c_low) if (c_open is not None and c_low is not None) else 0
            c5 = bool(above_ema50_15m) or swept_low or bullish_candle or lower_wick >= 15
            reasons["structure"] = f"Oversold structure: {'above EMA50' if above_ema50_15m else 'below EMA50 (expected)'} | reversal={'Y' if (swept_low or bullish_candle or lower_wick >= 15) else 'N'}"
        else:
            c5 = bool(above_ema50_15m) if above_ema50_15m is not None else False
            reasons["structure"] = f"Price {'above' if c5 else 'below'} EMA50 on 15M"
    else:
        if _overbought_setup and bool(above_ema50_15m):
            # Overbought: above EMA50 is EXPECTED at the reversal point
            swept_high = tf_15m.get("swept_high", False)
            cp_dir_s = tf_15m.get("candlestick_direction")
            bearish_candle = cp_dir_s == "bearish"
            c_open_s = tf_15m.get("open")
            c_high_s = tf_15m.get("high")
            upper_wick_s = (c_high_s - max(c_open_s, price)) if (c_open_s is not None and c_high_s is not None) else 0
            c5 = swept_high or bearish_candle or upper_wick_s >= 15 or not bool(above_ema50_15m)
            reasons["structure"] = f"Overbought structure: above EMA50 (expected) | reversal={'Y' if c5 else 'N'}"
        else:
            c5 = (not bool(above_ema50_15m)) if above_ema50_15m is not None else False
            reasons["structure"] = f"Price {'below (bearish)' if c5 else 'above (not bearish)'} EMA50 on 15M"
    criteria["structure"] = c5

    # ---- Criterion 6: Macro / 4H Aligned ----
    # LONG: 4H RSI not overbought (<70) AND not below 40 (losing momentum)
    # SHORT: 4H RSI not oversold (>30) AND not above 60 (still has room to fall)
    if rsi_4h is not None:
        if direction == "LONG":
            c6 = 35 <= rsi_4h <= 75  # expanded: strong trends can have 4H RSI 70-75 during pullbacks
            reasons["macro"] = f"4H RSI: {rsi_4h:.1f} (want 35-75)"
        else:
            c6 = 30 <= rsi_4h <= 60
            reasons["macro"] = f"4H RSI: {rsi_4h:.1f} (want 30-60 for short)"
    else:
        c6 = False
        reasons["macro"] = "4H RSI unavailable"
    criteria["macro"] = c6

    # ---- Criterion 7: No High-Impact Event Within 1 Hour ----
    c7 = True
    now_utc = datetime.now(timezone.utc)
    if upcoming_events:
        for event in upcoming_events:
            event_time = event.get("time") or event.get("datetime")
            impact = (event.get("impact") or event.get("importance") or "").upper()
            if impact not in ("HIGH", "3"):
                continue
            if isinstance(event_time, str):
                try:
                    # Try parsing as ISO format or just use basic comparison
                    from datetime import datetime as dt
                    event_dt = dt.fromisoformat(event_time.replace("Z", "+00:00"))
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=timezone.utc)
                    minutes_until = (event_dt - now_utc).total_seconds() / 60
                    if 0 < minutes_until < 60:
                        c7 = False
                        reasons["no_event_1hr"] = f"High-impact event in {minutes_until:.0f} min: {event.get('name', '?')}"
                        break
                except (ValueError, TypeError):
                    pass
    if c7:
        reasons["no_event_1hr"] = "No high-impact events within 1 hour"
    criteria["no_event_1hr"] = c7

    # ---- Criterion 8: No Friday/Month-End ----
    from core.session import is_friday_blackout, is_month_end_blackout
    friday_blocked, friday_reason = is_friday_blackout(upcoming_events)
    monthend_blocked, monthend_reason = is_month_end_blackout()
    c8 = not friday_blocked and not monthend_blocked
    if not c8:
        reasons["no_friday_monthend"] = friday_reason or monthend_reason
    else:
        reasons["no_friday_monthend"] = "Calendar clear"
    criteria["no_friday_monthend"] = c8

    # ---- Criterion 9: Volume Not Critically Low ----
    volume_signal = tf_15m.get("volume_signal", "NORMAL")
    c9 = (volume_signal != "LOW")
    reasons["volume"] = f"15M volume signal: {volume_signal}"
    criteria["volume"] = c9

    # ---- Criterion 10: 4H EMA50 Alignment (setup-type-aware) ----
    # Oversold bounces: 4H below EMA50 is EXPECTED (that's why it's oversold).
    # Pass if RSI 4H < 40 (multi-TF oversold confluence) or daily bullish (structural support).
    above_ema50_4h = tf_4h.get("above_ema50")
    if above_ema50_4h is not None:
        if direction == "LONG":
            if _oversold_setup and not bool(above_ema50_4h):
                # Oversold: below 4H EMA50 is expected; pass if multi-TF oversold or daily bullish
                rsi_4h_oversold = rsi_4h is not None and rsi_4h < 45
                c10 = rsi_4h_oversold or bool(above_ema200_daily)
                rsi_4h_str = f"{rsi_4h:.1f}" if rsi_4h is not None else "N/A"
                daily_str = "bullish" if above_ema200_daily else "bearish"
                reasons["trend_4h"] = f"4H EMA50: below (expected for oversold) | 4H RSI={rsi_4h_str} | daily={daily_str}"
            else:
                c10 = bool(above_ema50_4h)
                reasons["trend_4h"] = f"4H EMA50: {'above (bullish)' if c10 else 'below (bearish)'}"
        else:
            if _overbought_setup and bool(above_ema50_4h):
                # Overbought: 4H above EMA50 is expected; pass if RSI_4H > 55 or daily bearish
                rsi_4h_overbought = rsi_4h is not None and rsi_4h > 55
                c10 = rsi_4h_overbought or not bool(above_ema200_daily)
                rsi_4h_str_ob = f"{rsi_4h:.1f}" if rsi_4h is not None else "N/A"
                daily_str_ob = "bearish" if not above_ema200_daily else "bullish"
                reasons["trend_4h"] = f"4H EMA50: above (expected for overbought) | 4H RSI={rsi_4h_str_ob} | daily={daily_str_ob}"
            else:
                c10 = not bool(above_ema50_4h)
                reasons["trend_4h"] = f"4H EMA50: {'below (bearish)' if c10 else 'above (not bearish)'}"
    else:
        c10 = True  # default pass if 4H data unavailable
        reasons["trend_4h"] = "4H EMA50 unavailable — defaulting pass"
    criteria["trend_4h"] = c10

    # ---- Criterion 11: Heiken Ashi Alignment (setup-type-aware) ----
    # Oversold bounces: bearish HA is EXPECTED at the reversal point (mean-reversion).
    # Pass if: HA already turning bullish, OR streak weakening (>= -2), OR bullish candle pattern present.
    ha_bullish = tf_15m.get("ha_bullish")
    ha_streak = tf_15m.get("ha_streak")
    streak_str = f", streak={ha_streak}" if ha_streak is not None else ""
    if ha_bullish is not None:
        if direction == "LONG":
            if _oversold_setup and not bool(ha_bullish):
                # Oversold: bearish HA expected at reversal point
                streak_weakening = ha_streak is not None and ha_streak >= -2
                candle_patterns_ha = tf_15m.get("candlestick_patterns", [])
                bullish_pattern_ha = any(p.get("direction") == "bullish" for p in candle_patterns_ha) if candle_patterns_ha else False
                c11 = streak_weakening or bullish_pattern_ha or (rsi_15m is not None and rsi_15m < 30)
                reasons["ha_aligned"] = f"HA 15M: bearish (expected for oversold){streak_str} | streak_weakening={'Y' if streak_weakening else 'N'}"
            else:
                c11 = bool(ha_bullish)
                reasons["ha_aligned"] = f"HA 15M: {'bullish' if c11 else 'bearish'}{streak_str}"
        else:
            if _overbought_setup and bool(ha_bullish):
                # Overbought: bullish HA expected at reversal point
                streak_weakening_ob = ha_streak is not None and ha_streak <= 2
                cp_dir_ha = tf_15m.get("candlestick_direction")
                bearish_pattern_ha = cp_dir_ha == "bearish"
                c11 = streak_weakening_ob or bearish_pattern_ha or (rsi_15m is not None and rsi_15m > 70)
                reasons["ha_aligned"] = f"HA 15M: bullish (expected for overbought){streak_str} | streak_weakening={'Y' if streak_weakening_ob else 'N'}"
            else:
                c11 = not bool(ha_bullish)
                reasons["ha_aligned"] = f"HA 15M: {'bearish (aligned SHORT)' if c11 else 'bullish (counter-HA)'}{streak_str}"
    else:
        c11 = True  # default pass if HA unavailable (older candle data)
        reasons["ha_aligned"] = "HA unavailable — defaulting pass"
    criteria["ha_aligned"] = c11

    # ---- Criterion 12: Entry Quality (pullback depth + volatility regime) ----
    # Backtest data (1620 scalp trades):
    #   Pullback (<-30pts before LONG): 43% WR. Chase (>+30pts): 36% WR.
    #   High vol (>120pt candles): 49% WR. Medium vol (56-123pt): 37% WR.
    # LONG: price should have pulled back (fallen) before entry — buying a real dip.
    # SHORT: price should have rallied (risen) before entry — selling a real top.
    # High volatility passes regardless — moves are decisive in high vol.
    pullback = tf_15m.get("pullback_depth", 0) or 0
    avg_range = tf_15m.get("avg_candle_range", 0) or 0
    HIGH_VOL_THRESHOLD = 120  # pts — above this, pass regardless (big moves follow through)

    if avg_range >= HIGH_VOL_THRESHOLD:
        c12 = True
        reasons["entry_quality"] = f"High vol ({avg_range:.0f}pts avg range) — moves decisive"
    elif direction == "LONG":
        # Require pullback: price should have fallen before LONG entry (buying the dip)
        c12 = pullback < 0
        reasons["entry_quality"] = f"Pullback {pullback:+.0f}pts ({'dip' if c12 else 'chase — no pullback'}), vol={avg_range:.0f}pts"
    else:
        # Require rally: price should have risen before SHORT entry (selling the top)
        c12 = pullback > 0
        reasons["entry_quality"] = f"Pre-entry {pullback:+.0f}pts ({'rally' if c12 else 'falling — no rally'}), vol={avg_range:.0f}pts"
    criteria["entry_quality"] = c12

    # ---- Compute final score (proportional, 12 criteria) ----
    passed = sum(1 for v in criteria.values() if v)
    total = len(criteria)
    score = min(BASE_SCORE + int(passed * (MAX_SCORE - BASE_SCORE) / total), MAX_SCORE)

    min_threshold = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE_LONG
    meets_threshold = score >= min_threshold

    return {
        "score": score,
        "passed_criteria": passed,
        "total_criteria": len(criteria),
        "criteria": criteria,
        "reasons": reasons,
        "direction": direction,
        "meets_threshold": meets_threshold,
        "min_threshold": min_threshold,
    }


def format_confidence_breakdown(result: dict) -> str:
    """Format a confidence result for Telegram/logging."""
    lines = [f"Local confidence: {result['score']}% ({result['passed_criteria']}/{result['total_criteria']} criteria)"]
    for criterion, passed in result["criteria"].items():
        icon = "✓" if passed else "✗"
        reason = result["reasons"].get(criterion, "")
        lines.append(f"  {icon} {criterion}: {reason}")
    lines.append(f"Threshold: {result['min_threshold']}% | {'PASS' if result['meets_threshold'] else 'FAIL'}")
    return "\n".join(lines)
