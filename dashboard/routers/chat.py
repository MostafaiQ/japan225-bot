"""
POST /api/chat               — start Claude chat job (returns job_id immediately)
GET  /api/chat/status/{id}   — poll job status (cheap, no AI; 4-8 s interval from client)
GET  /api/chat/history       — load shared chat history (cross-device sync)
POST /api/chat/history       — save shared chat history
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

router = APIRouter()

_HISTORY_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "chat_history.json"

# ── In-memory job store ────────────────────────────────────────────────────────
# job_id → {"status": "pending"|"done"|"error", "response": str|None, "created": float}
_jobs: dict[str, dict] = {}
_JOB_TTL = 600.0  # expire jobs after 10 minutes


def _prune_jobs() -> None:
    now = monotonic()
    stale = [j for j, v in _jobs.items() if now - v["created"] > _JOB_TTL]
    for j in stale:
        _jobs.pop(j, None)


# ── History helpers ────────────────────────────────────────────────────────────

def _read_history() -> dict:
    try:
        return json.loads(_HISTORY_PATH.read_text())
    except Exception:
        return {"messages": [], "updated_at": ""}


def _write_history(messages: list) -> str:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    data = json.dumps({"messages": messages[-40:], "updated_at": ts})
    tmp = _HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.replace(_HISTORY_PATH)  # atomic on Linux
    return ts


# ── Chat history (cross-device sync) ──────────────────────────────────────────

@router.get("/api/chat/history")
async def get_chat_history():
    return _read_history()


class HistorySaveRequest(BaseModel):
    messages: list[dict]


@router.post("/api/chat/history")
async def save_chat_history(body: HistorySaveRequest):
    ts = _write_history(body.messages)
    return {"ok": True, "updated_at": ts}


_COSTS_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "chat_costs.json"


@router.get("/api/chat/costs")
async def get_chat_costs():
    """Return estimated chat costs from claude_client._log_chat_cost()."""
    try:
        if not _COSTS_PATH.exists():
            return {"today_usd": 0.0, "total_usd": 0.0, "note": "estimate", "entries": []}
        data = json.loads(_COSTS_PATH.read_text())
        if not isinstance(data, list):
            data = []
        today = __import__("datetime").date.today().isoformat()
        today_entries = [e for e in data if e.get("ts", "").startswith(today)]
        today_usd  = round(sum(e["cost_usd"] for e in today_entries), 4)
        total_usd  = round(sum(e["cost_usd"] for e in data), 4)
        today_tokens = sum(e.get("est_tokens", (e.get("input_chars", 0) + e.get("output_chars", 0)) // 4) for e in today_entries)
        return {
            "today_usd": today_usd,
            "total_usd": total_usd,
            "today_tokens": today_tokens,
            "note": "estimate (~±30%)",
            "entries": today_entries[-20:],
        }
    except Exception as e:
        return {"today_usd": 0.0, "total_usd": 0.0, "note": f"error: {e}", "entries": []}


# ── Claude chat (async job system) ────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []

    @field_validator("message")
    @classmethod
    def message_not_too_long(cls, v):
        if len(v) > 8000:
            raise ValueError("message too long (max 8000 chars)")
        return v.strip()

    @field_validator("history")
    @classmethod
    def history_cap(cls, v):
        return v[-20:]


@router.post("/api/chat")
async def chat(body: ChatRequest):
    """Start a Claude chat job. Returns job_id immediately; client polls /api/chat/status/{id}."""
    if not body.message.strip():
        raise HTTPException(400, "message is empty")

    _prune_jobs()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending", "response": None, "created": monotonic()}

    async def _run() -> None:
        from dashboard.services.claude_client import chat as _chat
        try:
            reply = await asyncio.to_thread(_chat, body.message, body.history)
            _jobs[job_id].update({"status": "done", "response": reply})
        except Exception as e:
            _jobs[job_id].update({"status": "error", "response": f"Claude error: {e}"})

    asyncio.create_task(_run())
    return {"job_id": job_id, "status": "pending"}


@router.get("/api/chat/status/{job_id}")
async def chat_status(job_id: str):
    """Poll job status. Returns status + response when done. No AI calls — cheap to poll."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or expired")
    return {"status": job["status"], "response": job["response"]}
