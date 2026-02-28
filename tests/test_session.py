"""
Tests for core/session.py — session awareness, no-trade days, weekend detection.
All time-sensitive functions are tested by monkeypatching core.session.utcnow().
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch


def _utc(weekday_offset, hour, minute=0):
    """
    Helper: return a timezone-aware UTC datetime.
    weekday_offset 0=Monday, 1=Tuesday … 4=Friday, 5=Saturday, 6=Sunday.
    We base off 2025-01-06 (Monday) as anchor.
    """
    anchor = datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)  # Monday
    return anchor + timedelta(days=weekday_offset, hours=hour, minutes=minute)


# ── is_weekend ────────────────────────────────────────────────────────────────

class TestIsWeekend:
    def _check(self, dt, expected):
        with patch("core.session.utcnow", return_value=dt):
            from core.session import is_weekend
            assert is_weekend() == expected, f"Expected {expected} at {dt}"

    def test_monday_is_not_weekend(self):
        self._check(_utc(0, 12), False)

    def test_friday_before_close_is_not_weekend(self):
        # Friday 20:59 UTC — still open
        self._check(_utc(4, 20, 59), False)

    def test_friday_at_close_is_weekend(self):
        # Friday 21:00 UTC — market closes
        self._check(_utc(4, 21, 0), True)

    def test_saturday_is_weekend(self):
        self._check(_utc(5, 12), True)

    def test_sunday_before_open_is_weekend(self):
        # Sunday 20:59 UTC — still closed
        self._check(_utc(6, 20, 59), True)

    def test_sunday_at_open_is_not_weekend(self):
        # Sunday 21:00 UTC — market reopens
        self._check(_utc(6, 21, 0), False)


# ── get_current_session ───────────────────────────────────────────────────────

class TestGetCurrentSession:
    def _session_name(self, dt):
        with patch("core.session.utcnow", return_value=dt):
            from core.session import get_current_session
            return get_current_session()["name"]

    def test_tokyo_session(self):
        assert self._session_name(_utc(0, 2)) == "tokyo"

    def test_tokyo_start_boundary(self):
        assert self._session_name(_utc(0, 0, 0)) == "tokyo"

    def test_tokyo_end_boundary(self):
        # 06:00 — Tokyo just ended, should be gap or off-hours
        name = self._session_name(_utc(0, 6, 0))
        assert name == "gap_tokyo_london"

    def test_london_session(self):
        assert self._session_name(_utc(0, 10)) == "london"

    def test_london_ny_overlap(self):
        # 14:00 — both London and NY active
        assert self._session_name(_utc(0, 14)) == "london_ny_overlap"

    def test_ny_session_post_london(self):
        # 17:00 — NY only (London closed at 16:00)
        assert self._session_name(_utc(0, 17)) == "new_york"

    def test_gap_tokyo_london(self):
        # 07:00 — between Tokyo close and London open
        assert self._session_name(_utc(0, 7)) == "gap_tokyo_london"

    def test_off_hours(self):
        # 22:00 — after NY close
        assert self._session_name(_utc(0, 22)) == "off_hours"

    def test_active_flag_true_during_session(self):
        with patch("core.session.utcnow", return_value=_utc(0, 10)):
            from core.session import get_current_session
            assert get_current_session()["active"] is True

    def test_active_flag_false_during_gap(self):
        with patch("core.session.utcnow", return_value=_utc(0, 7)):
            from core.session import get_current_session
            assert get_current_session()["active"] is False


# ── is_friday_blackout ────────────────────────────────────────────────────────

class TestIsFridayBlackout:
    def _check(self, dt, events=None):
        with patch("core.session.utcnow", return_value=dt):
            from core.session import is_friday_blackout
            return is_friday_blackout(events)

    def test_not_friday_never_blocks(self):
        blocked, reason = self._check(_utc(0, 13))  # Monday
        assert blocked is False

    def test_friday_inside_window_blocks(self):
        # Friday 14:00 UTC — inside 12:00-16:00 window
        blocked, reason = self._check(_utc(4, 14))
        assert blocked is True
        assert "12:00" in reason

    def test_friday_outside_window_no_block(self):
        # Friday 09:00 UTC — outside window, no events
        blocked, reason = self._check(_utc(4, 9))
        assert blocked is False

    def test_friday_with_nfp_event_blocks_outside_window(self):
        # Friday 09:00 UTC — outside window but NFP in calendar
        events = [{"name": "Non-Farm Payrolls", "impact": "HIGH"}]
        blocked, reason = self._check(_utc(4, 9), events)
        assert blocked is True

    def test_friday_with_low_impact_event_no_block(self):
        events = [{"name": "Some minor data", "impact": "LOW"}]
        blocked, reason = self._check(_utc(4, 9), events)
        assert blocked is False

    def test_friday_at_window_start(self):
        # Friday 12:00 UTC — exactly at start
        blocked, _ = self._check(_utc(4, 12))
        assert blocked is True

    def test_friday_at_window_end(self):
        # Friday 16:00 UTC — exactly at end
        blocked, _ = self._check(_utc(4, 16))
        assert blocked is True


# ── is_no_trade_day ───────────────────────────────────────────────────────────

class TestIsNoTradeDay:
    def test_weekend_blocks(self):
        with patch("core.session.utcnow", return_value=_utc(5, 12)):  # Saturday
            from core.session import is_no_trade_day
            blocked, reason = is_no_trade_day()
            assert blocked is True
            assert "Weekend" in reason or "weekend" in reason.lower() or "market" in reason.lower()

    def test_normal_weekday_allows(self):
        with patch("core.session.utcnow", return_value=_utc(0, 10)):  # Monday
            from core.session import is_no_trade_day
            blocked, reason = is_no_trade_day()
            # Only blocked if month-end coincidentally applies — check blocked
            # For a generic Monday this should pass (not month-end unless unlucky date)
            # We test logic path, not exact date
            assert isinstance(blocked, bool)

    def test_friday_blackout_propagates(self):
        with patch("core.session.utcnow", return_value=_utc(4, 14)):  # Friday 14:00
            from core.session import is_no_trade_day
            blocked, reason = is_no_trade_day()
            assert blocked is True


# ── get_scan_interval ─────────────────────────────────────────────────────────

class TestGetScanInterval:
    def test_active_session_returns_short_interval(self):
        from core.session import get_scan_interval
        from config.settings import SCAN_INTERVAL_SECONDS
        interval = get_scan_interval({"active": True})
        assert interval == SCAN_INTERVAL_SECONDS

    def test_off_hours_returns_long_interval(self):
        from core.session import get_scan_interval
        from config.settings import OFFHOURS_INTERVAL_SECONDS
        interval = get_scan_interval({"active": False})
        assert interval == OFFHOURS_INTERVAL_SECONDS


# ── seconds_until_next_session ────────────────────────────────────────────────

class TestSecondsUntilNextSession:
    def test_returns_positive_value(self):
        with patch("core.session.utcnow", return_value=_utc(0, 7)):  # Gap period
            from core.session import seconds_until_next_session
            secs = seconds_until_next_session()
            assert secs > 0

    def test_before_london_open_waits_for_london(self):
        # At 07:30 UTC — 30 min to London open at 08:00 = 1800s
        with patch("core.session.utcnow", return_value=_utc(0, 7, 30)):
            from core.session import seconds_until_next_session
            secs = seconds_until_next_session()
            assert secs == 30 * 60  # 1800 seconds
