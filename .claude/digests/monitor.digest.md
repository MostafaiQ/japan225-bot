# monitor.py — DIGEST (updated 2026-03-03)
# Purpose: Main VM process. Entry point. async event loop. Handles scan + monitor + Telegram.

## class TradingMonitor
__init__(): creates Storage, IGClient, RiskManager, TelegramBot, ExitManager, AIAnalyzer,
            WebResearcher. Sets scanning_paused=False, momentum_tracker=None,
            _position_empty_count=0, _force_scan_event=asyncio.Event()
            NOTE: telegram.on_trade_confirm and on_force_scan callbacks set AFTER initialize()

start(): initializes Telegram FIRST (always available), then writes bot_state.json(phase=STARTING),
  then connects IG (3 fast retries). If IG fails → writes phase=IG_DISCONNECTED, sends Telegram alert,
  retries IG with exponential backoff (60s→120s→240s→300s max) until recovered (never exits).
  _main_cycle() also uses backoff on ensure_connected() failures: 60→120→240→300s cap, _ig_fail_count tracks attempts.
  Once IG connected → startup_sync() → main loop.

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
  Keys: outcome (no_setup|low_conf|event_block|friday_block|ai_rejected|trade_alert),
        direction, confidence, price, setup_type
self._next_scan_at: datetime | None — set before sleep, cleared after. _write_state() computes next_scan_in from this.

_scanning_cycle() writes save_scan() for all active-session outcomes.
  action_taken values always include direction suffix where applicable:
    pending_{long|short}, ai_rejected_{long|short},
    event_block_{long|short}, friday_block_{long|short}, low_conf_{long|short}, no_setup
  off_hours early return does NOT write scan records (no analysis done).
  NO COOLDOWNS — subscription is $0/call, always scan every 5 min.

_scanning_cycle() -> int (sleep seconds):
  1. get_current_session() → skip if not active
  2. is_no_trade_day() → skip
  3. check scanning_paused
  4. ig.get_market_info() → 1 API call
  5. asyncio.gather(15M, 5M, 4H) parallel → then await Daily sequential (avoids 28 req/min burst)
     Cold start: all 4 sequential (rate limit). Warm: 5M+15M+4H parallel, Daily time-gated.
     All use candle caching: full fetch on first call, delta on subsequent (see ig_client.digest.md)
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
  8d. write_context() — generates storage/context/*.md for Claude CLI to read
  8e. market_context["prescreen_setup"] + market_context["secondary_setup"] — injected before Sonnet
  9. scan_with_sonnet(failed_criteria=...) — single subprocess, Sonnet 4.6 + Opus sub-agent
      Sonnet sees SECONDARY SETUP block in prompt (other direction's type/conf/reasoning)
      Sonnet handles everything: analysis + Opus delegation for borderline 72-86% (internally)
      → logs: AI reasoning (first 200 chars)
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

_auto_execute_after_timeout(alert_data, timeout_secs=120): asyncio background task.
  Waits 120s, then checks if pending alert still exists (user didn't respond).
  If same alert still pending → auto-execute via _on_trade_confirm(). Sends Telegram "Auto-executing" notice.

_execute_scalp(scalp_result, direction, setup, session, current_price, local_conf, final_confidence):
  Auto-execute Opus-approved scalp. Uses Opus's structure-based SL (60-120pts) and TP (150-300pts).
  Validates R:R >= 1.5 after spread. Gets balance, computes lots. Calls send_scalp_executed() then _on_trade_confirm().

Near-miss flow (in _scanning_cycle, after AI rejection):
  Triggers when: local_score >= min_conf AND not QUICK REJECT
  → Builds enriched opus_reasoning: primary rejection + secondary setup context (if available)
  → evaluate_scalp(primary_direction=direction) via Opus — BIDIRECTIONAL single call
  → Opus evaluates BOTH directions using Sonnet's rejection reasoning + secondary setup as context
  → Opus picks the best direction (may differ from pre-screen direction) or rejects both
  → if scalp_viable → _execute_scalp(direction=opus_direction) (auto-execute, no user confirmation)
  Mechanical bidirectional retry REMOVED — Opus handles both directions in one call.

Force Open flow (in _scanning_cycle, after AI rejection):
  Triggers when: local_score >= 100 (12/12 criteria pass)
  → computes lots (balance + risk), builds force_alert dict
  → telegram.send_force_open_alert() → user sees Force Open / Skip buttons
  → 15min TTL, NO auto-execute. User must explicitly click Force Open.
  → on Force Open: same _on_trade_confirm() path as regular trades

_on_trade_confirm(alert_data): protected by _trade_execution_lock (asyncio.Lock).
  Re-checks position-open under lock. Validates: loss cooldown, daily loss limit, system paused.
  Re-fetches live price, checks drift. Uses stop_distance/limit_distance (not absolute levels).
  calls ig.open_position(), open_trade_atomic(), inits MomentumTracker

_on_force_scan(): sends alert + sets _force_scan_event (wakes scanning sleep immediately)

_shutdown(): alerts Telegram, stops telegram polling, closes researcher

## Main entry
main(): creates monitor, runs asyncio loop, handles SIGINT/SIGTERM
