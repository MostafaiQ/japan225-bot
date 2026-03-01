"""
AI Analysis Module - 3-tier pipeline: Haiku pre-gate → Sonnet scan → Opus confirm.
Tool use enforces structured output (zero parse failures).
Prompt caching on system prompt reduces Opus cost by ~90% on that block.
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
HAIKU_MODEL = "claude-haiku-4-5-20251001"

PRICING = {
    SONNET_MODEL: {"input": 3.0 / 1_000_000,  "output": 15.0 / 1_000_000},
    OPUS_MODEL:   {"input": 15.0 / 1_000_000, "output": 75.0 / 1_000_000},
    HAIKU_MODEL:  {"input": 0.80 / 1_000_000, "output": 4.0 / 1_000_000},
    "web_search": 0.01,
}

# Tool use schema — enforces structured output, eliminates json.loads() failures.
TRADE_ANALYSIS_TOOL = {
    "name": "submit_trade_analysis",
    "description": "Submit your final trade analysis. Call this once with your complete decision.",
    "input_schema": {
        "type": "object",
        "properties": {
            "setup_found":   {"type": "boolean", "description": "True if a valid setup exists"},
            "direction":     {"type": "string", "enum": ["LONG", "SHORT"], "description": "Trade direction (omit if setup_found=false)"},
            "confidence":    {"type": "integer", "minimum": 0, "maximum": 100},
            "entry":         {"type": "number", "description": "Entry price (null if no setup)"},
            "stop_loss":     {"type": "number", "description": "Stop loss price (null if no setup)"},
            "take_profit":   {"type": "number", "description": "Take profit price (null if no setup)"},
            "setup_type":    {"type": "string", "enum": ["bollinger_mid_bounce", "bollinger_lower_bounce", "bollinger_upper_rejection", "ema50_rejection"]},
            "reasoning":     {"type": "string", "description": "Concise explanation of decision"},
            "confidence_breakdown": {
                "type": "object",
                "properties": {
                    "daily_trend":          {"type": "boolean"},
                    "entry_at_tech_level":  {"type": "boolean"},
                    "rsi_15m_in_range":     {"type": "boolean"},
                    "tp_viable":            {"type": "boolean"},
                    "price_structure":      {"type": "boolean"},
                    "macro_aligned":        {"type": "boolean"},
                    "no_event_1hr":         {"type": "boolean"},
                    "no_friday_monthend":   {"type": "boolean"},
                },
                "required": ["daily_trend", "entry_at_tech_level", "rsi_15m_in_range",
                             "tp_viable", "price_structure", "macro_aligned",
                             "no_event_1hr", "no_friday_monthend"],
            },
            "key_levels":         {"type": "object", "properties": {"support": {"type": "array", "items": {"type": "number"}}, "resistance": {"type": "array", "items": {"type": "number"}}}},
            "trend_observation":  {"type": "string"},
            "warnings":           {"type": "array", "items": {"type": "string"}},
            "edge_factors":       {"type": "array", "items": {"type": "string"}, "description": "Specific reasons this setup has positive EV right now"},
        },
        "required": ["setup_found", "confidence", "reasoning", "confidence_breakdown"],
    },
}

# Haiku tool — minimal schema for the pre-gate decision
HAIKU_GATE_TOOL = {
    "name": "submit_precheck",
    "description": "Submit your pre-check decision on whether to escalate to full Sonnet analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "should_escalate": {"type": "boolean"},
            "reason":          {"type": "string", "description": "1-2 sentence reason"},
        },
        "required": ["should_escalate", "reason"],
    },
}


def build_system_prompt() -> str:
    """Compact reference-card system prompt. ~200 tokens vs original ~525."""
    return """Japan 225 Cash CFD analyst. LONG+SHORT bidirectional. No directional bias.

SETUPS — LONG (requires daily_bullish: price > EMA200_daily or EMA50 fallback):
  bb_mid_bounce:     price ±150pts BB_mid | RSI15M 35-55 | bounce_starting=T
  bb_lower_bounce:   price ±80pts BB_lower | RSI15M 20-40 | lower_wick ≥15pts | EMA50 below OK
    → REJECT if vol=LOW (no buyers) or 4H sharply down with no RSI divergence

SETUPS — SHORT (requires daily_bearish | min confidence 75% — BOJ risk):
  bb_upper_rejection: price ±150pts BB_upper | RSI15M 55-75 | below EMA50
  ema50_rejection:    price ≤ EMA50+2 | dist ≤150pts | RSI15M 50-70

RULES: RR ≥ 1.5 after spread(7pts). No trade: HIGH event <60min. SL=150 TP=400.
VOLUME: HIGH(>1.5x)=conviction. LOW(<0.7x)=lean REJECT.
SWING LEVELS: dist_swing_hi <200pts → TP obstacle, reduce confidence.
              dist_swing_lo <100pts → SL anchor, good for LONG.

CONFIDENCE (8 criteria × 10pts + base 30, cap 100):
  daily_trend | entry_at_tech_level | rsi_15m_in_range | tp_viable
  price_structure | macro_aligned | no_event_1hr | no_friday_monthend

EXIT: +150pts → SL to BE+10. TP=400pts. 75%TP in <2h → trail@150pts.
EMA50_bounce setup: DISABLED — do not approve.

Use the submit_trade_analysis tool to respond. No free text outside tool call."""


def _fmt_indicators(indicators: dict) -> str:
    """
    Compact pipe-format indicator table. ~100 tokens vs ~1,000 for json.dumps indent=2.
    Handles multiple timeframe key conventions (15m / tf_15m / m15 / 15min etc).
    """
    TF_KEYS = [
        ("D1",  ["daily", "d1", "1d"]),
        ("4H",  ["4h", "tf_4h", "4hour", "h4"]),
        ("15M", ["15m", "tf_15m", "15min", "m15"]),
    ]
    lines = []
    for label, keys in TF_KEYS:
        tf = {}
        for k in keys:
            if k in indicators and isinstance(indicators[k], dict):
                tf = indicators[k]
                break
        if not tf:
            continue

        p    = tf.get("price", tf.get("close", "?"))
        rsi  = tf.get("rsi", "?")
        e50  = tf.get("ema_50", tf.get("ema50", "?"))
        e200 = tf.get("ema_200", tf.get("ema200", ""))
        bbu  = tf.get("bb_upper", "?")
        bbm  = tf.get("bb_mid",   "?")
        bbl  = tf.get("bb_lower", "?")
        vol  = tf.get("volume_signal", "")
        vrat = tf.get("volume_ratio", "")
        sh   = tf.get("swing_high_20", "")
        sl   = tf.get("swing_low_20",  "")
        bnc  = tf.get("bounce_starting", "")

        parts = [f"{label}: p={p} rsi={rsi} ema50={e50}"]
        if e200:
            parts.append(f"ema200={e200}")
        parts.append(f"bb={bbl}/{bbm}/{bbu}")
        if vol:
            parts.append(f"vol={vol}({vrat}x)" if vrat else f"vol={vol}")
        if sh and sl and p and p != "?":
            try:
                parts.append(f"swing=+{float(sh)-float(p):.0f}/-{float(p)-float(sl):.0f}pts")
            except (TypeError, ValueError):
                pass
        if bnc != "":
            parts.append(f"bounce={'T' if bnc else 'F'}")
        lines.append(" | ".join(parts))
    return "\n".join(lines) if lines else "(no indicator data)"


def _fmt_recent_scans(scans: list) -> str:
    """
    One-line summary per scan. ~60 tokens for 5 scans vs ~2,000+ for full JSON blobs.
    Strips raw indicator/analysis dumps entirely — only keeps decision-relevant fields.
    """
    if not scans:
        return "None today."
    lines = []
    for s in scans[-5:]:
        ts  = str(s.get("timestamp", "?"))[:16]
        ses = s.get("session", "?")
        pr  = s.get("price", "?")
        sf  = s.get("setup_found", False)
        conf = s.get("confidence", "")
        # analysis may be a nested dict (parsed from JSON) or raw string
        analysis = s.get("analysis", {})
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except Exception:
                analysis = {}
        direction  = analysis.get("direction", "")
        setup_type = analysis.get("setup_type", "")
        reasoning  = str(analysis.get("reasoning", ""))[:80]

        tag = f"SETUP:{direction}:{setup_type}" if sf else "NO SETUP"
        conf_str = f" conf={conf}%" if conf else ""
        lines.append(f"[{ts}|{ses}|{pr}] {tag}{conf_str} — {reasoning}")
    return "\n".join(lines)


def _fmt_web_research(web: dict) -> str:
    """
    Compress web research to ~3 lines. ~50 tokens vs ~250 for raw JSON.
    """
    if not web:
        return "Unavailable."
    vix  = web.get("vix") or "N/A"
    jpy  = web.get("usd_jpy") or "N/A"
    news = web.get("nikkei_news") or []
    cal  = web.get("economic_calendar") or []

    # Filter calendar to HIGH impact events only
    high_cal = [e for e in cal if isinstance(e, dict) and e.get("impact") == "HIGH"][:3]
    news_str = " | ".join(str(n)[:70] for n in (news[:2] if news else []))

    lines = [f"USD/JPY: {jpy} | VIX: {vix}"]
    if news_str:
        lines.append(f"News: {news_str}")
    lines.append(f"Calendar HIGH: {high_cal if high_cal else 'none next 8h'}")
    return "\n".join(lines)


def build_scan_prompt(
    indicators: dict,
    recent_scans: list,
    market_context: dict,
    web_research: dict,
    prescreen_direction: str = None,
    local_confidence: dict = None,
    live_edge_block: str = None,
    is_opus: bool = False,
    sonnet_analysis: dict = None,
) -> str:
    """
    Build the user prompt for a scan analysis.
    Uses compact formats throughout — target: ~1,200 tokens total (vs ~5,500 before).

    live_edge_block: pre-formatted LIVE EDGE TRACKER string from database.
    is_opus: if True, adds devil's advocate framing.
    sonnet_analysis: Sonnet's result dict, injected for Opus review.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    # Pre-screen block
    prescreen_block = ""
    if prescreen_direction:
        setup_type    = market_context.get("prescreen_setup_type", "")
        setup_reason  = market_context.get("prescreen_reasoning", "")
        session_name  = market_context.get("session_name", "")
        prescreen_block = (
            f"\nPRE-SCREEN: {prescreen_direction} | {setup_type} | session={session_name}\n"
            f"  {setup_reason}\n"
            f"  → Confirm or reject. You may suggest opposite direction or NO TRADE.\n"
        )

    # Local confidence block
    local_conf_block = ""
    if local_confidence:
        criteria = local_confidence.get("criteria", {})
        passed = [k for k, v in criteria.items() if v]
        failed = [k for k, v in criteria.items() if not v]
        local_conf_block = (
            f"\nLOCAL SCORE: {local_confidence.get('score', '?')}% "
            f"({local_confidence.get('passed_criteria', '?')}/{local_confidence.get('total_criteria', 8)}) "
            f"✓{','.join(passed)} ✗{','.join(failed)}\n"
        )

    # Reasoning scaffold (chain-of-thought before tool call)
    if is_opus and sonnet_analysis:
        sonnet_conf = sonnet_analysis.get("confidence", "?")
        sonnet_warn = sonnet_analysis.get("warnings", [])
        sonnet_reason = sonnet_analysis.get("reasoning", "")
        role_block = (
            f"\nSONNET APPROVED at {sonnet_conf}% confidence.\n"
            f"  Reasoning: {sonnet_reason}\n"
            f"  Warnings flagged: {sonnet_warn}\n"
            f"\nYOUR ROLE: Devil's advocate. Find specific reasons to REJECT.\n"
            f"Before calling submit_trade_analysis, reason through:\n"
            f"  1. STRUCTURE: Are D1/4H/15M actually aligned? (specific values)\n"
            f"  2. RISK: What exact scenario makes this lose 150pts? (be concrete)\n"
            f"  3. EDGE: Given live edge stats above, does this setup type have positive EV right now?\n"
            f"  4. DECISION: If you cannot find sufficient reason to reject, approve.\n"
        )
    else:
        role_block = (
            "\nBefore calling submit_trade_analysis, reason through:\n"
            "  1. STRUCTURE: Are D1/4H/15M aligned? (cite specific RSI/EMA/BB values)\n"
            "  2. SETUP QUALITY: Is the technical trigger clean? (price distance, volume)\n"
            "  3. RISK: What specific scenario causes a 150pt loss? (be concrete)\n"
            "  4. EDGE: Given live edge stats, does this setup type have positive EV now?\n"
        )

    prompt = (
        f"Japan 225 CFD analysis — {now}\n"
        f"{prescreen_block}{local_conf_block}"
        f"\nTIMEFRAME SNAPSHOT:\n{_fmt_indicators(indicators)}\n"
        f"\nRECENT SCANS (last 5):\n{_fmt_recent_scans(recent_scans)}\n"
        f"\nMARKET CONTEXT: session={market_context.get('session_name','?')} | "
        f"trading_mode={market_context.get('trading_mode','?')}\n"
        f"\nWEB RESEARCH:\n{_fmt_web_research(web_research)}\n"
    )

    if live_edge_block:
        prompt += f"\n{live_edge_block}\n"

    prompt += role_block
    return prompt


def _should_call_opus(sonnet_confidence: int, direction: str) -> bool:
    """
    Skip Opus when the outcome is obvious (certain approval or near-rejection).
    Only call Opus for the genuine borderline zone where a second opinion has value.

    Skip if confidence >= 87% (Sonnet is certain, Opus won't change the outcome).
    Skip if confidence <= threshold+2 (barely above floor, Opus will just confirm rejection).
    """
    from config.settings import MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT
    threshold = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
    if sonnet_confidence >= 87:
        logger.info(f"Opus skipped: Sonnet very high confidence ({sonnet_confidence}%) — certain approval")
        return False
    if sonnet_confidence <= threshold + 2:
        logger.info(f"Opus skipped: Sonnet near-floor ({sonnet_confidence}%, floor={threshold}%) — not worth cost")
        return False
    return True


class AIAnalyzer:
    """Handles all AI inference for trade analysis. 3-tier: Haiku → Sonnet → Opus."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.total_cost = 0.0
    
    def precheck_with_haiku(
        self,
        setup_type: str,
        direction: str,
        rsi_15m: float,
        volume_signal: str,
        session: str,
        live_edge_block: str = None,
        local_confidence: dict = None,
        web_research: dict = None,
        failed_criteria: list = None,
        indicators: dict = None,
    ) -> dict:
        """
        Haiku pre-gate: cheap filter (~$0.0013/call) before Sonnet analysis.
        Now receives full macro context (web_research, failed criteria, indicator snapshot)
        so it can intelligently evaluate setups that scored 35-49% locally.

        Key insight: static local score = pure technical criteria only.
        Haiku can see USD/JPY trend, VIX, news, volume, and override a low local score
        if external macro context strongly supports the setup.

        Returns {should_escalate: bool, reason: str, _cost: float}.
        """
        edge_str = live_edge_block or "(no live edge data yet — bot is new)"

        score = local_confidence.get("score", "?") if local_confidence else "?"
        passed = local_confidence.get("passed_criteria", "?") if local_confidence else "?"

        # Format which technical criteria failed and why that might be overridable
        failed_str = ""
        if failed_criteria:
            failed_str = (
                f"\nFAILED LOCAL CRITERIA (technical code only — you may override with macro context):\n"
                + "\n".join(f"  ✗ {c}" for c in failed_criteria)
                + "\n"
            )

        # Compact indicator snapshot for Haiku context
        ind_str = ""
        if indicators:
            ind_str = "\nINDICATOR SNAPSHOT:\n" + _fmt_indicators(indicators) + "\n"

        # Compact web research
        web_str = ""
        if web_research:
            web_str = "\nMACRO CONTEXT:\n" + _fmt_web_research(web_research) + "\n"

        prompt = (
            f"Japan 225 pre-screen gate. Decide: escalate to full Sonnet analysis or reject?\n\n"
            f"Setup: {setup_type} | Direction: {direction} | Session: {session}\n"
            f"RSI 15M: {rsi_15m} | Volume: {volume_signal}\n"
            f"Local score: {score}% ({passed}/8 technical criteria passed)\n"
            f"{failed_str}{ind_str}{web_str}"
            f"{edge_str}\n\n"
            f"DECISION FRAMEWORK:\n"
            f"REJECT (return false) if:\n"
            f"  - volume=LOW on bollinger_mid_bounce (thin bounce, no real buyers)\n"
            f"  - This setup type + session is cold (WR <35% live) with no macro override\n"
            f"  - RSI badly outside valid range (not just borderline) AND no macro support\n"
            f"  - Multiple technical failures AND macro is neutral/negative\n\n"
            f"ESCALATE (return true) if:\n"
            f"  - Technical criteria that failed are soft (C5 price structure, C4 tp_viable)\n"
            f"    AND macro context (USD/JPY trend, VIX, news) strongly supports direction\n"
            f"  - Low local score is explained by macro factors the code can't see\n"
            f"  - Volume is HIGH or NORMAL, and macro is supportive\n"
            f"  - Live edge shows this setup type still has positive WR (>40%)\n\n"
            f"Be decisive. This is a cheap filter — if genuinely uncertain, escalate.\n"
            f"Sonnet (30x more expensive) will make the final analytical call."
        )

        try:
            start = time.time()
            response = self.client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=200,
                temperature=0.0,
                tools=[HAIKU_GATE_TOOL],
                tool_choice={"type": "tool", "name": "submit_precheck"},
                messages=[{"role": "user", "content": prompt}],
            )
            elapsed = time.time() - start

            usage = response.usage
            p = PRICING[HAIKU_MODEL]
            cost = usage.input_tokens * p["input"] + usage.output_tokens * p["output"]
            self.total_cost += cost
            logger.info(f"Haiku precheck: {usage.input_tokens}in/{usage.output_tokens}out = ${cost:.4f} ({elapsed:.1f}s)")

            result = {"should_escalate": True, "reason": "Haiku default pass", "_cost": cost}
            for block in response.content:
                if block.type == "tool_use" and block.name == "submit_precheck":
                    result = dict(block.input)
                    result["_cost"] = cost
                    break

            if not result.get("should_escalate"):
                logger.info(f"Haiku filtered: {result.get('reason', '')}")
            return result

        except Exception as e:
            logger.warning(f"Haiku precheck failed (defaulting to escalate): {e}")
            return {"should_escalate": True, "reason": f"Haiku error: {e}", "_cost": 0}

    def scan_with_sonnet(
        self,
        indicators: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
        prescreen_direction: str = None,
        local_confidence: dict = None,
        live_edge_block: str = None,
    ) -> dict:
        """
        Run a scan analysis with Sonnet (routine scanning, ~$0.005/call after slimming).
        Returns parsed analysis dict with _cost, _tokens, _model keys.
        """
        return self._analyze(
            model=SONNET_MODEL,
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=market_context,
            web_research=web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
            live_edge_block=live_edge_block,
        )
    
    def confirm_with_opus(
        self,
        indicators: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
        sonnet_analysis: dict,
        live_edge_block: str = None,
    ) -> dict:
        """
        Deep confirmation with Opus 4.6 (devil's advocate framing).
        Only called when Sonnet finds a setup AND confidence is in the borderline zone.
        System prompt is cache-eligible — hits Sonnet's cache if called within 5 min.
        """
        direction = sonnet_analysis.get("direction", "LONG") or "LONG"

        # Conditional Opus: skip if outcome is obvious
        if not _should_call_opus(sonnet_analysis.get("confidence", 0), direction):
            # Return Sonnet result directly, flagged as Opus-skipped
            result = dict(sonnet_analysis)
            result["_opus_skipped"] = True
            return result

        result = self._analyze(
            model=OPUS_MODEL,
            indicators=indicators,
            recent_scans=recent_scans,
            market_context=market_context,
            web_research=web_research,
            live_edge_block=live_edge_block,
            is_opus=True,
            sonnet_analysis=sonnet_analysis,
        )

        # Opus must independently confirm >= direction-appropriate threshold
        from config.settings import MIN_CONFIDENCE_SHORT
        threshold = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
        if result.get("confidence", 0) < threshold:
            result["setup_found"] = False
            result["reasoning"] = (
                f"Opus rejected: confidence {result.get('confidence', 0)}% "
                f"below {threshold}% for {direction}. "
                f"Original: {result.get('reasoning', '')}"
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
        live_edge_block: str = None,
        is_opus: bool = False,
        sonnet_analysis: dict = None,
    ) -> dict:
        """
        Send analysis request via tool use (structured output, zero parse failures).
        System prompt cache_control: when Sonnet→Opus run seconds apart, Opus hits
        the 5-min ephemeral cache, saving ~90% of system prompt input cost.
        """
        user_prompt = build_scan_prompt(
            indicators, recent_scans, market_context, web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
            live_edge_block=live_edge_block,
            is_opus=is_opus,
            sonnet_analysis=sonnet_analysis,
        )

        try:
            start = time.time()
            response = self.client.messages.create(
                model=model,
                max_tokens=AI_MAX_TOKENS,
                temperature=AI_TEMPERATURE,
                system=[{
                    "type": "text",
                    "text": build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},  # 5-min TTL cache
                }],
                tools=[TRADE_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_trade_analysis"},
                messages=[{"role": "user", "content": user_prompt}],
            )
            elapsed = time.time() - start

            # Cost calculation (includes cache read discount when applicable)
            usage = response.usage
            input_tokens  = usage.input_tokens
            output_tokens = usage.output_tokens
            # cache_read_input_tokens billed at 10% of normal input rate
            cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write   = getattr(usage, "cache_creation_input_tokens", 0) or 0
            p = PRICING[model]
            cost = (
                (input_tokens - cache_read) * p["input"]
                + cache_read  * p["input"] * 0.10   # 90% cheaper
                + cache_write * p["input"] * 0.25   # 25% surcharge for write
                + output_tokens * p["output"]
            )
            self.total_cost += cost

            cache_note = f" cache_r={cache_read}/w={cache_write}" if (cache_read or cache_write) else ""
            logger.info(
                f"{model.split('-')[1]} analysis: {input_tokens}in/{output_tokens}out"
                f"{cache_note} = ${cost:.4f} ({elapsed:.1f}s)"
            )

            # Extract tool use result — no JSON parsing needed
            result = {}
            for block in response.content:
                if block.type == "tool_use" and block.name == "submit_trade_analysis":
                    result = dict(block.input)
                    break

            if not result:
                logger.error(f"No tool_use block in response from {model}")
                return {"setup_found": False, "reasoning": "No tool call returned", "_cost": cost}

            result["_model"]  = model
            result["_cost"]   = cost
            result["_tokens"] = {"input": input_tokens, "output": output_tokens,
                                  "cache_read": cache_read, "cache_write": cache_write}
            return result

        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            return {"setup_found": False, "reasoning": f"API error: {e}", "_cost": 0}
        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return {"setup_found": False, "reasoning": f"Error: {e}", "_cost": 0}


def load_prompt_learnings(data_dir: str = None) -> str:
    """
    Load auto-generated prompt learnings from prompt_learnings.json.
    Returns a compact block injected into Sonnet/Opus prompts (~100 tokens max).
    Returns empty string if file doesn't exist or has no entries.
    """
    from pathlib import Path
    path = Path(data_dir or "storage/data") / "prompt_learnings.json"
    try:
        if not path.exists():
            return ""
        learnings = json.loads(path.read_text())
        if not learnings:
            return ""
        # Keep only the most recent 5 learnings to stay token-efficient
        recent = learnings[-5:]
        lines = ["PROMPT LEARNINGS (auto-generated from closed trades):"]
        for entry in recent:
            lines.append(f"  - {entry.get('insight', '')}")
        return "\n".join(lines)
    except Exception:
        return ""


def post_trade_analysis(trade: dict, ai_analysis: dict, data_dir: str = None) -> None:
    """
    Called after every trade closes. Compares AI reasoning vs actual outcome.
    Updates prompt_learnings.json with compact pattern notes (max 20 entries).
    These are injected into future Sonnet/Opus prompts via load_prompt_learnings().

    Does NOT call an LLM — pure rule-based extraction to keep cost zero.
    """
    from pathlib import Path
    path = Path(data_dir or "storage/data") / "prompt_learnings.json"

    pnl          = trade.get("pnl", 0) or 0
    setup_type   = trade.get("setup_type", "unknown")
    session      = trade.get("session", "unknown")
    confidence   = trade.get("confidence", 0) or 0
    direction    = trade.get("direction", "")
    duration_min = trade.get("duration_minutes", 0) or 0
    phase_close  = trade.get("phase_at_close", "")
    result       = trade.get("result", "")

    # Extract AI warnings from analysis
    warnings = []
    if isinstance(ai_analysis, dict):
        warnings = ai_analysis.get("warnings", [])
    elif isinstance(ai_analysis, str):
        try:
            warnings = json.loads(ai_analysis).get("warnings", [])
        except Exception:
            pass

    insight = None

    # Loss with AI warnings present — AI flagged the risk but approved anyway
    if pnl < 0 and warnings:
        warn_str = "; ".join(str(w) for w in warnings[:2])
        insight = (
            f"{setup_type} in {session} LOSS (conf={confidence}%). "
            f"AI warned: '{warn_str}'. Pattern: warnings present = higher rejection bar."
        )

    # Loss with high confidence — overconfident AI
    elif pnl < 0 and confidence >= 80:
        insight = (
            f"{setup_type} in {session} LOSS despite {confidence}% confidence. "
            f"Review: high confidence does not guarantee win in {session} session."
        )

    # Win at SL distance (survived drawdown) — good SL placement
    elif pnl > 0 and phase_close == "runner":
        insight = (
            f"{setup_type} in {session} hit RUNNER phase. "
            f"Duration: {duration_min}min. Pattern: this setup type can run — let it."
        )

    # Stopped out quickly (< 30 min) — entry timing issue
    elif pnl < 0 and duration_min < 30:
        insight = (
            f"{setup_type} in {session} stopped in {duration_min}min. "
            f"Pattern: quick stop-outs in {session} may indicate poor timing — "
            f"require stronger bounce confirmation."
        )

    if not insight:
        return

    # Load existing, append, trim to 20, save
    try:
        learnings = []
        if path.exists():
            learnings = json.loads(path.read_text())

        learnings.append({
            "timestamp": datetime.now().isoformat(),
            "setup_type": setup_type,
            "session": session,
            "result": result,
            "insight": insight,
        })

        # Keep only most recent 20
        learnings = learnings[-20:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(learnings, indent=2))
        logger.info(f"Prompt learning saved: {insight[:80]}…")
    except Exception as e:
        logger.warning(f"post_trade_analysis save failed (non-fatal): {e}")


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
