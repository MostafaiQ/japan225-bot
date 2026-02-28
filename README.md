# Japan 225 Semi-Auto Trading Bot

Automated Japan 225 Cash CFD scanning system on IG Markets. Runs entirely on an Oracle Cloud VM, scanning every **5 minutes** during active sessions using Claude AI (Sonnet for routine pre-screening, Opus for full AI confirmation). Sends Telegram alerts with CONFIRM/REJECT buttons for human approval before execution.

**This is NOT a fully autonomous bot.** Every trade requires your explicit confirmation via Telegram. You stay in control.

---

## Architecture

```
Oracle Cloud VM (always-on, does everything)
┌─────────────────────────────────────────────────────────────────┐
│  monitor.py                                                     │
│                                                                 │
│  SCANNING MODE (no open position)                               │
│  - Every 5 min during active sessions (Tokyo/London/NY)         │
│  - Every 30 min off-hours (heartbeat only)                      │
│  - Local pre-screen → confidence score → AI escalation          │
│  - AI cooldown: 30 min between escalations (cost control)       │
│  - Supports LONG and SHORT setups                               │
│                                                                 │
│  MONITORING MODE (position open)                                │
│  - Every 60 seconds                                             │
│  - 3-phase exit management (breakeven / runner)                 │
│  - Adverse move alerts (mild / moderate / severe)               │
│  - Stale data detection                                         │
│                                                                 │
│  TELEGRAM BOT (always listening)                                │
│  - /status /balance /close etc.                                 │
│  - CONFIRM / REJECT trade alerts                                │
│  - Inline Close / Hold buttons on position alerts               │
└─────────────────────────────────────────────────────────────────┘
```

**One process, one VM:**

- **monitor.py:** Runs 24/7 on Oracle Cloud Free Tier. Scans for setups every 5 minutes during active market sessions, monitors open positions every 60 seconds, handles all Telegram commands, executes trades on user confirmation, and manages the 3-phase exit strategy.

GitHub Actions is used **only for CI tests** (on pull requests). Scanning no longer runs on GitHub Actions.

---

## Architecture

```
Oracle VM (always-on, does everything)
┌─────────────────────────────────────────────────────────────────┐
│  monitor.py  (systemd: japan225-bot)                            │
│  ├─ SCANNING (no open position)                                 │
│  │   every 5 min active sessions · 30 min off-hours             │
│  │   pre-screen → confidence → Sonnet → Opus → alert            │
│  ├─ MONITORING (position open)                                  │
│  │   every 60 s · 3-phase exit · adverse move alerts            │
│  └─ TELEGRAM (always-on polling)                                │
│      /menu · /status · CONFIRM/REJECT · Close/Hold              │
├─────────────────────────────────────────────────────────────────┤
│  Dashboard (systemd: japan225-dashboard · japan225-ngrok)       │
│  ├─ FastAPI on 127.0.0.1:8080                                   │
│  ├─ ngrok static tunnel → https://…ngrok-free.app              │
│  └─ Frontend: GitHub Pages (docs/index.html)                    │
│      6 tabs · agentic Claude chat · cross-device sync           │
└─────────────────────────────────────────────────────────────────┘
```

**One process, one VM.** GitHub Actions is used only for CI tests.

---

## Project Structure

```
japan225-bot/
├── monitor.py                 # Main VM process: scan + monitor + Telegram
├── config/
│   └── settings.py            # All constants (never scatter config)
├── core/
│   ├── ig_client.py           # IG Markets REST API wrapper
│   ├── indicators.py          # Bollinger, EMA, RSI, VWAP + detect_setup() LONG/SHORT
│   ├── session.py             # Session awareness (Tokyo/London/NY), no-trade days
│   ├── momentum.py            # MomentumTracker + adverse move tier detection
│   └── confidence.py          # 8-point confidence scoring (LONG and SHORT)
├── ai/
│   └── analyzer.py            # Claude Sonnet + Opus scan analysis + web research
├── trading/
│   ├── risk_manager.py        # 11-point pre-trade validation
│   └── exit_manager.py        # 3-phase exit strategy (Initial/Breakeven/Runner)
├── notifications/
│   └── telegram_bot.py        # Alerts, persistent menu, trade confirmation, inline buttons
├── storage/
│   ├── database.py            # SQLite WAL-mode persistent state
│   └── data/                  # VM only — never committed
│       ├── trading.db         # Main database
│       ├── bot_state.json     # Written each cycle by monitor.py → read by dashboard
│       ├── dashboard_overrides.json  # Hot/restart config from dashboard
│       ├── force_scan.trigger # Created by dashboard → consumed by monitor
│       ├── chat_history.json  # Dashboard chat history (cross-device sync)
│       └── chat_costs.json    # Per-call Anthropic cost log for dashboard chat
├── dashboard/
│   ├── main.py                # FastAPI app + CORS + Bearer auth
│   ├── run.py                 # uvicorn entrypoint
│   ├── routers/
│   │   ├── status.py          # GET /api/health · GET /api/status
│   │   ├── config.py          # GET/POST /api/config (hot + restart tiers)
│   │   ├── history.py         # GET /api/history
│   │   ├── logs.py            # GET /api/logs?type=scan|system
│   │   ├── chat.py            # POST /api/chat · GET/POST /api/chat/history · GET /api/chat/costs
│   │   └── controls.py        # POST /api/controls/{force-scan,restart,stop} · POST /api/apply-fix
│   └── services/
│       ├── claude_client.py   # Agentic Claude loop (tools: read/edit/write/run/search)
│       ├── config_manager.py  # dashboard_overrides.json atomic read/write
│       ├── db_reader.py       # Read-only SQLite access for dashboard
│       └── git_ops.py         # apply-fix: patch --dry-run → stash → apply → commit → push
├── docs/
│   └── index.html             # Single-page frontend (GitHub Pages)
├── tests/                     # 233 tests — all passing
├── .github/workflows/
│   └── tests.yml              # CI — runs tests only, no scanning
├── .env.example
├── requirements.txt
├── setup.sh
├── DEPLOY.md
└── README.md
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/japan225-bot.git
cd japan225-bot

# 2. Set up credentials
cp .env.example .env
nano .env  # Fill in all values

# 3. Run setup (installs deps, runs tests, verifies all connections)
chmod +x setup.sh
./setup.sh

# 4. Start the monitor (this is the only process needed)
python monitor.py
```

---

## Credentials Required

| Service | Where to Get | What You Need |
|---------|-------------|---------------|
| IG Markets API | labs.ig.com | API key, username, password, account number |
| Anthropic API | console.anthropic.com | API key (needs ~$10 credit) |
| Telegram Bot | @BotFather on Telegram | Bot token + your chat ID |

**Getting your Telegram Chat ID:** Send any message to your bot, then visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` and look for `"chat":{"id":XXXXXXX}`.

---

## Environment Variables

Set these in your `.env` file on the Oracle VM:

```
IG_API_KEY
IG_USERNAME
IG_PASSWORD
IG_ACC_NUMBER
IG_ENV          (demo or live)
ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TRADING_MODE    (paper or live)
```

---

## Web Dashboard

A mobile-friendly web dashboard for remote monitoring and control — no SSH required.

- **URL:** `https://mostafaiq.github.io/japan225-bot/`
- **Backend:** ngrok tunnel to FastAPI on the VM (port 8080)
- **Auth:** Bearer token (your `DASHBOARD_TOKEN` from `.env`)

| Tab | What it does |
|-----|-------------|
| **Overview** | Bot status, active position with live P&L, recent scan table. Auto-refreshes every 15s. |
| **Config** | Hot-reload settings (confidence, cooldown, scan interval, pause toggle) and restart-required settings (SL/TP/breakeven distances). |
| **History** | Full trade journal with W/L stats and total P&L. |
| **Logs** | Scan log and system log viewer, colour-coded by severity. |
| **Chat** | Fully agentic Claude Code. Reads files, fixes bugs, runs shell commands, pushes to GitHub. Cross-device synced — conversations persist across phone, tablet, and desktop. |
| **Controls** | Force scan, restart bot, stop bot, apply a raw unified diff. |

Chat history is stored server-side and synced across all devices every 5 seconds. Open the dashboard on your phone and PC simultaneously — messages appear on both.

---

## Telegram Commands

Use `/menu` for an interactive inline button panel (easiest on mobile).

| Command | Description |
|---------|-------------|
| `/menu` | Interactive inline panel with all Info + Control buttons |
| `/status` | Current mode, position, balance, today's P&L |
| `/balance` | Account details, compound plan progress |
| `/journal` | Last 5 trades with results |
| `/today` | Today's scan history |
| `/stats` | Win rate, avg win/loss, performance |
| `/cost` | Total API costs |
| `/force` | Force an immediate scan |
| `/stop` or `/pause` | Pause new entries |
| `/resume` | Resume scanning |
| `/close` | Close any open position (asks for confirmation) |
| `/kill` | EMERGENCY: close position immediately, no confirmation |

Trade alerts include inline **CONFIRM** / **REJECT** buttons. Position alerts include inline **Close now** / **Hold** buttons.

**Telegram stays available even when IG is down.** If IG Markets returns 503 (e.g. weekend maintenance), the bot sends a Telegram alert and retries the IG connection every 5 minutes — it does not exit.

---

## How a Scan Works

Every 5 minutes during active sessions:

1. **Session check:** Active session? (Tokyo / London / NY). Skip if off-hours or no-trade day.
2. **Pre-check:** System active? No open position? Not in cooldown?
3. **IG API:** Fetch 15M candles for local pre-screen.
4. **Indicators:** Calculate Bollinger Bands, EMA 50/200, RSI, VWAP. Detect LONG or SHORT setup.
5. **Confidence score:** Score the setup locally (0–100%). Skip AI if below threshold.
6. **AI escalation (if passes):** Fetch 4H/Daily data + web research (news, VIX, USD/JPY, calendar).
7. **Sonnet pre-screen:** Quick analysis to confirm the setup.
8. **Opus confirmation:** Deep analysis only if Sonnet agrees (saves cost). 30-min AI cooldown.
9. **Risk validation:** 11 independent checks must ALL pass.
10. **Telegram alert:** CONFIRM/REJECT buttons. Alert expires in 15 minutes if not confirmed.
11. **Execute:** On CONFIRM, trade placed via IG API.

---

## Risk Management (11 Checks, All Must Pass)

| # | Check | Rule |
|---|-------|------|
| 1 | Confidence | LONG >= 70%, SHORT >= 75% (hard floors, cannot be overridden) |
| 2 | Margin | Must not exceed 50% of account balance |
| 3 | Risk/Reward | Must be >= 1:1.5 after spread adjustment |
| 4 | Max Positions | Only 1 open position at a time |
| 5 | Consecutive Losses | 2 losses in a row = 4-hour cooldown |
| 6 | Daily Loss | Max 10% of balance lost per day |
| 7 | Weekly Loss | Max 20% of balance lost per week |
| 8 | Event Blackout | No trades within 60 min of high-impact events |
| 9 | Calendar Block | No Friday with PPI/CPI/NFP/BOJ, no month-end |
| 10 | Dollar Risk | Max 10% of balance risked on any single trade |
| 11 | System Active | System must not be paused |

---

## Confidence Scoring (8-Point System)

Base confidence starts at 30%. Each criterion adds 10 points. Capped at 100%.

| Criterion | +10% if... |
|-----------|-----------|
| Daily Trend | Price above (LONG) or below (SHORT) EMA 200 on daily chart |
| Entry at Tech Level | Entry at Bollinger midband or EMA 50 |
| RSI in Range | LONG: RSI 35–55 on 15M. SHORT: RSI 55–75 on 15M. |
| TP Viable | Take profit achievable |
| Higher Lows / Lower Highs | Price structure confirms direction |
| Macro Alignment | News/sentiment supports the trade direction |
| No Event 1hr | No high-impact event within 1 hour |
| No Friday/Month-End | Not Friday with data or last 2 days of month |

**Minimum to trade: 70% (LONG), 75% (SHORT).** Shorts have a higher bar due to BOJ intervention risk.

---

## 3-Phase Exit Strategy

| Phase | Trigger | Action |
|-------|---------|--------|
| **1. Initial** | Trade opened | Fixed SL (200pts) and TP (400pts). Wait. |
| **2. Breakeven** | +150pts reached | Move SL to entry + 10pt buffer. TP unchanged. |
| **3. Runner** | 75% of TP in under 2hrs | Remove TP. Trailing stop at 150pts. Let it run. |

The monitor checks every 60 seconds and auto-executes phase transitions.

**Adverse move alerts** (while position open):
- **+30pts against:** Alert only.
- **+50pts against:** Alert + suggest close.
- **+80pts against:** Auto-move SL to breakeven, then alert.

---

## Active Sessions (Kuwait Time, UTC+3)

Scanning runs every **5 minutes** during these sessions. Off-hours = 30-minute heartbeat only.

| Kuwait Time | Session | Priority |
|-------------|---------|----------|
| 03:00–05:00 | Tokyo Open | HIGH |
| 05:00–07:00 | Mid Tokyo | HIGH |
| 07:00–09:00 | Late Tokyo | MEDIUM |
| 09:00–11:00 | Tokyo Close | MEDIUM |
| 11:00–13:00 | London Open | HIGH |
| 13:00–15:00 | Mid London | MEDIUM |
| 15:00–17:00 | Late London | MEDIUM |
| 17:30–19:30 | NY Open | HIGH |
| 19:30–21:30 | Mid NY | MEDIUM |
| 21:30–23:30 | Late NY | LOW |
| 00:00–03:00 | Off Hours | SKIP |

Monday through Friday only.

---

## Estimated Costs

| Item | Cost |
|------|------|
| IG Markets API | Free |
| Oracle Cloud VM | Free (Always Free Tier) |
| Telegram | Free |
| **Anthropic API** | **~$30–60/month** |

Most scans use local pre-screening only (no AI cost). AI (Sonnet + Opus) is only called when the local confidence score passes the threshold, with a 30-minute cooldown between AI calls. Typical: 1–5 AI escalations per day.

---

## Testing

```bash
# Run all tests (no credentials needed)
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_indicators.py -v
python -m pytest tests/test_risk_manager.py -v
python -m pytest tests/test_storage.py -v

# Full setup verification (needs credentials)
./setup.sh

# Paper mode (live data, no real trades)
TRADING_MODE=paper python monitor.py
```

---

## Dashboard Deployment

The dashboard runs as two additional systemd services on the same Oracle VM:

| Service | What it runs |
|---------|-------------|
| `japan225-dashboard` | FastAPI on `127.0.0.1:8080` |
| `japan225-ngrok` | ngrok tunnel (static free domain) |

See [DEPLOY.md](DEPLOY.md) for full setup instructions including the dashboard and ngrok services.

## Deployment

Quick summary:
1. Create an Oracle Cloud Always Free VM (ARM, 1 OCPU, 6GB RAM)
2. Clone repo, install Python 3.10+, set up `.env`
3. Run `./setup.sh` to verify everything
4. Start monitor.py with systemd (auto-restart on crash)

---

## Safety Notes

- **Paper mode first.** Always test with `TRADING_MODE=paper` before going live.
- **Demo account first.** Use `IG_ENV=demo` to test against IG's demo environment.
- **Start small.** The compound plan starts at 0.01–0.02 lots for a reason.
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **The confidence floors are hard-coded.** 70% LONG, 75% SHORT. Cannot be overridden. Intentional.

---

*Built for Mostafa's Japan 225 compound trading project.*
*Scan. Analyze. Alert. Confirm. Execute. Repeat.*
