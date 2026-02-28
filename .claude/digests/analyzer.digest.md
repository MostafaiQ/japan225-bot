# ai/analyzer.py — DIGEST
# Purpose: Claude AI analysis. Sonnet pre-scan → Opus confirmation (cost-gated).
# System prompt: bidirectional, no LONG bias.

## class AIAnalyzer
__init__(): creates Anthropic client from ANTHROPIC_API_KEY

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None) -> dict
  # Calls SONNET_MODEL. Returns analysis dict. Includes _cost key.
  # prescreen_direction passed into prompt as context. AI can override direction.
  # Returns: {setup_found, direction, confidence, entry, stop_loss, take_profit,
  #           setup_type, reasoning, confidence_breakdown, _cost}

confirm_with_opus(indicators, recent_scans, market_context, web_research,
                  sonnet_analysis) -> dict
  # Calls OPUS_MODEL. Independent analysis given Sonnet's findings.
  # If Opus confidence < direction-appropriate threshold → overrides setup_found=False
  # Returns same shape as scan_with_sonnet output + _cost

_analyze(model, system_prompt, user_prompt) -> dict
  # Low-level call. Parses JSON from response. Falls back to empty dict on parse fail.

## class WebResearcher
__init__(): creates requests.Session

research() -> dict
  # Synchronous (blocking). Returns:
  # {timestamp, news_headlines: list, economic_calendar: list, vix: float|None,
  #  usd_jpy: float|None, fear_greed: int|None}
  # NOTE: Call via run_in_executor in async context

close()  # Close requests session

## Prompt setup (module-level, ~line 33)
SYSTEM_PROMPT: "Bidirectional. LONG and SHORT. No direction bias."
  # LONG: SL-200/TP+400. SHORT: SL+200/TP-400. MIN_RR=1.5. BB20/EMA50/RSI14.
  # Response must be valid JSON.
