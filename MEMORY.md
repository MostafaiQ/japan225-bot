# Japan 225 Bot — Project Memory
**Read this at the start of EVERY conversation before touching any code.**
Digests live in `.claude/digests/`. Read only the digest(s) relevant to your task — never scan raw files.

---


## Architecture (single VM process)
```
Oracle VM: monitor.py (24/7, systemd: japan225-bot)
  SCANNING (no position): every 5min active sessions, 30min off-hours
    → fetch 15M+5M parallel, then Daily sequential (cached, delta fetches after 1st)
    → BIDIRECTIONAL detect_setup(): LONG + SHORT checked independently (exclude_direction)
    → 5M fallback: per-direction (if 15M no setup → try 5M with alignment guard)
    → 5M setups tagged with _5m suffix. entry_timeframe passed to AI prompts.
    → NO cooldown ($0/call subscription) → compute_confidence() for BOTH directions
    → Primary = highest confidence. Secondary context passed to AI.
    → if score >= 60%: Sonnet 4.6 scan (with Opus sub-agent for borderline 72-86%)
    → Sequential: Sonnet runs first → if rejected, Opus scalp eval with Sonnet's full analysis
    → Single subprocess: Sonnet analyzes, delegates to Opus sub-agent internally when needed
    → if AI confirms & risk passes: auto-execute immediately + Telegram notification
  MONITORING (position open): every 60s
    → check IG position exists (2 consecutive empty = closed, SAFETY_CONSECUTIVE_EMPTY)
    → MomentumTracker.add_price() → adverse tier check → exit_manager.evaluate_position()
    → ExitPhase: INITIAL → BREAKEVEN (at +150pts) → RUNNER (75% TP in <2hrs)
  TELEGRAM: always-on polling, callbacks: on_trade_confirm, on_force_scan

Dashboard: FastAPI (systemd: japan225-dashboard, port 8080)
           Tunnel: ngrok static domain (systemd: japan225-ngrok)
           Frontend: GitHub Pages (docs/index.html)
```
GitHub Actions: CI tests ONLY (tests.yml). scan.yml is outdated/unused.

---

## File Map
### Core Bot
| File | Purpose |
|------|---------|
| `monitor.py` | Main process. TradingMonitor class. _scanning_cycle(), _monitoring_cycle(), _write_state(), _reload_overrides(), _check_force_scan_trigger() |
| `config/settings.py` | ALL constants. Never scatter config. |
| `core/ig_client.py` | IG REST API. connect/ensure_connected, get_prices, open/modify/close_position |
| `core/indicators.py` | Pure math. analyze_timeframe(), detect_setup() (LONG+SHORT bidirectional), ema/rsi/bb/vwap/heiken_ashi. Phase 1: HA, FVG, Fibonacci, PDH/PDL, liquidity sweep. Phase 2: pivot_points (daily floor pivots), detect_candlestick_patterns (12 patterns), analyze_body_trend (expansion/exhaustion). _build_confluence() uses pivots+candle+body for confluence/counter. indicators_snapshot includes all Phase 1+2 keys. |
| `core/session.py` | get_current_session() UTC, is_no_trade_day(), is_weekend(), is_friday_blackout() |
| `core/momentum.py` | MomentumTracker class. add_price(), should_alert(), is_stale(), milestone_alert() |
| `core/confidence.py` | compute_confidence(direction, tf_daily, tf_4h, tf_15m, events, web) → score dict. 12-criteria proportional scoring. C1 uses EMA50 primary (not EMA200). C2 has VWAP fallback. |
| `ai/analyzer.py` | AIAnalyzer. scan_with_sonnet() (single subprocess + Opus sub-agent). **CLI subprocess (OAuth/subscription, no API key)**. post_trade_analysis(), load_prompt_learnings(). |
| `ai/context_writer.py` | write_context() — writes storage/context/*.md before each AI call. market_snapshot, recent_activity, macro, live_edge. Called by monitor.py before Sonnet. |
| `trading/risk_manager.py` | RiskManager.validate_trade() 11 checks. get_safe_lot_size() |
| `trading/exit_manager.py` | ExitManager. evaluate_position(), execute_action(), manual_trail_update() |
| `notifications/telegram_bot.py` | TelegramBot. send_trade_alert(), /menu inline buttons, /close /kill /pause /resume |
| `storage/database.py` | Storage class. SQLite WAL mode. open_trade_atomic(), get/set position state, AI cooldown, get_ai_context_block() |
| `storage/scan_analyzer.py` | Cron-based scan analyzer. Tracks missed moves (rejections where price moved 150+pts). Writes `storage/data/scan_analysis.md` every 2hrs. |

### Dashboard
| File | Purpose |
|------|---------|
| `dashboard/main.py` | FastAPI app, CORS, Bearer auth middleware |
| `dashboard/run.py` | uvicorn entrypoint |
| `dashboard/routers/status.py` | GET /api/health (no auth), GET /api/status |
| `dashboard/routers/config.py` | GET/POST /api/config — two-tier: hot (live) vs restart |
| `dashboard/routers/history.py` | GET /api/history — closed trade journal |
| `dashboard/routers/logs.py` | GET /api/logs?type=scan|system |
| `dashboard/routers/chat.py` | POST /api/chat → Claude Code CLI · GET/POST /api/chat/history (cross-device sync) · GET /api/chat/costs (note only) |
| `dashboard/routers/controls.py` | POST /api/controls/{force-scan,restart,stop}, POST /api/apply-fix |
| `dashboard/services/db_reader.py` | Read-only SQLite (WAL mode, uri=file:...?mode=ro) |
| `dashboard/services/config_manager.py` | dashboard_overrides.json — hot/restart key validation, atomic write |
| `dashboard/services/claude_client.py` | 3-tier chat: Haiku (status/fast) → Sonnet (analysis) → Opus (code fixes). Rich context injection (services, errors, logs, trades). |
| `dashboard/services/git_ops.py` | apply_fix: patch --dry-run → stash → apply → git commit + push |
| `docs/index.html` | Single-page frontend. Dark theme. 6 tabs. localStorage settings + chat history. |

---

## Key Constants (settings.py)
```
EPIC = "IX.D.NIKKEI.IFM.IP"   CONTRACT_SIZE = 1 ($1/pt)   MARGIN_FACTOR = 0.005 (0.5%)
Lot size = margin cap only (50% of balance). AI finds the setup, you go in with conviction.
MIN_CONFIDENCE = 70            MIN_CONFIDENCE_SHORT = 75 (BOJ risk)
EXTREME_DAY_RANGE_PTS = 1000   EXTREME_DAY_MIN_CONFIDENCE = 85
OVERSOLD_SHORT_BLOCK_RSI_4H = 32  OVERBOUGHT_LONG_BLOCK_RSI_4H = 68
DEFAULT_SL_DISTANCE = 150      DEFAULT_TP_DISTANCE = 400      MIN_RR_RATIO = 1.5
BREAKEVEN_TRIGGER = 150        BREAKEVEN_BUFFER = 10          TRAILING_STOP_DISTANCE = 150
SCAN_INTERVAL_SECONDS = 300    MONITOR_INTERVAL_SECONDS = 2   OFFHOURS_INTERVAL_SECONDS = 1800
POSITION_CHECK_EVERY_N_CYCLES = 15  # 15 × 2s = 30s position existence check; position cycle REPLACES price cycle = exactly 30 calls/min
ADVERSE_LOOKBACK_READINGS = 150     # 150 × 2s = 5-minute adverse window
AI_COOLDOWN_MINUTES = 15       PRICE_DRIFT_ABORT_PTS = 20     SAFETY_CONSECUTIVE_EMPTY = 2
HAIKU_MIN_SCORE = 60  # requires 5/12 criteria (5/12=59 < 60; 6/12=65≥60)
PRE_SCREEN_CANDLES = 220 (15M fetch)   AI_ESCALATION_CANDLES = 220 (4H fetch)   DAILY_EMA200_CANDLES = 250
MINUTE_5_CANDLES = 100 (5M fallback TF fetch, ~8h of 5M data)
ADVERSE_MILD_PTS = 60          ADVERSE_MODERATE_PTS = 120     ADVERSE_SEVERE_PTS = 175
PAPER_TRADING_SESSION_GATE = REMOVED. All sessions live.
ENABLE_EMA50_BOUNCE_SETUP = False (disabled until validated)
RSI_ENTRY_HIGH_BOUNCE = 55 (backtest: RSI 55-65 LONG WR=38%, cut off dead zone)
SONNET_MODEL = "claude-sonnet-4-6"   OPUS_MODEL = "claude-opus-4-6"   (HAIKU_MODEL removed — 2-tier pipeline)
TRADING_MODE default = "live" (env var in .env also set to "live"). Paper mode code REMOVED.
```
Dashboard chat: 3-tier auto-select. Haiku (status, ≤60s) | Sonnet (analysis, ≤180s) | Opus (code fixes, ≤600s).
  Rich context injection: bot_state + services + recent errors + scan logs + recent trades.

---

## Known Bug
- monitor.py: naive vs UTC-aware datetime mismatch in duration calculation (MEDIUM)
- dashboard chat: non-atomic _write_history() race condition on concurrent writes (MEDIUM)
- monitor.py: _handle_position_closed uses last monitored price, not actual IG fill price (MEDIUM)
- exit_manager.py: Runner phase trailing stop can exceed IG rate limit (30 non-trading/min) (MEDIUM)

## AI Decision Quality Fixes (2026-03-04)
- confidence.py: **C1 daily trend: EMA50 primary** (was EMA200). EMA200 at 48,795 vs price 54,000 = always "bullish" = useless. EMA50 (55,205) is responsive to recent price action. On crash day, price below EMA50 → C1 FAILS for LONG → knife-catching LONGs blocked.
- settings.py: **Extreme day constants** — EXTREME_DAY_RANGE_PTS=1000, EXTREME_DAY_MIN_CONFIDENCE=85, OVERSOLD_SHORT_BLOCK_RSI_4H=32, OVERBOUGHT_LONG_BLOCK_RSI_4H=68.
- risk_manager.py: **Extreme day gate** — new `indicators_snapshot` param on validate_trade(). If intraday range > 1000pts AND confidence < 85%, trade is blocked. Works for both crash and rally days.
- analyzer.py: **5M data in AI prompt** — added to TF_KEYS. Was already in indicators dict, just never formatted.
- analyzer.py: **Full fibonacci grid** — all 5 levels (236/382/500/618/786) with distances from price. Was single `fib_near` only.
- analyzer.py: **BB width** — volatility proxy added to each TF line.
- analyzer.py: **MARKET REGIME block** — intraday range + crash day flag injected into user prompt.
- analyzer.py: **Extreme day rules** in system prompt — bidirectional: crash day (bearish) + bull day (bullish). Crash: prohibits shorting into oversold 4H RSI<32, prohibits LONG on single 15M candle. Bull: prohibits LONG into overbought 4H RSI>68, prohibits SHORT on single 15M candle. Both require multi-TF reversal + volume.
- analyzer.py: **Oversold shorting prohibition** — 4H RSI<32 + exhaustion signals = REJECT SHORT.
- analyzer.py: **Overbought longing prohibition** — 4H RSI>68 + exhaustion signals = REJECT LONG.
- analyzer.py: **Warning severity rule** — 4+ warnings → <70%, 6+ warnings → <60%. Prevents high confidence with many self-warnings.
- monitor.py: **Extreme day logging** — logs warning when intraday range > 1000pts.
- monitor.py: **indicators_snapshot wired** to both validate_trade() calls (Sonnet pipeline + scalp auto-execute).
- Tests: 338/338 passing. Test fixtures updated for EMA50-primary C1.

## Critical Fixes Applied (2026-03-04)
- monitor.py: **SL/TP verification after order placement** — verifies IG returned stopLevel/limitLevel in deal confirmation. If missing, immediately calls modify_position() to add SL/TP + sends CRITICAL Telegram alert. Root cause of Trade #3 losing 224pts past 102pt SL.
- monitor.py: **Sequential Opus pipeline** (was parallel). Sonnet runs first → Opus runs AFTER with Sonnet's full analysis as context. Opus gets: Sonnet reasoning, Sonnet confidence, Sonnet decision, local pre-screen, secondary setup. Clear chain of command.
- monitor.py: **Sonnet confidence gate** — Sonnet rejects with confidence < 50% skip Opus entirely. Saves API cost on clear rejects.
- analyzer.py: **Opus directional consistency** — `_last_opus_decision` tracks direction/reasoning/timestamp. Passed to evaluate_scalp() with actual elapsed time. Consistency rule: only flip direction on clear structural shift, not noise.
- monitor.py: `_last_opus_decision: dict | None` state var tracks most recent Opus scalp eval result.
- backtest.py: **Direct Anthropic API** — backtest AI evaluation now uses `anthropic` SDK instead of Claude CLI subprocess. No timeouts, ~5x faster. Sub-batching at 20 setups/call.
- ig_history.py: Reuse cached IG session (1hr TTL), removed _logout(). Threading lock prevents concurrent fetches. Cache TTL 60→300s.
- ig_client.py: _check_auth_error catches empty error strings (was causing 401 loop). get_market_info retries with fresh session on auth error.
- ig_client.py: close_position missing args fixed (epic, expiry, level, quote_id).
- monitor.py: SIGUSR1 handler for instant dashboard force scan (was polling every 2s).
- monitor.py: `_dashboard_force_scan` flag + poll task deletes file but sets flag (was spamming logs).
- monitor.py: return 0 after _execute_scalp() for immediate monitoring (was sleeping 5min).
- systemd: KillSignal=SIGTERM, KillMode=process, TimeoutStopSec=30 (was SIGKILL/1s).
- analyzer.py: AI subprocess stdout to unique temp file (uuid). Survives bot restart.
- claude_client.py: Chat subprocess timeouts increased (haiku 120s, sonnet/opus 600s).

## Backtest Results (2026-03-04, last 10 trading days Feb 16-Mar 02)
- Raw: Scalp SL=60/TP=300 is only profitable combo (PF=1.25, +$13k) vs Swing SL=150/TP=600 (PF=0.88, -$7.6k)
- AI filtered (305 setups): Sonnet approved 49 (16%). Opus found 163 scalp candidates from 211 borderlines.
- AI improves: PF 0.54 → 0.72, saves ~$1,646 in losses over 10 days.

## Execution Safety Fixes Applied (2026-03-03)
- monitor.py: `_trade_execution_lock` (asyncio.Lock) wraps `_on_trade_confirm()`. Prevents race between auto-execute timer, user click, scalp, and force-open. Position-open re-check under lock.
- monitor.py: `_on_trade_confirm_inner()` now validates: position-open check, consecutive loss cooldown, daily loss limit, system paused. All execution paths (normal, scalp, force-open) go through this.
- monitor.py: Distance-based SL/TP (`stop_distance`/`limit_distance`) passed to `ig.open_position()` instead of absolute levels. SL/TP always relative to actual fill, no drift.
- monitor.py: `_execute_scalp()` re-fetches live price before execution (was 30-120s stale). Uses live spread for R:R check.
- risk_manager.py: `get_safe_lot_size()` margin-only (50% cap). MAX_RISK_PER_TRADE removed — user wants full conviction sizing when AI finds the perfect setup.

## Critical Fixes Applied (2026-03-03)
- claude_client.py: Dashboard chat exit -15 fix. Root cause: chat subprocess would run `systemctl restart japan225-dashboard`, killing itself. Fix: (1) `start_new_session=True`, (2) safety prompt, (3) cleaned chat history.
- analyzer.py: `--tools ""` disables tool use in Sonnet subprocess → pure analysis from prompt. Cuts response from 60-180s to 10-30s. Also added `start_new_session=True`.
- indicators.py: `_strong_bearish_momentum` filter now bypassed when RSI < 35 (deeply oversold = bounce setups allowed). `extreme_oversold_reversal` widened from RSI < 22 to RSI < 28. Works without 4H at pre-screen (RSI < 25 fallback).
- indicators.py: `exclude_direction` parameter added to detect_setup() for bidirectional retry.
- monitor.py: 4H fetch moved to pre-screen (parallel with 15M+5M). detect_setup() now gets full tf_4h data. No extra API calls — just reordered.
- monitor.py: FULL bidirectional scanning pipeline. detect_setup() runs for BOTH directions (exclude_direction="SHORT" then "LONG"). compute_confidence() scored for both. Primary = higher confidence. Secondary setup context passed to Sonnet prompt (SECONDARY SETUP block) and to Opus scalp eval (enriched ai_reasoning). 5M fallback also bidirectional.
- confidence.py: C1 (daily_trend) now exempts `_breakdown_setup` (breakdown_continuation, bear_flag_breakdown, multi_tf_bearish) for SHORT direction.
- analyzer.py: Expanded BREAKDOWN/MOMENTUM SHORT RULES in Sonnet system prompt.
- Root cause: On 2026-03-03, Nikkei dropped 4,004pts (57,688→53,684, -7%). Bot ran 67 scans but AI rejected all 54 setups. 43 missed moves of 150+pts.

## Critical Fixes Applied (2026-03-02)
- ig_client.py: CRITICAL — Pandas 2.3.3 conv_resol() breaks on "MINUTE_15"/"DAY" strings. Added _PANDAS_RESOLUTIONS map to convert to "15min"/"D" etc before calling fetch_historical_prices_by_epic(). All price fetches were silently returning [] before this fix.
- monitor.py: _secs_to_next_session() helper. Off_hours sleep now exact-timed to session open (capped 30 min). Prevents missing session start when bot restarts near midnight UTC.
- ig_client.py: get_market_info() retries 3× on 503 (15s between). If all 3 fail: logout → re-auth → one final attempt before giving up. IG returns 503 for ~60s at cash CFD session open.
- ig_client.py: get_all_timeframes() had "HOUR4" (wrong) → fixed to "HOUR_4" to match _PANDAS_RESOLUTIONS map.
- settings.py + monitor.py: PRE_SCREEN_CANDLES 50→220, AI_ESCALATION_CANDLES 100→220. Both now imported/used in monitor.py (were previously dead — hardcoded values used). EMA200 now computed correctly on 15M and 4H, giving AI accurate long-term trend context.
- settings.py: AI_COOLDOWN_MINUTES 30→15. More scan opportunities (~2x), cost still ~$2/month.
- dashboard/routers/status.py: last_scan_detail was missing from /api/status response. Dashboard "Last Result" row was always showing '—'. Now passes last_scan_detail from bot_state.json.
- monitor.py: _next_scan_in → _next_scan_at (datetime). next_scan_in now computed dynamically in _write_state() as live countdown. Dashboard "Next Scan In" now shows real value.
- monitor.py: action_taken for haiku_rejected and pending now include direction suffix (_long/_short). Frontend could not derive direction without this → setup column in Recent Scans was blank.
- docs/index.html: Added haiku_rejected_long, haiku_rejected_short, pending_long, pending_short to _actionLabels.
- dashboard/services/db_reader.py: get_recent_scans() now filters out no_setup rows. Overview Recent Scans table only shows meaningful events (AI involved, blocked, cooldown, etc.).
- dashboard/services/config_manager.py: DEFAULTS dict replaced with _defaults() function that imports live from settings.py. Config page now always reflects actual settings values. Dashboard overrides still take precedence. Also fixed DEFAULT_SL_DISTANCE was hardcoded 200 (wrong) — now reads 150 from settings.py.
- dashboard + telegram: COOLDOWN phase badge, sortable Recent Scans (click headers), "↑ Escalate" button on cooldown rows (clears cooldown + triggers scan). Telegram /status shows cooldown countdown + "Escalate to Haiku" inline button. _today_text() now shows time/dir/conf/emoji per scan. "No active position" centered.
- monitor.py: Cooldown scans now compute approx confidence (tf_4h={}) so dashboard shows score instead of "—". clear_cooldown.trigger file added (dashboard writes it, monitor clears cooldown at next cycle).
- core/confidence.py: 12-criteria system. C12 entry_quality (pullback+vol). C1 oversold-exempt. RSI gate 65→55. Formula: 30+int(n*70/12). LONG 7/12=70%, SHORT 8/12=76%. 338/338 tests pass.
- monitor.py: No cooldown on AI reject — $0/call subscription, scan again in 5 min. Haiku pre-gate REMOVED (2026-03-02).
- monitor.py + status.py: Session "—" bug fixed — _current_session persists across write_state() calls. Next Scan In frozen bug fixed — bot stores next_scan_at (ISO datetime), status.py computes countdown dynamically on every API poll.
- indicators.py: bb_mid_bounce RSI range 35→30 (captures 30-35 zone). BB_LOWER_THRESHOLD 80→150. Relaxed bounce_starting gate for oversold RSI<40 (accepts wick/HA/candle pattern). New `oversold_reversal` setup type (RSI<30 + daily bullish + any reversal confirm).
- confidence.py: C5/C10/C11 now setup-type-aware. bb_lower_bounce + oversold_reversal: below-EMA50=expected, bearish HA=expected, 4H bearish passes if multi-TF oversold or daily bullish. LONG_RSI_LOW 35→30. C2 near_bb_lower 80→150.
- analyzer.py: Conditional Opus (--agents only when local conf 60-86%). Mean-reversion bounce rules in system prompt. Parse error auto-retry. WebResearcher: real news (Google News RSS), JP holidays (nager.date), CNN Fear & Greed.
- Scan analyzer data (2026-03-02): 29 missed moves of 150+pts, 48% AI LONG miss rate, RSI 20-35 zone averaged +327pts. These changes target 65-72% reduction in missed moves.
- analyzer.py: `--fast` flag added to ALL Claude CLI calls (Sonnet + Opus). Same model, faster output, $0 cost.
- analyzer.py: `evaluate_scalp()` — BIDIRECTIONAL single Opus call. Receives Sonnet's rejection reasoning + secondary setup context + all indicators. Evaluates BOTH directions. Opus picks the best play (direction may differ from pre-screen). SL 60-120pts (structure-based), TP 150-300pts. Enforces R:R >= 1.5 after spread.
- analyzer.py: `build_scan_prompt()` now includes SECONDARY SETUP block when bidirectional scan finds both directions.
- monitor.py: Near-miss → Opus bidirectional scalp auto-execute. Opus picks direction. Mechanical bidirectional retry REMOVED (Opus handles both in single call). No user confirmation for scalps. `_execute_scalp()` uses Opus-picked direction. AI confidence gate REMOVED (was >= 40%, now any non-quick-reject goes to Opus).
- monitor.py: **Sequential Sonnet → Opus** — Sonnet runs first. If Sonnet rejects, Opus scalp eval runs with Sonnet's full analysis (reasoning, confidence, decision) as context. Opus evaluates both directions. ~20s total (Sonnet ~10s + Opus ~10s). Opus gets directional consistency context from previous calls.
- analyzer.py: Parse error retry uses `effort="normal"` (not low) — `--effort low` can produce incomplete JSON. First attempt still uses low for speed.
- ig_client.py: **Deal confirmation fix** — `trading_ig` library returns full confirmation dict (not string) from `create_open_position()`/`close_open_position()`. Code now detects dict with `dealId` and uses directly instead of re-confirming (was causing 400 errors on `/confirms/{...dict...}`).
- ig_client.py: **Disk-backed candle cache** (`storage/data/candle_cache.json`). Survives restarts. 4hr max age. Delta fetches instead of full fetches after restart.
- systemd: KillSignal=SIGTERM, KillMode=process, TimeoutStopSec=30. AI subprocesses survive restart.
- settings.py: DAILY_LOSS_LIMIT_PERCENT = 1.0 (effectively disabled — user manages risk).
- ig_client.py: CRITICAL — `trailing_stop_distance` kwarg removed from `create_open_position()`. Not supported by trading_ig library. This caused ALL trade executions to fail silently. Root cause of 0 trades.
- telegram_bot.py: HTML parse fallback — if HTML alert fails, retry as plain text. Prevents silent alert failures.
- monitor.py: **Sonnet auto-execute** — trades auto-execute immediately when confidence passes thresholds (70% LONG, 75% SHORT). No 2-min timeout. Same flow as Opus scalps: send Telegram notification + execute.
- monitor.py: **Opus guardrails** — minimum 60% confidence required. Direction-flip block: bounce setups (bb_lower/mid_bounce, oversold_reversal, etc.) cannot go SHORT; breakdown/rejection setups cannot go LONG. Opus now goes through full risk validation pipeline + saves ai_analysis to DB.
- monitor.py: **High-confidence cooldown bypass** — local 100% → skip + reset consecutive losses; Sonnet >= 85% → skip; Opus >= 80% → skip.
- storage/database.py: `reset_consecutive_losses()` added for cooldown bypass.
- ai/analyzer.py: **R:R enforcement in Sonnet prompt** — mandatory `(TP_dist - 7) / (SL_dist + 7) >= 1.5` computation. `effective_rr` added to JSON schema.
- LICENSE: **Proprietary** — all rights reserved, unauthorized use prohibited, legal action provisions.
- analyzer.py: **BB values bug fixed** — `_fmt_indicators()` used `bb_upper/bb_mid/bb_lower` but indicators.py returns `bollinger_upper/bollinger_mid/bollinger_lower`. AI never saw actual BB levels (always `?/?/?`). Now uses correct keys.
- risk_manager.py: **Max positions check fixed** — `has_open_position` → `has_open` (matching DB schema). Check 4 was always passing.
- monitor.py: **Force-open NameError fixed** — `ai_reasoning` undefined → `final_result.get("reasoning", "")`.
- monitor.py: **time.time() fix** — `time` module not imported → use `datetime.now().timestamp()`.
- monitor.py: **Position closed TypeError fix** — `abs(last_price - sl)` when sl=None → added None guard.
- monitor.py: Dead `_auto_execute_after_timeout` method removed (replaced by immediate auto-execute).
- monitor.py: **Instant shutdown** — `os._exit(0)` in SIGTERM handler. Restart: 30s → 2s. TimeoutStopSec=5.
- monitor.py: **Learning loop wired up** — `post_trade_analysis()` called after every trade close. Updates `prompt_learnings.json` + `brier_scores.json`.
- analyzer.py: **Prompt learnings injected** — `load_prompt_learnings()` output included in Sonnet scan prompt. AI sees last 5 insights from closed trades.
- analyzer.py: System prompt fixed "11 criteria" → "12 criteria" (C12 = entry_quality).
- settings.py: **DISPLAY_TZ = UTC+3 (Kuwait)**. `display_now()` helper. All user-facing timestamps (Telegram, logs, reports, AI prompts) show Kuwait time. DB/API storage stays UTC.
- Dead code removed: _ai_reject_until, PRICING, CRITERIA_WEIGHT, _multi_tf_short, parallel_mode branch, unused imports (get_scan_interval, TIER_NONE, format_confidence_breakdown, ADVERSE_SEVERE_PTS, Optional in confidence/risk_manager).
- Deleted orphan `opus_position_eval.py` (hardcoded standalone script, not used by bot).
- telegram_bot.py: `send_scalp_executed()` replaces old 3-button `send_near_miss_alert()`. Notification-only (no buttons). Near-miss callback handlers removed.
- monitor.py + telegram_bot.py: Force Open feature. When local confidence == 100% (12/12) but AI rejects, Telegram alert with Force Open / Skip buttons. 15min TTL. No auto-execute — requires explicit user confirmation. Uses same `pending_alert` + `on_trade_confirm` flow as regular trades. Callback data: `force_open` / `reject_force`.

## Dashboard Fixes Applied (2026-03-01)
- monitor.py: _last_scan_detail added to bot_state.json. Scan records written for ALL active-session outcomes (no_setup, cooldown, low_conf, event_block, friday_block). Previously only Haiku-rejected and Sonnet/Opus scans wrote records.
- logs.py: grep pattern expanded with CONFIDENCE|HAIKU|SONNET|OPUS|REJECTED|APPROVED|COOLDOWN|ESCALAT|PRE-SCREEN|SCREEN:|BLOCK so all scan messages appear in Logs tab.
- claude_client.py: _log_chat_cost() re-added — estimates cost from char count (Sonnet pricing), writes to chat_costs.json. Better empty-response error messages.
- chat.py: /api/chat/costs now returns real today/total estimates from chat_costs.json.
- docs/index.html: Overview "Last Result" row shows last scan outcome badge+direction+confidence. Recent Scans table has readable action labels. Chat cost badge shows both chat (est) and bot scan costs.

## Strategy History (archived — see high-chancellor-archive.md for full details)
HC 6-fix redesign 2026-02-28: ADVERSE tiers widened, bounce confirmation added, RSI tuned,
C4 redesigned, EMA50 bounce disabled, session gate removed. Tests: 328/328 passing (C9/C10/C11, new indicators, Phase 1 confluence wiring, Phase 2 pivots/candle/body, oversold setup-type-aware scoring).
Live trading active 2026-03-01. Historical backtest (bad): 613 trades, 0.8% WR → fixed.

---

## Setup Types (detect_setup — updated 2026-03-03)
| Type | Direction | Trigger | RSI | Gate |
|------|-----------|---------|-----|------|
| bollinger_mid_bounce | LONG | price ±150pts from BB mid | 30-65 | bounce_starting OR (RSI<40 + wick/HA/candle pattern) |
| bollinger_lower_bounce | LONG | price ±150pts from BB lower | 20-40 | lower_wick ≥15pts (no EMA50 gate) |
| extreme_oversold_reversal | LONG | RSI <22 + 4H near BB lower(300pts) or 4H RSI<35 | <22 | wick≥10 OR HA bullish OR candle pattern OR sweep. No daily req. |
| oversold_reversal | LONG | RSI <30 + daily bullish | <30 | wick≥10 OR HA bullish OR candle pattern OR sweep |
| bollinger_upper_rejection | SHORT | price ±150pts from BB upper | 55-75 | below_ema50 |
| ema50_rejection | SHORT | price ≤ema50+2, dist ≤150 | 50-70 | daily bearish |
SL=150 (WFO-validated), TP=400 for all types.
Note: above_ema50 gate REMOVED from bollinger_mid_bounce. EMA50 status shown in reasoning string for Sonnet/Opus to evaluate.
5M fallback: all types can fire on 5M candles with `_5m` suffix (e.g. `bollinger_mid_bounce_5m`).
  5M alignment guard: LONG needs 15M RSI<65 + price within 300pts of 15M BB mid/lower. SHORT needs 15M RSI>35 + price within 300pts of 15M BB upper.
  No-setup reasoning: diagnostic string with BB_mid dist, RSI status, bounce status, daily trend.

## Backtest Status (2026-03-02, NKD=F, ~875 days combined, all sessions)
Data: NKD=F 1H (730d, 2023-10 to 2025-12) + 15M (60d, 2025-12 to 2026-03) + ^N225 daily.
Sessions: Tokyo(00-06) + London(08-16) + NY(16-21 UTC). backtest.py v2.
Combined 1H+15M: 2222 qualifying setups (1080 from 1H, 1142 from 15M). 807 trades after dedup.
Results WITHOUT AI filter (worst case):
  807 trades, 44% WR, PF=0.70. 1H: 486 trades 43% WR | 15M: 321 trades 46% WR
  Tokyo: 43% WR | London: 43% WR | NY: 50% WR
  bollinger_mid_bounce: 508 trades 46% WR | bollinger_lower_bounce: 231 trades 42% WR
WFO best swing: SL=150 TP=600 PF=0.74 | best scalp: SL=60 TP=300 PF=0.91 (scalp outperforms)
`--ai` flag: AI eval on last 10 trading days (Sonnet+Opus, cached). Runtime: 110s local, ~45min with AI.
`--sim20` flag: $20 account sim, last 10 days, dynamic lots, swing+scalp side-by-side, AI proxy (conf>=88%).
  Swing: blown by day 4 (SL=150 too wide for $20). Scalp: survives 10d but -32% ($13.60 final).
PF<1 is expected without AI — Sonnet/Opus are the quality gate.

## AI Pipeline (updated 2026-03-03) — Single subprocess: Sonnet 4.6 + Opus sub-agent
- **Auth: Claude Code CLI (OAuth/subscription) — no ANTHROPIC_API_KEY used in analyzer.**
  Single `claude --model sonnet-4-6 --print --effort low --tools "" --agents {...}` subprocess per scan.
  `--effort low` on ALL CLI calls — disables adaptive thinking (105s → 9s), quality unchanged for structured JSON.
  `--tools ""` disables file access — pure analysis from prompt data. Dead context_note removed.
  ANTHROPIC_API_KEY is stripped from env before each call to force OAuth.
  NOTE: `--fast` and `--max-tokens` DO NOT EXIST as CLI flags (tested v2.1.63). Only `/fast` in interactive mode.
- Opus sub-agent: defined via `--agents` flag. Sonnet delegates to it for borderline 72-86% calls.
  Both models run within the same subprocess — no extra Node.js startup overhead.
- Context data inlined directly into prompt (recent trades, Fear & Greed, scans).
  context_writer.py no longer called — files can't be read with --tools "".
- Haiku pre-gate: **REMOVED** (2026-03-02). Quick-reject logic in Sonnet prompt.
  C7/C8 (event/blackout) hard-blocked BEFORE Sonnet. No cooldown on AI reject.
  Proportional formula: score=30+int(passed*70/12). 12/12=100%, 7/12=70%, 8/12=76%.
  LONG needs 7/12 (70≥70), SHORT needs 8/12 (76≥75).
- C5/C10/C11 now setup-type-aware: bb_lower_bounce + oversold_reversal + extreme_oversold_reversal
  get lenient treatment (below-EMA50 expected, bearish HA expected, 4H bearish expected for mean-reversion).
- Sonnet 4.6: adaptive thinking. Mean-reversion bounce rules in system prompt.
  **Conditional Opus**: --agents only loaded when local conf 60-86%. Clear approve (≥87%) or
  reject (≤59%) → no Opus overhead. Saves ~25-30s on non-borderline calls.
  Parse error → automatic retry once (without Opus).
- WebResearcher: _get_nikkei_news (Google News RSS), _get_calendar (nager.date JP holidays),
  _get_fear_greed (CNN Fear & Greed). VIX + USD/JPY unchanged.
- U2: Sonnet reasoning logged (first 200 chars) after every call.
- U4: 5-min short cooldown after Sonnet/Opus rejection (prevents same-setup re-scan).
- U5: Warning logged when 4H fetch fails but bot still escalates.
- _cost field always 0.0 (subscription). _tokens always zeros. Kept for interface compat.
- prompt_learnings.json: auto-updated after each trade close, injected into future prompts
- brier_scores.json: Brier score calibration tracking (updated by post_trade_analysis, read by /brier-check skill)
- CLAUDE.md in project root: auto-loaded by all Claude Code sessions + dashboard subprocess
- Dashboard chat: 3-tier model (Haiku/Sonnet/Opus), rolling history summary (capped ~650 tokens),
  rich context injection (bot_state + service status + recent errors + scan logs + recent trades).
  Tier badge shown in thinking bubble. Draft text persists on refresh. Responses survive refresh.
- Skills: ~/.claude/skills/ — 9 skills (session-brief, brier-check, cost-report, deploy-check, prompt-audit, strategy-health, trade-review, backtest-import, recall)
- Agents: ~/.claude/agents/ — market-analyst.md (read-only market analysis), trade-debugger.md (trade postmortem)
- Hooks: PostToolUse on Edit|Write of .py → auto-runs pytest (catches regressions during dev)
- HC retired for routine use. Use skills instead. HC = break-glass only.

## Important Behavioral Notes (hard-won, never forget)
- **POSITIONS_API_ERROR** is a sentinel in ig_client.py. Check with `is POSITIONS_API_ERROR`, not `not positions`. Empty list `[]` = no positions. Sentinel = API call failed.
- **open_trade_atomic()** in storage.py: log_trade_open + set_position_open in one DB transaction. Always use this. Never call them separately.
- **Telegram starts FIRST** in TradingMonitor.start() — before IG connection. If IG is down, bot stays alive and retries IG every 5 min. on_trade_confirm / on_force_scan callbacks set immediately after initialize(), before start_polling().
- **Startup sync** handles 4 cases: clean start, IG-has/DB-none (recovery), DB-has/IG-none (closed offline), both agree.
- **MomentumTracker** is None when flat. Created at trade open, reset at close. Reinitiated in startup_sync if position recovered.
- **Local confidence pre-gate**: only escalates to AI if local score >= 60%. Sonnet rejects with conf < 50% skip Opus entirely. Opus minimum 60% confidence to execute.
- **WebResearcher.research()** is synchronous/blocking. Called in executor: `run_in_executor(None, self.researcher.research)`.
- **detect_setup()** bidirectional. No daily hard gate — C1 penalizes counter-trend in confidence.py.
  RSI: bb_mid 35-65 | bb_lower 20-40 | bb_upper 55-75 | ema50_rej 50-70.
  BB_MID_THRESHOLD=150pts, BB_LOWER_THRESHOLD=80pts. bounce_starting=price>prev_close (mid bounce).
  lower_wick>=15pts (lower bounce). NO above_ema50 gate on mid bounce (EMA50 in reasoning for AI).
  ig.close_position() calls use run_in_executor in Telegram handlers.
- **Session logic**: session.py uses UTC. SESSION_HOURS_UTC in settings.py is the UTC reference for backtest and monitor. `get_current_session()` is authoritative for live bot.
- **SEVERE adverse move** at Phase.INITIAL → auto-moves SL to breakeven (entry + BREAKEVEN_BUFFER=10). Does NOT close.
- **Dashboard ngrok header**: all fetch() calls must include `ngrok-skip-browser-warning: true` or the ngrok interstitial blocks the request.

---

## Dashboard — Running State
| systemd unit           | what it runs                                                        | status  |
|------------------------|---------------------------------------------------------------------|---------|
| `japan225-bot`         | python monitor.py                                                   | running |
| `japan225-dashboard`   | uvicorn dashboard.main:app --host 127.0.0.1 --port 8080             | running |
| `japan225-ngrok`       | ngrok http --domain=unmopped-shrimplike-sook.ngrok-free.app 8080    | running |

- Frontend : https://mostafaiq.github.io/japan225-bot/
- Backend  : https://unmopped-shrimplike-sook.ngrok-free.app
- Auth     : Bearer DASHBOARD_TOKEN (in .env). All routes except GET /api/health.
- CORS     : https://mostafaiq.github.io only (+ localhost:3000 for dev)

### Inter-process communication
| File | Writer | Reader |
|------|--------|--------|
| `storage/data/bot_state.json` | monitor._write_state() each cycle | /api/status router |
| `storage/data/dashboard_overrides.json` | config_manager.write_overrides() | monitor._reload_overrides() |
| `storage/data/force_scan.trigger` | /api/controls/force-scan | monitor._check_force_scan_trigger() |
| `storage/data/chat_history.json` | /api/chat/history POST | /api/chat/history GET · frontend poll |
| `storage/data/chat_costs.json` | N/A (Claude Code CLI, no cost tracking) | /api/chat/costs GET |
| `storage/data/prompt_learnings.json` | post_trade_analysis() in analyzer.py | injected into AI prompts |
| `storage/data/chat_usage.json` | claude_client._track_usage() | auto-skill drafting trigger |
| `storage/data/brier_scores.json` | post_trade_analysis() in analyzer.py | /brier-check skill |
| `storage/data/scan_analysis.md` | scan_analyzer.py (cron, every 2hr) | user inspection, future AI context |
| `storage/data/scan_analysis.log` | scan_analyzer.py (append per run) | historical tracking |

---

## Telegram
HTML parse_mode. REPLY_KB (4×2) always visible. `/menu` → inline panel.
Commands: `/status /balance /journal /today /stats /cost /force /stop /pause /resume /close /kill`
`/kill`=emergency close (no confirm). `/close`=confirm dialog. Trade alerts: CONFIRM/REJECT (15min TTL).
Force Open alert: when local confidence 100% (12/12) but AI rejected. Two buttons: Force Open / Skip. 15min TTL. No auto-execute.
_nav_kb(ctx)=contextual inline row after every response. _dispatch_menu()=shared handler.

## DB + Digests
DB: `storage/data/trading.db` — Oracle VM only. WAL mode. Never commit.
Digests: `.claude/digests/` — settings · monitor · database · indicators · session · momentum ·
confidence · ig_client · risk_manager · exit_manager · analyzer · telegram_bot · dashboard · claude_client · scan_analyzer
