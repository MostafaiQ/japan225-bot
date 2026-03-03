"""
Telegram Bot — User interface for the Japan 225 trading bot.

Features:
  - Persistent ReplyKeyboard at the bottom (always-visible quick nav)
  - Context-aware inline nav buttons after every command response
  - Full /menu inline panel on demand
  - /chat or free-text → Claude AI (same as dashboard chat)
  - HTML formatting: 🟢/🔴 P&L, ▲/▼ direction, <code> prices, <b> labels
  - Edge-case handling throughout (IG down, no position, double-tap, etc.)
"""
import asyncio
import html as _html
import logging
from datetime import datetime
from typing import Optional, Callable

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_EXPIRY_MINUTES, AI_COOLDOWN_MINUTES

logger = logging.getLogger(__name__)

# ── HTML formatting helpers ────────────────────────────────────────────────

DIV = "─" * 22


def _pnl(pts: float) -> str:
    """Green / red P&L with sign."""
    if pts > 0:
        return f"🟢 <b>+{pts:.0f} pts</b>"
    if pts < 0:
        return f"🔴 <b>{pts:.0f} pts</b>"
    return f"⚪ <b>0 pts</b>"


def _dir(d: str) -> str:
    return "▲ <b>LONG</b>" if str(d).upper() == "LONG" else "▼ <b>SHORT</b>"


def _price(p) -> str:
    try:
        return f"<code>{float(p):,.0f}</code>"
    except (TypeError, ValueError):
        return "<code>—</code>"


def _pct(v: float) -> str:
    icon = "🟢" if v >= 70 else "🟡" if v >= 50 else "🔴"
    return f"{icon} <b>{v:.0f}%</b>"


def _sys(active: bool) -> str:
    return "🟢 <b>ACTIVE</b>" if active else "🔴 <b>PAUSED</b>"


# ── Persistent bottom keyboard ─────────────────────────────────────────────
# Sent on /start and /help — stays visible until explicitly removed.
# Tapping a button sends its text as a message, handled by _handle_text().

REPLY_KB = ReplyKeyboardMarkup(
    [
        ["📊 Status",    "💰 Balance"],
        ["📈 Stats",     "📒 Journal"],
        ["📅 Today",     "💸 Cost"],
        ["⚡ Force Scan", "🔄 Menu"],
        ["💬 Chat"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Choose an action or type to chat…",
)

# Map reply-keyboard button text → callback data (or special token)
_KB_MAP = {
    "📊 Status":     "menu_status",
    "💰 Balance":    "menu_balance",
    "📈 Stats":      "menu_stats",
    "📒 Journal":    "menu_journal",
    "📅 Today":      "menu_today",
    "💸 Cost":       "menu_cost",
    "⚡ Force Scan": "menu_force",
    "🔄 Menu":       "__menu__",
    "💬 Chat":       "__chat__",
}

# ── Contextual nav keyboards (1-row, shown after each command) ─────────────

_NAV: dict[str, list[tuple[str, str]]] = {
    "status":  [("💰 Balance", "menu_balance"), ("📈 Stats",   "menu_stats"),   ("⚡ Force",   "menu_force")],
    "balance": [("📊 Status",  "menu_status"),  ("📈 Stats",   "menu_stats"),   ("📒 Journal","menu_journal")],
    "journal": [("📊 Status",  "menu_status"),  ("📈 Stats",   "menu_stats"),   ("💰 Balance","menu_balance")],
    "stats":   [("📊 Status",  "menu_status"),  ("📒 Journal","menu_journal"),  ("💰 Balance","menu_balance")],
    "today":   [("📊 Status",  "menu_status"),  ("⚡ Force",   "menu_force"),   ("📒 Journal","menu_journal")],
    "cost":    [("📊 Status",  "menu_status"),  ("📈 Stats",   "menu_stats"),   ("💰 Balance","menu_balance")],
    "pause":   [("▶️ Resume",  "menu_resume"),  ("📊 Status",  "menu_status"),  ("⚡ Force",  "menu_force")],
    "resume":  [("⏸ Pause",   "menu_pause"),   ("📊 Status",  "menu_status"),  ("⚡ Force",  "menu_force")],
    "force":   [("📊 Status",  "menu_status"),  ("💰 Balance","menu_balance"),  ("⏸ Pause",  "menu_pause")],
    "kill":    [("📊 Status",  "menu_status"),  ("💰 Balance","menu_balance"),  ("📒 Journal","menu_journal")],
    "close":   [("📊 Status",  "menu_status"),  ("💰 Balance","menu_balance"),  ("📒 Journal","menu_journal")],
    "default": [("📊 Status",  "menu_status"),  ("💰 Balance","menu_balance"),  ("⚡ Force",  "menu_force")],
}


def _nav_kb(ctx: str = "default") -> InlineKeyboardMarkup:
    """Compact single-row contextual navigation keyboard."""
    btns = _NAV.get(ctx, _NAV["default"])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=cb) for label, cb in btns
    ]])


# ── Main class ─────────────────────────────────────────────────────────────

class TelegramBot:
    """Telegram bot for trade alerts and system control."""

    def __init__(self, storage, ig_client=None):
        self.storage = storage
        self.ig = ig_client
        self.app = None
        self.on_trade_confirm: Optional[Callable] = None
        self.on_force_scan: Optional[Callable] = None

    async def initialize(self):
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        for cmd, fn in [
            ("start",   self._cmd_start),
            ("help",    self._cmd_help),
            ("menu",    self._cmd_menu),
            ("status",  self._cmd_status),
            ("balance", self._cmd_balance),
            ("journal", self._cmd_journal),
            ("today",   self._cmd_today),
            ("stats",   self._cmd_stats),
            ("cost",    self._cmd_cost),
            ("force",   self._cmd_force),
            ("stop",    self._cmd_stop),
            ("pause",   self._cmd_stop),
            ("resume",  self._cmd_resume),
            ("close",   self._cmd_close),
            ("kill",    self._cmd_kill),
            ("chat",    self._cmd_chat),
        ]:
            self.app.add_handler(CommandHandler(cmd, fn))

        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        # Handles reply-keyboard taps and unknown text
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self._handle_text
        ))

        await self.app.initialize()
        logger.info("Telegram bot initialized")

    async def start_polling(self):
        if not self.app:
            await self.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started")

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _status_text(self) -> str:
        pos = self.storage.get_position_state()
        acc = self.storage.get_account_state()
        on_cd = self.storage.is_ai_on_cooldown(AI_COOLDOWN_MINUTES)
        cd_info = self.storage.get_ai_cooldown()
        lines = ["🤖 <b>Japan 225 Bot</b>", DIV]
        if pos.get("has_open"):
            pnl = pos.get("unrealised_pnl", 0) or 0
            tp_raw = pos.get("limit_level")
            tp_str = _price(tp_raw) + " 🟢" if tp_raw else "<i>trailing</i>"
            lines += [
                "📌 <b>Open Position</b>",
                f"Direction: {_dir(pos.get('direction', '?'))}",
                f"Entry:  {_price(pos.get('entry_price', 0))}",
                f"SL:     {_price(pos.get('stop_level', 0))} 🔴",
                f"TP:     {tp_str}",
                f"Phase:  <b>{pos.get('phase', '?')}</b>",
                f"P&amp;L:    {_pnl(pnl)}",
                DIV,
            ]
        else:
            lines += ["💤 <i>No open position</i>", DIV]
        # Scanning state
        if on_cd and cd_info:
            try:
                last = datetime.fromisoformat(cd_info["last_escalation"])
                elapsed = int((datetime.now() - last).total_seconds() / 60)
                remain  = max(0, AI_COOLDOWN_MINUTES - elapsed)
                cd_dir  = cd_info.get("direction", "")
                dir_tag = f" ({cd_dir})" if cd_dir else ""
                lines.append(f"🔍 Scan: ⏳ <b>COOLDOWN{dir_tag}</b> — {remain}m remaining")
            except Exception:
                lines.append("🔍 Scan: ⏳ <b>COOLDOWN</b>")
        else:
            lines.append(f"🔍 Scan: {_sys(acc.get('system_active', True))}")
        lines += [
            DIV,
            "💰 <b>Account</b>",
            f"Balance:  <b>${acc.get('balance', 0):.2f}</b>",
            f"Losses:   {acc.get('consecutive_losses', 0)} consecutive",
        ]
        return "\n".join(lines)

    def _balance_text(self) -> str:
        acc  = self.storage.get_account_state()
        pnl  = acc.get("total_pnl", 0)
        cost = acc.get("total_api_cost", 0)
        net  = pnl - cost
        return "\n".join([
            "💰 <b>Account Balance</b>", DIV,
            f"Current:     <b>${acc.get('balance', 0):.2f}</b>",
            f"Starting:    ${acc.get('starting_balance', 0):.2f}", DIV,
            f"Total P&amp;L:   {'🟢 +' if pnl >= 0 else '🔴 '}${abs(pnl):.2f}",
            f"API costs:   ${cost:.4f}",
            f"Net profit:  {'🟢 +' if net >= 0 else '🔴 '}${abs(net):.2f}", DIV,
            f"Daily loss:  ${abs(acc.get('daily_loss_today', 0)):.2f}",
            f"Weekly loss: ${abs(acc.get('weekly_loss', 0)):.2f}",
        ])

    def _journal_text(self) -> str | None:
        """Returns formatted text or None if no trades."""
        trades = self.storage.get_recent_trades(5)
        if not trades:
            return None
        lines = ["📒 <b>Last 5 Trades</b>", DIV]
        for t in trades:
            pnl  = t.get("pnl") or 0
            sign = "+" if pnl > 0 else ""
            icon = "🟢" if pnl > 0 else "🔴"
            lines.append(
                f"{icon} #{t.get('trade_number')}  {t.get('direction')}  "
                f"<b>{sign}${pnl:.2f}</b>  {t.get('result', '—')}"
            )
        return "\n".join(lines)

    def _today_text(self) -> str | None:
        """Returns formatted text or None if no scans today."""
        scans = self.storage.get_scans_today()
        if not scans:
            return None
        lines = [f"📅 <b>Today's Scans</b>  ({len(scans)} total)", DIV]
        _icons = {
            "cooldown":       "⏳", "haiku_rejected": "🤖",
            "ai_rejected":    "❌", "low_conf":       "📉",
            "event_block":    "🚫", "friday_block":   "🚫",
            "pending":        "📤", "no_setup":       "·",
        }
        for s in scans[-10:]:
            act = (s.get("action_taken") or "").lower()
            act_key = act.replace("_long", "").replace("_short", "")
            icon = _icons.get(act_key, "🔍" if s.get("setup_found") else "·")
            ts = s.get("timestamp", "")
            try:
                t_str = datetime.fromisoformat(ts).strftime("%H:%M")
            except Exception:
                t_str = "—"
            direction = "LONG" if "long" in act else ("SHORT" if "short" in act else None)
            dir_icon  = "▲" if direction == "LONG" else ("▼" if direction == "SHORT" else "·")
            conf      = s.get("confidence")
            conf_str  = f"  {_pct(conf)}" if conf is not None else ""
            sess      = (s.get("session") or "—")[:3].upper()
            lines.append(f"{icon} <code>{t_str}</code> {sess}  {dir_icon}{direction or '—'}{conf_str}")
        return "\n".join(lines)

    def _stats_text(self) -> str:
        s   = self.storage.get_trade_stats()
        pnl = s.get("total_pnl", 0)
        return "\n".join([
            "📈 <b>Performance Stats</b>", DIV,
            f"Total trades: <b>{s.get('total', 0)}</b>",
            f"Wins: 🟢 {s.get('wins', 0)}   Losses: 🔴 {s.get('losses', 0)}",
            f"Win rate:  {_pct(s.get('win_rate', 0))}",
            DIV,
            f"Total P&amp;L:  {'🟢 +' if pnl >= 0 else '🔴 '}${abs(pnl):.2f}",
            f"Avg win:    🟢 ${s.get('avg_win', 0):.2f}",
            f"Avg loss:   🔴 ${s.get('avg_loss', 0):.2f}",
            f"Best:       🏆 ${s.get('best_trade', 0):.2f}",
            f"Worst:      💀 ${s.get('worst_trade', 0):.2f}",
            DIV,
            f"Avg confidence: {_pct(s.get('avg_confidence', 0))}",
        ])

    def _cost_text(self) -> str:
        total = self.storage.get_api_cost_total()
        return f"💸 <b>API Cost (trading AI)</b>\n{DIV}\nTotal: <b>${total:.4f}</b>"

    # ── Send methods (called by monitor.py) ───────────────────────────────

    async def send_alert(self, message: str, parse_mode: str = ParseMode.HTML):
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.error(f"Telegram send_alert failed: {e}")

    async def send_trade_alert(self, trade_data: dict):
        direction = trade_data.get("direction", "LONG")
        conf = trade_data.get("confidence", 0)
        text = "\n".join([
            "🚨 <b>TRADE SIGNAL</b> 🚨",
            DIV,
            f"{_dir(direction)}  |  {trade_data.get('session', '?')}",
            DIV,
            f"Entry:  {_price(trade_data.get('entry', 0))}",
            f"SL:     {_price(trade_data.get('sl', 0))} 🔴  (-${trade_data.get('dollar_risk', 0):.2f})",
            f"TP:     {_price(trade_data.get('tp', 0))} 🟢  (+${trade_data.get('dollar_reward', 0):.2f})",
            f"R:R:    1:{trade_data.get('rr_ratio', 0):.2f}",
            DIV,
            f"Confidence: {_pct(conf)}",
            f"Setup:      {trade_data.get('setup_type', 'N/A')}",
            f"Margin:     ${trade_data.get('margin', 0):.2f}  (free: ${trade_data.get('free_margin', 0):.2f})",
            DIV,
            _html.escape(trade_data.get("reasoning", "")),
            DIV,
            f"⏳ Expires in <b>{TRADE_EXPIRY_MINUTES} min</b>",
        ])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ CONFIRM", callback_data="confirm_trade"),
            InlineKeyboardButton("❌ REJECT",  callback_data="reject_trade"),
        ]])
        try:
            self.storage.set_pending_alert(trade_data)
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            logger.info("Trade alert sent")
        except Exception as e:
            logger.error(f"send_trade_alert failed: {e}")

    async def send_scalp_executed(self, alert_data: dict, scalp_result: dict):
        """Notify user that an Opus-approved scalp trade was auto-executed."""
        direction = alert_data.get("direction", "LONG")
        entry = alert_data.get("entry", 0)
        sl = alert_data.get("sl", 0)
        tp = alert_data.get("tp", 0)
        tp_dist = alert_data.get("scalp_tp_distance", abs(tp - entry))
        sl_dist = alert_data.get("scalp_sl_distance", abs(entry - sl))
        eff_rr = alert_data.get("effective_rr", 0)
        local_conf = alert_data.get("local_confidence", 0)
        confidence = alert_data.get("confidence", 0)
        opus_reason = scalp_result.get("reasoning", "")[:250]

        text = "\n".join([
            "⚡ <b>SCALP AUTO-EXECUTED</b> ⚡",
            DIV,
            f"{_dir(direction)}  |  {alert_data.get('session', '?')}  |  {alert_data.get('setup_type', '?')}",
            DIV,
            f"Entry: {_price(entry)}  |  SL: {_price(sl)}  |  TP: {_price(tp)}",
            f"SL: <b>{sl_dist:.0f}pts</b>  |  TP: <b>{tp_dist:.0f}pts</b>  |  R:R: <b>1:{eff_rr:.1f}</b>",
            f"Lots: {alert_data.get('lots', '?')}  |  Local: {local_conf}%  |  Sonnet: {confidence}%",
            DIV,
            f"<i>Opus:</i> {_html.escape(opus_reason)}",
            DIV,
            "Sonnet rejected — Opus found scalp. Auto-executed.",
        ])
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            logger.info(f"Scalp executed notification sent: {direction}")
        except Exception as e:
            logger.error(f"send_scalp_executed failed: {e}")

    async def send_force_open_alert(self, alert_data: dict):
        """Send a force-open alert when local confidence is 100% but AI rejected.

        Unlike regular trade alerts, force-open does NOT auto-execute.
        User must explicitly press Force Open to proceed.
        """
        direction = alert_data.get("direction", "LONG")
        ai_reasoning = alert_data.get("ai_reasoning", "")
        text = "\n".join([
            "🔓 <b>FORCE OPEN — 100% LOCAL</b> 🔓",
            DIV,
            f"{_dir(direction)}  |  {alert_data.get('session', '?')}",
            DIV,
            f"Entry:  {_price(alert_data.get('entry', 0))}",
            f"SL:     {_price(alert_data.get('sl', 0))} 🔴",
            f"TP:     {_price(alert_data.get('tp', 0))} 🟢",
            DIV,
            f"Setup:      {alert_data.get('setup_type', 'N/A')}",
            f"Local:      🟢 <b>100% (12/12)</b>",
            f"AI:         ❌ <b>REJECTED</b>",
            DIV,
            f"<i>AI reason:</i> {_html.escape(ai_reasoning[:250])}" if ai_reasoning else "",
            DIV,
            _html.escape(alert_data.get("reasoning", "")),
            DIV,
            f"⏳ Expires in <b>{TRADE_EXPIRY_MINUTES} min</b>",
            "⚠️ <b>No auto-execute</b> — requires manual confirmation.",
        ])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Force Open", callback_data="force_open"),
            InlineKeyboardButton("❌ Skip",       callback_data="reject_force"),
        ]])
        try:
            self.storage.set_pending_alert(alert_data)
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            logger.info(f"Force open alert sent: {direction} 100% local, AI rejected")
        except Exception as e:
            logger.error(f"send_force_open_alert failed: {e}")

    async def send_position_update(self, pnl_points: float, phase: str, current_price: float):
        text = "\n".join([
            "📊 <b>Position Update</b>",
            f"P&amp;L:   {_pnl(pnl_points)}",
            f"Phase:  <b>{phase}</b>",
            f"Price:  {_price(current_price)}",
        ])
        await self.send_alert(text)

    async def send_adverse_alert(self, message: str, tier: str, deal_id: str):
        header = {
            "mild":     "⚠️ <b>Adverse Move — Mild</b>",
            "moderate": "🟠 <b>Adverse Move — Moderate</b>",
            "severe":   "🔴 <b>Adverse Move — SEVERE</b>",
        }.get(tier, "⚠️ <b>Adverse Move</b>")
        text = f"{header}\n{DIV}\n{message}"
        if tier in ("moderate", "severe"):
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔴 Close now", callback_data=f"close_position:{deal_id}"),
                InlineKeyboardButton("⏳ Hold",       callback_data="hold_position"),
            ]])
            try:
                await self.app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                return
            except Exception as e:
                logger.error(f"send_adverse_alert failed: {e}")
        await self.send_alert(text)

    async def send_scan_summary(self, scan_data: dict):
        scans_today = len(self.storage.get_scans_today())
        badge = "🔍 <b>SETUP FOUND</b>" if scan_data.get("setup_found") else "—"
        text = (
            f"Scan {scans_today}  |  {scan_data.get('session', '?')}  |  "
            f"{_price(scan_data.get('price', 0))}  |  {badge}"
        )
        await self.send_alert(text)

    # ── Reply-keyboard text handler ────────────────────────────────────────

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route persistent keyboard taps; forward unknown text to Claude chat."""
        text = (update.message.text or "").strip()
        cb = _KB_MAP.get(text)
        if cb == "__menu__":
            await self._cmd_menu(update, context)
            return
        if cb == "__chat__":
            await update.message.reply_text(
                "💬 <b>Chat mode</b> — just type your message and I'll forward it to Claude.\n"
                "Use any keyboard button to go back to bot controls.",
                parse_mode=ParseMode.HTML,
                reply_markup=REPLY_KB,
            )
            return
        if cb:
            await self._dispatch_menu(cb, update.message)
            return
        # No matching button — forward to Claude chat
        await self._claude_chat(update.message, text)

    # ── Claude chat (same backend as dashboard) ─────────────────────────

    async def _cmd_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /chat <message> — or just /chat to explain chat mode."""
        text = (update.message.text or "").strip()
        # Strip the /chat prefix
        msg = text[5:].strip() if len(text) > 5 else ""
        if not msg:
            await update.message.reply_text(
                "💬 <b>Chat mode</b> — just type your message directly.\n"
                "Any text that isn't a button press gets forwarded to Claude.\n\n"
                "Or: <code>/chat your question here</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=REPLY_KB,
            )
            return
        await self._claude_chat(update.message, msg)

    async def _claude_chat(self, msg, text: str):
        """Forward text to Claude chat backend and reply with response."""
        # Send "typing" indicator
        await msg.reply_chat_action("typing")
        try:
            from dashboard.services.claude_client import chat as claude_chat
            # Run in executor (blocking subprocess)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, claude_chat, text, [])
            if not response:
                response = "(no response from Claude)"
            # Telegram max message = 4096 chars. Split if needed.
            for i in range(0, len(response), 4096):
                chunk = response[i:i + 4096]
                await msg.reply_text(chunk, reply_markup=REPLY_KB)
        except Exception as e:
            logger.error(f"Claude chat via Telegram failed: {e}")
            await msg.reply_text(
                f"Claude error: {str(e)[:200]}",
                reply_markup=REPLY_KB,
            )

    # ── Command handlers ───────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 <b>Japan 225 Bot</b> — online.\n\n"
            "The quick-access keyboard is now pinned at the bottom.\n"
            "Tap <b>🔄 Menu</b> for the full control panel.",
            parse_mode=ParseMode.HTML,
            reply_markup=REPLY_KB,
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 <b>Commands</b>\n" + DIV + "\n"
            "<b>Info:</b>\n"
            "/status  — position &amp; account\n"
            "/balance — balance &amp; P&amp;L\n"
            "/journal — last 5 trades\n"
            "/today   — today's scans\n"
            "/stats   — win rate &amp; performance\n"
            "/cost    — API costs\n\n"
            "<b>Controls:</b>\n"
            "/force   — trigger scan now\n"
            "/pause   — pause new entries\n"
            "/resume  — resume scanning\n"
            "/close   — close position (with confirm)\n"
            "/kill    — 🚨 emergency close, no confirm\n\n"
            "Or use the <b>keyboard below</b> for quick access.",
            parse_mode=ParseMode.HTML,
            reply_markup=REPLY_KB,
        )

    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("── Info ─────────────────────", callback_data="noop")],
            [InlineKeyboardButton("📊 Status",  callback_data="menu_status"),
             InlineKeyboardButton("💰 Balance", callback_data="menu_balance")],
            [InlineKeyboardButton("📒 Journal", callback_data="menu_journal"),
             InlineKeyboardButton("📅 Today",   callback_data="menu_today")],
            [InlineKeyboardButton("📈 Stats",   callback_data="menu_stats"),
             InlineKeyboardButton("💸 API Cost",callback_data="menu_cost")],
            [InlineKeyboardButton("── Controls ─────────────────", callback_data="noop")],
            [InlineKeyboardButton("⚡ Force Scan", callback_data="menu_force"),
             InlineKeyboardButton("⏸ Pause",       callback_data="menu_pause")],
            [InlineKeyboardButton("▶️ Resume",      callback_data="menu_resume"),
             InlineKeyboardButton("❌ Close Pos",   callback_data="menu_close")],
            [InlineKeyboardButton("🚨 KILL (emergency close)", callback_data="menu_kill")],
        ])
        await update.message.reply_text(
            "🤖 <b>Japan 225 — Control Panel</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        kb = _nav_kb("status")
        if self.storage.is_ai_on_cooldown(AI_COOLDOWN_MINUTES):
            # Append an Escalate button when bot is on cooldown
            kb = InlineKeyboardMarkup(
                list(kb.inline_keyboard) + [[
                    InlineKeyboardButton("⚡ Escalate to AI now", callback_data="force_escalate")
                ]]
            )
        await update.message.reply_text(
            self._status_text(), parse_mode=ParseMode.HTML, reply_markup=kb
        )

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self._balance_text(), parse_mode=ParseMode.HTML, reply_markup=_nav_kb("balance")
        )

    async def _cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = self._journal_text()
        await update.message.reply_text(
            text or "📒 No trades recorded yet.",
            parse_mode=ParseMode.HTML, reply_markup=_nav_kb("journal"),
        )

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = self._today_text()
        await update.message.reply_text(
            text or "📅 No scans today yet.",
            parse_mode=ParseMode.HTML, reply_markup=_nav_kb("today"),
        )

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self._stats_text(), parse_mode=ParseMode.HTML, reply_markup=_nav_kb("stats")
        )

    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self._cost_text(), parse_mode=ParseMode.HTML, reply_markup=_nav_kb("cost")
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.set_system_active(False)
        await update.message.reply_text(
            "⏸ <b>Scanning PAUSED.</b>\nNo new trades will open.\nUse /resume or tap ▶️ Resume.",
            parse_mode=ParseMode.HTML,
            reply_markup=_nav_kb("pause"),
        )

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.set_system_active(True)
        await update.message.reply_text(
            "▶️ <b>Scanning RESUMED.</b>\nBot is active and scanning.",
            parse_mode=ParseMode.HTML,
            reply_markup=_nav_kb("resume"),
        )

    async def _cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pos = self.storage.get_position_state()
        if not pos.get("has_open"):
            await update.message.reply_text(
                "ℹ️ No open position to close.", reply_markup=_nav_kb("default")
            )
            return
        pnl = pos.get("unrealised_pnl", 0) or 0
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Yes, close now",
                                 callback_data=f"close_position:{pos.get('deal_id')}"),
            InlineKeyboardButton("⏳ Cancel", callback_data="hold_position"),
        ]])
        await update.message.reply_text(
            f"❓ <b>Close position?</b>\n{DIV}\n"
            f"Direction: {_dir(pos.get('direction', '?'))}\n"
            f"Entry:     {_price(pos.get('entry_price', 0))}\n"
            f"SL:        {_price(pos.get('stop_level', 0))}\n"
            f"P&amp;L now:   {_pnl(pnl)}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pos = self.storage.get_position_state()
        if not pos.get("has_open"):
            await update.message.reply_text(
                "ℹ️ No open position.", reply_markup=_nav_kb("default")
            )
            return
        if not self.ig:
            await update.message.reply_text(
                "⚠️ IG client not connected — cannot execute kill.\n"
                "Close the position manually in IG.",
                parse_mode=ParseMode.HTML,
            )
            return
        await update.message.reply_text("🚨 KILL received. Closing immediately...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])
        )
        if result:
            self.storage.set_position_closed()
            await update.message.reply_text(
                "✅ <b>Position KILLED.</b>\nEmergency close executed.",
                parse_mode=ParseMode.HTML,
                reply_markup=_nav_kb("kill"),
            )
        else:
            await update.message.reply_text(
                "❌ <b>Kill FAILED.</b>\nCheck IG immediately — close manually if needed.",
                parse_mode=ParseMode.HTML,
            )

    async def _cmd_force(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "⚡ <b>Force scan triggered.</b>\nRunning on next cycle…",
            parse_mode=ParseMode.HTML,
            reply_markup=_nav_kb("force"),
        )
        if self.on_force_scan:
            asyncio.create_task(self.on_force_scan())

    # ── Menu dispatch (shared by inline callbacks + reply-keyboard handler) ─

    async def _dispatch_menu(self, cb: str, msg):
        """Execute menu action and reply to msg (Message object)."""
        if cb == "menu_status":
            kb = _nav_kb("status")
            if self.storage.is_ai_on_cooldown(AI_COOLDOWN_MINUTES):
                kb = InlineKeyboardMarkup(
                    list(kb.inline_keyboard) + [[
                        InlineKeyboardButton("⚡ Escalate to AI now", callback_data="force_escalate")
                    ]]
                )
            await msg.reply_text(
                self._status_text(), parse_mode=ParseMode.HTML, reply_markup=kb
            )
        elif cb == "menu_balance":
            await msg.reply_text(
                self._balance_text(), parse_mode=ParseMode.HTML, reply_markup=_nav_kb("balance")
            )
        elif cb == "menu_journal":
            text = self._journal_text()
            await msg.reply_text(
                text or "📒 No trades recorded yet.",
                parse_mode=ParseMode.HTML, reply_markup=_nav_kb("journal"),
            )
        elif cb == "menu_today":
            text = self._today_text()
            await msg.reply_text(
                text or "📅 No scans today yet.",
                parse_mode=ParseMode.HTML, reply_markup=_nav_kb("today"),
            )
        elif cb == "menu_stats":
            await msg.reply_text(
                self._stats_text(), parse_mode=ParseMode.HTML, reply_markup=_nav_kb("stats")
            )
        elif cb == "menu_cost":
            await msg.reply_text(
                self._cost_text(), parse_mode=ParseMode.HTML, reply_markup=_nav_kb("cost")
            )
        elif cb == "menu_force":
            await msg.reply_text(
                "⚡ <b>Force scan triggered.</b>",
                parse_mode=ParseMode.HTML, reply_markup=_nav_kb("force"),
            )
            if self.on_force_scan:
                asyncio.create_task(self.on_force_scan())
        elif cb == "menu_pause":
            self.storage.set_system_active(False)
            await msg.reply_text(
                "⏸ <b>Scanning PAUSED.</b>",
                parse_mode=ParseMode.HTML, reply_markup=_nav_kb("pause"),
            )
        elif cb == "menu_resume":
            self.storage.set_system_active(True)
            await msg.reply_text(
                "▶️ <b>Scanning RESUMED.</b>",
                parse_mode=ParseMode.HTML, reply_markup=_nav_kb("resume"),
            )
        elif cb == "menu_close":
            pos = self.storage.get_position_state()
            if not pos.get("has_open"):
                await msg.reply_text("ℹ️ No open position.", reply_markup=_nav_kb("default"))
            else:
                pnl = pos.get("unrealised_pnl", 0) or 0
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔴 Yes, close now",
                                         callback_data=f"close_position:{pos.get('deal_id')}"),
                    InlineKeyboardButton("⏳ Cancel", callback_data="hold_position"),
                ]])
                await msg.reply_text(
                    f"❓ <b>Close position?</b>\n{DIV}\n"
                    f"Direction: {_dir(pos.get('direction', '?'))}\n"
                    f"Entry:     {_price(pos.get('entry_price', 0))}\n"
                    f"P&amp;L now:   {_pnl(pnl)}",
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
        elif cb == "menu_kill":
            pos = self.storage.get_position_state()
            if not pos.get("has_open"):
                await msg.reply_text("ℹ️ No open position.", reply_markup=_nav_kb("default"))
            elif not self.ig:
                await msg.reply_text("⚠️ IG client not connected.", parse_mode=ParseMode.HTML)
            else:
                await msg.reply_text("🚨 KILL received. Closing immediately...")
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, lambda: self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])
                )
                if result:
                    self.storage.set_position_closed()
                    await msg.reply_text(
                        "✅ <b>Position KILLED.</b>",
                        parse_mode=ParseMode.HTML, reply_markup=_nav_kb("kill"),
                    )
                else:
                    await msg.reply_text(
                        "❌ <b>Kill FAILED.</b> Check IG immediately.",
                        parse_mode=ParseMode.HTML,
                    )

    # ── Callback handler ───────────────────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data  = query.data

        if data == "confirm_trade":
            alert = self.storage.get_pending_alert()
            if not alert:
                await query.edit_message_text(
                    "⏰ Alert already processed or expired.", parse_mode=ParseMode.HTML
                )
                return
            ts = alert.get("timestamp", "")
            if ts:
                try:
                    age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                    if age > TRADE_EXPIRY_MINUTES * 60:
                        self.storage.clear_pending_alert()
                        await query.edit_message_text(
                            "⏰ <b>Alert EXPIRED.</b> Setup may no longer be valid.",
                            parse_mode=ParseMode.HTML,
                        )
                        return
                except ValueError:
                    pass
            if not self.on_trade_confirm:
                await query.edit_message_text(
                    "⚠️ Trade execution not connected.", parse_mode=ParseMode.HTML
                )
                return
            self.storage.clear_pending_alert()
            await query.edit_message_text(
                query.message.text + "\n\n✅ <b>CONFIRMED</b> — executing trade…",
                parse_mode=ParseMode.HTML,
            )
            await self.on_trade_confirm(alert)

        elif data == "reject_trade":
            self.storage.clear_pending_alert()
            await query.edit_message_text(
                query.message.text + "\n\n❌ <b>REJECTED</b> by user.",
                parse_mode=ParseMode.HTML,
            )

        elif data == "force_open":
            alert = self.storage.get_pending_alert()
            if not alert:
                await query.edit_message_text(
                    "⏰ Alert already processed or expired.", parse_mode=ParseMode.HTML
                )
                return
            ts = alert.get("timestamp", "")
            if ts:
                try:
                    age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                    if age > TRADE_EXPIRY_MINUTES * 60:
                        self.storage.clear_pending_alert()
                        await query.edit_message_text(
                            "⏰ <b>Alert EXPIRED.</b> Setup may no longer be valid.",
                            parse_mode=ParseMode.HTML,
                        )
                        return
                except ValueError:
                    pass
            if not self.on_trade_confirm:
                await query.edit_message_text(
                    "⚠️ Trade execution not connected.", parse_mode=ParseMode.HTML
                )
                return
            self.storage.clear_pending_alert()
            await query.edit_message_text(
                query.message.text + "\n\n🔓 <b>FORCE OPENED</b> — executing trade…",
                parse_mode=ParseMode.HTML,
            )
            await self.on_trade_confirm(alert)

        elif data == "reject_force":
            self.storage.clear_pending_alert()
            await query.edit_message_text(
                query.message.text + "\n\n❌ <b>SKIPPED</b> by user.",
                parse_mode=ParseMode.HTML,
            )

        elif data.startswith("close_position:"):
            deal_id = data.split(":", 1)[1]
            pos = self.storage.get_position_state()
            if not pos.get("has_open"):
                await query.edit_message_text("ℹ️ Position already closed.")
                return
            if pos.get("deal_id") != deal_id:
                await query.edit_message_text(
                    "⚠️ Deal ID mismatch — position may have changed.", parse_mode=ParseMode.HTML
                )
                return
            if not self.ig:
                await query.edit_message_text(
                    "⚠️ IG client not connected.", parse_mode=ParseMode.HTML
                )
                return
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: self.ig.close_position(pos["deal_id"], pos["direction"], pos["lots"])
            )
            if result:
                self.storage.set_position_closed()
                await query.edit_message_text(
                    query.message.text + "\n\n✅ <b>Position CLOSED.</b>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.edit_message_text(
                    "❌ <b>Close FAILED.</b> Check IG manually.", parse_mode=ParseMode.HTML
                )

        elif data == "hold_position":
            await query.edit_message_text(
                query.message.text + "\n\n⏳ <b>Holding position.</b>",
                parse_mode=ParseMode.HTML,
            )

        elif data == "noop":
            pass

        elif data == "force_escalate":
            self.storage.clear_ai_cooldown()
            if self.on_force_scan:
                await self.on_force_scan()
            await query.edit_message_text(
                query.message.text + "\n\n⚡ <b>Cooldown cleared — escalating to AI on next scan.</b>",
                parse_mode=ParseMode.HTML,
            )

        elif data.startswith("menu_"):
            await self._dispatch_menu(data, query.message)

        else:
            await query.answer("Unknown action.", show_alert=False)


# ── Standalone helpers (for legacy/testing use) ────────────────────────────

async def send_standalone_message(message: str):
    from telegram import Bot
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.HTML
    )


async def send_standalone_trade_alert(trade_data: dict):
    from telegram import Bot
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    direction = trade_data.get("direction", "LONG")
    text = "\n".join([
        "🚨 <b>TRADE SIGNAL</b> 🚨", DIV,
        f"{_dir(direction)}  |  {trade_data.get('session', '?')}",
        DIV,
        f"Entry:  {_price(trade_data.get('entry', 0))}",
        f"SL:     {_price(trade_data.get('sl', 0))} 🔴",
        f"TP:     {_price(trade_data.get('tp', 0))} 🟢",
        f"R:R:    1:{trade_data.get('rr_ratio', 0):.2f}",
        f"Conf:   {_pct(trade_data.get('confidence', 0))}",
        DIV,
        f"⏳ Expires in <b>{TRADE_EXPIRY_MINUTES} min</b>",
    ])
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ CONFIRM", callback_data="confirm_trade"),
        InlineKeyboardButton("❌ REJECT",  callback_data="reject_trade"),
    ]])
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=text,
        parse_mode=ParseMode.HTML, reply_markup=keyboard,
    )
