# storage/database.py — DIGEST
# Purpose: SQLite persistence. Only written by monitor.py on the VM.
# DB path: storage/data/trading.db

## class Storage
__init__(db_path=None)  # Defaults to settings.DB_PATH

## Scan history
save_scan(scan_data: dict)                 # Saves to scans table
get_recent_scans(limit=5) -> list[dict]
get_scans_today() -> list[dict]

## Trade log
log_trade_open(trade: dict) -> int          # Returns trade_number
log_trade_close(deal_id: str, close_data: dict)
get_recent_trades(limit=10) -> list[dict]
get_trade_stats() -> dict                   # {total, wins, losses, win_rate, avg_win, avg_loss}

## Position state (single open position)
get_position_state() -> dict
  # Returns: {has_open, deal_id, direction, entry_price, stop_level, limit_level,
  #            lots, confidence, phase, opened_at} or has_open=False if none
set_position_open(position: dict)
set_position_closed()
update_position_phase(deal_id, phase)
update_position_levels(stop_level=None, limit_level=None)

## Pending alert (trade waiting for Telegram confirm)
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
  # log_trade_open + set_position_open in one DB transaction. Returns trade_number.

## Price history
save_price_point(price, session=None)
get_recent_prices(n=10) -> list[dict]

## AI cooldown
get_ai_cooldown() -> Optional[dict]
set_ai_cooldown(direction: str)              # Sets timestamp + direction
is_ai_on_cooldown(cooldown_minutes=30) -> bool

## Cost tracking
get_api_cost_total() -> float
