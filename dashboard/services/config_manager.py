"""
Manages dashboard_overrides.json — atomic reads and writes.
Two-tier system:
  hot    → applied immediately by monitor.py._reload_overrides()
  restart → requires bot restart (blocked if position open)
"""
import json
import os
from pathlib import Path
from config import settings as S

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


def _defaults() -> dict:
    """Read baseline values live from settings.py — never a stale hardcoded copy."""
    return {
        "MIN_CONFIDENCE":         S.MIN_CONFIDENCE,
        "MIN_CONFIDENCE_SHORT":   S.MIN_CONFIDENCE_SHORT,
        "AI_COOLDOWN_MINUTES":    S.AI_COOLDOWN_MINUTES,
        "SCAN_INTERVAL_SECONDS":  S.SCAN_INTERVAL_SECONDS,
        "DEBUG":                  False,
        "scanning_paused":        False,
        "BREAKEVEN_TRIGGER":      S.BREAKEVEN_TRIGGER,
        "TRAILING_STOP_DISTANCE": S.TRAILING_STOP_DISTANCE,
        "DEFAULT_SL_DISTANCE":    S.DEFAULT_SL_DISTANCE,
        "DEFAULT_TP_DISTANCE":    S.DEFAULT_TP_DISTANCE,
        "MAX_MARGIN_PERCENT":     S.MAX_MARGIN_PERCENT,
        "TRADING_MODE":           S.TRADING_MODE,
    }


def read_overrides() -> dict:
    """Return settings.py values as base, with dashboard_overrides.json on top."""
    overrides = {}
    try:
        if OVERRIDES_PATH.exists():
            with open(OVERRIDES_PATH) as f:
                overrides = json.load(f)
    except Exception:
        pass
    return {**_defaults(), **overrides}


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
