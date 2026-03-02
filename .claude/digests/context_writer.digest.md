# ai/context_writer.py — DIGEST (created 2026-03-02)
# Writes human-readable context files to storage/context/ before every AI call.
# Gives Claude Code CLI subprocess richer, auditable context than inline JSON dumps.
# Called by monitor.py immediately before the Haiku pre-gate.

## Public API
write_context(indicators, market_context, web_research, recent_scans, recent_trades,
              live_edge_block="", local_confidence=None, prescreen_direction=None,
              tf_5m=None) -> None
  # Writes 4 files. Non-fatal on error (logs warning, never crashes monitor).
  # tf_5m: optional 5M timeframe data, added to market_snapshot.md via TF_KEYS lookup

## Files Written (storage/context/)
market_snapshot.md  — session, mode, pre-screen setup (incl. Entry TF label), local confidence breakdown,
                      indicators for D1 / 4H / 15M / 5M (all fields, readable format)
recent_activity.md  — last 15 scans (timestamp, session, price, action, conf%)
                      last 10 closed trades (direction, setup, outcome, P&L, duration)
                      win rate summary by setup type
macro.md            — VIX, USD/JPY, news headlines, high-impact calendar events
live_edge.md        — raw live_edge_block string from storage.get_ai_context_block()

## Context dir
CONTEXT_DIR = PROJECT_ROOT / "storage" / "context"
Created automatically. *.md files are gitignored (runtime data).
.gitkeep committed so directory exists after fresh clone.

## Usage in monitor.py
Called after: 4H fetch, web research, local confidence, hard blocks, live_edge
Called before: haiku_result = self.analyzer.precheck_with_haiku(...)
Data sources: self.storage.get_recent_scans(15), self.storage.get_recent_trades(10)
