# Japan 225 Semi-Auto Trading Bot

Automated Japan 225 Cash CFD scanning system on IG Markets. Scans every 2 hours using Claude AI (Sonnet 4.5 for routine scanning, Opus 4.6 for trade confirmation), sends Telegram alerts with CONFIRM/REJECT buttons for human approval before execution.

**This is NOT a fully autonomous bot.** Every trade requires your explicit confirmation via Telegram. You stay in control.

---

## Architecture

```
GitHub Actions (every 2 hours)           Oracle Cloud VM (always-on)
┌──────────────────────────┐            ┌──────────────────────────┐
│  1. Fetch IG price data  │            │  Telegram Bot (polling)  │
│  2. Calculate indicators │            │  - /status /balance etc  │
│  3. Web research (news)  │───alert──> │  - CONFIRM/REJECT trades │
│  4. Sonnet 4.5 scan      │            │                          │
│  5. Opus 4.6 confirm     │            │  Position Monitor (60s)  │
│  6. Risk validation      │            │  - Phase 1: Fixed SL/TP  │
│  7. Telegram alert       │            │  - Phase 2: Breakeven    │
│  8. Save to SQLite       │            │  - Phase 3: Runner trail │
│  9. Git commit           │            │  - Trade execution       │
└──────────────────────────┘            └──────────────────────────┘
```

**Two processes, one system:**

- **Scanner (main.py):** Runs on GitHub Actions every 2 hours during market hours (Mon-Fri). Fetches prices, calculates indicators, runs AI analysis. If a setup is found with 70%+ confidence, sends a Telegram alert. Costs nothing to run.

- **Monitor (monitor.py):** Runs 24/7 on Oracle Cloud Free Tier. Handles Telegram commands, executes trades on user confirmation, monitors open positions every 60 seconds, manages the 3-phase exit strategy. Also costs nothing.

---

## Project Structure

```
japan225-bot/
├── main.py                    # Scan pipeline (GitHub Actions entry point)
├── monitor.py                 # Position monitor + Telegram bot (Oracle VM)
├── setup.sh                   # Interactive setup and verification script
├── config/
│   └── settings.py            # All trading rules, constants, credentials
├── core/
│   ├── ig_client.py           # IG Markets REST API wrapper
│   └── indicators.py          # Bollinger, EMA, RSI, VWAP (pure math)
├── ai/
│   └── analyzer.py            # Claude AI analysis + web research
├── trading/
│   ├── risk_manager.py        # 11-point pre-trade validation
│   └── exit_manager.py        # 3-phase exit strategy
├── notifications/
│   └── telegram_bot.py        # Alerts, commands, trade confirmation
├── storage/
│   └── database.py            # SQLite persistent state
├── tests/
│   ├── test_indicators.py     # Indicator + math tests (23 tests)
│   ├── test_risk_manager.py   # Risk + exit manager tests (21 tests)
│   └── test_storage.py        # Database tests (15 tests)
├── .github/workflows/
│   ├── scan.yml               # Cron scan schedule
│   └── tests.yml              # CI test pipeline
├── .env.example               # Credential template
├── .gitignore
├── requirements.txt
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

# 4. Test a scan (paper mode)
TRADING_MODE=paper python main.py

# 5. Start the monitor
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

## GitHub Secrets

Add these in your repo under **Settings > Secrets and variables > Actions:**

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

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Current position, balance, system state |
| `/balance` | Account details, P&L, API costs |
| `/journal` | Last 5 trades with results |
| `/today` | Today's scan history |
| `/stats` | Win rate, avg win/loss, performance |
| `/cost` | Total API costs |
| `/force` | Force an immediate scan |
| `/stop` | Pause all scanning |
| `/resume` | Resume scanning |
| `/close` | Close any open position |

---

## How a Scan Works

Every 2 hours during market hours:

1. **Pre-checks:** System active? No open position?
2. **IG API:** Fetch price data across 4 timeframes (Daily, 4H, 15M, 5M)
3. **Indicators:** Calculate Bollinger Bands, EMA 50/200, RSI 14, VWAP
4. **Web research:** News headlines, economic calendar, VIX, USD/JPY, Fear & Greed
5. **Sonnet 4.5 scan:** Quick analysis for setup detection
6. **Opus 4.6 confirmation:** Deep analysis only if Sonnet found something (saves cost)
7. **Risk validation:** 11 independent checks must ALL pass
8. **Telegram alert:** If everything passes, sends alert with CONFIRM/REJECT buttons
9. **Wait for you:** Alert expires in 15 minutes if not confirmed
10. **Execute:** On CONFIRM, the monitor places the trade via IG API

---

## Risk Management (11 Checks, All Must Pass)

| # | Check | Rule |
|---|-------|------|
| 1 | Confidence | Must be >= 70% (HARD FLOOR, cannot be overridden) |
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
| Daily Bullish | Price above EMA 200 on daily chart |
| Entry at Tech Level | Entry at Bollinger midband or EMA 50 |
| RSI 15M in Range | RSI between 35-55 on 15M |
| TP Viable | Take profit achievable |
| Higher Lows | Price making higher lows |
| Macro Bullish | News/sentiment supports long |
| No Event 1hr | No high-impact event within 1 hour |
| No Friday/Month-End | Not Friday with data or last 2 days of month |

**Minimum to trade: 70%.** 4 of 8 criteria plus the base must be met.

---

## 3-Phase Exit Strategy

| Phase | Trigger | Action |
|-------|---------|--------|
| **1. Initial** | Trade opened | Fixed SL (200pts) and TP (400pts). Wait. |
| **2. Breakeven** | +150pts reached | Move SL to entry + 10pt buffer. TP unchanged. |
| **3. Runner** | 75% of TP in under 2hrs | Remove TP. Trailing stop at 150pts. Let it run. |

The monitor checks every 60 seconds and auto-executes phase transitions.

---

## Scan Schedule (Kuwait Time, UTC+3)

| Kuwait Time | Session | Priority |
|-------------|---------|----------|
| 03:00 | Tokyo Open | HIGH |
| 05:00 | Mid Tokyo | HIGH |
| 07:00 | Late Tokyo | MEDIUM |
| 09:00 | Tokyo Close | MEDIUM |
| 11:00 | London Open | HIGH |
| 13:00 | Mid London | MEDIUM |
| 15:00 | Late London | MEDIUM |
| 17:30 | NY Open | HIGH |
| 19:30 | Mid NY | MEDIUM |
| 21:30 | Late NY | LOW |
| 23:30 | NY Close | LOW |

11 scans per day, Monday through Friday.

---

## Estimated Costs

| Item | Cost |
|------|------|
| IG Markets API | Free |
| GitHub Actions | Free (2,000 min/month) |
| Oracle Cloud VM | Free (Always Free Tier) |
| Telegram | Free |
| **Anthropic API** | **~$56/month** |

Most scans use Sonnet only (~$0.02/scan). Opus is only called when Sonnet finds a setup (~$0.15/call), maybe 1-3 times per day.

---

## Testing

```bash
# Run all 59 tests (no credentials needed)
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_indicators.py -v
python -m pytest tests/test_risk_manager.py -v
python -m pytest tests/test_storage.py -v

# Full setup verification (needs credentials)
./setup.sh

# Paper mode scan
TRADING_MODE=paper python main.py
```

---

## Deployment

See [DEPLOY.md](DEPLOY.md) for the full Oracle Cloud deployment guide.

Quick summary:
1. Create an Oracle Cloud Always Free VM (ARM, 1 OCPU, 6GB RAM)
2. Clone repo, install Python 3.11+, set up .env
3. Run `./setup.sh` to verify everything
4. Start the monitor with systemd (auto-restart on crash)
5. Push to GitHub to enable the scan workflow

---

## Safety Notes

- **Paper mode first.** Always test with `TRADING_MODE=paper` before going live.
- **Demo account first.** Use `IG_ENV=demo` to test against IG's demo environment.
- **Start small.** The compound plan starts at 0.01-0.02 lots for a reason.
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **The 70% confidence floor is hard-coded.** It cannot be overridden. Intentional.

---

*Built for Mostafa's Japan 225 compound trading project.*
*Scan. Analyze. Alert. Confirm. Execute. Repeat.*
