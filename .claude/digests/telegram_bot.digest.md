# notifications/telegram_bot.py — DIGEST
# Purpose: Telegram bot. Commands, trade alerts, position updates, persistent navigation.

## Module-level helpers
DIV = "─" * 22 (section divider)
_pnl(pts)  → "🟢 +N pts" / "🔴 -N pts" / "⚪ 0 pts" (HTML, bold)
_dir(d)    → "▲ LONG" / "▼ SHORT" (HTML, bold)
_price(p)  → "<code>N,NNN</code>"
_pct(v)    → "🟢/🟡/🔴 N%" based on thresholds (70/50)
_sys(bool) → "🟢 ACTIVE" / "🔴 PAUSED"

## Persistent bottom keyboard (ReplyKeyboardMarkup)
REPLY_KB: 4×2 grid. Sent on /start and /help. is_persistent=True, resize_keyboard=True.
Rows: [📊 Status, 💰 Balance] [📈 Stats, 📒 Journal] [📅 Today, 💸 Cost] [⚡ Force Scan, 🔄 Menu]
_KB_MAP: maps button text → callback_data (or "__menu__" for 🔄 Menu)

## Contextual inline nav keyboards (1-row, appended after each command response)
_NAV: dict[ctx_name → [(label, callback_data)]] — context-aware 3-button rows
_nav_kb(ctx="default") → InlineKeyboardMarkup (1 row of 3 context buttons)
Contexts: status, balance, journal, stats, today, cost, pause, resume, force, kill, close, default

## Text helpers (instance methods, no async)
_status_text()  → str   — full status block
_balance_text() → str   — account balance block
_journal_text() → str | None  — last 5 trades (None if no trades)
_today_text()   → str | None  — today's scans (None if no scans)
_stats_text()   → str   — performance stats block
_cost_text()    → str   — API cost line
Used by both command handlers and _dispatch_menu (DRY).

## class TelegramBot
__init__(storage, ig_client=None)
on_trade_confirm: Optional[Callable]  — set by TradingMonitor after initialize()
on_force_scan: Optional[Callable]     — set by TradingMonitor after initialize()

initialize()      → Creates Application, registers all handlers (CommandHandler + CallbackQueryHandler + MessageHandler)
start_polling()   → Starts Telegram polling (drop_pending_updates=True)
stop()            → Graceful shutdown

## All commands (ParseMode.HTML throughout)
/start  → welcome + sends REPLY_KB
/help   → command list + sends REPLY_KB
/menu   → full inline button panel (Info + Control sections)
/status → mode, position, session, scanning_paused, balance, today P&L + _nav_kb("status")
/balance→ account details, compound plan progress + _nav_kb("balance")
/journal→ last 5 trades (P&L colored) + _nav_kb("journal")
/today  → today's scan history + _nav_kb("today")
/stats  → win rate, avg win/loss, performance + _nav_kb("stats")
/cost   → API costs (today + total) + _nav_kb("cost")
/force  → triggers on_force_scan callback + _nav_kb("force")
/stop, /pause → sets storage.set_system_active(False) + _nav_kb("pause")
/resume → sets storage.set_system_active(True) + _nav_kb("resume")
/close  → confirmation dialog with "Close now" / "Hold" inline buttons
/kill   → EMERGENCY: close position immediately, no confirmation + _nav_kb("kill")

## MessageHandler (_handle_text)
Handles reply-keyboard taps (maps via _KB_MAP → dispatches same as callback)
Falls through to /help for unknown text

## _dispatch_menu(action, context, query=None, message=None)
Shared helper called by both CallbackQueryHandler and MessageHandler.
Reduces duplication between inline-button callbacks and reply-keyboard taps.

## /menu inline button panel
Info row:    📊 Status · 💰 Balance · 📒 Journal · 📅 Today · 📈 Stats · 💸 API Cost
Control row: ⚡ Force Scan · ⏸ Pause · ▶️ Resume · ❌ Close Pos · 🚨 KILL
Section headers (── Info ──, ── Controls ──) use callback_data="noop" → ignored

## Alert methods
send_alert(message: str)                    → plain HTML message
send_trade_alert(trade_data: dict)          → CONFIRM / REJECT inline buttons, stores in self.pending_alert
send_position_update(pnl_pts, phase, price) → milestone or phase change, colored P&L
send_adverse_alert(message, tier, deal_id)  → tier-colored alert with Close now / Hold inline buttons

## Inline button callbacks (CallbackQueryHandler)
confirm_trade     → checks expiry → on_trade_confirm(alert_data) → clears pending_alert
reject_trade      → clears pending_alert, appends REJECTED to message
close_position:<id> → validates deal_id match → run_in_executor(ig.close_position()) → records in DB
hold_position     → appends "Holding position" to message
noop              → no-op
menu_status/balance/journal/today/stats/cost/force/pause/resume/close/kill → same as /commands

## Edge cases handled
- IG not connected (self.ig is None): shows "IG not connected" message
- No open position for /close or /kill: shows "No open position"
- deal_id mismatch on close callback: "Position mismatch — already closed?"
- Double-tap CONFIRM/REJECT: "Alert already processed or expired"
- Alert expiry: checks datetime.now() vs pending_alert["expires_at"]
- Chat ID filter: only responds to TELEGRAM_CHAT_ID

## Standalone helpers (module-level, for legacy/testing)
send_standalone_message(message: str)          [async]
send_standalone_trade_alert(trade_data: dict)  [async]
