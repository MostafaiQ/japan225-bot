# core/confidence.py — DIGEST
# Purpose: Local 12-criteria confidence scorer. Gates AI escalation (score must be >=60%).
# Bidirectional: LONG and SHORT criteria differ.
# Updated 2026-03-02: 12-criteria (C12 entry_quality); C1 oversold-exempt; RSI gate 65→55.

## Constants
BASE_SCORE=30  MAX_SCORE=100
MIN_CONFIDENCE_LONG=70  MIN_CONFIDENCE_SHORT=75
BB_MID_THRESHOLD_PTS=150   # calibrated for Nikkei ~55k
EMA50_THRESHOLD_PTS=150    # calibrated for Nikkei ~55k
LONG_RSI_LOW/HIGH = 30 / RSI_ENTRY_HIGH_BOUNCE (imported from settings, currently 55)
SHORT_RSI_LOW/HIGH=55/75

## compute_confidence(direction, tf_daily, tf_4h, tf_15m, upcoming_events=None, web_research=None, setup_type=None) -> dict
# Returns: {score, passed_criteria, total_criteria, criteria, reasons, direction,
#           meets_threshold, min_threshold}
# score = min(30 + int(passed * 70 / total_criteria), 100)
# setup_type: optional parameter to enable setup-aware criteria (e.g., "oversold_reversal")
# 12/12=100%, 11/12=94%, 10/12=88%, 9/12=82%, 8/12=76%, 7/12=70%,
# 6/12=65% (fails 70 gate), 5/12=59% (below 60% gate)

# 12 criteria:
# 1. daily_trend:        LONG=above EMA200 daily.  SHORT=below EMA200 daily.  (EMA50 fallback)
#                        OVERSOLD EXEMPT: bb_lower_bounce/oversold_reversal pass C1 even when daily bearish.
#                        Backtest: counter-trend LONG WR=48% vs trend-aligned 41%.
# 2. entry_level:        LONG=near BB_mid (±150pts) OR EMA50 (±150pts) OR BB_lower (±150pts) OR VWAP below (±150pts).
#                        SHORT=near BB_upper or BB_mid or EMA50_from_below (price<=ema50) OR VWAP above (±150pts).
# 3. rsi_15m:            LONG standard=RSI 30-55 (was 30-65, tightened — RSI 55-65 WR=38%).
#                        LONG at BB lower=RSI 20-40 (deeply oversold).
#                        SHORT=RSI 55-75.
# 4. tp_viable:          LONG=price<=bb_mid.  SHORT=price>=bb_mid.
# 5. structure:          LONG=price above EMA50_15m OR reversal signals for oversold setups.
#                        SHORT=price below EMA50_15m.
# 6. macro:              LONG=4H RSI 35-75.  SHORT=4H RSI 30-60.
# 7. no_event_1hr:       No HIGH-impact event within 60min.
# 8. no_friday_monthend: Calls session.is_friday_blackout() + is_month_end_blackout().
# 9. volume:             tf_15m volume_signal != "LOW".
# 10. trend_4h:          LONG=4H above EMA50 OR oversold with RSI_4H<45/daily bullish.
#                        SHORT=4H below EMA50.
# 11. ha_aligned:        LONG=tf_15m ha_bullish OR oversold with streak>=-2/bullish candle/RSI<30.
#                        SHORT=ha_bullish is False.
# 12. entry_quality:     Pullback depth + volatility regime (data-backed).
#                        LONG: requires pullback_depth < 0 (price fell before entry = buying the dip).
#                        SHORT: requires pullback_depth > 0 (price rose before entry = selling the top).
#                        HIGH VOL OVERRIDE: avg_candle_range >= 120pts passes regardless (moves decisive).
#                        Backtest: pullback LONGs 43% WR vs chase 36%. High vol 49% vs med vol 37%.

## Thresholds & Setup-Type Rules
# HAIKU_MIN_SCORE=60 → requires 6/12 criteria (6/12=65≥60).
# MIN_CONFIDENCE_LONG=70 → requires 7/12 (7/12=70≥70).
# MIN_CONFIDENCE_SHORT=75 → requires 8/12 (8/12=76≥75).
# Oversold setups (bb_lower_bounce, oversold_reversal, extreme_oversold_reversal): C1/C5/C10/C11 have relaxed gates.

## format_confidence_breakdown(result: dict) -> str
# Human-readable string for Telegram/logging. Shows ✓/✗ per criterion.
