"""
GET /api/stream — Server-Sent Events endpoint.

Pushes real-time updates to the dashboard frontend, replacing setInterval polling.

Event types:
  - state_update : bot_state.json changed (overview data)
  - new_logs     : new journal entries appeared
  - keep_alive   : ping every 15s to prevent connection timeout

Auth: Bearer token required (same as all other endpoints).
"""
import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from dashboard.services import db_reader

log = logging.getLogger(__name__)

router = APIRouter()

STATE_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "bot_state.json"
CHAT_COSTS_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "chat_costs.json"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SERVICE = "japan225-bot"


def _read_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception:
        pass
    return {}


def _chat_tokens_today() -> int:
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


def _next_scan_in(state: dict):
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


def _enrich_position_from_state(position: dict, state: dict) -> dict:
    """Add live price/pnl using state file price (no IG API call)."""
    cp = float(state.get("current_price", position.get("entry_price", 0)))
    ep = float(position.get("entry_price") or 0)
    lots = float(position.get("size") or 0)
    direction = position.get("direction", "LONG")
    pnl_pts = (cp - ep) if direction == "LONG" else (ep - cp)
    pnl_dollars = pnl_pts * lots
    position["current_price"] = cp
    position["unrealised_pnl"] = round(pnl_dollars, 2)
    position["unrealised_pnl_pts"] = round(pnl_pts, 1)
    return position


def _build_status() -> dict:
    """Build the same status payload as /api/status (without live IG price call for perf)."""
    state = _read_state()
    positions = db_reader.get_positions()

    for pos in positions:
        _enrich_position_from_state(pos, state)

    position = positions[0] if positions else None

    scans = db_reader.get_recent_scans(50)

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
        "positions":        positions,
        "recent_scans":     scans,
        "db_connected":     db_reader.db_exists(),
    }


def _get_log_lines(log_type: str = "scan", lines: int = 70) -> list[str]:
    """Fetch journal lines (same logic as logs router)."""
    cmd = [
        "journalctl", "-u", SERVICE,
        f"-n{lines * 3 if log_type == 'scan' else lines}",
        "--no-pager",
        "--output=short-iso",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        raw = result.stdout or result.stderr or ""
        out = [ANSI_RE.sub("", l) for l in raw.splitlines() if l.strip()]
        if log_type == "scan":
            pattern = re.compile(
                r"SCAN|SETUP|SIGNAL|TRADE|ALERT|CONFIRM|PHASE|MOMENTUM|ERROR|WARN|CONFIDENCE"
                r"|HAIKU|SONNET|OPUS|REJECTED|APPROVED|COOLDOWN|ESCALAT|PRE-SCREEN|SCREEN:|BLOCK",
                re.IGNORECASE,
            )
            out = [l for l in out if pattern.search(l)]
            out = out[-lines:]
        return out
    except Exception:
        return []


def _sse_event(event: str, data: dict) -> str:
    """Format a single SSE event."""
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def _event_generator(request: Request):
    """Async generator that yields SSE events."""
    last_state_mtime = 0.0
    last_log_hash = ""
    tick = 0

    # Send initial state immediately
    try:
        status = _build_status()
        yield _sse_event("state_update", status)
    except Exception as e:
        log.warning("SSE initial state_update failed: %s", e)

    try:
        logs = _get_log_lines()
        log_hash = str(hash(tuple(logs[-10:]))) if logs else ""
        last_log_hash = log_hash
        yield _sse_event("new_logs", {"lines": logs, "type": "scan"})
    except Exception as e:
        log.warning("SSE initial new_logs failed: %s", e)

    while True:
        # Check if client disconnected
        if await request.is_disconnected():
            break

        await asyncio.sleep(3)  # Check every 3 seconds
        tick += 1

        try:
            # Check bot_state.json mtime for changes
            try:
                current_mtime = STATE_PATH.stat().st_mtime if STATE_PATH.exists() else 0.0
            except OSError:
                current_mtime = 0.0

            if current_mtime != last_state_mtime:
                last_state_mtime = current_mtime
                status = _build_status()
                yield _sse_event("state_update", status)

            # Check for new log entries every ~9 seconds (tick % 3)
            if tick % 3 == 0:
                logs = _get_log_lines()
                log_hash = str(hash(tuple(logs[-10:]))) if logs else ""
                if log_hash != last_log_hash:
                    last_log_hash = log_hash
                    yield _sse_event("new_logs", {"lines": logs, "type": "scan"})

            # Keep-alive ping every ~15 seconds (tick % 5)
            if tick % 5 == 0:
                yield _sse_event("keep_alive", {"ts": datetime.now(timezone.utc).isoformat()})

        except Exception as e:
            log.warning("SSE event loop error: %s", e)
            # Send error event but keep connection alive
            yield _sse_event("error", {"message": str(e)})
            await asyncio.sleep(5)


@router.get("/api/stream")
async def stream(request: Request):
    """SSE endpoint. Streams state_update, new_logs, and keep_alive events."""
    return StreamingResponse(
        _event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx/proxy buffering
        },
    )
