# core/confidence.py — DIGEST
# Purpose: Local 9-criteria weighted confidence scorer + R:R penalty. Gates AI escalation (score must be >=60%).
# Bidirectional: LONG and SHORT criteria differ.
# Updated 2026-03-10: Added R:R estimate penalty multiplier on final score.

## Constants
BASE_SCORE=30  MAX_SCORE=100
MIN_CONFIDENCE_LONG=70  MIN_CONFIDENCE_SHORT=75
BB_MID_THRESHOLD_PTS=150   EMA50_THRESHOLD_PTS=150

## compute_confidence(direction, tf_daily, tf_4h, tf_15m, upcoming_events=None, web_research=None, setup_type=None) -> dict
# score = sum(weight_i × 100) for each passing weighted criterion × rr_factor
# 9 weighted criteria:
# 1. daily_trend (17%):  EMA50 PRIMARY. Oversold exempt. Momentum LONG/SHORT exempt.
# 2. entry_level (17%):  Near BB mid/EMA50/BB lower/VWAP. MOMENTUM: near BB upper, EMA9, anchored VWAP.
# 3. rsi_15m (13%):      LONG 30-55. BB lower 20-40. MOMENTUM LONG 40-75. SHORT 55-75. MOMENTUM SHORT 30-60.
# 4. structure (13%):    LONG: above EMA50. SHORT: below EMA50. Oversold/overbought: reversal signals.
# 5. tp_viable (11%):    LONG: price<=bb_mid. MOMENTUM: always pass. SHORT breakdown/momentum: always pass.
# 6. macro (11%):        4H RSI range. LONG 35-75. SHORT 30-60.
# 7. trend_4h (10%):     4H EMA50 alignment. Oversold/overbought: lenient.
# 8. volume (9%):        15M volume signal != LOW.
# 9. entry_timing (9%):  ha_aligned OR entry_quality (OR logic — one of two confirms entry)
#
# C7 (no_event_1hr) and C8 (no_friday_monthend) are hard pre-gates only — NOT scored.
#
# R:R ESTIMATE PENALTY (applied after criteria scoring):
#   Estimates R:R from ATR (SL) and nearest obstacle (swing_high/BB upper for TP).
#   rr >= 1.5 → ×1.0 | rr >= 1.2 → ×0.95 | rr >= 1.0 → ×0.90 | rr >= 0.8 → ×0.80 | rr < 0.8 → ×0.70
#   Example: 91% score + R:R 0.5 → 91 × 0.70 = 64% (still passes AI gate but below LONG threshold)
#
# Return dict includes:
#   criteria (original 12-key dict + rr_estimate for logging/diagnostics)
#   weighted_criteria (9-key dict used for scoring)
#   estimated_rr, rr_factor (new fields for R:R penalty tracking)

## Setup-Type Flags
# _oversold_setup: bb_lower_bounce, oversold_reversal, extreme_oversold_reversal → C1/C5/C10/C11 lenient
# _overbought_setup: overbought_reversal → C1/C5/C10/C11 lenient
# _breakdown_setup: breakdown_continuation, bear_flag_breakdown, multi_tf_bearish → C1/C4 exempt
# _momentum_setup: momentum_continuation_long, breakout_long, vwap_bounce_long, ema9_pullback_long → C1/C2/C3/C4/C12 adjusted
# _momentum_short_setup: momentum_continuation_short, vwap_rejection_short_momentum → C1/C4/C12 adjusted

## format_confidence_breakdown(result: dict) -> str
# Human-readable string for Telegram/logging.
