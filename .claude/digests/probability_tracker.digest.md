# storage/probability_tracker.py — DIGEST
# Created 2026-03-04

## Purpose
Conditional probability tracker. Applies quant principles from literature:
- Conditional probability P(win | session, direction, confidence) — not raw win rate
- Wilson 95% confidence intervals — honest uncertainty quantification
- Kelly quarter-fraction — safe position sizing given current evidence
- Sample size warnings — prevents acting on insufficient data

## Key Constants
MIN_TRADES_FOR_ESTIMATE = 10  # minimum per bucket before reporting
MAX_KELLY_FRACTION = 0.25     # safety cap on Kelly output

## Core Functions
_load_closed_trades(conn) -> list[dict]
  Fetches trades WHERE result IN ('TP_HIT','SL_HIT','BREAKEVEN','TRAILING_STOP')
  Returns: direction, session, setup_type, confidence, pnl, result, entry_price, etc.

_confidence_tier(confidence) -> str
  high (85+) / mid (75-84) / low (65-74) / below-threshold (<65)

compute_conditionals(trades) -> dict
  Groups by (session, direction, confidence_tier).
  Per bucket: wins, losses, win_rate, Wilson 95% CI [lo, hi], avg_win_pnl, avg_loss_pnl,
  kelly_quarter (0.25 × Kelly formula), reliable (n >= MIN_TRADES_FOR_ESTIMATE)

_kelly_fraction(win_rate, avg_win, avg_loss) -> float
  f* = (p*b - q) / b, b = avg_win/avg_loss. Quarter-Kelly. Capped at 0.25. Returns 0 if negative.

_wilson_interval(wins, n, z=1.96) -> (lo, hi)
  Wilson score interval. More accurate than normal approximation for small n.

generate_report(trades, conditionals) -> str
  Header: sample size warning + overall win rate
  Table: session × direction × confidence → n, win_rate, 95% CI, avg win/loss, Kelly¼, reliable?
  Estimation error context section (what sample sizes mean in practice)

## Outputs
storage/data/probability_tracker.md    (overwritten hourly)
storage/data/probability_tracker.json  (machine-readable, for dashboard)

## Current state (2026-03-04)
Only 2 clean trades (TP_HIT or SL_HIT). Wilson CI spans 0-100% — completely unreliable.
Kelly = 0 for all buckets. This is the CORRECT output — demonstrates estimation error.
Useful at 50+ clean trades per bucket. Run for months before trusting numbers.

## Cron
Runs alongside scan_analyzer, hourly.
