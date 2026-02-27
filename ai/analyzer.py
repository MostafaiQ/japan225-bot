"""
AI Analysis Module - Sonnet 4.5 for scanning, Opus 4.6 for confirmation.
Handles prompt construction, API calls, response parsing.
Web research integrated for news, calendar, VIX, JPY.
"""
import json
import logging
import time
from datetime import datetime
from typing import Optional

import anthropic
import httpx

from config.settings import (
    ANTHROPIC_API_KEY, SONNET_MODEL, OPUS_MODEL,
    AI_MAX_TOKENS, AI_TEMPERATURE,
    CONFIDENCE_BASE, CONFIDENCE_CRITERIA, MIN_CONFIDENCE,
    DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE, SPREAD_ESTIMATE,
)

logger = logging.getLogger(__name__)

# Token tracking for cost calculation
PRICING = {
    SONNET_MODEL: {"input": 3.0 / 1_000_000, "output": 15.0 / 1_000_000},
    OPUS_MODEL: {"input": 5.0 / 1_000_000, "output": 25.0 / 1_000_000},
    "web_search": 0.01,  # $0.01 per search
}


def build_system_prompt() -> str:
    """Build the system prompt with all trading rules."""
    return """You are a Japan 225 Cash CFD trading analyst. Your job is to analyze market data and determine if there is a valid trading setup.

RULES (non-negotiable):
- Only LONG setups (trend following on bullish daily structure)
- Entry must be at a technical level (Bollinger mid, EMA50 bounce)
- RSI 15M must be 35-55 (not overbought)
- Daily trend must be bullish (price above EMA200)
- 4H RSI must not be >75 (overbought)
- Minimum R:R is 1:1.5 after spread (7pts)
- No trades if high-impact event within 60 minutes

CONFIDENCE SCORING (8 criteria, 10pts each + 30 base):
1. daily_bullish: Daily trend bullish (above EMA200)
2. entry_at_tech_level: Entry at Bollinger mid or EMA50
3. rsi_15m_in_range: RSI 15M between 35-55
4. tp_viable: Take profit level is achievable (no major resistance before)
5. higher_lows: Price making higher lows
6. macro_bullish: News/sentiment supports long
7. no_event_1hr: No high-impact event within 1 hour
8. no_friday_monthend: Not Friday with data release or month-end

EXIT STRATEGY:
- Default SL: 200 points (adjustable based on structure)
- Default TP: 400 points (1:2 R:R)
- At +150pts: move SL to breakeven
- If price reaches 75% of TP within 2 hours: activate trailing stop (runner)

RESPOND IN JSON ONLY. No markdown, no explanation outside the JSON."""


def build_scan_prompt(
    indicators: dict,
    recent_scans: list,
    market_context: dict,
    web_research: dict,
) -> str:
    """Build the user prompt for a scan analysis."""
    prompt = f"""Analyze the following Japan 225 Cash data and determine if there is a valid trading setup.

CURRENT TIMESTAMP: {datetime.now().isoformat()}

INDICATOR DATA (4 timeframes):
{json.dumps(indicators, indent=2, default=str)}

RECENT SCAN HISTORY (last 5 scans for trend context):
{json.dumps(recent_scans[-5:], indent=2, default=str) if recent_scans else "No previous scans today."}

MARKET CONTEXT:
{json.dumps(market_context, indent=2, default=str)}

WEB RESEARCH:
{json.dumps(web_research, indent=2, default=str)}

Respond with ONLY this JSON structure:
{{
    "setup_found": true/false,
    "direction": "LONG" or null,
    "entry": price or null,
    "stop_loss": price or null,
    "take_profit": price or null,
    "confidence": 0-100,
    "confidence_breakdown": {{
        "daily_bullish": true/false,
        "entry_at_tech_level": true/false,
        "rsi_15m_in_range": true/false,
        "tp_viable": true/false,
        "higher_lows": true/false,
        "macro_bullish": true/false,
        "no_event_1hr": true/false,
        "no_friday_monthend": true/false
    }},
    "setup_type": "bollinger_mid_bounce" or "ema50_bounce" or null,
    "reasoning": "Brief explanation",
    "key_levels": {{
        "support": [list of support levels],
        "resistance": [list of resistance levels]
    }},
    "trend_observation": "How price has been moving across recent scans",
    "warnings": ["any risk warnings"]
}}"""
    return prompt


class AIAnalyzer:
    """Handles all AI inference for trade analysis."""
    
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.total_cost = 0.0
    
    def scan_with_sonnet(
        self,
        indicators: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
    ) -> dict:
        """
        Run a scan analysis with Sonnet 4.5 (cheaper, routine scanning).
        Returns parsed analysis dict.
        """
        return self._analyze(
            model=SONNET_MODEL,
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=market_context,
            web_research=web_research,
        )
    
    def confirm_with_opus(
        self,
        indicators: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
        sonnet_analysis: dict,
    ) -> dict:
        """
        Deep confirmation with Opus 4.6 (expensive, only for setups).
        Gets called only when Sonnet finds a setup.
        """
        # Add Sonnet's analysis to the context
        enhanced_context = dict(market_context)
        enhanced_context["sonnet_preliminary_analysis"] = sonnet_analysis
        
        result = self._analyze(
            model=OPUS_MODEL,
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=enhanced_context,
            web_research=web_research,
        )
        
        # Opus must independently confirm >= MIN_CONFIDENCE
        if result.get("confidence", 0) < MIN_CONFIDENCE:
            result["setup_found"] = False
            result["reasoning"] = (
                f"Opus rejected: confidence {result.get('confidence', 0)}% "
                f"below {MIN_CONFIDENCE}% threshold. "
                f"Original reasoning: {result.get('reasoning', '')}"
            )
        
        return result
    
    def _analyze(
        self,
        model: str,
        indicators: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
    ) -> dict:
        """Send analysis request to Claude API."""
        system_prompt = build_system_prompt()
        user_prompt = build_scan_prompt(
            indicators, recent_scans, market_context, web_research
        )
        
        try:
            start = time.time()
            response = self.client.messages.create(
                model=model,
                max_tokens=AI_MAX_TOKENS,
                temperature=AI_TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            elapsed = time.time() - start
            
            # Calculate cost
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = (
                input_tokens * PRICING[model]["input"]
                + output_tokens * PRICING[model]["output"]
            )
            self.total_cost += cost
            
            logger.info(
                f"{model.split('-')[1]} analysis: {input_tokens} in / {output_tokens} out "
                f"= ${cost:.4f} ({elapsed:.1f}s)"
            )
            
            # Parse response
            text = response.content[0].text.strip()
            # Clean JSON from potential markdown
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            
            result = json.loads(text)
            result["_model"] = model
            result["_cost"] = cost
            result["_tokens"] = {"input": input_tokens, "output": output_tokens}
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {e}")
            logger.error(f"Raw response: {text[:500]}")
            return {"setup_found": False, "reasoning": f"Parse error: {e}", "_cost": 0}
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            return {"setup_found": False, "reasoning": f"API error: {e}", "_cost": 0}
        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return {"setup_found": False, "reasoning": f"Error: {e}", "_cost": 0}


class WebResearcher:
    """Fetches market data from free APIs for context."""
    
    def __init__(self):
        self.client = httpx.Client(timeout=10)
    
    def research(self) -> dict:
        """Gather all market context from web sources."""
        result = {
            "timestamp": datetime.now().isoformat(),
            "nikkei_news": self._get_nikkei_news(),
            "economic_calendar": self._get_calendar(),
            "vix": self._get_vix(),
            "usd_jpy": self._get_usd_jpy(),
            "fear_greed": self._get_fear_greed(),
        }
        return result
    
    def _get_nikkei_news(self) -> list[str]:
        """Get recent Nikkei/Japan market news headlines."""
        try:
            # Using a free news API (newsapi.org or similar)
            # For now, return placeholder - will integrate with actual API
            return ["News API integration pending"]
        except Exception as e:
            logger.warning(f"News fetch failed: {e}")
            return []
    
    def _get_calendar(self) -> list[dict]:
        """Get today's economic calendar events."""
        try:
            # Using Finnhub free tier (60 calls/min)
            # For now, return placeholder
            return []
        except Exception as e:
            logger.warning(f"Calendar fetch failed: {e}")
            return []
    
    def _get_vix(self) -> Optional[float]:
        """Get current VIX level."""
        try:
            return None  # Will integrate with free API
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
            return None
    
    def _get_usd_jpy(self) -> Optional[float]:
        """Get current USD/JPY rate."""
        try:
            return None  # Will integrate with free API
        except Exception as e:
            logger.warning(f"USD/JPY fetch failed: {e}")
            return None
    
    def _get_fear_greed(self) -> Optional[int]:
        """Get CNN Fear & Greed Index."""
        try:
            return None  # Will integrate with free API
        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")
            return None
    
    def close(self):
        self.client.close()
