# ai/analyzer.py — DIGEST (updated 2026-03-05)
# Single-subprocess pipeline: Sonnet 4.6 primary, Opus 4.6 sub-agent for borderline/oversold setups.
# --effort low on ALL CLI calls (disables adaptive thinking: 105s→9s, quality unchanged for JSON).
# Haiku pre-gate REMOVED. Separate Opus subprocess REMOVED. All in one `claude` invocation with --agents flag.
# AUTH: Claude Code CLI subprocess (OAuth/subscription). No ANTHROPIC_API_KEY used.
# JSON output via prompt schema + regex parser. No tool use schemas.
# Context data inlined into prompt (recent trades, Fear & Greed). context_writer.py no longer called.

## Models (subscription billing = $0/call)
SONNET_MODEL = "claude-sonnet-4-6"  (adaptive thinking on by default)
OPUS_MODEL   = "claude-opus-4-6"    (used as sub-agent via --agents flag, not a separate subprocess)
_cost always 0.0. _tokens always zeros. Kept for interface compat with save_scan().

## class AIAnalyzer
__init__(): total_cost=0.0 (subscription, always zero)

_run_claude(model, system_prompt, user_prompt, use_opus_agent=False, timeout=180) -> (str, dict)
  # subprocess.run([CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions",
  #   "--no-session-persistence", "--effort", "low", "--tools", "", ...agents_json...])
  # --effort low: disables adaptive thinking. --tools "": no file access, pure prompt analysis.
  # NOTE: --fast and --max-tokens DO NOT EXIST as CLI flags (tested v2.1.63).
  # If use_opus_agent=True: agents_json defines "opus_reviewer" sub-agent (model=OPUS_MODEL)
  # If use_opus_agent=False: no --agents flag, standard Sonnet run only
  # Strips ANTHROPIC_API_KEY from env to force OAuth.
  # Returns (stdout_string, token_estimates). Returns ("", zeros) on timeout or binary not found.

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None, live_edge_block=None,
                 failed_criteria=None) -> dict
  # Only public analysis method. Calls _analyze() with SONNET_MODEL.
  # Sonnet handles everything: analysis, quick-reject, and Opus delegation when needed.

_analyze(model, indicators, recent_scans, market_context, web_research,
         prescreen_direction=None, local_confidence=None, live_edge_block=None,
         failed_criteria=None) -> dict
  # Builds prompt via build_scan_prompt() + JSON schema trailer
  # Conditional Opus: if local_confidence in 60-86% range → use_opus_agent=True, calls _run_claude with Opus
  # Parse failure auto-retry: on first parse failure, retries once without Opus (use_opus_agent=False)
  # Calls _run_claude() → _parse_json() → returns dict + _model, _cost, _tokens
  # Safe default on parse failure after retry: {setup_found: False, confidence: 0, ...}

## _parse_json(text, default) -> dict
Tries fenced ```json...``` block first, then any {…} in text, then returns default.
Logs warning if parse fails.

## build_system_prompt() -> str
Compact reference card. ~350 tokens. Includes HA, FVG, Fibonacci, sweep signal guidance.
VWAP guidance: above=premium (SHORT), below=discount (LONG). PDH/PDL.
11-criteria confidence breakdown. Quick-reject guidance for junk setups.
NEW: EXTREME DAY RULES section — bidirectional: crash day (bearish) + bull day (bullish).
  Crash: prohibits shorting into oversold 4H<32, prohibits LONG on single 15M candle.
  Bull: prohibits LONG into overbought 4H>68, prohibits SHORT on single 15M candle.
  MARKET REGIME block detects direction (price vs midpoint of range).
NEW: OVERSOLD SHORTING PROHIBITION — 4H RSI<32 + exhaustion = REJECT SHORT.
NEW: OVERBOUGHT LONGING PROHIBITION — 4H RSI>68 + exhaustion = REJECT LONG.
NEW: WARNING SEVERITY RULE — 4+ warnings → <70%, 6+ warnings → <60%.
NEW: MEAN-REVERSION BOUNCE RULES section:
  - bb_lower_bounce: ±150pts from lower band, RSI 20-40, reversal confirms on wick/HA/candle/sweep.
  - oversold_reversal: RSI<30 + daily bullish + reversal confirm.
  Both: expect to fail C5/C10/C11 (EMA50 gates relaxed for oversold).
NEW: BREAKDOWN/MOMENTUM SHORT RULES section (added 2026-03-03):
  - breakdown_continuation, bear_flag_breakdown, multi_tf_bearish: daily bullish = EXPECTED during
    transition. AI instructed to evaluate on 4H/15M structure, NOT daily trend. Default APPROVE
    if HA streak ≤-2 and below EMA50. Reject only on oversold bounce risk, major support, LOW volume.
Opus review instructions simplified for oversold setups (focus on reversal signals).
Passed as <system> block in _run_claude.

## build_scan_prompt(..., failed_criteria=None) -> str
Compact format. Appends JSON schema template at end.
Dead context_note removed (was telling AI about files it can't access since --tools "" disables all tools).
PRE-SCREEN line includes `Entry TF: {entry_tf}`.
SECONDARY SETUP block: shown when bidirectional scan finds both directions. Includes direction, type, conf, reasoning.
  Threshold-aware framing: if secondary meets its threshold (≥70% LONG / ≥75% SHORT) → "INDEPENDENT CANDIDATE: evaluate as primary trade."
  If below threshold → "context only, do not execute independently."
failed_criteria → FAILED LOCAL CRITERIA block.
MARKET REGIME block: intraday range + crash day flag injected into user prompt.
Role block: 5-step analysis (structure → quality → risk → edge → opus review if borderline).
Key formatters:
  _fmt_indicators(indicators)   → pipe-format table (HA, FVG, full fib grid, sweep, VWAP, PDH/PDL, BB width)
                                  TF_KEYS: D1, 4H, 15M, 5M (5M data now formatted for AI).
                                  Full fibonacci: 5 levels (236/382/500/618/786) with distance from price.
                                  BB width: volatility proxy per TF.
  _fmt_recent_scans(scans)      → 1-line per scan summary
  _fmt_web_research(web)        → 3 lines, HIGH-impact calendar only

## evaluate_opposite (NEW 2026-03-05)
evaluate_opposite(indicators, opposite_direction, opposite_local_conf, sonnet_rejection_reasoning,
                  sonnet_key_levels, recent_scans, market_context, web_research,
                  recent_trades=None, live_edge_block=None, recent_opus_decision=None) -> dict
  # Called after Sonnet rejects primary direction. Opus evaluates OPPOSITE direction as a SWING trade.
  # Gate (checked in monitor.py before calling): opposite direction must have a detected setup + local conf >= 60%.
  # Full context (same as Sonnet): all TF indicators, web research, recent trades, Sonnet's rejection reasoning.
  # Sonnet's key_levels (support/resistance) injected for SL/TP placement.
  # Full SL/TP freedom from structure — NO scalp bounds (was 60-120pt / 150-300pt in old evaluate_scalp).
  # Same confidence thresholds: MIN_CONFIDENCE (70%) for LONG, MIN_CONFIDENCE_SHORT (75%) for SHORT.
  # Direction validation: if result direction != opposite_direction → setup_found=False (safety guard).
  # Includes consistency block (recent_opus_decision) to prevent flip-flopping.
  # Uses OPUS_MODEL, timeout=150s, effort="medium".
  # Returns: {setup_found, direction, confidence, entry, stop_loss, take_profit, setup_type, reasoning, effective_rr, warnings, edge_factors}

## evaluate_scalp (RETAINED for momentum bypass only)
evaluate_scalp(indicators, primary_direction, setup_type, local_confidence, ai_confidence, ai_reasoning,
               recent_opus_decision=None) -> dict
  # BIDIRECTIONAL Opus scalp. Called ONLY by momentum bypass path (no formal setup detected).
  # NOT called after Sonnet rejection anymore (replaced by evaluate_opposite).
  # SL (60-120pts), TP (150-300pts), clamped. Enforces R:R >= 1.5 after spread.
  # Returns: {scalp_viable: bool, direction: str, tp_distance: int, sl_distance: int, effective_rr: float, reasoning: str, confidence: int}

## load_prompt_learnings(data_dir=None) -> str
Reads storage/data/prompt_learnings.json. Returns compact block (last 5 entries).

## post_trade_analysis(trade, ai_analysis, data_dir=None) -> None
Rule-based (no LLM cost). Updates prompt_learnings.json (max 20 entries).
Also computes Brier score in storage/data/brier_scores.json (last 100 trades).

## class WebResearcher
research() -> dict  # Synchronous/blocking. Run via run_in_executor.
Returns: {timestamp, nikkei_news, economic_calendar, vix, usd_jpy, fear_greed}

_get_nikkei_news() -> list[str]
  # Google News RSS feed: "Nikkei 225 OR Japan economy OR BOJ"
  # Returns top 5 headlines with timestamps

_get_calendar() -> list[dict]
  # nager.date API for JP public holidays and key economic events
  # Filters for HIGH-impact only (BOJ, NFP, CPI, PPI, etc.)

_get_fear_greed() -> float|None
  # CNN Fear & Greed Index API. Returns index value or None if unavailable.
