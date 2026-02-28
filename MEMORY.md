# Japan 225 Bot — Project Memory
**Read this at the start of EVERY conversation before touching any code.**
Digests live in `.claude/digests/`. Read only the digest(s) relevant to your task — never scan raw files.

---

## NEXT SESSION — DO THIS FIRST
**Fix the pre-screen bug** (see Known Bug section below).
Bot is fully deployed and scanning, but AI is NEVER called because the pre-screen always returns found=False.
This is the single most important outstanding issue.

After the bug fix, also test:
- Telegram `/menu` button panel (was added but not yet real-world tested)

---

## Architecture (single VM process)
```
Oracle VM: monitor.py (24/7, systemd: japan225-bot)
  SCANNING (no position): every 5min active sessions, 30min off-hours
    → 15M candles only → detect_setup() pre-screen → if found:
    → AI cooldown check (30min) → fetch 4H+Daily → compute_confidence()
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
| `dashboard/routers/chat.py` | POST /api/chat → agentic Claude |
| `dashboard/routers/controls.py` | POST /api/controls/{force-scan,restart,stop}, POST /api/apply-fix |
| `dashboard/services/db_reader.py` | Read-only SQLite (WAL mode, uri=file:...?mode=ro) |
| `dashboard/services/config_manager.py` | dashboard_overrides.json — hot/restart key validation, atomic write |
| `dashboard/services/claude_client.py` | Agentic Claude loop. Tools: read/edit/write/run/search. Prompt caching. |
| `dashboard/services/git_ops.py` | apply_fix: patch --dry-run → stash → apply → git commit + push |
| `docs/index.html` | Single-page frontend. Dark theme. 6 tabs. localStorage settings + chat history. |

---

## Key Constants (settings.py)
```
EPIC = "IX.D.NIKKEI.IFM.IP"   CONTRACT_SIZE = 1 ($1/pt)   MARGIN_FACTOR = 0.005 (0.5%)
MIN_CONFIDENCE = 70            MIN_CONFIDENCE_SHORT = 75 (BOJ risk)
DEFAULT_SL_DISTANCE = 200      DEFAULT_TP_DISTANCE = 400      MIN_RR_RATIO = 1.5
BREAKEVEN_TRIGGER = 150        BREAKEVEN_BUFFER = 10          TRAILING_STOP_DISTANCE = 150
SCAN_INTERVAL_SECONDS = 300    MONITOR_INTERVAL_SECONDS = 60  OFFHOURS_INTERVAL_SECONDS = 1800
AI_COOLDOWN_MINUTES = 30       PRICE_DRIFT_ABORT_PTS = 20     SAFETY_CONSECUTIVE_EMPTY = 2
ADVERSE_MILD_PTS = 30          ADVERSE_MODERATE_PTS = 50      ADVERSE_SEVERE_PTS = 80
SONNET_MODEL = "claude-sonnet-4-5-20250929"   OPUS_MODEL = "claude-opus-4-6"
```
Dashboard chat uses: MODEL = "claude-sonnet-4-6" (in claude_client.py, NOT settings.py)

---

## Known Bug (critical — unresolved as of 2026-02-28)
**Pre-screen always fails → AI never called. Bot scans but never sends a trade alert.**

In `monitor.py:_scanning_cycle()` ~line 295:
```python
setup = detect_setup(tf_daily={"above_ema200_fallback": None}, tf_4h={}, tf_15m=tf_15m)
```
`detect_setup()` requires `above_ema200_fallback` to be `True` (LONG) or `False` (SHORT).
With `None`: both `if daily_bullish:` and `if not daily_bullish and daily_bullish is not None:`
evaluate to False → `setup["found"]` always False → early return → AI never escalates.

Fix options:
- (a) **Recommended**: fetch daily candles first, pass True/False based on price vs EMA200
- (b) Pass `True`/`False` from a quick separate daily fetch before pre-screen
- (c) Remove daily requirement from pre-screen entirely; let confidence.py be the gate

---

## Important Behavioral Notes (hard-won, never forget)
- **POSITIONS_API_ERROR** is a sentinel in ig_client.py. Check with `is POSITIONS_API_ERROR`, not `not positions`. Empty list `[]` = no positions. Sentinel = API call failed.
- **open_trade_atomic()** in storage.py: log_trade_open + set_position_open in one DB transaction. Always use this. Never call them separately.
- **on_trade_confirm / on_force_scan** callbacks set on self.telegram AFTER telegram.initialize(). Order matters in TradingMonitor.start().
- **Startup sync** handles 4 cases: clean start, IG-has/DB-none (recovery), DB-has/IG-none (closed offline), both agree.
- **MomentumTracker** is None when flat. Created at trade open, reset at close. Reinitiated in startup_sync if position recovered.
- **Local confidence pre-gate**: only escalates to AI if local score >= 50%. AI cooldown 30min regardless of result.
- **WebResearcher.research()** is synchronous/blocking. Called in executor: `run_in_executor(None, self.researcher.research)`.
- **detect_setup()** is bidirectional: LONG (BB mid bounce, EMA50 bounce) requires `daily_bullish=True`. SHORT (BB upper rejection, EMA50 rejection) requires `daily_bullish=False`.
- **Session logic**: session.py uses UTC. SESSIONS dict in settings.py is Kuwait Time reference only. `get_current_session()` is authoritative.
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

---

## Telegram — Commands & Buttons
`/menu` → interactive inline button panel (preferred entry point)
- Info buttons: Status · Balance · Journal · Today · Stats · API Cost
- Control buttons: Force Scan · Pause · Resume · Close Position · KILL

Text commands still work: `/status /balance /journal /today /stats /cost /force /stop /pause /resume /close /kill`
- `/kill` = emergency close, no confirmation
- `/close` = confirmation dialog (Close now / Hold)
- Trade alerts: CONFIRM / REJECT inline buttons
- Adverse move alerts: Close now / Hold inline buttons

---

## DB Location
`storage/data/trading.db` — Oracle VM only. WAL mode enabled. Never commit to git.

---

## Digests Available
`.claude/digests/`: settings · monitor · database · indicators · session · momentum · confidence · ig_client · risk_manager · exit_manager · analyzer · telegram_bot · dashboard · claude_client
