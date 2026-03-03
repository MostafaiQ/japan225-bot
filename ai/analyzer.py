"""
AI Analysis Module - Single-subprocess pipeline: Sonnet 4.6 with adaptive thinking.
Sonnet does analysis + built-in devil's advocate self-review in one call.
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
    SONNET_MODEL, OPUS_MODEL, SPREAD_ESTIMATE,
)

logger = logging.getLogger(__name__)

CLAUDE_BIN    = "/home/ubuntu/.local/bin/claude"
PROJECT_ROOT  = Path(__file__).parent.parent
CONTEXT_DIR   = PROJECT_ROOT / "storage" / "context"

# Cost is $0 for subscription; keep structure for compat with save_scan()
PRICING = {
    SONNET_MODEL: {"input": 0.0, "output": 0.0},
}


# ─── Prompt helpers (unchanged from original) ─────────────────────────────────

def build_system_prompt() -> str:
    """Compact reference-card system prompt. ~230 tokens."""
    return """Japan 225 Cash CFD analyst. LONG+SHORT bidirectional. No directional bias.

SETUPS — LONG (daily trend in reasoning; counter-trend allowed if strong confluence):
  bb_mid_bounce:     price ±150pts BB_mid | RSI15M 30-65 | bounce confirmed (price>prev_close OR oversold reversal signals)
  bb_lower_bounce:   price ±150pts BB_lower | RSI15M 20-40 | lower_wick ≥15pts | EMA50 below OK
  oversold_reversal: RSI15M <30 | daily bullish | reversal confirmation (wick/HA/candle/sweep)
    → Caution if vol=LOW, but not auto-reject (late session vol naturally lower)

SETUPS — SHORT (min confidence 75% — BOJ risk; counter-trend allowed if strong confluence):
  bb_upper_rejection:      price ±150pts BB_upper | RSI15M 55-75 | below EMA50
  ema50_rejection:         price ≤ EMA50+2 | dist ≤150pts | RSI15M 50-70
  bb_mid_rejection:        price ±150pts BB_mid | RSI15M 40-65 | rejection confirmed (price<prev_close OR wick/HA/candle)
  overbought_reversal:     RSI15M >70 | daily bearish | reversal confirmation (wick/HA/candle/sweep)
  breakdown_continuation:  price >100pts below BB_mid | RSI 25-45 | below EMA50 | HA streak ≤-2 | vol not LOW
  dead_cat_bounce_short:   price at BB_mid/EMA9 from below | RSI 43-62 | daily bearish | below EMA50 | HA turning bearish
  bear_flag_breakdown:     RSI 35-52 | vol LOW/NORMAL (flag) | HA streak ≤-1 | price between BB_lower–BB_mid | below EMA50
  high_volume_distribution: price ±200pts BB_upper OR swept_high | RSI 55-75 | vol ratio ≥1.4x | bearish candle/wick
  multi_tf_bearish:        rsi_15m<48 AND rsi_4h<48 AND daily bearish AND below EMA50 AND below VWAP AND HA bearish (≥4/5 factors)

RULES: RR ≥ 1.5 after spread(7pts). No trade: HIGH event <60min. SL=150 TP=400.
VOLUME: HIGH(>1.5x)=conviction. LOW(<0.7x)=caution, weigh alongside other criteria. Not auto-reject.
SWING LEVELS: dist_swing_hi <200pts → TP obstacle, reduce confidence.
              dist_swing_lo <100pts → SL anchor, good for LONG.
HA: ha_bullish=T → buying pressure confirmed. ha_streak≥3 → strong momentum.
FVG: fvg_bullish → unfilled demand zone (support). fvg_bearish → unfilled supply zone (resistance).
VWAP: above = premium (SHORT bias), below = discount (LONG bias). Key intraday mean-reversion level.
FIBO: fib_near = nearest fib level (key S/R). pdh/pdl = prev candle high/low (key levels).
SWEEP: swept_low=T → liquidity grab + bullish reversal. swept_high=T → bearish reversal.
PIVOT: PP/R1-R3/S1-S3 from daily. Near S1/S2=support (LONG). Near R1/R2=resistance (SHORT).
CANDLE: hammer/engulfing/morning_star etc. Direction + strength. Strong pattern at key level = high conviction.
BODY: expanding=momentum, contracting=exhaustion. |consec|>=4=overextended. wick_ratio>2=indecision.

CONFIDENCE (11 criteria, proportional scoring, base 30, cap 100):
  daily_trend | entry_at_tech_level | rsi_15m_in_range | tp_viable
  price_structure | macro_aligned | no_event_1hr | no_friday_monthend
  volume (prefer NORMAL+) | trend_4h (EMA50 aligned) | ha_aligned (HA candle direction)

MEAN-REVERSION BOUNCE RULES (CRITICAL — read before evaluating bb_lower_bounce or oversold_reversal):
  These setups fire BECAUSE of bearish conditions. Do NOT reject them for being bearish:
  - Bearish HA streak is EXPECTED at oversold reversal — it's the setup trigger, not a disqualifier.
  - Price below EMA50 is EXPECTED for lower-band bounces — the band IS below EMA50 in selloffs.
  - FVG supply zones are SOFT resistance, not hard ceilings — they frequently get filled during reversals.
  - 4H bearish structure is EXPECTED — it creates the oversold condition for the bounce.
  - Evaluate bounce QUALITY: wick rejection, pattern, sweep, volume surge, RSI divergence.
  - For bb_lower_bounce/oversold_reversal: the DEFAULT should be to APPROVE if daily trend is bullish
    and any reversal confirmation exists. Only reject if there's a specific concrete catalyst against
    (imminent high-impact event, massive volume on breakdown, no reversal signal at all).

MEAN-REVERSION SHORT RULES (CRITICAL — read before evaluating overbought_reversal or bb_mid_rejection):
  These setups fire BECAUSE of bullish overextension. Do NOT reject them for being bullish:
  - Bullish HA streak is EXPECTED at overbought reversal — it's the setup trigger, not a disqualifier.
  - Price above EMA50 is EXPECTED for upper-band/overbought setups — the band IS above EMA50 in rallies.
  - FVG demand zones are SOFT support, not hard floors — they frequently get filled during reversals.
  - 4H bullish structure is EXPECTED — it creates the overbought condition for the reversal.
  - Evaluate reversal QUALITY: wick rejection, bearish pattern, sweep, volume surge, RSI divergence.
  - For overbought_reversal: the DEFAULT should be to APPROVE if daily trend is bearish
    and any reversal confirmation exists. Only reject if there's a specific concrete catalyst against.

BREAKDOWN / MOMENTUM SHORT RULES (CRITICAL — read before evaluating breakdown_continuation, bear_flag_breakdown, multi_tf_bearish):
  These setups fire BECAUSE of bearish momentum already in progress. Do NOT reject them for daily being bullish:
  - Daily EMA200/EMA50 LAGS on big selloff days — price can drop 2000-4000pts while daily still reads "bullish".
    Daily bullish + 4H/15M deeply bearish = TRANSITION PHASE, not a contradiction. This is where momentum shorts have edge.
  - Price already broke below key levels with conviction (HA streak ≤-2, volume not LOW).
  - Three_black_crows on 4H/15M with HIGH volume = genuine distribution, NOT a reason to reject.
  - 4H below EMA50 by >500pts = deep bearish extension, trend is confirmed on lower TFs regardless of daily.
  - For breakdown_continuation/bear_flag_breakdown/multi_tf_bearish: the DEFAULT should be to APPROVE
    if 4H and 15M are aligned bearish (HA streak ≤-2, below EMA50, RSI<50). Only reject if:
    (1) RSI<25 with reversal candle (oversold bounce risk), or
    (2) price sitting on major support (BB lower, pivot S2/S3), or
    (3) volume is LOW (no conviction behind the move).
  - Do NOT reject because "daily trend is bullish" or "price above daily EMA50/200" — that is EXPECTED.

QUICK REJECT: If ≥4 technical criteria fail AND volume is LOW AND no macro catalyst → set setup_found=false immediately.
  Do not spend analysis time on junk setups. Volume=LOW alone is NOT a reject (Tokyo session inherently lower).

OPUS REVIEW: If an opus_reviewer agent is available AND your confidence lands in 72-86%,
  use it to challenge your case. If no agent available, do your own devil's advocate check.
  If confidence >=87% (clear approve) or <=71% (clear reject), skip extra review.

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
        # New indicators
        ha   = tf.get("ha_bullish")
        hast = tf.get("ha_streak")
        fibn = tf.get("fib_near")
        swlo = tf.get("swept_low")
        swhi = tf.get("swept_high")
        fvg_bull = tf.get("fvg_bullish")
        fvg_bear = tf.get("fvg_bearish")
        fvg_lvl  = tf.get("fvg_level")

        # VWAP
        vwap_val = tf.get("vwap")
        above_vwap = tf.get("above_vwap")
        # PDH/PDL
        pdh = tf.get("prev_candle_high")
        pdl = tf.get("prev_candle_low")

        parts = [f"{label}: p={p} rsi={rsi} ema50={e50}"]
        if e200:
            parts.append(f"ema200={e200}")
        parts.append(f"bb={bbl}/{bbm}/{bbu}")
        if vwap_val is not None:
            vwap_side = "above" if above_vwap else "below" if above_vwap is False else "?"
            parts.append(f"vwap={vwap_val:.0f}({vwap_side})" if isinstance(vwap_val, (int, float)) else f"vwap={vwap_val}({vwap_side})")
        if pdh is not None and pdl is not None:
            parts.append(f"pdh/pdl={pdh:.0f}/{pdl:.0f}" if isinstance(pdh, (int, float)) else f"pdh/pdl={pdh}/{pdl}")
        if vol:
            parts.append(f"vol={vol}({vrat}x)" if vrat else f"vol={vol}")
        if sh and sl and p and p != "?":
            try:
                parts.append(f"swing=+{float(sh)-float(p):.0f}/-{float(p)-float(sl):.0f}pts")
            except (TypeError, ValueError):
                pass
        if bnc != "":
            parts.append(f"bounce={'T' if bnc else 'F'}")
        if ha is not None:
            parts.append(f"ha={'bull' if ha else 'bear'}({hast})")
        if fvg_bull or fvg_bear:
            fvg_type = "bull" if fvg_bull else "bear"
            parts.append(f"fvg={fvg_type}" + (f"@{fvg_lvl}" if fvg_lvl else ""))
        if fibn:
            parts.append(f"fib={fibn}")
        if swlo or swhi:
            parts.append(f"sweep={'low' if swlo else 'high'}")
        # Candlestick pattern
        cp_name = tf.get("candlestick_pattern")
        cp_dir  = tf.get("candlestick_direction")
        cp_str  = tf.get("candlestick_strength")
        if cp_name:
            parts.append(f"candle={cp_name}({cp_dir},{cp_str})")
        # Body trend
        bt = tf.get("body_trend")
        consec = tf.get("consecutive_direction")
        wr = tf.get("wick_ratio")
        if bt and bt != "neutral":
            parts.append(f"bodies={bt}(consec={consec})")
        if wr and isinstance(wr, (int, float)) and wr > 1.5:
            parts.append(f"wick_ratio={wr:.1f}")

        lines.append(" | ".join(parts))

    # Pivot points — top-level key, rendered once (from 15M detect_setup snapshot)
    pvt = indicators.get("pivots")
    if pvt and isinstance(pvt, dict) and pvt.get("pp"):
        pp = pvt["pp"]
        # Find nearest S below and R above current price (use 15M price if available)
        m15 = None
        for k in ["15m", "tf_15m", "15min", "m15"]:
            if k in indicators and isinstance(indicators[k], dict):
                m15 = indicators[k]
                break
        cur_price = m15.get("price") if m15 else None
        s_near = None
        r_near = None
        if cur_price:
            for lvl in ("s1", "s2", "s3"):
                val = pvt.get(lvl)
                if val and val < cur_price:
                    s_near = f"{lvl.upper()}={val:.0f}"
                    break
            for lvl in ("r1", "r2", "r3"):
                val = pvt.get(lvl)
                if val and val > cur_price:
                    r_near = f"{lvl.upper()}={val:.0f}"
                    break
        s_str = s_near or f"S1={pvt.get('s1', '?')}"
        r_str = r_near or f"R1={pvt.get('r1', '?')}"
        lines.append(f"PIVOT: PP={pp:.0f} {s_str} {r_str}")

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
    fg   = web.get("fear_greed")
    news = web.get("nikkei_news") or []
    cal  = web.get("economic_calendar") or []
    high_cal = [e for e in cal if isinstance(e, dict) and e.get("impact") == "HIGH"][:3]
    news_str = " | ".join(str(n)[:70] for n in (news[:2] if news else []))
    lines = [f"USD/JPY: {jpy} | VIX: {vix}" + (f" | Fear&Greed: {fg}" if fg else "")]
    if news_str:
        lines.append(f"News: {news_str}")
    lines.append(f"Calendar HIGH: {high_cal if high_cal else 'none next 8h'}")
    return "\n".join(lines)


def _fmt_recent_trades(trades: list) -> str:
    if not trades:
        return "No closed trades yet."
    lines = []
    for t in trades[-5:]:
        pnl = t.get("pnl") or 0
        outcome = "W" if pnl > 0 else "L"
        lines.append(
            f"[{str(t.get('opened_at', '?'))[:16]}] {t.get('direction','?')} "
            f"{t.get('setup_type','?')} conf={t.get('confidence','?')}% → {outcome} ${pnl:+.2f}"
        )
    return "\n".join(lines)


def build_scan_prompt(
    indicators: dict,
    recent_scans: list,
    market_context: dict,
    web_research: dict,
    prescreen_direction: str = None,
    local_confidence: dict = None,
    live_edge_block: str = None,
    failed_criteria: list = None,
    recent_trades: list = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    prescreen_block = ""
    if prescreen_direction:
        setup_type   = market_context.get("prescreen_setup_type", "")
        setup_reason = market_context.get("prescreen_reasoning", "")
        session_name = market_context.get("session_name", "")
        entry_tf = market_context.get("entry_timeframe", "15m")
        prescreen_block = (
            f"\nPRE-SCREEN: {prescreen_direction} | {setup_type} | session={session_name} | Entry TF: {entry_tf}\n"
            f"  {setup_reason}\n"
            f"  → Confirm or reject. You may suggest opposite direction or NO TRADE.\n"
        )

    # Secondary setup block (bidirectional context)
    secondary_block = ""
    sec = market_context.get("secondary_setup")
    if sec:
        sec_dir = sec.get("direction", "?")
        sec_type = sec.get("type", "?")
        sec_conf = sec.get("confidence", "?")
        sec_passed = sec.get("passed_criteria", "?")
        sec_reason = sec.get("reasoning", "")[:200]
        secondary_block = (
            f"\nSECONDARY SETUP: {sec_dir} | {sec_type} | Local: {sec_conf}% ({sec_passed}/12)\n"
            f"  {sec_reason}\n"
            f"  → Consider this direction if primary doesn't work.\n"
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

    # --- Build failed criteria block ---
    failed_block = ""
    if failed_criteria:
        failed_block = (
            f"\nFAILED LOCAL CRITERIA (technical code only — you may override with macro):\n"
            + "\n".join(f"  ✗ {c}" for c in failed_criteria) + "\n"
        )

    role_block = (
        f"{failed_block}"
        "\nBefore outputting JSON, reason through these steps IN ORDER:\n"
        "  1. STRUCTURE: Are D1/4H/15M aligned? (cite specific RSI/EMA/BB values)\n"
        "  2. SETUP QUALITY: Is the technical trigger clean? (price distance, volume, HA, FVG)\n"
        "  3. RISK: What specific scenario causes a 150pt loss? (be concrete)\n"
        "  4. EDGE: Given live edge stats, does this setup type have positive EV now?\n"
        "  5. OPUS REVIEW (if borderline 72-86%): Spawn the opus_reviewer agent.\n"
        "     Pass it your analysis and ask it to find risks you missed.\n"
        "     Incorporate its feedback before outputting final JSON.\n"
    )

    return (
        f"Japan 225 CFD analysis — {now}\n"
        f"{prescreen_block}{secondary_block}{local_conf_block}"
        f"\nTIMEFRAME SNAPSHOT:\n{_fmt_indicators(indicators)}\n"
        f"\nRECENT SCANS (last 5):\n{_fmt_recent_scans(recent_scans)}\n"
        f"\nRECENT TRADES (last 5):\n{_fmt_recent_trades(recent_trades or [])}\n"
        f"\nMARKET CONTEXT: session={market_context.get('session_name','?')} | "
        f"trading_mode={market_context.get('trading_mode','?')}\n"
        f"\nWEB RESEARCH:\n{_fmt_web_research(web_research)}\n"
        + (("\n" + live_edge_block) if live_edge_block else "")
        + "\n"
        + role_block
    )


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
    Single-subprocess pipeline: Sonnet 4.6 with Opus sub-agent.
    Sonnet analyzes, delegates to Opus sub-agent for devil's advocate on borderline calls.
    All within one Claude Code CLI subprocess — OAuth subscription billing, not API key.
    """

    def __init__(self):
        self.total_cost = 0.0  # Always $0 (subscription); kept for interface compat

    def _run_claude(self, model: str, system_prompt: str, user_prompt: str,
                    timeout: int = 180, use_opus_agent: bool = False) -> tuple[str, dict]:
        """
        Invoke Claude Code CLI with OAuth credentials (no API key).
        Combines system + user prompt so Claude has full context in --print mode.
        Opus sub-agent only loaded when use_opus_agent=True (borderline 60-86% local conf).
        Returns (output_text, token_estimates) — tokens are estimates from char count.
        """
        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)   # strip key → force OAuth subscription

        full_prompt = (
            f"<system>\n{system_prompt}\n</system>\n\n"
            f"<task>\n{user_prompt}\n</task>"
        )
        # Estimate input tokens: ~4 chars/token for English/structured data
        est_input_tokens = len(full_prompt) // 4

        # Build CLI command — only include Opus sub-agent when borderline confidence
        # --tools "" disables all tools → Sonnet responds directly from prompt data (no file reads/commands)
        # This cuts response time from 60-180s to 10-30s by eliminating CLAUDE.md loading + tool calls.
        cmd = [CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions",
               "--no-session-persistence", "--tools", ""]
        if use_opus_agent:
            from config.settings import OPUS_MODEL
            agents_json = json.dumps({
                "opus_reviewer": {
                    "description": "Devil's advocate trade setup reviewer. Challenges the analysis, finds risks, verifies technical case. Use when your confidence is 72-86% (borderline zone).",
                    "model": OPUS_MODEL,
                }
            })
            cmd.extend(["--agents", agents_json])

        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
            elapsed = time.time() - start
            output = (result.stdout or "").strip()
            est_output_tokens = len(output) // 4
            total_tokens = est_input_tokens + est_output_tokens
            if result.returncode != 0:
                logger.warning(f"Claude CLI exit {result.returncode}: {(result.stderr or '')[:200]}")
            model_short = model.split('-')[1] if '-' in model else model[:8]
            logger.info(
                f"Claude CLI {model_short} ({elapsed:.1f}s) | "
                f"~{est_input_tokens:,}in + {est_output_tokens:,}out = {total_tokens:,} tokens (est) | "
                f"subscription (no cost)"
            )
            tokens = {"input": est_input_tokens, "output": est_output_tokens, "total": total_tokens}
            return output, tokens
        except subprocess.TimeoutExpired:
            logger.error(f"Claude CLI timeout ({timeout}s) for model {model}")
            return "", {"input": 0, "output": 0, "total": 0}
        except FileNotFoundError:
            logger.error(f"Claude binary not found at {CLAUDE_BIN}")
            return "", {"input": 0, "output": 0, "total": 0}

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
        failed_criteria: list = None,
        recent_trades: list = None,
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
            failed_criteria=failed_criteria,
            recent_trades=recent_trades,
        )

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
        failed_criteria: list = None,
        recent_trades: list = None,
    ) -> dict:
        user_prompt = build_scan_prompt(
            indicators, recent_scans, market_context, web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
            live_edge_block=live_edge_block,
            failed_criteria=failed_criteria,
            recent_trades=recent_trades,
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

        # Conditional Opus: only load sub-agent when local conf in borderline zone (60-86%)
        local_score = local_confidence.get("score", 0) if local_confidence else 0
        use_opus = 60 <= local_score <= 86

        system_prompt = build_system_prompt()
        raw, tokens = self._run_claude(model, system_prompt, user_prompt,
                                       timeout=180, use_opus_agent=use_opus)

        default = {
            "setup_found": False,
            "confidence": 0,
            "reasoning": "Parse error — CLI returned unparseable output",
            "confidence_breakdown": {},
            "warnings": [],
            "edge_factors": [],
        }
        result = _parse_json(raw, default)

        # Retry once on parse error (empty output or no reasoning)
        if not raw.strip() or (result.get("reasoning", "").startswith("Parse error") and not result.get("setup_found")):
            logger.warning("AI returned empty/unparseable output — retrying once")
            raw2, tokens2 = self._run_claude(model, system_prompt, user_prompt,
                                             timeout=180, use_opus_agent=False)
            if raw2.strip():
                result = _parse_json(raw2, default)
                tokens = tokens2

        result["_model"]  = model
        result["_cost"]   = 0.0
        result["_tokens"] = tokens
        return result

    def evaluate_scalp(
        self,
        indicators: dict,
        primary_direction: str,
        setup_type: str,
        local_confidence: int,
        ai_confidence: int,
        ai_reasoning: str,
    ) -> dict:
        """
        Opus evaluates BOTH directions for a quick scalp opportunity.
        Called when Sonnet rejected the primary direction. Opus uses Sonnet's rejection
        reasoning as context — the reasons for rejecting one direction often contain
        the thesis for the opposite direction.

        Returns: {scalp_viable, direction, tp_distance, sl_distance, reasoning, confidence}
        Note: direction may differ from primary_direction — Opus picks the best play.
        """
        indicator_block = _fmt_indicators(indicators)
        opposite_direction = "LONG" if primary_direction == "SHORT" else "SHORT"

        system_prompt = (
            "You are a scalp-trade evaluator for Japan 225 Cash CFD ($1/pt, spread ~7pts).\n"
            "A setup was detected and passed local confidence scoring, but the primary AI\n"
            "(Sonnet) rejected it. Your job: evaluate BOTH directions for a quick scalp.\n\n"
            "CRITICAL INSIGHT: Sonnet's rejection reasoning often contains the opposite thesis.\n"
            "If Sonnet rejected SHORT because 'too oversold, bounce likely' — that IS the LONG case.\n"
            "If Sonnet rejected LONG because 'overbought, distribution' — that IS the SHORT case.\n"
            "Don't treat the directions independently — use the full market picture.\n\n"
            "DECISION FLOW:\n"
            f"1. Consider {primary_direction} scalp: Is there a quick play despite Sonnet's concerns?\n"
            f"   Sometimes Sonnet is right about the swing but wrong about the scalp.\n"
            f"2. Consider {opposite_direction} scalp: Does Sonnet's rejection reasoning\n"
            f"   actually support a scalp in the opposite direction?\n"
            "3. Pick the BEST one (or neither). You must choose ONE direction or reject both.\n\n"
            "SL/TP PLACEMENT FROM STRUCTURE:\n"
            "- SL: Nearest support (LONG) / resistance (SHORT). Use swing lows/highs, BB,\n"
            "  pivot S1-S2/R1-R2, fib levels, PDL/PDH. BOUNDS: 60-120pts.\n"
            "- TP: Nearest obstacle. Use BB mid/upper, EMA50, pivots, VWAP, PDH/PDL, fibs.\n"
            "  BOUNDS: 150-300pts.\n\n"
            "R:R REQUIREMENT: (TP - 7) / (SL + 7) >= 1.5  (7pt spread adjustment)\n\n"
            "RULES:\n"
            "- If Sonnet's rejection is about imminent risk (events, gap), respect it for BOTH.\n"
            "- Oversold RSI<30 + daily bullish = bounce to BB mid is high-probability LONG scalp.\n"
            "- Deeply oversold 4H RSI<25 = snap-back bounce even in bear market.\n"
            "- Extended move (price far from EMA50) = reversion likely toward the mean.\n"
            "- Be decisive. Pick the direction with better R:R and clearer structure.\n"
            "- Explain WHERE SL/TP are placed and WHY."
        )

        user_prompt = (
            f"PRIMARY SETUP: {primary_direction} {setup_type}\n"
            f"LOCAL CONFIDENCE: {local_confidence}% (passed threshold)\n"
            f"SONNET CONFIDENCE: {ai_confidence}% (rejected)\n"
            f"SONNET REASONING: {ai_reasoning}\n\n"
            f"INDICATORS:\n{indicator_block}\n\n"
            f"Evaluate BOTH {primary_direction} and {opposite_direction}. Pick the best scalp.\n"
            f"Output ONLY valid JSON:\n"
            f'{{"scalp_viable": true, "direction": "LONG", "sl_distance": 85, "tp_distance": 200, '
            f'"reasoning": "LONG better because [...]. SL below X, TP at Y", "confidence": 65}}\n'
            f"or\n"
            f'{{"scalp_viable": false, "reasoning": "Neither direction offers good R:R because..."}}'
        )

        raw, tokens = self._run_claude(
            OPUS_MODEL, system_prompt, user_prompt,
            timeout=90, use_opus_agent=False,
        )

        default = {
            "scalp_viable": False,
            "reasoning": "Opus scalp eval returned unparseable output",
        }
        result = _parse_json(raw, default)

        # Enforce bounds and R:R
        if result.get("scalp_viable"):
            sl = result.get("sl_distance", 0)
            tp = result.get("tp_distance", 0)

            # Clamp SL to 60-120
            sl = max(60, min(120, sl)) if sl else 100
            # Clamp TP to 150-300
            tp = max(150, min(300, tp)) if tp else 200

            # Check effective R:R >= 1.5 after spread
            effective_rr = (tp - SPREAD_ESTIMATE) / (sl + SPREAD_ESTIMATE)
            if effective_rr < 1.5:
                result["scalp_viable"] = False
                result["reasoning"] = (
                    f"R:R too low: SL={sl} TP={tp} → effective "
                    f"{effective_rr:.2f} < 1.5 minimum"
                )
            else:
                result["sl_distance"] = sl
                result["tp_distance"] = tp
                result["effective_rr"] = round(effective_rr, 2)

        result["_model"] = OPUS_MODEL
        logger.info(
            f"Opus scalp eval: viable={result.get('scalp_viable')}, "
            f"sl={result.get('sl_distance', 'N/A')}, tp={result.get('tp_distance', 'N/A')}, "
            f"rr={result.get('effective_rr', 'N/A')}, "
            f"reason={result.get('reasoning', '')[:100]}"
        )
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
    except Exception as e:
        logger.debug(f"Failed to load prompt learnings: {e}")
        return ""


def post_trade_analysis(trade: dict, ai_analysis: dict, data_dir: str = None) -> None:
    """Post-trade learning — pure rule-based, no LLM call. Also records Brier score."""
    from pathlib import Path
    data_path = Path(data_dir or "storage/data")
    path = data_path / "prompt_learnings.json"

    pnl          = trade.get("pnl", 0) or 0
    setup_type   = trade.get("setup_type", "unknown")
    session      = trade.get("session", "unknown")
    confidence   = trade.get("confidence", 0) or 0

    # ── Brier Score ───────────────────────────────────────────────────────────
    # Measures calibration: (predicted_probability - actual_outcome)^2
    # Perfect calibration = 0.0; worst = 1.0. Lower is better.
    try:
        brier_path = data_path / "brier_scores.json"
        outcome = 1.0 if pnl > 0 else 0.0
        brier_score = (confidence / 100 - outcome) ** 2

        brier_data = {"scores": [], "summary": {}}
        if brier_path.exists():
            brier_data = json.loads(brier_path.read_text())
        scores = brier_data.get("scores", [])
        scores.append({
            "timestamp": datetime.now().isoformat(),
            "setup_type": setup_type,
            "session": session,
            "confidence": confidence,
            "outcome": outcome,
            "brier_score": round(brier_score, 4),
        })
        scores = scores[-100:]  # keep last 100

        # Recompute summary
        all_bs = [s["brier_score"] for s in scores]
        by_setup, by_session = {}, {}
        for s in scores:
            by_setup.setdefault(s["setup_type"], []).append(s["brier_score"])
            by_session.setdefault(s["session"], []).append(s["brier_score"])
        summary = {
            "mean": round(sum(all_bs) / len(all_bs), 4) if all_bs else 0.0,
            "count": len(all_bs),
            "by_setup": {k: round(sum(v) / len(v), 4) for k, v in by_setup.items()},
            "by_session": {k: round(sum(v) / len(v), 4) for k, v in by_session.items()},
        }
        brier_path.parent.mkdir(parents=True, exist_ok=True)
        brier_path.write_text(json.dumps({"scores": scores, "summary": summary}, indent=2))
        logger.info(f"Brier score: {brier_score:.4f} (mean={summary['mean']:.4f}, n={summary['count']})")
    except Exception as e:
        logger.warning(f"Brier score save failed (non-fatal): {e}")
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
        """Fetch Japan/Nikkei market headlines from Google News RSS (no API key)."""
        try:
            import xml.etree.ElementTree as ET
            # Google News RSS for Japan economy / Nikkei / BOJ
            urls = [
                "https://news.google.com/rss/search?q=Nikkei+225+OR+Japan+economy+OR+BOJ&hl=en&gl=US&ceid=US:en",
            ]
            headlines = []
            for url in urls:
                try:
                    resp = self.client.get(url, timeout=5)
                    if resp.status_code == 200:
                        root = ET.fromstring(resp.text)
                        for item in root.findall(".//item")[:5]:  # top 5 headlines
                            title = item.findtext("title", "")
                            if title:
                                headlines.append(title.strip())
                except Exception:
                    continue
            if not headlines:
                headlines = ["No headlines available"]
            logger.debug(f"News: {len(headlines)} headlines fetched")
            return headlines[:8]  # cap at 8
        except Exception as e:
            logger.warning(f"News fetch failed: {e}")
            return ["News fetch failed"]

    def _get_calendar(self) -> list[dict]:
        """Fetch upcoming economic events from free calendar API."""
        try:
            # Try nager.date for public holidays (Japan = JP)
            today = datetime.now().strftime("%Y-%m-%d")
            year = datetime.now().year
            resp = self.client.get(
                f"https://date.nager.at/api/v3/publicholidays/{year}/JP",
                timeout=5
            )
            events = []
            if resp.status_code == 200:
                for h in resp.json():
                    if h.get("date", "") >= today:
                        events.append({
                            "name": h.get("localName", h.get("name", "Holiday")),
                            "time": h.get("date"),
                            "impact": "MEDIUM",
                        })
                        if len(events) >= 3:
                            break
            return events
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
        """Fetch market Fear & Greed index (alternative.me — crypto-based sentiment proxy)."""
        try:
            resp = self.client.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                if items:
                    score = int(items[0].get("value", 0))
                    logger.debug(f"Fear & Greed: {score}")
                    return score
        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")
        return None

    def close(self):
        self.client.close()
