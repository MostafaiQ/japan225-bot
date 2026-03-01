"""
GET /api/logs?type=scan|system&lines=N

scan   → journalctl for japan225-bot service (trading activity)
system → journalctl for japan225-bot service (all levels including errors)
Strips ANSI escape codes before returning.
"""
import re
import subprocess
from fastapi import APIRouter, Query

router = APIRouter()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SERVICE  = "japan225-bot"


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _journalctl(lines: int, grep: str = None) -> list[str]:
    cmd = [
        "journalctl", "-u", SERVICE,
        f"-n{lines}",
        "--no-pager",
        "--output=short-iso",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        raw = result.stdout or result.stderr or ""
        out = [_strip_ansi(l) for l in raw.splitlines() if l.strip()]
        if grep:
            pattern = re.compile(grep, re.IGNORECASE)
            out = [l for l in out if pattern.search(l)]
        return out
    except Exception as e:
        return [f"[log error] {e}"]


@router.get("/api/logs")
async def logs(
    type:  str = Query("scan",  pattern="^(scan|system)$"),
    lines: int = Query(70, ge=10, le=200),
):
    if type == "scan":
        # Filter for trading-relevant lines
        entries = _journalctl(lines * 3, grep=r"SCAN|SETUP|SIGNAL|TRADE|ALERT|CONFIRM|PHASE|MOMENTUM|ERROR|WARN|CONFIDENCE|HAIKU|SONNET|OPUS|REJECTED|APPROVED|COOLDOWN|ESCALAT|PRE-SCREEN|SCREEN:|BLOCK")
        entries = entries[-lines:]
    else:
        entries = _journalctl(lines)

    return {"lines": entries, "type": type}
