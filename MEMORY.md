# Japan 225 Bot — Project Memory
**Read this at the start of every conversation. Then read only the digest(s) relevant to your task.**
Digests live in `.claude/digests/`. Each digest is a compact skeleton of one module (~30 lines vs 300+).

---

## Architecture (single VM process)
```
Oracle VM: monitor.py (24/7)
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
```
GitHub Actions: CI tests ONLY (tests.yml). No scanning runs there anymore.

---

## File Map
| File | Purpose |
|------|---------|
| `monitor.py` | Main process. TradingMonitor class. Entry point. |
| `config/settings.py` | ALL constants. See digest. Never scatter config. |
| `core/ig_client.py` | IG REST API. connect/ensure_connected, get_prices, open/modify/close_position |
| `core/indicators.py` | Pure math. analyze_timeframe(), detect_setup() (LONG+SHORT), ema/rsi/bb/vwap |
| `core/session.py` | get_current_session() UTC, is_no_trade_day(), is_weekend(), is_friday_blackout() |
| `core/momentum.py` | MomentumTracker class. add_price(), should_alert(), is_stale(), milestone_alert() |
| `core/confidence.py` | compute_confidence(direction, tf_daily, tf_4h, tf_15m, events, web) → score dict |
| `ai/analyzer.py` | AIAnalyzer.scan_with_sonnet(), confirm_with_opus(). WebResearcher.research() |
| `trading/risk_manager.py` | RiskManager.validate_trade() 11 checks. get_safe_lot_size() |
| `trading/exit_manager.py` | ExitManager. evaluate_position(), execute_action(), manual_trail_update() |
| `notifications/telegram_bot.py` | TelegramBot. send_trade_alert(), inline buttons, /stop /resume /close /kill /pause |
| `storage/database.py` | Storage class. SQLite. open_trade_atomic(), get/set position state, AI cooldown |

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

---

## Known Bug (critical, unresolved as of 2026-02-28)
**Pre-screen always fails → AI never called**

In `monitor.py:_scanning_cycle()` line ~295, the pre-screen calls:
```python
setup = detect_setup(tf_daily={"above_ema200_fallback": None}, tf_4h={}, tf_15m=tf_15m)
```
`detect_setup()` requires `above_ema200_fallback` to be `True` (LONG path) or `False` (SHORT path).
With `None`, both `if daily_bullish:` and `if not daily_bullish and daily_bullish is not None:`
evaluate to False → `setup["found"]` is always False → early return → AI never escalates.
**Effect: Bot scans but never sends a trade alert.**
Fix options: (a) fetch daily candles first, (b) pass `True`/`False` based on a quick daily fetch,
or (c) remove pre-screen daily requirement and let confidence.py be the gate.

---

## Important Behavioral Notes (hard-won)
- **POSITIONS_API_ERROR** is a sentinel object in ig_client.py. Check with `is POSITIONS_API_ERROR`, not `not positions`. Empty list `[]` means "no positions". The sentinel means "API call failed".
- **open_trade_atomic()** in storage.py does log_trade_open + set_position_open in one DB transaction. Always use this when opening a trade, never call them separately.
- **on_trade_confirm / on_force_scan** are callbacks set on self.telegram AFTER telegram.initialize(). Order matters in TradingMonitor.start().
- **Startup sync** runs on every restart and handles 4 cases: clean start, IG-has/DB-none (recovery), DB-has/IG-none (closed offline), both agree.
- **MomentumTracker** is None when flat. Created at trade open, reset to None at trade close. Reinitiated in startup_sync if position recovered.
- **Local confidence pre-gate**: only escalates to AI if local score >= 50%. AI cooldown is 30min regardless of result.
- **WebResearcher.research()** is synchronous/blocking. Called in executor: `run_in_executor(None, self.researcher.research)`.
- **detect_setup()** is bidirectional: LONG setups (BB mid bounce, EMA50 bounce) require `daily_bullish=True`. SHORT setups (BB upper rejection, EMA50 rejection) require `daily_bullish=False`.
- **Session logic**: session.py uses UTC internally. SESSIONS dict in settings.py is Kuwait Time reference only. `get_current_session()` in session.py is the authoritative one used by monitor.py.
- **SEVERE adverse move** at Phase.INITIAL → auto-moves SL to breakeven (entry + BREAKEVEN_BUFFER=10). Does NOT close the trade.

---

## Telegram Commands (all implemented)
`/status /balance /journal /today /stats /cost /force /stop /pause /resume /close /kill`
- `/pause` = alias for `/stop`
- `/kill` = emergency close, no confirm dialog
- `/close` = asks for inline confirmation (Close now / Hold buttons)
- Trade alerts: CONFIRM / REJECT inline buttons
- Position alerts: Close now / Hold inline buttons

---

## NEXT SESSION — Dashboard Implementation
Start here if opening a new chat to build the dashboard.
Read digests: `settings`, `monitor`, `database` (always). Others on demand.

Pre-requisites status (as of 2026-02-28):
1. Git SSH key: DONE. ed25519 key added to GitHub. Remote switched to git@github.com.
2. DASHBOARD_TOKEN: DONE. Added to .env.
3. SQLite WAL mode: PENDING — add `PRAGMA journal_mode=WAL;` to `Storage._init_db()` in storage/database.py
4. sudoers rule: PENDING — create /etc/sudoers.d/japan225-dashboard:
   `ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart japan225-bot, /bin/systemctl restart japan225-dashboard, /bin/systemctl stop japan225-bot`
5. Install dashboard deps: PENDING — `pip install fastapi "uvicorn[standard]" python-multipart httpx`
6. Cloudflare Tunnel: PENDING — user needs free Cloudflare account, then run cloudflared on VM

Dashboard architecture (UPDATED — approved Option C):
- Frontend: GitHub Pages at https://mostafaiq.github.io/japan225-bot/ (served from docs/ folder)
- Backend: FastAPI on VM localhost:8080, exposed via Cloudflare Tunnel (HTTPS, zero open ports)
- Tunnel URL: https://japan225-[name].cloudflare.dev (or custom domain if user has one)
- CORS: FastAPI allows requests from https://mostafaiq.github.io only
- Auth: Bearer token (DASHBOARD_TOKEN) — safe over HTTPS
- No SSH tunnel needed. No Oracle Cloud firewall changes needed.

Key decisions: FastAPI + GitHub Pages (docs/index.html), Cloudflare Tunnel, two-tier config, atomic overrides JSON.
Telegram stays 100% intact — dashboard is additive only.

Build order:
  Step 1: Pre-requisites above
  Step 2: dashboard/main.py + routers/status.py + services/db_reader.py + UI Status/Position panels
  Step 3: routers/config.py + services/config_manager.py + _reload_overrides() in monitor.py + Config panel UI
  Step 4: routers/history.py + routers/logs.py + UI Trade/Scan/Log sections
  Step 5: services/claude_client.py + routers/chat.py + Chat UI
  Step 6: services/git_ops.py + routers/apply_fix.py + Apply Fix UI
  Step 7: routers/controls.py + Bot Controls UI + full test + update digests

## Current State (as of 2026-02-28)
Upgrade to 5-min VM scanning complete. All modules created:
- session.py ✓  momentum.py ✓  confidence.py ✓
- indicators.py updated for bidirectional ✓
- analyzer.py updated for bidirectional ✓
- risk_manager.py updated for SHORT (direction-aware confidence floor) ✓
- telegram_bot.py: /pause /kill /close inline buttons ✓
- monitor.py: full scanning + monitoring loop ✓
- README.md / DEPLOY.md: updated to reflect 5-min scanning ✓

**Remaining issue**: Pre-screen bug (see Known Bug above). Fix this next session.

---

## DB Location
`storage/data/trading.db` — lives ONLY on Oracle VM. Never committed to git.
