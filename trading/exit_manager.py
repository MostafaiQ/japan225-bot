"""
Exit Manager - 3-Phase exit strategy for position management.

Phase 1: Initial protection (entry to +150pts) - fixed SL and TP
Phase 2: Breakeven lock (at +150pts) - move SL to entry + buffer  
Phase 3: Runner mode (75% of TP fast) - remove TP, activate trailing stop

This runs on the position monitor (every 60 seconds), NOT the 2-hour scan.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from config.settings import (
    BREAKEVEN_TRIGGER, BREAKEVEN_BUFFER, SPREAD_ESTIMATE,
    RUNNER_VELOCITY_THRESHOLD, TRAILING_STOP_DISTANCE,
    TRAILING_STOP_INCREMENT, DEFAULT_TP_DISTANCE,
)

logger = logging.getLogger(__name__)


class ExitPhase:
    INITIAL = "initial"         # Phase 1: SL + TP set, waiting
    BREAKEVEN = "breakeven"     # Phase 2: SL moved to BE
    RUNNER = "runner"           # Phase 3: Trailing stop active
    CLOSED = "closed"           # Trade finished


class ExitManager:
    """Manages the 3-phase exit strategy for open positions."""
    
    def __init__(self, ig_client, storage, telegram=None):
        self.ig = ig_client
        self.storage = storage
        self.telegram = telegram
    
    def evaluate_position(self, position: dict) -> dict:
        """
        Evaluate an open position and determine if any action is needed.
        
        Args:
            position: dict with keys:
                deal_id, direction, entry, size, stop_level, limit_level,
                current_price, opened_at, phase
        
        Returns:
            {
                "action": str (none/move_be/activate_runner/close_early),
                "details": str,
                "new_stop": float or None,
                "new_limit": float or None,
                "trailing": bool,
            }
        """
        result = {
            "action": "none",
            "details": "",
            "new_stop": None,
            "new_limit": None,
            "trailing": False,
        }
        
        entry = position.get("entry", 0)
        current = position.get("current_price", 0)
        direction = position.get("direction", "BUY").upper()
        phase = position.get("phase", ExitPhase.INITIAL)
        limit_level = position.get("limit_level")
        opened_at = position.get("opened_at")
        
        if not entry or not current:
            return result
        
        # Calculate current P&L in points
        if direction == "BUY":
            pnl_points = current - entry
        else:
            pnl_points = entry - current
        
        # Time since entry
        time_open = None
        if opened_at:
            try:
                open_dt = datetime.fromisoformat(opened_at)
                time_open = datetime.now() - open_dt
            except ValueError:
                pass
        
        # ---- PHASE 1 -> PHASE 2: Breakeven Lock ----
        if phase == ExitPhase.INITIAL and pnl_points >= BREAKEVEN_TRIGGER:
            if direction == "BUY":
                new_stop = entry + BREAKEVEN_BUFFER  # Buffer for spread
            else:
                new_stop = entry - BREAKEVEN_BUFFER
            
            result.update({
                "action": "move_be",
                "details": (
                    f"Price +{pnl_points:.0f}pts. Moving SL to breakeven "
                    f"({new_stop:.0f}, +{BREAKEVEN_BUFFER}pt buffer for spread)."
                ),
                "new_stop": new_stop,
                "new_limit": limit_level,  # Keep TP unchanged
                "trailing": False,
            })
            return result
        
        # ---- PHASE 2 -> PHASE 3: Runner Detection ----
        if phase == ExitPhase.BREAKEVEN and limit_level:
            tp_distance = abs(limit_level - entry)
            progress = pnl_points / tp_distance if tp_distance > 0 else 0
            
            # Runner condition: reached 75% of TP within first 2 hours
            is_fast = time_open and time_open < timedelta(hours=2)
            is_near_tp = progress >= RUNNER_VELOCITY_THRESHOLD
            
            if is_fast and is_near_tp:
                if direction == "BUY":
                    trail_stop = current - TRAILING_STOP_DISTANCE
                else:
                    trail_stop = current + TRAILING_STOP_DISTANCE
                
                result.update({
                    "action": "activate_runner",
                    "details": (
                        f"RUNNER DETECTED. Price at {progress:.0%} of TP in "
                        f"{time_open.total_seconds()/60:.0f} min. "
                        f"Removing TP, trailing stop at {TRAILING_STOP_DISTANCE}pts."
                    ),
                    "new_stop": trail_stop,
                    "new_limit": None,  # Remove TP to let it run
                    "trailing": True,
                })
                return result
        
        # ---- RUNNER PHASE: Log trailing progress ----
        if phase == ExitPhase.RUNNER:
            result.update({
                "action": "none",
                "details": f"Runner active. P&L: +{pnl_points:.0f}pts. Trailing stop protecting profits.",
            })
        
        # ---- Event proximity check ----
        # This would be called with upcoming_events if available
        
        return result
    
    async def execute_action(self, position: dict, action: dict) -> bool:
        """Execute the recommended exit action via IG API."""
        deal_id = position.get("deal_id")
        
        if action["action"] == "none":
            return True
        
        if action["action"] == "move_be":
            success = self.ig.modify_position(
                deal_id=deal_id,
                stop_level=action["new_stop"],
                limit_level=action["new_limit"],
            )
            if success:
                # Update stored phase
                self.storage.update_position_phase(deal_id, ExitPhase.BREAKEVEN)
                if self.telegram:
                    await self.telegram.send_alert(
                        f"🔒 *SL moved to breakeven*\n"
                        f"Stop: {action['new_stop']:.0f}\n"
                        f"{action['details']}"
                    )
            return success
        
        if action["action"] == "activate_runner":
            # First try trailing stop via API
            success = self.ig.modify_position(
                deal_id=deal_id,
                trailing_stop=True,
                trailing_stop_distance=TRAILING_STOP_DISTANCE,
                trailing_stop_increment=TRAILING_STOP_INCREMENT,
                limit_level=None,  # Remove TP
            )
            
            if not success:
                # Fallback: just move stop up and remove TP
                logger.warning("Trailing stop not available, using manual trail")
                success = self.ig.modify_position(
                    deal_id=deal_id,
                    stop_level=action["new_stop"],
                    limit_level=None,
                )
            
            if success:
                self.storage.update_position_phase(deal_id, ExitPhase.RUNNER)
                if self.telegram:
                    await self.telegram.send_alert(
                        f"🏃 *RUNNER MODE ACTIVATED*\n"
                        f"Trailing stop: {TRAILING_STOP_DISTANCE}pts\n"
                        f"{action['details']}"
                    )
            return success
        
        if action["action"] == "close_early":
            direction = position.get("direction", "BUY")
            size = position.get("size", 0)
            result = self.ig.close_position(deal_id, direction, size)
            if result:
                self.storage.update_position_phase(deal_id, ExitPhase.CLOSED)
                if self.telegram:
                    await self.telegram.send_alert(
                        f"⚡ *Position closed early*\n{action['details']}"
                    )
            return result is not None
        
        return False
    
    def manual_trail_update(self, position: dict) -> Optional[dict]:
        """
        For positions in runner phase where trailing stops aren't available via API.
        Manually ratchet the stop up every 60 seconds.
        """
        phase = position.get("phase")
        if phase != ExitPhase.RUNNER:
            return None
        
        entry = position.get("entry", 0)
        current = position.get("current_price", 0)
        current_stop = position.get("stop_level", 0)
        direction = position.get("direction", "BUY").upper()
        
        if direction == "BUY":
            ideal_stop = current - TRAILING_STOP_DISTANCE
            # Only move stop UP, never down
            if ideal_stop > current_stop:
                return {
                    "action": "manual_trail",
                    "new_stop": ideal_stop,
                    "details": f"Manual trail: moving stop from {current_stop:.0f} to {ideal_stop:.0f}",
                }
        else:
            ideal_stop = current + TRAILING_STOP_DISTANCE
            if ideal_stop < current_stop:
                return {
                    "action": "manual_trail",
                    "new_stop": ideal_stop,
                    "details": f"Manual trail: moving stop from {current_stop:.0f} to {ideal_stop:.0f}",
                }
        
        return None
