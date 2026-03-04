# Japan 225 Trading Bot

An AI-powered semi-automated trading system for Japan 225 (Nikkei) Cash CFD on IG Markets. Uses Claude AI (Sonnet + Opus) for market analysis, sends trade alerts via Telegram for human confirmation, and manages positions with a 3-phase exit strategy.

**This is NOT a fully autonomous bot.** Every trade requires your explicit confirmation via Telegram. You stay in control.

## How It Works

```
Every 5 minutes during market hours:

  Fetch candles (15M + 5M + 4H + Daily)
       |
  Detect setup (bidirectional: LONG + SHORT simultaneously)
       |
  Score confidence (12 criteria, 0-100%)
       |
  [Below 60%] -----> Skip
       |
  Sonnet AI analysis ──── runs in parallel ──── Opus scalp evaluation
       |                                              |
  [Approved] -----> Risk validation                   |
       |            -----> Telegram alert              |
       |                   -----> You confirm          |
       |                          -----> Execute       |
  [Rejected, near-miss] -----> Check Opus result -----> Auto-execute scalp
```

All AI calls use the Claude Code CLI with your subscription (Pro/Max plan) -- no per-token billing.

## Features

- **Bidirectional scanning** -- evaluates both LONG and SHORT setups every cycle
- **12-criteria confidence scoring** -- filters noise before AI evaluation
- **Parallel AI pipeline** -- Sonnet + Opus run simultaneously (~10s total)
- **3-phase exit management** -- Initial SL/TP, breakeven lock at +150pts, trailing runner at 75% TP
- **Telegram control** -- alerts with CONFIRM/REJECT buttons, position management, full command set
- **Web dashboard** -- real-time monitoring, config, trade history, logs, Claude chat
- **Risk management** -- 11 independent pre-trade checks, all must pass
- **Adverse move detection** -- tiered alerts at 60/120/175pts against position

## Project Structure

```
japan225-bot/
├── monitor.py                  # Main process: scan + monitor + Telegram (systemd)
├── config/
│   └── settings.py             # All constants -- single source of truth
├── core/
│   ├── ig_client.py            # IG Markets REST API (candle caching, delta fetches)
│   ├── indicators.py           # BB, EMA, RSI, VWAP, Heiken Ashi, FVG, Fib, pivots + setup detection
│   ├── session.py              # Session hours (Tokyo/London/NY), no-trade days, blackouts
│   ├── momentum.py             # MomentumTracker, adverse move tier detection
│   └── confidence.py           # 12-criteria proportional scoring (LONG + SHORT)
├── ai/
│   ├── analyzer.py             # Sonnet + Opus pipeline (Claude Code CLI subprocess)
│   └── context_writer.py       # Market context file writer
├── trading/
│   ├── risk_manager.py         # 11-point pre-trade validation
│   └── exit_manager.py         # 3-phase exit (Initial → Breakeven → Runner)
├── notifications/
│   └── telegram_bot.py         # Alerts, inline buttons, trade confirmation
├── storage/
│   ├── database.py             # SQLite WAL-mode persistent state
│   └── data/                   # Runtime data (never committed)
├── dashboard/                  # FastAPI web dashboard + ngrok tunnel
│   ├── main.py                 # FastAPI app, CORS, Bearer auth
│   ├── routers/                # API endpoints (status, config, history, logs, chat, controls)
│   └── services/               # Claude client, config manager, DB reader
├── docs/
│   └── index.html              # Single-page dashboard frontend (GitHub Pages)
├── tests/                      # 338 tests (all passing, no credentials needed)
├── .github/workflows/
│   └── tests.yml               # CI -- runs tests on push/PR
├── .env.example                # Template for environment variables
├── requirements.txt
├── setup.sh                    # Setup verification script
├── DEPLOY.md                   # Full deployment guide
└── CONTRIBUTING.md             # Contribution guidelines
```

## Quick Start

### Prerequisites

- Python 3.10+
- [IG Markets API account](https://labs.ig.com) (free demo available)
- [Claude Code CLI](https://claude.ai/download) with Pro or Max subscription
- [Telegram bot](https://core.telegram.org/bots#botfather) (free)

### Setup

```bash
# Clone the repo
git clone https://github.com/mostafaiq/japan225-bot.git
cd japan225-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your values (see Environment Variables below)

# Verify everything works
./setup.sh

# Run the bot
python3 monitor.py
```

### Environment Variables

Create a `.env` file from `.env.example`:

```bash
# IG Markets API (get from labs.ig.com)
IG_API_KEY=your_api_key
IG_USERNAME=your_username
IG_PASSWORD=your_password
IG_ACC_NUMBER=your_account_number
IG_ENV=demo                    # "demo" for testing, "live" for real money

# Telegram Bot (get from @BotFather)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id  # See note below

# Trading
TRADING_MODE=live              # Bot mode
DEBUG=false                    # Extra verbose logging

# Dashboard (optional)
DASHBOARD_TOKEN=your_secret    # Long random string for web dashboard auth
```

**Getting your Telegram Chat ID:** Send any message to your bot, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and find `"chat":{"id":XXXXXXX}`.

## Scanning Pipeline

Every 5 minutes during active sessions:

1. **Session check** -- Tokyo (00-06 UTC), London (08-16), New York (16-21). Skip gaps and weekends.
2. **Fetch candles** -- 15M + 5M + 4H parallel, Daily sequential. Delta fetches after first call.
3. **Detect setups** -- Bidirectional: checks LONG and SHORT independently.
4. **5M fallback** -- If 15M finds no setup, tries 5M candles with alignment guard.
5. **Score confidence** -- 12-criteria proportional scoring for both directions.
6. **Hard blocks** -- Events within 1hr (C7) or Friday/month-end blackout (C8) skip immediately.
7. **AI analysis** -- Sonnet evaluates primary setup. Opus scalp eval runs in parallel.
8. **Risk validation** -- 11 independent checks, all must pass.
9. **Telegram alert** -- CONFIRM/REJECT buttons. Auto-executes after 2 min if no response.

## Setup Types

| Setup | Direction | Trigger | RSI Range |
|-------|-----------|---------|-----------|
| `bollinger_mid_bounce` | LONG | Price within 150pts of BB mid | 30-65 |
| `bollinger_lower_bounce` | LONG | Price within 150pts of BB lower | 20-40 |
| `oversold_reversal` | LONG | RSI < 30 + daily bullish + reversal confirm | < 30 |
| `extreme_oversold_reversal` | LONG | RSI < 22 + 4H near BB lower | < 22 |
| `bollinger_upper_rejection` | SHORT | Price within 150pts of BB upper | 55-75 |
| `ema50_rejection` | SHORT | Price at/below EMA50 | 50-70 |

All setups use SL=150pts, TP=400pts. Counter-trend setups are penalized by confidence scoring, not blocked.

## Confidence Scoring (12 Criteria)

Formula: `score = 30 + int(passed * 70 / 12)`. LONG needs 70% (7/12), SHORT needs 75% (8/12).

| # | Criterion | Description |
|---|-----------|-------------|
| C1 | Daily Trend | Price position relative to EMA200 on daily |
| C2 | Entry at Tech Level | Near BB mid/lower/upper or VWAP |
| C3 | RSI in Range | RSI within setup-specific zone |
| C4 | TP Viable | Take-profit target is reachable |
| C5 | Price Structure | Position relative to EMA50 on 15M |
| C6 | Macro Alignment | 4H RSI in healthy range |
| C7 | No Event 1hr | No high-impact economic event imminent |
| C8 | No Friday/Month-End | Calendar blackout check |
| C9 | Volume Confirmation | 15M volume ratio >= 0.8 |
| C10 | 4H EMA50 Alignment | 4H structure confirmation |
| C11 | Heiken Ashi Aligned | HA candle direction matches trade |
| C12 | Entry Quality | Pullback + volume confirmation |

C7/C8 are hard blocks -- if either fails, no AI call is made.

## Risk Management (11 Checks)

All must pass before a trade is placed:

| Check | Rule |
|-------|------|
| Confidence | LONG >= 70%, SHORT >= 75% |
| Margin | Must not exceed 50% of account balance |
| Risk/Reward | >= 1.5:1 after spread adjustment |
| Max Positions | 1 open position at a time |
| Consecutive Losses | 2 losses = 4-hour cooldown |
| Weekly Loss | Max 20% of balance per week |
| Event Blackout | No trades within 60 min of high-impact events |
| Calendar Block | No Friday PPI/CPI/NFP/BOJ, no month-end |
| System Active | System must not be paused |

## Exit Strategy (3 Phases)

| Phase | Trigger | Action |
|-------|---------|--------|
| Initial | Trade opened | SL at 150pts, TP at 400pts |
| Breakeven | +150pts profit | Move SL to entry + 10pt buffer |
| Runner | 75% of TP reached | Remove TP, trailing stop at 150pts |

**Adverse move alerts** while a position is open:
- 60pts against: alert only
- 120pts against: alert + suggest close
- 175pts against: auto-move SL to breakeven

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/menu` | Interactive inline button panel |
| `/status` | Current mode, position, balance |
| `/balance` | Account details |
| `/journal` | Last 5 trades |
| `/today` | Today's scan history |
| `/stats` | Win rate, performance metrics |
| `/force` | Force an immediate scan |
| `/stop` / `/pause` | Pause new entries |
| `/resume` | Resume scanning |
| `/close` | Close position (with confirmation) |
| `/kill` | Emergency close (immediate, no confirmation) |

Trade alerts include CONFIRM/REJECT inline buttons. Telegram stays available even when IG is down.

## Web Dashboard

An optional mobile-friendly dashboard for remote monitoring:

| Tab | What it does |
|-----|-------------|
| Overview | Bot status, live P&L, recent scans |
| Config | Hot-reload settings without restart |
| History | Full trade journal with stats |
| Logs | Scan and system logs, colour-coded |
| Chat | Claude Code AI assistant |
| Controls | Force scan, restart, stop, apply patches |

See [DEPLOY.md](DEPLOY.md) for dashboard setup.

## Active Sessions (UTC)

| Hours | Session | Notes |
|-------|---------|-------|
| 00:00-06:00 | Tokyo | N225 cash market, highest quality |
| 06:00-08:00 | -- | Skipped: chaotic crossover |
| 08:00-16:00 | London | Strong directional moves |
| 16:00-21:00 | New York | US-correlated |
| 21:00-00:00 | -- | Skipped: thin volume |

Monday-Friday only. No trading on US/JP holidays.

## Costs

| Item | Cost |
|------|------|
| IG Markets API | Free |
| Oracle Cloud VM | Free (Always Free Tier) |
| Telegram | Free |
| Claude Code CLI | Included in Pro/Max subscription |

## Testing

```bash
# Run all tests (no credentials needed)
python3 -m pytest tests/ -v

# Run specific module tests
python3 -m pytest tests/test_indicators.py -v
python3 -m pytest tests/test_risk_manager.py -v
python3 -m pytest tests/test_confidence.py -v
```

338 tests covering indicators, confidence scoring, risk management, exit strategy, storage, and recovery.

## Customization

### Changing the instrument

Edit `config/settings.py`:

```python
EPIC = "IX.D.NIKKEI.IFM.IP"  # Change to your IG epic
CONTRACT_SIZE = 1              # Dollars per point
MARGIN_FACTOR = 0.005          # Check IG's margin requirement
```

### Adjusting risk parameters

All in `config/settings.py`:

```python
DEFAULT_SL_DISTANCE = 150      # Stop loss in points
DEFAULT_TP_DISTANCE = 400      # Take profit in points
MIN_RR_RATIO = 1.5             # Minimum risk:reward
MAX_MARGIN_PERCENT = 0.50      # Max margin as % of balance
MIN_CONFIDENCE = 70            # LONG confidence floor
MIN_CONFIDENCE_SHORT = 75      # SHORT confidence floor
```

### Adding setup types

Setup detection is in `core/indicators.py` in the `detect_setup()` function. Each setup type checks price position relative to technical levels (BB bands, EMA, RSI) and returns a dict with `found`, `type`, `reasoning`, `sl`, `tp`.

### Modifying AI prompts

The system prompt is in `ai/analyzer.py` in `build_system_prompt()`. The scan prompt is built by `build_scan_prompt()`. Both are plain text -- edit directly.

### Adding confidence criteria

Criteria are in `core/confidence.py` in `compute_confidence()`. Each criterion is a function that returns True/False. The scoring formula automatically adjusts when criteria are added or removed.

## Deployment

For production deployment on Oracle Cloud (free tier), see the full guide: **[DEPLOY.md](DEPLOY.md)**

Quick version:
1. Create an Oracle Cloud Always Free VM (ARM, 1 OCPU, 6GB RAM)
2. Clone repo, install Python 3.10+, configure `.env`
3. Run `./setup.sh` to verify
4. Set up systemd service for auto-start and crash recovery

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Areas where contributions are especially useful:**
- New setup types and entry strategies
- Additional indicators and technical analysis
- Backtesting improvements
- Dashboard UI/UX enhancements
- Documentation and tutorials
- Bug fixes and test coverage

## Safety

- **Always start with a demo account.** Set `IG_ENV=demo` in your `.env`.
- **Start with minimum lot sizes** (0.01-0.02 lots).
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **Confidence floors are intentionally hard-coded.** 70% LONG, 75% SHORT. This protects against overconfidence.
- **The bot will not trade without your confirmation** (unless you enable auto-execute).

## License

MIT License. See [LICENSE](LICENSE) for details.
