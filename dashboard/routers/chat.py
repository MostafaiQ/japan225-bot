"""
POST /api/chat             — Claude agentic chat
GET  /api/chat/history     — load shared chat history (cross-device sync)
POST /api/chat/history     — save shared chat history
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

router = APIRouter()

_HISTORY_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "chat_history.json"


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


@router.get("/api/chat/costs")
async def get_chat_costs():
    """Returns dashboard chat API costs (today + all-time total)."""
    costs_path = Path(__file__).parent.parent.parent / "storage" / "data" / "chat_costs.json"
    try:
        entries = json.loads(costs_path.read_text()) if costs_path.exists() else []
        today   = datetime.now(timezone.utc).date().isoformat()
        today_e = [e for e in entries if e.get("ts", "").startswith(today)]
        return {
            "today_usd": round(sum(e.get("cost_usd", 0) for e in today_e), 4),
            "total_usd": round(sum(e.get("cost_usd", 0) for e in entries), 4),
            "entries":   today_e[-20:],
        }
    except Exception as e:
        return {"today_usd": 0.0, "total_usd": 0.0, "entries": [], "error": str(e)}


# ── Claude chat ───────────────────────────────────────────────────────────────

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
    if not body.message.strip():
        raise HTTPException(400, "message is empty")

    from dashboard.services.claude_client import chat as _chat
    try:
        reply = _chat(body.message, body.history)
        return {"response": reply}
    except Exception as e:
        raise HTTPException(500, f"Claude error: {e}")
