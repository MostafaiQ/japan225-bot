"""
GET /api/health  — unauthenticated ping
GET /api/status  — full bot state for Overview tab
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from dashboard.services import db_reader

router = APIRouter()

STATE_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "bot_state.json"
CHAT_COSTS_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "chat_costs.json"


def _chat_tokens_today() -> int:
    """Estimate chat tokens used today from chat_costs.json."""
    try:
        if not CHAT_COSTS_PATH.exists():
            return 0
        data = json.loads(CHAT_COSTS_PATH.read_text())
        if not isinstance(data, list):
            return 0
        from datetime import date
        today = date.today().isoformat()
        total = 0
        for e in data:
            if e.get("ts", "").startswith(today):
                total += e.get("est_tokens", (e.get("input_chars", 0) + e.get("output_chars", 0)) // 4)
        return total
    except Exception:
        return 0


def _next_scan_in(state: dict) -> int | None:
    """Compute live countdown (seconds) from next_scan_at ISO timestamp."""
    ts = state.get("next_scan_at")
    if not ts:
        return None
    try:
        target = datetime.fromisoformat(ts)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0, int((target - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return None


def _read_state() -> dict:
    """Read the state file written by monitor.py each cycle."""
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception:
        pass
    return {}


@router.get("/api/health")
async def health():
    return {"status": "ok"}


@router.get("/api/status")
async def status():
    state = _read_state()
    position = db_reader.get_position()

    # Enrich position with live price/pnl from state file
    if position and state.get("current_price"):
        cp = float(state["current_price"])
        ep = float(position.get("entry_price") or 0)
        direction = position.get("direction", "LONG")
        pnl = (cp - ep) if direction == "LONG" else (ep - cp)
        position["current_price"]    = cp
        position["unrealised_pnl"]   = round(pnl, 1)

    scans = db_reader.get_recent_scans(10)

    # Uptime from state or started_at
    uptime = state.get("uptime", "—")
    if not uptime and state.get("started_at"):
        try:
            started = datetime.fromisoformat(state["started_at"])
            mins = int((datetime.now() - started).total_seconds() / 60)
            h, m = divmod(mins, 60)
            uptime = f"{h}h {m}m"
        except Exception:
            uptime = "—"

    return {
        "session":          state.get("session", "—"),
        "phase":            state.get("phase", "SCANNING" if not position else "MONITORING"),
        "scanning_paused":  state.get("scanning_paused", False),
        "last_scan":        state.get("last_scan"),
        "next_scan_in":     _next_scan_in(state),
        "last_scan_detail": state.get("last_scan_detail"),
        "ai_calls_today":   db_reader.get_ai_calls_today(),
        "tokens_today":     db_reader.get_tokens_today(),
        "chat_tokens_today": _chat_tokens_today(),
        "uptime":           uptime,
        "position":         position,
        "recent_scans":     scans,
        "db_connected":     db_reader.db_exists(),
    }
