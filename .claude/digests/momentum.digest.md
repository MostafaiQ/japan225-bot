# core/momentum.py — DIGEST
# Purpose: Track price history for open position, detect adverse moves, milestones, stale data.

## Constants (module-level)
TIER_NONE = "none"  TIER_MILD = "mild"  TIER_MODERATE = "moderate"  TIER_SEVERE = "severe"

## class MomentumTracker
__init__(direction: str, entry_price: float)
  # direction = "LONG" or "SHORT". Keeps last 60 price readings.

add_price(price: float)
  # Call every monitoring cycle (60s). Keeps rolling window of 60 readings.

current_pnl_points() -> float
  # LONG: current - entry. SHORT: entry - current

adverse_move_5min() -> float
  # How far price moved AGAINST position in last 5 readings
  # Positive = bad (against us). Negative = good (in our favor).

get_adverse_tier() -> str
  # Based on adverse_move_5min(): NONE <30, MILD 30-50, MODERATE 50-80, SEVERE 80+

should_alert() -> (bool, str, str)  # (should_alert, tier, message)
  # Only alerts when tier WORSENS (de-dup). Resets when conditions improve.
  # SEVERE messages say "Auto-protecting: moving SL to breakeven"
  # NOTE: actual SL move is done in monitor.py._monitoring_cycle(), not here

is_stale() -> bool
  # True if last STALE_DATA_THRESHOLD=10 readings are identical (API/market issue)

reset_alert_state()
  # Call after phase change (e.g., moved to breakeven) to reset alert de-dup

milestone_alert() -> Optional[str]
  # Fires once at: +150, +200, +250, +300, +400, +500 pts. One-shot (uses getattr flags).

get_summary() -> dict
  # {direction, entry, current, pnl_points, adverse_5min, tier, stale, readings}
