"""
Tests for trading/risk_manager.py — SL/TP direction assertions, R:R calculation,
confidence floor, direction normalisation.

Uses a minimal mock storage object to avoid SQLite dependency.
"""
import pytest
from unittest.mock import MagicMock
from trading.risk_manager import RiskManager
from config.settings import (
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT, MIN_RR_RATIO, SPREAD_ESTIMATE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_storage(has_position=False, consec_losses=0, last_loss_time=None,
                 daily_loss=0, weekly_loss=0, system_active=True):
    """Create a mock storage object returning safe defaults."""
    storage = MagicMock()
    storage.get_position_state.return_value = {"has_open_position": has_position}
    storage.get_account_state.return_value = {
        "consecutive_losses": consec_losses,
        "last_loss_time": last_loss_time,
        "daily_loss_today": daily_loss,
        "weekly_loss": weekly_loss,
        "system_active": system_active,
    }
    return storage


def make_manager(has_position=False):
    return RiskManager(make_storage(has_position=has_position))


ENTRY = 38000.0
BALANCE = 500.0  # Enough to cover margin
LOTS = 0.1


# ── SL/TP Direction Validation (CHECK 0) ─────────────────────────────────────

class TestSlTpDirectionValidation:
    """CHECK 0: hard safety — SL/TP must be on correct sides of entry."""

    def test_long_valid_direction(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200,
            take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is True

    def test_long_sl_above_entry_fails(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY + 100,  # SL above entry — wrong for LONG
            take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is False
        assert result["approved"] is False

    def test_long_tp_below_entry_fails(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200,
            take_profit=ENTRY - 100,  # TP below entry — wrong for LONG
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is False
        assert result["approved"] is False

    def test_short_valid_direction(self):
        rm = make_manager()
        result = rm.validate_trade(
            "SHORT", LOTS, ENTRY,
            stop_loss=ENTRY + 200,   # SL above entry ✓
            take_profit=ENTRY - 400,  # TP below entry ✓
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is True

    def test_short_sl_below_entry_fails(self):
        rm = make_manager()
        result = rm.validate_trade(
            "SHORT", LOTS, ENTRY,
            stop_loss=ENTRY - 200,  # SL below entry — wrong for SHORT
            take_profit=ENTRY - 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is False
        assert result["approved"] is False

    def test_short_tp_above_entry_fails(self):
        rm = make_manager()
        result = rm.validate_trade(
            "SHORT", LOTS, ENTRY,
            stop_loss=ENTRY + 200,
            take_profit=ENTRY + 100,  # TP above entry — wrong for SHORT
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is False
        assert result["approved"] is False

    def test_rejection_reason_set_on_direction_fail(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY + 100,
            take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["rejection_reason"] is not None
        assert "MISMATCH" in result["rejection_reason"] or "LONG" in result["rejection_reason"]


# ── Direction Normalisation ───────────────────────────────────────────────────

class TestDirectionNormalisation:
    def test_buy_normalised_to_long(self):
        rm = make_manager()
        # BUY should be treated as LONG — same SL/TP validation
        result = rm.validate_trade(
            "BUY", LOTS, ENTRY,
            stop_loss=ENTRY - 200,
            take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is True

    def test_sell_normalised_to_short(self):
        rm = make_manager()
        result = rm.validate_trade(
            "SELL", LOTS, ENTRY,
            stop_loss=ENTRY + 200,
            take_profit=ENTRY - 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is True

    def test_sell_with_long_levels_fails(self):
        rm = make_manager()
        result = rm.validate_trade(
            "SELL", LOTS, ENTRY,
            stop_loss=ENTRY - 200,  # Wrong for short
            take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["sl_tp_direction"]["pass"] is False


# ── Confidence Floor ──────────────────────────────────────────────────────────

class TestConfidenceFloor:
    def _trade(self, direction, confidence):
        rm = make_manager()
        if direction == "LONG":
            sl, tp = ENTRY - 200, ENTRY + 400
        else:
            sl, tp = ENTRY + 200, ENTRY - 400
        return rm.validate_trade(
            direction, LOTS, ENTRY,
            stop_loss=sl, take_profit=tp,
            confidence=confidence, balance=BALANCE,
        )

    def test_long_at_min_confidence_passes(self):
        result = self._trade("LONG", MIN_CONFIDENCE)
        assert result["checks"]["confidence"]["pass"] is True

    def test_long_below_min_confidence_fails(self):
        result = self._trade("LONG", MIN_CONFIDENCE - 1)
        assert result["checks"]["confidence"]["pass"] is False

    def test_short_at_min_confidence_passes(self):
        result = self._trade("SHORT", MIN_CONFIDENCE_SHORT)
        assert result["checks"]["confidence"]["pass"] is True

    def test_short_below_short_threshold_fails(self):
        result = self._trade("SHORT", MIN_CONFIDENCE_SHORT - 1)
        assert result["checks"]["confidence"]["pass"] is False

    def test_short_threshold_higher_than_long(self):
        assert MIN_CONFIDENCE_SHORT > MIN_CONFIDENCE

    def test_long_at_short_threshold_still_passes(self):
        """Long only needs MIN_CONFIDENCE; passing at 75 should pass."""
        result = self._trade("LONG", MIN_CONFIDENCE_SHORT)
        assert result["checks"]["confidence"]["pass"] is True


# ── R:R Ratio (with spread both sides) ───────────────────────────────────────

class TestRiskReward:
    def _rr_check(self, risk_pts, reward_pts):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - risk_pts,
            take_profit=ENTRY + reward_pts,
            confidence=80, balance=BALANCE,
        )
        return result["checks"]["risk_reward"]

    def test_standard_200sl_400tp_passes(self):
        check = self._rr_check(200, 400)
        assert check["pass"] is True

    def test_effective_rr_deducts_spread_both_sides(self):
        """
        Effective R:R should be:
          effective_risk   = risk + SPREAD_ESTIMATE
          effective_reward = reward - SPREAD_ESTIMATE
          effective_rr     = effective_reward / effective_risk
        """
        risk, reward = 200, 400
        expected_eff_rr = (reward - SPREAD_ESTIMATE) / (risk + SPREAD_ESTIMATE)
        check = self._rr_check(risk, reward)
        # Detail string must mention effective R:R
        assert "Effective" in check["detail"] or "effective" in check["detail"]
        assert check["pass"] == (expected_eff_rr >= MIN_RR_RATIO)

    def test_marginal_rr_with_tiny_reward_fails(self):
        # risk=200, reward=100 → effective = (100-7)/(200+7) < 1
        check = self._rr_check(200, 100)
        assert check["pass"] is False

    def test_equal_risk_reward_may_fail_after_spread(self):
        # 1:1 gross → after spread deduction it's below minimum
        check = self._rr_check(200, 200)
        effective_rr = (200 - SPREAD_ESTIMATE) / (200 + SPREAD_ESTIMATE)
        expected_pass = effective_rr >= MIN_RR_RATIO
        assert check["pass"] == expected_pass

    def test_large_rr_passes(self):
        # 1:4 gross → very safe
        check = self._rr_check(200, 800)
        assert check["pass"] is True

    def test_short_rr_calculation(self):
        rm = make_manager()
        # SHORT: SL=+200, TP=-400
        result = rm.validate_trade(
            "SHORT", LOTS, ENTRY,
            stop_loss=ENTRY + 200,
            take_profit=ENTRY - 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["risk_reward"]["pass"] is True


# ── Max Positions ─────────────────────────────────────────────────────────────

class TestMaxPositions:
    def test_no_position_allows_trade(self):
        rm = make_manager(has_position=False)
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200, take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["max_positions"]["pass"] is True

    def test_existing_position_blocks_trade(self):
        rm = make_manager(has_position=True)
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200, take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["max_positions"]["pass"] is False
        assert result["approved"] is False


# ── System Active ─────────────────────────────────────────────────────────────

class TestSystemActive:
    def test_paused_system_blocks_trade(self):
        rm = RiskManager(make_storage(system_active=False))
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200, take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["system_active"]["pass"] is False
        assert result["approved"] is False

    def test_active_system_allows(self):
        rm = RiskManager(make_storage(system_active=True))
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200, take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["checks"]["system_active"]["pass"] is True


# ── Return Structure ──────────────────────────────────────────────────────────

class TestReturnStructure:
    def test_approved_key_present(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200, take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert "approved" in result
        assert "checks" in result
        assert "rejection_reason" in result
        assert "warnings" in result
        assert "summary" in result

    def test_approved_false_when_any_check_fails(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY + 100,  # Wrong direction — hard fail
            take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        assert result["approved"] is False

    def test_rejection_reason_none_when_all_pass(self):
        rm = make_manager()
        result = rm.validate_trade(
            "LONG", LOTS, ENTRY,
            stop_loss=ENTRY - 200, take_profit=ENTRY + 400,
            confidence=80, balance=BALANCE,
        )
        if result["approved"]:
            assert result["rejection_reason"] is None
