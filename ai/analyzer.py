"""
AI Analysis Module - 3-tier pipeline: Haiku pre-gate → Sonnet scan → Opus confirm.
Uses Claude Code CLI subprocess (OAuth / subscription billing, not API key).
Context files at storage/context/ are written before each call for full transparency.
JSON output enforced via prompt schema + robust parser with safe fallbacks.
"""
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from config.settings import (
    SONNET_MODEL, OPUS_MODEL,
    AI_MAX_TOKENS, AI_TEMPERATURE,
    CONFIDENCE_BASE, CONFIDENCE_CRITERIA, MIN_CONFIDENCE,
    DEFAULT_SL_DISTANCE, DEFAULT_TP_DISTANCE, SPREAD_ESTIMATE,
)

logger = logging.getLogger(__name__)

HAIKU_MODEL   = "claude-haiku-4-5-20251001"
CLAUDE_BIN    = "/home/ubuntu/.local/bin/claude"
PROJECT_ROOT  = Path(__file__).parent.parent
CONTEXT_DIR   = PROJECT_ROOT / "storage" / "context"

# Cost is $0 for subscription; keep structure for compat with save_scan()
PRICING = {
    SONNET_MODEL: {"input": 0.0, "output": 0.0},
    OPUS_MODEL:   {"input": 0.0, "output": 0.0},
    HAIKU_MODEL:  {"input": 0.0, "output": 0.0},
}


# ─── Prompt helpers (unchanged from original) ─────────────────────────────────

def build_system_prompt() -> str:
    """Compact reference-card system prompt. ~200 tokens."""
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

CONFIDENCE (10 criteria, proportional scoring, base 30, cap 100):
  daily_trend | entry_at_tech_level | rsi_15m_in_range | tp_viable
  price_structure | macro_aligned | no_event_1hr | no_friday_monthend
  volume (not LOW) | trend_4h (EMA50 aligned)

EXIT: +150pts → SL to BE+10. TP=400pts. 75%TP in <2h → trail@150pts.
EMA50_bounce setup: DISABLED — do not approve."""


def _fmt_indicators(indicators: dict) -> str:
    """Compact pipe-format indicator table."""
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
        bbm  = tf.get("bb_mid", "?")
        bbl  = tf.get("bb_lower", "?")
        vol  = tf.get("volume_signal", "")
        vrat = tf.get("volume_ratio", "")
        sh   = tf.get("swing_high_20", "")
        sl   = tf.get("swing_low_20", "")
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
    if not scans:
        return "None today."
    lines = []
    for s in scans[-5:]:
        ts  = str(s.get("timestamp", "?"))[:16]
        ses = s.get("session", "?")
        pr  = s.get("price", "?")
        sf  = s.get("setup_found", False)
        conf = s.get("confidence", "")
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
    if not web:
        return "Unavailable."
    vix  = web.get("vix") or "N/A"
    jpy  = web.get("usd_jpy") or "N/A"
    news = web.get("nikkei_news") or []
    cal  = web.get("economic_calendar") or []
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
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    prescreen_block = ""
    if prescreen_direction:
        setup_type   = market_context.get("prescreen_setup_type", "")
        setup_reason = market_context.get("prescreen_reasoning", "")
        session_name = market_context.get("session_name", "")
        prescreen_block = (
            f"\nPRE-SCREEN: {prescreen_direction} | {setup_type} | session={session_name}\n"
            f"  {setup_reason}\n"
            f"  → Confirm or reject. You may suggest opposite direction or NO TRADE.\n"
        )

    local_conf_block = ""
    if local_confidence:
        criteria = local_confidence.get("criteria", {})
        passed = [k for k, v in criteria.items() if v]
        failed = [k for k, v in criteria.items() if not v]
        local_conf_block = (
            f"\nLOCAL SCORE: {local_confidence.get('score', '?')}% "
            f"({local_confidence.get('passed_criteria', '?')}/{local_confidence.get('total_criteria', 10)}) "
            f"✓{','.join(passed)} ✗{','.join(failed)}\n"
        )

    if is_opus and sonnet_analysis:
        sonnet_conf   = sonnet_analysis.get("confidence", "?")
        sonnet_warn   = sonnet_analysis.get("warnings", [])
        sonnet_reason = sonnet_analysis.get("reasoning", "")
        role_block = (
            f"\nSONNET APPROVED at {sonnet_conf}% confidence.\n"
            f"  Reasoning: {sonnet_reason}\n"
            f"  Warnings flagged: {sonnet_warn}\n"
            f"\nYOUR ROLE: Devil's advocate. Find specific reasons to REJECT.\n"
            f"Before outputting JSON, reason through:\n"
            f"  1. STRUCTURE: Are D1/4H/15M actually aligned? (specific values)\n"
            f"  2. RISK: What exact scenario makes this lose 150pts? (be concrete)\n"
            f"  3. EDGE: Given live edge stats above, does this setup type have positive EV right now?\n"
            f"  4. DECISION: If you cannot find sufficient reason to reject, approve.\n"
        )
    else:
        role_block = (
            "\nBefore outputting JSON, reason through:\n"
            "  1. STRUCTURE: Are D1/4H/15M aligned? (cite specific RSI/EMA/BB values)\n"
            "  2. SETUP QUALITY: Is the technical trigger clean? (price distance, volume)\n"
            "  3. RISK: What specific scenario causes a 150pt loss? (be concrete)\n"
            "  4. EDGE: Given live edge stats, does this setup type have positive EV now?\n"
        )

    context_note = ""
    if CONTEXT_DIR.exists():
        context_note = (
            f"\nFull context files available at {CONTEXT_DIR}/ "
            f"(market_snapshot.md, recent_activity.md, macro.md, live_edge.md)\n"
        )

    return (
        f"Japan 225 CFD analysis — {now}\n"
        f"{prescreen_block}{local_conf_block}"
        f"\nTIMEFRAME SNAPSHOT:\n{_fmt_indicators(indicators)}\n"
        f"\nRECENT SCANS (last 5):\n{_fmt_recent_scans(recent_scans)}\n"
        f"\nMARKET CONTEXT: session={market_context.get('session_name','?')} | "
        f"trading_mode={market_context.get('trading_mode','?')}\n"
        f"\nWEB RESEARCH:\n{_fmt_web_research(web_research)}\n"
        f"{context_note}"
        + (("\n" + live_edge_block) if live_edge_block else "")
        + "\n"
        + role_block
    )


def _should_call_opus(sonnet_confidence: int, direction: str) -> bool:
    from config.settings import MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT
    threshold = MIN_CONFIDENCE_SHORT if direction == "SHORT" else MIN_CONFIDENCE
    if sonnet_confidence >= 87:
        logger.info(f"Opus skipped: Sonnet very high confidence ({sonnet_confidence}%) — certain approval")
        return False
    if sonnet_confidence <= threshold + 2:
        logger.info(f"Opus skipped: Sonnet near-floor ({sonnet_confidence}%, floor={threshold}%) — not worth cost")
        return False
    return True


# ─── JSON parsing ─────────────────────────────────────────────────────────────

def _parse_json(text: str, default: dict) -> dict:
    """Extract first JSON object from CLI output. Handles ```json...``` blocks."""
    # Try fenced code block first
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try raw JSON object (greedy then shrink)
    m = re.search(r"\{[\s\S]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    logger.warning(f"JSON parse failed — using safe default. Raw output[:300]: {text[:300]}")
    return default


# ─── AIAnalyzer ───────────────────────────────────────────────────────────────

class AIAnalyzer:
    """
    3-tier pipeline: Haiku → Sonnet → Opus.
    All calls via Claude Code CLI subprocess — OAuth subscription billing, not API key.
    """

    def __init__(self):
        self.total_cost = 0.0  # Always $0 (subscription); kept for interface compat

    def _run_claude(self, model: str, system_prompt: str, user_prompt: str, timeout: int = 180) -> str:
        """
        Invoke Claude Code CLI with OAuth credentials (no API key).
        Combines system + user prompt so Claude has full context in --print mode.
        """
        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)   # strip key → force OAuth subscription

        full_prompt = (
            f"<system>\n{system_prompt}\n</system>\n\n"
            f"<task>\n{user_prompt}\n</task>"
        )

        start = time.time()
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions"],
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(PROJECT_ROOT),
            )
            elapsed = time.time() - start
            output = (result.stdout or "").strip()
            if result.returncode != 0:
                logger.warning(f"Claude CLI exit {result.returncode}: {(result.stderr or '')[:200]}")
            logger.info(f"Claude CLI {model.split('-')[1]} ({elapsed:.1f}s) → {len(output)} chars")
            return output
        except subprocess.TimeoutExpired:
            logger.error(f"Claude CLI timeout ({timeout}s) for model {model}")
            return ""
        except FileNotFoundError:
            logger.error(f"Claude binary not found at {CLAUDE_BIN}")
            return ""

    # ── Haiku pre-gate ─────────────────────────────────────────────────────────

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
        Haiku pre-gate: cheap filter before Sonnet analysis.
        Returns {should_escalate: bool, reason: str, _cost: float}.
        """
        edge_str = live_edge_block or "(no live edge data yet — bot is new)"
        score  = local_confidence.get("score", "?")  if local_confidence else "?"
        passed = local_confidence.get("passed_criteria", "?") if local_confidence else "?"

        failed_str = ""
        if failed_criteria:
            failed_str = (
                "\nFAILED LOCAL CRITERIA (technical code only — you may override with macro):\n"
                + "\n".join(f"  ✗ {c}" for c in failed_criteria) + "\n"
            )

        ind_str = ""
        if indicators:
            ind_str = "\nINDICATOR SNAPSHOT:\n" + _fmt_indicators(indicators) + "\n"

        web_str = ""
        if web_research:
            web_str = "\nMACRO CONTEXT:\n" + _fmt_web_research(web_research) + "\n"

        context_note = ""
        if CONTEXT_DIR.exists():
            context_note = (
                f"\nDetailed context files are at {CONTEXT_DIR}/ — "
                f"read market_snapshot.md, macro.md, live_edge.md if you need more detail.\n"
            )

        user_prompt = (
            f"Japan 225 pre-screen gate. Decide: escalate to full Sonnet analysis or reject?\n\n"
            f"Setup: {setup_type} | Direction: {direction} | Session: {session}\n"
            f"RSI 15M: {rsi_15m} | Volume: {volume_signal}\n"
            f"Local score: {score}% ({passed}/10 technical criteria passed)\n"
            f"{failed_str}{ind_str}{web_str}{context_note}"
            f"{edge_str}\n\n"
            f"DECISION FRAMEWORK:\n"
            f"REJECT (should_escalate=false) if:\n"
            f"  - volume=LOW on bollinger_mid_bounce (thin bounce, no real buyers)\n"
            f"  - This setup type + session WR <35% with no macro override\n"
            f"  - RSI badly outside valid range AND no macro support\n"
            f"  - Multiple technical failures AND macro is neutral/negative\n\n"
            f"ESCALATE (should_escalate=true) if:\n"
            f"  - Soft criteria failed (C5 price structure, C4 tp_viable) AND macro strongly supports\n"
            f"  - Volume is HIGH or NORMAL with supportive macro\n"
            f"  - Live edge shows setup type WR >40%\n"
            f"  - Genuinely uncertain → escalate (Sonnet makes the final call)\n\n"
            f"Output ONLY a JSON object — no other text:\n"
            f'{{"should_escalate": true, "reason": "one sentence reason"}}'
        )

        system_prompt = "You are a Japan 225 CFD pre-screen filter. Output ONLY the JSON object requested. No explanations, no markdown, just the JSON."

        raw = self._run_claude(HAIKU_MODEL, system_prompt, user_prompt, timeout=60)
        result = _parse_json(raw, default={"should_escalate": True, "reason": "Haiku parse error — defaulting to escalate"})
        result["_cost"] = 0.0

        if not result.get("should_escalate"):
            logger.info(f"Haiku filtered: {result.get('reason', '')}")
        return result

    # ── Sonnet scan ────────────────────────────────────────────────────────────

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

    # ── Opus confirmation ──────────────────────────────────────────────────────

    def confirm_with_opus(
        self,
        indicators: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
        sonnet_analysis: dict,
        live_edge_block: str = None,
    ) -> dict:
        direction = sonnet_analysis.get("direction", "LONG") or "LONG"

        if not _should_call_opus(sonnet_analysis.get("confidence", 0), direction):
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

    # ── Core analysis ──────────────────────────────────────────────────────────

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
        user_prompt = build_scan_prompt(
            indicators, recent_scans, market_context, web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
            live_edge_block=live_edge_block,
            is_opus=is_opus,
            sonnet_analysis=sonnet_analysis,
        )

        schema_comment = (
            "// All fields required if setup_found=true. "
            "direction/entry/stop_loss/take_profit/setup_type may be null if setup_found=false."
        )
        user_prompt += (
            f"\n\nOutput ONLY a valid JSON object — no other text, no markdown:\n"
            f"{schema_comment}\n"
            f'{{"setup_found": false, "direction": null, "confidence": 0, '
            f'"entry": null, "stop_loss": null, "take_profit": null, '
            f'"setup_type": null, "reasoning": "...", '
            f'"confidence_breakdown": {{"daily_trend": false, "entry_at_tech_level": false, '
            f'"rsi_15m_in_range": false, "tp_viable": false, "price_structure": false, '
            f'"macro_aligned": false, "no_event_1hr": false, "no_friday_monthend": false}}, '
            f'"key_levels": {{"support": [], "resistance": []}}, '
            f'"trend_observation": "...", "warnings": [], "edge_factors": []}}'
        )

        raw = self._run_claude(model, build_system_prompt(), user_prompt, timeout=180)

        default = {
            "setup_found": False,
            "confidence": 0,
            "reasoning": f"Parse error — CLI returned unparseable output",
            "confidence_breakdown": {},
            "warnings": [],
            "edge_factors": [],
        }
        result = _parse_json(raw, default)
        result["_model"]  = model
        result["_cost"]   = 0.0
        result["_tokens"] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        return result


# ─── Prompt learnings (unchanged) ─────────────────────────────────────────────

def load_prompt_learnings(data_dir: str = None) -> str:
    from pathlib import Path
    path = Path(data_dir or "storage/data") / "prompt_learnings.json"
    try:
        if not path.exists():
            return ""
        learnings = json.loads(path.read_text())
        if not learnings:
            return ""
        recent = learnings[-5:]
        lines = ["PROMPT LEARNINGS (auto-generated from closed trades):"]
        for entry in recent:
            lines.append(f"  - {entry.get('insight', '')}")
        return "\n".join(lines)
    except Exception:
        return ""


def post_trade_analysis(trade: dict, ai_analysis: dict, data_dir: str = None) -> None:
    """Post-trade learning — pure rule-based, no LLM call."""
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

    warnings = []
    if isinstance(ai_analysis, dict):
        warnings = ai_analysis.get("warnings", [])
    elif isinstance(ai_analysis, str):
        try:
            warnings = json.loads(ai_analysis).get("warnings", [])
        except Exception:
            pass

    insight = None

    if pnl < 0 and warnings:
        warn_str = "; ".join(str(w) for w in warnings[:2])
        insight = (
            f"{setup_type} in {session} LOSS (conf={confidence}%). "
            f"AI warned: '{warn_str}'. Pattern: warnings present = higher rejection bar."
        )
    elif pnl < 0 and confidence >= 80:
        insight = (
            f"{setup_type} in {session} LOSS despite {confidence}% confidence. "
            f"Review: high confidence does not guarantee win in {session} session."
        )
    elif pnl > 0 and phase_close == "runner":
        insight = (
            f"{setup_type} in {session} hit RUNNER phase. "
            f"Duration: {duration_min}min. Pattern: this setup type can run — let it."
        )
    elif pnl < 0 and duration_min < 30:
        insight = (
            f"{setup_type} in {session} stopped in {duration_min}min. "
            f"Pattern: quick stop-outs in {session} may indicate poor timing — "
            f"require stronger bounce confirmation."
        )

    if not insight:
        return

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
        learnings = learnings[-20:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(learnings, indent=2))
        logger.info(f"Prompt learning saved: {insight[:80]}…")
    except Exception as e:
        logger.warning(f"post_trade_analysis save failed (non-fatal): {e}")


# ─── WebResearcher (unchanged) ────────────────────────────────────────────────

class WebResearcher:
    """Fetches market data from free APIs for context."""

    def __init__(self):
        self.client = httpx.Client(timeout=10)

    def research(self) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "nikkei_news": self._get_nikkei_news(),
            "economic_calendar": self._get_calendar(),
            "vix": self._get_vix(),
            "usd_jpy": self._get_usd_jpy(),
            "fear_greed": self._get_fear_greed(),
        }

    def _get_nikkei_news(self) -> list[str]:
        try:
            return ["News API integration pending"]
        except Exception as e:
            logger.warning(f"News fetch failed: {e}")
            return []

    def _get_calendar(self) -> list[dict]:
        try:
            return []
        except Exception as e:
            logger.warning(f"Calendar fetch failed: {e}")
            return []

    def _get_vix(self) -> Optional[float]:
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

    def _get_usd_jpy(self) -> Optional[float]:
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

    def _get_fear_greed(self) -> Optional[int]:
        return None

    def close(self):
        self.client.close()
