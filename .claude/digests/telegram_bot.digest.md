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

## Alert methods
send_alert(message: str)                    # Plain text message
send_trade_alert(trade_data: dict)          # CONFIRM / REJECT inline buttons + trade details
send_position_update(pnl_pts, phase, price) # Milestone or phase change update
send_adverse_alert(message, tier, deal_id)  # Adverse move with Close now / Hold buttons

## Inline button callbacks
CONFIRM → calls on_trade_confirm(alert_data), clears pending_alert
REJECT  → clears pending_alert, sends rejected msg
Close now → calls ig.close_position(), records close
Hold    → sends "holding" msg

## Standalone helpers (module-level, for legacy/testing)
send_standalone_message(message: str)          [async]
send_standalone_trade_alert(trade_data: dict)  [async]
