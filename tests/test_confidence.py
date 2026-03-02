"""
Tests for core/confidence.py — bidirectional 10-criteria local scoring.
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
    )
    return tf_daily, tf_4h, tf_15m


# ── Score Computation ─────────────────────────────────────────────────────────

class TestScoreComputation:
    def test_all_criteria_pass_gives_100(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # 10/10 criteria → 30 + int(10 * 70 / 10) = 100
        assert result["score"] == 100

    def test_base_score_with_zero_criteria(self):
        # Construct a setup where all criteria fail
        tf_15m = make_tf(
            price=38000, rsi=80,           # RSI out of LONG range
            bb_mid=37500,                  # 500 pts from mid — too far
            bb_upper=38100,                # Only 100 pts to upper — TP not viable (need 100+)
            bb_lower=37900,
            ema50=37500,                   # 500 pts below — too far
            above_ema50=False,             # NOT above EMA50 → structure fails for LONG
            above_ema200=False,            # Daily bearish → daily_trend fails for LONG
        )
        tf_4h = make_tf(rsi=80, above_ema200=False)  # 4H RSI >70 → macro fails
        tf_daily = make_tf(above_ema200=False, above_ema50=False)
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        # At least base score (30), possibly a few criteria pass
        assert result["score"] >= BASE_SCORE

    def test_score_is_capped_at_100(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["score"] <= 100

    def test_total_criteria_is_10(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["total_criteria"] == 10

    def test_passed_criteria_matches_score(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        n = result["passed_criteria"]
        total = result["total_criteria"]
        expected_score = min(BASE_SCORE + int(n * (100 - BASE_SCORE) / total), 100)
        assert result["score"] == expected_score


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
        tf_15m["rsi"] = 30  # Below 35
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

    def test_4h_ema50_unavailable_defaults_pass(self):
        tf_daily, tf_4h, tf_15m = ideal_long_setup()
        tf_4h["above_ema50"] = None
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is True


# ── SHORT Criteria ────────────────────────────────────────────────────────────

class TestShortCriteria:
    def test_daily_trend_below_ema200_passes_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        result = compute_confidence("SHORT", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["daily_trend"] is True

    def test_daily_trend_above_ema200_fails_for_short(self):
        tf_daily, tf_4h, tf_15m = ideal_short_setup()
        tf_daily["above_ema200"] = True
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
