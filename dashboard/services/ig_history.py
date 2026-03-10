"""
Fetch transaction + activity history from IG REST API.
Merges with local DB trades to produce a complete journal with Auto/Manual flags.
Reuses a single IG session (never logs out — avoids kicking user off IG web).
Caches results for 1 min with a lock to prevent concurrent fetches.
"""
import os
import json
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_cache = {"ts": 0, "data": None}
CACHE_TTL = 60  # seconds — 1 min (short enough to show new trades quickly)

# Date cutoff: only show trades from this date onwards
JOURNAL_CUTOFF = "2026-02-25T00:00:00"

# Reuse IG session tokens across calls (avoid creating/destroying sessions)
_ig_session = {"cst": None, "token": None, "ts": 0}
_IG_SESSION_TTL = 3600  # 1 hour — IG sessions last ~6h

# Prevent concurrent fetches (rapid refresh clicks)
_fetch_lock = threading.Lock()

# IG API endpoints
_BASE = "https://api.ig.com/gateway/deal"
_DEMO_BASE = "https://demo-api.ig.com/gateway/deal"


def _get_base():
    env = os.getenv("IG_ENV", "LIVE").upper()
    return _DEMO_BASE if env == "DEMO" else _BASE


def _fetch_ig_balance(cst, token):
    """Fetch current account balance directly from IG /accounts — never stale."""
    _load_env()
    api_key = os.getenv("IG_API_KEY", "")
    acc_id = os.getenv("IG_ACC_NUMBER", "")
    base = _get_base()
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "CST": cst,
        "X-SECURITY-TOKEN": token,
        "Version": "1",
    }
    try:
        resp = requests.get(f"{base}/accounts", headers=headers, timeout=10)
        if resp.status_code == 200:
            for acc in resp.json().get("accounts", []):
                if not acc_id or str(acc.get("accountId")) == str(acc_id):
                    return float(acc.get("balance", {}).get("balance", 0))
    except Exception as e:
        logger.warning(f"IG balance fetch error: {e}")
    return None


def _sync_trades_to_db(trades):
    """Write IG-sourced pnl, exit_price, balance_before, balance_after back to DB."""
    import sqlite3
    db_path = Path(__file__).parent.parent.parent / "storage" / "data" / "trading.db"
    try:
        conn = sqlite3.connect(str(db_path))
        updated = 0
        for t in trades:
            db_deal_id = t.get("_db_deal_id")
            if not db_deal_id:
                continue
            cursor = conn.execute(
                "UPDATE trades SET pnl=?, exit_price=?, balance_before=?, balance_after=? WHERE deal_id=?",
                (t["pnl"], t["exit_price"], t.get("balance_before"), t.get("balance_after"), db_deal_id),
            )
            updated += cursor.rowcount
        conn.commit()
        conn.close()
        if updated:
            logger.info(f"Synced {updated} trades from IG to DB")
    except Exception as e:
        logger.error(f"DB sync error: {e}")


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
    url = f"{base}/history/transactions?from={from_date}&to={to_date}&type=ALL&pageSize=500"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            txns = data.get("transactions", [])
            # Handle pagination — fetch remaining pages if needed
            meta = data.get("metadata", {}).get("pageData", {})
            total_pages = meta.get("totalPages", 1)
            if total_pages > 1:
                for page in range(2, total_pages + 1):
                    page_url = f"{url}&pageNumber={page}"
                    try:
                        r2 = requests.get(page_url, headers=headers, timeout=15)
                        if r2.status_code == 200:
                            txns.extend(r2.json().get("transactions", []))
                    except Exception:
                        break
            return txns
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


def _get_or_create_session():
    """Reuse cached IG session tokens. Only re-auth when expired or invalid."""
    global _ig_session
    now = time.time()
    if _ig_session["cst"] and (now - _ig_session["ts"]) < _IG_SESSION_TTL:
        return _ig_session["cst"], _ig_session["token"]
    cst, token = _auth()
    if cst:
        _ig_session = {"cst": cst, "token": token, "ts": now}
    return cst, token


def _invalidate_session():
    """Mark cached session as expired (e.g. after a 401)."""
    global _ig_session
    _ig_session = {"cst": None, "token": None, "ts": 0}


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


_SETUP_DESCRIPTIONS = {
    "bear_flag_breakdown": "price was in a downtrend, consolidated in a flag, bot shorted the breakdown",
    "bull_flag_breakout":  "price was in an uptrend, consolidated in a flag, bot longed the breakout",
    "bb_lower_bounce":     "price hit the lower Bollinger Band (oversold extreme), bot longed expecting a bounce back toward the mean",
    "bb_upper_bounce":     "price hit the upper Bollinger Band (overbought extreme), bot shorted expecting a pullback",
    "bb_mid_bounce":       "price pulled back to the BB midline (20 EMA support/resistance), bot traded the continuation",
}


def _close_outcome(result, phase, close_ch, pnl, dur):
    """Return a plain-English sentence describing how the trade closed."""
    if result == "TP_HIT":
        return f"Hit take-profit after {dur}" + (" (trailing stop locked in profit)" if phase == "trailing" else "")
    if result == "SL_HIT":
        return f"Stop loss hit after {dur}" + (" (stop was at breakeven)" if phase == "breakeven" else "")
    if result == "MANUAL_CLOSE":
        return f"Manually closed after {dur}"
    if result == "BREAKEVEN":
        return f"Closed at breakeven after {dur}"
    if result == "TIMEOUT":
        return f"Exited on time limit after {dur}"
    # CLOSED_UNKNOWN — infer from channel and phase
    if close_ch == "SYSTEM":
        if phase == "breakeven":
            return f"SL/TP triggered at breakeven level after {dur}"
        if phase == "trailing":
            return f"Trailing stop triggered after {dur}"
        outcome = "profit" if pnl > 0 else "a loss"
        return f"System closed in {outcome} after {dur} (SL or TP hit)"
    if close_ch in ("WEB", "MOBILE", "PUBLIC_FIX_API"):
        outcome = "in profit" if pnl > 0 else "at a loss"
        return f"Manually closed {outcome} after {dur}"
    outcome = "in profit" if pnl > 0 else "at a loss"
    return f"Closed {outcome} after {dur}"


def _build_trade_note(db_match, close_ch, pnl, direction, dur):
    """Build a meaningful, human-readable note for a bot trade."""
    setup = db_match.get("setup_type", "")
    result = db_match.get("result", "")
    phase = db_match.get("exit_phase") or ""
    confidence = db_match.get("confidence") or 0
    session_raw = (db_match.get("session") or "").lower()
    session_map = {"tokyo": "Tokyo", "london": "London", "new_york": "NY", "unknown": ""}
    session = session_map.get(session_raw, session_raw.title())

    if setup == "recovered":
        close_note = _close_outcome(result, phase, close_ch, pnl, dur)
        return f"Position was already open when bot restarted — not a new signal. {close_note}."

    desc = _SETUP_DESCRIPTIONS.get(setup, setup.replace("_", " ").title() if setup else "")
    dir_label = "SHORT" if direction == "SHORT" else "LONG"
    session_part = f" ({session} session)" if session else ""
    close_note = _close_outcome(result, phase, close_ch, pnl, dur)
    conf_part = f" Conf: {confidence}%." if confidence else ""
    return f"{dir_label}{session_part} — {desc}. {close_note}.{conf_part}"


def _build_manual_note(direction, close_ch, pnl, instrument, dur):
    """Build a note for a manually placed trade (no DB match)."""
    dir_label = "Long" if direction == "LONG" else "Short"
    outcome = "in profit" if pnl > 0 else "at a loss"
    if close_ch == "SYSTEM":
        close_note = f"SL/TP triggered {outcome}"
    elif close_ch in ("WEB", "MOBILE", "PUBLIC_FIX_API"):
        close_note = f"Manually closed {outcome}"
    else:
        close_note = f"Closed {outcome}"
    instr = "" if "Japan 225" in instrument else f" on {instrument.split('(')[0].strip()}"
    return f"Manual {dir_label.lower()}{instr}. {close_note} after {dur}."


def fetch_full_journal(days=30):
    """
    Fetch all trades from IG and merge with local DB data.
    Returns: {trades: [...], account: {...}}
    """
    global _cache
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    # Prevent concurrent fetches (rapid refresh clicks)
    if not _fetch_lock.acquire(blocking=False):
        # Another fetch is in progress — return stale cache or empty
        if _cache["data"]:
            return _cache["data"]
        return {"trades": [], "account": {}, "source": "busy"}

    try:
        return _fetch_journal_locked(days)
    finally:
        _fetch_lock.release()


def _fetch_journal_locked(days):
    """Inner fetch — called under _fetch_lock."""
    global _cache
    now = time.time()

    # Re-check cache under lock (another thread may have just populated it)
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    # Get DB trades for AI analysis data
    from dashboard.services.db_reader import get_trade_history, get_account_state
    db_trades = get_trade_history(200)
    db_by_ref = {}
    for t in db_trades:
        did = t.get("deal_id", "") or ""
        # deal_id in DB is like "DIAAAAQ2W9K76AJ", reference in IG is "2W9K76AJ"
        # Match by the last part (strip DIAAAAQ prefix once)
        if did:
            short_ref = did.replace("DIAAAAQ", "", 1)
            db_by_ref[short_ref] = t
            db_by_ref[did] = t

    # Reuse cached IG session (never logout — avoids kicking user off IG web)
    cst, token = _get_or_create_session()
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

    transactions = _fetch_transactions(cst, token, days)
    activities = _fetch_activities(cst, token, days)

    # If both empty, session may have expired — retry once with fresh auth
    if not transactions and not activities:
        _invalidate_session()
        cst, token = _get_or_create_session()
        if cst:
            transactions = _fetch_transactions(cst, token, days)
            activities = _fetch_activities(cst, token, days)

    open_ch_map, close_ch_map, close_to_open = _build_activity_map(activities)

    # Build a sorted list of DB trades for timestamp-based fallback matching
    # (used when ref-based matching fails because IG activity is missing/malformed)
    _db_by_ts = sorted(
        [t for t in db_trades if t.get("opened_at")],
        key=lambda t: t["opened_at"]
    )

    def _ts_fallback_match(open_date_utc: str, direction: str):
        """Find the closest DB trade opened within 60s of open_date_utc, same direction."""
        if not open_date_utc or not _db_by_ts:
            return None
        try:
            txn_ts = datetime.fromisoformat(open_date_utc.replace("Z", "+00:00"))
        except Exception:
            return None
        best = None
        best_delta = 600  # max 10 minutes (allows "recovered" trades with timestamp drift)
        for t in _db_by_ts:
            try:
                db_ts = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
                # Normalise timezone: compare both as UTC-aware or both naive
                cmp_txn = txn_ts
                if cmp_txn.tzinfo and not db_ts.tzinfo:
                    db_ts = db_ts.replace(tzinfo=cmp_txn.tzinfo)
                elif db_ts.tzinfo and not cmp_txn.tzinfo:
                    cmp_txn = cmp_txn.replace(tzinfo=db_ts.tzinfo)
                delta = abs((cmp_txn - db_ts).total_seconds())
                t_dir = (t.get("direction") or "").upper()
                if delta < best_delta and t_dir == direction.upper():
                    best_delta = delta
                    best = t
            except Exception:
                continue
        return best

    # Filter all transactions by cutoff date first
    cutoff = JOURNAL_CUTOFF
    transactions = [t for t in transactions if (t.get("dateUtc") or t.get("openDateUtc") or "") >= cutoff]

    # Separate real deposits/withdrawals (ignore interest, dividends, adjustments)
    deposits = []
    for txn in transactions:
        txn_type = (txn.get("transactionType") or "").upper()
        if txn_type in ("DEPO", "WITH", "DEPOSIT", "WITHDRAWAL"):
            amt = _parse_pnl(txn.get("profitAndLoss", "$0"))
            # Only count real cash deposits (> $5), not interest/concession adjustments
            if abs(amt) >= 5:
                deposits.append({
                    "date": txn.get("dateUtc", ""),
                    "amount": amt,
                    "type": "deposit" if amt > 0 else "withdrawal",
                })

    # Build trade list from IG transactions
    trades = []
    matched_db_ids = set()  # track which DB trades were matched to IG
    acc = get_account_state()
    ig_live_balance = _fetch_ig_balance(cst, token)

    # Transactions come newest-first from IG, reverse for chronological
    for txn in reversed(transactions):
        ref = txn.get("reference", "")  # This is the close deal short ref
        size_str = txn.get("size", "0")
        try:
            size_val = float(size_str)
        except (ValueError, TypeError):
            continue  # Skip non-trade transactions (deposits, adjustments)
        direction = "LONG" if size_val > 0 else "SHORT"
        pnl = _parse_pnl(txn.get("profitAndLoss", "$0"))
        instrument = txn.get("instrumentName", "Unknown")

        # Map close ref → open ref → channels
        open_ref = close_to_open.get(ref, "")
        open_ch = open_ch_map.get(open_ref, "")
        close_ch = close_ch_map.get(open_ref, "")

        # Check if we have DB data for this trade (try close ref and open ref first)
        db_match = db_by_ref.get(ref) or db_by_ref.get(open_ref)

        # Fallback: match by timestamp if ref-based lookup failed
        if db_match is None:
            db_match = _ts_fallback_match(txn.get("openDateUtc", ""), direction)

        if db_match and db_match.get("deal_id"):
            matched_db_ids.add(db_match["deal_id"])

        # Determine auto/manual labels.
        # If we have a DB match, it's a bot trade (opened via PUBLIC_WEB_API).
        # Only fall back to "Manual" when we have no DB match AND no channel info.
        if open_ch:
            opened_by = _channel_label(open_ch)
        elif db_match:
            opened_by = "Auto"  # DB match = bot opened it
        else:
            opened_by = "Manual"

        if close_ch:
            closed_by = _channel_label(close_ch)
        elif db_match:
            # Infer close channel: if result is SL_HIT or TP_HIT it was a system close
            db_result = (db_match.get("result") or "").upper()
            closed_by = "System" if db_result in ("SL_HIT", "TP_HIT") else "Manual"
        else:
            closed_by = "Manual"

        entry = float(txn.get("openLevel", 0))
        exit_p = float(txn.get("closeLevel", 0))
        sl = db_match.get("stop_loss") if db_match else None
        tp = db_match.get("take_profit") if db_match else None

        # Compute R:R (planned if SL/TP available, realized from actual entry/exit otherwise)
        rr = None
        if sl and tp and entry:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            if risk > 0:
                rr = round(reward / risk, 1)
        elif entry and exit_p and pnl != 0:
            # Realized R:R: actual move / estimated risk (SL ~150pts for Japan 225)
            move_pts = abs(exit_p - entry)
            est_sl_pts = 150  # standard SL estimate for Japan 225
            if est_sl_pts > 0:
                rr = round(move_pts / est_sl_pts, 1)
                if pnl < 0:
                    rr = -rr  # negative R:R for losing trades

        # Compute duration string
        dur_str = db_match.get("duration") if db_match else None
        if dur_str == "—":
            dur_str = None  # DB placeholder — recompute from IG timestamps
        if not dur_str:
            try:
                t_open = datetime.fromisoformat(txn.get("openDateUtc", ""))
                t_close = datetime.fromisoformat(txn.get("dateUtc", ""))
                mins = int((t_close - t_open).total_seconds() / 60)
                h, m = divmod(mins, 60)
                dur_str = f"{h}h {m}m" if h else f"{m}m"
            except Exception:
                dur_str = "?"

        # Build human-readable notes
        if db_match:
            notes = _build_trade_note(db_match, close_ch, pnl, direction, dur_str)
        else:
            notes = _build_manual_note(direction, close_ch, pnl, instrument, dur_str)

        trade = {
            "opened_at": txn.get("openDateUtc", ""),
            "closed_at": txn.get("dateUtc", ""),
            "instrument": instrument,
            "direction": direction,
            "lots": abs(size_val),
            "entry_price": entry,
            "exit_price": exit_p,
            "stop_loss": sl,
            "take_profit": tp,
            "pnl": pnl,
            "opened_by": opened_by,
            "closed_by": closed_by,
            "notes": notes,
            "reference": ref,
            "confidence": db_match.get("confidence") if db_match else None,
            "session": db_match.get("session") if db_match else None,
            "result": db_match.get("result") if db_match else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"),
            "duration": dur_str,
            "rr": rr,
            "_db_deal_id": db_match.get("deal_id") if db_match else None,
        }

        trades.append(trade)

    # Add DB-only trades (not matched to any IG transaction — e.g. from demo or pre-switch)
    for dbt in db_trades:
        if dbt.get("deal_id") in matched_db_ids:
            continue
        if not dbt.get("closed_at"):
            continue  # skip open positions
        pnl = dbt.get("pnl") or 0
        direction = (dbt.get("direction") or "LONG").upper()
        entry = dbt.get("entry_price") or 0
        exit_p = dbt.get("exit_price") or 0
        sl = dbt.get("stop_loss")
        tp = dbt.get("take_profit")
        rr = None
        if sl and tp and entry:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            if risk > 0:
                rr = round(reward / risk, 1)
        dur_str = None
        try:
            t_open = datetime.fromisoformat(dbt["opened_at"])
            t_close = datetime.fromisoformat(dbt["closed_at"])
            mins = int((t_close - t_open).total_seconds() / 60)
            h, m = divmod(mins, 60)
            dur_str = f"{h}h {m}m" if h else f"{m}m"
        except Exception:
            dur_str = "?"
        notes = _build_trade_note(dbt, "", pnl, direction, dur_str)
        trades.append({
            "opened_at": dbt.get("opened_at", ""),
            "closed_at": dbt.get("closed_at", ""),
            "instrument": "Japan 225 Cash ($1)",
            "direction": direction,
            "lots": dbt.get("lots") or 0,
            "entry_price": entry,
            "exit_price": exit_p,
            "stop_loss": sl,
            "take_profit": tp,
            "pnl": pnl,
            "opened_by": "Auto",
            "closed_by": "System" if (dbt.get("result") or "") in ("SL_HIT", "TP_HIT") else "Manual",
            "notes": notes,
            "reference": (dbt.get("deal_id") or "").replace("DIAAAAQ", ""),
            "confidence": dbt.get("confidence"),
            "session": dbt.get("session"),
            "result": dbt.get("result") or ("WIN" if pnl > 0 else "LOSS"),
            "duration": dur_str,
            "rr": rr,
            "_db_deal_id": dbt.get("deal_id"),
        })

    # Sort all trades chronologically by open date
    trades.sort(key=lambda t: t.get("opened_at") or "")

    # Filter: only trades from Feb 26, 2026 onwards
    cutoff = JOURNAL_CUTOFF
    trades = [t for t in trades if (t.get("opened_at") or "") >= cutoff]

    # Compute running balance anchored to live IG balance, accounting for deposits
    current_balance = ig_live_balance if ig_live_balance is not None else acc.get("balance", 0)
    total_trade_pnl = sum(t["pnl"] for t in trades)
    total_deposits = sum(d["amount"] for d in deposits if d["type"] == "deposit")
    total_withdrawals = sum(abs(d["amount"]) for d in deposits if d["type"] == "withdrawal")
    starting_bal = current_balance - total_trade_pnl - total_deposits + total_withdrawals

    # Build chronological event list: trades + deposits interleaved by date
    # so the running balance correctly accounts for money in/out
    events = []
    for t in trades:
        events.append(("trade", t.get("closed_at") or t.get("opened_at", ""), t))
    for d in deposits:
        events.append(("deposit", d["date"], d))
    events.sort(key=lambda e: e[1])

    current_bal = starting_bal
    deposit_idx = 0
    for etype, edate, edata in events:
        if etype == "deposit":
            if edata["type"] == "deposit":
                current_bal += edata["amount"]
            else:
                current_bal -= abs(edata["amount"])
        else:  # trade
            edata["balance_before"] = round(current_bal, 2)
            current_bal += edata["pnl"]
            edata["balance_after"] = round(current_bal, 2)

    # Sync IG values (pnl, exit_price, balance_before, balance_after) back to DB
    _sync_trades_to_db(trades)

    # Compute total PnL for this period (trading only, not deposits)
    total_pnl = total_trade_pnl

    # Filter: only Japan 225 trades for win rate
    j225 = [t for t in trades if "Japan 225" in t.get("instrument", "")]
    j225_wins = len([t for t in j225 if t["pnl"] > 0])
    # Separate bot vs manual stats
    j225_bot = [t for t in j225 if t.get("opened_by") == "Auto"]
    j225_bot_wins = len([t for t in j225_bot if t["pnl"] > 0])
    j225_manual = [t for t in j225 if t.get("opened_by") != "Auto"]
    j225_manual_wins = len([t for t in j225_manual if t["pnl"] > 0])

    # Strip internal field before returning
    for t in trades:
        t.pop("_db_deal_id", None)

    result = {
        "trades": trades,
        "account": {
            "balance": round(current_balance, 2),
            "starting_balance": round(starting_bal, 2),
            "total_pnl": round(total_pnl, 2),
            "total_deposits": round(total_deposits, 2),
            "j225_wins": j225_wins,
            "j225_total": len(j225),
            "j225_bot_wins": j225_bot_wins,
            "j225_bot_total": len(j225_bot),
            "j225_manual_wins": j225_manual_wins,
            "j225_manual_total": len(j225_manual),
        },
        "source": "ig_api",
    }
    _cache = {"ts": now, "data": result}
    return result
