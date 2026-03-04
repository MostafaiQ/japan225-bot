"""
POST /api/controls/force-scan  — write trigger file + SIGUSR1; monitor wakes immediately
POST /api/controls/restart     — sudo systemctl restart japan225-bot
POST /api/controls/stop        — sudo systemctl stop japan225-bot
POST /api/apply-fix            — apply unified diff, commit, push
"""
import json
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.services import db_reader

router = APIRouter()

TRIGGER_PATH         = Path(__file__).parent.parent.parent / "storage" / "data" / "force_scan.trigger"
CLEAR_CD_PATH        = Path(__file__).parent.parent.parent / "storage" / "data" / "clear_cooldown.trigger"
FORCE_OPEN_PENDING   = Path(__file__).parent.parent.parent / "storage" / "data" / "force_open_pending.json"
FORCE_OPEN_TRIGGER   = Path(__file__).parent.parent.parent / "storage" / "data" / "force_open.trigger"
POS_CHECK_TRIGGER    = Path(__file__).parent.parent.parent / "storage" / "data" / "pos_check.trigger"


def _signal_monitor():
    """Send SIGUSR1 to the monitor process to wake main loop immediately."""
    try:
        r = subprocess.run(
            ["systemctl", "show", "japan225-bot", "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        pid = int(r.stdout.strip())
        if pid > 0:
            os.kill(pid, signal.SIGUSR1)
    except Exception:
        pass  # Fallback: poll task picks up trigger file within 2s


def _systemctl(action: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "/bin/systemctl", action, "japan225-bot"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


@router.post("/api/controls/force-scan")
async def force_scan():
    TRIGGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRIGGER_PATH.touch()
    _signal_monitor()
    return {"ok": True, "message": "Force scan triggered."}


@router.post("/api/controls/pos-check")
async def pos_check():
    pos = db_reader.get_position()
    if not pos:
        return {"ok": False, "warning": "No open position."}
    POS_CHECK_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    POS_CHECK_TRIGGER.touch()
    _signal_monitor()
    return {"ok": True, "message": "Position check running — result sent to Telegram."}


@router.post("/api/controls/clear-cooldown")
async def clear_cooldown():
    """Write trigger file — monitor will clear AI cooldown at next cycle."""
    CLEAR_CD_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLEAR_CD_PATH.touch()
    TRIGGER_PATH.touch()   # also force-scan so it takes effect immediately
    _signal_monitor()
    return {"ok": True, "message": "Cooldown cleared. Escalating to AI on next scan."}


@router.post("/api/controls/restart")
async def restart(force: bool = False):
    pos = db_reader.get_position()
    if pos and not force:
        return {
            "ok": False,
            "warning": "Position is open. Pass force=true to restart anyway.",
        }
    ok, msg = _systemctl("restart")
    if not ok:
        raise HTTPException(500, f"Restart failed: {msg}")
    return {"ok": True, "message": "Bot restarting…"}


@router.post("/api/controls/stop")
async def stop():
    pos = db_reader.get_position()
    if pos:
        return {
            "ok": False,
            "warning": "Position is open — stopping would leave it unmonitored. Close position first.",
        }
    ok, msg = _systemctl("stop")
    if not ok:
        raise HTTPException(500, f"Stop failed: {msg}")
    return {"ok": True, "message": "Bot stopped."}


# ── Force Open ───────────────────────────────────────────────────────────────

@router.get("/api/controls/force-open-pending")
async def get_force_open_pending():
    """Return the pending force-open setup if one exists and hasn't expired (15 min)."""
    try:
        if not FORCE_OPEN_PENDING.exists():
            return {"pending": None}
        data = json.loads(FORCE_OPEN_PENDING.read_text())
        ts = datetime.fromisoformat(data.get("timestamp", ""))
        age = (datetime.now() - ts).total_seconds()
        if age > 900:
            FORCE_OPEN_PENDING.unlink(missing_ok=True)
            return {"pending": None}
        return {"pending": data}
    except Exception:
        return {"pending": None}


@router.post("/api/controls/force-open")
async def force_open():
    """User confirmed force-open — write trigger for monitor to execute immediately."""
    if not FORCE_OPEN_PENDING.exists():
        raise HTTPException(404, "No pending force-open setup (expired or already executed)")
    try:
        data = json.loads(FORCE_OPEN_PENDING.read_text())
    except Exception:
        raise HTTPException(400, "Could not read pending setup data")
    FORCE_OPEN_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    FORCE_OPEN_TRIGGER.write_text(json.dumps(data))
    FORCE_OPEN_PENDING.unlink(missing_ok=True)
    return {"ok": True, "message": f"Force-open queued: {data.get('direction')} {data.get('setup_type')} @ {data.get('entry')}"}


# ── Apply Fix ─────────────────────────────────────────────────────────────────

class FixRequest(BaseModel):
    target: str
    diff: str


@router.post("/api/apply-fix")
async def apply_fix(body: FixRequest):
    if not body.target.strip() or not body.diff.strip():
        raise HTTPException(400, "target and diff are required")
    from dashboard.services.git_ops import apply_fix as _apply
    try:
        result = _apply(body.target, body.diff)
        return result
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
