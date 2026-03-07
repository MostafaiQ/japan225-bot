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

_run_claude(model, system_prompt, user_prompt, timeout=180) -> (str, dict)
  # subprocess.run([CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions",
  #   "--no-session-persistence", "--effort", "low", "--tools", ""])
  # --effort low: disables adaptive thinking. --tools "": no file access, pure prompt analysis.
  # NOTE: --fast and --max-tokens DO NOT EXIST as CLI flags (tested v2.1.63).
  # No --agents flag. Opus reviewer sub-agent REMOVED (was unused, logged "NOT loaded" every scan).
  # Strips ANTHROPIC_API_KEY from env to force OAuth.
  # Returns (stdout_string, token_estimates). Returns ("", zeros) on timeout or binary not found.

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None, live_edge_block=None,
                 failed_criteria=None, open_positions_context=None) -> dict
  # Only public analysis method. Calls _analyze() with SONNET_MODEL.
  # open_positions_context: {"count": N, "directions": [...], "daily_pnl": X}
  # Sonnet handles everything: analysis, quick-reject, and Opus delegation when needed.

_analyze(model, indicators, recent_scans, market_context, web_research,
         prescreen_direction=None, local_confidence=None, live_edge_block=None,
         failed_criteria=None, open_positions_context=None) -> dict
  # Builds prompt via build_scan_prompt() + JSON schema trailer
  # Parse failure auto-retry: on first parse failure, retries once with effort="medium"
  # Calls _run_claude() → _parse_json() → returns dict + _model, _cost, _tokens
  # Safe default on parse failure after retry: {setup_found: False, confidence: 0, ...}

## _parse_json(text, default) -> dict
Tries fenced ```json...``` block first, then any {…} in text, then returns default.
Logs warning if parse fails.

## JSON schema fields (in scan_with_sonnet response)
Standard fields: setup_found, direction, confidence, entry, stop_loss, take_profit, setup_type,
  reasoning, effective_rr, warnings, edge_factors, _model, _cost, _tokens
NEW (2026-03-05): counter_signal (null | "LONG" | "SHORT") — Sonnet sets this when it sees an
  opposite-direction opportunity during evaluation. If counter_signal == opposite_direction AND
  sonnet_conf <= 45%, monitor.py triggers evaluate_opposite() even without a pre-detected setup
  (counter_gate). Fixes cases like 08:05 where Sonnet saw swept_low=bullish reversal during SHORT eval.
  counter_reasoning (null | str) — explanation of why Sonnet flagged the counter-direction.

## build_system_prompt() -> str
REWRITTEN 2026-03-05 (prompt engineering pass). Was ~14,568 chars, now ~9,690 chars (-34%).
Condensed verbose per-setup rule blocks into compact tables + decision tables.
Added NEW framework sections:

WYCKOFF PHASE DETECTION:
  4 phases: Accumulation (Spring=LONG), Markup (LONG preferred), Distribution (UT=SHORT), Markdown (SHORT preferred).
  Coil/slow market: BBW<200 + HA~0 → lower bar for band-edge mean-reversion, pre-breakout pre-positioning.
  Spring = swept_low + quick recovery = strong LONG signal. UpThrust = swept_high + fails = strong SHORT signal.

VOLUME PROFILE USAGE (POC/VAH/VAL):
  POC = equilibrium (wait for break). VAH from below = resistance/SHORT. VAL from above = support/LONG.
  Inside VA = slow mean-reversion. Outside VA = rejection (reversal) or acceptance (continuation).
  LVN = price moves fast. VP edge at slow-day band extremes → lower confidence threshold by 5pts.

SMC (Smart Money Concepts):
  Order Block: last bearish candle before bullish impulse = demand OB (LONG retest). Vice versa = supply OB.
  FVG: fvg_bullish = demand zone. fvg_bearish = supply zone. Soft S/R (fills during reversals).
  Sweeps: swept_low = liquidity grab → bullish. swept_high → bearish. Sweep+OB+FVG = highest conviction.
  BOS vs CHoCH: BOS = trend continuation. CHoCH = reversal (pivot_low broken in uptrend = CHoCH bearish).
  SMC + Wyckoff: Spring = sweep(equal_lows) + bullish_FVG + demand_OB. UT = sweep(equal_highs) + bearish_FVG + supply_OB.

SETUP-CLASS RULES (condensed from verbose sections):
  Mean-reversion longs: bearish HA + below EMA50 = EXPECTED. Approve if daily bullish + reversal signal.
  Mean-reversion shorts: bullish HA + above EMA50 = EXPECTED. Approve if daily bearish + reversal signal.
  Momentum longs: RSI 60-75 = healthy. Above BB mid = expected. Approve if >EMA50+VWAP+HA bull+RSI45-75.
  Momentum shorts: RSI 30-55 = healthy. Daily "bullish" during selloff = expected (EMA lags). Approve if 4H+15M aligned.

HARD PROHIBITIONS (unchanged): oversold shorting (4H RSI<32), overbought longing (4H RSI>68), extreme day rules.
WARNINGS RULE (unchanged): 4+ → <70%, 6+ → <60%.
COUNTER SIGNAL (unchanged): set counter_signal="LONG"/"SHORT" on concrete structural evidence.
Passed as <system> block in _run_claude.

## build_scan_prompt(..., failed_criteria=None) -> str
Compact format. Appends JSON schema template at end.
PRE-SCREEN line includes `Entry TF: {entry_tf}`.
SECONDARY SETUP block: threshold-aware framing (INDEPENDENT CANDIDATE vs context-only).
failed_criteria → FAILED LOCAL CRITERIA block.
MARKET REGIME block: intraday range + crash day flag injected into user prompt.

NEW (2026-03-05 prompt engineering pass):
WYCKOFF/SMC CONTEXT block: pre-computed from live 15M + 4H indicators — injected after MARKET REGIME.
  Fields: Phase hint (Accumulation/Markup/Distribution/Markdown/Coil) | Bias | HA streaks 15M + 4H
           Sweep status | VP position (AT POC / INSIDE VA / ABOVE VAH / BELOW VAL) | BB width with COIL tag
  Phase detection heuristic:
    HA≥3 + above VWAP + above EMA50 → MARKUP / LONG preferred
    HA≤-3 + below VWAP + below EMA50 → MARKDOWN / SHORT preferred
    swept_low + HA≥-1 → ACCUMULATION Spring / LONG signal
    swept_high + HA≤1 → DISTRIBUTION UpThrust / SHORT signal
    BBW<200 + |HA|≤1 → COIL / band-edge mean-reversion or pre-breakout
  VP position computed from volume_poc/volume_vah/volume_val in 15M TF dict.
  Caveat line: "AI: verify phase from full indicator data — hint is heuristic only"

Role block: 7-step analysis (was 5-step):
  1. WYCKOFF PHASE (HA/BB/sweeps/VP) → phase + bias
  2. VOLUME PROFILE (POC/VAH/VAL position)
  3. SMC CONTEXT (sweep + FVG + OB confluence)
  4. STRUCTURE (D1/4H/15M alignment, specific values)
  5. SETUP QUALITY (trigger, distance, volume, HA, FVG)
  6. RISK/EDGE (loss scenario, EV from live stats)
  7. DEVIL'S ADVOCATE (challenge your case; SLOW DAY CHECK: coil → lower bar for band-edge MR)

Key formatters (unchanged):
  _fmt_indicators(indicators)   → pipe-format table with MARKET STRUCTURE block (VP, anchored VWAP, etc.)
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
  # Uses OPUS_MODEL, timeout=150s, effort="low".
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
