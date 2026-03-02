# storage/scan_analyzer.py — DIGEST
# Purpose: Cron-based scan analysis. Tracks missed moves (rejections where price moved 150+pts).
# Runs every 2 hours via crontab. Reads SQLite scans table (read-only), writes reports.

## Entry point
run()  # Main. Connects to DB, fetches 24h scans, generates report, writes files.

## Output files
- storage/data/scan_analysis.md  — full report (overwritten each run)
- storage/data/scan_analysis.log — one-line append per run (historical tracking)

## Key functions
_get_scans(conn, hours=24) -> list[dict]          # Fetch scans from last N hours
_parse_rejection_reason(scan) -> str               # Classify rejection: RSI out, BB far, bounce=NO, AI rejected, etc.
_extract_rsi(scan) -> float|None                   # Parse RSI from reasoning string
_infer_expected_direction(scan) -> str|None         # LONG/SHORT from action_taken or Daily=bullish/bearish
_find_price_after(scans, time, minutes) -> float    # Find price N minutes after rejection (from later scans)
_compute_missed_moves(scans) -> list[dict]          # For each rejection: compute price movement in expected direction
_build_reason_summary(missed_data) -> dict          # Aggregate stats by rejection reason
_build_rsi_buckets(missed_data) -> dict             # Bucket rejections by RSI range
generate_report(scans) -> str                       # Full markdown report

## Report sections
1. Last 2 Hours — scan counts, biggest missed LONG/SHORT
2. Missed Moves table — top 20 rejections where price moved 150+pts in expected direction
3. Rejection Pattern Summary — count, avg move, action-needed flag per reason
4. RSI at Rejection — bucketed by RSI range, avg move, should-trade flag
5. 24h Totals

## Thresholds
MISSED_MOVE_THRESHOLD = 150pts
LOOKBACK_HOURS = 24
PRICE_COMPARE_WINDOWS = 30min, 1hr, 2hr

## Cron
0 */2 * * * cd /home/ubuntu/japan225-bot && venv/bin/python -m storage.scan_analyzer
