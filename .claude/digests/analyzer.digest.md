# ai/analyzer.py — DIGEST (updated 2026-03-02)
# 3-tier AI pipeline: Haiku pre-gate → Sonnet scan → Opus confirm.
# AUTH: Claude Code CLI subprocess (OAuth/subscription). No ANTHROPIC_API_KEY used.
# JSON output via prompt schema + regex parser. No tool use schemas.
# Context files written to storage/context/ by ai/context_writer.py before each call.

## Models (all via CLI --model flag, subscription billing = $0/call)
HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-5-20250929"
OPUS_MODEL   = "claude-opus-4-6"
_cost always 0.0. _tokens always zeros. Kept for interface compat with save_scan().

## class AIAnalyzer
__init__(): total_cost=0.0 (subscription, always zero)

_run_claude(model, system_prompt, user_prompt, timeout=180) -> str
  # subprocess.run([CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions"])
  # Strips ANTHROPIC_API_KEY from env to force OAuth.
  # Returns stdout string. Returns "" on timeout or binary not found.

precheck_with_haiku(setup_type, direction, rsi_15m, volume_signal, session,
                    live_edge_block=None, local_confidence=None, web_research=None,
                    failed_criteria=None, indicators=None) -> dict
  # Returns {should_escalate, reason, _cost}
  # Default on parse error: {should_escalate: True, reason: "Haiku parse error..."} — safe fail-open

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None, live_edge_block=None) -> dict

confirm_with_opus(indicators, recent_scans, market_context, web_research,
                  sonnet_analysis, live_edge_block=None) -> dict
  # Calls _should_call_opus() first — may return Sonnet result directly (opus_skipped=True)

_analyze(model, indicators, recent_scans, market_context, web_research,
         prescreen_direction=None, local_confidence=None, live_edge_block=None,
         is_opus=False, sonnet_analysis=None) -> dict
  # Builds prompt via build_scan_prompt() + JSON schema trailer
  # Calls _run_claude() → _parse_json() → returns dict + _model, _cost, _tokens
  # Safe default on parse failure: {setup_found: False, confidence: 0, ...}

## _parse_json(text, default) -> dict
Tries fenced ```json...``` block first, then any {…} in text, then returns default.
Logs warning if parse fails.

## _should_call_opus(sonnet_confidence, direction) -> bool
Skip Opus if confidence >= 87% (certain) or <= threshold+2 (near-reject floor).

## build_system_prompt() -> str
Compact reference card. ~195 tokens. Passed as <system> block in _run_claude.

## build_scan_prompt(...) -> str
Same compact format as before. Appends JSON schema template at end for model output.
Includes path to storage/context/ files as a note (Claude can read them if needed).
Key formatters:
  _fmt_indicators(indicators)   → pipe-format table
  _fmt_recent_scans(scans)      → 1-line per scan summary
  _fmt_web_research(web)        → 3 lines, HIGH-impact calendar only

## load_prompt_learnings(data_dir=None) -> str
Reads storage/data/prompt_learnings.json. Returns compact block (last 5 entries).

## post_trade_analysis(trade, ai_analysis, data_dir=None) -> None
Rule-based (no LLM cost). Updates prompt_learnings.json (max 20 entries).

## class WebResearcher
research() -> dict  # Synchronous/blocking. Run via run_in_executor in async context.
Returns: {timestamp, nikkei_news, economic_calendar, vix, usd_jpy, fear_greed}
usd_jpy: from frankfurter.app (free). vix: from yfinance ^VIX.
