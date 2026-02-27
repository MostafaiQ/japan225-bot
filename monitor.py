"""
Position Monitor - Always-on process running on Oracle Cloud Free Tier.
Handles:
1. Telegram bot (polling for commands and trade confirmations)
2. Position monitoring every 60 seconds
3. 3-phase exit management (breakeven, runner detection, trailing)
4. Trade execution on user confirmation

This is the "hands" of the system. GitHub Actions is the "brain".
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime

from config.settings import (
    LOG_FORMAT, LOG_LEVEL, MONITOR_INTERVAL_SECONDS,
    TRADING_MODE, CONTRACT_SIZE,
)
from core.ig_client import IGClient
from trading.exit_manager import ExitManager, ExitPhase
from trading.risk_manager import RiskManager
from storage.database import Storage
from notifications.telegram_bot import TelegramBot

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("monitor")


class PositionMonitor:
    """Main monitor process that runs continuously."""
    
    def __init__(self):
        self.storage = Storage()
        self.ig = IGClient()
        self.risk = RiskManager(self.storage)
        self.telegram = TelegramBot(self.storage, self.ig)
        self.exit_manager = ExitManager(self.ig, self.storage, self.telegram)
        self.running = False
    
    async def start(self):
        """Start the monitor process."""
        logger.info("=" * 60)
        logger.info("POSITION MONITOR STARTING")
        logger.info(f"Mode: {TRADING_MODE}")
        logger.info(f"Monitor interval: {MONITOR_INTERVAL_SECONDS}s")
        logger.info("=" * 60)
        
        # Connect to IG
        if not self.ig.connect():
            logger.error("Failed to connect to IG API. Retrying in 30s...")
            await asyncio.sleep(30)
            if not self.ig.connect():
                logger.critical("IG connection failed. Exiting.")
                return
        
        # Initialize Telegram bot
        await self.telegram.initialize()
        
        # Register callbacks
        self.telegram.on_trade_confirm = self._on_trade_confirm
        self.telegram.on_force_scan = self._on_force_scan
        
        # Start Telegram polling
        await self.telegram.start_polling()
        
        # Send startup message
        await self.telegram.send_alert("Monitor started. Watching for positions and commands.")
        
        self.running = True
        
        # Main monitoring loop
        try:
            while self.running:
                await self._monitor_cycle()
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Monitor loop cancelled")
        finally:
            await self._shutdown()
    
    async def _monitor_cycle(self):
        """Single monitoring cycle - runs every 60 seconds."""
        try:
            # Ensure IG connection is alive
            if not self.ig.ensure_connected():
                logger.warning("IG reconnection failed. Skipping cycle.")
                return
            
            # Check for open position
            pos_state = self.storage.get_position_state()
            
            if not pos_state.get("has_open"):
                # No position - check for expired alerts
                alert = self.storage.get_pending_alert()
                if alert:
                    from config.settings import TRADE_EXPIRY_MINUTES
                    try:
                        alert_time = datetime.fromisoformat(alert.get("timestamp", ""))
                        elapsed = (datetime.now() - alert_time).total_seconds() / 60
                        if elapsed > TRADE_EXPIRY_MINUTES:
                            self.storage.clear_pending_alert()
                            await self.telegram.send_alert("Trade alert expired (no confirmation).")
                            logger.info("Pending alert expired")
                    except ValueError:
                        pass
                return
            
            # --- Position is open: Monitor it ---
            
            # Get current market price
            market = self.ig.get_market_info()
            if not market:
                logger.warning("Failed to get market price")
                return
            
            current_price = market.get("bid", 0)
            entry = pos_state.get("entry_price", 0)
            direction = pos_state.get("direction", "BUY")
            phase = pos_state.get("phase", ExitPhase.INITIAL)
            
            # Calculate P&L
            if direction == "BUY":
                pnl_points = current_price - entry
            else:
                pnl_points = entry - current_price
            
            pnl_dollars = pnl_points * pos_state.get("lots", 0) * CONTRACT_SIZE
            
            # Log status
            logger.info(
                f"Position: {direction} | Entry: {entry:.0f} | "
                f"Current: {current_price:.0f} | P&L: {pnl_points:+.0f}pts (${pnl_dollars:+.2f}) | "
                f"Phase: {phase}"
            )
            
            # Check if position still exists on broker side
            live_positions = self.ig.get_open_positions()
            deal_id = pos_state.get("deal_id")
            
            position_exists = any(p.get("deal_id") == deal_id for p in live_positions)
            
            if not position_exists:
                # Position was closed (TP or SL hit on broker side)
                logger.info("Position no longer exists on broker. Logging closure.")
                
                # Get final balance
                account = self.ig.get_account_info()
                new_balance = account.get("balance", 0) if account else 0
                
                # Determine result
                result = "TP_HIT" if pnl_points > 0 else "SL_HIT"
                
                # Log trade close
                opened_at = pos_state.get("opened_at", "")
                duration = 0
                if opened_at:
                    try:
                        open_dt = datetime.fromisoformat(opened_at)
                        duration = int((datetime.now() - open_dt).total_seconds() / 60)
                    except ValueError:
                        pass
                
                self.storage.log_trade_close(deal_id, {
                    "exit_price": current_price,
                    "pnl": pnl_dollars,
                    "balance_after": new_balance,
                    "result": result,
                    "duration_minutes": duration,
                    "phase_at_close": phase,
                })
                
                # Update account state
                self.storage.record_trade_result(pnl_dollars, new_balance)
                self.storage.set_position_closed()
                
                # Notify
                emoji = "" if pnl_dollars > 0 else ""
                await self.telegram.send_alert(
                    f"{emoji} *Trade Closed: {result}*\n"
                    f"P&L: {pnl_points:+.0f}pts (${pnl_dollars:+.2f})\n"
                    f"Balance: ${new_balance:.2f}\n"
                    f"Duration: {duration} min\n"
                    f"Phase: {phase}"
                )
                return
            
            # --- Position exists: Evaluate exit strategy ---
            position_data = {
                "deal_id": deal_id,
                "direction": direction,
                "entry": entry,
                "size": pos_state.get("lots", 0),
                "stop_level": pos_state.get("stop_level"),
                "limit_level": pos_state.get("limit_level"),
                "current_price": current_price,
                "opened_at": pos_state.get("opened_at"),
                "phase": phase,
            }
            
            # Evaluate what action to take
            action = self.exit_manager.evaluate_position(position_data)
            
            if action["action"] != "none":
                logger.info(f"Exit action: {action['action']} - {action['details']}")
                success = await self.exit_manager.execute_action(position_data, action)
                
                if success and action.get("new_stop"):
                    self.storage.update_position_levels(
                        stop_level=action.get("new_stop"),
                        limit_level=action.get("new_limit"),
                    )
            
            # Manual trailing (if API trailing not available)
            if phase == ExitPhase.RUNNER:
                trail_action = self.exit_manager.manual_trail_update(position_data)
                if trail_action:
                    self.ig.modify_position(
                        deal_id=deal_id,
                        stop_level=trail_action["new_stop"],
                    )
                    self.storage.update_position_levels(stop_level=trail_action["new_stop"])
                    logger.info(trail_action["details"])
            
        except Exception as e:
            logger.error(f"Monitor cycle error: {e}", exc_info=True)
            await self.telegram.send_alert(f"Monitor error: {str(e)[:200]}")
    
    async def _on_trade_confirm(self, alert_data: dict):
        """Called when user confirms a trade alert via Telegram."""
        logger.info("Trade CONFIRMED by user. Executing...")
        
        try:
            direction = alert_data.get("direction", "LONG")
            ig_direction = "BUY" if direction == "LONG" else "SELL"
            
            result = self.ig.open_position(
                direction=ig_direction,
                size=alert_data.get("lots", 0.01),
                stop_level=alert_data.get("sl"),
                limit_level=alert_data.get("tp"),
            )
            
            if result and not result.get("error"):
                # Success - record in storage
                balance = self.ig.get_account_info()
                current_balance = balance.get("balance", 0) if balance else 0
                
                trade_num = self.storage.log_trade_open({
                    "deal_id": result.get("deal_id"),
                    "direction": direction,
                    "lots": alert_data.get("lots"),
                    "entry_price": result.get("level"),
                    "stop_loss": result.get("stop_level") or alert_data.get("sl"),
                    "take_profit": result.get("limit_level") or alert_data.get("tp"),
                    "balance_before": current_balance,
                    "confidence": alert_data.get("confidence"),
                    "confidence_breakdown": alert_data.get("confidence_breakdown"),
                    "setup_type": alert_data.get("setup_type"),
                    "session": alert_data.get("session"),
                    "ai_analysis": alert_data.get("ai_analysis"),
                })
                
                self.storage.set_position_open({
                    "deal_id": result.get("deal_id"),
                    "direction": direction,
                    "lots": alert_data.get("lots"),
                    "entry_price": result.get("level"),
                    "stop_level": result.get("stop_level") or alert_data.get("sl"),
                    "limit_level": result.get("limit_level") or alert_data.get("tp"),
                    "confidence": alert_data.get("confidence"),
                })
                
                await self.telegram.send_alert(
                    f"*Trade #{trade_num} OPENED*\n"
                    f"{direction} {alert_data.get('lots')} lots @ {result.get('level'):.0f}\n"
                    f"SL: {result.get('stop_level'):.0f} | TP: {result.get('limit_level'):.0f}\n"
                    f"Monitoring active."
                )
            elif result and result.get("error"):
                await self.telegram.send_alert(
                    f"Trade REJECTED by broker: {result.get('reason')}"
                )
            else:
                await self.telegram.send_alert("Trade execution FAILED. Check logs.")
                
        except Exception as e:
            logger.error(f"Trade execution error: {e}", exc_info=True)
            await self.telegram.send_alert(f"Execution error: {str(e)[:200]}")
    
    async def _on_force_scan(self):
        """Triggered by /force command - runs a scan immediately."""
        await self.telegram.send_alert("Force scan triggered. Running main.py...")
        # Import and run the scan
        from main import run_scan
        await run_scan()
    
    async def _shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down monitor...")
        await self.telegram.send_alert("Monitor shutting down.")
        await self.telegram.stop()
        logger.info("Monitor stopped.")


def main():
    """Entry point for the position monitor."""
    monitor = PositionMonitor()
    
    # Handle graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    def signal_handler(sig, frame):
        logger.info(f"Signal {sig} received. Shutting down...")
        monitor.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        loop.run_until_complete(monitor.start())
    except KeyboardInterrupt:
        monitor.running = False
    finally:
        loop.close()


if __name__ == "__main__":
    main()
