# ai/analyzer.py — DIGEST
# Purpose: Claude AI analysis. Sonnet pre-scan → Opus confirmation (cost-gated).
# System prompt: bidirectional, volume/S-R aware, all setup types documented.

## class AIAnalyzer
__init__(): creates Anthropic client from ANTHROPIC_API_KEY

scan_with_sonnet(indicators, recent_scans, market_context, web_research,
                 prescreen_direction=None, local_confidence=None) -> dict
  # Calls SONNET_MODEL. Returns analysis dict. Includes _cost key.
  # prescreen_direction passed into prompt as context. AI can override direction.
  # Returns: {setup_found, direction, confidence, entry, stop_loss, take_profit,
  #           setup_type, reasoning, confidence_breakdown, key_levels, trend_observation,
  #           warnings, _cost}

confirm_with_opus(indicators, recent_scans, market_context, web_research,
                  sonnet_analysis) -> dict
  # Calls OPUS_MODEL. Independent analysis given Sonnet's findings.
  # If Opus confidence < direction-appropriate threshold → overrides setup_found=False
  # Returns same shape as scan_with_sonnet output + _cost

_analyze(model, indicators, recent_scans, market_context, web_research,
         prescreen_direction=None, local_confidence=None) -> dict
  # Low-level call. Parses JSON from response. Falls back to empty dict on parse fail.

## build_system_prompt() -> str
  # LONG setup types: bollinger_mid_bounce (RSI 35-48), bollinger_lower_bounce (RSI 20-40, deeply oversold)
  # SHORT setup types: bollinger_upper_rejection, ema50_rejection
  # SL=150pts, TP=400pts (WFO-validated)
  # Volume guidance: HIGH=genuine conviction, LOW=lean toward REJECT
  # Key levels: dist_to_swing_high < 200pts = TP obstacle; swing_low < 100pts below = good SL anchor

## build_scan_prompt(...) -> str
  # Includes MARKET STRUCTURE section (extracted from indicators["15m"]):
  #   - Volume signal + ratio
  #   - 20-bar swing high/low and distance from current price
  # Includes PRE-SCREEN DETECTED block if prescreen_direction given
  # Includes LOCAL CONFIDENCE SCORE block if local_confidence given
  # JSON response includes: setup_found, direction, confidence, confidence_breakdown,
  #   entry, stop_loss, take_profit, setup_type, reasoning, key_levels, warnings

## class WebResearcher
research() -> dict
  # Synchronous (blocking). Returns:
  # {timestamp, nikkei_news, economic_calendar, vix, usd_jpy, fear_greed}
  # usd_jpy: from frankfurter.app (free, no key). vix: from yfinance ^VIX.
  # NOTE: Call via run_in_executor in async context
close()  # Close httpx session
