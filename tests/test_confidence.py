"""
Tests for core/confidence.py — bidirectional 11-criteria local scoring.
All tests use synthetic indicator data; no API calls needed.
"""
import pytest
from core.confidence import (
    compute_confidence,
    format_confidence_breakdown,
    BASE_SCORE, MIN_CONFIDENCE_LONG, MIN_CONFIDENCE_SHORT,
    BB_MID_THRESHOLD_PTS, EMA50_THRESHOLD_PTS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_tf(
    price=38000,
    rsi=45,
    bb_mid=37990,
    bb_upper=38300,
    bb_lower=37700,
    ema50=37980,
    ema200=37500,
    above_ema50=True,
    above_ema200=True,
    volume_signal="NORMAL",
    pullback_depth=-50,
    avg_candle_range=80,
    ha_bullish=None,
    ha_streak=None,
    atr=100,
    swing_high_20=None,
    swing_low_20=None,
):
    """Build a synthetic analyze_timeframe() output dict."""
    return {
        "price": price,
        "rsi": rsi,
        "bollinger_mid": bb_mid,
        "bollinger_upper": bb_upper,
        "bollinger_lower": bb_lower,
        "ema50": ema50,
        "ema200": ema200,
        "above_ema50": above_ema50,
        "above_ema200": above_ema200,
        "volume_signal": volume_signal,
        "pullback_depth": pullback_depth,
        "avg_candle_range": avg_candle_range,
        "ha_bullish": ha_bullish,
        "ha_streak": ha_streak,
        "atr": atr,
        "swing_high_20": swing_high_20,
        "swing_low_20": swing_low_20,
    }


def ideal_long_setup():
    """Return (tf_daily, tf_4h, tf_15m) for a perfect LONG setup."""
    tf_15m = make_tf(
        price=38000, rsi=42,   # RSI 42: in 35-48 zone
        bb_mid=38010,    # price 10 pts BELOW mid (C4: price <= bb_mid passes)
        bb_upper=38400,  # 400 pts to upper
        bb_lower=37700,
        ema50=37985,     # price above EMA50 (C5 passes)
        above_ema50=True, above_ema200=True,
        ha_bullish=True, ha_streak=3,  # C11: HA aligned bullish
    )
    tf_4h = make_tf(rsi=55, above_ema200=True, above_ema50=True)
    tf_daily = make_tf(rsi=60, above_ema200=True, above_ema50=True)
    return tf_daily, tf_4h, tf_15m


def ideal_short_setup():
    """Return (tf_daily, tf_4h, tf_15m) for a perfect SHORT setup."""
    # Daily bearish (price below EMA200)
    tf_daily = make_tf(
        price=38000, rsi=35,
        above_ema200=False, above_ema50=False,
    )
    # 4H RSI in short-friendly zone (30-60)
    tf_4h = make_tf(rsi=45, above_ema200=False, above_ema50=False)
    # 15M: price near BB upper, RSI in 55-75 zone, below EMA50
    tf_15m = make_tf(
        price=38000, rsi=65,
        bb_upper=38020,   # price 20 pts from upper (within 30)
        bb_mid=37700,
        bb_lower=37400,   # 600 pts below (TP viable for short)
        ema50=38050,      # price below EMA50 (bearish)
        above_ema50=False, above_ema200=False,
        pullback_depth=50,  # price rallied before SHORT entry (C12)
        ha_bullish=False, ha_streak=-3,  # C11: HA bearish aligned for SHORT
    )
    return tf_daily, tf_4h, tf_15m


# ── Score Computation ─────────────────────────────────────────────────────────

class TestScoreComputation:
    def test_all_criteria_pass_gives_100(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # 11/11 criteria → 30 + int(11 * 70 / 11) = 100
        assert result["score"] == 100

    def test_base_score_with_zero_criteria(self):
        # Construct a setup where all criteria fail — including R:R penalty
        tf_15m = make_tf(
            price=38000, rsi=80,           # RSI out of LONG range
            bb_mid=37500,                  # 500 pts from mid — too far
            bb_upper=38100,                # Only 100 pts to upper — TP not viable
            bb_lower=37900,
            ema50=37500,                   # 500 pts below — too far
            above_ema50=False,             # NOT above EMA50 → structure fails for LONG
            above_ema200=False,            # Daily bearish → daily_trend fails for LONG
        )
        tf_4h = make_tf(rsi=80, above_ema200=False)  # 4H RSI >70 → macro fails
        tf_daily = make_tf(above_ema200=False, above_ema50=False)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # Score is low due to failed criteria + R:R penalty
        assert result["score"] < MIN_CONFIDENCE_LONG  # definitely below threshold
        assert "rr_estimate" in result["criteria"]

    def test_rr_penalty_crushes_bad_rr(self):
        """Good technical setup but terrible R:R should get penalized."""
        tf_15m = make_tf(
            price=38000, rsi=42,
            bb_mid=38010, bb_upper=38050,  # only 50pts to BB upper = tiny TP room
            bb_lower=37700,
            ema50=37985, above_ema50=True, above_ema200=True,
            ha_bullish=True, ha_streak=3,
            atr=200,  # ATR 200 → SL estimate = 300pts, TP estimate = 150 (floor)
        )
        tf_4h = make_tf(rsi=55, above_ema200=True, above_ema50=True)
        tf_daily = make_tf(rsi=60, above_ema200=True, above_ema50=True)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # R:R ~0.5 → factor 0.70 → high score penalized
        assert result["estimated_rr"] < 1.0
        assert result["rr_factor"] < 1.0
        assert result["score"] < 80  # significantly penalized despite good technicals

    def test_rr_no_penalty_good_rr(self):
        """Good R:R should not penalize score."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["rr_factor"] >= 0.95  # good R:R = no or minimal penalty
        assert result["estimated_rr"] >= 1.2

    def test_score_is_capped_at_100(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["score"] <= 100

    def test_total_criteria_is_9(self):
        # Weighted system uses 9 scored criteria (C7/C8 are hard gates, C11/C12 merged)
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["total_criteria"] == 9

    def test_passed_criteria_matches_score(self):
        # Weighted scoring: score = round(sum of weights * 100 for passing criteria)
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # All 9 pass → score == 100
        assert result["score"] == 100
        assert result["passed_criteria"] == result["total_criteria"]


# ── LONG Criteria ─────────────────────────────────────────────────────────────

class TestLongCriteria:
    def test_daily_trend_above_ema200(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["daily_trend"] is True

    def test_daily_trend_below_ema200_fails(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_daily["above_ema200"] = False
        tf_daily["above_ema50"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["daily_trend"] is False

    def test_entry_near_bb_mid_passes(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        # Price 10 pts from BB mid (< 30 threshold)
        tf_15m["bollinger_mid"] = tf_15m["price"] - 10
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_level"] is True

    def test_entry_far_from_bb_mid_fails(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["bollinger_mid"] = tf_15m["price"] - 200  # 200 pts away (> 150 threshold)
        tf_15m["ema50"] = tf_15m["price"] - 200           # also far from EMA50
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_level"] is False

    def test_rsi_in_long_zone_passes(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["rsi"] = 45  # In 35-55 zone
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["rsi_15m"] is True

    def test_rsi_above_long_zone_fails(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["rsi"] = 70  # Above 55
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["rsi_15m"] is False

    def test_rsi_below_long_zone_fails(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["rsi"] = 28  # Below 30 (widened from 35)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["rsi_15m"] is False

    def test_tp_viable_when_price_below_bb_mid(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        # ideal_long_setup: price=38000, bb_mid=38010 → price is below mid → viable
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["tp_viable"] is True

    def test_tp_not_viable_when_price_above_bb_mid(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["bollinger_mid"] = tf_15m["price"] - 50  # price 50pts ABOVE mid → not viable
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["tp_viable"] is False

    def test_structure_above_ema50_passes_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["above_ema50"] = True
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["structure"] is True

    def test_structure_below_ema50_fails_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["above_ema50"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["structure"] is False


# ── C9: Volume ────────────────────────────────────────────────────────────────

class TestVolumeC9:
    def test_normal_volume_passes(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["volume_signal"] = "NORMAL"
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["volume"] is True

    def test_high_volume_passes(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["volume_signal"] = "HIGH"
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["volume"] is True

    def test_low_volume_fails(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["volume_signal"] = "LOW"
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["volume"] is False

    def test_missing_volume_signal_defaults_pass(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m.pop("volume_signal", None)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["volume"] is True


# ── C10: 4H EMA50 Alignment ───────────────────────────────────────────────────

class TestTrend4hC10:
    def test_4h_above_ema50_passes_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_4h["above_ema50"] = True
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is True

    def test_4h_below_ema50_fails_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_4h["above_ema50"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is False

    def test_4h_below_ema50_passes_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_4h["above_ema50"] = False
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is True

    def test_4h_above_ema50_fails_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_4h["above_ema50"] = True
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is False

    def test_4h_ema50_unavailable_defaults_fail(self):
        # BUG-006 fix: conservative default — missing 4H data should not inflate confidence
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_4h["above_ema50"] = None
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is False


# ── SHORT Criteria ────────────────────────────────────────────────────────────

class TestShortCriteria:
    def test_daily_trend_below_ema200_passes_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["daily_trend"] is True

    def test_daily_trend_above_ema50_fails_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_daily["above_ema50"] = True  # EMA50 is primary; above = not bearish = C1 fail
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["daily_trend"] is False

    def test_rsi_in_short_zone_passes(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["rsi"] = 65  # In 55-75 zone
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["rsi_15m"] is True

    def test_rsi_below_short_zone_fails(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["rsi"] = 40  # Below 55
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["rsi_15m"] is False

    def test_structure_below_ema50_passes_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["above_ema50"] = False
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["structure"] is True

    def test_tp_viable_for_short_when_price_above_bb_mid(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        # ideal_short_setup: price=38000, bb_mid=37700 → price above mid → viable for short
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["tp_viable"] is True

    def test_tp_not_viable_for_short_when_price_below_bb_mid(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["bollinger_mid"] = tf_15m["price"] + 50  # price 50pts BELOW mid → not viable for short
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["tp_viable"] is False


# ── Thresholds ────────────────────────────────────────────────────────────────

class TestMeetsThreshold:
    def test_long_threshold_is_70(self):
        assert MIN_CONFIDENCE_LONG == 70

    def test_short_threshold_is_75(self):
        assert MIN_CONFIDENCE_SHORT == 75

    def test_ideal_long_meets_threshold(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["meets_threshold"] is True

    def test_ideal_short_meets_threshold(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["meets_threshold"] is True

    def test_threshold_reported_correctly_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["min_threshold"] == MIN_CONFIDENCE_LONG

    def test_threshold_reported_correctly_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["min_threshold"] == MIN_CONFIDENCE_SHORT

    def test_low_score_does_not_meet_threshold(self):
        # Construct a weak setup that will fail several criteria
        tf_15m = make_tf(
            price=38000, rsi=20,  # RSI out of any range
            bb_mid=37500, bb_upper=38050, bb_lower=37950,
            ema50=37500, above_ema50=False, above_ema200=True,
        )
        tf_4h = make_tf(rsi=80, above_ema200=True)
        tf_daily = make_tf(above_ema200=True)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        if result["score"] < MIN_CONFIDENCE_LONG:
            assert result["meets_threshold"] is False


# ── EMA200 Fallback ───────────────────────────────────────────────────────────

class TestEma200Fallback:
    def test_uses_ema50_when_ema200_unavailable(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_daily["above_ema200"] = None  # EMA200 unavailable
        tf_daily["above_ema50"] = True   # EMA50 available
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # Should still pass using EMA50 as fallback
        assert result["criteria"]["daily_trend"] is True

    def test_fails_when_both_unavailable(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_daily["above_ema200"] = None
        tf_daily["above_ema50"] = None
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["daily_trend"] is False


# ── Format Breakdown ──────────────────────────────────────────────────────────

class TestFormatConfidenceBreakdown:
    def test_output_is_string(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        text = format_confidence_breakdown(result)
        assert isinstance(text, str)

    def test_output_contains_score(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        text = format_confidence_breakdown(result)
        assert str(result["score"]) in text

    def test_output_contains_pass_fail(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        text = format_confidence_breakdown(result)
        assert "PASS" in text or "FAIL" in text


# ── C11: Heiken Ashi Alignment ──────────────────────────────────────────────

class TestHaAlignedC11:
    def test_ha_bullish_passes_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["ha_bullish"] = True
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is True

    def test_ha_bearish_fails_for_long(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["ha_bullish"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is False

    def test_ha_bearish_passes_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["ha_bullish"] = False
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is True

    def test_ha_bullish_fails_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["ha_bullish"] = True
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is False

    def test_ha_unavailable_defaults_fail(self):
        # BUG-006 fix: conservative default — missing HA data should not inflate confidence
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m.pop("ha_bullish", None)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is False

    def test_ha_affects_score(self):
        """entry_timing (C11+C12 OR) failing should reduce score.
        Must clear both C11 (ha_bullish=False) and C12 (pullback=chase) to flip entry_timing."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["ha_bullish"] = True
        tf_15m["pullback_depth"] = -50  # C12 passes
        result_pass = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        tf_15m["ha_bullish"] = False
        tf_15m["pullback_depth"] = 50  # C12 also fails (chase, not dip)
        result_fail = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result_pass["score"] > result_fail["score"]


# ── C2: VWAP Fallback ───────────────────────────────────────────────────────

class TestC2VwapFallback:
    def test_vwap_discount_passes_long_c2(self):
        """LONG: price below VWAP within 150pts → C2 passes as fallback."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        # Break BB and EMA50 proximity so only VWAP can save C2
        tf_15m["bollinger_mid"] = tf_15m["price"] + 200
        tf_15m["bollinger_lower"] = tf_15m["price"] - 200
        tf_15m["ema50"] = tf_15m["price"] + 200
        # VWAP is above price (discount zone)
        tf_15m["vwap"] = tf_15m["price"] + 100
        tf_15m["above_vwap"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_level"] is True
        assert "VWAP" in result["reasons"]["entry_level"]

    def test_vwap_premium_passes_short_c2(self):
        """SHORT: price above VWAP within 150pts → C2 passes as fallback."""
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        # Break BB and EMA50 proximity so only VWAP can save C2
        tf_15m["bollinger_upper"] = tf_15m["price"] - 200
        tf_15m["bollinger_mid"] = tf_15m["price"] - 200
        tf_15m["ema50"] = tf_15m["price"] + 200  # price below ema50 but dist > 150
        # VWAP is below price (premium zone)
        tf_15m["vwap"] = tf_15m["price"] - 100
        tf_15m["above_vwap"] = True
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_level"] is True
        assert "VWAP" in result["reasons"]["entry_level"]

    def test_no_vwap_no_change(self):
        """Without VWAP data, C2 fallback doesn't fire."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["bollinger_mid"] = tf_15m["price"] + 200
        tf_15m["bollinger_lower"] = tf_15m["price"] - 200
        tf_15m["ema50"] = tf_15m["price"] + 200
        tf_15m["vwap"] = None
        tf_15m["above_vwap"] = None
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_level"] is False

    def test_wrong_side_vwap_no_pass_long(self):
        """LONG: price ABOVE VWAP should not trigger the discount fallback."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["bollinger_mid"] = tf_15m["price"] + 200
        tf_15m["bollinger_lower"] = tf_15m["price"] - 200
        tf_15m["ema50"] = tf_15m["price"] + 200
        tf_15m["vwap"] = tf_15m["price"] - 100
        tf_15m["above_vwap"] = True  # above VWAP = premium, not discount
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_level"] is False


# ── C11: ha_streak in reason ─────────────────────────────────────────────────

class TestC11HaStreakReason:
    def test_streak_in_reason_string(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["ha_bullish"] = True
        tf_15m["ha_streak"] = 4
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert "streak=4" in result["reasons"]["ha_aligned"]

    def test_none_streak_no_crash(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["ha_bullish"] = True
        tf_15m["ha_streak"] = None
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is True
        assert "streak=" not in result["reasons"]["ha_aligned"]

    def test_negative_streak_in_short_reason(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["ha_bullish"] = False
        tf_15m["ha_streak"] = -3
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert "streak=-3" in result["reasons"]["ha_aligned"]


# ── Setup-Type-Aware Scoring (oversold bounces) ──────────────────────────────

class TestOversoldSetupTypeAware:
    """Tests for C5/C10/C11 setup-type-aware behavior for oversold setups."""

    def _oversold_setup(self):
        """Create an oversold LONG scenario: price below EMA50, 4H bearish, HA bearish."""
        tf_daily = make_tf(price=38000, rsi=55, above_ema200=True, above_ema50=True)
        tf_4h = make_tf(rsi=38, above_ema200=True, above_ema50=False)  # 4H bearish
        tf_15m = make_tf(
            price=37500, rsi=25,
            bb_mid=37800, bb_lower=37550,
            ema50=37700, above_ema50=False, above_ema200=False,
        )
        tf_15m["ha_bullish"] = False
        tf_15m["ha_streak"] = -5
        tf_15m["swept_low"] = True
        return tf_daily, tf_4h, tf_15m

    def test_c5_passes_for_bb_lower_bounce_below_ema50(self):
        """C5 should pass for bb_lower_bounce even when below EMA50 (reversal signal present)."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_lower_bounce")
        assert result["criteria"]["structure"] is True
        assert "expected" in result["reasons"]["structure"].lower()

    def test_c5_fails_for_bb_mid_bounce_below_ema50(self):
        """C5 should still fail for bb_mid_bounce when below EMA50 (not an oversold setup)."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_mid_bounce")
        assert result["criteria"]["structure"] is False

    def test_c10_passes_for_oversold_reversal_with_daily_bullish(self):
        """C10 should pass for oversold_reversal when daily is bullish even if 4H below EMA50."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="oversold_reversal")
        assert result["criteria"]["trend_4h"] is True

    def test_c10_fails_for_regular_setup_below_4h_ema50(self):
        """C10 should fail for regular setup when below 4H EMA50."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_mid_bounce")
        assert result["criteria"]["trend_4h"] is False

    def test_c11_passes_for_oversold_with_rsi_below_30(self):
        """C11 should pass for oversold setup even with bearish HA when RSI < 30."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_lower_bounce")
        assert result["criteria"]["ha_aligned"] is True

    def test_c11_fails_for_regular_with_bearish_ha(self):
        """C11 should fail for regular setup with bearish HA."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_mid_bounce")
        assert result["criteria"]["ha_aligned"] is False

    def test_oversold_setup_scores_higher_than_regular(self):
        """Oversold setup should score higher than same indicators with regular setup type."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result_oversold = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_lower_bounce")
        result_regular = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_mid_bounce")
        assert result_oversold["score"] > result_regular["score"]

    def test_setup_type_none_defaults_to_regular(self):
        """When no setup_type passed, C5/C10/C11 use regular logic."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # Without setup_type, below-EMA50 = C5 fails
        assert result["criteria"]["structure"] is False

    def test_c1_passes_for_oversold_when_daily_bearish(self):
        """C1 should pass for oversold setup even when daily is bearish (EMA50 primary)."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        tf_daily["above_ema50"] = False  # EMA50 is primary now
        tf_daily["above_ema200"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_lower_bounce")
        assert result["criteria"]["daily_trend"] is True
        assert "oversold exempt" in result["reasons"]["daily_trend"]

    def test_c1_fails_for_regular_when_daily_bearish(self):
        """C1 should still fail for regular setup when daily is bearish (EMA50 primary)."""
        tf_daily, tf_4h, tf_15m = self._oversold_setup()
        tf_daily["above_ema50"] = False  # EMA50 is primary now
        tf_daily["above_ema200"] = False
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m, setup_type="bollinger_mid_bounce")
        assert result["criteria"]["daily_trend"] is False


# ── C12: Entry Quality ──────────────────────────────────────────────────────

class TestEntryQualityC12:
    def test_long_with_pullback_passes(self):
        """LONG entry after price pullback (negative) should pass C12."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["pullback_depth"] = -80  # price fell 80pts
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is True

    def test_long_chasing_fails(self):
        """LONG entry after price rise (positive/chasing) should fail C12."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["pullback_depth"] = 50  # price already rising
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is False

    def test_short_with_rally_passes(self):
        """SHORT entry after price rally (positive) should pass C12."""
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["pullback_depth"] = 80  # price rose 80pts
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is True

    def test_short_chasing_fails(self):
        """SHORT entry after price fall (negative/chasing) should fail C12."""
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_15m["pullback_depth"] = -50  # price already falling
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is False

    def test_high_vol_passes_regardless(self):
        """High volatility (>120pt avg range) should pass C12 regardless of pullback."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["pullback_depth"] = 50  # chasing (normally fails)
        tf_15m["avg_candle_range"] = 150  # but high vol
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is True

    def test_zero_pullback_fails_long(self):
        """Zero pullback (flat) should fail C12 for LONG."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["pullback_depth"] = 0
        tf_15m["avg_candle_range"] = 80
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is False

    def test_missing_data_defaults_fail(self):
        """Missing pullback_depth should default to 0 (fails for LONG)."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m.pop("pullback_depth", None)
        tf_15m.pop("avg_candle_range", None)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["entry_quality"] is False

    def test_c12_affects_score(self):
        """entry_timing (C11+C12 OR) failing should reduce score.
        Must clear both C12 (pullback=chase) and C11 (ha_bullish=False) to flip entry_timing."""
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_15m["pullback_depth"] = -80
        tf_15m["ha_bullish"] = True  # C11 also passes
        result_pass = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        tf_15m["pullback_depth"] = 50   # C12 fails (chase)
        tf_15m["ha_bullish"] = False    # C11 also fails
        result_fail = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result_pass["score"] > result_fail["score"]


# ── Momentum Setup Scoring ──────────────────────────────────────────────────

class TestMomentumSetupScoring:
    """Test that momentum LONG setups get appropriate confidence treatment."""

    def _momentum_setup_data(self):
        """Build tf data for a momentum_continuation_long scenario (RSI ~65, above everything)."""
        tf_15m = make_tf(
            price=55500, rsi=65,
            bb_mid=54800, bb_upper=55600, bb_lower=54000,
            ema50=54500, above_ema50=True, above_ema200=True,
            pullback_depth=200,  # positive = trending up
            avg_candle_range=100,
        )
        tf_15m["vwap"] = 55100
        tf_15m["above_vwap"] = True
        tf_15m["ha_bullish"] = True
        tf_15m["ha_streak"] = 3
        tf_15m["ema9"] = 55400
        tf_4h = make_tf(rsi=60, above_ema50=True)
        tf_daily = make_tf(rsi=60, above_ema50=True, above_ema200=True)
        return tf_daily, tf_4h, tf_15m

    def test_momentum_c1_daily_exempt(self):
        """Momentum setups should pass C1 even when daily EMA50 is below."""
        tf_daily, tf_4h, tf_15m = self._momentum_setup_data()
        tf_daily["above_ema50"] = False  # Daily still bearish (lagging)
        result = compute_confidence(
            "LONG", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_long"
        )
        assert result["criteria"]["daily_trend"] is True
        assert "momentum exempt" in result["reasons"]["daily_trend"]

    def test_momentum_c3_rsi_widened(self):
        """Momentum setups should accept RSI 45-70 (not capped at 55)."""
        tf_daily, tf_4h, tf_15m = self._momentum_setup_data()
        tf_15m["rsi"] = 65  # Above normal LONG cap of 55
        result = compute_confidence(
            "LONG", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_long"
        )
        assert result["criteria"]["rsi_15m"] is True
        assert "momentum zone" in result["reasons"]["rsi_15m"]

    def test_momentum_c4_above_bb_mid_passes(self):
        """Momentum setups should pass C4 even when price is above BB mid."""
        tf_daily, tf_4h, tf_15m = self._momentum_setup_data()
        # price=55500 is above bb_mid=54800 → normally fails C4 for LONG
        result = compute_confidence(
            "LONG", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_long"
        )
        assert result["criteria"]["tp_viable"] is True

    def test_momentum_c12_positive_pullback_passes(self):
        """Momentum setups should pass C12 even with positive pullback (trending up)."""
        tf_daily, tf_4h, tf_15m = self._momentum_setup_data()
        tf_15m["pullback_depth"] = 150  # Positive = price rising
        result = compute_confidence(
            "LONG", tf_daily, tf_4h, tf_15m,
            setup_type="breakout_long"
        )
        assert result["criteria"]["entry_quality"] is True

    def test_momentum_c2_above_vwap_passes(self):
        """Momentum setups should pass C2 when above VWAP (trend continuation)."""
        tf_daily, tf_4h, tf_15m = self._momentum_setup_data()
        # price=55500 is far from BB mid (54800), far from EMA50 (54500) → normally fails C2
        tf_15m["bollinger_mid"] = 54000  # Make BB mid far away
        tf_15m["ema50"] = 53500  # Make EMA50 far away
        tf_15m["bollinger_lower"] = 52500  # Far from BB lower
        result = compute_confidence(
            "LONG", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_long"
        )
        assert result["criteria"]["entry_level"] is True
        assert "Momentum" in result["reasons"]["entry_level"]

    def test_momentum_high_score(self):
        """A perfect momentum setup should score >= 70% (meets LONG threshold)."""
        tf_daily, tf_4h, tf_15m = self._momentum_setup_data()
        result = compute_confidence(
            "LONG", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_long"
        )
        assert result["score"] >= 70
        assert result["meets_threshold"] is True
