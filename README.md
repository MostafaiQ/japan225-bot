# Japan 225 Trading Bot

A fully autonomous AI-powered trading system for Japan 225 (Nikkei) Cash CFD on IG Markets. Uses a 2-tier Claude AI pipeline (Sonnet + Opus) for market analysis, auto-executes trades when confidence thresholds are met, and lets the broker enforce stops and targets directly.

## How It Works

```
Every 5 minutes during active sessions (Tokyo / London / New York):

  Fetch candles (15M + 5M parallel, 4H + Daily sequential)
       |
  Detect setup — BIDIRECTIONAL (LONG and SHORT independently)
  Session-specific setups: tokyo_gap_fill (00-02 UTC), london_orb (08-10 UTC)
       |
  Score confidence (9-criteria weighted scoring, 0-100%)
       |
  [Both dirs >= 80% and gap <= 5%] -----> Skip (contradictory signal, no edge)
       |
  [Below 60%] -----> Skip
       |
  Sonnet 4.6 analysis
  (full indicators: D1 + 4H + 15M + 5M, Wyckoff/SMC/VP framework,
   portfolio state, USD/JPY direction, calendar events)
       |
  [Approved, conf >= 70% LONG / 75% SHORT] --> 12-point risk validation --> Auto-execute
       |
  [Rejected + opposite direction has setup + local conf >= 60%]
       |
  Opus 4.6 evaluates opposite direction as swing trade
       |
  [Approved, >= 70% / 75%] --> Risk validation --> Auto-execute
       |
  [Rejected] --> Skip

  Open position: monitored every 2s via Lightstreamer real-time ticks.
  SL and TP enforced directly by broker. Opus position evaluator every 2min.
```

## Features

- **Fully autonomous execution** — auto-executes when AI confidence meets thresholds (70% LONG, 75% SHORT)
- **2-tier sequential AI pipeline** — Sonnet analyzes first; if rejected and opposite direction has a setup, Opus evaluates as a full swing trade
- **Bidirectional scanning** — evaluates both LONG and SHORT setups every 5 minutes, independently
- **9-criteria weighted confidence scoring** — filters technical noise before AI evaluation
- **Session-specific setups** — `tokyo_gap_fill` (gap fills at Tokyo open) and `london_orb` (Asia range breakouts at London open)
- **Contradictory signal gate** — skips when both directions score >= 80% with gap <= 5% (no clear edge)
- **Wyckoff / SMC / Volume Profile AI framework** — AI reasons about market phase, liquidity sweeps, order blocks, FVGs
- **AI portfolio context** — AI sees open positions, directions, and daily P&L before approving new trades
- **ATR-based dynamic SL/TP** — SL floored at 60pts (tight scalp), TP floored at 250pts. Risk-based lot sizing (2% of balance)
- **Real-time Lightstreamer streaming** — price ticks (BID/OFR mid) during monitoring; REST fallback on disconnect
- **Extreme day detection** — crash/rally days (range > 1000pts) require 85% confidence
- **12-point risk validation** — portfolio risk cap, dollar risk, margin cap, position count, drawdown protection
- **SL/TP verification** — verifies IG set stops after order; auto-repairs if missing
- **Prompt learnings feedback loop** — post-trade rules written to `prompt_learnings.json`; Brier score calibration tracking
- **Telegram notifications** — trade alerts with entry/SL/TP, position management, full command set
- **Web dashboard** — real-time monitoring, config, trade history, logs, Claude chat assistant
- **428 tests passing** — indicators, confidence, risk, exit, storage, streaming, recovery

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

### LONG (Mean-Reversion)
| Setup | Trigger | RSI |
|-------|---------|-----|
| `bollinger_mid_bounce` | ±80pts from BB mid, bounce confirm | 30-55 |
| `oversold_reversal` | RSI < 30 + daily bullish + reversal confirm | < 30 |
| `extreme_oversold_reversal` | RSI < 28 + 4H near BB lower | < 28 |

### LONG (Momentum)
| Setup | Trigger | RSI |
|-------|---------|-----|
| `ema9_pullback_long` | ±100pts from EMA9 + above EMA50 + HA bullish | 40-65 |

### SHORT (Mean-Reversion)
| Setup | Trigger | RSI |
|-------|---------|-----|
| `bb_upper_rejection` | ±150pts from BB upper, reversal confirm | 55-75 |
| `overbought_reversal` | RSI > 70 + daily bearish + reversal confirm | > 70 |
| `breakdown_continuation` | Below BB mid + HA ≤ -2 | 25-45 |
| `dead_cat_bounce_short` | Bounce to BB mid/EMA9 in downtrend | 43-62 |
| `bear_flag_breakdown` | Flag pattern in downtrend | 35-52 |
| `high_volume_distribution` | High-vol rejection at BB upper | 55-75 |
| `ema200_rejection` | Rejection at EMA200 in downtrend | 45-65 |
| `lower_lows_bearish` | New lows + HA bearish streak | 30-55 |
| `pivot_r1_rejection` | Rejection at R1 pivot | 50-70 |

### Session-Specific
| Setup | Trigger | Session |
|-------|---------|---------|
| `tokyo_gap_fill` | Overnight gap ≥ 100pts, fill reversal | Tokyo 00-02 UTC |
| `london_orb` | Break above/below Asia range | London 08-10 UTC |

> **Disabled** (below breakeven WR): `breakout_long`, `momentum_continuation_long`, `bollinger_lower_bounce`, `vwap_bounce_long`, `multi_tf_bearish`, `momentum_continuation_short`, `bb_mid_rejection`, `ema50_rejection`, `vwap_rejection_short`, `vwap_rejection_short_momentum`, `ema9_pullback_short`

## AI Pipeline

### Sonnet 4.6 (Primary Analyzer)
- System prompt: Wyckoff phase detection, SMC (sweeps/FVGs/order blocks), Volume Profile (POC/VAH/VAL), setup-class rules
- Indicators: D1 + 4H + 15M + 5M — RSI, BB, EMA9/50/200, VWAP, HA streak, FVG, pivot highs/lows, sweeps, ATR
- Context injected: portfolio state (open count + directions + daily P&L), USD/JPY directional hint, calendar events
- Confidence framing: local score is criteria-based (not win probability); historical WR ranges shown explicitly
- Mandatory R:R: `(TP_dist - 7) / (SL_dist + 7) >= 1.5`
- Auto-executes when confidence >= 70% (LONG) or 75% (SHORT)
- Subscription billing ($0/call via Claude Code CLI OAuth)

### Opus 4.6 (Opposite-Direction Swing Evaluator)
- Triggered when Sonnet rejects AND opposite direction has a setup with local conf >= 60%
- Receives Sonnet's rejection reasoning and key levels as context
- Full SL/TP freedom from market structure (no scalp bounds)
- Consistency guard: recent Opus decisions tracked to prevent flip-flopping
- Same confidence thresholds: 70% LONG / 75% SHORT

## Risk Management

All 12 checks must pass before any trade:

| Check | Rule |
|-------|------|
| Confidence | LONG >= 70%, SHORT >= 75% |
| Margin per position | Max 5% of account balance |
| Portfolio risk cap | Total open risk <= 8% of balance |
| Dollar risk | Risk per trade capped at 3% of balance |
| Risk/Reward | >= 1.5:1 after 7pt spread adjustment |
| Max Positions | Up to 3 concurrent open positions |
| Consecutive Losses | 2 losses = 1-hour cooldown |
| Drawdown protection | -10% → halve size; -15% → quarter size; -20% → stop trading |
| Event Blackout | No trades within 60 min of high-impact events |
| Calendar Block | No Friday PPI/CPI/NFP/BOJ, no month-end |
| Extreme Day Gate | Crash/rally days (range > 1000pts) require 85% confidence |
| SL/TP Verified | Post-execution: verify IG set stops, auto-repair if missing |

**Lot sizing:** Risk-based. `lots = (balance × 2%) / (SL_pts × $1/pt)`. ATR-based SL (1.2-1.8× ATR14). SL floored at 60pts.

## Trade Management

SL and TP are set by AI at entry from market structure + ATR. The broker enforces them directly. After entry:
- **Every 2s**: Lightstreamer streaming price tick. Adverse move tiers: 60pts (alert), 120pts (alert), 175pts (severity tier)
- **Every 2 min**: Opus evaluates open position — can recommend `CLOSE_NOW` (auto-executes at >= 70%) or `TIGHTEN_SL` (Telegram alert only)
- No mechanical breakeven lock or trailing stop (removed — Opus does this more intelligently)

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

428 tests covering indicators, confidence scoring, risk management, exit strategy, storage, streaming state machine, and startup recovery.

## Safety

- **Start with a demo account.** Set `IG_ENV=demo` in your `.env`.
- **Start with minimum lot sizes** (0.01-0.02 lots).
- **This is a tool, not financial advice.** You are responsible for all trading decisions.
- **The bot auto-executes trades.** Make sure you understand the risk parameters before going live.

## License

**Proprietary.** All rights reserved. See [LICENSE](LICENSE).

Unauthorized use, reproduction, or distribution is strictly prohibited. Commercial use without written permission will result in legal action.
