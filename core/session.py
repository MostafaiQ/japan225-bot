"""
Session awareness for Japan 225 Trading Bot.
All times in UTC. Kuwait = UTC+3 (for reference only, logic runs on UTC).

Trading sessions (UTC):
  Tokyo:    00:00 - 06:00
  London:   08:00 - 16:00
  New York: 13:30 - 21:00  (14:30 for actual NY open but 13:30 catches pre-market)

Off-hours (UTC): 06:00-08:00 (Tokyo-London gap), 21:00-00:00 (after NY)
Weekend: Saturday 21:00 UTC to Sunday 21:00 UTC (IG Japan 225 opens Sunday ~21:00)
"""
import calendar
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Session definitions in UTC
SESSIONS_UTC = {
    "tokyo":    {"start": (0, 0),  "end": (6, 0),  "priority": "HIGH"},
    "london":   {"start": (8, 0),  "end": (16, 0), "priority": "HIGH"},
    "new_york": {"start": (13, 30), "end": (21, 0), "priority": "HIGH"},
}

# Gap periods — market open but lower activity
GAP_SESSIONS_UTC = {
    "tokyo_london_gap": {"start": (6, 0),  "end": (8, 0)},
    "after_ny":         {"start": (21, 0), "end": (24, 0)},
    "pre_tokyo":        {"start": (0, 0),  "end": (0, 0)},  # placeholder for midnight wrap
}

# High-impact event keywords that always trigger a no-trade on Friday
FRIDAY_BLOCK_KEYWORDS = ["NFP", "Non-Farm", "CPI", "PPI", "BOJ", "FOMC", "Rate Decision"]

# Months where month-end rebalancing is strongest (all months, but especially quarter-end)
MONTHEND_BLACKOUT_DAYS = 2  # Last 2 trading days of month


def utcnow() -> datetime:
    """Current time in UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def get_current_session() -> dict:
    """
    Determine the current trading session based on UTC time.

    Returns dict with:
        name: str (tokyo / london / new_york / gap / off_hours)
        priority: HIGH / MEDIUM / OFF
        active: bool  (True if in a major session)
        description: str
    """
    now = utcnow()
    hour = now.hour
    minute = now.minute
    current_minutes = hour * 60 + minute

    # London/NY overlap (13:30-16:00 UTC) — check before individual sessions
    # because London is matched first in the loop and would mask the overlap.
    london_ny_start = 13 * 60 + 30  # 13:30
    london_ny_end = 16 * 60          # 16:00
    if london_ny_start <= current_minutes < london_ny_end:
        return {
            "name": "london_ny_overlap",
            "priority": "HIGH",
            "active": True,
            "description": "London/NY overlap — highest liquidity",
        }

    # Check major sessions
    for session_name, info in SESSIONS_UTC.items():
        start_min = info["start"][0] * 60 + info["start"][1]
        end_min = info["end"][0] * 60 + info["end"][1]
        if start_min <= current_minutes < end_min:
            return {
                "name": session_name,
                "priority": info["priority"],
                "active": True,
                "description": f"{session_name.replace('_', ' ').title()} session",
            }

    # Gap period
    if 6 * 60 <= current_minutes < 8 * 60:
        return {
            "name": "gap_tokyo_london",
            "priority": "MEDIUM",
            "active": False,
            "description": "Tokyo-London gap — reduced liquidity",
        }

    # Off-hours (21:00-00:00 UTC)
    return {
        "name": "off_hours",
        "priority": "OFF",
        "active": False,
        "description": "Off-hours — no new entries",
    }


def is_active_session() -> bool:
    """Returns True if currently in a major trading session (Tokyo, London, NY)."""
    session = get_current_session()
    return session["active"]


def is_weekend() -> bool:
    """
    Returns True if markets are closed for the weekend.
    Japan 225 on IG closes Friday 21:00 UTC and reopens Sunday ~21:00 UTC.
    """
    now = utcnow()
    weekday = now.weekday()  # 0=Monday, 5=Saturday, 6=Sunday
    current_minutes = now.hour * 60 + now.minute

    # Saturday: always closed
    if weekday == 5:
        return True

    # Friday after 21:00 UTC: closed
    if weekday == 4 and current_minutes >= 21 * 60:
        return True

    # Sunday before 21:00 UTC: still closed
    if weekday == 6 and current_minutes < 21 * 60:
        return True

    return False


def is_friday_blackout(upcoming_events: list = None) -> tuple[bool, str]:
    """
    Check if we're in the Friday high-impact blackout window.

    The default window (12:00-16:00 UTC) covers NFP and most US data releases.
    Also blocks if upcoming_events contains known high-impact keywords, even
    outside the default window.

    Returns (blocked: bool, reason: str)
    """
    now = utcnow()
    if now.weekday() != 4:  # Not Friday
        return False, ""

    current_minutes = now.hour * 60 + now.minute
    blackout_start = 12 * 60  # 12:00 UTC
    blackout_end = 16 * 60    # 16:00 UTC

    if blackout_start <= current_minutes <= blackout_end:
        return True, f"Friday NFP/data window (12:00-16:00 UTC)"

    # Check calendar for keywords outside default window
    if upcoming_events:
        for event in upcoming_events:
            name = event.get("name", "").upper()
            if any(kw.upper() in name for kw in FRIDAY_BLOCK_KEYWORDS):
                return True, f"Friday: {event.get('name', 'high-impact event')} scheduled"

    return False, ""


def is_month_end_blackout() -> tuple[bool, str]:
    """
    Returns True if we're in the month-end rebalancing zone
    (last 2 trading days of the month).
    """
    now = utcnow()
    last_day = calendar.monthrange(now.year, now.month)[1]
    days_left = last_day - now.day

    # Count trading days remaining (rough: subtract weekends)
    trading_days_left = 0
    for i in range(1, days_left + 1):
        check_date = now.date().replace(day=now.day + i)
        if check_date.weekday() < 5:  # Not weekend
            trading_days_left += 1

    if trading_days_left <= MONTHEND_BLACKOUT_DAYS:
        return True, f"Month-end rebalancing zone ({trading_days_left} trading days to EOM)"

    return False, ""


def is_no_trade_day(upcoming_events: list = None) -> tuple[bool, str]:
    """
    Master check: should we trade at all today?

    Returns (blocked: bool, reason: str)
    Checks: weekend, Friday blackout, month-end.
    """
    if is_weekend():
        return True, "Weekend — market closed"

    friday_blocked, friday_reason = is_friday_blackout(upcoming_events)
    if friday_blocked:
        return True, friday_reason

    monthend_blocked, monthend_reason = is_month_end_blackout()
    if monthend_blocked:
        return True, monthend_reason

    return False, ""


def get_scan_interval(session: dict = None) -> int:
    """
    Return the appropriate scan interval in seconds based on session.
    Active sessions: 300s (5 min)
    Off-hours: 1800s (30 min)
    """
    from config.settings import SCAN_INTERVAL_SECONDS, OFFHOURS_INTERVAL_SECONDS
    if session is None:
        session = get_current_session()
    return SCAN_INTERVAL_SECONDS if session["active"] else OFFHOURS_INTERVAL_SECONDS


def seconds_until_next_session() -> int:
    """
    How many seconds until the next active trading session opens.
    Used to calculate how long to sleep during off-hours.
    """
    now = utcnow()
    current_minutes = now.hour * 60 + now.minute

    # Next session opens: Tokyo at 00:00, London at 08:00, NY at 13:30
    session_opens = [0 * 60, 8 * 60, 13 * 60 + 30]

    for open_min in sorted(session_opens):
        if open_min > current_minutes:
            minutes_to_wait = open_min - current_minutes
            return minutes_to_wait * 60

    # Past NY open — next is Tokyo tomorrow at 00:00
    minutes_to_midnight = 24 * 60 - current_minutes
    return minutes_to_midnight * 60
