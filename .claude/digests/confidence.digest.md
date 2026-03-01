# core/confidence.py — DIGEST
# Purpose: Local 8-criteria confidence scorer. Gates AI escalation (score must be >=50%).
# Bidirectional: LONG and SHORT criteria differ.
# Updated 2026-02-28: thresholds recalibrated for Nikkei ~55k; RSI tightened; C4 redesigned.

## Constants
BASE_SCORE=30  CRITERIA_WEIGHT=10  MAX_SCORE=100
MIN_CONFIDENCE_LONG=70  MIN_CONFIDENCE_SHORT=75
BB_MID_THRESHOLD_PTS=150   # calibrated for Nikkei ~55k
EMA50_THRESHOLD_PTS=150    # calibrated for Nikkei ~55k
LONG_RSI_LOW/HIGH = 35 / RSI_ENTRY_HIGH_BOUNCE (imported from settings, currently 48)
SHORT_RSI_LOW/HIGH=55/75

## compute_confidence(direction, tf_daily, tf_4h, tf_15m, upcoming_events=None, web_research=None) -> dict
# Returns: {score, passed_criteria, total_criteria, criteria, reasons, direction,
#           meets_threshold, min_threshold}
# score = min(30 + passed_count * 10, 100)

# 8 criteria:
# 1. daily_trend:     LONG=above EMA200 daily.  SHORT=below EMA200 daily.  (EMA50 fallback)
# 2. entry_level:     LONG=near BB_mid (±150pts) OR EMA50 (±150pts) OR BB_lower (±80pts).
#                     SHORT=near BB_upper or BB_mid or EMA50_from_below.
# 3. rsi_15m:         LONG standard=RSI 35-48.  LONG at BB lower=RSI 20-40 (deeply oversold zone).
#                     SHORT=RSI 55-75.
#                     Setup-aware: if price within 80pts of bb_lower → uses 20-40 zone.
# 4. tp_viable:       LONG=price<=bb_mid (confirms price reached pullback level).
#                     SHORT=price>=bb_mid (confirms rally to midline before rejection).
# 5. structure:       LONG=price above EMA50_15m.  SHORT=price below EMA50_15m.
#                     NOTE: bollinger_lower_bounce may fail C5 (price below EMA50) — expected.
# 6. macro:           LONG=4H RSI 35-75.  SHORT=4H RSI 30-60.
# 7. no_event_1hr:    No HIGH-impact event within 60min (checks upcoming_events list).
# 8. no_friday_monthend: Calls session.is_friday_blackout() + is_month_end_blackout().

## format_confidence_breakdown(result: dict) -> str
# Human-readable string for Telegram/logging. Shows ✓/✗ per criterion.
