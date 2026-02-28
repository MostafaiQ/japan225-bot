# core/confidence.py — DIGEST
# Purpose: Local 8-criteria confidence scorer. Gates AI escalation (score must be >=50%).
# Bidirectional: LONG and SHORT criteria differ.

## Constants
BASE_SCORE=30  CRITERIA_WEIGHT=10  MAX_SCORE=100
MIN_CONFIDENCE_LONG=70  MIN_CONFIDENCE_SHORT=75
BB_MID_THRESHOLD_PTS=30  EMA50_THRESHOLD_PTS=30
LONG_RSI_LOW/HIGH=35/55  SHORT_RSI_LOW/HIGH=55/75

## compute_confidence(direction, tf_daily, tf_4h, tf_15m, upcoming_events=None, web_research=None) -> dict
# Returns: {score, passed_criteria, total_criteria, criteria, reasons, direction,
#           meets_threshold, min_threshold}
# score = min(30 + passed_count * 10, 100)

# 8 criteria:
# 1. daily_trend:     LONG=above EMA200 daily.  SHORT=below EMA200 daily.  (EMA50 fallback)
# 2. entry_level:     LONG=near BB_mid or EMA50 (±30pts). SHORT=near BB_upper or EMA50_from_below.
# 3. rsi_15m:         LONG=RSI 35-55.  SHORT=RSI 55-75.
# 4. tp_viable:       LONG=100+pts to BB_upper.  SHORT=100+pts to BB_lower.
# 5. structure:       LONG=price above EMA50_15m.  SHORT=price below EMA50_15m.
# 6. macro:           LONG=4H RSI 40-70.  SHORT=4H RSI 30-60.
# 7. no_event_1hr:    No HIGH-impact event within 60min (checks upcoming_events list).
# 8. no_friday_monthend: Calls session.is_friday_blackout() + is_month_end_blackout().

## format_confidence_breakdown(result: dict) -> str
# Human-readable string for Telegram/logging. Shows ✓/✗ per criterion.
