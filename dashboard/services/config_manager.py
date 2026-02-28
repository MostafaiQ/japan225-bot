"""
Manages dashboard_overrides.json — atomic reads and writes.
Two-tier system:
  hot    → applied immediately by monitor.py._reload_overrides()
  restart → requires bot restart (blocked if position open)
"""
import json
import os
import tempfile
from pathlib import Path

OVERRIDES_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "dashboard_overrides.json"

# Hot-reload allowed keys
HOT_KEYS = {
    "MIN_CONFIDENCE", "MIN_CONFIDENCE_SHORT", "AI_COOLDOWN_MINUTES",
    "SCAN_INTERVAL_SECONDS", "DEBUG", "scanning_paused",
}
# Restart-required keys
RESTART_KEYS = {
    "BREAKEVEN_TRIGGER", "TRAILING_STOP_DISTANCE",
    "DEFAULT_SL_DISTANCE", "DEFAULT_TP_DISTANCE",
    "MAX_MARGIN_PERCENT", "TRADING_MODE",
}

# Default values (mirrors settings.py)
DEFAULTS = {
    "MIN_CONFIDENCE":         70,
    "MIN_CONFIDENCE_SHORT":   75,
    "AI_COOLDOWN_MINUTES":    30,
    "SCAN_INTERVAL_SECONDS":  300,
    "DEBUG":                  False,
    "scanning_paused":        False,
    "BREAKEVEN_TRIGGER":      150,
    "TRAILING_STOP_DISTANCE": 150,
    "DEFAULT_SL_DISTANCE":    200,
    "DEFAULT_TP_DISTANCE":    400,
    "MAX_MARGIN_PERCENT":     0.50,
    "TRADING_MODE":           "paper",
}


def read_overrides() -> dict:
    """Read current overrides, merged with defaults."""
    overrides = {}
    try:
        if OVERRIDES_PATH.exists():
            with open(OVERRIDES_PATH) as f:
                overrides = json.load(f)
    except Exception:
        pass
    return {**DEFAULTS, **overrides}


def write_overrides(updates: dict, tier: str) -> dict:
    """
    Validate and write override values atomically.
    Returns the updated full config.
    Raises ValueError for unknown keys or wrong tier.
    """
    allowed = HOT_KEYS if tier == "hot" else RESTART_KEYS
    bad = set(updates) - allowed
    if bad:
        raise ValueError(f"Keys not allowed for tier '{tier}': {bad}")

    current = read_overrides()
    current.update(updates)

    # Atomic write via temp file
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(current, f, indent=2)
    os.replace(tmp, OVERRIDES_PATH)

    return current
