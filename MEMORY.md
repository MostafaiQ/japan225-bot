# Japan 225 Bot — Project Memory
**Read this at the start of EVERY conversation before touching any code.**
Digests live in `.claude/digests/`. Read only the digest(s) relevant to your task — never scan raw files.

---


## Architecture (single VM process)
```
Oracle VM: monitor.py (24/7, systemd: japan225-bot)
  SCANNING (no position): every 5min active sessions, 30min off-hours
    → fetch 15M+Daily in parallel → detect_setup() pre-screen → if found:
    → AI cooldown check (30min) → fetch 4H → compute_confidence() (Daily reused from pre-screen)
    → if score >= 50%: escalate to Sonnet → if Sonnet >=70%: Opus confirm
    → if AI confirms & risk passes: Telegram CONFIRM/REJECT alert (15min TTL)
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
| `core/indicators.py` | Pure math. analyze_timeframe(), detect_setup() (LONG+SHORT bidirectional), ema/rsi/bb/vwap |
| `core/session.py` | get_current_session() UTC, is_no_trade_day(), is_weekend(), is_friday_blackout() |
| `core/momentum.py` | MomentumTracker class. add_price(), should_alert(), is_stale(), milestone_alert() |
| `core/confidence.py` | compute_confidence(direction, tf_daily, tf_4h, tf_15m, events, web) → score dict |
| `ai/analyzer.py` | AIAnalyzer.scan_with_sonnet(), confirm_with_opus(). WebResearcher.research() |
| `trading/risk_manager.py` | RiskManager.validate_trade() 11 checks. get_safe_lot_size() |
| `trading/exit_manager.py` | ExitManager. evaluate_position(), execute_action(), manual_trail_update() |
| `notifications/telegram_bot.py` | TelegramBot. send_trade_alert(), /menu inline buttons, /close /kill /pause /resume |
| `storage/database.py` | Storage class. SQLite WAL mode. open_trade_atomic(), get/set position state, AI cooldown |

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
| `dashboard/services/claude_client.py` | Spawns `claude --print --dangerously-skip-permissions`. Full Claude Code toolset. History passed via stdin. Cost tracking removed. |
| `dashboard/services/git_ops.py` | apply_fix: patch --dry-run → stash → apply → git commit + push |
| `docs/index.html` | Single-page frontend. Dark theme. 6 tabs. localStorage settings + chat history. |

---

## Key Constants (settings.py)
```
EPIC = "IX.D.NIKKEI.IFM.IP"   CONTRACT_SIZE = 1 ($1/pt)   MARGIN_FACTOR = 0.005 (0.5%)
MIN_CONFIDENCE = 70            MIN_CONFIDENCE_SHORT = 75 (BOJ risk)
DEFAULT_SL_DISTANCE = 150      DEFAULT_TP_DISTANCE = 400      MIN_RR_RATIO = 1.5
BREAKEVEN_TRIGGER = 150        BREAKEVEN_BUFFER = 10          TRAILING_STOP_DISTANCE = 150
SCAN_INTERVAL_SECONDS = 300    MONITOR_INTERVAL_SECONDS = 2   OFFHOURS_INTERVAL_SECONDS = 1800
POSITION_CHECK_EVERY_N_CYCLES = 15  # 15 × 2s = 30s position existence check; position cycle REPLACES price cycle = exactly 30 calls/min
ADVERSE_LOOKBACK_READINGS = 150     # 150 × 2s = 5-minute adverse window
AI_COOLDOWN_MINUTES = 30       PRICE_DRIFT_ABORT_PTS = 20     SAFETY_CONSECUTIVE_EMPTY = 2
ADVERSE_MILD_PTS = 60          ADVERSE_MODERATE_PTS = 120     ADVERSE_SEVERE_PTS = 175
PAPER_TRADING_SESSION_GATE = REMOVED. All sessions live.
ENABLE_EMA50_BOUNCE_SETUP = False (disabled until validated)
RSI_ENTRY_HIGH_BOUNCE = 55 (relaxed from 48 for frequency; AI gates RSI 48-55 range)
SONNET_MODEL = "claude-sonnet-4-5-20250929"   OPUS_MODEL = "claude-opus-4-6"
TRADING_MODE default = "live" (env var in .env also set to "live"). Paper mode code REMOVED.
```
Dashboard chat: Claude Code CLI (claude --print). No model constant needed.

---

## Known Bug
*(none — pre-screen bug fixed 2026-02-28)*

## Critical Strategic Issue (found + partially fixed 2026-02-28)
Expert agent analysis + WFO backtest confirmed ZERO edge in original strategy.
HC-prescribed 6-fix redesign applied 2026-02-28. All 233 tests pass.

### Fixes applied (HC redesign):
1. ADVERSE tiers: 30/50/80 → 60/120/175 (80pts was inside 1-candle ATR noise)
2. Bounce confirmation: bounce_starting = price > prev_close (no entry mid-fall)
3. RSI tightened: LONG BB mid zone 35-60 → 35-48 then relaxed to 35-55 (2026-03-01 for frequency; AI gates 48-55 range)
4. C4 redesigned: pts_to_upper>=350 (trivially true 78%) → price<=bb_mid (confirms actual pullback)
5. EMA50 bounce disabled: ENABLE_EMA50_BOUNCE_SETUP=False (median dist=325pts, entries unvalidated)
6. Session gate: PAPER_TRADING_SESSION_GATE was True (Tokyo only), now False (all sessions — backtest validated)

### Original diagnosis (for reference):
- Backtest: 613 trades, 0.8% win rate, -$126,942 P&L (60 days, before fixes)
- Root cause: BB mid bounce = mean-reversion logic in +21% trending bull market
- Backtest data: Tokyo-session only (^N225). IG CFD London/NY sessions unvalidated.

### Live trading active from Sunday night (2026-03-01).
HC NO-GO conditions superseded — user approved going live.

---

## Setup Types (detect_setup — updated 2026-03-01)
| Type | Direction | Trigger | RSI | Gate |
|------|-----------|---------|-----|------|
| bollinger_mid_bounce | LONG | price ±150pts from BB mid | 35-55 | bounce_starting (EMA50 status in reasoning for AI) |
| bollinger_lower_bounce | LONG | price ±80pts from BB lower | 20-40 | lower_wick ≥15pts (no EMA50 gate) |
| bollinger_upper_rejection | SHORT | price ±150pts from BB upper | 55-75 | below_ema50 |
| ema50_rejection | SHORT | price ≤ema50+2, dist ≤150 | 50-70 | daily bearish |
SL=150 (WFO-validated), TP=400 for all types.
Note: above_ema50 gate REMOVED from bollinger_mid_bounce. EMA50 status shown in reasoning string for Sonnet/Opus to evaluate.

## Backtest Status (2026-03-01, NKD=F, 42 days, all sessions)
Data: NKD=F 15M + 1H, ^N225 daily. Sessions: Tokyo(00-06) + London(08-16) + NY(16-21 UTC).
SESSION_HOURS_UTC added to settings.py — single source of truth for backtest + monitor.
Results WITHOUT AI filter (worst case):
  731 raw setups, 208 trades after dedup, 47% WR, PF=0.72
  Setup frequency: 17.4/day raw → ~8-12 AI evaluations/day (30-min cooldown)
  Tokyo: 49% WR | London: 44% WR | NY: 48% WR
  bollinger_mid_bounce: 148 trades, 47% WR | bollinger_lower_bounce: 60 trades, 45% WR
PF<1 is expected without AI — Sonnet/Opus are the quality gate.

## AI Pipeline Fixes (2026-03-01)
1. analyzer.py build_scan_prompt(): "m15" key added to lookup → MARKET STRUCTURE block now renders in live bot.
2. monitor.py: prescreen_setup (type + reasoning + session name) injected into market_context before Sonnet call.
   Sonnet now sees: specific setup type, full reasoning string, and current session name.
3. detect_setup() BB mid bounce: above_ema50 gate removed; EMA50 status in reasoning string for AI to evaluate.
4. RSI_ENTRY_HIGH_BOUNCE: 48 → 55 (AI evaluates RSI 48-55 range; code no longer hard-blocks it).

## Important Behavioral Notes (hard-won, never forget)
- **POSITIONS_API_ERROR** is a sentinel in ig_client.py. Check with `is POSITIONS_API_ERROR`, not `not positions`. Empty list `[]` = no positions. Sentinel = API call failed.
- **open_trade_atomic()** in storage.py: log_trade_open + set_position_open in one DB transaction. Always use this. Never call them separately.
- **Telegram starts FIRST** in TradingMonitor.start() — before IG connection. If IG is down, bot stays alive and retries IG every 5 min. on_trade_confirm / on_force_scan callbacks set immediately after initialize(), before start_polling().
- **Startup sync** handles 4 cases: clean start, IG-has/DB-none (recovery), DB-has/IG-none (closed offline), both agree.
- **MomentumTracker** is None when flat. Created at trade open, reset at close. Reinitiated in startup_sync if position recovered.
- **Local confidence pre-gate**: only escalates to AI if local score >= 50%. AI cooldown 30min regardless of result.
- **WebResearcher.research()** is synchronous/blocking. Called in executor: `run_in_executor(None, self.researcher.research)`.
- **detect_setup()** is bidirectional: LONG (BB mid bounce, BB lower bounce) requires `daily_bullish=True`. SHORT (BB upper rejection, EMA50 rejection) requires `daily_bullish=False`.
  RSI windows: LONG BB mid: 35-55 (RSI_ENTRY_HIGH_BOUNCE=55). LONG BB lower: 20-40. SHORT BB upper: 55-75. SHORT EMA50: rsi 50-70 + price<=ema50+2.
  BB_MID_THRESHOLD=150pts, BB_LOWER_THRESHOLD=80pts (tighter — lower band is harder to reach).
  Bounce confirmation (mid bounce): bounce_starting = price > prev_close. NO above_ema50 gate (EMA50 status in reasoning string for AI).
  Lower band bounce: lower_wick >= 15pts (any rejection at extreme oversold counts).
  4H macro: LONG 35-75 (confidence.py), SHORT 30-60. ig.close_position() calls use run_in_executor in Telegram handlers.
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

---

## Telegram — Commands & Buttons
Bot uses HTML parse_mode throughout. REPLY_KB (ReplyKeyboardMarkup) always-visible at bottom.
`/menu` → full inline button panel (preferred on mobile)
- Info: Status · Balance · Journal · Today · Stats · API Cost
- Control: Force Scan · Pause · Resume · Close Pos · KILL

Reply-keyboard (4×2 bottom nav): same actions as /menu, always visible.
_nav_kb(ctx) → contextual 1-row inline buttons appended after every command response.
Text commands: `/status /balance /journal /today /stats /cost /force /stop /pause /resume /close /kill`
- `/kill` = emergency close, no confirmation
- `/close` = confirmation dialog (Close now / Hold)
- Trade alerts: CONFIRM / REJECT inline buttons (15min TTL)
- Adverse move alerts: Close now / Hold inline buttons

---

## DB Location
`storage/data/trading.db` — Oracle VM only. WAL mode enabled. Never commit to git.

---

## Digests Available
`.claude/digests/`: settings · monitor · database · indicators · session · momentum · confidence · ig_client · risk_manager · exit_manager · analyzer · telegram_bot · dashboard · claude_client
