# ai/analyzer.py — DIGEST (updated 2026-03-01)
# 3-tier AI pipeline: Haiku pre-gate → Sonnet scan → Opus confirm.
# Tool use enforces structured output (zero parse failures).
# Prompt caching (cache_control ephemeral) on system prompt for Sonnet→Opus pairs.

## Models & Pricing
HAIKU_MODEL  = "claude-haiku-4-5-20251001"   $0.80/$4.00 per million tokens
SONNET_MODEL = "claude-sonnet-4-5-20250929"  $3.00/$15.00 per million tokens
OPUS_MODEL   = "claude-opus-4-6"             $15.00/$75.00 per million tokens  ← CORRECTED
Monthly target: ~$3-5 (after all optimizations)

## Tool Schemas
TRADE_ANALYSIS_TOOL: submit_trade_analysis — full trade decision schema (Sonnet + Opus)
  Required: setup_found, confidence, reasoning, confidence_breakdown (8 booleans)
  Optional: direction, entry, stop_loss, take_profit, setup_type, key_levels, warnings, edge_factors

HAIKU_GATE_TOOL: submit_precheck — {should_escalate: bool, reason: str}

## class AIAnalyzer
__init__(): creates Anthropic client, total_cost counter

precheck_with_haiku(setup_type, direction, rsi_15m, volume_signal, session,
                    live_edge_block=None, local_confidence=None) -> dict
  # ~$0.0013/call. Slim context. Filters obvious rejects before Sonnet.
  # Returns {should_escalate, reason, _cost}
  # Default: escalate=True on any error (fail-safe)

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None, live_edge_block=None) -> dict
  # Tool use. Prompt cached. Compact pipe-format inputs. ~$0.004/call after slimming.

confirm_with_opus(indicators, recent_scans, market_context, web_research,
                  sonnet_analysis, live_edge_block=None) -> dict
  # Calls _should_call_opus() first — may return Sonnet result directly (opus_skipped=True)
  # Devil's advocate framing. Prompt caching hits Sonnet's cache if called within 5 min.
  # ~$0.010/call (conditional — skipped if Sonnet >=87% or <=threshold+2)

_analyze(model, indicators, recent_scans, market_context, web_research,
         prescreen_direction=None, local_confidence=None, live_edge_block=None,
         is_opus=False, sonnet_analysis=None) -> dict
  # Low-level. Tool use. cache_control ephemeral on system prompt.
  # Parses block.input from tool_use response — no json.loads needed.
  # Returns dict + _model, _cost, _tokens{input,output,cache_read,cache_write}

## _should_call_opus(sonnet_confidence, direction) -> bool
Skip Opus if confidence >= 87% (certain) or <= threshold+2 (near-reject floor).

## build_system_prompt() -> str
Compact reference card. ~195 tokens (was ~525). Cache-eligible.

## build_scan_prompt(...) -> str
Compact pipe-format. Target: ~1,200 tokens total (was ~5,500).
Key formatters:
  _fmt_indicators(indicators)   → pipe-format table (~100 tokens, was ~1,000)
  _fmt_recent_scans(scans)      → 1-line per scan summary (~60 tokens for 5, was ~2,000+)
  _fmt_web_research(web)        → 3 lines, HIGH-impact calendar only (~50 tokens, was ~250)
New params: live_edge_block (LIVE EDGE TRACKER string), is_opus (devil's advocate framing),
            sonnet_analysis (Sonnet result shown to Opus for review)

## load_prompt_learnings(data_dir=None) -> str
Reads storage/data/prompt_learnings.json. Returns compact block (~100 tokens max, last 5 entries).
Returns "" if file missing or empty.

## post_trade_analysis(trade, ai_analysis, data_dir=None) -> None
Called after every trade closes. Rule-based (no LLM cost). Updates prompt_learnings.json (max 20 entries).
Detects: loss+warnings, high-conf-loss, runner win, quick stop-out. Logs insight string.

## class WebResearcher
research() -> dict  # Synchronous/blocking. Run via run_in_executor in async context.
Returns: {timestamp, nikkei_news, economic_calendar, vix, usd_jpy, fear_greed}
usd_jpy: from frankfurter.app (free, no key). vix: from yfinance ^VIX.
