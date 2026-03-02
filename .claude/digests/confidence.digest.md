# core/confidence.py — DIGEST
# Purpose: Local 10-criteria confidence scorer. Gates AI escalation (score must be >=60%).
# Bidirectional: LONG and SHORT criteria differ.
# Updated 2026-03-02: 10-criteria (C9 volume, C10 4H EMA50); proportional scoring formula.

## Constants
BASE_SCORE=30  MAX_SCORE=100
MIN_CONFIDENCE_LONG=70  MIN_CONFIDENCE_SHORT=75
BB_MID_THRESHOLD_PTS=150   # calibrated for Nikkei ~55k
EMA50_THRESHOLD_PTS=150    # calibrated for Nikkei ~55k
LONG_RSI_LOW/HIGH = 35 / RSI_ENTRY_HIGH_BOUNCE (imported from settings, currently 55)
SHORT_RSI_LOW/HIGH=55/75

## compute_confidence(direction, tf_daily, tf_4h, tf_15m, upcoming_events=None, web_research=None) -> dict
# Returns: {score, passed_criteria, total_criteria, criteria, reasons, direction,
#           meets_threshold, min_threshold}
# score = min(30 + int(passed * 70 / total_criteria), 100)
# 10/10=100%, 9/10=93%, 8/10=86%, 7/10=79%, 6/10=72%, 5/10=65%, 4/10=58% (fails 60 gate)

# 10 criteria:
# 1. daily_trend:        LONG=above EMA200 daily.  SHORT=below EMA200 daily.  (EMA50 fallback)
# 2. entry_level:        LONG=near BB_mid (±150pts) OR EMA50 (±150pts) OR BB_lower (±80pts).
#                        SHORT=near BB_upper or BB_mid or EMA50_from_below (price<=ema50).
# 3. rsi_15m:            LONG standard=RSI 35-55.  LONG at BB lower=RSI 20-40 (deeply oversold).
#                        SHORT=RSI 55-75.
#                        Setup-aware: if price within 80pts of bb_lower → uses 20-40 zone.
# 4. tp_viable:          LONG=price<=bb_mid (confirms price reached pullback level).
#                        SHORT=price>=bb_mid (confirms rally to midline before rejection).
# 5. structure:          LONG=price above EMA50_15m.  SHORT=price below EMA50_15m.
#                        NOTE: bollinger_lower_bounce may fail C5 — expected.
# 6. macro:              LONG=4H RSI 35-75.  SHORT=4H RSI 30-60.
# 7. no_event_1hr:       No HIGH-impact event within 60min (checks upcoming_events list).
# 8. no_friday_monthend: Calls session.is_friday_blackout() + is_month_end_blackout().
# 9. volume:             tf_15m.get("volume_signal","NORMAL") != "LOW". Defaults pass if missing.
# 10. trend_4h:          LONG=4H above EMA50.  SHORT=4H below EMA50.
#                        Defaults pass (True) if tf_4h.get("above_ema50") is None.

## Thresholds
# HAIKU_MIN_SCORE=60 → requires 5/10 criteria (5/10=65≥60).
# MIN_CONFIDENCE_LONG=70 → requires 6/10 (6/10=72≥70).
# MIN_CONFIDENCE_SHORT=75 → requires 7/10 (7/10=79≥75).

## format_confidence_breakdown(result: dict) -> str
# Human-readable string for Telegram/logging. Shows ✓/✗ per criterion.
