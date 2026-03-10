# monitor.py — DIGEST (updated 2026-03-05)
# Purpose: Main VM process. Entry point. async event loop. Handles scan + monitor + Telegram.

## class TradingMonitor
__init__(): creates Storage, IGClient, RiskManager, TelegramBot, ExitManager, AIAnalyzer,
            WebResearcher. Sets scanning_paused=False, momentum_tracker=None (legacy),
            _position_trackers={} (per deal_id: {momentum_tracker, price_buffer, opus_eval_counter, buffer_save_counter, empty_count, check_counter}),
            _force_scan_event=asyncio.Event(),
            _streaming_reconnect_counter=0 (consecutive cycles without streaming price → triggers reconnect)
            NOTE: telegram.on_trade_confirm and on_force_scan callbacks set AFTER initialize()

start(): initializes Telegram FIRST (always available), then writes bot_state.json(phase=STARTING),
  then connects IG (3 fast retries). If IG fails → writes phase=IG_DISCONNECTED, sends Telegram alert,
  retries IG with exponential backoff (60s→120s→240s→300s max) until recovered (never exits).
  _main_cycle() also uses backoff on ensure_connected() failures: 60→120→240→300s cap, _ig_fail_count tracks attempts.
  Once IG connected → startup_sync() → ig.start_streaming() → main loop.
  start_streaming() is best-effort: logs warning and falls back to REST on failure.

startup_sync(): reconciles DB ↔ IG on every restart. Multi-position aware (2026-03-10).
  Iterates ALL IG positions and ALL DB positions independently:
  - IG has / DB none → open_trade_atomic(), init tracker, alert (per position)
  - DB has / IG none → set_position_closed(deal_id), log_trade_close, alert (per position)
  - Both match (deal_id) → reinit tracker from DB state, alert
  - Neither → clean start alert
  - POSITIONS_API_ERROR → abort, no DB changes, send warning

_main_cycle(): calls get_all_position_states(), iterates _monitoring_cycle(pos) for each.
  Single _check_all_positions_exist() call (one IG API call for all positions).
  If under MAX_OPEN_POSITIONS cap: allows concurrent scanning.
  Scanning sleep uses asyncio.wait_for(_force_scan_event.wait(), timeout=interval) — interruptible.
  After _scanning_cycle() returns: sets self._next_scan_at = now + timedelta(seconds=interval), calls _write_state().
  Clears _next_scan_at after sleep completes.

self._last_scan_detail: dict — set at every _scanning_cycle outcome. Included in bot_state.json AND /api/status.
  Keys: outcome (no_setup|low_conf|event_block|friday_block|ai_rejected|trade_alert),
        direction, confidence, price, setup_type
self._next_scan_at: datetime | None — set before sleep, cleared after. _write_state() computes next_scan_in from this.

_scanning_cycle() writes save_scan() for all active-session outcomes.
  action_taken values always include direction suffix where applicable:
    pending_{long|short}, ai_rejected_{long|short},
    event_block_{long|short}, friday_block_{long|short}, low_conf_{long|short}, no_setup
  off_hours early return does NOT write scan records (no analysis done).
  NO COOLDOWNS — subscription is $0/call, always scan every 5 min.
  No-setup reasoning truncated to 200 chars in log output (was 80).

_scanning_cycle() -> int (sleep seconds):
  1. get_current_session() → skip if not active
  2. is_no_trade_day() → skip
  3. check scanning_paused
  4. ig.get_market_info() → 1 API call
  5. asyncio.gather(15M, 5M, 4H) parallel → then await Daily sequential (avoids 28 req/min burst)
     Cold start: all 4 sequential (rate limit). Warm: 5M+15M+4H parallel, Daily time-gated.
     All use candle caching: full fetch on first call, delta on subsequent (see ig_client.digest.md)
  5b. Extreme day detection: if tf_daily high-low > EXTREME_DAY_RANGE_PTS (1000pts) → logs warning.
  5c. SESSION CONTEXT (NEW 2026-03-05): after indicators dict built, calls compute_session_context(candles_15m, candles_daily).
      Also calls ig.get_tick_density(). Injects results into indicators["indicators_snapshot"]:
        session_open, asia_high, asia_low, pdh_daily, pdl_daily, prev_week_high, prev_week_low, gap_pts,
        tick_density_signal, tick_density_latest. Same injection done in momentum bypass path.
  6. BIDIRECTIONAL detect_setup(): two calls with exclude_direction="SHORT" and "LONG"
  6b. 5M FALLBACK (per-direction): if 15M no setup → try 5M with _5m_aligns_with_15m() guard
      → LONG: 15M RSI<65 + price within 300pts of 15M BB mid/lower
        SHORT: 15M RSI>35 + price within 300pts of 15M BB upper. Missing data → pass through.
      → setup["type"] += "_5m", entry_timeframe="5m".
  7. researcher.research() → web data
  8. BIDIRECTIONAL compute_confidence(): scored for BOTH directions that found setups
     → Primary = highest confidence score. Secondary stored as context for AI.
     → If only one direction found: that's primary, no secondary.
  8b. HARD BLOCKS: C7(no_event_1hr) + C8(no_friday_monthend) fail → skip immediately
  8c. if score < HAIKU_MIN_SCORE (60%) → skip (true technical junk)
  8d. market_context["prescreen_setup"] + market_context["secondary_setup"] — injected before Sonnet
  9. SEQUENTIAL AI LAUNCH (via run_in_executor):
      9a. Sonnet scan launches (single subprocess, Sonnet 4.6 + Opus sub-agent)
      9b. await Sonnet result
      9c. If Sonnet approves → proceed to risk validation + execute
      9d. If Sonnet rejects (conf >= 30%) → check if OPPOSITE direction has detected setup + conf >= 60%
          → If yes: evaluate_opposite() — Opus evaluates opposite direction as SWING trade (full context)
          → Gate: _opposite_conf.score >= 60 AND _opposite_setup.found AND (sonnet_conf >= 30 OR parse_error)
            parse_error = sonnet_conf==0 AND found==False (JSON parse failure fallback — don't block valid opposite)
          → COUNTER GATE (NEW 2026-03-05): fires if Sonnet sets counter_signal == opposite_direction
            AND sonnet_conf <= 45%. Does NOT require pre-detected opposite setup (that's the whole point).
            Fixes: Sonnet identifies LONG opportunity during SHORT eval even when local screening found nothing.
          → If Opus approves opposite at >= 70%/75%: risk validate + execute via send_trade_alert + _on_trade_confirm
          → Consistency tracking: storage.save_opus_decision() / storage.get_recent_opus_decision() (30-min persistence)
      9e. If Sonnet conf < 30% → skip Opus entirely (clear reject)
      NOTE: momentum bypass (no formal setup) still uses evaluate_scalp() → _execute_scalp() path (separate)
  12. save_scan(), action_taken = pending_* (if confirmed) or ai_rejected_* (if not)
  13. risk.validate_trade(), set_pending_alert(), telegram.send_trade_alert()

_monitoring_cycle(pos_state):  # every 2s price check; per-position tracker via _get_tracker(deal_id)
  1. Position existence: handled by _check_all_positions_exist() in _main_cycle (single IG call)
  2. STREAMING PRICE: ig.get_streaming_price() — if fresh (<10s old) → use as current_price
     FALLBACK (streaming None/stale): REST ig.get_market_info(). After 30 consecutive REST cycles (60s)
     → background asyncio.create_task(_try_reconnect_streaming()) to restore streaming.
     REST fallback is transparent — position monitoring continues uninterrupted.
  3. tracker = _get_tracker(deal_id) → per-position momentum_tracker, price_buffer, eval counter
  4. momentum_tracker.add_price(), should_alert()
  5. SEVERE adverse tier + Phase.INITIAL → auto-move SL to entry+BREAKEVEN_BUFFER (safety net, kept)
  6. exit_manager.evaluate_position() → execute_action()
  7. Phase.RUNNER → manual_trail_update()
  8. Per-position price_buffer: rolling 30-price deque (last 60s of prices)
     Per-position opus_eval_counter: resets at OPUS_POSITION_EVAL_EVERY_N=60
     Every 60 cycles (120s): run_in_executor(ai.evaluate_open_position(...))
       → telegram.send_position_eval(eval_result)
       → if CLOSE_NOW and conf >= 70: auto-close position

_check_all_positions_exist(): single ig.get_open_positions() call, checks all DB positions against IG.
  Per-position empty_count tracking. Triggers _handle_position_closed(pos) after SAFETY_CONSECUTIVE_EMPTY=2.

_get_tracker(deal_id) -> dict: returns or creates per-position tracker dict.
_remove_tracker(deal_id): cleans up tracker on position close.
Per-position price buffer files: price_buffer_{deal_id}.json

_handle_position_closed(pos_state): logs trade, sends Telegram, calls set_position_closed(deal_id), _remove_tracker(deal_id)

_auto_execute_after_timeout(alert_data, timeout_secs=120): asyncio background task.
  Waits 120s, then checks if pending alert still exists (user didn't respond).
  If same alert still pending → auto-execute via _on_trade_confirm(). Sends Telegram "Auto-executing" notice.

_execute_scalp(scalp_result, direction, setup, session, current_price, local_conf, final_confidence, indicators_snapshot=None):
  Auto-execute Opus-approved scalp. Uses Opus's structure-based SL (60-120pts) and TP (150-300pts).
  Validates R:R >= 1.5 after spread. Gets balance, computes lots. Passes indicators_snapshot to validate_trade().
  Calls send_scalp_executed() then _on_trade_confirm().

Near-miss flow (in _scanning_cycle, after AI rejection):
  Triggers when: local_score >= min_conf AND not QUICK REJECT
  → Builds enriched opus_reasoning: primary rejection + secondary setup context (if available)
  → evaluate_scalp(primary_direction=direction) via Opus — BIDIRECTIONAL single call
  → Opus evaluates BOTH directions using Sonnet's rejection reasoning + secondary setup as context
  → Opus picks the best direction (may differ from pre-screen direction) or rejects both
  → if scalp_viable → _execute_scalp(direction=opus_direction) (auto-execute, no user confirmation)
  Mechanical bidirectional retry REMOVED — Opus handles both directions in one call.

Force Open flow (in _scanning_cycle, after AI rejection):
  Triggers when: local_score >= 100 (9/9 weighted criteria pass)
  → computes lots (balance + risk), builds force_alert dict
  → telegram.send_force_open_alert() → user sees Force Open / Skip buttons
  → 15min TTL, NO auto-execute. User must explicitly click Force Open.
  → on Force Open: same _on_trade_confirm() path as regular trades

_on_trade_confirm(alert_data): protected by _trade_execution_lock (asyncio.Lock).
  Re-checks position-open under lock. Validates: loss cooldown, daily loss limit, system paused.
  Re-fetches live price, checks drift. Uses stop_distance/limit_distance (not absolute levels).
  ATR GATE: compute_atr(15M_cache, 14) == 0 → abort (market just opened, <15 candles). All sessions.
  CONSECUTIVE LOSSES: uses MAX_CONSECUTIVE_LOSSES=2 for all sessions (Tokyo no longer has special threshold).
  calls ig.open_position(size=_final_lots), open_trade_atomic(), inits MomentumTracker

_on_force_scan(): sends alert + sets _force_scan_event (wakes scanning sleep immediately)

_shutdown(): stops streaming (ig.stop_streaming()), alerts Telegram, stops telegram polling, closes researcher

## Main entry
main(): creates monitor, runs asyncio loop, handles SIGINT/SIGTERM
