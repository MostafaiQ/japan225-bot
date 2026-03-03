"""
GET /api/history  — closed trade journal (fetches from IG API + merges DB)
GET /api/scans    — scan history with date filter + pagination
"""
from fastapi import APIRouter, Query
from typing import Optional
from dashboard.services import db_reader
from dashboard.services.ig_history import fetch_full_journal

router = APIRouter()


@router.get("/api/history")
async def history(days: int = Query(30, ge=1, le=90)):
    return fetch_full_journal(days=days)


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
