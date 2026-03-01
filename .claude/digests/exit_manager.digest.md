# trading/exit_manager.py — DIGEST
# Purpose: 3-phase exit strategy. Called every 60s by monitor._monitoring_cycle().

## class ExitPhase (constants)
INITIAL = "initial"   BREAKEVEN = "breakeven"   RUNNER = "runner"   CLOSED = "closed"

## class ExitManager
__init__(ig_client, storage, telegram=None)

evaluate_position(position: dict) -> dict
  # Input keys: deal_id, direction (BUY/SELL), entry, size, stop_level, limit_level,
  #             current_price, opened_at, phase
  # Returns: {action, details, new_stop, new_limit, trailing}
  # action values: "none", "move_be", "activate_runner", "close_early"
  #
  # Phase transitions:
  #   INITIAL → BREAKEVEN: pnl_points >= BREAKEVEN_TRIGGER(150)
  #     new_stop = entry ± BREAKEVEN_BUFFER(10)  [+ for BUY, - for SELL]
  #   BREAKEVEN → RUNNER: pnl >= 75% of TP AND time_open < 2 hours (is_fast_trade gate)
  #     new_stop = current ± TRAILING_STOP_DISTANCE(150), new_limit=None (remove TP)
  #   RUNNER: action="none" (manual_trail_update handles movement)

execute_action(position, action) -> bool  [async]
  # Calls ig.modify_position() or ig.close_position() via run_in_executor
  # move_be: storage.update_position_phase(BREAKEVEN), sends Telegram alert
  # activate_runner: tries API trailing stop first, falls back to manual stop placement
  #   storage.update_position_phase(RUNNER), sends Telegram alert
  # close_early: storage.update_position_phase(CLOSED), sends Telegram alert

manual_trail_update(position) -> Optional[dict]
  # For RUNNER phase only. Ratchets stop in direction of profit.
  # BUY: only moves stop UP (ideal = current - 150, only if > current_stop)
  # SELL: only moves stop DOWN (ideal = current + 150, only if < current_stop)
  # Returns: {action:"manual_trail", new_stop, details} or None
