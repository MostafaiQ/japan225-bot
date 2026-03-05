"""
Tests for the Opus opposite-direction gate logic and evaluate_opposite() robustness.

Covers:
  - _normal_gate: requires opposite_found + conf>=60 + (sonnet_conf>=30 OR parse_error)
  - _counter_gate: counter_signal==opposite_dir + sonnet_conf<=45, NO conf required
  - evaluate_opposite(): must not crash when opposite_local_conf=None (counter-signal trigger)
"""
import pytest
from unittest.mock import MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def make_final_result(found=False, confidence=35, counter_signal=None):
    return {
        "found": found,
        "confidence": confidence,
        "counter_signal": counter_signal,
        "reasoning": "test",
        "key_levels": {"support": [], "resistance": []},
    }


def make_conf(score=65):
    return {
        "score": score,
        "passed_criteria": 8,
        "total_criteria": 12,
        "criteria": {"C1": True, "C2": False},
    }


def make_setup(found=True):
    return {"found": found, "setup_type": "breakdown_continuation", "indicators_snapshot": {}}


def _compute_gates(final_result, opposite_conf, opposite_setup):
    """Mirrors the gate logic in monitor.py _scanning_cycle."""
    _sonnet_conf_score = final_result.get("confidence", 0)
    _counter_signal = final_result.get("counter_signal")
    _sonnet_parse_error = _sonnet_conf_score == 0 and not final_result.get("found", False)

    _normal_gate = (
        opposite_conf is not None
        and opposite_conf.get("score", 0) >= 60
        and opposite_setup.get("found", False)
        and (_sonnet_conf_score >= 30 or _sonnet_parse_error)
    )
    _counter_gate = (
        _counter_signal is not None
        and _counter_signal == "LONG"   # opposite_dir fixed to LONG for these tests
        and _sonnet_conf_score <= 45
    )
    return _normal_gate, _counter_gate


# ── normal gate ───────────────────────────────────────────────────────────────

def test_normal_gate_passes_when_all_conditions_met():
    fr = make_final_result(found=False, confidence=35)
    normal, counter = _compute_gates(fr, make_conf(65), make_setup(True))
    assert normal is True
    assert counter is False


def test_normal_gate_fails_when_sonnet_conf_too_low():
    fr = make_final_result(found=False, confidence=20)
    normal, counter = _compute_gates(fr, make_conf(65), make_setup(True))
    assert normal is False


def test_normal_gate_passes_on_parse_error_with_valid_opposite():
    """sonnet_conf=0 + found=False = parse error → normal gate should still open."""
    fr = make_final_result(found=False, confidence=0)
    normal, counter = _compute_gates(fr, make_conf(65), make_setup(True))
    assert normal is True


def test_normal_gate_fails_when_opposite_conf_below_60():
    fr = make_final_result(found=False, confidence=35)
    normal, counter = _compute_gates(fr, make_conf(55), make_setup(True))
    assert normal is False


def test_normal_gate_fails_when_opposite_not_found():
    fr = make_final_result(found=False, confidence=35)
    normal, counter = _compute_gates(fr, make_conf(65), make_setup(False))
    assert normal is False


def test_normal_gate_fails_when_opposite_conf_is_none():
    fr = make_final_result(found=False, confidence=35)
    normal, counter = _compute_gates(fr, None, make_setup(True))
    assert normal is False


# ── counter gate ──────────────────────────────────────────────────────────────

def test_counter_gate_fires_without_pre_detected_opposite():
    """Core scenario: Sonnet sees LONG reversal during SHORT eval, no LONG pre-detected."""
    fr = make_final_result(found=False, confidence=35, counter_signal="LONG")
    normal, counter = _compute_gates(fr, None, make_setup(False))
    assert normal is False
    assert counter is True


def test_counter_gate_fires_when_sonnet_conf_at_boundary_45():
    fr = make_final_result(found=False, confidence=45, counter_signal="LONG")
    normal, counter = _compute_gates(fr, None, make_setup(False))
    assert counter is True


def test_counter_gate_fails_when_sonnet_conf_above_45():
    fr = make_final_result(found=False, confidence=50, counter_signal="LONG")
    normal, counter = _compute_gates(fr, None, make_setup(False))
    assert counter is False


def test_counter_gate_fails_when_wrong_direction():
    """counter_signal=SHORT but opposite_dir is LONG → no gate."""
    fr = make_final_result(found=False, confidence=35, counter_signal="SHORT")
    normal, counter = _compute_gates(fr, None, make_setup(False))
    assert counter is False


def test_counter_gate_fails_when_no_counter_signal():
    fr = make_final_result(found=False, confidence=35, counter_signal=None)
    normal, counter = _compute_gates(fr, None, make_setup(False))
    assert counter is False


# ── evaluate_opposite robustness ──────────────────────────────────────────────

def test_evaluate_opposite_does_not_crash_with_none_local_conf():
    """Counter-signal trigger passes opposite_local_conf=None — must not raise."""
    from ai.analyzer import AIAnalyzer

    analyzer = AIAnalyzer.__new__(AIAnalyzer)
    # Minimal indicators dict
    indicators = {
        "tf_15m": {"price": 54000, "rsi": 38, "bollinger_mid": 54200, "bollinger_upper": 54500,
                   "bollinger_lower": 53700, "ema50": 54100, "ema200": 55000,
                   "above_ema50": False, "above_ema200": False, "volume_signal": "NORMAL",
                   "ha_color": "bear", "ha_streak": -5, "atr14": 100,
                   "vwap": 54300, "prev_candle_high": 54400, "prev_candle_low": 53800,
                   "anchor_date": None},
        "tf_5m": {"price": 54000, "rsi": 37, "bollinger_mid": 54100, "bollinger_upper": 54400,
                  "bollinger_lower": 53700, "ema50": 54050, "ema200": 55000,
                  "above_ema50": False, "above_ema200": False, "volume_signal": "LOW",
                  "ha_color": "bear", "ha_streak": -3, "atr14": 80,
                  "vwap": 54200, "prev_candle_high": 54200, "prev_candle_low": 53900,
                  "anchor_date": None},
        "tf_4h": {"price": 54000, "rsi": 41, "bollinger_mid": 55000, "bollinger_upper": 56000,
                  "bollinger_lower": 54000, "ema50": 56800, "ema200": 57000,
                  "above_ema50": False, "above_ema200": False, "volume_signal": "NORMAL",
                  "ha_color": "bear", "ha_streak": -3, "atr14": 200,
                  "vwap": 55000, "prev_candle_high": 55000, "prev_candle_low": 53500,
                  "anchor_date": None},
        "tf_daily": {"price": 54000, "rsi": 44, "bollinger_mid": 56000, "bollinger_upper": 58000,
                     "bollinger_lower": 54000, "ema50": 55200, "ema200": 55000,
                     "above_ema50": False, "above_ema200": False, "volume_signal": "HIGH",
                     "ha_color": "bear", "ha_streak": -4, "atr14": 500,
                     "vwap": 56000, "prev_candle_high": 56000, "prev_candle_low": 53000,
                     "anchor_date": None},
        "indicators_snapshot": {},
    }

    # Patch the actual Claude CLI call so no subprocess is spawned
    fake_response = (
        '{"setup_found": false, "direction": "LONG", "confidence": 40, '
        '"entry": 54000, "stop_loss": 53500, "take_profit": 55000, '
        '"setup_type": "bounce", "reasoning": "test", '
        '"effective_rr": 2.0, "warnings": [], "edge_factors": []}'
    )
    with patch.object(analyzer, "_run_claude", return_value=(fake_response, {})):
        # Should not raise AttributeError for None.get()
        result = analyzer.evaluate_opposite(
            indicators=indicators,
            opposite_direction="LONG",
            opposite_local_conf=None,   # ← counter-signal path
            sonnet_rejection_reasoning="Bearish structure, counter sweep identified.",
            sonnet_key_levels={"support": [53500], "resistance": [55000]},
            recent_scans=[],
            market_context={},
            web_research={},
        )
    assert isinstance(result, dict)
    assert "setup_found" in result


def test_evaluate_opposite_works_normally_with_valid_local_conf():
    """Normal path: opposite_local_conf is a proper dict."""
    from ai.analyzer import AIAnalyzer

    analyzer = AIAnalyzer.__new__(AIAnalyzer)
    indicators = {
        "tf_15m": {"price": 54000, "rsi": 38, "bollinger_mid": 54200, "bollinger_upper": 54500,
                   "bollinger_lower": 53700, "ema50": 54100, "ema200": 55000,
                   "above_ema50": False, "above_ema200": False, "volume_signal": "NORMAL",
                   "ha_color": "bear", "ha_streak": -5, "atr14": 100,
                   "vwap": 54300, "prev_candle_high": 54400, "prev_candle_low": 53800,
                   "anchor_date": None},
        "tf_5m": {"price": 54000, "rsi": 37, "bollinger_mid": 54100, "bollinger_upper": 54400,
                  "bollinger_lower": 53700, "ema50": 54050, "ema200": 55000,
                  "above_ema50": False, "above_ema200": False, "volume_signal": "LOW",
                  "ha_color": "bear", "ha_streak": -3, "atr14": 80,
                  "vwap": 54200, "prev_candle_high": 54200, "prev_candle_low": 53900,
                  "anchor_date": None},
        "tf_4h": {"price": 54000, "rsi": 41, "bollinger_mid": 55000, "bollinger_upper": 56000,
                  "bollinger_lower": 54000, "ema50": 56800, "ema200": 57000,
                  "above_ema50": False, "above_ema200": False, "volume_signal": "NORMAL",
                  "ha_color": "bear", "ha_streak": -3, "atr14": 200,
                  "vwap": 55000, "prev_candle_high": 55000, "prev_candle_low": 53500,
                  "anchor_date": None},
        "tf_daily": {"price": 54000, "rsi": 44, "bollinger_mid": 56000, "bollinger_upper": 58000,
                     "bollinger_lower": 54000, "ema50": 55200, "ema200": 55000,
                     "above_ema50": False, "above_ema200": False, "volume_signal": "HIGH",
                     "ha_color": "bear", "ha_streak": -4, "atr14": 500,
                     "vwap": 56000, "prev_candle_high": 56000, "prev_candle_low": 53000,
                     "anchor_date": None},
        "indicators_snapshot": {},
    }

    fake_response = (
        '{"setup_found": true, "direction": "LONG", "confidence": 75, '
        '"entry": 54000, "stop_loss": 53500, "take_profit": 55500, '
        '"setup_type": "bounce", "reasoning": "strong bounce", '
        '"effective_rr": 2.5, "warnings": [], "edge_factors": ["swept_low"]}'
    )
    with patch.object(analyzer, "_run_claude", return_value=(fake_response, {})):
        result = analyzer.evaluate_opposite(
            indicators=indicators,
            opposite_direction="LONG",
            opposite_local_conf=make_conf(65),
            sonnet_rejection_reasoning="Bearish structure.",
            sonnet_key_levels={"support": [53500], "resistance": [55000]},
            recent_scans=[],
            market_context={},
            web_research={},
        )
    assert result.get("setup_found") is True
    assert result.get("confidence") == 75
