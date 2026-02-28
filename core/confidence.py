"""
Local confidence scorer — bidirectional 8-criteria system.

Computes the 8-point confidence score LOCALLY from indicator data
before any AI call. This acts as a gate: only escalate to AI if
local score is promising. The AI then performs its own independent
scoring, but the local score is included in the prompt for context.

Scoring:
  Base: 30%
  Each criterion: +10%
  Max: 30 + 8×10 = 110%, capped at 100%

Minimum to trade:
  LONG:  70%  (4/8 criteria + base)
  SHORT: 75%  (5/8 criteria - but 4/8 + base = 70%, so 75% needs partial overlap)

Note: 75% = base(30) + 4.5 criteria → in practice, 5 criteria gives 80%, 4 gives 70%.
The SHORT threshold is a calibration gate, not a hard floor on criteria count.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Weights — each 10 points
CRITERIA_WEIGHT = 10
BASE_SCORE = 30
MAX_SCORE = 100

MIN_CONFIDENCE_LONG = 70
MIN_CONFIDENCE_SHORT = 75

# RSI ranges
LONG_RSI_LOW, LONG_RSI_HIGH = 35, 55
SHORT_RSI_LOW, SHORT_RSI_HIGH = 55, 75

# Bollinger nearness threshold (points from midband)
BB_MID_THRESHOLD_PTS = 30

# EMA50 nearness for EMA bounce setup (points)
EMA50_THRESHOLD_PTS = 30


def compute_confidence(
    direction: str,
    tf_daily: dict,
    tf_4h: dict,
    tf_15m: dict,
    upcoming_events: list = None,
    web_research: dict = None,
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

    # ---- Criterion 1: Daily Trend Aligned ----
    if direction == "LONG":
        # Price above daily EMA200 (primary) or EMA50 if EMA200 unavailable
        if above_ema200_daily is not None:
            c1 = bool(above_ema200_daily)
            reasons["daily_trend"] = f"Daily EMA200: {'above' if c1 else 'below'} (price={price:.0f})"
        elif above_ema50_daily is not None:
            c1 = bool(above_ema50_daily)
            reasons["daily_trend"] = f"Daily EMA200 N/A, using EMA50: {'above' if c1 else 'below'}"
        else:
            c1 = False
            reasons["daily_trend"] = "Daily EMA data unavailable"
    else:  # SHORT
        if above_ema200_daily is not None:
            c1 = not bool(above_ema200_daily)
            reasons["daily_trend"] = f"Daily EMA200: {'below (bearish)' if c1 else 'above (not bearish)'}"
        elif above_ema50_daily is not None:
            c1 = not bool(above_ema50_daily)
            reasons["daily_trend"] = f"Daily EMA200 N/A, using EMA50: {'below' if c1 else 'above'}"
        else:
            c1 = False
            reasons["daily_trend"] = "Daily EMA data unavailable"
    criteria["daily_trend"] = c1

    # ---- Criterion 2: Entry at Technical Level ----
    if direction == "LONG":
        # Near Bollinger midband (within BB_MID_THRESHOLD_PTS) OR near EMA50 (within EMA50_THRESHOLD_PTS)
        near_bb_mid = (
            bb_mid is not None and abs(price - bb_mid) <= BB_MID_THRESHOLD_PTS
        )
        near_ema50 = (
            ema50_15m is not None and abs(price - ema50_15m) <= EMA50_THRESHOLD_PTS
        )
        c2 = near_bb_mid or near_ema50
        if near_bb_mid:
            reasons["entry_level"] = f"Price {abs(price - bb_mid):.0f}pts from BB mid ({bb_mid:.0f})"
        elif near_ema50:
            reasons["entry_level"] = f"Price {abs(price - ema50_15m):.0f}pts from EMA50 ({ema50_15m:.0f})"
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
        c2 = near_bb_upper or near_bb_mid or near_ema50_short
        if near_bb_upper:
            reasons["entry_level"] = f"Price {abs(price - bb_upper):.0f}pts from BB upper ({bb_upper:.0f})"
        elif near_ema50_short:
            reasons["entry_level"] = f"Price {abs(price - ema50_15m):.0f}pts from EMA50 (rejection)"
        elif near_bb_mid:
            reasons["entry_level"] = f"Price {abs(price - bb_mid):.0f}pts from BB mid"
        else:
            reasons["entry_level"] = "Not at technical level for short"
    criteria["entry_level"] = c2

    # ---- Criterion 3: RSI 15M in Zone ----
    if rsi_15m is not None:
        if direction == "LONG":
            c3 = LONG_RSI_LOW <= rsi_15m <= LONG_RSI_HIGH
            reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (zone {LONG_RSI_LOW}-{LONG_RSI_HIGH})"
        else:
            c3 = SHORT_RSI_LOW <= rsi_15m <= SHORT_RSI_HIGH
            reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (zone {SHORT_RSI_LOW}-{SHORT_RSI_HIGH})"
    else:
        c3 = False
        reasons["rsi_15m"] = "RSI 15M unavailable"
    criteria["rsi_15m"] = c3

    # ---- Criterion 4: TP Viable (100 pts to next level) ----
    # For LONG: 100 pts to upper BB or resistance implies target is achievable
    # For SHORT: 100 pts to lower BB or support implies target is achievable
    if direction == "LONG":
        if bb_upper is not None:
            pts_to_upper = bb_upper - price
            c4 = pts_to_upper >= 100
            reasons["tp_viable"] = f"BB upper {pts_to_upper:.0f}pts away (need 100+)"
        else:
            c4 = False
            reasons["tp_viable"] = "BB upper unavailable"
    else:
        if bb_lower is not None:
            pts_to_lower = price - bb_lower
            c4 = pts_to_lower >= 100
            reasons["tp_viable"] = f"BB lower {pts_to_lower:.0f}pts away (need 100+)"
        else:
            c4 = False
            reasons["tp_viable"] = "BB lower unavailable"
    criteria["tp_viable"] = c4

    # ---- Criterion 5: Price Structure ----
    # LONG: price above EMA50 on 15M (higher lows structure)
    # SHORT: price below EMA50 on 15M (lower highs structure)
    if direction == "LONG":
        c5 = bool(above_ema50_15m) if above_ema50_15m is not None else False
        reasons["structure"] = f"Price {'above' if c5 else 'below'} EMA50 on 15M"
    else:
        c5 = (not bool(above_ema50_15m)) if above_ema50_15m is not None else False
        reasons["structure"] = f"Price {'below (bearish)' if c5 else 'above (not bearish)'} EMA50 on 15M"
    criteria["structure"] = c5

    # ---- Criterion 6: Macro / 4H Aligned ----
    # LONG: 4H RSI not overbought (<70) AND not below 40 (losing momentum)
    # SHORT: 4H RSI not oversold (>30) AND not above 60 (still has room to fall)
    if rsi_4h is not None:
        if direction == "LONG":
            c6 = 40 <= rsi_4h <= 70
            reasons["macro"] = f"4H RSI: {rsi_4h:.1f} (want 40-70)"
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

    # ---- Compute final score ----
    passed = sum(1 for v in criteria.values() if v)
    score = min(BASE_SCORE + passed * CRITERIA_WEIGHT, MAX_SCORE)

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
