# ai/analyzer.py — DIGEST (updated 2026-03-02)
# Single-subprocess pipeline: Sonnet 4.6 with Opus sub-agent for borderline calls.
# Haiku pre-gate REMOVED. Separate Opus subprocess REMOVED. All in one `claude` invocation.
# AUTH: Claude Code CLI subprocess (OAuth/subscription). No ANTHROPIC_API_KEY used.
# JSON output via prompt schema + regex parser. No tool use schemas.
# Context files written to storage/context/ by ai/context_writer.py before each call.

## Models (subscription billing = $0/call)
SONNET_MODEL = "claude-sonnet-4-6"  (adaptive thinking on by default)
OPUS_MODEL   = "claude-opus-4-6"    (used as sub-agent via --agents flag, not a separate subprocess)
_cost always 0.0. _tokens always zeros. Kept for interface compat with save_scan().

## class AIAnalyzer
__init__(): total_cost=0.0 (subscription, always zero)

_run_claude(model, system_prompt, user_prompt, timeout=180) -> str
  # subprocess.run([CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions", "--agents", agents_json])
  # agents_json defines "opus_reviewer" sub-agent (model=OPUS_MODEL)
  # Sonnet can spawn this sub-agent internally for borderline 72-86% calls
  # Strips ANTHROPIC_API_KEY from env to force OAuth.
  # Returns stdout string. Returns "" on timeout or binary not found.

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None, live_edge_block=None,
                 failed_criteria=None) -> dict
  # Only public analysis method. Calls _analyze() with SONNET_MODEL.
  # Sonnet handles everything: analysis, quick-reject, and Opus delegation when needed.

_analyze(model, indicators, recent_scans, market_context, web_research,
         prescreen_direction=None, local_confidence=None, live_edge_block=None,
         failed_criteria=None) -> dict
  # Builds prompt via build_scan_prompt() + JSON schema trailer
  # Calls _run_claude() → _parse_json() → returns dict + _model, _cost, _tokens
  # Safe default on parse failure: {setup_found: False, confidence: 0, ...}

## _parse_json(text, default) -> dict
Tries fenced ```json...``` block first, then any {…} in text, then returns default.
Logs warning if parse fails.

## build_system_prompt() -> str
Compact reference card. ~280 tokens. Includes HA, FVG, Fibonacci, sweep signal guidance.
VWAP guidance: above=premium (SHORT), below=discount (LONG). PDH/PDL.
11-criteria confidence breakdown. Quick-reject guidance for junk setups.
OPUS REVIEW instruction: spawn opus_reviewer agent for borderline 72-86% confidence.
Passed as <system> block in _run_claude.

## build_scan_prompt(..., failed_criteria=None) -> str
Compact format. Appends JSON schema template at end.
Includes path to storage/context/ files as a note.
PRE-SCREEN line includes `Entry TF: {entry_tf}`.
failed_criteria → FAILED LOCAL CRITERIA block.
Role block: 5-step analysis (structure → quality → risk → edge → opus review if borderline).
Key formatters:
  _fmt_indicators(indicators)   → pipe-format table (HA, FVG, fib_near, sweep, VWAP, PDH/PDL)
  _fmt_recent_scans(scans)      → 1-line per scan summary
  _fmt_web_research(web)        → 3 lines, HIGH-impact calendar only

## load_prompt_learnings(data_dir=None) -> str
Reads storage/data/prompt_learnings.json. Returns compact block (last 5 entries).

## post_trade_analysis(trade, ai_analysis, data_dir=None) -> None
Rule-based (no LLM cost). Updates prompt_learnings.json (max 20 entries).
Also computes Brier score in storage/data/brier_scores.json (last 100 trades).

## class WebResearcher
research() -> dict  # Synchronous/blocking. Run via run_in_executor.
Returns: {timestamp, nikkei_news, economic_calendar, vix, usd_jpy, fear_greed}
