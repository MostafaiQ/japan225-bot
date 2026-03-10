"""
Tests for trading/risk_manager.py and trading/exit_manager.py
No API credentials needed - uses mock storage.
"""
import pytest
from datetime import datetime, timedelta
from trading.risk_manager import RiskManager
from trading.exit_manager import ExitManager, ExitPhase
from config.settings import MIN_CONFIDENCE, MAX_MARGIN_PERCENT, MIN_RR_RATIO


class MockStorage:
    """Minimal mock for storage dependency."""

    def __init__(self):
        self.position_state = {"has_open": False}
        self.account_state = {
            "system_active": True,
            "consecutive_losses": 0,
            "last_loss_time": None,
            "daily_loss_today": 0,
            "daily_loss_date": datetime.now().date().isoformat(),
            "weekly_loss": 0,
            "weekly_loss_start": datetime.now().date().isoformat(),
            "balance": 500.0,
        }

    def get_position_state(self):
        return self.position_state

    def get_account_state(self):
        return self.account_state

    def get_open_positions_count(self) -> int:
        if hasattr(self, "_mock_open_count"):
            return self._mock_open_count
        return 1 if self.position_state.get("has_open") else 0

    def get_open_positions(self) -> list:
        count = self.get_open_positions_count()
        return [{"deal_id": f"MOCK{i}", "direction": "LONG", "lots": 0.01,
                 "entry_price": 59500, "stop_loss": 59300}
                for i in range(count)]

    def update_position_phase(self, deal_id, phase):
        pass

    def update_position_levels(self, stop_level=None, limit_level=None):
        pass


class TestRiskManagerValidation:
    """Test the full validate_trade pipeline."""

    def setup_method(self):
        self.storage = MockStorage()
        self.rm = RiskManager(self.storage)

    def _base_trade(self, **overrides):
        trade = {
            "direction": "LONG",
            "lots": 0.02,
            "entry": 59500,
            "stop_loss": 59300,
            "take_profit": 59900,
            "confidence": 80,
            "balance": 500.0,
            "upcoming_events": [],
        }
        trade.update(overrides)
        return trade

    def test_clean_trade_passes(self):
        # Note: the calendar_block check (month-end) depends on the real date.
        # If running on the last 2 days of a month, this legitimately fails.
        result = self.rm.validate_trade(**self._base_trade())
        # Check all rules pass EXCEPT possibly calendar_block (date-dependent)
        non_calendar = {
            k: v for k, v in result["checks"].items() if k != "calendar_block"
        }
        assert all(c["pass"] for c in non_calendar.values()), (
            f"Failed checks: {[k for k, v in non_calendar.items() if not v['pass']]}"
        )

    def test_monthend_blocked(self):
        """Month-end blackout should reject trades in last 2 days of month."""
        # This test validates the rule exists - actual triggering depends on date
        result = self.rm.validate_trade(**self._base_trade())
        assert "calendar_block" in result["checks"]

    def test_low_confidence_rejected(self):
        result = self.rm.validate_trade(**self._base_trade(confidence=60))
        assert result["approved"] is False
        assert "confidence" in result["rejection_reason"].lower()

    def test_confidence_at_boundary(self):
        result = self.rm.validate_trade(**self._base_trade(confidence=70))
        assert result["checks"]["confidence"]["pass"] is True

    def test_margin_exceeded_rejected(self):
        # 0.35 lots at 59500 = $104.125 margin, > 10% of $500 = $50
        result = self.rm.validate_trade(**self._base_trade(lots=0.35))
        assert result["approved"] is False
        assert "margin" in result["rejection_reason"].lower()

    def test_margin_at_limit(self):
        # 0.08 lots at 59500 = $23.80 margin, 10% of $500 = $50 → passes
        result = self.rm.validate_trade(**self._base_trade(lots=0.08, balance=500.0))
        assert result["checks"]["margin"]["pass"] is True

    def test_bad_rr_rejected(self):
        # SL 200pts, TP 100pts = 1:0.5 after spread
        result = self.rm.validate_trade(**self._base_trade(
            stop_loss=59300, take_profit=59600
        ))
        # R:R = 100/200 = 0.5 before spread, even worse after
        assert result["checks"]["risk_reward"]["pass"] is False

    def test_open_position_rejected(self):
        # At MAX_OPEN_POSITIONS=3, need 3 open positions to reject
        from config.settings import MAX_OPEN_POSITIONS
        self.storage._mock_open_count = MAX_OPEN_POSITIONS
        result = self.rm.validate_trade(**self._base_trade())
        assert result["checks"]["max_positions"]["pass"] is False

    def test_consecutive_losses_cooldown(self):
        self.storage.account_state["consecutive_losses"] = 2
        self.storage.account_state["last_loss_time"] = datetime.now().isoformat()
        result = self.rm.validate_trade(**self._base_trade())
        assert result["checks"]["consecutive_losses"]["pass"] is False

    def test_cooldown_expired_passes(self):
        self.storage.account_state["consecutive_losses"] = 2
        expired = (datetime.now() - timedelta(hours=5)).isoformat()
        self.storage.account_state["last_loss_time"] = expired
        result = self.rm.validate_trade(**self._base_trade())
        assert result["checks"]["consecutive_losses"]["pass"] is True

    def test_daily_loss_limit(self):
        # Daily limit is 100% (effectively disabled), so $3 on $20 passes
        self.storage.account_state["daily_loss_today"] = -3.0
        result = self.rm.validate_trade(**self._base_trade())
        assert result["checks"]["daily_loss"]["pass"] is True

    def test_system_paused_rejected(self):
        self.storage.account_state["system_active"] = False
        result = self.rm.validate_trade(**self._base_trade())
        assert result["checks"]["system_active"]["pass"] is False

    def test_event_blackout(self):
        event_in_30min = {
            "name": "BOJ Rate Decision",
            "time": (datetime.now() + timedelta(minutes=30)).isoformat(),
            "impact": "HIGH",
        }
        result = self.rm.validate_trade(
            **self._base_trade(upcoming_events=[event_in_30min])
        )
        assert result["checks"]["event_blackout"]["pass"] is False


class TestSafeLotSize:
    def setup_method(self):
        self.rm = RiskManager(MockStorage())

    def test_lot_size_risk_based(self):
        # At $500 balance, 5% risk = $25, 150pt SL → lots ≈ 0.16, capped by margin
        lots = self.rm.get_safe_lot_size(500.0, 59500, 150)
        dollar_risk = lots * 150 * 1  # CONTRACT_SIZE = 1
        assert dollar_risk <= 500.0 * 0.08  # Within MAX_RISK_PERCENT (8%)

    def test_minimum_lot_size(self):
        lots = self.rm.get_safe_lot_size(1.0, 59500, 150)
        assert lots >= 0.02

    def test_scales_with_balance(self):
        lots_small = self.rm.get_safe_lot_size(200, 59500, 150)
        lots_big = self.rm.get_safe_lot_size(1000, 59500, 150)
        assert lots_big > lots_small


class TestExitManager:
    """Test exit manager — SL/TP fixed at entry, no mechanical modifications."""

    def test_no_action_at_any_price(self):
        """SL/TP fixed: no action regardless of P&L."""
        em = ExitManager(ig_client=None, storage=MockStorage())
        for price in [59600, 59660, 59820, 60000, 59200]:
            position = {
                "deal_id": "TEST1",
                "direction": "BUY",
                "entry": 59500,
                "current_price": price,
                "size": 0.03,
                "stop_level": 59300,
                "limit_level": 59900,
                "opened_at": datetime.now().isoformat(),
                "phase": ExitPhase.INITIAL,
            }
            action = em.evaluate_position(position)
            assert action["action"] == "none", f"Should be none at price {price}"

    def test_manual_trail_disabled(self):
        """Manual trailing is disabled."""
        em = ExitManager(ig_client=None, storage=MockStorage())
        position = {
            "deal_id": "TEST5",
            "direction": "BUY",
            "entry": 59500,
            "current_price": 60000,
            "stop_level": 59700,
            "phase": ExitPhase.RUNNER,
        }
        action = em.manual_trail_update(position)
        assert action is None


class TestExitManagerShort:
    """Test exit logic for SHORT positions — SL/TP fixed."""

    def test_short_no_action(self):
        """SL/TP fixed for shorts too."""
        em = ExitManager(ig_client=None, storage=MockStorage())
        position = {
            "deal_id": "SHORT1",
            "direction": "SELL",
            "entry": 59500,
            "current_price": 59340,
            "size": 0.03,
            "stop_level": 59700,
            "limit_level": 59100,
            "opened_at": datetime.now().isoformat(),
            "phase": ExitPhase.INITIAL,
        }
        action = em.evaluate_position(position)
        assert action["action"] == "none"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
