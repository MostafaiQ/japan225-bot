# Japan 225 Trading Bot

A fully autonomous AI-powered trading system for Japan 225 (Nikkei) Cash CFD on IG Markets. Uses a 2-tier Claude AI pipeline (Sonnet + Opus) for market analysis, auto-executes trades when confidence thresholds are met, and lets the broker enforce stops and targets directly.

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
  [Both directions >= 80% and gap <= 5%] -----> Skip (contradictory, no edge)
       |
  Sonnet AI analysis (full system prompt + indicators)
       |
  [Approved, conf >= 70% LONG / 75% SHORT] --> Risk validation --> Auto-execute
       |
  [Rejected] --> Is opposite direction setup detected with local conf >= 60%?
       |                         |
  [No] --> Skip           [Yes] --> Opus evaluates opposite direction (full swing trade)
                                         |
                                 [Approved, >= 70% / 75%] --> Risk validation --> Auto-execute
                                         |
                                 [Rejected] --> Skip
```

## Features

- **Fully autonomous execution** -- auto-executes when AI confidence meets thresholds (70% LONG, 75% SHORT)
- **2-tier sequential AI pipeline** -- Sonnet analyzes first; if rejected and opposite direction has a setup, Opus evaluates it as a full swing trade
- **Bidirectional scanning** -- evaluates both LONG and SHORT setups every cycle
- **12-criteria confidence scoring** -- filters noise before AI evaluation
- **Contradictory signal gate** -- skips when both directions score >= 80% with gap <= 5% (no clear edge)
- **Mandatory R:R enforcement** -- AI computes effective R:R (must be >= 1.5 after spread), code double-checks
- **Fixed SL/TP at entry** -- AI determines stop and target from market structure; no mechanical post-entry modifications
- **Real-time Lightstreamer streaming** -- price ticks (BID/OFR mid) during position monitoring; REST fallback on disconnect
- **Proper pivot point detection** -- identifies most recent actual swing highs/lows (3-neighbour confirmation, unlimited lookback)
- **Extreme day detection** -- direction-aware gate on crash/rally days (only counter-trend trades require 85% confidence)
- **Tokyo session volatility mode** -- forces minimum lots (0.01) for the entire Tokyo session; higher loss tolerance
- **ATR-based AI guidance** -- ATR(14) provided to AI on all timeframes; AI widens SL/TP on high-volatility days
- **SL/TP verification** -- verifies IG set stop/limit after order; auto-repairs via modify_position if missing
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
    |     Contradictory signal gate (both >= 80%, gap <= 5% -> skip)
    |     Sonnet analysis -> if rejected, Opus evaluates opposite direction
    |     Risk validation -> Auto-execute
    |
    +-- Monitoring (position open): every 2s
    |     Lightstreamer price ticks (real-time, ~0 REST calls for price)
    |     REST fallback if streaming stale; background reconnect after 60s
    |     Adverse move detection (60/120/175pt tiers); alerts only
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
|   +-- ig_client.py            # IG Markets REST API + Lightstreamer streaming
|   +-- indicators.py           # BB, EMA, RSI, VWAP, Heiken Ashi, FVG, Fib, pivots, ATR, 12 candlestick patterns
|   +-- session.py              # Session hours (Tokyo/London/NY), no-trade days, blackouts
|   +-- momentum.py             # MomentumTracker, adverse move tier detection
|   +-- confidence.py           # 12-criteria proportional scoring (LONG + SHORT aware)
+-- ai/
|   +-- analyzer.py             # Sonnet -> Opus 2-tier pipeline (Claude CLI subprocess)
|   +-- context_writer.py       # Market context file writer (snapshot, macro, live edge)
+-- trading/
|   +-- risk_manager.py         # 11-point pre-trade validation (both pipelines)
|   +-- exit_manager.py         # Position phase tracking
+-- notifications/
|   +-- telegram_bot.py         # Alerts, inline buttons, trade confirmation
+-- storage/
|   +-- database.py             # SQLite WAL-mode persistent state
|   +-- scan_analyzer.py        # Cron-based missed-move tracker
|   +-- data/                   # Runtime data (never committed)
+-- dashboard/                  # FastAPI web dashboard + ngrok tunnel
+-- backtest.py                 # Strategy backtester with real AI evaluation
+-- tests/                      # 395+ tests (all passing)
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
- Full system prompt with setup-specific rules (bounce, breakdown, momentum, crash/rally day rules)
- 50+ indicators across D1, 4H, 15M, 5M timeframes including ATR, pivot levels, FVG, Heiken Ashi
- Mandatory R:R computation: `(TP_dist - 7) / (SL_dist + 7) >= 1.5`
- Confidence breakdown across 12 criteria
- Opus sub-agent available for borderline setups (60-86% local confidence)
- Auto-executes when confidence >= 70% (LONG) or 75% (SHORT)

### Opus (Opposite-Direction Swing Evaluator)
- Runs only when Sonnet rejects AND opposite direction has a detected setup with local conf >= 60%
- Evaluates ONLY the opposite direction as a full swing trade (same SL/TP freedom as Sonnet)
- Receives Sonnet's rejection reasoning and key levels as context
- Same confidence thresholds: 70% LONG / 75% SHORT
- Consistency guard: recent Opus decisions tracked to prevent direction flip-flopping
- Full risk validation before execution

## Risk Management

All 11 checks must pass before any trade (Sonnet or Opus):

| Check | Rule |
|-------|------|
| Confidence | LONG >= 70%, SHORT >= 75% |
| Margin | Must not exceed 50% of account balance |
| Risk/Reward | >= 1.5:1 after 7pt spread adjustment |
| Max Positions | 1 open position at a time |
| Consecutive Losses | 2 losses = 1-hour cooldown (bypassed at high confidence) |
| Daily Loss Limit | Configurable % of balance |
| Event Blackout | No trades within 60 min of high-impact events |
| Calendar Block | No Friday PPI/CPI/NFP/BOJ, no month-end |
| Extreme Day Gate | Counter-trend trades require 85% confidence on crash/rally days |
| System Active | System must not be paused |
| SL/TP Verified | Post-execution: verify IG set stops, auto-repair if missing |

## Trade Management

SL and TP are determined by AI at entry based on market structure and ATR volatility. The broker enforces them directly. No mechanical post-entry modifications (no breakeven lock, no trailing stop).

## Active Sessions (UTC)

| Hours | Session |
|-------|---------|
| 00:00-06:00 | Tokyo (volatile; minimum lots enforced) |
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

395+ tests covering indicators, confidence scoring, risk management, exit strategy, storage, and recovery.

## Safety

- **Start with a demo account.** Set `IG_ENV=demo` in your `.env`.
- **Start with minimum lot sizes** (0.01-0.02 lots).
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **The bot auto-executes trades.** Make sure you understand the risk parameters before going live.

## License

**Proprietary.** All rights reserved. See [LICENSE](LICENSE).

Unauthorized use, reproduction, or distribution is strictly prohibited. Commercial use without written permission will result in legal action.
