"""
Tests for storage/database.py
Uses a temporary in-memory or temp file database.
"""
import json
import pytest
import tempfile
from datetime import datetime, date
from storage.database import Storage


@pytest.fixture
def db():
    """Create a temporary database for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        storage = Storage(db_path=f.name)
        yield storage


class TestScanHistory:
    def test_save_and_retrieve(self, db):
        db.save_scan({
            "timestamp": datetime.now().isoformat(),
            "session": "tokyo_open",
            "price": 59500,
            "indicators": {"m15": {"rsi": 45}},
            "setup_found": True,
            "confidence": 85,
            "action_taken": "alert_sent",
            "api_cost": 0.012,
        })
        scans = db.get_recent_scans(5)
        assert len(scans) == 1
        assert scans[0]["session"] == "tokyo_open"
        assert scans[0]["price"] == 59500

    def test_order_recent_first(self, db):
        for i in range(5):
            db.save_scan({
                "timestamp": f"2026-02-28T0{i}:00:00",
                "price": 59500 + i * 10,
            })
        scans = db.get_recent_scans(3)
        assert len(scans) == 3
        # Should be in chronological order (reversed from DESC)
        assert scans[0]["price"] < scans[-1]["price"]


class TestTradingJournal:
    def test_log_open_and_close(self, db):
        trade_num = db.log_trade_open({
            "deal_id": "DEAL_001",
            "direction": "LONG",
            "lots": 0.03,
            "entry_price": 59500,
            "stop_loss": 59300,
            "take_profit": 59900,
            "balance_before": 20.09,
            "confidence": 85,
            "setup_type": "bollinger_mid_bounce",
            "session": "tokyo_open",
        })
        assert trade_num == 1

        db.log_trade_close("DEAL_001", {
            "exit_price": 59900,
            "pnl": 12.0,
            "balance_after": 32.09,
            "result": "TP_HIT",
            "duration_minutes": 45,
            "phase_at_close": "breakeven",
        })

        trades = db.get_recent_trades(5)
        assert len(trades) == 1
        assert trades[0]["pnl"] == 12.0
        assert trades[0]["result"] == "TP_HIT"

    def test_trade_numbering(self, db):
        for i in range(3):
            num = db.log_trade_open({
                "deal_id": f"DEAL_{i}",
                "direction": "LONG",
                "lots": 0.03,
                "entry_price": 59500,
            })
            assert num == i + 1

    def test_stats_calculation(self, db):
        # Log 3 wins and 1 loss
        for i, (pnl, deal_id) in enumerate([
            (2.0, "W1"), (3.0, "W2"), (4.0, "W3"), (-4.08, "L1")
        ]):
            db.log_trade_open({
                "deal_id": deal_id,
                "direction": "LONG",
                "lots": 0.03,
                "entry_price": 59500,
                "confidence": 85,
            })
            db.log_trade_close(deal_id, {
                "exit_price": 59600 if pnl > 0 else 59300,
                "pnl": pnl,
                "balance_after": 20 + pnl,
                "result": "TP_HIT" if pnl > 0 else "SL_HIT",
            })

        stats = db.get_trade_stats()
        assert stats["total"] == 4
        assert stats["wins"] == 3
        assert stats["losses"] == 1
        assert stats["win_rate"] == 75.0
        assert abs(stats["total_pnl"] - 4.92) < 0.01


class TestPositionState:
    def test_open_and_close_position(self, db):
        db.set_position_open({
            "deal_id": "POS_001",
            "direction": "LONG",
            "lots": 0.03,
            "entry_price": 59500,
            "stop_level": 59300,
            "limit_level": 59900,
            "confidence": 85,
        })
        state = db.get_position_state()
        assert state["has_open"] == 1
        assert state["deal_id"] == "POS_001"

        db.set_position_closed()
        state = db.get_position_state()
        assert state["has_open"] == 0
        assert state["deal_id"] is None

    def test_pending_alert(self, db):
        alert = {"direction": "LONG", "entry": 59500, "confidence": 85}
        db.set_pending_alert(alert)
        retrieved = db.get_pending_alert()
        assert retrieved["direction"] == "LONG"
        assert retrieved["confidence"] == 85

        db.clear_pending_alert()
        assert db.get_pending_alert() is None


class TestAccountState:
    def test_initial_balance(self, db):
        state = db.get_account_state()
        assert state["balance"] == 20.09
        assert state["starting_balance"] == 16.67

    def test_record_win(self, db):
        db.record_trade_result(pnl=3.0, new_balance=23.09)
        state = db.get_account_state()
        assert state["balance"] == 23.09
        assert state["consecutive_losses"] == 0

    def test_record_loss(self, db):
        db.record_trade_result(pnl=-4.0, new_balance=16.09)
        state = db.get_account_state()
        assert state["balance"] == 16.09
        assert state["consecutive_losses"] == 1

    def test_consecutive_losses_reset_on_win(self, db):
        db.record_trade_result(pnl=-2.0, new_balance=18.09)
        db.record_trade_result(pnl=-2.0, new_balance=16.09)
        state = db.get_account_state()
        assert state["consecutive_losses"] == 2

        db.record_trade_result(pnl=3.0, new_balance=19.09)
        state = db.get_account_state()
        assert state["consecutive_losses"] == 0

    def test_system_pause_resume(self, db):
        db.set_system_active(False)
        assert db.get_account_state()["system_active"] == 0
        db.set_system_active(True)
        assert db.get_account_state()["system_active"] == 1

    def test_api_cost_tracking(self, db):
        db.save_scan({"api_cost": 0.012})
        db.save_scan({"api_cost": 0.035})
        total = db.get_api_cost_total()
        assert abs(total - 0.047) < 0.001


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
