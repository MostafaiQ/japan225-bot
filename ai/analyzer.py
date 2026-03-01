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
    """Build the system prompt with all trading rules. Bidirectional."""
    return """You are a Japan 225 Cash CFD trading analyst. Analyze market data and determine if there is a valid trading setup.

SUPPORTED DIRECTIONS: LONG and SHORT. Do not bias toward either direction.

LONG SETUP TYPES:
1. bollinger_mid_bounce: Price near BB midband (within 150pts), RSI 15M 35-48, above EMA50, bounce starting
2. bollinger_lower_bounce: Price near BB lower band (within 80pts), RSI 15M 20-40 (deeply oversold), lower wick rejection >= 15pts
   - At the lower band, price may be below EMA50 — this is expected. Evaluate 4H RSI for macro confluence.
   - REJECT if: volume is LOW (thin bounce = no real buyers), 4H trend sharply down with no divergence.
3. EMA50 bounce: Price near EMA50 within 150pts, RSI < 55 (DISABLED in code — do not approve)
Daily trend must be BULLISH (price above EMA200 daily, or EMA50 if EMA200 unavailable).

SHORT SETUP TYPES:
1. bollinger_upper_rejection: Price near BB upper band (within 150pts), RSI 15M 55-75, below EMA50
2. ema50_rejection: Price at EMA50 from below (testing resistance), RSI 50-70
Daily trend must be BEARISH. SHORT minimum confidence: 75% (BOJ intervention risk).

COMMON RULES:
- Minimum R:R is 1:1.5 after spread (7pt spread costs both sides)
- No trades if high-impact event within 60 minutes
- Default SL: 150 points | Default TP: 400 points (1:2.7 R:R)
- Breakeven trigger: +150pts → move SL to entry + 10pts buffer

MARKET STRUCTURE FIELDS (in indicators data):
- volume_signal: "HIGH" (>1.5x avg) | "NORMAL" | "LOW" (<0.7x avg)
  HIGH volume on bounce = genuine conviction. LOW volume = suspect, likely reject.
- swing_high_20 / swing_low_20: Highest high and lowest low of last 20 candles.
  Nearest resistance above = potential TP obstacle. Nearest support below = SL viability.
- dist_to_swing_high / dist_to_swing_low: Points from current price to each level.
  If dist_to_swing_high < 200pts → TP at 400pts faces resistance — reduce confidence.
  If dist_to_swing_low < 100pts → SL has support nearby — this is GOOD for LONG setups.

CONFIDENCE SCORING (8 criteria, 10pts each + 30 base, cap 100%):
1. daily_trend: Daily trend aligned with direction
2. entry_at_tech_level: Entry at BB band / EMA50 / BB lower band (within threshold)
3. rsi_15m_in_range: RSI 15M in correct zone (20-40 for lower bounce, 35-48 for mid bounce)
4. tp_viable: Price at/below BB mid for LONG (pullback confirmed)
5. price_structure: Higher lows (LONG) or lower highs (SHORT) on 15M
6. macro_aligned: News/sentiment/USD-JPY supports direction. 4H RSI confluence.
7. no_event_1hr: No high-impact event within 1 hour
8. no_friday_monthend: Not Friday data window or month-end

EXIT STRATEGY:
- At +150pts: move SL to breakeven (entry + 10pt buffer)
- Default TP: +400pts
- If price reaches 75% of TP within 2 hours: activate trailing stop at 150pts

RESPOND IN JSON ONLY. No markdown, no explanation outside the JSON."""


def build_scan_prompt(
    indicators: dict,
    recent_scans: list,
    market_context: dict,
    web_research: dict,
    prescreen_direction: str = None,
    local_confidence: dict = None,
) -> str:
    """
    Build the user prompt for a scan analysis.

    prescreen_direction: 'LONG', 'SHORT', or None (if pre-screen detected a direction)
    local_confidence: output of core.confidence.compute_confidence() for context
    """
    prescreen_block = ""
    if prescreen_direction:
        prescreen_block = f"""
PRE-SCREEN DETECTED: {prescreen_direction} setup forming locally.
Analyze the data and CONFIRM or REJECT this direction.
You MAY suggest the opposite direction or NO TRADE if the data supports it.
Do NOT feel obligated to confirm just because the pre-screen flagged it.
"""

    local_conf_block = ""
    if local_confidence:
        local_conf_block = f"""
LOCAL CONFIDENCE SCORE: {local_confidence.get('score', 'N/A')}% ({local_confidence.get('passed_criteria', 'N/A')}/{local_confidence.get('total_criteria', 8)} criteria)
Local breakdown: {json.dumps(local_confidence.get('criteria', {}), default=str)}
"""

    # Build human-readable market structure summary from 15M indicators
    tf15m = {}
    for k in ("15m", "tf_15m", "15min"):
        if k in indicators and isinstance(indicators[k], dict):
            tf15m = indicators[k]
            break
    market_struct_lines = []
    if tf15m.get("volume_signal"):
        market_struct_lines.append(
            f"  Volume (15M): {tf15m['volume_signal']} "
            f"({tf15m.get('volume_ratio', '?')}x 20-period avg) — "
            + ("HIGH = genuine conviction" if tf15m['volume_signal'] == 'HIGH'
               else "LOW = suspect, lean toward REJECT" if tf15m['volume_signal'] == 'LOW'
               else "normal conditions")
        )
    if tf15m.get("swing_high_20") and tf15m.get("swing_low_20"):
        p = tf15m.get("price", 0)
        sh, sl_lvl = tf15m["swing_high_20"], tf15m["swing_low_20"]
        market_struct_lines.append(
            f"  Swing High (20-bar): {sh:.0f} (+{sh - p:.0f}pts above — potential resistance / TP obstacle)"
        )
        market_struct_lines.append(
            f"  Swing Low  (20-bar): {sl_lvl:.0f} (-{p - sl_lvl:.0f}pts below — potential support / SL anchor)"
        )
    market_struct_block = (
        "\nMARKET STRUCTURE (computed from 15M candles):\n" + "\n".join(market_struct_lines) + "\n"
        if market_struct_lines else ""
    )

    prompt = f"""Analyze the following Japan 225 Cash CFD data and determine if there is a valid trading setup.

CURRENT TIMESTAMP: {datetime.now().isoformat()}
{prescreen_block}{local_conf_block}{market_struct_block}
INDICATOR DATA (available timeframes):
{json.dumps(indicators, indent=2, default=str)}

RECENT SCAN HISTORY (last 5 for trend context):
{json.dumps(recent_scans[-5:], indent=2, default=str) if recent_scans else "No previous scans today."}

MARKET CONTEXT:
{json.dumps(market_context, indent=2, default=str)}

WEB RESEARCH (USD/JPY, VIX, news, calendar):
{json.dumps(web_research, indent=2, default=str)}

Respond with ONLY this JSON structure:
{{
    "setup_found": true/false,
    "direction": "LONG" or "SHORT" or null,
    "entry": price or null,
    "stop_loss": price or null,
    "take_profit": price or null,
    "confidence": 0-100,
    "confidence_breakdown": {{
        "daily_trend": true/false,
        "entry_at_tech_level": true/false,
        "rsi_15m_in_range": true/false,
        "tp_viable": true/false,
        "price_structure": true/false,
        "macro_aligned": true/false,
        "no_event_1hr": true/false,
        "no_friday_monthend": true/false
    }},
    "setup_type": "bollinger_mid_bounce" or "bollinger_lower_bounce" or "bollinger_upper_rejection" or "ema50_rejection" or null,
    "reasoning": "Brief explanation of decision",
    "key_levels": {{
        "support": [list of support prices],
        "resistance": [list of resistance prices]
    }},
    "trend_observation": "How price has been moving across recent scans",
    "warnings": ["any risk warnings or concerns"]
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
        prescreen_direction: str = None,
        local_confidence: dict = None,
    ) -> dict:
        """
        Run a scan analysis with Sonnet (cheaper, routine scanning).
        prescreen_direction and local_confidence are passed into the prompt for context.
        Returns parsed analysis dict.
        """
        return self._analyze(
            model=SONNET_MODEL,
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=market_context,
            web_research=web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
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
        
        # Opus must independently confirm >= direction-appropriate threshold
        direction = result.get("direction", "LONG") or "LONG"
        from config.settings import MIN_CONFIDENCE_SHORT
        threshold = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
        if result.get("confidence", 0) < threshold:
            result["setup_found"] = False
            result["reasoning"] = (
                f"Opus rejected: confidence {result.get('confidence', 0)}% "
                f"below {threshold}% threshold for {direction}. "
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
        prescreen_direction: str = None,
        local_confidence: dict = None,
    ) -> dict:
        """Send analysis request to Claude API."""
        system_prompt = build_system_prompt()
        user_prompt = build_scan_prompt(
            indicators, recent_scans, market_context, web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
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
        """
        Get current USD/JPY rate from frankfurter.app (free, no API key).
        USD/JPY direction matters: JPY strength = bearish for Nikkei 225.
        """
        try:
            from config.settings import USD_JPY_API
            resp = self.client.get(USD_JPY_API, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                rate = data.get("rates", {}).get("JPY")
                if rate:
                    logger.debug(f"USD/JPY: {rate:.2f}")
                    return float(rate)
        except Exception as e:
            logger.warning(f"USD/JPY fetch failed: {e}")
        return None

    def _get_vix(self) -> Optional[float]:
        """
        Get VIX level via yfinance (free, no API key).
        VIX > 25 = elevated risk, Nikkei correlation strengthens.
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker("^VIX")
            info = ticker.fast_info
            vix = getattr(info, "last_price", None)
            if vix:
                logger.debug(f"VIX: {vix:.2f}")
                return float(vix)
        except Exception as e:
            logger.warning(f"VIX fetch failed (yfinance): {e}")
        return None

    def _get_fear_greed(self) -> Optional[int]:
        """Get CNN Fear & Greed Index (placeholder — no reliable free endpoint)."""
        return None
    
    def close(self):
        self.client.close()
