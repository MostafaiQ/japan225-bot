"""
POST /api/chat               — start Claude chat job (returns job_id immediately)
GET  /api/chat/status/{id}   — poll job status (cheap, no AI; 4-8 s interval from client)
GET  /api/chat/history       — load shared chat history (cross-device sync)
POST /api/chat/history       — save shared chat history
"""
import asyncio
import base64
import json
import tempfile
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

class _Attachment(BaseModel):
    b64: str
    name: str

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    # Multi-file attachments (new format)
    attachments: list[_Attachment] = []
    # Legacy single-file attachment (backward compat)
    attachment_b64: str | None = None
    attachment_name: str | None = None

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

    from dashboard.services.claude_client import _pick_tier
    model, effort, timeout = _pick_tier(body.message)
    # Derive a short tier label for the frontend
    if "haiku" in model:
        tier = "haiku"
    elif "opus" in model:
        tier = "opus"
    else:
        tier = "sonnet"

    _prune_jobs()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending", "response": None, "created": monotonic(), "tier": tier}

    # Resolve attachments before spawning task (save to temp files)
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    attachment_paths: list[tuple[str, str, str]] = []  # (path, suffix, name)

    # Build unified list: prefer new `attachments` array, fallback to legacy single
    raw_attachments: list[tuple[str, str]] = []  # (b64, name)
    if body.attachments:
        raw_attachments = [(a.b64, a.name) for a in body.attachments[:5]]
    elif body.attachment_b64 and body.attachment_name:
        raw_attachments = [(body.attachment_b64, body.attachment_name)]

    for att_b64, att_name in raw_attachments:
        try:
            raw = base64.b64decode(att_b64)
            suffix = Path(att_name).suffix.lower() or ".bin"
            tmp = tempfile.NamedTemporaryFile(
                prefix="j225_attach_", suffix=suffix, delete=False
            )
            tmp.write(raw)
            tmp.close()
            attachment_paths.append((tmp.name, suffix, att_name))
        except Exception:
            continue

    effective_message = body.message
    if attachment_paths:
        parts = [body.message]
        for att_path, att_suffix, att_name in attachment_paths:
            if att_suffix in _IMAGE_EXTS:
                parts.append(f"\n[Attached image saved at: {att_path} — please read and analyse it]")
            else:
                try:
                    content = Path(att_path).read_text(errors="replace")[:6000]
                    parts.append(f"\n[Attached file: {att_name}]\n```\n{content}\n```")
                except Exception:
                    pass
        effective_message = "\n".join(parts)

    async def _run() -> None:
        from dashboard.services.claude_client import chat as _chat, send_telegram_message
        try:
            reply = await asyncio.to_thread(_chat, effective_message, body.history)
            _jobs[job_id].update({"status": "done", "response": reply, "acknowledged": False})
        except Exception as e:
            reply = f"Claude error: {e}"
            _jobs[job_id].update({"status": "error", "response": reply, "acknowledged": True})
        # Persist assistant reply to chat_history.json so it survives
        # client disconnect / refresh — user will see it when they reconnect.
        try:
            h = _read_history()
            msgs = h.get("messages", [])
            # Append the user message if not already present (client may have saved it)
            if not msgs or msgs[-1].get("content") != effective_message:
                msgs.append({"role": "user", "content": effective_message})
            msgs.append({"role": "assistant", "content": reply, "tier": tier})
            _write_history(msgs)
        except Exception:
            pass  # non-fatal — client can still get response via poll

        # Clean up temp attachment files
        for att_path, _, _ in attachment_paths:
            try:
                Path(att_path).unlink(missing_ok=True)
            except Exception:
                pass

        # Telegram fallback: if user doesn't read the response within 2 min, forward to Telegram
        async def _telegram_fallback():
            await asyncio.sleep(120)  # 2 minutes
            job = _jobs.get(job_id)
            if job and not job.get("acknowledged", True) and job.get("response"):
                short_q = body.message[:80] + ("..." if len(body.message) > 80 else "")
                tg_text = (
                    f"[Dashboard Chat]\n"
                    f"Q: {short_q}\n\n"
                    f"{job['response']}"
                )
                # Use plain text (no parse_mode) — Claude responses contain markdown
                # that would break HTML/MarkdownV2 parsing
                await asyncio.to_thread(send_telegram_message, tg_text)
                if job_id in _jobs:
                    _jobs[job_id]["acknowledged"] = True

        asyncio.create_task(_telegram_fallback())

    asyncio.create_task(_run())
    return {"job_id": job_id, "status": "pending", "tier": tier}


@router.get("/api/chat/status/{job_id}")
async def chat_status(job_id: str):
    """Poll job status. Returns status + response when done. No AI calls — cheap to poll."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or expired")
    # Mark as acknowledged once client sees the completed response (prevents Telegram fallback)
    if job["status"] in ("done", "error"):
        job["acknowledged"] = True
    return {"status": job["status"], "response": job["response"], "tier": job.get("tier", "sonnet")}
