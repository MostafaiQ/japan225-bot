"""
Tests for monitor.py startup_sync() crash recovery logic.
Four cases:
  1. IG has position / DB doesn't   → call set_position_open, init momentum_tracker
  2. DB has position / IG doesn't   → call set_position_closed, clear tracker
  3. Both have position              → re-init momentum_tracker from DB state
  4. Neither                         → clean start, send Telegram alert

Keys verified by reading monitor.startup_sync() implementation:
  - DB state key: "has_open" (NOT "has_open_position")
  - IG position keys: lowercase (deal_id, direction, size, level, etc.)
  - Storage methods: set_position_open(), set_position_closed(), log_trade_close()
  - Tracker attribute: self.momentum_tracker (NOT self._momentum_tracker)

conftest.py stubs: trading_ig, anthropic, telegram, yfinance.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from core.ig_client import POSITIONS_API_ERROR
from monitor import TradingMonitor


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ig_position(deal_id="DEAL001", direction="BUY", size=0.1,
                      level=38000.0, stop_level=37800.0, limit_level=38400.0):
    """IG position dict with lowercase keys (as returned by ig_client)."""
    return {
        "deal_id": deal_id,
        "direction": direction,
        "size": size,
        "level": level,
        "stop_level": stop_level,
        "limit_level": limit_level,
        "created": "2024-01-15T10:00:00",
    }


def _make_db_state(has_open=True, deal_id="DEAL001", direction="LONG",
                   entry_price=38000.0, phase="initial"):
    """DB position state dict with the correct key 'has_open'."""
    if not has_open:
        return {"has_open": False}
    return {
        "has_open": True,
        "deal_id": deal_id,
        "direction": direction,
        "entry_price": entry_price,
        "phase": phase,
        "size": 0.1,
        "stop_level": 37800.0,
        "limit_level": 38400.0,
        "opened_at": "2024-01-15T10:00:00",
    }


def _make_monitor(ig_positions, db_state):
    """
    Build a TradingMonitor with fully mocked dependencies.
    Bypasses __init__ and sets attributes directly.
    """
    monitor = TradingMonitor.__new__(TradingMonitor)

    # IG client mock
    ig = MagicMock()
    ig.get_open_positions.return_value = ig_positions
    ig.modify_position.return_value = True
    ig.close_position.return_value = {"dealStatus": "ACCEPTED"}
    monitor.ig = ig

    # Storage mock — expose all relevant methods
    storage = MagicMock()
    storage.get_position_state.return_value = db_state
    storage.get_account_state.return_value = {
        "system_active": True, "consecutive_losses": 0,
        "daily_loss_today": 0, "weekly_loss": 0,
    }
    monitor.storage = storage

    # Telegram mock
    telegram = MagicMock()
    telegram.send_alert = AsyncMock()
    monitor.telegram = telegram

    # Internal state
    monitor._paused = False
    monitor._running = True
    monitor._position_empty_count = 0
    monitor._momentum_tracker = None  # private attr (may not be used; real attr is self.momentum_tracker)
    monitor.momentum_tracker = None   # actual attr name in monitor.py
    monitor._exit_manager = MagicMock()

    return monitor


# ── Case 1: IG has position, DB doesn't (ORPHAN recovery) ────────────────────

class TestOrphanRecovery:
    """
    IG reports an open position, but DB has no record (bot crashed after open,
    before DB write). Expected: call set_position_open and init momentum_tracker.
    """
    @pytest.mark.asyncio
    async def test_orphan_calls_set_position_open(self):
        ig_pos = _make_ig_position()
        monitor = _make_monitor([ig_pos], _make_db_state(has_open=False))
        await monitor.startup_sync()
        monitor.storage.set_position_open.assert_called_once()

    @pytest.mark.asyncio
    async def test_orphan_initialises_momentum_tracker(self):
        ig_pos = _make_ig_position()
        monitor = _make_monitor([ig_pos], _make_db_state(has_open=False))
        await monitor.startup_sync()
        assert monitor.momentum_tracker is not None

    @pytest.mark.asyncio
    async def test_orphan_sends_telegram_alert(self):
        ig_pos = _make_ig_position()
        monitor = _make_monitor([ig_pos], _make_db_state(has_open=False))
        await monitor.startup_sync()
        monitor.telegram.send_alert.assert_called()
        call_text = monitor.telegram.send_alert.call_args[0][0].lower()
        assert any(w in call_text for w in ["restart", "not in db", "sync", "found", "position"])

    @pytest.mark.asyncio
    async def test_orphan_tracker_direction_is_long_for_buy(self):
        ig_pos = _make_ig_position(direction="BUY")
        monitor = _make_monitor([ig_pos], _make_db_state(has_open=False))
        await monitor.startup_sync()
        if monitor.momentum_tracker:
            assert monitor.momentum_tracker.direction == "LONG"

    @pytest.mark.asyncio
    async def test_orphan_tracker_direction_is_short_for_sell(self):
        ig_pos = _make_ig_position(direction="SELL")
        monitor = _make_monitor([ig_pos], _make_db_state(has_open=False))
        await monitor.startup_sync()
        if monitor.momentum_tracker:
            assert monitor.momentum_tracker.direction == "SHORT"


# ── Case 2: DB has position, IG doesn't (GHOST cleanup) ──────────────────────

class TestGhostCleanup:
    """
    DB says we have a position but IG doesn't — position closed while offline.
    Expected: call set_position_closed and log_trade_close; no tracker.
    """
    @pytest.mark.asyncio
    async def test_ghost_calls_set_position_closed(self):
        monitor = _make_monitor([], _make_db_state(has_open=True))
        await monitor.startup_sync()
        monitor.storage.set_position_closed.assert_called_once()

    @pytest.mark.asyncio
    async def test_ghost_logs_trade_close(self):
        monitor = _make_monitor([], _make_db_state(has_open=True, deal_id="DEAL001"))
        await monitor.startup_sync()
        monitor.storage.log_trade_close.assert_called_once()
        # First arg should be the deal_id
        call_args = monitor.storage.log_trade_close.call_args[0]
        assert call_args[0] == "DEAL001"

    @pytest.mark.asyncio
    async def test_ghost_no_momentum_tracker(self):
        monitor = _make_monitor([], _make_db_state(has_open=True))
        await monitor.startup_sync()
        assert monitor.momentum_tracker is None

    @pytest.mark.asyncio
    async def test_ghost_sends_telegram_alert(self):
        monitor = _make_monitor([], _make_db_state(has_open=True))
        await monitor.startup_sync()
        monitor.telegram.send_alert.assert_called()
        call_text = monitor.telegram.send_alert.call_args[0][0].lower()
        assert any(w in call_text for w in ["restart", "closed", "offline", "check"])


# ── Case 3: Both have same position (REINIT) ─────────────────────────────────

class TestReinit:
    """
    Both IG and DB have the same position (matching deal_id).
    Expected: momentum_tracker re-initialized from DB, no DB modification.
    """
    @pytest.mark.asyncio
    async def test_reinit_creates_momentum_tracker(self):
        ig_pos = _make_ig_position(deal_id="DEAL001")
        db = _make_db_state(deal_id="DEAL001", direction="LONG", entry_price=38000.0)
        monitor = _make_monitor([ig_pos], db)
        await monitor.startup_sync()
        assert monitor.momentum_tracker is not None

    @pytest.mark.asyncio
    async def test_reinit_tracker_direction_is_long(self):
        ig_pos = _make_ig_position(deal_id="DEAL001", direction="BUY")
        db = _make_db_state(deal_id="DEAL001", direction="LONG", entry_price=38000.0)
        monitor = _make_monitor([ig_pos], db)
        await monitor.startup_sync()
        if monitor.momentum_tracker:
            assert monitor.momentum_tracker.direction == "LONG"

    @pytest.mark.asyncio
    async def test_reinit_tracker_entry_matches_db(self):
        ig_pos = _make_ig_position(deal_id="DEAL001")
        db = _make_db_state(deal_id="DEAL001", entry_price=38500.0)
        monitor = _make_monitor([ig_pos], db)
        await monitor.startup_sync()
        if monitor.momentum_tracker:
            assert monitor.momentum_tracker.entry_price == pytest.approx(38500.0)

    @pytest.mark.asyncio
    async def test_reinit_does_not_modify_db(self):
        ig_pos = _make_ig_position(deal_id="DEAL001")
        db = _make_db_state(deal_id="DEAL001")
        monitor = _make_monitor([ig_pos], db)
        await monitor.startup_sync()
        monitor.storage.set_position_closed.assert_not_called()
        monitor.storage.set_position_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_reinit_sends_telegram_alert(self):
        ig_pos = _make_ig_position(deal_id="DEAL001")
        db = _make_db_state(deal_id="DEAL001")
        monitor = _make_monitor([ig_pos], db)
        await monitor.startup_sync()
        monitor.telegram.send_alert.assert_called()


# ── Case 4: Neither has position (clean start) ───────────────────────────────

class TestCleanStart:
    """
    No position on IG and no position in DB.
    Expected: clean start, no tracker, no DB modifications, sends startup alert.
    """
    @pytest.mark.asyncio
    async def test_clean_start_no_tracker(self):
        monitor = _make_monitor([], _make_db_state(has_open=False))
        await monitor.startup_sync()
        assert monitor.momentum_tracker is None

    @pytest.mark.asyncio
    async def test_clean_start_no_db_changes(self):
        monitor = _make_monitor([], _make_db_state(has_open=False))
        await monitor.startup_sync()
        monitor.storage.set_position_closed.assert_not_called()
        monitor.storage.set_position_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_clean_start_sends_telegram_alert(self):
        monitor = _make_monitor([], _make_db_state(has_open=False))
        await monitor.startup_sync()
        monitor.telegram.send_alert.assert_called()


# ── Case 5: IG API failure on startup ────────────────────────────────────────

class TestApiFailureOnStartup:
    """
    IG API returns POSITIONS_API_ERROR on startup.
    INVARIANT: DB must NOT be cleared — we cannot confirm position is closed.
    """
    @pytest.mark.asyncio
    async def test_api_error_does_not_clear_db(self):
        monitor = _make_monitor([], _make_db_state(has_open=True))
        monitor.ig.get_open_positions.return_value = POSITIONS_API_ERROR

        try:
            await monitor.startup_sync()
        except Exception:
            pass

        # Safety invariant: never clear DB when API fails
        monitor.storage.set_position_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_sends_warning_alert(self):
        monitor = _make_monitor([], _make_db_state(has_open=False))
        monitor.ig.get_open_positions.return_value = POSITIONS_API_ERROR

        await monitor.startup_sync()

        monitor.telegram.send_alert.assert_called()
        call_text = monitor.telegram.send_alert.call_args[0][0].lower()
        assert any(w in call_text for w in ["unavailable", "api", "cannot", "error", "verify"])

    @pytest.mark.asyncio
    async def test_api_error_returns_early(self):
        """After API error, no further DB or tracker work should happen."""
        monitor = _make_monitor([], _make_db_state(has_open=False))
        monitor.ig.get_open_positions.return_value = POSITIONS_API_ERROR

        await monitor.startup_sync()

        # No position recovery should happen
        monitor.storage.set_position_open.assert_not_called()
        assert monitor.momentum_tracker is None
