"""
Read-only access to trading.db for the dashboard.
Opens the DB in WAL read-only mode — safe to run alongside monitor.py.
"""
import sqlite3
import json
import os
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "storage" / "data" / "trading.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row(r) -> dict:
    if r is None:
        return {}
    d = dict(r)
    for k in ["indicators", "market_context", "analysis", "confidence_breakdown", "news_at_entry"]:
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


# ── Position ──────────────────────────────────────────────────────────────────

def get_position() -> dict | None:
    """Return open position from position_state, or None."""
    try:
        with _conn() as conn:
            ps = conn.execute("SELECT * FROM position_state WHERE id=1").fetchone()
        if not ps or not ps["has_open"]:
            return None
        p = dict(ps)
        # Compute duration
        if p.get("opened_at"):
            try:
                opened = datetime.fromisoformat(p["opened_at"])
                mins = int((datetime.now() - opened).total_seconds() / 60)
                h, m = divmod(mins, 60)
                p["duration"] = f"{h}h {m}m" if h else f"{m}m"
            except Exception:
                p["duration"] = "—"
        # Map DB column names → frontend names
        return {
            "direction":      p.get("direction"),
            "entry_price":    p.get("entry_price"),
            "stop_loss":      p.get("stop_level"),
            "take_profit":    p.get("limit_level"),
            "size":           p.get("lots"),
            "phase":          (p.get("phase") or "INITIAL").upper(),
            "opened_at":      p.get("opened_at"),
            "duration":       p.get("duration", "—"),
            "confidence":     p.get("confidence"),
            # current_price / unrealised_pnl injected from bot_state.json in status router
        }
    except Exception:
        return None


# ── Scans ─────────────────────────────────────────────────────────────────────

def get_recent_scans(limit: int = 10) -> list[dict]:
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT timestamp, session, price, setup_found, confidence, action_taken "
                "FROM scans WHERE action_taken != 'no_setup' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Try to parse direction from action_taken
            a = d.get("action_taken", "") or ""
            d["direction"] = "LONG" if "long" in a.lower() else ("SHORT" if "short" in a.lower() else None)
            result.append(d)
        return list(reversed(result))
    except Exception:
        return []


# ── Trade history ─────────────────────────────────────────────────────────────

def get_trade_history(limit: int = 50) -> list[dict]:
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT trade_number, deal_id, opened_at, closed_at, direction, lots, "
                "entry_price, exit_price, stop_loss, take_profit, pnl, "
                "duration_minutes, phase_at_close, result, notes "
                "FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            mins = d.get("duration_minutes")
            if mins:
                h, m = divmod(int(mins), 60)
                d["duration"] = f"{h}h {m}m" if h else f"{m}m"
            else:
                d["duration"] = "—"
            d["exit_phase"]    = d.pop("phase_at_close", None)
            d["close_reason"]  = d.pop("notes", None)
            result.append(d)
        return result
    except Exception:
        return []


# ── Cost / AI stats ───────────────────────────────────────────────────────────

def get_cost_today() -> float:
    today = date.today().isoformat()
    try:
        with _conn() as conn:
            r = conn.execute(
                "SELECT COALESCE(SUM(api_cost),0) as c FROM scans WHERE timestamp LIKE ?",
                (f"{today}%",)
            ).fetchone()
        return round(float(r["c"]), 6) if r else 0.0
    except Exception:
        return 0.0


def get_ai_calls_today() -> int:
    today = date.today().isoformat()
    try:
        with _conn() as conn:
            r = conn.execute(
                "SELECT COUNT(*) as c FROM scans WHERE timestamp LIKE ? AND api_cost > 0",
                (f"{today}%",)
            ).fetchone()
        return int(r["c"]) if r else 0
    except Exception:
        return 0


def db_exists() -> bool:
    return DB_PATH.exists()
