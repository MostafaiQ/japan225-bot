"""
Tests for core/momentum.py — tiered adverse move detection, milestone alerts,
stale data detection.
"""
import pytest
from core.momentum import (
    MomentumTracker,
    TIER_NONE, TIER_MILD, TIER_MODERATE, TIER_SEVERE,
)
from config.settings import (
    ADVERSE_MILD_PTS, ADVERSE_MODERATE_PTS, ADVERSE_SEVERE_PTS,
    STALE_DATA_THRESHOLD,
)


class TestCurrentPnlPoints:
    def test_long_profit(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38200)
        assert t.current_pnl_points() == pytest.approx(200.0)

    def test_long_loss(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(37800)
        assert t.current_pnl_points() == pytest.approx(-200.0)

    def test_short_profit(self):
        t = MomentumTracker("SHORT", 38000)
        t.add_price(37800)
        assert t.current_pnl_points() == pytest.approx(200.0)

    def test_short_loss(self):
        t = MomentumTracker("SHORT", 38000)
        t.add_price(38200)
        assert t.current_pnl_points() == pytest.approx(-200.0)

    def test_no_prices_returns_zero(self):
        t = MomentumTracker("LONG", 38000)
        assert t.current_pnl_points() == 0.0

    def test_direction_case_insensitive(self):
        t = MomentumTracker("long", 38000)
        t.add_price(38100)
        assert t.current_pnl_points() == pytest.approx(100.0)


class TestAdverseMove5Min:
    def _make_tracker(self, direction, prices):
        t = MomentumTracker(direction, prices[0])
        for p in prices:
            t.add_price(p)
        return t

    def test_long_adverse_drop(self):
        # Price dropped 40 pts in 5 readings
        prices = [38100, 38080, 38060, 38070, 38060]
        t = self._make_tracker("LONG", prices)
        # adverse = reference(prices[-5]) - current(prices[-1])
        # reference is prices[-5] = prices[0] = 38100 (since we have exactly 5)
        assert t.adverse_move_5min() == pytest.approx(40.0)

    def test_long_favorable_move_is_negative_adverse(self):
        prices = [38000, 38020, 38040, 38060, 38080]
        t = self._make_tracker("LONG", prices)
        # Price rose — not adverse
        assert t.adverse_move_5min() < 0

    def test_short_adverse_rise(self):
        prices = [38000, 38010, 38020, 38030, 38040]
        t = self._make_tracker("SHORT", prices)
        # current - reference = 38040 - 38000 = 40
        assert t.adverse_move_5min() == pytest.approx(40.0)

    def test_single_reading_returns_zero(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38000)
        assert t.adverse_move_5min() == 0.0

    def test_lookback_capped_at_5(self):
        # Add 10 prices; only last 5 should be used
        t = MomentumTracker("LONG", 38000)
        for i in range(10):
            t.add_price(38000 + i * 10)  # Rising prices — favorable for long
        # Last 5: 38050, 38060, 38070, 38080, 38090
        # adverse = 38050 - 38090 = -40 (favorable)
        assert t.adverse_move_5min() < 0


class TestGetAdverseTier:
    def _tracker_with_drop(self, direction, drop):
        entry = 38000
        t = MomentumTracker(direction, entry)
        # Add 5 readings: start at entry, end at entry ∓ drop (adverse direction)
        if direction == "LONG":
            t.add_price(entry)
            for _ in range(4):
                t.add_price(entry - drop)
        else:
            t.add_price(entry)
            for _ in range(4):
                t.add_price(entry + drop)
        return t

    def test_no_adverse_move(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38000)
        assert t.get_adverse_tier() == TIER_NONE

    def test_favorable_move_is_none(self):
        t = MomentumTracker("LONG", 38000)
        for p in [38000, 38010, 38020, 38030, 38040]:
            t.add_price(p)
        assert t.get_adverse_tier() == TIER_NONE

    def test_mild_tier(self):
        t = self._tracker_with_drop("LONG", ADVERSE_MILD_PTS)
        assert t.get_adverse_tier() == TIER_MILD

    def test_moderate_tier(self):
        t = self._tracker_with_drop("LONG", ADVERSE_MODERATE_PTS)
        assert t.get_adverse_tier() == TIER_MODERATE

    def test_severe_tier(self):
        t = self._tracker_with_drop("LONG", ADVERSE_SEVERE_PTS)
        assert t.get_adverse_tier() == TIER_SEVERE

    def test_short_mild_tier(self):
        t = self._tracker_with_drop("SHORT", ADVERSE_MILD_PTS)
        assert t.get_adverse_tier() == TIER_MILD


class TestShouldAlert:
    def _tracker_at_tier(self, direction, drop):
        entry = 38000
        t = MomentumTracker(direction, entry)
        t.add_price(entry)
        for _ in range(4):
            target = entry - drop if direction == "LONG" else entry + drop
            t.add_price(target)
        return t

    def test_no_alert_at_tier_none(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38000)
        should, tier, msg = t.should_alert()
        assert should is False

    def test_first_mild_alert_fires(self):
        t = self._tracker_at_tier("LONG", ADVERSE_MILD_PTS)
        should, tier, msg = t.should_alert()
        assert should is True
        assert tier == TIER_MILD

    def test_repeated_mild_does_not_re_alert(self):
        t = self._tracker_at_tier("LONG", ADVERSE_MILD_PTS)
        t.should_alert()  # First call fires
        should, tier, msg = t.should_alert()  # Second — same tier
        assert should is False

    def test_escalation_from_mild_to_moderate_fires(self):
        t = self._tracker_at_tier("LONG", ADVERSE_MILD_PTS)
        t.should_alert()  # Consume mild alert

        # Now escalate to moderate
        t2 = MomentumTracker("LONG", 38000)
        t2._last_alerted_tier = TIER_MILD
        t2.add_price(38000)
        for _ in range(4):
            t2.add_price(38000 - ADVERSE_MODERATE_PTS)
        should, tier, msg = t2.should_alert()
        assert should is True
        assert tier == TIER_MODERATE

    def test_severe_always_alerts(self):
        # Severe should alert even if last_alerted_tier is already SEVERE
        t = self._tracker_at_tier("LONG", ADVERSE_SEVERE_PTS)
        t._last_alerted_tier = TIER_SEVERE
        should, tier, msg = t.should_alert()
        assert should is True
        assert tier == TIER_SEVERE

    def test_message_contains_direction_word(self):
        t = self._tracker_at_tier("LONG", ADVERSE_MILD_PTS)
        _, _, msg = t.should_alert()
        assert "dropped" in msg  # LONG adverse move = price dropped

    def test_short_message_direction_word(self):
        t = self._tracker_at_tier("SHORT", ADVERSE_MILD_PTS)
        _, _, msg = t.should_alert()
        assert "risen" in msg  # SHORT adverse move = price risen


class TestIsStale:
    def test_not_stale_with_varied_prices(self):
        t = MomentumTracker("LONG", 38000)
        for i in range(STALE_DATA_THRESHOLD):
            t.add_price(38000 + i)
        assert t.is_stale() is False

    def test_stale_with_identical_prices(self):
        t = MomentumTracker("LONG", 38000)
        for _ in range(STALE_DATA_THRESHOLD):
            t.add_price(38000)
        assert t.is_stale() is True

    def test_not_stale_below_threshold(self):
        t = MomentumTracker("LONG", 38000)
        for _ in range(STALE_DATA_THRESHOLD - 1):
            t.add_price(38000)
        assert t.is_stale() is False

    def test_stale_detection_on_recent_only(self):
        # First 5 readings varied, last STALE_DATA_THRESHOLD identical
        t = MomentumTracker("LONG", 38000)
        for i in range(5):
            t.add_price(38000 + i * 10)
        for _ in range(STALE_DATA_THRESHOLD):
            t.add_price(38100)  # Identical
        assert t.is_stale() is True


class TestMilestoneAlert:
    def test_no_milestone_below_first(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38100)  # +100 pts, below first milestone of 150
        assert t.milestone_alert() is None

    def test_first_milestone_at_150(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38150)
        msg = t.milestone_alert()
        assert msg is not None
        assert "150" in msg

    def test_milestone_fires_once(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38150)
        t.milestone_alert()  # Fires
        assert t.milestone_alert() is None  # Does not fire again

    def test_multiple_milestones_fire_sequentially(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(38200)
        msg150 = t.milestone_alert()  # 200pts > 150 and 200
        # First call should return the first milestone hit
        assert msg150 is not None

    def test_short_milestone(self):
        t = MomentumTracker("SHORT", 38000)
        t.add_price(37850)  # +150 pts for short
        msg = t.milestone_alert()
        assert msg is not None
        assert "150" in msg

    def test_no_milestone_on_losing_position(self):
        t = MomentumTracker("LONG", 38000)
        t.add_price(37500)  # -500 pts loss
        assert t.milestone_alert() is None


class TestResetAlertState:
    def test_reset_clears_tier(self):
        t = MomentumTracker("LONG", 38000)
        t._last_alerted_tier = TIER_MODERATE
        t.reset_alert_state()
        assert t._last_alerted_tier == TIER_NONE


class TestMaxPriceBuffer:
    def test_buffer_capped_at_120(self):
        """Buffer holds 120 readings = 1 hour at 30s monitoring interval."""
        t = MomentumTracker("LONG", 38000)
        for i in range(130):
            t.add_price(38000 + i)
        assert len(t._prices) == 120
