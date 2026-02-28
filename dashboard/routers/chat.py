"""
POST /api/chat             — Claude agentic chat
GET  /api/chat/history     — load shared chat history (cross-device sync)
POST /api/chat/history     — save shared chat history
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
    _HISTORY_PATH.write_text(json.dumps({
        "messages": messages[-40:],   # keep last 40 entries (20 pairs)
        "updated_at": ts,
    }))
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


# ── Claude chat ───────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


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
