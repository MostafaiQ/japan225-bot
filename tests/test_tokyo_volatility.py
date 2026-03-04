"""
Tests for Tokyo volatility mode and ATR calculation.

Covers:
  1. compute_atr() — pure math, all edge cases
  2. analyze_timeframe() includes atr key
  3. Tokyo lot capping in _on_trade_confirm_inner()
  4. Non-Tokyo: lots unchanged
  5. Tokyo vs non-Tokyo consecutive loss threshold
  6. Settings constants are correct values
  7. ATR in analyze_timeframe with malformed candles
"""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from core.indicators import compute_atr, analyze_timeframe
from config.settings import (
    TOKYO_FORCED_LOTS, TOKYO_MAX_CONSECUTIVE_LOSSES,
    ATR_PERIOD, MAX_CONSECUTIVE_LOSSES,
)
from monitor import TradingMonitor


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


def _make_alert(direction="LONG", lots=0.5, sl=54700.0, tp=55200.0, entry=54850.0):
    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lots": lots,
        "confidence": 75,
        "confidence_breakdown": None,
        "setup_type": "bollinger_lower_bounce",
        "session": "tokyo",
        "ai_analysis": "test",
        "local_confidence": 70,
        "opus_confidence": 0,
        "indicators_compact": {},
    }


def _make_monitor_for_confirm(session_name="tokyo", consec_losses=0, lots_in_alert=0.5):
    """
    Build a TradingMonitor with mocked dependencies for _on_trade_confirm_inner().
    Returns (monitor, alert_data, open_position_mock).
    """
    monitor = TradingMonitor.__new__(TradingMonitor)

    # IG mock
    ig = MagicMock()
    ig.get_account_info.return_value = {"balance": 100.0}
    ig.get_market_info.return_value = {
        "bid": 54840.0, "offer": 54850.0, "spread": 10.0,
    }
    ig._candle_cache = {}  # empty cache — no ATR gate (removed)
    open_pos_result = {
        "deal_id": "TESTDEAL001",
        "level": 54850.0,
        "stop_level": 54700.0,
        "limit_level": 55200.0,
        "dealStatus": "ACCEPTED",
        "error": False,
    }
    ig.open_position.return_value = open_pos_result
    monitor.ig = ig

    # Storage mock
    storage = MagicMock()
    storage.get_position_state.return_value = {"has_open": False}
    storage.get_account_state.return_value = {
        "system_active": True,
        "consecutive_losses": consec_losses,
        "last_loss_time": None,
        "daily_loss_today": 0,
        "daily_loss_date": "2026-03-04",
    }
    storage.open_trade_atomic.return_value = 42
    monitor.storage = storage

    # Telegram mock
    telegram = MagicMock()
    telegram.send_alert = AsyncMock()
    monitor.telegram = telegram

    # Risk mock (not called in _on_trade_confirm_inner directly)
    monitor.risk = MagicMock()

    # Internal state attributes
    monitor.momentum_tracker = None
    monitor._position_price_buffer = []
    monitor._opus_pos_eval_counter = 0
    monitor._buffer_save_counter = 0
    monitor._position_empty_count = 0
    monitor._price_buffer_cache_path = Path("/tmp/test_price_buffer_cache.json")
    monitor._force_open_pending_path = Path("/tmp/test_force_open_pending.json")

    alert_data = _make_alert(lots=lots_in_alert)
    return monitor, alert_data, ig.open_position, session_name


async def _run_confirm(monitor, alert_data, session_name):
    """Run _on_trade_confirm_inner with patched session and synchronous executor."""
    session_return = {"name": session_name, "active": True, "priority": "HIGH"}

    # Mock the event loop's run_in_executor to call functions synchronously
    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(
        side_effect=lambda executor, fn, *args: fn(*args) if args else fn()
    )

    with patch("monitor.get_current_session", return_value=session_return), \
         patch("monitor.asyncio.get_event_loop", return_value=mock_loop):
        await monitor._on_trade_confirm_inner(alert_data)


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

    def test_tokyo_forced_lots(self):
        assert TOKYO_FORCED_LOTS == 0.01

    def test_tokyo_max_consecutive_losses(self):
        assert TOKYO_MAX_CONSECUTIVE_LOSSES == 5

    def test_atr_period(self):
        assert ATR_PERIOD == 14

    def test_tokyo_threshold_greater_than_default(self):
        """Tokyo allows more losses before cooldown than normal sessions."""
        assert TOKYO_MAX_CONSECUTIVE_LOSSES > MAX_CONSECUTIVE_LOSSES

    def test_tokyo_forced_lots_is_minimum(self):
        """TOKYO_FORCED_LOTS must equal MIN_LOT_SIZE."""
        from config.settings import MIN_LOT_SIZE
        assert TOKYO_FORCED_LOTS == MIN_LOT_SIZE


# ══════════════════════════════════════════════════════════════════════════════
# 4. Tokyo lot capping in _on_trade_confirm_inner()
# ══════════════════════════════════════════════════════════════════════════════

class TestTokyoLotCapping:

    @pytest.mark.asyncio
    async def test_tokyo_caps_lots_to_minimum(self):
        """Large lot in Tokyo → capped to 0.01."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", lots_in_alert=0.50
        )
        await _run_confirm(monitor, alert_data, session)
        call_kwargs = open_pos_mock.call_args
        assert call_kwargs is not None, "open_position was not called"
        actual_size = call_kwargs[1].get("size") or call_kwargs[0][1]
        assert actual_size == TOKYO_FORCED_LOTS, \
            f"Expected lots={TOKYO_FORCED_LOTS} in Tokyo, got {actual_size}"

    @pytest.mark.asyncio
    async def test_tokyo_already_minimum_lots_unchanged(self):
        """Lots already at 0.01 in Tokyo → stays 0.01 (no double-cap)."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", lots_in_alert=0.01
        )
        await _run_confirm(monitor, alert_data, session)
        call_kwargs = open_pos_mock.call_args
        actual_size = call_kwargs[1].get("size") or call_kwargs[0][1]
        assert actual_size == 0.01

    @pytest.mark.asyncio
    async def test_london_lots_not_capped(self):
        """Outside Tokyo (London) → lots unchanged from alert_data."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="london", lots_in_alert=0.50
        )
        await _run_confirm(monitor, alert_data, session)
        call_kwargs = open_pos_mock.call_args
        actual_size = call_kwargs[1].get("size") or call_kwargs[0][1]
        assert actual_size == 0.50, \
            f"Expected lots=0.50 in London (no cap), got {actual_size}"

    @pytest.mark.asyncio
    async def test_new_york_lots_not_capped(self):
        """New York session → lots unchanged."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="new_york", lots_in_alert=0.30
        )
        await _run_confirm(monitor, alert_data, session)
        call_kwargs = open_pos_mock.call_args
        actual_size = call_kwargs[1].get("size") or call_kwargs[0][1]
        assert actual_size == 0.30

    @pytest.mark.asyncio
    async def test_tokyo_tp_not_mechanically_overridden(self):
        """TP from alert_data is passed as-is — AI decides TP, not the bot."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", lots_in_alert=0.50
        )
        # alert has sl=54700, tp=55200 relative to entry=54850
        # tp_distance from current_price(54850) to tp(55200) = 350
        await _run_confirm(monitor, alert_data, session)
        call_kwargs = open_pos_mock.call_args
        limit_dist = call_kwargs[1].get("limit_distance") or call_kwargs[0][3]
        # Should NOT be forced to sl×1.5 — AI's TP is respected
        # tp_distance = |55200 - 54850| = 350
        assert limit_dist == 350, \
            f"Expected AI-chosen TP distance=350, got {limit_dist} (should not be overridden)"

    @pytest.mark.asyncio
    async def test_tokyo_sl_not_overridden(self):
        """SL from alert_data is passed as-is in Tokyo."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", lots_in_alert=0.50
        )
        await _run_confirm(monitor, alert_data, session)
        call_kwargs = open_pos_mock.call_args
        stop_dist = call_kwargs[1].get("stop_distance") or call_kwargs[0][2]
        # sl_distance = |54850 - 54700| = 150
        assert stop_dist == 150

    @pytest.mark.asyncio
    async def test_lots_saved_to_db_are_capped(self):
        """open_trade_atomic is called with the capped lot size, not the original."""
        monitor, alert_data, _, session = _make_monitor_for_confirm(
            session_name="tokyo", lots_in_alert=0.75
        )
        await _run_confirm(monitor, alert_data, session)
        call_args = monitor.storage.open_trade_atomic.call_args
        assert call_args is not None
        trade_dict = call_args[1]["trade"] if "trade" in call_args[1] else call_args[0][0]
        assert trade_dict["lots"] == TOKYO_FORCED_LOTS, \
            f"DB should store capped lots={TOKYO_FORCED_LOTS}, got {trade_dict['lots']}"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Consecutive loss threshold: Tokyo=5, others=2
# ══════════════════════════════════════════════════════════════════════════════

class TestConsecutiveLossThreshold:

    @pytest.mark.asyncio
    async def test_tokyo_3_losses_does_not_block(self):
        """3 consecutive losses in Tokyo → still trades (threshold=5)."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", consec_losses=3
        )
        # Need last_loss_time set to simulate real cooldown scenario
        monitor.storage.get_account_state.return_value["last_loss_time"] = "2026-03-04T00:00:00"
        await _run_confirm(monitor, alert_data, session)
        assert open_pos_mock.called, \
            "With 3 losses in Tokyo (threshold=5), trade should proceed"

    @pytest.mark.asyncio
    async def test_tokyo_4_losses_does_not_block(self):
        """4 consecutive losses in Tokyo → still trades (threshold=5)."""
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", consec_losses=4
        )
        monitor.storage.get_account_state.return_value["last_loss_time"] = "2026-03-04T00:00:00"
        await _run_confirm(monitor, alert_data, session)
        assert open_pos_mock.called, \
            "With 4 losses in Tokyo (threshold=5), trade should proceed"

    @pytest.mark.asyncio
    async def test_london_2_losses_blocks(self):
        """2 consecutive losses in London → blocked (threshold=2)."""
        from datetime import datetime, timedelta
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="london", consec_losses=2
        )
        # Set last_loss_time to recent (within cooldown window)
        recent = (datetime.now() - timedelta(minutes=30)).isoformat()
        monitor.storage.get_account_state.return_value["last_loss_time"] = recent
        await _run_confirm(monitor, alert_data, session)
        assert not open_pos_mock.called, \
            "With 2 losses in London (threshold=2), trade should be blocked"

    @pytest.mark.asyncio
    async def test_tokyo_5_losses_blocks(self):
        """5 consecutive losses in Tokyo → blocked (threshold=5)."""
        from datetime import datetime, timedelta
        monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
            session_name="tokyo", consec_losses=5
        )
        recent = (datetime.now() - timedelta(minutes=30)).isoformat()
        monitor.storage.get_account_state.return_value["last_loss_time"] = recent
        await _run_confirm(monitor, alert_data, session)
        assert not open_pos_mock.called, \
            "With 5 losses in Tokyo (threshold=5), trade should be blocked"

    @pytest.mark.asyncio
    async def test_zero_losses_always_proceeds(self):
        """0 consecutive losses in any session → never blocked by cooldown."""
        for session_name in ("tokyo", "london", "new_york"):
            monitor, alert_data, open_pos_mock, session = _make_monitor_for_confirm(
                session_name=session_name, consec_losses=0
            )
            await _run_confirm(monitor, alert_data, session)
            assert open_pos_mock.called, \
                f"0 losses in {session_name} should not block trade"


# ══════════════════════════════════════════════════════════════════════════════
# 6. ATR in analyzer prompt formatting
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
