"""
Fetch transaction + activity history from IG REST API.
Merges with local DB trades to produce a complete journal with Auto/Manual flags.
Caches results for 60s to avoid hammering the API.
"""
import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_cache = {"ts": 0, "data": None}
CACHE_TTL = 60  # seconds

# IG API endpoints
_BASE = "https://api.ig.com/gateway/deal"
_DEMO_BASE = "https://demo-api.ig.com/gateway/deal"


def _get_base():
    env = os.getenv("IG_ENV", "LIVE").upper()
    return _DEMO_BASE if env == "DEMO" else _BASE


def _load_env():
    """Load .env with override to ensure correct credentials."""
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent.parent / ".env"
    load_dotenv(env_path, override=True)


def _auth():
    """Create a new IG session and return (cst, token) or (None, None)."""
    _load_env()
    api_key = os.getenv("IG_API_KEY", "")
    username = os.getenv("IG_USERNAME", "")
    password = os.getenv("IG_PASSWORD", "")

    if not all([api_key, username, password]):
        logger.error("IG credentials not set in environment")
        return None, None

    base = _get_base()
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "Version": "2",
    }
    payload = {"identifier": username, "password": password}

    try:
        resp = requests.post(f"{base}/session", headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            return resp.headers.get("CST"), resp.headers.get("X-SECURITY-TOKEN")
        logger.error(f"IG auth failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"IG auth error: {e}")
    return None, None


def _fetch_transactions(cst, token, days=30):
    """Fetch closed transactions from IG."""
    base = _get_base()
    api_key = os.getenv("IG_API_KEY", "")
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "CST": cst,
        "X-SECURITY-TOKEN": token,
        "Version": "2",
    }

    from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    to_date = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    url = f"{base}/history/transactions?from={from_date}&to={to_date}&type=ALL"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("transactions", [])
        logger.error(f"IG transactions failed: {resp.status_code}")
    except Exception as e:
        logger.error(f"IG transactions error: {e}")
    return []


def _fetch_activities(cst, token, days=30):
    """Fetch activity history (has channel info for auto/manual detection)."""
    base = _get_base()
    api_key = os.getenv("IG_API_KEY", "")
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "CST": cst,
        "X-SECURITY-TOKEN": token,
        "Version": "3",
    }

    from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    to_date = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    url = f"{base}/history/activity?from={from_date}&to={to_date}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("activities", [])
        logger.error(f"IG activities failed: {resp.status_code}")
    except Exception as e:
        logger.error(f"IG activities error: {e}")
    return []


def _logout(cst, token):
    """Delete the IG session to free the slot."""
    base = _get_base()
    api_key = os.getenv("IG_API_KEY", "")
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "CST": cst,
        "X-SECURITY-TOKEN": token,
        "_method": "DELETE",
    }
    try:
        requests.post(f"{base}/session", headers=headers, timeout=10)
    except Exception:
        pass


def _build_activity_map(activities):
    """Build maps from activity history.

    IG structure:
    - Transaction reference = close deal ref (short, e.g. "2W9K76AJ")
    - Activity deal_id = "DIAAAAQ" + short ref
    - Close activity desc = "Position/s closed: <open_ref>"
    - Open activity desc = "Position opened: <open_ref>"

    Returns:
      open_channel:  {open_ref -> channel}  (how position was opened)
      close_channel: {open_ref -> channel}  (how position was closed)
      close_to_open: {close_ref -> open_ref}  (maps transaction reference to open ref)
    """
    open_channel = {}   # open_ref -> channel
    close_channel = {}  # open_ref -> channel
    close_to_open = {}  # close_short_ref -> open_ref

    for a in activities:
        desc = a.get("description", "")
        channel = a.get("channel", "")
        deal_id = a.get("dealId", "")
        # Short ref = deal_id without DIAAAAQ prefix
        short_ref = deal_id.replace("DIAAAAQ", "") if deal_id.startswith("DIAAAAQ") else deal_id

        if "Position opened" in desc:
            # Extract open ref: "Position opened: ZX45TUAH" or "Position opened: ZX45TUAH; Limit order..."
            part = desc.split("Position opened: ")[-1]
            open_ref = part.split(";")[0].strip()
            if open_ref:
                open_channel[open_ref] = channel
        elif "Position/s closed" in desc or "Position closed" in desc:
            # "Position/s closed: 2W92EZAV"
            part = desc.split("closed: ")[-1]
            open_ref = part.split(";")[0].strip()
            if open_ref:
                close_channel[open_ref] = channel
                # Map close deal short ref to open ref
                close_to_open[short_ref] = open_ref

    return open_channel, close_channel, close_to_open


def _parse_pnl(pnl_str):
    """Parse IG P&L string like '$7.88' or '$-3.09' to float."""
    try:
        return float(pnl_str.replace("$", "").replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _channel_label(channel):
    """Convert IG channel to human label."""
    if channel in ("PUBLIC_WEB_API",):
        return "Auto"  # Bot uses REST API
    elif channel in ("SYSTEM",):
        return "System"  # SL/TP/margin close
    elif channel in ("WEB", "PUBLIC_FIX_API", "MOBILE"):
        return "Manual"
    return "Manual"


def fetch_full_journal(days=30):
    """
    Fetch all trades from IG and merge with local DB data.
    Returns: {trades: [...], account: {...}}
    """
    global _cache
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    # Get DB trades for AI analysis data
    from dashboard.services.db_reader import get_trade_history, get_account_state
    db_trades = get_trade_history(200)
    db_by_ref = {}
    for t in db_trades:
        did = t.get("deal_id", "") or ""
        # deal_id in DB is like "DIAAAAQ2W9K76AJ", reference in IG is "2W9K76AJ"
        # Match by the last part
        if did:
            short_ref = did.replace("DIAAAAQ", "").replace("DIAAAAQ", "")
            db_by_ref[short_ref] = t
            db_by_ref[did] = t

    # Auth to IG
    cst, token = _auth()
    if not cst:
        # Fallback to DB-only trades — add missing fields for frontend
        acc = get_account_state()
        for t in db_trades:
            t.setdefault("instrument", "Japan 225 Cash ($1)")
            t.setdefault("opened_by", "Auto")
            t.setdefault("closed_by", "System")
            t.setdefault("notes", t.get("close_reason", "") or "")
            t.setdefault("balance_after", t.get("balance_after"))
        j225_wins = len([t for t in db_trades if (t.get("pnl") or 0) > 0])
        result = {
            "trades": db_trades,
            "account": {
                "balance": acc.get("balance", 0),
                "starting_balance": acc.get("starting_balance", 0),
                "total_pnl": acc.get("total_pnl", 0),
                "j225_wins": j225_wins,
                "j225_total": len(db_trades),
            },
            "source": "db_only",
        }
        _cache = {"ts": now, "data": result}
        return result

    try:
        transactions = _fetch_transactions(cst, token, days)
        activities = _fetch_activities(cst, token, days)
    finally:
        _logout(cst, token)

    open_ch_map, close_ch_map, close_to_open = _build_activity_map(activities)

    # Build trade list from IG transactions
    trades = []
    acc = get_account_state()

    # Transactions come newest-first from IG, reverse for chronological
    for txn in reversed(transactions):
        ref = txn.get("reference", "")  # This is the close deal short ref
        size_str = txn.get("size", "0")
        size_val = float(size_str)
        direction = "LONG" if size_val > 0 else "SHORT"
        pnl = _parse_pnl(txn.get("profitAndLoss", "$0"))
        instrument = txn.get("instrumentName", "Unknown")

        # Map close ref → open ref → channels
        open_ref = close_to_open.get(ref, "")
        open_ch = open_ch_map.get(open_ref, "")
        close_ch = close_ch_map.get(open_ref, "")

        # Check if we have DB data for this trade (try close ref and open ref)
        db_match = db_by_ref.get(ref) or db_by_ref.get(open_ref)

        # Determine auto/manual labels
        opened_by = _channel_label(open_ch) if open_ch else "Manual"
        closed_by = _channel_label(close_ch) if close_ch else "Manual"

        # Build the notes
        notes_parts = []
        if db_match:
            ai = db_match.get("ai_analysis")
            if isinstance(ai, dict):
                for key in ["reasoning", "why_entry", "risk_notes", "lesson", "exit_reason"]:
                    val = ai.get(key)
                    if val:
                        notes_parts.append(val)
            elif isinstance(ai, str) and ai:
                notes_parts.append(ai)
            if db_match.get("setup_type"):
                notes_parts.insert(0, f"Setup: {db_match['setup_type']}")
            if db_match.get("exit_phase"):
                notes_parts.append(f"Phase: {db_match['exit_phase']}")
            if db_match.get("close_reason"):
                notes_parts.append(f"Close: {db_match['close_reason']}")
            if db_match.get("result"):
                notes_parts.append(f"Result: {db_match['result']}")
        else:
            # No bot data — generate basic notes from the trade itself
            pts = abs(float(txn.get("closeLevel", 0)) - float(txn.get("openLevel", 0)))
            if pnl > 0:
                notes_parts.append(f"Winner +{pts:.1f}pts")
            elif pnl < 0:
                notes_parts.append(f"Loser -{pts:.1f}pts")
            if "Japan 225" not in instrument:
                notes_parts.append("Non-bot instrument")

        trade = {
            "opened_at": txn.get("openDateUtc", ""),
            "closed_at": txn.get("dateUtc", ""),
            "instrument": instrument,
            "direction": direction,
            "lots": abs(size_val),
            "entry_price": float(txn.get("openLevel", 0)),
            "exit_price": float(txn.get("closeLevel", 0)),
            "stop_loss": db_match.get("stop_loss") if db_match else None,
            "take_profit": db_match.get("take_profit") if db_match else None,
            "pnl": pnl,
            "opened_by": opened_by,
            "closed_by": closed_by,
            "notes": " · ".join(notes_parts) if notes_parts else "",
            "reference": ref,
            "confidence": db_match.get("confidence") if db_match else None,
            "session": db_match.get("session") if db_match else None,
            "result": db_match.get("result") if db_match else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"),
            "duration": db_match.get("duration") if db_match else None,
        }

        # Compute duration if not from DB
        if not trade["duration"]:
            try:
                t_open = datetime.fromisoformat(trade["opened_at"])
                t_close = datetime.fromisoformat(trade["closed_at"])
                mins = int((t_close - t_open).total_seconds() / 60)
                h, m = divmod(mins, 60)
                trade["duration"] = f"{h}h {m}m" if h else f"{m}m"
            except Exception:
                trade["duration"] = "—"

        trades.append(trade)

    # Compute running balance (work backwards from current balance)
    current_bal = acc.get("balance", 0)
    for t in reversed(trades):
        t["balance_after"] = round(current_bal, 2)
        current_bal -= t["pnl"]
        t["balance_before"] = round(current_bal, 2)

    # The earliest balance_before should approximate starting balance
    starting_balance = trades[0]["balance_before"] if trades else acc.get("starting_balance", 0)
    total_pnl = sum(t["pnl"] for t in trades)

    # Filter: only Japan 225 trades for win rate
    j225 = [t for t in trades if "Japan 225" in t.get("instrument", "")]
    j225_wins = len([t for t in j225 if t["pnl"] > 0])

    result = {
        "trades": trades,
        "account": {
            "balance": acc.get("balance", 0),
            "starting_balance": round(starting_balance, 2),
            "total_pnl": round(total_pnl, 2),
            "j225_wins": j225_wins,
            "j225_total": len(j225),
        },
        "source": "ig_api",
    }
    _cache = {"ts": now, "data": result}
    return result
