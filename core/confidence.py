"""
Local confidence scorer — bidirectional 9-criteria weighted system.

Computes a confidence score LOCALLY from indicator data before any AI call.
Acts as a gate: only escalate to Sonnet/Opus if local score >= HAIKU_MIN_SCORE (60%).

Scoring (weighted, 9 criteria):
  score = sum(weight_i × 100) for passing criteria
  Weights (normalized to sum=1.0): daily_trend=15.5%, entry_level=15.5%,
           rsi_15m=11.8%, structure=11.8%, tp_viable=10%, macro=10%,
           trend_4h=9.1%, volume=8.2%, entry_timing=8.2%

  100% = all 9 pass. Thresholds: LONG≥70%, SHORT≥75%.
  C7(event) and C8(friday) are hard pre-gates — not scored.
  C11(ha_aligned) + C12(entry_quality) merged into entry_timing (OR logic).

Criteria (9 scored):
  daily_trend  — daily EMA50 agrees with direction (oversold exempt)
  entry_level  — price at BB/EMA/VWAP technical level
  rsi_15m      — 15M RSI in valid entry zone
  tp_viable    — price below/above BB mid (room for TP)
  structure    — 15M EMA50 or reversal signals (setup-type-aware)
  macro        — 4H RSI in healthy range
  volume       — 15M volume not critically low
  trend_4h     — 4H EMA50 or oversold reversal (setup-type-aware)
  entry_timing — HA aligned OR entry quality (either is sufficient)

Hard gates (not scored):
  no_event_1hr      — checked before AI escalation
  no_friday_monthend — checked before AI escalation
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

    # Setup-type-aware flags — used by C1, C2, C3, C4, C5, C10, C11, C12
    _oversold_setup = setup_type in ("bollinger_lower_bounce", "oversold_reversal", "extreme_oversold_reversal")
    _overbought_setup = setup_type in ("overbought_reversal",)
    _breakdown_setup = setup_type in (
        "breakdown_continuation", "bear_flag_breakdown", "multi_tf_bearish"
    )
    _momentum_setup = setup_type in (
        "momentum_continuation_long", "breakout_long", "vwap_bounce_long", "ema9_pullback_long",
    )
    _momentum_short_setup = setup_type in (
        "momentum_continuation_short", "vwap_rejection_short_momentum",
        "ema9_pullback_short",
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
            elif not c1 and _momentum_setup:
                # Momentum setups fire on 15M EMA50 which recovers faster than daily.
                # After a crash, 15M trends up while daily EMA50 still lags below.
                c1 = True
                reasons["daily_trend"] = f"Daily EMA50: below (momentum exempt — 15M trend recovers faster)"
            else:
                reasons["daily_trend"] = f"Daily EMA50: {'above' if c1 else 'below'} (price={price:.0f})"
        elif above_ema200_daily is not None:
            c1 = bool(above_ema200_daily)
            if not c1 and _oversold_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: below (oversold exempt — counter-trend bounce)"
            elif not c1 and _momentum_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: below (momentum exempt — 15M trend recovers faster)"
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
            elif not c1 and (_breakdown_setup or _momentum_short_setup):
                # Breakdown/momentum shorts: daily bullish is EXPECTED during transition.
                # 4H/15M already turned bearish but daily EMA lags on big selloff days.
                c1 = True
                reasons["daily_trend"] = f"Daily EMA50: above (breakdown/momentum exempt — daily lags in transition)"
            else:
                reasons["daily_trend"] = f"Daily EMA50: {'below (bearish)' if c1 else 'above (not bearish)'}"
        elif above_ema200_daily is not None:
            c1 = not bool(above_ema200_daily)
            if not c1 and _overbought_setup:
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: above (overbought exempt — counter-trend reversal)"
            elif not c1 and (_breakdown_setup or _momentum_short_setup):
                c1 = True
                reasons["daily_trend"] = f"Daily EMA200: above (breakdown/momentum exempt — daily lags in transition)"
            else:
                reasons["daily_trend"] = f"Daily EMA50 N/A, using EMA200: {'below (bearish)' if c1 else 'above (not bearish)'}"
        else:
            c1 = False
            reasons["daily_trend"] = "Daily EMA data unavailable"
    criteria["daily_trend"] = c1

    # ---- Criterion 2: Entry at Technical Level ----
    vwap_15m = tf_15m.get("vwap")
    above_vwap_15m = tf_15m.get("above_vwap")
    ema9_15m = tf_15m.get("ema9")
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
        # Momentum entries: near BB upper (breakout), above VWAP (trend), near EMA9 (pullback)
        near_bb_upper_mom = (
            _momentum_setup and bb_upper is not None and abs(price - bb_upper) <= 200
        )
        above_vwap_mom = (
            _momentum_setup and above_vwap_15m is True
        )
        near_ema9_mom = (
            _momentum_setup and ema9_15m is not None and abs(price - ema9_15m) <= 100
        )
        # Anchored weekly VWAP: near the weekly institutional fair value (within 200pts)
        avwap_weekly = tf_15m.get("anchored_vwap_weekly")
        near_avwap_weekly = (
            avwap_weekly is not None and abs(price - avwap_weekly) <= 200
        )
        c2 = near_bb_mid or near_ema50 or near_bb_lower or near_vwap_long or near_bb_upper_mom or above_vwap_mom or near_ema9_mom or near_avwap_weekly
        if near_bb_lower:
            reasons["entry_level"] = f"Price {abs(price - bb_lower):.0f}pts from BB lower ({bb_lower:.0f})"
        elif near_bb_mid:
            reasons["entry_level"] = f"Price {abs(price - bb_mid):.0f}pts from BB mid ({bb_mid:.0f})"
        elif near_ema50:
            reasons["entry_level"] = f"Price {abs(price - ema50_15m):.0f}pts from EMA50 ({ema50_15m:.0f})"
        elif near_vwap_long:
            reasons["entry_level"] = f"Price {abs(price - vwap_15m):.0f}pts below VWAP ({vwap_15m:.0f}, discount)"
        elif near_bb_upper_mom:
            reasons["entry_level"] = f"Momentum: near BB upper ({bb_upper:.0f}, breakout zone)"
        elif above_vwap_mom:
            reasons["entry_level"] = f"Momentum: above VWAP ({vwap_15m:.0f}, trend continuation)"
        elif near_ema9_mom:
            reasons["entry_level"] = f"Momentum: near EMA9 ({ema9_15m:.0f}, pullback entry)"
        elif near_avwap_weekly:
            reasons["entry_level"] = f"Near weekly anchored VWAP ({avwap_weekly:.0f}, {abs(price - avwap_weekly):.0f}pts)"
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
        # Anchored weekly VWAP: near the weekly institutional fair value (within 200pts)
        avwap_weekly_s = tf_15m.get("anchored_vwap_weekly")
        near_avwap_weekly_s = (
            avwap_weekly_s is not None and abs(price - avwap_weekly_s) <= 200
        )
        c2 = near_bb_upper or near_bb_mid or near_ema50_short or near_vwap_short or near_avwap_weekly_s
        if near_bb_upper:
            reasons["entry_level"] = f"Price {abs(price - bb_upper):.0f}pts from BB upper ({bb_upper:.0f})"
        elif near_ema50_short:
            reasons["entry_level"] = f"Price {abs(price - ema50_15m):.0f}pts from EMA50 (rejection)"
        elif near_bb_mid:
            reasons["entry_level"] = f"Price {abs(price - bb_mid):.0f}pts from BB mid"
        elif near_vwap_short:
            reasons["entry_level"] = f"Price {abs(price - vwap_15m):.0f}pts above VWAP ({vwap_15m:.0f}, premium)"
        elif near_avwap_weekly_s:
            reasons["entry_level"] = f"Near weekly anchored VWAP ({avwap_weekly_s:.0f}, {abs(price - avwap_weekly_s):.0f}pts)"
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
            elif _momentum_setup:
                # Momentum: RSI 40-75 is healthy trending (widened: extreme rally RSI hits 73-75)
                c3 = 40 <= rsi_15m <= 75
                reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (momentum zone 40-75)"
            else:
                c3 = LONG_RSI_LOW <= rsi_15m <= LONG_RSI_HIGH
                reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (zone {LONG_RSI_LOW}-{LONG_RSI_HIGH})"
        elif _momentum_short_setup:
            # Momentum shorts: RSI 30-60 is valid (strong downtrend with room to fall)
            c3 = 30 <= rsi_15m <= 60
            reasons["rsi_15m"] = f"RSI 15M: {rsi_15m:.1f} (momentum short zone 30-60)"
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
        if _momentum_setup:
            # Momentum: price above BB mid is EXPECTED (trending up, TP = new highs / BB upper)
            c4 = True
            reasons["tp_viable"] = f"Momentum: price above BB mid expected (targeting BB upper / new highs)"
        else:
            c4 = bb_mid is not None and price <= bb_mid
            reasons["tp_viable"] = (
                f"Price {'at/below' if c4 else 'above'} BB mid ({bb_mid:.0f})" if bb_mid else "BB mid unavailable"
            )
    else:
        if _breakdown_setup or _momentum_short_setup:
            # Breakdown/momentum continuation: price below BB mid is EXPECTED
            c4 = True
            reasons["tp_viable"] = f"Breakdown/momentum: price below BB mid (expected — targeting BB lower/beyond)"
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
        c10 = False  # default FAIL if 4H data unavailable (conservative — don't inflate on API failure)
        reasons["trend_4h"] = "4H EMA50 unavailable — defaulting fail"
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
        c11 = False  # default FAIL if HA unavailable (conservative — don't inflate on missing data)
        reasons["ha_aligned"] = "HA unavailable — defaulting fail"
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
    elif direction == "LONG" and _momentum_setup:
        # Momentum: positive pullback_depth = trending up = EXPECTED (not chasing)
        c12 = True
        reasons["entry_quality"] = f"Momentum: trend {pullback:+.0f}pts (trending up is expected), vol={avg_range:.0f}pts"
    elif direction == "LONG":
        # Require pullback: price should have fallen before LONG entry (buying the dip)
        c12 = pullback < 0
        reasons["entry_quality"] = f"Pullback {pullback:+.0f}pts ({'dip' if c12 else 'chase — no pullback'}), vol={avg_range:.0f}pts"
    elif _momentum_short_setup:
        # Momentum SHORT: negative pullback_depth = trending down = EXPECTED
        c12 = True
        reasons["entry_quality"] = f"Momentum SHORT: trend {pullback:+.0f}pts (trending down is expected), vol={avg_range:.0f}pts"
    else:
        # Require rally: price should have risen before SHORT entry (selling the top)
        c12 = pullback > 0
        reasons["entry_quality"] = f"Pre-entry {pullback:+.0f}pts ({'rally' if c12 else 'falling — no rally'}), vol={avg_range:.0f}pts"
    criteria["entry_quality"] = c12

    # Weighted scoring: C7 (no_event_1hr) and C8 (no_friday_monthend) are hard gates
    # that run BEFORE AI escalation — they do not contribute to setup quality score.
    # C11 (ha_aligned) and C12 (entry_quality) merged into entry_timing.
    # Weights are normalized so all-9-pass = exactly 100.
    # Relative order preserved from spec (daily_trend/entry_level highest,
    # volume/entry_timing lowest). Raw spec values summed to 1.10; divided by 1.10.
    CRITERIA_WEIGHTS = {
        "daily_trend":  0.1545,
        "entry_level":  0.1545,
        "rsi_15m":      0.1182,
        "tp_viable":    0.1000,
        "structure":    0.1182,
        "macro":        0.1000,
        "volume":       0.0818,
        "trend_4h":     0.0909,
        "entry_timing": 0.0818,
    }

    # Map existing criteria names to weighted names
    # C11 (ha_aligned) + C12 (entry_quality) → entry_timing (both must pass for True)
    weighted_criteria = {
        "daily_trend":  criteria.get("daily_trend", False),
        "entry_level":  criteria.get("entry_level", False),
        "rsi_15m":      criteria.get("rsi_15m", False),
        "tp_viable":    criteria.get("tp_viable", False),
        "structure":    criteria.get("structure", False),
        "macro":        criteria.get("macro", False),
        "volume":       criteria.get("volume", False),
        "trend_4h":     criteria.get("trend_4h", False),
        # entry_timing = either HA aligned OR entry quality (either confirmation is sufficient)
        "entry_timing": criteria.get("ha_aligned", False) or criteria.get("entry_quality", False),
    }

    score = round(sum(CRITERIA_WEIGHTS[k] * 100 for k, v in weighted_criteria.items() if v))
    score = min(score, MAX_SCORE)

    # ---- R:R Estimate Penalty ----
    # Estimate R:R from market structure BEFORE AI call.
    # Prevents 91% confidence on R:R 0.4 setups from wasting API calls.
    atr_val = tf_15m.get("atr") or 150  # fallback to DEFAULT_SL_DISTANCE
    sl_estimate = max(atr_val * 1.5, 60)  # ATR × default multiplier, floored at SL_FLOOR

    if direction == "LONG":
        obstacles = []
        sh = tf_15m.get("swing_high_20")
        if sh and price and sh > price:
            obstacles.append(sh - price)
        if bb_upper and price and bb_upper > price:
            obstacles.append(bb_upper - price)
        tp_estimate = min(obstacles) if obstacles else 400
    else:
        obstacles = []
        sl_val = tf_15m.get("swing_low_20")
        if sl_val and price and sl_val < price:
            obstacles.append(price - sl_val)
        if bb_lower and price and bb_lower < price:
            obstacles.append(price - bb_lower)
        tp_estimate = min(obstacles) if obstacles else 400

    tp_estimate = max(tp_estimate, 150)  # minimum TP floor
    estimated_rr = tp_estimate / sl_estimate if sl_estimate > 0 else 0

    if estimated_rr >= 1.5:
        rr_factor = 1.0
    elif estimated_rr >= 1.2:
        rr_factor = 0.95
    elif estimated_rr >= 1.0:
        rr_factor = 0.90
    elif estimated_rr >= 0.8:
        rr_factor = 0.80
    else:
        rr_factor = 0.70  # soft penalty — AI still gets a chance on strong setups (91%→64%)

    pre_rr_score = score
    score = round(score * rr_factor)
    score = min(score, MAX_SCORE)

    rr_penalized = rr_factor < 1.0
    criteria["rr_estimate"] = not rr_penalized
    reasons["rr_estimate"] = (
        f"Est. R:R {estimated_rr:.2f} (TP~{tp_estimate:.0f}pts / SL~{sl_estimate:.0f}pts)"
        + (f" → score {pre_rr_score}→{score} (×{rr_factor})" if rr_penalized else "")
    )

    passed_count = sum(1 for v in weighted_criteria.values() if v)
    total = len(weighted_criteria)

    min_threshold = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE_LONG
    meets = score >= min_threshold
    return {
        "score": score,
        "criteria": criteria,           # keep original 12-criteria dict for logging/diagnostics
        "weighted_criteria": weighted_criteria,  # the 9-criteria weighted view
        "reasons": reasons,
        "direction": direction,
        "meets_threshold": meets,
        "passed_criteria": passed_count,
        "total_criteria": total,
        "min_threshold": min_threshold,
        "estimated_rr": round(estimated_rr, 2),
        "rr_factor": rr_factor,
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
