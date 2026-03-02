# dashboard/ — DIGEST
# Purpose: FastAPI backend (port 8080) + GitHub Pages frontend. Admin UI for the bot.

## Services on VM
| systemd unit           | what it runs                                          |
|------------------------|-------------------------------------------------------|
| japan225-dashboard     | uvicorn dashboard.main:app --host 127.0.0.1 --port 8080 |
| japan225-ngrok         | ngrok http --domain=unmopped-shrimplike-sook.ngrok-free.app 8080 |

## URLs
- Frontend : https://mostafaiq.github.io/japan225-bot/  (docs/index.html, GitHub Pages)
- Backend  : https://unmopped-shrimplike-sook.ngrok-free.app  (ngrok free static domain)

## dashboard/main.py
FastAPI app. Auth middleware: Bearer DASHBOARD_TOKEN on all routes except OPTIONS + /api/health.
CORS: allow_origins=["https://mostafaiq.github.io", "http://localhost:3000"]
      allow_headers includes "ngrok-skip-browser-warning" (required for ngrok)
Routers: status, config, history, logs, chat, controls

## Routers

### routers/status.py
GET /api/health       → {"status":"ok"} — no auth
GET /api/status       → session, phase, scanning_paused, last_scan, next_scan_in,
                        last_scan_detail, ai_calls_today, cost_today, uptime, position, recent_scans, db_connected
Reads: bot_state.json (written by monitor._write_state()) + db_reader
NOTE: last_scan_detail is passed from bot_state.json — contains outcome, direction, confidence, price, setup_type, reason

### routers/config.py
GET  /api/config      → merged DEFAULTS + overrides
POST /api/config      → body: {tier:"hot"|"restart", ...key:value}
                        hot keys: MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT, AI_COOLDOWN_MINUTES,
                                  SCAN_INTERVAL_SECONDS, DEBUG, scanning_paused
                        restart keys: BREAKEVEN_TRIGGER, TRAILING_STOP_DISTANCE,
                                      DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE,
                                      MAX_MARGIN_PERCENT, TRADING_MODE
                        Blocks restart-tier if position is open (409)

### routers/history.py
GET /api/history?limit=N  → {trades, total, wins, losses, total_pnl}

### routers/logs.py
GET /api/logs?type=scan|system&lines=N  (lines: 10-200, default 70)
  scan   → journalctl filtered by: SCAN|SETUP|SIGNAL|TRADE|ALERT|CONFIRM|PHASE|MOMENTUM|ERROR|WARN|CONFIDENCE|HAIKU|SONNET|OPUS|REJECTED|APPROVED|COOLDOWN|ESCALAT|PRE-SCREEN|SCREEN:|BLOCK
  system → raw journalctl output
Strips ANSI escape codes.

### routers/chat.py
POST /api/chat              → body: {message, history:[{role,content}]} → {job_id, status:"pending"}
GET  /api/chat/status/{id}  → {status:"pending"|"done"|"error", response:str|null} — poll every 4s
GET  /api/chat/history      → {messages:[{role,content}], updated_at: ISO str}
POST /api/chat/history      → body: {messages:[]} → {ok: true, updated_at: ISO str}
  Persists to storage/data/chat_history.json (last 40 messages). Cross-device sync.
GET  /api/chat/costs        → {today_usd, total_usd, note:"estimate", entries:[last 20 today]}
  Reads storage/data/chat_costs.json (written by claude_client._log_chat_cost())

### routers/controls.py
POST /api/controls/force-scan  → writes storage/data/force_scan.trigger
POST /api/controls/restart     → sudo systemctl restart japan225-bot (warns if position open)
POST /api/controls/stop        → sudo systemctl stop japan225-bot (blocks if position open)
POST /api/apply-fix            → body:{target, diff} → git_ops.apply_fix()

## Services

### services/db_reader.py
Read-only SQLite: file:{DB_PATH}?mode=ro
get_position()            → dict or None (maps stop_level→stop_loss, limit_level→take_profit)
get_recent_scans(n)       → list[dict] last N scans, oldest-first
get_trade_history(n)      → list[dict] last N closed trades
get_cost_today()          → float ($)
get_ai_calls_today()      → int
db_exists()               → bool

### services/config_manager.py
OVERRIDES_PATH = storage/data/dashboard_overrides.json
read_overrides()          → merged {**DEFAULTS, **overrides}
write_overrides(updates, tier) → validates keys for tier, atomic write, returns merged config

### services/claude_client.py  [see claude_client.digest.md]

### services/git_ops.py
apply_fix(target: str, diff: str) → dict
  Validates: path in project root, .py/.json/.md only, no .env or *.db
  Sequence: patch --dry-run → git stash <file> (rollback safety) → patch apply → git add/commit/push

## Inter-process communication (monitor.py ↔ dashboard)
storage/data/bot_state.json           ← monitor._write_state() each cycle
storage/data/dashboard_overrides.json ← config_manager, read by monitor._reload_overrides()
storage/data/force_scan.trigger       ← created by /api/controls/force-scan, deleted by monitor
storage/data/chat_history.json        ← dashboard chat history (cross-device sync, last 40 msgs)
storage/data/chat_costs.json          ← per-call Anthropic cost log (max 500 entries)

## docs/index.html (frontend)
Single-page app. Dark trading theme. 6 tabs: Overview, Config, History, Logs, Chat, Controls.
Settings modal: API URL + DASHBOARD_TOKEN stored in localStorage.
All fetch() calls include headers: Authorization: Bearer <token>, ngrok-skip-browser-warning: true
Chat: marked.js markdown rendering, localStorage + server-side persistence.
  Cross-device sync: saves to POST /api/chat/history on send, polls GET /api/chat/history every 5s.
  Max 40 messages kept server-side. BroadcastChannel('j225') for instant same-browser sync.
Auto-refresh: Overview tab every 15s.
