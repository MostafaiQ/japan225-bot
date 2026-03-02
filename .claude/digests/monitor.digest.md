# monitor.py — DIGEST
# Purpose: Main VM process. Entry point. async event loop. Handles scan + monitor + Telegram.

## class TradingMonitor
__init__(): creates Storage, IGClient, RiskManager, TelegramBot, ExitManager, AIAnalyzer,
            WebResearcher. Sets scanning_paused=False, momentum_tracker=None,
            _position_empty_count=0, _force_scan_event=asyncio.Event()
            NOTE: telegram.on_trade_confirm and on_force_scan callbacks set AFTER initialize()

start(): initializes Telegram FIRST (always available), then writes bot_state.json(phase=STARTING),
  then connects IG (3 fast retries). If IG fails → writes phase=IG_DISCONNECTED, sends Telegram alert,
  retries IG every 1 min until recovered (never exits). Once IG connected → startup_sync() → main loop.

startup_sync(): reconciles DB ↔ IG on every restart. 4 cases:
  - IG has / DB none → write DB, init MomentumTracker, alert
  - DB has / IG none → set_position_closed(), log_trade_close, alert
  - Both agree → reinit MomentumTracker from DB state, alert
  - Neither → clean start alert

_main_cycle(): dispatches to _monitoring_cycle or _scanning_cycle based on DB position state.
  Scanning sleep uses asyncio.wait_for(_force_scan_event.wait(), timeout=interval) — interruptible.
  After _scanning_cycle() returns: sets self._next_scan_at = now + timedelta(seconds=interval), calls _write_state().
  Clears _next_scan_at after sleep completes.

self._last_scan_detail: dict — set at every _scanning_cycle outcome. Included in bot_state.json AND /api/status.
  Keys: outcome (no_setup|low_conf|event_block|friday_block|haiku_rejected|ai_rejected|trade_alert),
        direction, confidence, price, setup_type, reason (haiku only)
self._next_scan_at: datetime | None — set before sleep, cleared after. _write_state() computes next_scan_in from this.

_scanning_cycle() writes save_scan() for all active-session outcomes.
  action_taken values always include direction suffix where applicable:
    haiku_rejected_{long|short}, pending_{long|short},
    event_block_{long|short}, friday_block_{long|short}, low_conf_{long|short}, no_setup
  off_hours early return does NOT write scan records (no analysis done).
  NO COOLDOWNS — subscription is $0/call, always scan every 5 min.

_scanning_cycle() -> int (sleep seconds):
  1. get_current_session() → skip if not active
  2. is_no_trade_day() → skip
  3. check scanning_paused
  4. ig.get_market_info() → 1 API call
  5. asyncio.gather(15M, Daily, 5M) → 3 parallel calls (PRE_SCREEN_CANDLES=220, DAILY_EMA200_CANDLES=250, MINUTE_5_CANDLES=100)
  6. detect_setup(tf_daily, {}, tf_15m) — 15M tried first
  6b. 5M FALLBACK: if 15M no setup + tf_5m available → detect_setup(tf_daily, {}, tf_5m)
      → _5m_aligns_with_15m() guard: LONG needs 15M RSI<65 + price within 300pts of 15M BB mid/lower
        SHORT needs 15M RSI>35 + price within 300pts of 15M BB upper. Missing data → pass through.
      → setup["type"] += "_5m", entry_timeframe="5m". Logged: "5M fallback: LONG bollinger_mid_bounce_5m"
      → entry_tf for Haiku RSI/volume = tf_5m when entry_timeframe="5m", else tf_15m
  7. fetch 4H candles (AI_ESCALATION_CANDLES=220) → 1 API call (daily already fetched in step 5, reused here)
  8. researcher.research() → web data
  9. compute_confidence() → extract criteria dict (11 criteria, C1-C11)
  9b. HARD BLOCKS: C7(no_event_1hr) + C8(no_friday_monthend) fail → skip immediately
  9c. if score < HAIKU_MIN_SCORE (60%) → skip (true technical junk)
  9d. precheck_with_haiku() with full context: web_research, failed_criteria, indicators, live_edge
       → if Haiku rejects: save scan record, return (no cooldown)
  9e. write_context() — generates storage/context/*.md for Claude CLI to read
  9f. market_context["prescreen_setup"] = {type, reasoning, session} — injected before Sonnet call
  10. scan_with_sonnet(haiku_reasoning=...) — Haiku's reason injected into Sonnet prompt
      → U2: log Sonnet reasoning (first 200 chars)
  11. U1: if Sonnet rejected but conf >= threshold-5 → Opus second opinion (with Haiku+Sonnet reasoning)
      OR: if Sonnet found=True and 75≤conf<87 → Opus devil's advocate (with Haiku+Sonnet reasoning)
      **Cumulative chain**: Opus sees both Haiku and Sonnet reasoning in PRIOR AI ASSESSMENTS block
  12. save_scan(), action_taken = pending_* (if confirmed) or ai_rejected_* (if not)
  13. risk.validate_trade(), set_pending_alert(), telegram.send_trade_alert()

_monitoring_cycle(pos_state):  # every 2s price check; position existence check every 15 cycles (30s)
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

_on_force_scan(): sends alert + sets _force_scan_event (wakes scanning sleep immediately)

_shutdown(): alerts Telegram, stops telegram polling, closes researcher

## Main entry
main(): creates monitor, runs asyncio loop, handles SIGINT/SIGTERM
