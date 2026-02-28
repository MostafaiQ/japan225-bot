"""
POST /api/chat  — Claude chat with bot context injected
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


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
