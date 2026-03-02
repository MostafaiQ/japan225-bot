"""
GET /api/history  — closed trade journal
GET /api/scans    — scan history with date filter + pagination
"""
from fastapi import APIRouter, Query
from typing import Optional
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


@router.get("/api/scans")
async def scans(
    limit: int = Query(50, ge=1, le=500),
    date: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}-\d{2}$"),
    all: bool = Query(False),
):
    """Fetch scan records. ?limit=50&date=2026-03-02&all=true (include no_setup)."""
    rows = db_reader.get_recent_scans(limit=limit, date=date, include_no_setup=all)
    return {
        "scans": rows,
        "total": len(rows),
        "date": date,
    }
