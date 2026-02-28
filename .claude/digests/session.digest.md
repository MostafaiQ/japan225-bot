# core/session.py — DIGEST
# Purpose: Session detection and no-trade-day checks. All logic in UTC.
# Note: settings.py SESSIONS dict is Kuwait reference only. This file is authoritative.

## Session boundaries (UTC)
Tokyo: 00:00-06:00  London: 08:00-16:00  New York: 13:30-21:00
London/NY overlap: 13:30-16:00 (detected first, highest priority)
Gap (Tokyo-London): 06:00-08:00  active=False
Off-hours: 21:00-00:00  active=False
Weekend: Sat always, Fri after 21:00 UTC, Sun before 21:00 UTC

## Functions

utcnow() -> datetime (UTC-aware)

get_current_session() -> dict
  # Returns: {name, priority (HIGH/MEDIUM/OFF), active: bool, description}
  # Names: tokyo, london, new_york, london_ny_overlap, gap_tokyo_london, off_hours

is_active_session() -> bool  # True if major session (active=True)

is_weekend() -> bool  # Fri 21:00 UTC to Sun 21:00 UTC

is_friday_blackout(upcoming_events=None) -> (bool, str)
  # Default window: 12:00-16:00 UTC (covers NFP)
  # Also blocks on FRIDAY_BLOCK_KEYWORDS: NFP, Non-Farm, CPI, PPI, BOJ, FOMC, Rate Decision

is_month_end_blackout() -> (bool, str)
  # Last MONTHEND_BLACKOUT_DAYS=2 trading days of month

is_no_trade_day(upcoming_events=None) -> (bool, str)
  # Master check. Combines: is_weekend + is_friday_blackout + is_month_end_blackout

get_scan_interval(session=None) -> int
  # Returns SCAN_INTERVAL_SECONDS (300) if active, else OFFHOURS_INTERVAL_SECONDS (1800)

seconds_until_next_session() -> int
  # Sessions open at: Tokyo 00:00, London 08:00, NY 13:30 UTC
