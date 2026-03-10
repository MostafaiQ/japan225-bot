"""
Exit Manager - Position exit evaluation.

SL and TP are fixed at entry (set by AI). No mechanical modifications.
Position evaluation is done by Opus AI every 120s in monitor.py.
This module is kept for API compatibility but all exit logic is disabled.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ExitPhase:
    INITIAL = "initial"         # SL + TP set at entry, fixed
    CLOSED = "closed"           # Trade finished
    # Legacy phases kept for DB compatibility
    BREAKEVEN = "breakeven"
    RUNNER = "runner"


class ExitManager:
    """Position exit manager. SL/TP fixed at entry — no mechanical modifications."""

    def __init__(self, ig_client, storage, telegram=None):
        self.ig = ig_client
        self.storage = storage
        self.telegram = telegram

    def evaluate_position(self, position: dict) -> dict:
        """No mechanical exit modifications. Returns 'none' always."""
        return {
            "action": "none",
            "details": "SL/TP fixed at entry",
            "new_stop": None,
            "new_limit": None,
            "trailing": False,
        }

    async def execute_action(self, position: dict, action: dict) -> bool:
        """Execute exit action (close_early only, called by Opus evaluator)."""
        if action["action"] == "none":
            return True

        if action["action"] == "close_early":
            deal_id = position.get("deal_id")
            direction = position.get("direction", "BUY")
            size = position.get("size", 0)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self.ig.close_position, deal_id, direction, size
            )
            if result:
                self.storage.update_position_phase(deal_id, ExitPhase.CLOSED)
                if self.telegram:
                    await self.telegram.send_alert(
                        f"*Position closed early*\n{action['details']}"
                    )
            return result is not None

        return False

    def manual_trail_update(self, position: dict) -> Optional[dict]:
        """Trailing disabled. Returns None always."""
        return None
