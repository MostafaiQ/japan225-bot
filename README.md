# Japan 225 Semi-Auto Trading Bot

Automated Japan 225 Cash CFD scanning system on IG Markets. Runs entirely on an Oracle Cloud VM, scanning every **5 minutes** during active sessions using a 3-tier Claude AI pipeline (Haiku pre-gate → Sonnet analysis → Opus confirmation). Uses Claude Code CLI subscription ($0/call). Sends Telegram alerts with CONFIRM/REJECT buttons for human approval before execution.

**This is NOT a fully autonomous bot.** Every trade requires your explicit confirmation via Telegram. You stay in control.

---

## Architecture

```
Oracle VM (always-on, does everything)
┌─────────────────────────────────────────────────────────────────┐
│  monitor.py  (systemd: japan225-bot)                            │
│  ├─ SCANNING (no open position)                                 │
│  │   every 5 min active sessions · 30 min off-hours             │
│  │   pre-screen → confidence → Haiku → Sonnet → Opus → alert   │
│  ├─ MONITORING (position open)                                  │
│  │   every 2 s · 3-phase exit · adverse move alerts             │
│  └─ TELEGRAM (always-on polling)                                │
│      /menu · /status · CONFIRM/REJECT · Close/Hold              │
├─────────────────────────────────────────────────────────────────┤
│  Dashboard (systemd: japan225-dashboard · japan225-ngrok)       │
│  ├─ FastAPI on 127.0.0.1:8080                                   │
│  ├─ ngrok static tunnel → https://…ngrok-free.app              │
│  └─ Frontend: GitHub Pages (docs/index.html)                    │
│      6 tabs · Claude Code chat · cross-device sync              │
└─────────────────────────────────────────────────────────────────┘
```

**One process, one VM.** GitHub Actions is used only for CI tests.

---

## Project Structure

```
japan225-bot/
├── monitor.py                 # Main VM process: scan + monitor + Telegram
├── healthcheck.py             # Session-start health check (services/tests/git/trades)
├── config/
│   └── settings.py            # All constants — single source of truth
├── core/
│   ├── ig_client.py           # IG Markets REST API wrapper
│   ├── indicators.py          # Bollinger, EMA, RSI, VWAP, Heiken Ashi, FVG, Fibonacci, PDH/PDL, liquidity sweep + detect_setup()
│   ├── session.py             # Session awareness (Tokyo/London/NY), no-trade days
│   ├── momentum.py            # MomentumTracker + adverse move tier detection
│   └── confidence.py          # 11-criteria proportional confidence scoring (LONG and SHORT)
├── ai/
│   ├── analyzer.py            # 3-tier AI: Haiku pre-gate → Sonnet → Opus (Claude Code CLI, subscription)
│   └── context_writer.py      # Writes storage/context/*.md before each AI call
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
│       └── chat_history.json  # Dashboard chat history (cross-device sync)
├── dashboard/
│   ├── main.py                # FastAPI app + CORS + Bearer auth
│   ├── run.py                 # uvicorn entrypoint
│   ├── routers/
│   │   ├── status.py          # GET /api/health · GET /api/status
│   │   ├── config.py          # GET/POST /api/config (hot + restart tiers)
│   │   ├── history.py         # GET /api/history
│   │   ├── logs.py            # GET /api/logs?type=scan|system
│   │   ├── chat.py            # POST /api/chat · GET/POST /api/chat/history
│   │   └── controls.py        # POST /api/controls/{force-scan,restart,stop} · POST /api/apply-fix
│   └── services/
│       ├── claude_client.py   # Spawns Claude Code CLI (claude --print) as subprocess
│       ├── config_manager.py  # dashboard_overrides.json atomic read/write
│       ├── db_reader.py       # Read-only SQLite access for dashboard
│       └── git_ops.py         # apply-fix: patch --dry-run → stash → apply → commit → push
├── docs/
│   └── index.html             # Single-page frontend (GitHub Pages)
├── tests/                     # 264 tests — all passing
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
python3 monitor.py
```

---

## Credentials Required

| Service | Where to Get | What You Need |
|---------|-------------|---------------|
| IG Markets API | labs.ig.com | API key, username, password, account number |
| Claude Code CLI | claude.ai/download | Subscription (Pro/Max plan, $0/call) |
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
IG_ENV              (demo or live)
ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TRADING_MODE        (live)
DASHBOARD_TOKEN     (long random secret for web dashboard auth)
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
| **Chat** | Full Claude Code — reads files, fixes bugs, runs shell commands, pushes to GitHub, searches the web. Cross-device synced. |
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

**Telegram stays available even when IG is down.** If IG Markets returns 503 (e.g. weekend maintenance), the bot sends a Telegram alert and retries the IG connection every 1 minute — it does not exit. Dashboard shows "IG OFFLINE" phase during this time.

---

## How a Scan Works

Every 5 minutes during active sessions:

1. **Session check:** Active session? (Tokyo / London / NY). Skip if off-hours or no-trade day.
2. **Pre-check:** System active? No open position? Not in cooldown?
3. **IG API:** Fetch 15M + Daily candles in parallel for local pre-screen.
4. **Indicators:** Bollinger Bands, EMA 50/200, RSI, VWAP, Heiken Ashi, FVG, Fibonacci, PDH/PDL, liquidity sweep. Detect LONG or SHORT setup (bidirectional — no daily hard gate).
5. **Confidence score:** 11-criteria proportional scoring (0–100%). Skip AI if below 60%.
6. **Haiku pre-gate:** Fast filter with full context (web research, failed criteria, indicators). Rejects obvious noise. 15-min cooldown on reject.
7. **AI escalation (if Haiku approves):** Fetch 4H data. Write context files to `storage/context/*.md`.
8. **Sonnet analysis:** Full scan with setup type, reasoning, direction, and session context.
9. **Opus confirmation:** Called if Sonnet confidence is 75–86% (devil's advocate) or if Sonnet rejects but confidence is near threshold (second opinion).
10. **Risk validation:** 11 independent checks must ALL pass.
11. **Telegram alert:** CONFIRM/REJECT buttons. Alert expires in 15 minutes if not confirmed.
12. **Execute:** On CONFIRM, trade placed via IG API.

---

## Setup Types

| Setup | Direction | Trigger | RSI Gate |
|-------|-----------|---------|----------|
| `bollinger_mid_bounce` | LONG | Price within 150pts of BB midband | 35–55 |
| `bollinger_lower_bounce` | LONG | Price within 80pts of BB lower band | 20–40 (deeply oversold) |
| `bollinger_upper_rejection` | SHORT | Price within 150pts of BB upper band | 55–75 |
| `ema50_rejection` | SHORT | Price at or below EMA50 + 2pts | 50–70 |

Setup detection is **bidirectional** — no daily trend hard gate. Counter-trend setups are penalized by confidence criterion C1 (daily trend). AI (Haiku + Sonnet + Opus) acts as the quality gate.

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

## Confidence Scoring (11-Criteria Proportional)

Proportional formula: `score = min(30 + passed × 70 / 11, 100)`. LONG needs 7/11 (74% ≥ 70%), SHORT needs 8/11 (80% ≥ 75%).

| # | Criterion | Passes if... |
|---|-----------|-------------|
| C1 | Daily Trend | Price above (LONG) or below (SHORT) EMA 200 on daily chart |
| C2 | Entry at Tech Level | Entry within 150pts of BB midband, EMA50, or 80pts of BB lower |
| C3 | RSI in Range | LONG: RSI 35–55. LONG lower band: RSI 20–40. SHORT: RSI 55–75 |
| C4 | TP Viable | Price at or below BB midband (pullback confirmation) |
| C5 | Structure | Price above (LONG) or below (SHORT) EMA50 on 15M |
| C6 | Macro Alignment | 4H RSI 35–75 (LONG) or 30–60 (SHORT) |
| C7 | No Event 1hr | No high-impact event within 1 hour |
| C8 | No Friday/Month-End | Not Friday with PPI/CPI/NFP/BOJ or last 2 days of month |
| C9 | Volume Confirmation | 15M volume ratio ≥ 0.8 |
| C10 | 4H EMA50 Alignment | Price above (LONG) or below (SHORT) 4H EMA50 |
| C11 | Heiken Ashi Aligned | 15M HA candle bullish (LONG) or bearish (SHORT) |

**Minimum to trade: 70% (LONG), 75% (SHORT).** Shorts have a higher bar due to BOJ intervention risk. C7/C8 are hard blocks (skip immediately, no AI call).

---

## 3-Phase Exit Strategy

| Phase | Trigger | Action |
|-------|---------|--------|
| **1. Initial** | Trade opened | SL at 150pts, TP at 400pts. |
| **2. Breakeven** | +150pts reached | Move SL to entry + 10pt buffer. TP unchanged. |
| **3. Runner** | 75% of TP reached | Remove TP. Trailing stop at 150pts. Let it run. |

The monitor checks every 2 seconds and auto-executes phase transitions.

**Adverse move alerts** (while position is open):
- **60pts against:** Alert only.
- **120pts against:** Alert + suggest close.
- **175pts against:** Auto-move SL to breakeven.

---

## Active Sessions

Scanning runs every **5 minutes** during active sessions. Off-hours = sleep until next session (capped at 30 min).

| UTC Hours | Session | Notes |
|-----------|---------|-------|
| 00:00–06:00 | Tokyo | N225 cash market, highest quality |
| 06:00–08:00 | Gap | Skipped — chaotic Tokyo-close / London-open crossover |
| 08:00–16:00 | London | Strong directional moves |
| 16:00–21:00 | New York | US-correlated, decent quality |
| 21:00–00:00 | Gap | Skipped — thin volume |

Monday through Friday only. No trading on US/JP holidays or NFP/CPI Fridays.

---

## Estimated Costs

| Item | Cost |
|------|------|
| IG Markets API | Free |
| Oracle Cloud VM | Free (Always Free Tier) |
| Telegram | Free |
| **Claude Code CLI** | **$0/call** (included in Pro/Max subscription) |

All AI calls (Haiku, Sonnet, Opus) use the Claude Code CLI with OAuth subscription — no per-token billing. Local pre-screening filters most scans before any AI call. Haiku pre-gate rejects weak setups cheaply (15-min cooldown). Typical: 5–12 AI evaluations per day across all sessions.

---

## Health Check

Run at the start of every session to confirm system state:

```bash
python3 healthcheck.py
```

Checks: all 3 services, test suite, git status, live trade stats, config overrides, recent errors.

---

## Testing

```bash
# Run all tests (no credentials needed)
python3 -m pytest tests/ -v

# Run specific test file
python3 -m pytest tests/test_indicators.py -v
python3 -m pytest tests/test_risk_manager.py -v
python3 -m pytest tests/test_storage.py -v
```

---

## Dashboard Deployment

The dashboard runs as two additional systemd services on the same Oracle VM:

| Service | What it runs |
|---------|-------------|
| `japan225-dashboard` | FastAPI on `127.0.0.1:8080` |
| `japan225-ngrok` | ngrok tunnel (static free domain) |

See [DEPLOY.md](DEPLOY.md) for full setup instructions.

## Deployment

Quick summary:
1. Create an Oracle Cloud Always Free VM (ARM, 1 OCPU, 6GB RAM)
2. Clone repo, install Python 3.10+, set up `.env`
3. Run `./setup.sh` to verify everything
4. Start monitor.py with systemd (auto-restart on crash)

---

## Safety Notes

- **Demo account first.** Use `IG_ENV=demo` to test against IG's demo environment before going live.
- **Start small.** The compound plan starts at 0.01–0.02 lots for a reason.
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **The confidence floors are hard-coded.** 70% LONG, 75% SHORT. Cannot be overridden. Intentional.

---

*Built for Mostafa's Japan 225 compound trading project.*
*Scan. Analyze. Alert. Confirm. Execute. Repeat.*
