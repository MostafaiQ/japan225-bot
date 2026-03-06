"""
Tests for Tokyo ATR calculation and prompt formatting.

Covers:
  1. compute_atr() — pure math, all edge cases
  2. analyze_timeframe() includes atr key
  3. ATR in analyze_timeframe with malformed candles
  4. ATR in AI prompt formatting
"""
import pytest

from core.indicators import compute_atr, analyze_timeframe
from config.settings import ATR_PERIOD


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_candle(high, low, close, open_=None, volume=1000):
    """Build a minimal candle dict."""
    return {
        "open":   open_ if open_ is not None else (high + low) / 2,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
        "timestamp": "2026-03-04T00:00:00",
    }


def _flat_candles(n, price=55000, range_=100):
    """n identical candles with known H-L range (ATR = range_)."""
    return [_make_candle(price + range_, price, price) for _ in range(n)]


# ══════════════════════════════════════════════════════════════════════════════
# 1. compute_atr() — pure math
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeAtr:

    def test_insufficient_data_returns_zero(self):
        """Fewer than period+1 candles → 0.0."""
        candles = _flat_candles(14)  # need 15 for ATR(14)
        assert compute_atr(candles, period=14) == 0.0

    def test_exact_minimum_data(self):
        """Exactly period+1 candles → valid result."""
        candles = _flat_candles(15, range_=100)
        result = compute_atr(candles, period=14)
        assert result > 0.0

    def test_empty_list_returns_zero(self):
        assert compute_atr([], period=14) == 0.0

    def test_single_candle_returns_zero(self):
        assert compute_atr([_make_candle(55100, 55000, 55050)], period=14) == 0.0

    def test_flat_market_atr_equals_range(self):
        """All candles same H/L → ATR = H-L exactly."""
        candles = _flat_candles(20, price=55000, range_=120)
        atr = compute_atr(candles, period=14)
        assert abs(atr - 120.0) < 1.0, f"Expected ~120, got {atr}"

    def test_volatile_market_larger_atr(self):
        """Candles with 300pt range → ATR ≈ 300."""
        candles = _flat_candles(20, range_=300)
        atr = compute_atr(candles, period=14)
        assert abs(atr - 300.0) < 1.0

    def test_calm_market_smaller_atr(self):
        """Candles with 50pt range → ATR ≈ 50."""
        candles = _flat_candles(20, range_=50)
        atr = compute_atr(candles, period=14)
        assert abs(atr - 50.0) < 1.0

    def test_period_1_uses_last_candle(self):
        """ATR(1) = TR of most recent candle."""
        candles = _flat_candles(5, range_=100)
        atr = compute_atr(candles, period=1)
        assert abs(atr - 100.0) < 1.0

    def test_uses_only_last_period_candles(self):
        """ATR uses last N candles, not all history."""
        # First 10: range=50, last 15: range=200. ATR(14) should be ~200.
        calm = _flat_candles(10, range_=50)
        volatile = _flat_candles(15, range_=200)
        candles = calm + volatile
        atr = compute_atr(candles, period=14)
        assert atr > 150.0, f"Expected ATR driven by volatile candles, got {atr}"

    def test_zero_values_in_candle_skipped_gracefully(self):
        """Malformed candles with zero H/L/C are skipped without crash."""
        bad = _make_candle(0, 0, 0)
        good = _flat_candles(20, range_=100)
        candles = good[:10] + [bad] + good[10:]
        result = compute_atr(candles, period=14)
        # Should still compute something or return 0 — must not raise
        assert isinstance(result, float)

    def test_returns_float(self):
        candles = _flat_candles(20, range_=150)
        assert isinstance(compute_atr(candles), float)

    def test_default_period_is_14(self):
        """compute_atr() without period arg uses 14."""
        candles = _flat_candles(20, range_=100)
        assert compute_atr(candles) == compute_atr(candles, period=14)

    def test_large_atr_tokyo_realistic(self):
        """Simulate Tokyo-level volatility: 180pt candles → ATR ~180."""
        candles = _flat_candles(20, range_=180)
        atr = compute_atr(candles, period=14)
        assert 175 <= atr <= 185, f"Expected Tokyo-level ATR ~180, got {atr}"

    def test_true_range_uses_prev_close(self):
        """
        TR = max(H-L, |H-PC|, |L-PC|). A gap candle has TR > H-L.
        Candle: prev_close=55200, H=55100, L=55000. TR = max(100, |55100-55200|, |55000-55200|) = 200.
        """
        prev = _make_candle(high=55300, low=55200, close=55200)
        gap  = _make_candle(high=55100, low=55000, close=55050)
        # Fill with 14 more candles to satisfy period
        padding = [_make_candle(55100, 55000, 55050)] * 14
        candles = [prev, gap] + padding
        atr = compute_atr(candles, period=1)
        # ATR(1) = TR of last candle = max(100, |55100-55050|, |55000-55050|) = 100 (padding candles)
        # Let's test with exactly [prev, gap] and period=1 → TR of gap candle
        atr_gap = compute_atr([prev, gap], period=1)
        assert atr_gap == 200.0, f"Expected gap TR=200, got {atr_gap}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. analyze_timeframe() includes ATR
# ══════════════════════════════════════════════════════════════════════════════

def _realistic_candles(n=50, base=55000):
    """Realistic candles with varying ranges for analyze_timeframe."""
    import random
    random.seed(42)
    candles = []
    price = base
    for i in range(n):
        move = random.uniform(-100, 100)
        range_ = random.uniform(80, 200)
        h = price + range_ / 2
        l = price - range_ / 2
        c = price + move * 0.3
        candles.append(_make_candle(h, l, c, open_=price))
        price = c
    return candles


class TestAnalyzeTimeframeAtr:

    def test_atr_key_present_in_output(self):
        candles = _realistic_candles(50)
        result = analyze_timeframe(candles)
        assert "atr" in result, "analyze_timeframe() must include 'atr' key"

    def test_atr_positive_with_sufficient_data(self):
        candles = _realistic_candles(50)
        result = analyze_timeframe(candles)
        assert result["atr"] > 0.0, f"Expected ATR > 0 with 50 candles, got {result['atr']}"

    def test_atr_zero_with_insufficient_data(self):
        """Fewer than 15 candles → compute_atr returns 0.0 (analyze_timeframe needs 20+ for BB).
        Test compute_atr directly since analyze_timeframe always has enough data when it runs."""
        from core.indicators import compute_atr
        candles = _realistic_candles(10)
        assert compute_atr(candles, period=14) == 0.0

    def test_atr_is_float(self):
        candles = _realistic_candles(30)
        result = analyze_timeframe(candles)
        assert isinstance(result["atr"], float)

    def test_atr_realistic_range_for_nikkei(self):
        """With Nikkei-like candles (80-200pt range), ATR should be in that range."""
        candles = _realistic_candles(50)
        result = analyze_timeframe(candles)
        # Realistic Nikkei 15M ATR: 60-300pts
        assert 20.0 < result["atr"] < 500.0, f"Unrealistic ATR: {result['atr']}"

    def test_atr_not_affected_by_missing_volume(self):
        """ATR only uses H/L/C — missing volume doesn't break it."""
        candles = _realistic_candles(30)
        for c in candles:
            c.pop("volume", None)
        result = analyze_timeframe(candles)
        assert "atr" in result
        assert isinstance(result["atr"], float)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Settings constants
# ══════════════════════════════════════════════════════════════════════════════

class TestSettingsConstants:

    def test_atr_period(self):
        assert ATR_PERIOD == 14


# ══════════════════════════════════════════════════════════════════════════════
# 4. ATR in analyzer prompt formatting
# ══════════════════════════════════════════════════════════════════════════════

class TestAtrInPrompt:

    def test_atr_appears_in_formatted_indicators(self):
        """_fmt_indicators formats ATR14 into the prompt string."""
        from ai.analyzer import _fmt_indicators

        tf_with_atr = {
            "price": 55000,
            "rsi": 45.0,
            "ema50": 54800,
            "ema200": 54000,
            "bb_width": 400.0,
            "atr": 185.0,
        }
        indicators = {"15m": tf_with_atr}
        formatted = _fmt_indicators(indicators)
        assert "ATR14=185pts" in formatted, \
            f"Expected 'ATR14=185pts' in formatted indicators, got:\n{formatted}"

    def test_atr_zero_not_shown(self):
        """ATR=0 (no data) is not shown in prompt (would confuse AI)."""
        from ai.analyzer import _fmt_indicators
        tf_no_atr = {
            "price": 55000,
            "rsi": 45.0,
            "atr": 0.0,
        }
        indicators = {"15m": tf_no_atr}
        formatted = _fmt_indicators(indicators)
        assert "ATR14=0" not in formatted, \
            "ATR=0 should not appear in formatted indicators"

    def test_atr_missing_not_shown(self):
        """If ATR key missing entirely, no crash."""
        from ai.analyzer import _fmt_indicators
        tf_no_atr = {"price": 55000, "rsi": 45.0}
        indicators = {"15m": tf_no_atr}
        formatted = _fmt_indicators(indicators)
        assert "ATR14" not in formatted

    def test_atr_realistic_value_formatted(self):
        """ATR=142.5 → formatted as 'ATR14=142pts' (rounded)."""
        from ai.analyzer import _fmt_indicators
        indicators = {"15m": {"price": 55000, "atr": 142.5}}
        formatted = _fmt_indicators(indicators)
        assert "ATR14=142pts" in formatted or "ATR14=143pts" in formatted
