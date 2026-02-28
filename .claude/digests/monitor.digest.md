# monitor.py — DIGEST
# Purpose: Main VM process. Entry point. async event loop. Handles scan + monitor + Telegram.

## class TradingMonitor
__init__(): creates Storage, IGClient, RiskManager, TelegramBot, ExitManager, AIAnalyzer,
            WebResearcher. Sets scanning_paused=False, momentum_tracker=None,
            _position_empty_count=0
            NOTE: telegram.on_trade_confirm and on_force_scan callbacks set AFTER initialize()

start(): initializes Telegram FIRST (always available), then connects IG (3 fast retries).
  If IG fails → sends Telegram alert, retries IG every 5 min until recovered (never exits).
  Once IG connected → startup_sync() → main loop.

startup_sync(): reconciles DB ↔ IG on every restart. 4 cases:
  - IG has / DB none → write DB, init MomentumTracker, alert
  - DB has / IG none → set_position_closed(), log_trade_close, alert
  - Both agree → reinit MomentumTracker from DB state, alert
  - Neither → clean start alert

_main_cycle(): dispatches to _monitoring_cycle or _scanning_cycle based on DB position state

_scanning_cycle() -> int (sleep seconds):
  1. get_current_session() → skip if not active
  2. is_no_trade_day() → skip
  3. check scanning_paused
  4. ig.get_market_info() → 1 API call
  5. asyncio.gather(ig.get_prices("MINUTE_15", 50), ig.get_prices("DAY", 100)) → 2 parallel calls
  6. detect_setup(tf_daily, {}, tf_15m) — tf_daily is real (above_ema200_fallback is bool)
  7. storage.is_ai_on_cooldown(30min)
  8. fetch 4H candles → 1 API call (daily already fetched in step 5, reused here)
  9. researcher.research() → web data
  10. compute_confidence() → if score < 50: skip
  11. set_ai_cooldown(), scan_with_sonnet()
  12. if Sonnet >=70%: confirm_with_opus()
  13. save_scan(), risk.validate_trade()
  14. set_pending_alert(), telegram.send_trade_alert()

_monitoring_cycle(pos_state):
  1. ig.get_open_positions() → check POSITIONS_API_ERROR sentinel
  2. consecutive empty check (SAFETY_CONSECUTIVE_EMPTY=2)
  3. ig.get_market_info() → current price
  4. momentum_tracker.add_price(), should_alert()
  5. SEVERE tier + Phase.INITIAL → auto-move SL to entry+BREAKEVEN_BUFFER
  6. exit_manager.evaluate_position() → execute_action()
  7. Phase.RUNNER → manual_trail_update()

_handle_position_closed(pos_state): logs trade, sends Telegram, resets momentum_tracker=None

_on_trade_confirm(alert_data): re-fetches price, checks drift vs PRICE_DRIFT_ABORT_PTS,
  calls ig.open_position(), open_trade_atomic(), inits MomentumTracker

_on_force_scan(): sends alert (actual interrupt not implemented — next cycle picks up naturally)

_shutdown(): alerts Telegram, stops telegram polling, closes researcher

## Main entry
main(): creates monitor, runs asyncio loop, handles SIGINT/SIGTERM
