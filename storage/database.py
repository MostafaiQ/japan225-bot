"""
Storage layer using SQLite for persistent state.
Handles: scan history, trading journal, position state, account state.
Single file database, no server needed, works on Oracle Cloud free tier.
"""
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from config.settings import DB_PATH

logger = logging.getLogger(__name__)

# Whitelists prevent SQL injection via f-string column interpolation
_ACCOUNT_STATE_COLUMNS = {
    "balance", "starting_balance", "total_pnl", "total_api_cost",
    "daily_loss_today", "daily_loss_date", "weekly_loss", "weekly_loss_start",
    "consecutive_losses", "last_loss_time", "system_active",
    "compound_trade_number", "updated_at",
}
_MARKET_CONTEXT_COLUMNS = {
    "date", "economic_events", "macro_snapshot", "session_summaries",
    "trend_observation", "updated_at",
}
_POSITION_STATE_COLUMNS = {
    "has_open", "deal_id", "direction", "lots", "entry_price",
    "stop_level", "limit_level", "opened_at", "phase", "confidence",
    "pending_alert", "updated_at", "entry_context",
}


class Storage:
    """SQLite-backed persistent storage for the trading bot."""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self.data_dir = Path(self.db_path).parent
        self._init_db()
    
    def _init_db(self):
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    session TEXT,
                    price REAL,
                    indicators TEXT,
                    market_context TEXT,
                    analysis TEXT,
                    setup_found INTEGER DEFAULT 0,
                    confidence INTEGER,
                    action_taken TEXT,
                    api_cost REAL DEFAULT 0
                );
                
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_number INTEGER,
                    deal_id TEXT UNIQUE,
                    opened_at TEXT,
                    closed_at TEXT,
                    direction TEXT,
                    lots REAL,
                    entry_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    exit_price REAL,
                    pnl REAL,
                    balance_before REAL,
                    balance_after REAL,
                    confidence INTEGER,
                    confidence_breakdown TEXT,
                    setup_type TEXT,
                    session TEXT,
                    ai_analysis TEXT,
                    news_at_entry TEXT,
                    result TEXT,
                    duration_minutes INTEGER,
                    phase_at_close TEXT,
                    api_cost REAL DEFAULT 0,
                    notes TEXT
                );
                
                CREATE TABLE IF NOT EXISTS position_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    has_open INTEGER DEFAULT 0,
                    deal_id TEXT,
                    direction TEXT,
                    lots REAL,
                    entry_price REAL,
                    stop_level REAL,
                    limit_level REAL,
                    opened_at TEXT,
                    phase TEXT DEFAULT 'initial',
                    confidence INTEGER,
                    pending_alert TEXT,
                    updated_at TEXT
                );
                
                CREATE TABLE IF NOT EXISTS account_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance REAL DEFAULT 0,
                    starting_balance REAL DEFAULT 16.67,
                    total_pnl REAL DEFAULT 0,
                    total_api_cost REAL DEFAULT 0,
                    daily_loss_today REAL DEFAULT 0,
                    daily_loss_date TEXT,
                    weekly_loss REAL DEFAULT 0,
                    weekly_loss_start TEXT,
                    consecutive_losses INTEGER DEFAULT 0,
                    last_loss_time TEXT,
                    system_active INTEGER DEFAULT 1,
                    compound_trade_number INTEGER DEFAULT 0,
                    updated_at TEXT
                );
                
                CREATE TABLE IF NOT EXISTS market_context (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    date TEXT,
                    economic_events TEXT,
                    macro_snapshot TEXT,
                    session_summaries TEXT,
                    trend_observation TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    price REAL NOT NULL,
                    session TEXT
                );

                CREATE TABLE IF NOT EXISTS ai_cooldown (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_escalation TEXT,
                    direction TEXT
                );

                -- Initialize singleton rows if empty
                INSERT OR IGNORE INTO position_state (id, has_open) VALUES (1, 0);

                INSERT OR IGNORE INTO account_state (id, balance, starting_balance) VALUES (1, 20.09, 16.67);
                INSERT OR IGNORE INTO market_context (id, date) VALUES (1, date('now'));
                INSERT OR IGNORE INTO ai_cooldown (id) VALUES (1);

                -- Indexes for frequently queried columns
                CREATE INDEX IF NOT EXISTS idx_scans_timestamp ON scans(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_deal_id ON trades(deal_id);
                CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
            """)
            # Migrations — ADD COLUMN is idempotent via try/except
            for migration in [
                "ALTER TABLE position_state ADD COLUMN entry_context TEXT",
                "ALTER TABLE trades ADD COLUMN phase TEXT DEFAULT 'initial'",
                "ALTER TABLE trades ADD COLUMN entry_context TEXT",
            ]:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists

            # Create pending_alerts table (replaces pending_alert column in position_state)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expired INTEGER DEFAULT 0
                )
            """)

            # Backfill: copy phase/entry_context from position_state to matching open trade
            try:
                ps = conn.execute("SELECT deal_id, phase, entry_context FROM position_state WHERE id=1 AND has_open=1").fetchone()
                if ps and ps["deal_id"]:
                    conn.execute(
                        "UPDATE trades SET phase = ?, entry_context = ? WHERE deal_id = ? AND closed_at IS NULL AND phase = 'initial'",
                        (ps["phase"] or "initial", ps["entry_context"], ps["deal_id"])
                    )
            except Exception:
                pass  # backfill is best-effort

            logger.info("Database initialized")
    
    def _conn(self) -> sqlite3.Connection:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    # ==========================================
    # SCAN HISTORY
    # ==========================================
    
    def save_scan(self, scan_data: dict):
        """Save a scan result."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO scans (timestamp, session, price, indicators,
                    market_context, analysis, setup_found, confidence, action_taken, api_cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                scan_data.get("timestamp", datetime.now().isoformat()),
                scan_data.get("session"),
                scan_data.get("price"),
                json.dumps(scan_data.get("indicators", {})),
                json.dumps(scan_data.get("market_context", {})),
                json.dumps(scan_data.get("analysis", {})),
                1 if scan_data.get("setup_found") else 0,
                scan_data.get("confidence"),
                scan_data.get("action_taken", "no_trade"),
                scan_data.get("api_cost", 0),
            ))
    
    def get_recent_scans(self, limit: int = 5) -> list[dict]:
        """Get the most recent N scans for context passing."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r) for r in reversed(rows)]
    
    def get_scans_today(self) -> list[dict]:
        """Get all scans from today."""
        today = date.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scans WHERE timestamp LIKE ? ORDER BY id",
                (f"{today}%",)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]
    
    # ==========================================
    # TRADING JOURNAL
    # ==========================================
    
    def log_trade_close(self, deal_id: str, close_data: dict):
        """Update a trade record with close information."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE trades SET
                    closed_at = ?, exit_price = ?, pnl = ?, balance_after = ?,
                    result = ?, duration_minutes = ?, phase_at_close = ?,
                    api_cost = ?, notes = ?
                WHERE deal_id = ?
            """, (
                close_data.get("closed_at", datetime.now().isoformat()),
                close_data.get("exit_price"),
                close_data.get("pnl"),
                close_data.get("balance_after"),
                close_data.get("result"),
                close_data.get("duration_minutes"),
                close_data.get("phase_at_close"),
                close_data.get("api_cost", 0),
                close_data.get("notes"),
                deal_id,
            ))
    
    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        """Get recent trades for journal display."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]
    
    def get_trade_stats(self) -> dict:
        """Calculate win rate, avg P&L, etc."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE pnl IS NOT NULL"
            ).fetchall()
        
        if not rows:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0}
        
        trades = [self._row_to_dict(r) for r in rows]
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
        
        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "total_pnl": sum(t.get("pnl", 0) for t in trades),
            "avg_win": sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.get("pnl", 0) for t in losses) / len(losses) if losses else 0,
            "best_trade": max(t.get("pnl", 0) for t in trades),
            "worst_trade": min(t.get("pnl", 0) for t in trades),
            "avg_confidence": sum(t.get("confidence", 0) for t in trades) / len(trades),
        }
    
    # ==========================================
    # POSITION STATE
    # ==========================================
    
    def get_position_state(self) -> dict:
        """Get current position state from trades table (first open position).
        Returns dict with has_open, deal_id, direction, lots, entry_price,
        stop_level, limit_level, opened_at, phase, confidence, entry_context.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if not row:
            return {"has_open": False}
        d = self._row_to_dict(row)
        return {
            "has_open": True,
            "deal_id": d.get("deal_id"),
            "direction": d.get("direction"),
            "lots": d.get("lots"),
            "entry_price": d.get("entry_price"),
            "stop_level": d.get("stop_loss"),
            "limit_level": d.get("take_profit"),
            "opened_at": d.get("opened_at"),
            "phase": d.get("phase", "initial"),
            "confidence": d.get("confidence"),
            "entry_context": d.get("entry_context"),
            "updated_at": d.get("opened_at"),
        }

    def get_all_position_states(self) -> list[dict]:
        """Get all open positions from trades table."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id ASC"
            ).fetchall()
        result = []
        for row in rows:
            d = self._row_to_dict(row)
            result.append({
                "has_open": True,
                "deal_id": d.get("deal_id"),
                "direction": d.get("direction"),
                "lots": d.get("lots"),
                "entry_price": d.get("entry_price"),
                "stop_level": d.get("stop_loss"),
                "limit_level": d.get("take_profit"),
                "opened_at": d.get("opened_at"),
                "phase": d.get("phase", "initial"),
                "confidence": d.get("confidence"),
                "entry_context": d.get("entry_context"),
                "updated_at": d.get("opened_at"),
            })
        return result

    def set_position_open(self, position: dict):
        """Record a new open position (legacy compat — updates position_state singleton).
        New code should use open_trade_atomic() which writes directly to trades table.
        """
        entry_ctx = position.get("entry_context")
        with self._conn() as conn:
            conn.execute("""
                UPDATE position_state SET
                    has_open = 1, deal_id = ?, direction = ?, lots = ?,
                    entry_price = ?, stop_level = ?, limit_level = ?,
                    opened_at = ?, phase = 'initial', confidence = ?,
                    pending_alert = NULL, updated_at = ?, entry_context = ?
                WHERE id = 1
            """, (
                position.get("deal_id"),
                position.get("direction"),
                position.get("lots"),
                position.get("entry_price"),
                position.get("stop_level"),
                position.get("limit_level"),
                position.get("opened_at", datetime.now().isoformat()),
                position.get("confidence"),
                datetime.now().isoformat(),
                json.dumps(entry_ctx) if entry_ctx else None,
            ))

    def set_position_closed(self, deal_id: str = None):
        """Mark position as closed.
        If deal_id provided: sets closed_at on that specific trade row.
        Also updates legacy position_state singleton for backward compat.
        """
        now = datetime.now().isoformat()
        with self._conn() as conn:
            if deal_id:
                conn.execute(
                    "UPDATE trades SET phase = 'closed' WHERE deal_id = ? AND closed_at IS NULL",
                    (deal_id,)
                )
            # Legacy: clear position_state singleton
            conn.execute("""
                UPDATE position_state SET
                    has_open = 0, deal_id = NULL, direction = NULL,
                    lots = NULL, entry_price = NULL, stop_level = NULL,
                    limit_level = NULL, opened_at = NULL, phase = 'closed',
                    pending_alert = NULL, updated_at = ?
                WHERE id = 1
            """, (now,))

    def update_position_phase(self, deal_id: str, phase: str):
        """Update the exit phase of a specific position."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET phase = ? WHERE deal_id = ? AND closed_at IS NULL",
                (phase, deal_id)
            )
            # Legacy: update position_state if it matches
            conn.execute("""
                UPDATE position_state SET phase = ?, updated_at = ?
                WHERE id = 1 AND deal_id = ?
            """, (phase, datetime.now().isoformat(), deal_id))

    def update_position_levels(self, stop_level=None, limit_level=None, deal_id: str = None):
        """Update SL/TP levels after modification."""
        with self._conn() as conn:
            if deal_id:
                if stop_level is not None:
                    conn.execute(
                        "UPDATE trades SET stop_loss = ? WHERE deal_id = ? AND closed_at IS NULL",
                        (stop_level, deal_id)
                    )
                if limit_level is not None:
                    conn.execute(
                        "UPDATE trades SET take_profit = ? WHERE deal_id = ? AND closed_at IS NULL",
                        (limit_level, deal_id)
                    )
            # Legacy: update position_state
            if stop_level is not None:
                conn.execute(
                    "UPDATE position_state SET stop_level = ?, updated_at = ? WHERE id = 1",
                    (stop_level, datetime.now().isoformat())
                )
            if limit_level is not None:
                conn.execute(
                    "UPDATE position_state SET limit_level = ?, updated_at = ? WHERE id = 1",
                    (limit_level, datetime.now().isoformat())
                )
    
    def get_open_positions_count(self) -> int:
        """Count currently open positions (trades without a close timestamp)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE closed_at IS NULL"
            ).fetchone()
            return row[0] if row else 0

    def get_open_positions(self) -> list:
        """Return all open positions with risk data for portfolio cap calculation."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT deal_id, direction, lots, entry_price, stop_loss FROM trades WHERE closed_at IS NULL"
            ).fetchall()
            return [dict(r) for r in rows]

    def set_pending_alert(self, alert_data: dict):
        """Store a pending trade alert waiting for user confirmation."""
        now = datetime.now().isoformat()
        with self._conn() as conn:
            # Clear any existing non-expired alerts
            conn.execute("UPDATE pending_alerts SET expired = 1 WHERE expired = 0")
            conn.execute(
                "INSERT INTO pending_alerts (alert_data, created_at) VALUES (?, ?)",
                (json.dumps(alert_data), now)
            )
            # Legacy: also write to position_state for backward compat
            conn.execute("""
                UPDATE position_state SET pending_alert = ?, updated_at = ?
                WHERE id = 1
            """, (json.dumps(alert_data), now))

    def get_pending_alert(self) -> Optional[dict]:
        """Get pending trade alert if any."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT alert_data FROM pending_alerts WHERE expired = 0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            try:
                return json.loads(row["alert_data"])
            except (json.JSONDecodeError, TypeError):
                return None
        # Fallback to legacy position_state
        state = self.get_position_state()
        alert = state.get("pending_alert")
        if alert and isinstance(alert, str):
            try:
                return json.loads(alert)
            except json.JSONDecodeError:
                return None
        return alert

    def clear_pending_alert(self):
        """Clear pending alert (expired or rejected)."""
        with self._conn() as conn:
            conn.execute("UPDATE pending_alerts SET expired = 1 WHERE expired = 0")
            # Legacy
            conn.execute(
                "UPDATE position_state SET pending_alert = NULL WHERE id = 1"
            )
    
    # ==========================================
    # ACCOUNT STATE
    # ==========================================
    
    def get_account_state(self) -> dict:
        """Get current account state."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
        state = self._row_to_dict(row) if row else {}
        
        # Reset daily loss if new day
        if state.get("daily_loss_date") != date.today().isoformat():
            self.reset_daily_loss()
            state["daily_loss_today"] = 0
        
        # Reset weekly loss if new week
        if state.get("weekly_loss_start"):
            start = date.fromisoformat(state["weekly_loss_start"])
            if (date.today() - start).days >= 7:
                self.reset_weekly_loss()
                state["weekly_loss"] = 0
        
        return state
    
    def update_account_state(self, **kwargs):
        """Update account state fields. Only whitelisted columns accepted."""
        with self._conn() as conn:
            for key, value in kwargs.items():
                if key not in _ACCOUNT_STATE_COLUMNS:
                    logger.error(f"update_account_state: rejected unknown column '{key}'")
                    continue
                conn.execute(
                    f"UPDATE account_state SET {key} = ?, updated_at = ? WHERE id = 1",
                    (value, datetime.now().isoformat())
                )
    
    def record_trade_result(self, pnl: float, new_balance: float):
        """Update account state after a trade closes."""
        state = self.get_account_state()
        
        updates = {
            "balance": new_balance,
            "total_pnl": (state.get("total_pnl") or 0) + pnl,
        }
        
        if pnl < 0:
            updates["consecutive_losses"] = (state.get("consecutive_losses") or 0) + 1
            updates["last_loss_time"] = datetime.now().isoformat()
            updates["daily_loss_today"] = (state.get("daily_loss_today") or 0) + pnl
            updates["weekly_loss"] = (state.get("weekly_loss") or 0) + pnl
        else:
            updates["consecutive_losses"] = 0  # Reset on win
        
        self.update_account_state(**updates)
    
    def reset_daily_loss(self):
        """Reset daily loss counter."""
        self.update_account_state(daily_loss_today=0, daily_loss_date=date.today().isoformat())
    
    def reset_weekly_loss(self):
        """Reset weekly loss counter."""
        self.update_account_state(weekly_loss=0, weekly_loss_start=date.today().isoformat())
    
    def reset_consecutive_losses(self):
        """Reset consecutive loss counter (high-confidence cooldown bypass)."""
        self.update_account_state(consecutive_losses=0, last_loss_time=None)

    def set_system_active(self, active: bool):
        """Pause or resume the trading system."""
        self.update_account_state(system_active=1 if active else 0)
    
    # ==========================================
    # MARKET CONTEXT
    # ==========================================
    
    def get_market_context(self) -> dict:
        """Get today's accumulated market context."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM market_context WHERE id = 1").fetchone()
        ctx = self._row_to_dict(row) if row else {}
        
        # Reset if new day
        if ctx.get("date") != date.today().isoformat():
            self.reset_market_context()
            return {"date": date.today().isoformat()}
        
        # Parse JSON fields
        for field in ["economic_events", "macro_snapshot", "session_summaries"]:
            if ctx.get(field) and isinstance(ctx[field], str):
                try:
                    ctx[field] = json.loads(ctx[field])
                except json.JSONDecodeError:
                    ctx[field] = {}
        
        return ctx
    
    def update_market_context(self, **kwargs):
        """Update market context fields. Only whitelisted columns accepted."""
        with self._conn() as conn:
            for key, value in kwargs.items():
                if key not in _MARKET_CONTEXT_COLUMNS:
                    logger.error(f"update_market_context: rejected unknown column '{key}'")
                    continue
                if isinstance(value, (dict, list)):
                    value = json.dumps(value)
                conn.execute(
                    f"UPDATE market_context SET {key} = ?, updated_at = ? WHERE id = 1",
                    (value, datetime.now().isoformat())
                )
    
    def reset_market_context(self):
        """Reset market context for a new day."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE market_context SET
                    date = ?, economic_events = NULL, macro_snapshot = NULL,
                    session_summaries = NULL, trend_observation = NULL, updated_at = ?
                WHERE id = 1
            """, (date.today().isoformat(), datetime.now().isoformat()))
    
    # ==========================================
    # HELPERS
    # ==========================================
    
    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite3.Row to a regular dict."""
        if row is None:
            return {}
        d = dict(row)
        # Parse JSON fields
        for key in ["indicators", "market_context", "analysis",
                     "confidence_breakdown", "news_at_entry"]:
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"JSON decode failed for field '{key}': {e}")
        return d
    
    # ==========================================
    # ATOMIC TRADE OPEN (trade log + position state in one transaction)
    # ==========================================

    def open_trade_atomic(self, trade: dict, position: dict) -> int:
        """
        Log trade open AND set position state in a single transaction.
        Trades table is the source of truth. position_state updated for legacy compat.

        Returns trade number on success.
        """
        entry_ctx = position.get("entry_context")
        entry_ctx_json = json.dumps(entry_ctx) if entry_ctx else None
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(trade_number) as max_num FROM trades").fetchone()
            trade_num = (row["max_num"] or 0) + 1

            conn.execute("""
                INSERT INTO trades (trade_number, deal_id, opened_at, direction, lots,
                    entry_price, stop_loss, take_profit, balance_before, confidence,
                    confidence_breakdown, setup_type, session, ai_analysis, news_at_entry,
                    phase, entry_context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_num,
                trade.get("deal_id"),
                trade.get("opened_at", datetime.now().isoformat()),
                trade.get("direction"),
                trade.get("lots"),
                trade.get("entry_price"),
                trade.get("stop_loss"),
                trade.get("take_profit"),
                trade.get("balance_before"),
                trade.get("confidence"),
                json.dumps(trade.get("confidence_breakdown", {})),
                trade.get("setup_type"),
                trade.get("session"),
                trade.get("ai_analysis"),
                json.dumps(trade.get("news_at_entry", [])),
                "initial",
                entry_ctx_json,
            ))

            # Legacy: also update position_state singleton
            conn.execute("""
                UPDATE position_state SET
                    has_open = 1, deal_id = ?, direction = ?, lots = ?,
                    entry_price = ?, stop_level = ?, limit_level = ?,
                    opened_at = ?, phase = 'initial', confidence = ?,
                    pending_alert = NULL, updated_at = ?, entry_context = ?
                WHERE id = 1
            """, (
                position.get("deal_id"),
                position.get("direction"),
                position.get("lots"),
                position.get("entry_price"),
                position.get("stop_level"),
                position.get("limit_level"),
                position.get("opened_at", datetime.now().isoformat()),
                position.get("confidence"),
                datetime.now().isoformat(),
                entry_ctx_json,
            ))

        return trade_num

    # ==========================================
    # PRICE HISTORY (for momentum tracking)
    # ==========================================

    def save_price_point(self, price: float, session: str = None):
        """Save a price reading for momentum calculation."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO price_history (timestamp, price, session) VALUES (?, ?, ?)",
                (datetime.now().isoformat(), price, session)
            )
            # Keep only last 60 readings (1 hour at 1-min intervals)
            conn.execute("""
                DELETE FROM price_history WHERE id NOT IN (
                    SELECT id FROM price_history ORDER BY id DESC LIMIT 60
                )
            """)

    def get_recent_prices(self, n: int = 10) -> list[dict]:
        """Get the last N price readings, oldest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM price_history ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ==========================================
    # AI COOLDOWN (duplicate signal suppression)
    # ==========================================

    def get_ai_cooldown(self) -> Optional[dict]:
        """Get the last AI escalation time and direction."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM ai_cooldown WHERE id = 1").fetchone()
        return dict(row) if row else None

    def set_ai_cooldown(self, direction: str):
        """Record that AI was just escalated. Resets the 30-min cooldown."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE ai_cooldown SET last_escalation = ?, direction = ? WHERE id = 1",
                (datetime.now().isoformat(), direction)
            )

    def is_ai_on_cooldown(self, cooldown_minutes: int = 30) -> bool:
        """Returns True if AI was escalated within the last cooldown_minutes."""
        state = self.get_ai_cooldown()
        if not state or not state.get("last_escalation"):
            return False
        try:
            last = datetime.fromisoformat(state["last_escalation"])
            return (datetime.now() - last).total_seconds() < cooldown_minutes * 60
        except ValueError:
            return False

    def clear_ai_cooldown(self):
        """Reset AI cooldown — called when a position closes so the next scan can escalate immediately."""
        with self._conn() as conn:
            conn.execute("UPDATE ai_cooldown SET last_escalation = NULL, direction = NULL WHERE id = 1")

    def get_api_cost_total(self) -> float:
        """Get total API cost across all scans and trades."""
        with self._conn() as conn:
            scan_cost = conn.execute("SELECT COALESCE(SUM(api_cost), 0) as total FROM scans").fetchone()
            trade_cost = conn.execute("SELECT COALESCE(SUM(api_cost), 0) as total FROM trades").fetchone()
        return (scan_cost["total"] or 0) + (trade_cost["total"] or 0)

    def get_ai_context_block(self, n_trades: int = 20) -> str:
        """
        Build a compact LIVE EDGE TRACKER block for AI prompts (~250 tokens).
        Queries last n_trades closed trades and formats WR by setup_type + session.
        Returns empty string if fewer than 3 closed trades (insufficient data).
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT setup_type, session, pnl, opened_at FROM trades "
                "WHERE pnl IS NOT NULL ORDER BY id DESC LIMIT ?",
                (n_trades,)
            ).fetchall()

        trades = [dict(r) for r in rows]
        if len(trades) < 3:
            return ""

        # Win rate by setup type
        by_setup: dict[str, list] = {}
        by_session: dict[str, list] = {}
        for t in trades:
            st = t.get("setup_type") or "unknown"
            se = t.get("session") or "unknown"
            win = 1 if (t.get("pnl") or 0) > 0 else 0
            by_setup.setdefault(st, []).append(win)
            by_session.setdefault(se, []).append(win)

        def _wr(wins_list: list) -> str:
            n = len(wins_list)
            w = sum(wins_list)
            pct = round(w / n * 100) if n else 0
            return f"{w}W/{n-w}L({pct}%)"

        setup_lines = " | ".join(
            f"{st}:{_wr(v)}" for st, v in sorted(by_setup.items())
        )
        session_lines = " | ".join(
            f"{se}:{_wr(v)}" for se, v in sorted(by_session.items())
        )

        # Current streak
        streak_count, streak_type = 0, ""
        for t in trades:
            win = (t.get("pnl") or 0) > 0
            label = "W" if win else "L"
            if not streak_type:
                streak_type = label
            if label == streak_type:
                streak_count += 1
            else:
                break

        # Time since last win
        last_win_str = "N/A"
        for t in trades:
            if (t.get("pnl") or 0) > 0:
                try:
                    opened = datetime.fromisoformat(t["opened_at"])
                    hours_ago = round((datetime.now() - opened).total_seconds() / 3600, 1)
                    last_win_str = f"{hours_ago}h ago"
                except Exception:
                    pass
                break

        lines = [
            f"LIVE EDGE TRACKER (last {len(trades)} trades):",
            f"  By setup: {setup_lines}",
            f"  By session: {session_lines}",
            f"  Streak: {streak_count}x{streak_type} | Last win: {last_win_str}",
        ]

        # Warn if any category is >10% below backtest baseline
        BASELINES = {
            "bollinger_mid_bounce": 47, "bollinger_lower_bounce": 45,
            "bollinger_upper_rejection": 50, "ema50_rejection": 50,
            "Tokyo": 49, "London": 44, "New York": 48,
        }
        warnings = []
        for key, wins_list in {**by_setup, **by_session}.items():
            baseline = BASELINES.get(key)
            if baseline and len(wins_list) >= 5:
                live_wr = sum(wins_list) / len(wins_list) * 100
                if live_wr < baseline - 10:
                    warnings.append(f"{key} WR={live_wr:.0f}% (baseline {baseline}%) — COLD")
        if warnings:
            lines.append(f"  ⚠ Cold: {' | '.join(warnings)}")

        return "\n".join(lines)

    def save_opus_decision(self, decision: dict) -> None:
        """Persist latest Opus opposite-direction decision for consistency tracking."""
        path = self.data_dir / "opus_last_decision.json"
        try:
            path.write_text(json.dumps(decision))
        except Exception as e:
            logger.warning(f"Failed to save Opus decision: {e}")

    def get_recent_opus_decision(self) -> dict | None:
        """Return last Opus decision if within 30 minutes, else None."""
        path = self.data_dir / "opus_last_decision.json"
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text())
            ts = datetime.fromisoformat(data.get("timestamp", ""))
            if (datetime.now() - ts).total_seconds() < 1800:  # 30 min
                return data
            return None
        except Exception:
            return None
