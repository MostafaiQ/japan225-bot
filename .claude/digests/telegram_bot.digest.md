# notifications/telegram_bot.py — DIGEST
# Purpose: Telegram bot. Commands, trade alerts with inline buttons, position updates.

## class TelegramBot
__init__(storage, ig_client=None)

initialize()      # Creates Application, registers handlers
start_polling()   # Starts telegram polling (async, background)
stop()            # Stops polling

## Callbacks (set externally after initialize)
on_trade_confirm: callable  # Set by TradingMonitor after init
on_force_scan: callable     # Set by TradingMonitor after init

## Commands
/menu     → sends grouped inline button panel (Info + Controls sections)
/status   → mode, position state, balance, today P&L
/balance  → account details, compound plan progress
/journal  → last 5 trades
/today    → today's scans
/stats    → win rate, avg win/loss
/cost     → API costs
/force    → triggers on_force_scan callback
/stop or /pause → sets storage.set_system_active(False)
/resume   → sets storage.set_system_active(True)
/close    → sends inline "Close now" / "Hold" buttons (confirmation dialog)
/kill     → EMERGENCY: closes position immediately, no confirmation
/help     → text list of commands + mention /menu
/start    → welcome message, points to /menu

## /menu button panel
Info row:    📊 Status · 💰 Balance · 📒 Journal · 📅 Today · 📈 Stats · 💸 API Cost
Control row: ⚡ Force Scan · ⏸ Pause · ▶️ Resume · ❌ Close Pos · 🚨 KILL
All buttons have callback handlers with full logic (same as text commands).
Section header buttons (── Info ──) use callback_data="noop" → do nothing.

## Alert methods
send_alert(message: str)                    # Plain text message
send_trade_alert(trade_data: dict)          # CONFIRM / REJECT inline buttons + trade details
send_position_update(pnl_pts, phase, price) # Milestone or phase change update
send_adverse_alert(message, tier, deal_id)  # Adverse move with Close now / Hold buttons

## Inline button callbacks
confirm_trade     → calls on_trade_confirm(alert_data), clears pending_alert, checks expiry
reject_trade      → clears pending_alert, appends REJECTED to message
close_position:<id> → calls ig.close_position(), records close (validates deal_id match)
hold_position     → appends "Holding position" to message
noop              → no-op (section header buttons)
menu_status/balance/journal/today/stats/cost → sends reply_text same as /command
menu_force/pause/resume/close/kill → executes same logic as /force /pause /resume /close /kill

## Standalone helpers (module-level, for legacy/testing)
send_standalone_message(message: str)          [async]
send_standalone_trade_alert(trade_data: dict)  [async]
