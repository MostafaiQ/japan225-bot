# trading/exit_manager.py — DIGEST
# Purpose: Position exit management. SL/TP fixed at entry — no mechanical modifications.
# Updated 2026-03-10: breakeven and trailing removed. Only close_early (Opus evaluator) remains.

## class ExitPhase (constants)
INITIAL = "initial"   CLOSED = "closed"
BREAKEVEN = "breakeven"  RUNNER = "runner"  # Legacy, kept for DB compatibility

## class ExitManager
__init__(ig_client, storage, telegram=None)

evaluate_position(position: dict) -> dict
  # Always returns action="none". SL/TP fixed at entry.
  # No breakeven trigger, no trailing stop, no runner mode.

execute_action(position, action) -> bool  [async]
  # Only handles close_early (called by Opus position evaluator in monitor.py)
  # Calls ig.close_position() + storage.update_position_phase(CLOSED) + Telegram alert

manual_trail_update(position) -> None
  # Disabled. Always returns None.
