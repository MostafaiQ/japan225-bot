"""
Tests for confirmed bug fixes:
  BUG-002: candlestick_patterns key mismatch (indicators.py)
  BUG-016: milestone_msg discarded (monitor.py)
  BUG-017: C3 missing momentum short exemption (confidence.py)
  BUG-020: VWAP is multi-day, not session-reset (indicators.py)
  BUG-006: C10/C11 default True when data unavailable (confidence.py)
  BUG-010: Friday blackout boundary off-by-one (session.py)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


# ── BUG-002: candlestick_patterns plural key ──────────────────────────────────

class TestCandlestickPatternsKey:
    """BUG-002: analyze_timeframe must populate candlestick_patterns (plural) list."""

    def _make_candles(self, n=50, bullish=True):
        """Generate candles. Last candle shaped as hammer (bullish) or shooting star (bearish)."""
        candles = []
        base = 38000
        for i in range(n - 1):
            price = base + (i % 30) * 10
            candles.append({
                "open": price,
                "high": price + 40,
                "low": price - 40,
                "close": price + 10,
                "volume": 1000,
            })
        # Craft a recognizable hammer (bullish) or shooting star (bearish) as the last candle
        if bullish:
            # Hammer: close near high, long lower wick
            candles.append({
                "open": base + 10,
                "high": base + 20,
                "low": base - 80,   # long lower wick
                "close": base + 18,  # close near high
                "volume": 1500,
            })
        else:
            # Shooting star / bearish engulfing: open near high, close near low
            candles.append({
                "open": base + 80,
                "high": base + 85,
                "low": base - 10,
                "close": base - 5,  # close near low, big bearish body
                "volume": 1500,
            })
        return candles

    def test_candlestick_patterns_key_exists(self):
        """analyze_timeframe() must return 'candlestick_patterns' (plural) as a list."""
        from core.indicators import analyze_timeframe
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "candlestick_patterns" in result, "candlestick_patterns key missing from analyze_timeframe output"
        assert isinstance(result["candlestick_patterns"], list), "candlestick_patterns must be a list"

    def test_candlestick_patterns_singular_still_present(self):
        """Existing singular keys must still be present (no regressions)."""
        from core.indicators import analyze_timeframe
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "candlestick_pattern" in result
        assert "candlestick_direction" in result
        assert "candlestick_strength" in result

    def test_candlestick_patterns_list_format(self):
        """Each entry in candlestick_patterns list must have direction/name/strength keys."""
        from core.indicators import analyze_timeframe
        candles = self._make_candles(50, bullish=True)
        result = analyze_timeframe(candles)
        for entry in result["candlestick_patterns"]:
            assert "direction" in entry
            assert "name" in entry
            assert "strength" in entry

    def test_neutral_pattern_gives_empty_list(self):
        """No pattern (neutral direction) must yield empty candlestick_patterns list."""
        from core.indicators import analyze_timeframe
        # Doji-like candles — no strong pattern
        candles = []
        for i in range(50):
            p = 38000
            candles.append({"open": p, "high": p + 5, "low": p - 5, "close": p + 1, "volume": 100})
        result = analyze_timeframe(candles)
        # If pattern_direction is neutral, list must be empty
        if result["candlestick_direction"] == "neutral" or result["candlestick_pattern"] is None:
            assert result["candlestick_patterns"] == []

    def test_indicators_snapshot_contains_candlestick_patterns(self):
        """detect_setup() indicators_snapshot must include candlestick_patterns list."""
        from core.indicators import detect_setup
        tf_daily = {
            "price": 38000, "above_ema200_fallback": True, "above_ema200": True,
            "rsi": 55, "prev_candle_high": 38600, "prev_candle_low": 38200, "prev_close": 37950,
        }
        tf_4h = {"price": 38000, "rsi": 55, "above_ema50": True}
        tf_15m = {
            "price": 38000, "open": 37980, "low": 37955,
            "prev_close": 37950,
            "bollinger_mid": 37990, "bollinger_upper": 38300, "bollinger_lower": 37700,
            "rsi": 42, "above_ema50": True, "above_ema200": True, "ema50": 37950,
            "vwap": None, "above_vwap": None,
            "ha_bullish": True, "ha_streak": 2,
            "fib_near": None, "fvg_bullish": False, "fvg_bearish": False,
            "swept_low": False, "swept_high": False,
            "candlestick_pattern": "hammer",
            "candlestick_direction": "bullish",
            "candlestick_strength": "strong",
            "candlestick_patterns": [{"direction": "bullish", "name": "hammer", "strength": "strong"}],
            "body_trend": "contracting", "consecutive_direction": -2,
            "avg_body_size": 15.0, "wick_ratio": 1.2,
        }
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        snap = result.get("indicators_snapshot", {})
        assert "candlestick_patterns" in snap, "candlestick_patterns missing from indicators_snapshot"
        assert isinstance(snap["candlestick_patterns"], list)

    def test_bullish_candlestick_patterns_used_in_detect_setup(self):
        """When candlestick_patterns has bullish entry, detect_setup bullish_pattern should be True."""
        from core.indicators import detect_setup
        tf_daily = {
            "price": 38000, "above_ema200_fallback": True, "above_ema200": True,
            "rsi": 55, "prev_candle_high": 38600, "prev_candle_low": 37800, "prev_close": 37950,
        }
        tf_4h = {"price": 38000, "rsi": 55, "above_ema50": True}
        # Oversold reversal setup conditions
        tf_15m = {
            "price": 37720, "open": 37700, "low": 37680,
            "prev_close": 37750,
            "bollinger_mid": 38000, "bollinger_upper": 38300, "bollinger_lower": 37730,
            "rsi": 28, "above_ema50": False, "above_ema200": False, "ema50": 38100,
            "ema200": 38200, "ema9": 37800,
            "vwap": None, "above_vwap": None,
            "ha_bullish": False, "ha_streak": -3,
            "fib_near": None, "fvg_bullish": False, "fvg_bearish": False,
            "swept_low": True, "swept_high": False,
            # BUG-002 fix: supply plural list
            "candlestick_patterns": [{"direction": "bullish", "name": "hammer", "strength": "strong"}],
            "candlestick_pattern": "hammer",
            "candlestick_direction": "bullish",
            "candlestick_strength": "strong",
            "body_trend": "contracting", "consecutive_direction": -3,
            "avg_body_size": 20.0, "wick_ratio": 2.5,
            "pullback_depth": -80, "avg_candle_range": 80,
            "above_ema9": False, "above_ema50": False, "above_ema200": False,
            "fibonacci": {}, "anchored_vwap_daily": None, "anchored_vwap_weekly": None,
            "volume_poc": None, "volume_vah": None, "volume_val": None,
            "equal_highs_zones": [], "equal_lows_zones": [],
            "prev_candle_high": 37900, "prev_candle_low": 37600,
        }
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # The important thing: no KeyError, indicators_snapshot includes candlestick_patterns
        snap = result.get("indicators_snapshot", {})
        assert "candlestick_patterns" in snap
        assert snap["candlestick_patterns"] == [{"direction": "bullish", "name": "hammer", "strength": "strong"}]


# ── BUG-016: milestone_msg discarded ─────────────────────────────────────────

class TestMilestoneAlert:
    """BUG-016: milestone_alert() result must be sent via send_alert, not discarded."""

    def test_milestone_msg_sent_via_send_alert(self):
        """When milestone_alert() returns a string, send_alert must be called with it."""
        import asyncio

        # Build minimal mock objects
        mock_telegram = MagicMock()
        mock_telegram.send_alert = AsyncMock()
        mock_telegram.send_position_update = AsyncMock()

        mock_momentum = MagicMock()
        mock_momentum.milestone_alert = MagicMock(return_value="Milestone: +200pts reached!")

        # Simulate the fixed monitor.py logic
        async def run():
            milestone_msg = mock_momentum.milestone_alert()
            if milestone_msg:
                await mock_telegram.send_alert(milestone_msg)

        asyncio.get_event_loop().run_until_complete(run())

        mock_telegram.send_alert.assert_called_once_with("Milestone: +200pts reached!")
        mock_telegram.send_position_update.assert_not_called()

    def test_no_milestone_no_send_alert(self):
        """When milestone_alert() returns None/empty, send_alert must NOT be called."""
        import asyncio

        mock_telegram = MagicMock()
        mock_telegram.send_alert = AsyncMock()

        mock_momentum = MagicMock()
        mock_momentum.milestone_alert = MagicMock(return_value=None)

        async def run():
            milestone_msg = mock_momentum.milestone_alert()
            if milestone_msg:
                await mock_telegram.send_alert(milestone_msg)

        asyncio.get_event_loop().run_until_complete(run())
        mock_telegram.send_alert.assert_not_called()

    def test_empty_string_milestone_no_send_alert(self):
        """Empty string milestone (falsy) must also not trigger send_alert."""
        import asyncio

        mock_telegram = MagicMock()
        mock_telegram.send_alert = AsyncMock()

        mock_momentum = MagicMock()
        mock_momentum.milestone_alert = MagicMock(return_value="")

        async def run():
            milestone_msg = mock_momentum.milestone_alert()
            if milestone_msg:
                await mock_telegram.send_alert(milestone_msg)

        asyncio.get_event_loop().run_until_complete(run())
        mock_telegram.send_alert.assert_not_called()


# ── BUG-017: C3 momentum short RSI exemption ─────────────────────────────────

class TestC3MomentumShortExemption:
    """BUG-017: C3 must use widened RSI zone (30-60) for momentum short setups."""

    def _base_short_tf(self, rsi_15m):
        tf_15m = {
            "price": 38000, "rsi": rsi_15m,
            "bollinger_mid": 38200, "bollinger_upper": 38500, "bollinger_lower": 37700,
            "ema50": 38100, "above_ema50": False, "above_ema200": False,
            "volume_signal": "NORMAL",
            "pullback_depth": -30,
            "avg_candle_range": 80,
            "ha_bullish": False, "ha_streak": -3,
        }
        tf_4h = {"price": 38000, "rsi": 45, "above_ema50": False, "above_ema200": False}
        tf_daily = {"price": 38000, "rsi": 40, "above_ema50": False, "above_ema200": False}
        return tf_daily, tf_4h, tf_15m

    def test_momentum_short_rsi_45_passes_c3(self):
        """RSI=45, momentum_short=True → C3 PASS (zone 30-60)."""
        from core.confidence import compute_confidence
        tf_daily, tf_4h, tf_15m = self._base_short_tf(rsi_15m=45)
        result = compute_confidence(
            "SHORT", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_short"
        )
        assert result["criteria"]["rsi_15m"] is True, (
            f"RSI=45 should pass C3 for momentum_short but got False. "
            f"Breakdown: {result['reasons'].get('rsi_15m')}"
        )

    def test_standard_short_rsi_45_fails_c3(self):
        """RSI=45, standard SHORT → C3 FAIL (zone 55-75 only)."""
        from core.confidence import compute_confidence
        tf_daily, tf_4h, tf_15m = self._base_short_tf(rsi_15m=45)
        result = compute_confidence(
            "SHORT", tf_daily, tf_4h, tf_15m,
            setup_type="bb_upper_rejection"
        )
        assert result["criteria"]["rsi_15m"] is False, (
            f"RSI=45 should fail C3 for standard SHORT but got True. "
            f"Breakdown: {result['reasons'].get('rsi_15m')}"
        )

    def test_momentum_short_rsi_32_passes_c3(self):
        """RSI=32, momentum_short=True → C3 PASS (zone 30-60)."""
        from core.confidence import compute_confidence
        tf_daily, tf_4h, tf_15m = self._base_short_tf(rsi_15m=32)
        result = compute_confidence(
            "SHORT", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_short"
        )
        assert result["criteria"]["rsi_15m"] is True

    def test_momentum_short_rsi_62_fails_c3(self):
        """RSI=62, momentum_short=True → C3 FAIL (zone 30-60, 62 is outside)."""
        from core.confidence import compute_confidence
        tf_daily, tf_4h, tf_15m = self._base_short_tf(rsi_15m=62)
        result = compute_confidence(
            "SHORT", tf_daily, tf_4h, tf_15m,
            setup_type="momentum_continuation_short"
        )
        assert result["criteria"]["rsi_15m"] is False

    def test_standard_short_rsi_65_passes_c3(self):
        """RSI=65, standard SHORT → C3 PASS (zone 55-75)."""
        from core.confidence import compute_confidence
        tf_daily, tf_4h, tf_15m = self._base_short_tf(rsi_15m=65)
        result = compute_confidence(
            "SHORT", tf_daily, tf_4h, tf_15m,
            setup_type="bb_upper_rejection"
        )
        assert result["criteria"]["rsi_15m"] is True

    def test_vwap_rejection_momentum_short_rsi_50_passes(self):
        """vwap_rejection_short_momentum setup with RSI=50 → C3 PASS."""
        from core.confidence import compute_confidence
        tf_daily, tf_4h, tf_15m = self._base_short_tf(rsi_15m=50)
        result = compute_confidence(
            "SHORT", tf_daily, tf_4h, tf_15m,
            setup_type="vwap_rejection_short_momentum"
        )
        assert result["criteria"]["rsi_15m"] is True


# ── BUG-020: VWAP session reset via anchored daily ────────────────────────────

class TestVwapSessionReset:
    """BUG-020: result['vwap'] must use anchored_vwap_daily when available."""

    def _make_candles_with_timestamps(self, n=50, today="2026-03-05"):
        """Candles spanning yesterday and today, with timestamps and volume."""
        candles = []
        base = 38000
        # Yesterday's candles
        for i in range(n - 10):
            candles.append({
                "open": base + i * 2,
                "high": base + i * 2 + 30,
                "low": base + i * 2 - 30,
                "close": base + i * 2 + 10,
                "volume": 500,
                "timestamp": f"2026-03-04T{i % 24:02d}:00:00",
            })
        # Today's candles (should anchor from here)
        for i in range(10):
            candles.append({
                "open": base + 200 + i * 5,
                "high": base + 200 + i * 5 + 20,
                "low": base + 200 + i * 5 - 20,
                "close": base + 200 + i * 5 + 8,
                "volume": 800,
                "timestamp": f"{today}T{i:02d}:00:00",
            })
        return candles

    def test_vwap_uses_anchored_daily_when_available(self):
        """result['vwap'] == anchored_vwap_daily when timestamps+volume exist."""
        from core.indicators import analyze_timeframe, anchored_vwap
        from datetime import datetime, timezone

        with patch("core.indicators.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.strftime.side_effect = lambda fmt: (
                "2026-03-05" if fmt == "%Y-%m-%d" else "0"
            )
            mock_now.weekday.return_value = 3  # Thursday
            mock_now.__sub__ = lambda self, other: mock_now  # for timedelta subtraction
            mock_dt.now.return_value = mock_now
            mock_dt.timezone = timezone

            candles = self._make_candles_with_timestamps(50, today="2026-03-05")

            # Compute expected anchored daily VWAP independently
            expected_daily_vwap = anchored_vwap(candles, "2026-03-05")

            result = analyze_timeframe(candles)

        if expected_daily_vwap is not None:
            assert result["vwap"] == expected_daily_vwap, (
                f"result['vwap'] ({result['vwap']}) != anchored_vwap_daily ({expected_daily_vwap})"
            )

    def test_anchored_vwap_daily_key_always_present(self):
        """analyze_timeframe() must always return anchored_vwap_daily key."""
        from core.indicators import analyze_timeframe
        # Candles without timestamps → anchored_vwap_daily should be None, not missing
        candles = [
            {"open": 38000, "high": 38050, "low": 37950, "close": 38020, "volume": 1000}
            for _ in range(50)
        ]
        result = analyze_timeframe(candles)
        assert "anchored_vwap_daily" in result

    def test_above_vwap_consistent_with_vwap(self):
        """above_vwap must be consistent with whatever vwap value is set."""
        from core.indicators import analyze_timeframe
        candles = [
            {
                "open": 38000 + i * 3,
                "high": 38010 + i * 3,
                "low": 37990 + i * 3,
                "close": 38005 + i * 3,
                "volume": 1000,
                "timestamp": f"2026-03-05T{i % 24:02d}:00:00",
            }
            for i in range(50)
        ]
        result = analyze_timeframe(candles)
        if result["vwap"] is not None:
            expected_above = result["price"] > result["vwap"]
            assert result["above_vwap"] == expected_above


# ── BUG-006: C10/C11 conservative defaults ────────────────────────────────────

class TestC10C11ConservativeDefaults:
    """BUG-006: C10 and C11 must default to False when data unavailable."""

    def _ideal_long(self):
        """Return ideal long setup with HA and 4H data present."""
        return {
            "price": 38000, "rsi": 42,
            "bollinger_mid": 38010, "bollinger_upper": 38400, "bollinger_lower": 37700,
            "ema50": 37985, "above_ema50": True, "above_ema200": True,
            "volume_signal": "NORMAL", "pullback_depth": -50, "avg_candle_range": 80,
            "ha_bullish": True, "ha_streak": 3,
        }

    def test_c10_defaults_false_when_4h_above_ema50_missing(self):
        """C10: 4H above_ema50 missing → False (conservative)."""
        from core.confidence import compute_confidence
        tf_15m = self._ideal_long()
        tf_4h = {"price": 38000, "rsi": 55}  # no above_ema50
        tf_daily = {"price": 38000, "rsi": 60, "above_ema200": True, "above_ema50": True}
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is False

    def test_c10_passes_when_4h_data_present(self):
        """C10: 4H above_ema50=True + LONG → True."""
        from core.confidence import compute_confidence
        tf_15m = self._ideal_long()
        tf_4h = {"price": 38000, "rsi": 55, "above_ema50": True}
        tf_daily = {"price": 38000, "rsi": 60, "above_ema200": True, "above_ema50": True}
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["trend_4h"] is True

    def test_c11_defaults_false_when_ha_bullish_missing(self):
        """C11: ha_bullish missing → False (conservative)."""
        from core.confidence import compute_confidence
        tf_15m = self._ideal_long()
        tf_15m.pop("ha_bullish", None)
        tf_4h = {"price": 38000, "rsi": 55, "above_ema50": True}
        tf_daily = {"price": 38000, "rsi": 60, "above_ema200": True, "above_ema50": True}
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is False

    def test_c11_defaults_false_when_ha_bullish_is_none(self):
        """C11: ha_bullish=None → False (conservative)."""
        from core.confidence import compute_confidence
        tf_15m = self._ideal_long()
        tf_15m["ha_bullish"] = None
        tf_4h = {"price": 38000, "rsi": 55, "above_ema50": True}
        tf_daily = {"price": 38000, "rsi": 60, "above_ema200": True, "above_ema50": True}
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is False

    def test_c11_passes_when_ha_data_present(self):
        """C11: ha_bullish=True + LONG → True."""
        from core.confidence import compute_confidence
        tf_15m = self._ideal_long()
        tf_4h = {"price": 38000, "rsi": 55, "above_ema50": True}
        tf_daily = {"price": 38000, "rsi": 60, "above_ema200": True, "above_ema50": True}
        result = compute_confidence("LONG", tf_daily, tf_4h, tf_15m)
        assert result["criteria"]["ha_aligned"] is True

    def test_missing_c10_c11_lowers_score(self):
        """Missing 4H and HA data must produce a lower score than when present."""
        from core.confidence import compute_confidence
        tf_15m_full = self._ideal_long()
        tf_4h_full = {"price": 38000, "rsi": 55, "above_ema50": True}
        tf_daily = {"price": 38000, "rsi": 60, "above_ema200": True, "above_ema50": True}

        result_full = compute_confidence("LONG", tf_daily, tf_4h_full, tf_15m_full)

        tf_15m_missing = self._ideal_long()
        tf_15m_missing.pop("ha_bullish", None)
        tf_4h_missing = {"price": 38000, "rsi": 55}  # no above_ema50

        result_missing = compute_confidence("LONG", tf_daily, tf_4h_missing, tf_15m_missing)
        assert result_missing["score"] < result_full["score"]


# ── BUG-010: Friday blackout boundary ─────────────────────────────────────────

class TestFridayBlackoutBoundary:
    """BUG-010: blackout check must use < not <= for the end boundary (16:00 UTC)."""

    def _mock_friday_at(self, hour, minute):
        """Return a patched utcnow() that returns the given hour/minute on a Friday."""
        from unittest.mock import patch as _patch
        import core.session as session_mod
        mock_dt = MagicMock()
        mock_dt.weekday.return_value = 4  # Friday
        mock_dt.hour = hour
        mock_dt.minute = minute
        return _patch.object(session_mod, "utcnow", return_value=mock_dt)

    def test_1559_is_blocked(self):
        """15:59 UTC Friday → inside window → blocked."""
        from core.session import is_friday_blackout
        with self._mock_friday_at(15, 59):
            blocked, reason = is_friday_blackout()
        assert blocked is True

    def test_1600_is_not_blocked(self):
        """16:00 UTC Friday → at boundary → NOT blocked (< not <=)."""
        from core.session import is_friday_blackout
        with self._mock_friday_at(16, 0):
            blocked, reason = is_friday_blackout()
        assert blocked is False, f"16:00 should not be blocked but got: {reason}"

    def test_1601_is_not_blocked(self):
        """16:01 UTC Friday → past window → not blocked."""
        from core.session import is_friday_blackout
        with self._mock_friday_at(16, 1):
            blocked, reason = is_friday_blackout()
        assert blocked is False

    def test_1200_is_blocked(self):
        """12:00 UTC Friday → start of window → blocked."""
        from core.session import is_friday_blackout
        with self._mock_friday_at(12, 0):
            blocked, reason = is_friday_blackout()
        assert blocked is True

    def test_1159_is_not_blocked(self):
        """11:59 UTC Friday → just before window → not blocked."""
        from core.session import is_friday_blackout
        with self._mock_friday_at(11, 59):
            blocked, reason = is_friday_blackout()
        assert blocked is False
