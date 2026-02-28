"""
GET /api/history  — closed trade journal
"""
from fastapi import APIRouter, Query
from dashboard.services import db_reader

router = APIRouter()


@router.get("/api/history")
async def history(limit: int = Query(50, ge=1, le=200)):
    trades = db_reader.get_trade_history(limit)
    wins   = [t for t in trades if (t.get("pnl") or 0) > 0]
    total_pnl = sum(t.get("pnl") or 0 for t in trades)
    return {
        "trades":    trades,
        "total":     len(trades),
        "wins":      len(wins),
        "losses":    len(trades) - len(wins),
        "total_pnl": round(total_pnl, 2),
    }
