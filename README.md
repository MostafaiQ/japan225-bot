# Japan 225 Trading Bot

A fully autonomous AI-powered trading system for Japan 225 (Nikkei) Cash CFD on IG Markets. Uses a 2-tier Claude AI pipeline (Sonnet + Opus) for market analysis, auto-executes trades when confidence thresholds are met, and manages positions with a 3-phase exit strategy.

## How It Works

```
Every 5 minutes during market hours:

  Fetch candles (15M + 5M + 4H + Daily)
       |
  Detect setup (bidirectional: LONG + SHORT simultaneously)
       |
  Score confidence (11 criteria, 0-100%)
       |
  [Below 60%] -----> Skip
       |
  Sonnet AI analysis (full system prompt + indicators)
       |
  [Approved, conf >= 70/75%] -----> Risk validation -----> Auto-execute
       |
  [Rejected, conf >= 50%] -----> Opus scalp evaluation (both directions)
       |                              |
  [Rejected, conf < 50%] -> Skip     |--- Opus conf >= 60% + direction valid
                                      |       -----> Risk validation -----> Auto-execute
                                      |--- Otherwise -> Skip
```

## Features

- **Fully autonomous execution** -- auto-executes when AI confidence meets thresholds (70% LONG, 75% SHORT)
- **2-tier sequential AI pipeline** -- Sonnet analyzes first, Opus evaluates rejected setups for scalp opportunities
- **Bidirectional scanning** -- evaluates both LONG and SHORT setups every cycle
- **11-criteria confidence scoring** -- filters noise before AI evaluation
- **Mandatory R:R enforcement** -- AI computes effective R:R (must be >= 1.5 after spread), code double-checks
- **Direction-flip guards** -- Opus cannot flip bounce setups to SHORT or breakdown setups to LONG
- **SL/TP verification** -- verifies IG set stop/limit after order; auto-repairs via modify_position if missing
- **3-phase exit management** -- Initial SL/TP, breakeven lock at +150pts, trailing runner at 75% TP
- **Telegram notifications** -- trade alerts, position management, full command set
- **Web dashboard** -- real-time monitoring, config, trade history, logs, Claude chat
- **Risk management** -- 11 independent pre-trade checks for both Sonnet and Opus trades
- **Adverse move detection** -- tiered alerts at 60/120/175pts against position
- **High-confidence cooldown bypass** -- local 100%, Sonnet >= 85%, or Opus >= 80% skip loss cooldown
- **Backtest with real AI** -- backtester uses Anthropic API with the same prompts as the live bot

## Architecture

```
Oracle VM (24/7, systemd):
  monitor.py ---- Main process
    |
    +-- Scanning (no position): every 5min active sessions
    |     Fetch 15M+5M parallel, Daily+4H sequential
    |     Bidirectional detect_setup() + compute_confidence()
    |     Sonnet analysis (sequential) -> Opus scalp eval if rejected
    |     Risk validation -> Auto-execute
    |
    +-- Monitoring (position open): every 2s
    |     Price tracking, adverse move detection
    |     Exit manager: Initial -> Breakeven -> Runner phases
    |
    +-- Telegram: always-on polling, commands + alerts
    |
  dashboard/ ---- FastAPI (port 8080) + ngrok tunnel
```

## Project Structure

```
japan225-bot/
+-- monitor.py                  # Main process: scan + monitor + Telegram (systemd)
+-- config/
|   +-- settings.py             # All constants -- single source of truth
+-- core/
|   +-- ig_client.py            # IG Markets REST API (candle caching, delta fetches)
|   +-- indicators.py           # BB, EMA, RSI, VWAP, Heiken Ashi, FVG, Fib, pivots, 12 candlestick patterns
|   +-- session.py              # Session hours (Tokyo/London/NY), no-trade days, blackouts
|   +-- momentum.py             # MomentumTracker, adverse move tier detection
|   +-- confidence.py           # 11-criteria proportional scoring (LONG + SHORT aware)
+-- ai/
|   +-- analyzer.py             # Sonnet -> Opus 2-tier pipeline (Claude CLI subprocess)
|   +-- context_writer.py       # Market context file writer (snapshot, macro, live edge)
+-- trading/
|   +-- risk_manager.py         # 11-point pre-trade validation (both pipelines)
|   +-- exit_manager.py         # 3-phase exit (Initial -> Breakeven -> Runner)
+-- notifications/
|   +-- telegram_bot.py         # Alerts, inline buttons, trade confirmation
+-- storage/
|   +-- database.py             # SQLite WAL-mode persistent state
|   +-- scan_analyzer.py        # Cron-based missed-move tracker
|   +-- data/                   # Runtime data (never committed)
+-- dashboard/                  # FastAPI web dashboard + ngrok tunnel
+-- backtest.py                 # Strategy backtester with real AI evaluation
+-- tests/                      # 338+ tests (all passing)
+-- DEPLOY.md                   # Full deployment guide
```

## Setup Types

### LONG Setups
| Setup | Trigger | RSI Range |
|-------|---------|-----------|
| `bollinger_mid_bounce` | Price near BB mid from below | 30-65 |
| `bollinger_lower_bounce` | Price near BB lower band | 20-40 |
| `ema50_bounce` | Price bouncing off EMA50 | 30-55 |
| `oversold_reversal` | RSI < 30 + daily bullish + reversal confirm | < 30 |
| `extreme_oversold_reversal` | RSI < 28 + 4H near BB lower | < 28 |

### SHORT Setups
| Setup | Trigger | RSI Range |
|-------|---------|-----------|
| `bollinger_upper_rejection` | Price near BB upper band | 55-75 |
| `ema50_rejection` | Price rejected at EMA50 from below | 50-70 |
| `bb_mid_rejection` | Price rejected at BB mid from below | 40-65 |
| `overbought_reversal` | RSI > 70 + daily bearish + reversal confirm | > 70 |
| `breakdown_continuation` | Below BB mid, RSI 25-45, HA bearish | 25-45 |
| `bear_flag_breakdown` | Flag consolidation in downtrend | 35-52 |
| `dead_cat_bounce_short` | Weak bounce to BB mid/EMA9 in downtrend | 43-62 |
| `multi_tf_bearish` | 4+ bearish factors across timeframes | < 48 |
| `high_volume_distribution` | High vol rejection at BB upper | 55-75 |
| `vwap_rejection_short` | Rejection at VWAP in downtrend | 40-60 |

## AI Pipeline

### Sonnet (Primary Analyzer)
- Full system prompt with setup-specific rules (bounce, breakdown, momentum)
- 50+ indicators across D1, 4H, 15M timeframes
- Mandatory R:R computation: `(TP_dist - 7) / (SL_dist + 7) >= 1.5`
- Confidence breakdown across 11 criteria
- Auto-executes when confidence >= 70% (LONG) or 75% (SHORT)

### Opus (Scalp Evaluator)
- Runs only when Sonnet rejects with confidence >= 50%
- Evaluates BOTH directions for scalp opportunities
- Gets Sonnet's full reasoning as context
- Structure-based SL (60-120pts) and TP (150-300pts)
- Direction-flip guards: cannot short a bounce setup or long a breakdown setup
- Minimum 60% confidence required to execute
- Full risk validation before execution

## Risk Management

All 11 checks must pass before any trade (Sonnet or Opus):

| Check | Rule |
|-------|------|
| Confidence | LONG >= 70%, SHORT >= 75% (Sonnet) |
| Margin | Must not exceed 50% of account balance |
| Risk/Reward | >= 1.5:1 after 7pt spread adjustment |
| Max Positions | 1 open position at a time |
| Consecutive Losses | 2 losses = 1-hour cooldown (bypassed at high confidence) |
| Daily Loss Limit | Configurable % of balance |
| Event Blackout | No trades within 60 min of high-impact events |
| Calendar Block | No Friday PPI/CPI/NFP/BOJ, no month-end |
| System Active | System must not be paused |
| SL/TP Verified | Post-execution: verify IG set stops, auto-repair if missing |

## Exit Strategy (3 Phases)

| Phase | Trigger | Action |
|-------|---------|--------|
| Initial | Trade opened | SL at 150pts, TP at 400pts |
| Breakeven | +150pts profit | Move SL to entry + 10pt buffer |
| Runner | 75% of TP in < 2hrs | Remove TP, trailing stop at 150pts |

## Active Sessions (UTC)

| Hours | Session |
|-------|---------|
| 00:00-06:00 | Tokyo |
| 08:00-16:00 | London |
| 16:00-21:00 | New York |

Monday-Friday only. No trading on US/JP holidays.

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
| `/kill` | Emergency close (immediate) |

## Testing

```bash
python3 -m pytest tests/ -v
```

338+ tests covering indicators, confidence scoring, risk management, exit strategy, storage, and recovery.

## Safety

- **Start with a demo account.** Set `IG_ENV=demo` in your `.env`.
- **Start with minimum lot sizes** (0.01-0.02 lots).
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **The bot auto-executes trades.** Make sure you understand the risk parameters before going live.

## License

**Proprietary.** All rights reserved. See [LICENSE](LICENSE).

Unauthorized use, reproduction, or distribution is strictly prohibited. Commercial use without written permission will result in legal action.
