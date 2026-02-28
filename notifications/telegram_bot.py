"""
Telegram Bot - User interface for the trading bot.
Sends alerts, receives confirmations, handles commands.

Commands:
    /status  - Current position, balance, today's P&L
    /balance - Account balance and compound plan progress
    /journal - Last 5 trades summary
    /today   - Today's scan history
    /stop    - Pause all scanning
    /resume  - Resume scanning
    /close   - Close any open position immediately
    /force   - Force an immediate scan
    /cost    - API costs this month
    /stats   - Win rate and performance stats
"""
import json
import logging
import asyncio
from datetime import datetime
from typing import Optional, Callable

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_EXPIRY_MINUTES

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot for trade alerts and system control."""
    
    def __init__(self, storage, ig_client=None):
        self.storage = storage
        self.ig = ig_client
        self.app = None
        self.on_trade_confirm: Optional[Callable] = None  # Callback for trade confirmation
        self.on_force_scan: Optional[Callable] = None  # Callback for forced scan
    
    async def initialize(self):
        """Build and initialize the bot application."""
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Register command handlers
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("balance", self._cmd_balance))
        self.app.add_handler(CommandHandler("journal", self._cmd_journal))
        self.app.add_handler(CommandHandler("today", self._cmd_today))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("pause", self._cmd_stop))    # alias
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("close", self._cmd_close))
        self.app.add_handler(CommandHandler("kill", self._cmd_kill))     # emergency close
        self.app.add_handler(CommandHandler("force", self._cmd_force))
        self.app.add_handler(CommandHandler("cost", self._cmd_cost))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("menu", self._cmd_menu))

        # Callback handler for inline buttons
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        
        await self.app.initialize()
        logger.info("Telegram bot initialized")
    
    async def start_polling(self):
        """Start polling for updates (for Oracle Cloud monitor process)."""
        if not self.app:
            await self.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started")
    
    async def stop(self):
        """Stop the bot."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
    
    # ==========================================
    # SEND METHODS (called by other modules)
    # ==========================================
    
    async def send_alert(self, message: str, parse_mode: str = ParseMode.MARKDOWN):
        """Send a simple text alert."""
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
    
    async def send_trade_alert(self, trade_data: dict):
        """
        Send a trade setup alert with CONFIRM/REJECT buttons.

        trade_data should contain:
            direction, entry, sl, tp, lots, confidence, rr_ratio,
            margin, free_margin, setup_type, reasoning, session
        """
        direction = trade_data.get("direction", "LONG")
        sl_label = f"SL: {trade_data.get('sl', 0):.0f} (-${trade_data.get('dollar_risk', 0):.2f})"
        tp_label = f"TP: {trade_data.get('tp', 0):.0f} (+${trade_data.get('dollar_reward', 0):.2f})"

        text = (
            f"SETUP FOUND\n\n"
            f"Direction: {direction}\n"
            f"Entry: {trade_data.get('entry', 0):.0f}\n"
            f"{sl_label}\n"
            f"{tp_label}\n"
            f"R:R: 1:{trade_data.get('rr_ratio', 0):.2f}\n"
            f"Confidence: {trade_data.get('confidence', 0)}%\n"
            f"Margin: ${trade_data.get('margin', 0):.2f} / Free: ${trade_data.get('free_margin', 0):.2f}\n"
            f"Setup: {trade_data.get('setup_type', 'N/A')} | {trade_data.get('session', 'N/A')}\n\n"
            f"{trade_data.get('reasoning', '')}\n\n"
            f"Expires in {TRADE_EXPIRY_MINUTES} min."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Confirm", callback_data="confirm_trade"),
                InlineKeyboardButton("Reject", callback_data="reject_trade"),
            ]
        ])
        
        try:
            # Store the alert data for when user confirms
            self.storage.set_pending_alert(trade_data)
            
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            logger.info("Trade alert sent to Telegram")
        except Exception as e:
            logger.error(f"Failed to send trade alert: {e}")
    
    async def send_scan_summary(self, scan_data: dict):
        """Send a brief scan result update."""
        scans_today = len(self.storage.get_scans_today())
        setup = "Setup found!" if scan_data.get("setup_found") else "No setup"
        
        text = (
            f"Scan {scans_today}/11 | "
            f"{scan_data.get('session', 'N/A')} | "
            f"Price {scan_data.get('price', 0):.0f} | "
            f"RSI {scan_data.get('rsi', 'N/A')} | "
            f"{setup}"
        )
        
        await self.send_alert(text, parse_mode=None)
    
    async def send_position_update(self, pnl_points: float, phase: str, current_price: float):
        """Send periodic position status update (milestones only, not every cycle)."""
        arrow = "+" if pnl_points >= 0 else ""
        text = (
            f"POSITION UPDATE\n"
            f"P&L: {arrow}{pnl_points:.0f} pts | Phase: {phase} | Price: {current_price:.0f}"
        )
        await self.send_alert(text, parse_mode=None)

    async def send_adverse_alert(self, message: str, tier: str, deal_id: str):
        """
        Send an adverse momentum alert with Close/Hold buttons.
        tier: 'mild' | 'moderate' | 'severe'
        """
        # Only add buttons for moderate and severe (mild is informational only)
        if tier in ("moderate", "severe"):
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Close now", callback_data=f"close_position:{deal_id}"),
                    InlineKeyboardButton("Hold", callback_data="hold_position"),
                ]
            ])
            try:
                await self.app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    reply_markup=keyboard,
                )
                return
            except Exception as e:
                logger.error(f"Failed to send adverse alert: {e}")
        # Mild — plain text, no buttons
        await self.send_alert(message, parse_mode=None)
    
    # ==========================================
    # COMMAND HANDLERS
    # ==========================================
    
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Japan 225 Trading Bot active.\nUse /menu for the button panel."
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Tap /menu for the interactive button panel.\n\n"
            "Text commands:\n"
            "/status — position & account\n"
            "/balance — balance details\n"
            "/journal — last 5 trades\n"
            "/today — today's scans\n"
            "/stats — win rate & P&L\n"
            "/cost — API cost total\n"
            "/force — force scan now\n"
            "/pause or /stop — pause new entries\n"
            "/resume — resume scanning\n"
            "/close — close position (confirm)\n"
            "/kill — EMERGENCY close, no confirm"
        )

    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send an interactive button panel grouped by category."""
        keyboard = InlineKeyboardMarkup([
            # Row label (non-functional separator via disabled-looking button)
            [InlineKeyboardButton("── Info ──────────────────", callback_data="noop")],
            [
                InlineKeyboardButton("📊 Status",  callback_data="menu_status"),
                InlineKeyboardButton("💰 Balance", callback_data="menu_balance"),
            ],
            [
                InlineKeyboardButton("📒 Journal", callback_data="menu_journal"),
                InlineKeyboardButton("📅 Today",   callback_data="menu_today"),
            ],
            [
                InlineKeyboardButton("📈 Stats",   callback_data="menu_stats"),
                InlineKeyboardButton("💸 API Cost",callback_data="menu_cost"),
            ],
            [InlineKeyboardButton("── Controls ──────────────", callback_data="noop")],
            [
                InlineKeyboardButton("⚡ Force Scan", callback_data="menu_force"),
                InlineKeyboardButton("⏸ Pause",      callback_data="menu_pause"),
            ],
            [
                InlineKeyboardButton("▶️ Resume",    callback_data="menu_resume"),
                InlineKeyboardButton("❌ Close Pos", callback_data="menu_close"),
            ],
            [
                InlineKeyboardButton("🚨 KILL (emergency close)", callback_data="menu_kill"),
            ],
        ])
        await update.message.reply_text(
            "Japan 225 Bot — Control Panel",
            reply_markup=keyboard,
        )
    
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pos = self.storage.get_position_state()
        acc = self.storage.get_account_state()
        
        if pos.get("has_open"):
            text = (
                f"*Open Position*\n"
                f"Direction: {pos.get('direction')}\n"
                f"Entry: {pos.get('entry_price', 0):.0f}\n"
                f"SL: {pos.get('stop_level', 0):.0f}\n"
                f"TP: {pos.get('limit_level', 'trailing')}\n"
                f"Phase: {pos.get('phase')}\n"
                f"Lots: {pos.get('lots')}\n\n"
            )
        else:
            text = "No open positions.\n\n"
        
        text += (
            f"*Account*\n"
            f"Balance: ${acc.get('balance', 0):.2f}\n"
            f"System: {'ACTIVE' if acc.get('system_active') else 'PAUSED'}\n"
            f"Consec. losses: {acc.get('consecutive_losses', 0)}"
        )
        
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    
    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        acc = self.storage.get_account_state()
        text = (
            f"*Account Balance*\n"
            f"Current: ${acc.get('balance', 0):.2f}\n"
            f"Starting: ${acc.get('starting_balance', 16.67):.2f}\n"
            f"Total P&L: ${acc.get('total_pnl', 0):.2f}\n"
            f"Total API cost: ${acc.get('total_api_cost', 0):.2f}\n"
            f"Net profit: ${(acc.get('total_pnl', 0) - acc.get('total_api_cost', 0)):.2f}\n"
            f"Daily loss: ${abs(acc.get('daily_loss_today', 0)):.2f}\n"
            f"Weekly loss: ${abs(acc.get('weekly_loss', 0)):.2f}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    
    async def _cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        trades = self.storage.get_recent_trades(5)
        if not trades:
            await update.message.reply_text("No trades recorded yet.")
            return
        
        lines = ["*Last 5 Trades*\n"]
        for t in trades:
            emoji = "✅" if (t.get("pnl") or 0) > 0 else "❌"
            lines.append(
                f"{emoji} #{t.get('trade_number')} | "
                f"{t.get('direction')} {t.get('lots')} lots | "
                f"P&L: ${t.get('pnl', 0):.2f} | "
                f"{t.get('result', 'open')}"
            )
        
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    
    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        scans = self.storage.get_scans_today()
        if not scans:
            await update.message.reply_text("No scans today.")
            return
        
        lines = [f"*Today's Scans ({len(scans)} total)*\n"]
        for s in scans[-5:]:  # Last 5 scans
            setup = "SETUP" if s.get("setup_found") else "-"
            lines.append(
                f"{s.get('session', '?')} | "
                f"Price {s.get('price', 0):.0f} | "
                f"{setup}"
            )
        
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    
    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.storage.get_trade_stats()
        text = (
            f"*Performance Stats*\n"
            f"Total trades: {stats.get('total', 0)}\n"
            f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
            f"Win rate: {stats.get('win_rate', 0):.1f}%\n"
            f"Total P&L: ${stats.get('total_pnl', 0):.2f}\n"
            f"Avg win: ${stats.get('avg_win', 0):.2f}\n"
            f"Avg loss: ${stats.get('avg_loss', 0):.2f}\n"
            f"Best trade: ${stats.get('best_trade', 0):.2f}\n"
            f"Worst trade: ${stats.get('worst_trade', 0):.2f}\n"
            f"Avg confidence: {stats.get('avg_confidence', 0):.0f}%"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    
    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        total = self.storage.get_api_cost_total()
        await update.message.reply_text(f"Total API cost: ${total:.2f}")
    
    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.set_system_active(False)
        await update.message.reply_text("System PAUSED. Use /resume to reactivate.")
    
    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.set_system_active(True)
        await update.message.reply_text("System RESUMED. Scanning active.")
    
    async def _cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Close position with inline confirmation."""
        pos = self.storage.get_position_state()
        if not pos.get("has_open"):
            await update.message.reply_text("No open position to close.")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes, close now", callback_data=f"close_position:{pos.get('deal_id')}"),
                InlineKeyboardButton("Cancel", callback_data="hold_position"),
            ]
        ])
        await update.message.reply_text(
            f"Close {pos.get('direction')} position @ entry {pos.get('entry_price', 0):.0f}?\n"
            f"Current stop: {pos.get('stop_level', 0):.0f}",
            reply_markup=keyboard,
        )

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Emergency close — no confirmation required."""
        pos = self.storage.get_position_state()
        if not pos.get("has_open"):
            await update.message.reply_text("No open position.")
            return

        if not self.ig:
            await update.message.reply_text("IG client not connected.")
            return

        await update.message.reply_text("KILL command received. Closing position immediately...")
        result = self.ig.close_position(
            pos["deal_id"], pos["direction"], pos["lots"]
        )
        if result:
            self.storage.set_position_closed()
            await update.message.reply_text("Position KILLED (emergency close).")
        else:
            await update.message.reply_text("Kill FAILED. Check IG immediately.")
    
    async def _cmd_force(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Forcing immediate scan...")
        if self.on_force_scan:
            asyncio.create_task(self.on_force_scan())
    
    # ==========================================
    # CALLBACK HANDLERS (inline buttons)
    # ==========================================
    
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "confirm_trade":
            alert = self.storage.get_pending_alert()
            if not alert:
                await query.edit_message_text("Alert expired or already processed.")
                return
            
            # Check if expired
            alert_time = alert.get("timestamp", "")
            if alert_time:
                try:
                    created = datetime.fromisoformat(alert_time)
                    if (datetime.now() - created).total_seconds() > TRADE_EXPIRY_MINUTES * 60:
                        self.storage.clear_pending_alert()
                        await query.edit_message_text("Alert EXPIRED. Setup may no longer be valid.")
                        return
                except ValueError:
                    pass
            
            # Execute the trade
            if self.on_trade_confirm:
                await self.on_trade_confirm(alert)
                await query.edit_message_text(
                    query.message.text + "\n\nCONFIRMED - Executing trade..."
                )
            else:
                await query.edit_message_text("Trade execution not connected.")
            
            self.storage.clear_pending_alert()
        
        elif query.data == "reject_trade":
            self.storage.clear_pending_alert()
            await query.edit_message_text(
                query.message.text + "\n\nREJECTED by user."
            )

        elif query.data.startswith("close_position:"):
            deal_id = query.data.split(":", 1)[1]
            pos = self.storage.get_position_state()
            if not pos.get("has_open") or pos.get("deal_id") != deal_id:
                await query.edit_message_text("Position already closed or deal ID mismatch.")
                return
            if not self.ig:
                await query.edit_message_text("IG client not connected.")
                return
            result = self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])
            if result:
                self.storage.set_position_closed()
                await query.edit_message_text(query.message.text + "\n\nPosition CLOSED.")
            else:
                await query.edit_message_text("Close FAILED. Check IG manually.")

        elif query.data == "hold_position":
            await query.edit_message_text(query.message.text + "\n\nHolding position.")

        elif query.data == "noop":
            pass  # section header buttons — do nothing

        # ── Menu button callbacks ────────────────────────────────────────
        elif query.data == "menu_status":
            pos = self.storage.get_position_state()
            acc = self.storage.get_account_state()
            if pos.get("has_open"):
                text = (
                    f"*Open Position*\n"
                    f"Direction: {pos.get('direction')}\n"
                    f"Entry: {pos.get('entry_price', 0):.0f}\n"
                    f"SL: {pos.get('stop_level', 0):.0f}\n"
                    f"TP: {pos.get('limit_level', 'trailing')}\n"
                    f"Phase: {pos.get('phase')}\n"
                    f"Lots: {pos.get('lots')}\n\n"
                )
            else:
                text = "No open positions.\n\n"
            text += (
                f"*Account*\n"
                f"Balance: ${acc.get('balance', 0):.2f}\n"
                f"System: {'ACTIVE' if acc.get('system_active') else 'PAUSED'}\n"
                f"Consec. losses: {acc.get('consecutive_losses', 0)}"
            )
            await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        elif query.data == "menu_balance":
            acc = self.storage.get_account_state()
            text = (
                f"*Account Balance*\n"
                f"Current: ${acc.get('balance', 0):.2f}\n"
                f"Starting: ${acc.get('starting_balance', 16.67):.2f}\n"
                f"Total P&L: ${acc.get('total_pnl', 0):.2f}\n"
                f"Total API cost: ${acc.get('total_api_cost', 0):.2f}\n"
                f"Net profit: ${(acc.get('total_pnl', 0) - acc.get('total_api_cost', 0)):.2f}\n"
                f"Daily loss: ${abs(acc.get('daily_loss_today', 0)):.2f}\n"
                f"Weekly loss: ${abs(acc.get('weekly_loss', 0)):.2f}"
            )
            await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        elif query.data == "menu_journal":
            trades = self.storage.get_recent_trades(5)
            if not trades:
                await query.message.reply_text("No trades recorded yet.")
            else:
                lines = ["*Last 5 Trades*\n"]
                for t in trades:
                    emoji = "✅" if (t.get("pnl") or 0) > 0 else "❌"
                    lines.append(
                        f"{emoji} #{t.get('trade_number')} | "
                        f"{t.get('direction')} {t.get('lots')} lots | "
                        f"P&L: ${t.get('pnl', 0):.2f} | "
                        f"{t.get('result', 'open')}"
                    )
                await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

        elif query.data == "menu_today":
            scans = self.storage.get_scans_today()
            if not scans:
                await query.message.reply_text("No scans today.")
            else:
                lines = [f"*Today's Scans ({len(scans)} total)*\n"]
                for s in scans[-5:]:
                    setup = "SETUP" if s.get("setup_found") else "-"
                    lines.append(
                        f"{s.get('session', '?')} | "
                        f"Price {s.get('price', 0):.0f} | "
                        f"{setup}"
                    )
                await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

        elif query.data == "menu_stats":
            stats = self.storage.get_trade_stats()
            text = (
                f"*Performance Stats*\n"
                f"Total trades: {stats.get('total', 0)}\n"
                f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
                f"Win rate: {stats.get('win_rate', 0):.1f}%\n"
                f"Total P&L: ${stats.get('total_pnl', 0):.2f}\n"
                f"Avg win: ${stats.get('avg_win', 0):.2f}\n"
                f"Avg loss: ${stats.get('avg_loss', 0):.2f}\n"
                f"Best trade: ${stats.get('best_trade', 0):.2f}\n"
                f"Worst trade: ${stats.get('worst_trade', 0):.2f}\n"
                f"Avg confidence: {stats.get('avg_confidence', 0):.0f}%"
            )
            await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        elif query.data == "menu_cost":
            total = self.storage.get_api_cost_total()
            await query.message.reply_text(f"Total API cost: ${total:.2f}")

        elif query.data == "menu_force":
            await query.message.reply_text("Forcing immediate scan...")
            if self.on_force_scan:
                asyncio.create_task(self.on_force_scan())

        elif query.data == "menu_pause":
            self.storage.set_system_active(False)
            await query.message.reply_text("System PAUSED. Use Resume button or /resume to reactivate.")

        elif query.data == "menu_resume":
            self.storage.set_system_active(True)
            await query.message.reply_text("System RESUMED. Scanning active.")

        elif query.data == "menu_close":
            pos = self.storage.get_position_state()
            if not pos.get("has_open"):
                await query.message.reply_text("No open position to close.")
            else:
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Yes, close now", callback_data=f"close_position:{pos.get('deal_id')}"),
                        InlineKeyboardButton("Cancel", callback_data="hold_position"),
                    ]
                ])
                await query.message.reply_text(
                    f"Close {pos.get('direction')} position @ entry {pos.get('entry_price', 0):.0f}?\n"
                    f"Current stop: {pos.get('stop_level', 0):.0f}",
                    reply_markup=keyboard,
                )

        elif query.data == "menu_kill":
            pos = self.storage.get_position_state()
            if not pos.get("has_open"):
                await query.message.reply_text("No open position.")
            elif not self.ig:
                await query.message.reply_text("IG client not connected.")
            else:
                await query.message.reply_text("KILL command received. Closing position immediately...")
                result = self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])
                if result:
                    self.storage.set_position_closed()
                    await query.message.reply_text("Position KILLED (emergency close).")
                else:
                    await query.message.reply_text("Kill FAILED. Check IG immediately.")


async def send_standalone_message(message: str):
    """Send a one-off message without running the full bot. For GitHub Actions scans."""
    from telegram import Bot
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message,
        parse_mode=ParseMode.MARKDOWN,
    )


async def send_standalone_trade_alert(trade_data: dict):
    """Send a trade alert with buttons from GitHub Actions."""
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    
    direction = trade_data.get("direction", "LONG")
    emoji = "🟢" if direction == "LONG" else "🔴"
    
    text = (
        f"{emoji} *TRADE SIGNAL*\n\n"
        f"*Direction:* {direction}\n"
        f"*Entry:* {trade_data.get('entry', 0):.0f}\n"
        f"*Stop Loss:* {trade_data.get('sl', 0):.0f}\n"
        f"*Take Profit:* {trade_data.get('tp', 0):.0f}\n"
        f"*R:R:* 1:{trade_data.get('rr_ratio', 0):.2f}\n"
        f"*Lots:* {trade_data.get('lots', 0)}\n"
        f"*Confidence:* {trade_data.get('confidence', 0)}%\n\n"
        f"Expires in {TRADE_EXPIRY_MINUTES} min."
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("CONFIRM", callback_data="confirm_trade"),
            InlineKeyboardButton("REJECT", callback_data="reject_trade"),
        ]
    ])
    
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
