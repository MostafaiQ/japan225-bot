"""
GET  /api/config          — current config (defaults + overrides)
POST /api/config          — update overrides (hot or restart tier)
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any

from dashboard.services import config_manager, db_reader

router = APIRouter()


class ConfigUpdate(BaseModel):
    tier: str           # "hot" or "restart"
    model_config = {"extra": "allow"}


@router.get("/api/config")
async def get_config():
    return config_manager.read_overrides()


@router.post("/api/config")
async def post_config(body: ConfigUpdate):
    tier = body.tier
    if tier not in ("hot", "restart"):
        raise HTTPException(400, "tier must be 'hot' or 'restart'")

    # Block restart-tier changes if position is open
    if tier == "restart":
        pos = db_reader.get_position()
        if pos:
            raise HTTPException(409, "Cannot change restart-tier settings while a position is open.")

    updates = body.model_extra or {}
    try:
        updated = config_manager.write_overrides(updates, tier)
    except ValueError as e:
        raise HTTPException(400, str(e))

    msg = "Config saved." if tier == "hot" else "Config saved. Restart the bot to apply."
    return {"ok": True, "message": msg, "config": updated}
