# storage/scan_analyzer.py — DIGEST
# Updated 2026-03-04

## Purpose
Cron-based scan analyzer. Reads SQLite scans table, classifies each rejection as
`true_missed`, `thank_god`, or `near_miss` by walking the subsequent price sequence
chronologically (SL=150pts vs TP=400pts — first threshold hit wins).
Runs hourly via cron. `--all` flag = full history instead of 24h.

## Key Constants
SL_DISTANCE = 150          # pts — trade stopped out if price moves this far adverse first
TP_DISTANCE = 400          # pts — true profit target (was wrong 150 before 2026-03-04)
NEAR_MISS_THRESHOLD = 300  # pts — almost won
LOOKBACK_HOURS = 24        # default window (--all overrides)
PRICE_SEQUENCE_WINDOW = 120  # minutes ahead to walk

## Core Functions
_get_price_sequence(scans, from_time) -> list[tuple]
  Returns (datetime, price) pairs in chronological order from rejection to +120min.

_classify_trade_outcome(entry_price, direction, price_seq) -> dict
  Walks prices chronologically. First threshold hit wins.
  Returns: outcome, near_miss, max_favorable, max_adverse, adverse_before_tp, time_to_tp_min

_get_intraday_regime(scans, ts) -> str
  Compares rejection price to price 4h earlier. BULL>+300pts, BEAR<-300pts, else NEUTRAL.

_get_session_label(iso_str) -> str
  UTC hour → Tokyo(0-7) / London(7-15) / NY(15-22) / Off

_binomial_significance(true_missed, total, null_rate) -> str
  scipy.stats.binomtest one-sided. Returns p-value string with significance marker.
  Bonferroni note in report header (0.05 / n_gates).

_compute_missed_moves(scans) -> list[dict]
  Per-scan: timestamp, price, rsi, direction, reason, session, regime,
            outcome, near_miss, max_favorable, max_adverse,
            adverse_before_tp, time_to_tp_min, is_missed (alias)

_build_session_summary, _build_regime_summary, _build_reason_summary, _build_rsi_buckets
  Aggregate by session / regime / reason / RSI bucket.

generate_report(scans) -> str
  Sections: Last 2h | True Missed | Near Miss | Thank God | Session Breakdown |
  Rejection Pattern (with p-values + Bonferroni) | RSI buckets |
  Regime × Outcome (conditional probability) | Per-Day (with EXTREME DAY flag) | Totals

## Outputs
storage/data/scan_analysis.md   (overwritten each run)
storage/data/scan_analysis.log  (appended one-line summary per run)

## Cron
0 * * * * cd /home/ubuntu/japan225-bot && venv/bin/python -m storage.scan_analyzer >> /dev/null 2>&1 && venv/bin/python -m storage.probability_tracker >> /dev/null 2>&1

## Usage
python -m storage.scan_analyzer          # last 24h
python -m storage.scan_analyzer --all    # full history

## Statistical findings as of 2026-03-04 (3 days, not yet actionable)
- RSI gate: p=0.000 SIGNIFICANT (only gate that survives Bonferroni)
- All other gates: p > 0.20 (noise — do not change)
- Normal day (Mar 2): 6% miss rate. Extreme crash day inflates average to 20-24%.
- RSI 55-65 LONG London: Net +18 (30 true_missed vs 12 thank_god) — but same RSI in Tokyo during crash saved account
- ACTION: Do nothing until 20+ days AND p < 0.008
