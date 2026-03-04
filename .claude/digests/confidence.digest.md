# core/confidence.py — DIGEST
# Purpose: Local 12-criteria confidence scorer. Gates AI escalation (score must be >=60%).
# Bidirectional: LONG and SHORT criteria differ.
# Updated 2026-03-04: _momentum_setup + _momentum_short_setup flags for trend-following setups.

## Constants
BASE_SCORE=30  MAX_SCORE=100
MIN_CONFIDENCE_LONG=70  MIN_CONFIDENCE_SHORT=75
BB_MID_THRESHOLD_PTS=150   EMA50_THRESHOLD_PTS=150
LONG_RSI_LOW/HIGH = 30 / RSI_ENTRY_HIGH_BOUNCE (55)
SHORT_RSI_LOW/HIGH=55/75

## compute_confidence(direction, tf_daily, tf_4h, tf_15m, upcoming_events=None, web_research=None, setup_type=None) -> dict
# score = min(30 + int(passed * 70 / total_criteria), 100)
# 12 criteria:
# 1. daily_trend:   EMA50 PRIMARY. Oversold exempt. Momentum LONG exempt. Breakdown/momentum SHORT exempt.
# 2. entry_level:   Near BB mid/EMA50/BB lower/VWAP. MOMENTUM: accepts above VWAP, near BB upper, near EMA9.
# 3. rsi_15m:       LONG 30-55. BB lower 20-40. MOMENTUM LONG 40-75. SHORT 55-75.
# 4. tp_viable:     LONG: price<=bb_mid. MOMENTUM: always pass. SHORT breakdown/momentum: always pass.
# 5. structure:     LONG: above EMA50. SHORT: below EMA50. Oversold/overbought: reversal signals.
# 6. macro:         4H RSI range. LONG 35-75. SHORT 30-60.
# 7. no_event_1hr:  No HIGH-impact event within 60min.
# 8. no_friday_monthend: Calendar clear.
# 9. volume:        15M volume signal != LOW.
# 10. trend_4h:     4H EMA50 alignment. Oversold/overbought: lenient.
# 11. ha_aligned:   HA direction. Oversold/overbought: reversal signals accepted.
# 12. entry_quality: LONG: pullback<0. MOMENTUM LONG: always pass. SHORT: pullback>0. MOMENTUM SHORT: always pass. High vol override.

## Setup-Type Flags
# _oversold_setup: bb_lower_bounce, oversold_reversal, extreme_oversold_reversal → C1/C5/C10/C11 lenient
# _overbought_setup: overbought_reversal → C1/C5/C10/C11 lenient
# _breakdown_setup: breakdown_continuation, bear_flag_breakdown, multi_tf_bearish → C1/C4 exempt
# _momentum_setup: momentum_continuation_long, breakout_long, vwap_bounce_long, ema9_pullback_long → C1/C2/C3/C4/C12 adjusted
# _momentum_short_setup: momentum_continuation_short, vwap_rejection_short_momentum → C1/C4/C12 adjusted

## format_confidence_breakdown(result: dict) -> str
# Human-readable string for Telegram/logging.
