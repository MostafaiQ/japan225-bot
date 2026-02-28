"""
POST /api/controls/force-scan  — write trigger file; monitor picks it up
POST /api/controls/restart     — sudo systemctl restart japan225-bot
POST /api/controls/stop        — sudo systemctl stop japan225-bot
POST /api/apply-fix            — apply unified diff, commit, push
"""
import subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.services import db_reader

router = APIRouter()

TRIGGER_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "force_scan.trigger"


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
    return {"ok": True, "message": "Scan trigger written. Bot will scan at next cycle check."}


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
