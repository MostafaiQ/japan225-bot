# storage/database.py — DIGEST
# Purpose: SQLite persistence. Only written by monitor.py on the VM.
# DB path: storage/data/trading.db

## class Storage
__init__(db_path=None)  # Defaults to settings.DB_PATH
  # Sets self.data_dir = Path(self.db_path).parent (used by save_opus_decision, get_recent_opus_decision)

## Scan history
save_scan(scan_data: dict)                 # Saves to scans table
get_recent_scans(limit=5) -> list[dict]
get_scans_today() -> list[dict]

## Trade log
log_trade_open(trade: dict) -> int          # Returns trade_number
log_trade_close(deal_id: str, close_data: dict)
get_recent_trades(limit=10) -> list[dict]
get_trade_stats() -> dict                   # {total, wins, losses, win_rate, avg_win, avg_loss}

## Position state (trades-based, multi-position — migrated 2026-03-10)
# Source of truth: trades table WHERE closed_at IS NULL (not position_state singleton)
# position_state table kept for legacy compat (still written, never read as primary)
get_position_state() -> dict
  # Queries trades WHERE closed_at IS NULL ORDER BY id ASC LIMIT 1
  # Returns: {has_open, deal_id, direction, entry_price, stop_level, limit_level,
  #            lots, confidence, phase, opened_at, setup_type, entry_context} or {has_open: False}
  # Column mapping: stop_loss→stop_level, take_profit→limit_level
get_all_position_states() -> list[dict]
  # Same query without LIMIT — returns list of ALL open positions
  # Used by monitor._main_cycle(), telegram._status_text(), dashboard get_positions()
set_position_open(position: dict)       # Legacy — writes to position_state only
set_position_closed(deal_id=None)
  # Sets phase='closed' on trades row WHERE deal_id=? AND closed_at IS NULL
  # Also clears legacy position_state singleton
  # IMPORTANT: call log_trade_close() BEFORE this (sets closed_at which get_position_state uses)
update_position_phase(deal_id, phase)
  # Writes to trades WHERE deal_id=? AND closed_at IS NULL + legacy position_state
update_position_levels(deal_id=None, stop_level=None, limit_level=None)
  # Writes to trades WHERE deal_id=? AND closed_at IS NULL + legacy position_state

## Multi-position count (for risk_manager)
get_open_positions_count() -> int
  # Count open positions via trades table (closed_at IS NULL). Use for max_positions check.
get_open_positions() -> list[dict]
  # Returns all open positions: [{deal_id, direction, lots, entry_price, stop_loss}, ...]
  # Used by validate_trade() portfolio risk cap check.

## Pending alert (trade waiting for Telegram confirm)
# Migrated to pending_alerts table (2026-03-10). Falls back to legacy position_state column.
set_pending_alert(alert_data: dict)
get_pending_alert() -> Optional[dict]
clear_pending_alert()

## Account state
get_account_state() -> dict
  # Keys: balance, consecutive_losses, last_loss_time, daily_loss_today,
  #       weekly_loss, system_active, last_updated
update_account_state(**kwargs)
record_trade_result(pnl: float, new_balance: float)
  # Updates consecutive_losses, daily/weekly loss, balance
reset_daily_loss()
reset_weekly_loss()
set_system_active(active: bool)

## Market context
get_market_context() -> dict
update_market_context(**kwargs)
reset_market_context()

## Atomic operations (use these)
open_trade_atomic(trade: dict, position: dict) -> int
  # INSERT into trades (with phase + entry_context) + legacy position_state write.
  # Returns trade_number. Single DB transaction.

## Price history
save_price_point(price, session=None)
get_recent_prices(n=10) -> list[dict]

## AI cooldown
get_ai_cooldown() -> Optional[dict]
set_ai_cooldown(direction: str)              # Sets timestamp + direction
is_ai_on_cooldown(cooldown_minutes=30) -> bool
clear_ai_cooldown()                          # Resets to NULL — called by _handle_position_closed() on trade close

## Cost tracking
get_api_cost_total() -> float

## AI context for prompts (added 2026-03-01)
get_ai_context_block(n_trades=20) -> str
  # Returns compact LIVE EDGE TRACKER string (~250 tokens) for injection into Sonnet/Opus prompts.
  # Queries last n_trades closed trades. Computes WR by setup_type and session.
  # Includes: streak count, last win time, cold-streak warnings vs backtest baselines.
  # Returns "" if fewer than 3 closed trades (insufficient data).
  # Baselines: bb_mid_bounce=47% | bb_lower_bounce=45% | Tokyo=49% | London=44% | NY=48%

## Opus decision persistence (added 2026-03-05)
save_opus_decision(decision: dict) -> None
  # Writes storage/data/opus_last_decision.json. Used for Opus opposite-direction consistency tracking.
  # dict: {direction, viable, confidence, reasoning[:300], timestamp (ISO)}
get_recent_opus_decision() -> dict | None
  # Returns last Opus decision if timestamp < 30 minutes ago, else None.
  # Called before each evaluate_opposite() call to inject consistency context.
