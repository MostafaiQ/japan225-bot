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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from config.settings import (
    SONNET_MODEL, OPUS_MODEL, SPREAD_ESTIMATE,
    MIN_CONFIDENCE, MIN_CONFIDENCE_SHORT,
)

logger = logging.getLogger(__name__)

CLAUDE_BIN    = "/home/ubuntu/.local/bin/claude"
PROJECT_ROOT  = Path(__file__).parent.parent
CONTEXT_DIR   = PROJECT_ROOT / "storage" / "context"


# ─── Prompt helpers (unchanged from original) ─────────────────────────────────

def build_system_prompt() -> str:
    """Enhanced system prompt: Wyckoff + SMC + VP + setup quality for slow days."""
    return """DECISION FRAME: Approve or reject a trade entry happening in the NEXT 5-30 MINUTES.
Not in the next hour. Not after confirmation. The next candle is the entry candle.
Trade horizon: 1-8 HOURS (15M setup managed by live bot). Not asking about next 15 minutes.
If you need more confirmation → REJECT. Never write "wait for" or "monitor". Decide NOW.

Japan 225 Cash CFD analyst. LONG+SHORT bidirectional. No directional bias.

━━ SETUPS (reference — indicators data decides which fired) ━━
LONG MEAN-REVERSION: bb_mid_bounce(±150 BB_mid,RSI30-65) | bb_lower_bounce(±150 BB_low,RSI20-40,wick≥15) | oversold_reversal(RSI<30,daily bullish,reversal confirm)
LONG MOMENTUM:       momentum_continuation_long(>EMA50+VWAP,HA≥2,RSI45-75) | breakout_long(BB_up/swing_hi,vol≥1.3x,RSI55-75) | vwap_bounce_long(±120 VWAP,>EMA50,RSI40-65) | ema9_pullback_long(±100 EMA9,>EMA50,RSI40-65)
SHORT MEAN-REV(75%): bb_upper_rejection(±150 BB_up,RSI55-75) | ema50_rejection(≤EMA50+150,RSI50-70) | bb_mid_rejection(±150 BB_mid,RSI40-65) | overbought_reversal(RSI>70,daily bearish) | high_volume_distribution(±200 BB_up/swept_hi,vol≥1.4x)
SHORT BREAKDOWN:     breakdown_continuation(>100 below BB_mid,RSI25-45,HA≤-2) | dead_cat_bounce_short(at BB_mid/EMA9 from below,RSI43-62) | bear_flag_breakdown(RSI35-52,vol flag,HA≤-1) | multi_tf_bearish(≥4/5: rsi15m<48,rsi4h<48,daily bear,<EMA50,<VWAP,HA bear)
SHORT MOMENTUM:      momentum_continuation_short(<EMA50+VWAP,HA≤-2,RSI30-55) | vwap_rejection_short_momentum(±120 VWAP from below,<EMA50,RSI35-60)
SESSION SETUPS:      tokyo_gap_fill(±100pt gap from prev close,Tokyo 00-02UTC,RSI30-70) | london_orb(break Asia range,London 08-10UTC,vol≠LOW)
DISABLED: ema50_bounce — do not approve.

━━ WYCKOFF PHASE — detect and trade WITH the phase ━━
ACCUMULATION (smart money buying at lows):
  Signals: price dips quickly rebound | shrinking BB width (coil) | swept_low (Spring) then strong recovery | vol LOW on dips, higher on bounces | equal_lows zones near support
  Trade bias: LONG ONLY (Springs = strong entry). Accumulation Spring = price breaks below support then reverses fast = STRONG LONG signal.
  Counter-bias: Do NOT short Springs — they trap shorts before the markup phase begins.
MARKUP (trending up after accumulation):
  Signals: HH+HL structure | HA bullish streak ≥3 | above EMA50+VWAP | vol expansion on upmoves, contraction on pullbacks | BB width expanding upward
  Trade bias: LONG preferred (momentum_continuation_long, vwap_bounce_long, ema9_pullback_long on dips). Breakouts valid.
  Counter-bias: Avoid shorting pullbacks — treat them as LONG entries.
DISTRIBUTION (smart money selling at highs):
  Signals: wide BB but closes near mid (indecision) | swept_high (UpThrust / UT) then fails | vol HIGH at highs | equal_highs zones near resistance | RSI divergence at highs
  Trade bias: SHORT ONLY (UTs = strong entry). UpThrust = price breaks above resistance then quickly fails = STRONG SHORT signal.
  Counter-bias: Do NOT buy UTs — they trap longs before markdown.
MARKDOWN (trending down after distribution):
  Signals: LH+LL structure | HA bearish streak ≤-3 | below EMA50+VWAP | vol expansion on declines, contraction on bounces | BB width expanding downward
  Trade bias: SHORT preferred (momentum_continuation_short, dead_cat_bounce_short on bounces). Breakdowns valid.
  Counter-bias: Avoid longing bounces — treat them as SHORT entries.

SLOW/CHOPPY MARKET (no clear Wyckoff phase = coil):
  Signals: BB width narrow (bb_width < 200 on 15M) | HA streak near 0 | RSI near 50 | price hugging BB mid | vol consistently LOW
  Action: Lower the bar for counter-trend mean-reversion at band extremes (bb_lower_bounce / bb_upper_rejection).
  Pre-breakout play: If price is compressing in a tight range, identify which side has equal_highs/equal_lows (liquidity pool) — next sweep of that pool = breakout direction signal.
  VP edge: In slow markets, if price is at the edge of a multi-day Value Area (near VAH or VAL), that alone is high-probability S/R even without a clean 15M setup — lower confidence threshold by 5pts.

━━ VOLUME PROFILE — how to use POC/VAH/VAL ━━
POC (Point of Control) = highest volume price = equilibrium. Price at POC = direction-neutral, wait for break.
VAH (Value Area High) = upper edge of fair value. Price at VAH from below = resistance → SHORT opportunity.
VAL (Value Area Low) = lower edge of fair value. Price at VAL from above = support → LONG opportunity.
INSIDE value area: price moves slowly, mean-reverts toward POC. Reduce momentum confidence.
OUTSIDE value area (above VAH or below VAL): two scenarios:
  - Rejection (price quickly returns inside): high-probability reversal trade back toward POC.
  - Acceptance (price stays outside for 2+ bars): breakout likely to continue. Trade WITH the move.
LVN (Low Volume Node = thin area between VAL and VAH): price moves fast through it — widen TP.
Volume Profile + Wyckoff: VAL = Accumulation support zone. VAH = Distribution resistance zone.

━━ SMC CONCEPTS (Smart Money) ━━
ORDER BLOCK (OB): Last bearish candle before bullish impulse = demand OB (LONG entry on retest).
  Last bullish candle before bearish impulse = supply OB (SHORT entry on retest).
  OB + FVG overlap = highest conviction entry zone.
FVG (Fair Value Gap): Unfilled imbalance zone. Price retraces to fill FVGs during pullbacks.
  fvg_bullish = demand zone (support, LONG). fvg_bearish = supply zone (resistance, SHORT).
  FVGs are SOFT S/R — they get filled during reversals. Do NOT treat as hard walls.
LIQUIDITY SWEEPS: swept_low=T → smart money grabbed buy-stop liquidity below equal_lows → bullish reversal.
  swept_high=T → grabbed sell-stop liquidity above equal_highs → bearish reversal.
  Sweep + OB + FVG confluence = highest conviction entry (Wyckoff Spring/UT equivalent).
BOS vs CHoCH: Break of Structure (new HH in uptrend = BOS = continuation). Change of Character (LL in uptrend = CHoCH = reversal).
  Use HA streaks + pivot_high/pivot_low to detect: pivot_low broken = CHoCH bearish. pivot_high broken = CHoCH bullish.
SMC + Wyckoff: Spring = sweep of equal_lows + bullish FVG + demand OB = textbook Accumulation entry.
  UT = sweep of equal_highs + bearish FVG + supply OB = textbook Distribution entry.

━━ INDICATOR QUICK-REFERENCE ━━
ATR RULE: If 15M ATR14 > 120pts → VOLATILE. SL must be ≥1× ATR. Tokyo ATR often 140-220pts.
VOLUME: HIGH(>1.5x)=conviction. LOW(<0.7x)=caution but not auto-reject (Tokyo inherently lower).
SWING: dist_swing_hi <200pts → TP obstacle. dist_swing_lo <100pts → SL anchor.
HA: ha_bullish=T → buying pressure. ha_streak≥3 → strong momentum. streak≤-3 → strong sell.
VWAP: above=premium(SHORT bias). below=discount(LONG bias). Key intraday mean.
FIBO: fib_near = nearest S/R level. Use for SL/TP placement.
SWEEP: swept_low=T → liquidity grab → bullish. swept_high=T → bearish.
PIVOT: PP/R1-R3/S1-S3 daily. S1/S2=LONG support. R1/R2=SHORT resistance.
CANDLE: Strong pattern at key level = high conviction. BODY: expanding=momentum, contracting=exhaustion.
R:R (MANDATORY): effective_rr = (TP_dist - 7) / (SL_dist + 7) ≥ 1.5. Compute before approving. REJECT if below.

━━ CONFIDENCE (12 criteria, base 30, cap 100) ━━
daily_trend | entry_at_tech_level | rsi_15m_in_range | tp_viable | price_structure | macro_aligned
no_event_1hr | no_friday_monthend | volume | trend_4h | ha_aligned | entry_quality

━━ SETUP-CLASS RULES (apply without exception) ━━
MEAN-REVERSION LONGS (bb_lower_bounce, oversold_reversal): bearish HA + below EMA50 + 4H bearish = EXPECTED (setup trigger). Approve if daily bullish + any reversal signal. Only reject: imminent event, massive breakdown volume, zero reversal signal.
MEAN-REVERSION SHORTS (overbought_reversal, bb_mid_rejection): bullish HA + above EMA50 + 4H bullish = EXPECTED. Approve if daily bearish + any reversal signal. Only reject: imminent event, massive breakout volume, zero reversal signal.
MOMENTUM LONGS (momentum_continuation_long, breakout_long, vwap_bounce_long, ema9_pullback_long): RSI 60-75 = HEALTHY (not overbought). Above BB mid = EXPECTED. HA≥2 = confirmation. Approve if >EMA50, >VWAP, HA bull, RSI45-75. Reject only: 4H RSI>68 with exhaustion OR price >800pts above open on extreme day.
MOMENTUM SHORTS (momentum_continuation_short, breakdown_continuation, bear_flag_breakdown, multi_tf_bearish): RSI 30-55 = HEALTHY (not oversold). Daily "bullish" during big selloff = EXPECTED (EMA lags). Approve if 4H+15M aligned bearish (HA≤-2, <EMA50, RSI<50). Reject only: RSI<25 with reversal candle OR major support (BB lower/S2/S3) OR volume LOW.

━━ HARD PROHIBITIONS ━━
OVERSOLD SHORTING: 4H RSI < 32 + exhaustion (spinning_top/doji/contracting bodies) → REJECT SHORT.
OVERBOUGHT LONGING: 4H RSI > 68 + exhaustion → REJECT LONG.
EVENTS: No trade within 60min of HIGH-impact event.
EXTREME DAY (range > 1000pts):
  CRASH (price lower half): No short into oversold. LONG requires multi-TF confirm + hard support.
  BULL (price upper half): No long into overbought. SHORT requires multi-TF confirm + hard resistance.
  4+ warnings in analysis → confidence < 70%. DEFAULT REJECT unless all TFs + volume + pattern align.

━━ WARNINGS & COUNTER SIGNAL ━━
WARNINGS: Each warning = -3-5% confidence. 4+ warnings → confidence < 70%. 6+ → confidence < 60%.
QUICK REJECT: ≥4 criteria fail AND volume LOW AND no macro catalyst → setup_found=false.
COUNTER SIGNAL: On rejection, check opposite direction. Set counter_signal="LONG"/"SHORT" if concrete structural evidence exists (swept_low + reversal pattern = LONG; swept_high + distribution = SHORT). Null if rejected for quality reasons only.

━━ EXIT & REASONING ━━
EXIT: +150pts → SL to BE+10. TP=400pts. 75%TP in <2h → trail@150pts.
REASONING_SHORT: ~3-5 sentences (~400-500 chars). Format: "[APPROVE/REJECT] [dir]. [Structure]. [Decisive signal]. [Key risk/edge]. [Final call]."
  Example: "REJECT SHORT. D1/4H/15M bearish but CRASH DAY 1794pts overrides — crash rules require bounce short not continuation. swept_low + bullish_engulfing at lows = reversal, not continuation. 4H RSI 41 near oversold = squeeze risk. Wait for dead-cat then SHORT from higher."
  reasoning field holds full analysis. reasoning_short is the log summary.

━━ FATAL FLAWS — APPROVE UNLESS ONE EXISTS ━━
1. SL direction wrong (LONG SL above entry, SHORT SL below entry)
2. Effective R:R < 1.5 after spread
3. HIGH-impact event within 60 minutes
4. 4H RSI < 32 on a SHORT trade
5. 4H RSI > 68 on a LONG trade
6. Counter-trend on extreme day (range > 1000pts) with confidence < 85%
7. BOTH daily_trend AND entry_level fail simultaneously (no macro alignment AND not at tech level)

If NONE of these apply → APPROVE. No other reason to reject a setup that has already passed local scoring."""


def _fmt_indicators(indicators: dict) -> str:
    """Compact pipe-format indicator table."""
    TF_KEYS = [
        ("D1",  ["daily", "d1", "1d"]),
        ("4H",  ["4h", "tf_4h", "4hour", "h4"]),
        ("15M", ["15m", "tf_15m", "15min", "m15"]),
        ("5M",  ["5m", "tf_5m", "5min", "m5"]),
    ]
    # Extract live price from 15M or 5M for D1 EMA50 comparison
    _live_price = None
    for _k in ("m15", "15m", "tf_15m", "15min", "m5", "5m", "tf_5m", "5min"):
        _tf = indicators.get(_k)
        if isinstance(_tf, dict) and _tf.get("price"):
            _live_price = _tf["price"]
            break

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
        bbu  = tf.get("bollinger_upper", tf.get("bb_upper", "?"))
        bbm  = tf.get("bollinger_mid", tf.get("bb_mid", "?"))
        bbl  = tf.get("bollinger_lower", tf.get("bb_lower", "?"))
        vol  = tf.get("volume_signal", "")
        vrat = tf.get("volume_ratio", "")
        sh   = tf.get("swing_high_20", "")
        sl   = tf.get("swing_low_20", "")
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
        # D1 special: annotate with live price vs D1 EMA50 (D1 p= is PREV CLOSE, not current price)
        if label == "D1" and _live_price and e50 and e50 != "?":
            try:
                live_vs_ema50 = float(_live_price) - float(e50)
                side = "ABOVE" if live_vs_ema50 >= 0 else "BELOW"
                parts[0] += f"(PREV_CLOSE) | LIVE={_live_price:.0f}({side} D1EMA50 by {abs(live_vs_ema50):.0f}pts)"
            except (TypeError, ValueError):
                pass
        # EMA200 only useful for D1 and 4H (15M/5M: EMA200 on short TF is just noise)
        if e200 and label in ("D1", "4H"):
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
        if ha is not None:
            parts.append(f"ha={'bull' if ha else 'bear'}({hast})")
        if fvg_bull or fvg_bear:
            fvg_type = "bull" if fvg_bull else "bear"
            parts.append(f"fvg={fvg_type}" + (f"@{fvg_lvl}" if fvg_lvl else ""))
        # Fibonacci: show 2 nearest levels only (1 sup below, 1 res above) — full grid is noise
        fib_data = tf.get("fibonacci", {})
        if fib_data and p != "?":
            try:
                fp = float(p)
                fib_above, fib_below = None, None
                for lvl in ("fib_236", "fib_382", "fib_500", "fib_618", "fib_786"):
                    val = fib_data.get(lvl)
                    if not val:
                        continue
                    if val > fp and (fib_above is None or val < fib_above[0]):
                        fib_above = (val, lvl.split("_")[1])
                    elif val < fp and (fib_below is None or val > fib_below[0]):
                        fib_below = (val, lvl.split("_")[1])
                fib_parts = []
                if fib_below:
                    fib_parts.append(f"SUP:{fib_below[1]}={fib_below[0]:.0f}({fib_below[0]-fp:+.0f})")
                if fib_above:
                    fib_parts.append(f"RES:{fib_above[1]}={fib_above[0]:.0f}({fib_above[0]-fp:+.0f})")
                if fib_parts:
                    parts.append(f"fib=[{','.join(fib_parts)}]")
            except (TypeError, ValueError):
                if fibn:
                    parts.append(f"fib={fibn}")
        elif fibn:
            parts.append(f"fib={fibn}")
        # BB width (volatility proxy)
        bbw = tf.get("bb_width")
        if bbw and isinstance(bbw, (int, float)):
            parts.append(f"bb_width={bbw:.0f}")
        # ATR(14) — true per-candle volatility, critical for SL/TP sizing
        atr_val = tf.get("atr")
        if atr_val and isinstance(atr_val, (int, float)) and atr_val > 0:
            parts.append(f"ATR14={atr_val:.0f}pts")
        if swlo or swhi:
            parts.append(f"sweep={'low' if swlo else 'high'}")
        # Most recent swing pivot high/low — the last real price reversal on the chart
        # Shows: level, how many candles ago it formed, distance from current price
        ph = tf.get("pivot_high")
        ph_age = tf.get("pivot_high_age")
        pl = tf.get("pivot_low")
        pl_age = tf.get("pivot_low_age")
        if ph and pl and p and p != "?":
            try:
                fp = float(p)
                parts.append(
                    f"last_pivot=H{ph:.0f}({ph_age}c,+{ph-fp:.0f}pts)"
                    f"/L{pl:.0f}({pl_age}c,-{fp-pl:.0f}pts)"
                )
            except (TypeError, ValueError):
                pass
        # Candlestick pattern
        cp_name = tf.get("candlestick_pattern")
        cp_dir  = tf.get("candlestick_direction")
        cp_str  = tf.get("candlestick_strength")
        if cp_name:
            parts.append(f"candle={cp_name}({cp_dir},{cp_str})")
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

    # ── Market Structure Block ────────────────────────────────────────────────
    # Pull from 15M timeframe (most relevant for entry context)
    tf15 = indicators.get("m15") or indicators.get("tf_15m") or indicators.get("15m") or indicators.get("15M") or {}

    ms_lines = []

    # Anchored VWAPs
    avd = tf15.get("anchored_vwap_daily")
    avw = tf15.get("anchored_vwap_weekly")
    price_now = tf15.get("price")
    if avd or avw:
        avd_str = f"{avd:.0f}" if avd else "n/a"
        avw_str = f"{avw:.0f}" if avw else "n/a"
        avd_dist = f"({'+' if price_now and price_now > avd else '-'}{abs(price_now - avd):.0f}pts)" if avd and price_now else ""
        avw_dist = f"({'+' if price_now and price_now > avw else '-'}{abs(price_now - avw):.0f}pts)" if avw and price_now else ""
        ms_lines.append(f"  Anchored VWAP: Daily={avd_str}{avd_dist} | Weekly={avw_str}{avw_dist}")

    # Volume Profile
    poc = tf15.get("volume_poc")
    vah = tf15.get("volume_vah")
    val = tf15.get("volume_val")
    if poc:
        poc_dist = f"({'+' if price_now and price_now > poc else '-'}{abs(price_now - poc):.0f}pts)" if price_now else ""
        inside_va = val and vah and val <= price_now <= vah if price_now else None
        va_str = "INSIDE value area" if inside_va else ("ABOVE value area" if price_now and vah and price_now > vah else "BELOW value area" if price_now and val and price_now < val else "")
        ms_lines.append(f"  Volume Profile: POC={poc:.0f}{poc_dist} | VAH={f'{vah:.0f}' if vah else 'n/a'} | VAL={f'{val:.0f}' if val else 'n/a'} | {va_str}")

    # PDH/PDL (daily)
    snap = indicators.get("indicators_snapshot") or {}
    pdh_d = snap.get("pdh_daily") or tf15.get("pdh_daily")
    pdl_d = snap.get("pdl_daily") or tf15.get("pdl_daily")
    pdh_swept = snap.get("pdh_swept", False)
    pdl_swept = snap.get("pdl_swept", False)
    if pdh_d or pdl_d:
        pdh_str = f"{pdh_d:.0f}{'↑SWEPT' if pdh_swept else ''}" if pdh_d else "n/a"
        pdl_str = f"{pdl_d:.0f}{'↓SWEPT' if pdl_swept else ''}" if pdl_d else "n/a"
        ms_lines.append(f"  PDH/PDL (Daily): {pdh_str} / {pdl_str}")

    # Session Open + Asia Range + Gap
    sess_open = snap.get("session_open")
    asia_h = snap.get("asia_high")
    asia_l = snap.get("asia_low")
    gap_pts = snap.get("gap_pts")
    if sess_open and price_now:
        dist_from_open = price_now - sess_open
        ms_lines.append(f"  Session Open: {sess_open:.0f} (price {'+' if dist_from_open >= 0 else ''}{dist_from_open:.0f}pts from open)")
    if asia_h and asia_l:
        in_asia = asia_l <= price_now <= asia_h if price_now else None
        asia_pos = "inside" if in_asia else ("above" if price_now and price_now > asia_h else "below")
        ms_lines.append(f"  Asia Range: {asia_h:.0f}–{asia_l:.0f} (price {asia_pos})")
    if gap_pts is not None:
        ms_lines.append(f"  Gap from prev close: {'+' if gap_pts >= 0 else ''}{gap_pts:.0f}pts {'(gap up)' if gap_pts > 30 else '(gap down)' if gap_pts < -30 else '(flat open)'}")

    # Equal Highs/Lows zones
    eq_highs = tf15.get("equal_highs_zones", [])
    eq_lows  = tf15.get("equal_lows_zones",  [])
    if eq_highs:
        ms_lines.append(f"  Equal Highs (liquidity): {', '.join(f'{z:.0f}' for z in eq_highs[:3])}")
    if eq_lows:
        ms_lines.append(f"  Equal Lows  (liquidity): {', '.join(f'{z:.0f}' for z in eq_lows[:3])}")

    if ms_lines:
        lines.append("\nMARKET STRUCTURE:")
        lines.extend(ms_lines)

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
    # JPY direction hint: higher USD/JPY = weaker JPY = bullish Nikkei; lower = stronger JPY = bearish
    jpy_hint = ""
    try:
        jpy_f = float(jpy)
        if jpy_f > 152:
            jpy_hint = " [JPY WEAK → Nikkei tailwind]"
        elif jpy_f < 148:
            jpy_hint = " [JPY STRONG → Nikkei headwind]"
        else:
            jpy_hint = " [JPY neutral 148-152]"
    except (TypeError, ValueError):
        pass
    high_cal = [e for e in cal if isinstance(e, dict) and e.get("impact") == "HIGH"][:3]
    med_cal  = [e for e in cal if isinstance(e, dict) and e.get("impact") == "MEDIUM"][:3]
    news_str = " | ".join(str(n)[:70] for n in (news[:2] if news else []))
    lines = [f"USD/JPY: {jpy}{jpy_hint} | VIX: {vix}"]
    if news_str:
        lines.append(f"News: {news_str}")
    lines.append(f"Calendar HIGH: {high_cal if high_cal else 'none next 8h'}")
    if med_cal:
        lines.append(f"Calendar MEDIUM: {med_cal}")
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
    open_positions_context: dict = None,
) -> str:
    from config.settings import display_now, DISPLAY_TZ_LABEL
    now = display_now().strftime(f"%Y-%m-%d %H:%M {DISPLAY_TZ_LABEL}")

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

    # --- Open positions / risk context ---
    position_block = ""
    if open_positions_context:
        count = open_positions_context.get("count", 0)
        dirs = open_positions_context.get("directions", [])
        daily_pnl = open_positions_context.get("daily_pnl")
        dirs_str = ", ".join(dirs) if dirs else "none"
        pnl_str = f" | Today P&L: ${daily_pnl:+.2f}" if daily_pnl is not None else ""
        position_block = (
            f"\nPORTFOLIO STATE: {count} open position(s) [{dirs_str}]{pnl_str}\n"
            f"  [Factor this into your decision — adding another position in the same direction "
            f"increases correlated risk. Already losing today = be more selective.]\n"
        )

    # Secondary setup block — removed from Sonnet prompt to prevent contamination of primary analysis.
    # Opus receives secondary context when evaluating opposite direction after Sonnet rejection.
    secondary_block = ""

    local_conf_block = ""
    if local_confidence:
        criteria = local_confidence.get("criteria", {})
        passed = [k for k, v in criteria.items() if v]
        failed = [k for k, v in criteria.items() if not v]
        local_conf_block = (
            f"\nLOCAL SCORE: {local_confidence.get('score', '?')}% "
            f"({local_confidence.get('passed_criteria', '?')}/{local_confidence.get('total_criteria', 10)}) "
            f"[CRITERIA-BASED — not probability of profit. "
            f"Historical win rate at 70-79%: ~43%. At 80-89%: ~34%. At 90-100%: ~46%. "
            f"Treat as setup quality signal, not edge certainty.] "
            f"✓{','.join(passed)} ✗{','.join(failed)}\n"
        )

    # --- Build failed criteria block (separate expected vs unexpected by setup type) ---
    SETUP_EXPECTED_FAILURES = {
        'bollinger_lower_bounce':      {'daily_trend', 'price_structure', 'trend_4h', 'ha_aligned', 'entry_timing'},
        'bollinger_mid_bounce':        {'ha_aligned'},
        'oversold_reversal':           {'daily_trend', 'price_structure', 'trend_4h', 'ha_aligned'},
        'extreme_oversold_reversal':   {'daily_trend', 'price_structure', 'trend_4h', 'ha_aligned', 'entry_timing'},
        'bb_upper_rejection':          {'daily_trend', 'price_structure', 'trend_4h', 'ha_aligned', 'entry_timing'},
        'overbought_reversal':         {'daily_trend', 'price_structure', 'trend_4h', 'ha_aligned'},
        'breakdown_continuation':      {'daily_trend'},
        'momentum_continuation_short': {'daily_trend'},
        'bear_flag_breakdown':         {'daily_trend'},
        'multi_tf_bearish':            {'daily_trend'},
        'dead_cat_bounce_short':       {'daily_trend', 'ha_aligned'},
        'ema50_rejection':             {'ha_aligned'},
    }
    setup_type_ctx = market_context.get("prescreen_setup_type", "")
    expected_fails = SETUP_EXPECTED_FAILURES.get(setup_type_ctx, set())

    failed_block = ""
    if failed_criteria:
        unexpected = [c for c in failed_criteria if c not in expected_fails]
        expected   = [c for c in failed_criteria if c in expected_fails]
        if expected:
            failed_block += (
                f"\nEXPECTED FAILURES for {setup_type_ctx} (counter-trend by design — DO NOT penalize):\n"
                + "\n".join(f"  ✓ {c} (expected counter-trend fail — see setup-class rules)" for c in expected) + "\n"
            )
        if unexpected:
            failed_block += (
                f"\nUNEXPECTED FAILURES (evaluate these):\n"
                + "\n".join(f"  ✗ {c}" for c in unexpected) + "\n"
            )

    role_block = (
        f"{failed_block}"
        "\nBefore outputting JSON, answer these 5 gates IN ORDER, then COMMIT:\n"
        "  1. HARD GATES: Does 4H RSI / extreme day / upcoming event disqualify this trade? "
        "If yes → REJECT immediately. No further analysis needed.\n"
        "  2. PHASE: From the WYCKOFF/SMC CONTEXT hint above, does the phase support this setup class? "
        "Accept the hint unless a specific indicator directly contradicts it.\n"
        "  3. TRIGGER QUALITY: Is the setup trigger clean at a real S/R level? "
        "(check: price proximity, HA direction, volume, FVG/sweep confluence)\n"
        "  4. R:R: Compute effective_rr = (TP_dist - 7) / (SL_dist + 7). Is it ≥ 1.5?\n"
        "  5. COMMIT: Write APPROVE or REJECT on the first line of your reasoning. "
        "If borderline (72-86%): name the SINGLE decisive factor, then commit. "
        "NEVER write 'wait for' or 'need more' — this is a NOW decision.\n"
        "     SLOW DAY CHECK: If BB width narrow + HA near 0 + RSI near 50 = coil market. "
        "Lower bar for band-edge mean-reversion.\n"
    )

    # Inject prompt learnings from closed trades (auto-updated feedback loop)
    learnings_block = load_prompt_learnings()
    learnings_str = f"\n{learnings_block}\n" if learnings_block else ""

    # Compute intraday range from daily TF data + detect crash vs rally
    from config.settings import EXTREME_DAY_RANGE_PTS
    daily_tf = indicators.get("daily") or indicators.get("d1") or {}
    daily_high = daily_tf.get("high", 0)
    daily_low = daily_tf.get("low", 0)
    daily_price = daily_tf.get("price", daily_tf.get("close", 0))
    daily_range = daily_high - daily_low if daily_high and daily_low else 0
    extreme_day = daily_range > EXTREME_DAY_RANGE_PTS
    # Detect direction: price in lower 40% = crash, upper 40% = rally
    if extreme_day and daily_high and daily_low and daily_price:
        midpoint = (daily_high + daily_low) / 2
        if daily_price < midpoint:
            extreme_label = "CRASH DAY (bearish)"
        else:
            extreme_label = "BULL DAY (bullish)"
    else:
        extreme_label = ""

    range_block = f"\nMARKET REGIME: Intraday range={daily_range:.0f}pts (H={daily_high:.0f} L={daily_low:.0f})"
    if extreme_day:
        range_block += f" *** {extreme_label} — EXTREME VOLATILITY ***"

    # ── Wyckoff Phase Inference (from live indicators — pre-computed hint for AI) ──
    _tf15 = indicators.get("m15") or indicators.get("tf_15m") or indicators.get("15m") or {}
    _tf4h = indicators.get("4h") or indicators.get("tf_4h") or {}
    _ha_streak_15m = _tf15.get("ha_streak", 0) or 0
    _ha_streak_4h  = _tf4h.get("ha_streak", 0) or 0
    _bbw_15m = _tf15.get("bb_width")
    _vol_15m = _tf15.get("volume_signal", "")
    _swept_lo = _tf15.get("swept_low", False)
    _swept_hi = _tf15.get("swept_high", False)
    _above_vwap_15m = _tf15.get("above_vwap")
    _above_ema50_15m = None
    _p15 = _tf15.get("price")
    _e50_15m = _tf15.get("ema_50") or _tf15.get("ema50")
    if _p15 and _e50_15m:
        try:
            _above_ema50_15m = float(_p15) > float(_e50_15m)
        except (TypeError, ValueError):
            pass
    _poc = _tf15.get("volume_poc")
    _vah = _tf15.get("volume_vah")
    _val_vp = _tf15.get("volume_val")

    # Phase detection heuristic (for pre-computed hint — AI still reasons independently)
    _wyckoff_signals = []
    _wyckoff_phase = "UNDETERMINED"
    _wyckoff_bias = "neutral"
    if _ha_streak_15m >= 3 and _above_vwap_15m and _above_ema50_15m:
        _wyckoff_phase = "MARKUP"
        _wyckoff_bias = "LONG preferred"
    elif _ha_streak_15m <= -3 and _above_vwap_15m is False and _above_ema50_15m is False:
        _wyckoff_phase = "MARKDOWN"
        _wyckoff_bias = "SHORT preferred"
    elif _swept_lo and (_ha_streak_15m is not None and _ha_streak_15m >= -1):
        _wyckoff_phase = "ACCUMULATION (Spring detected)"
        _wyckoff_bias = "LONG — Spring signal"
    elif _swept_hi and (_ha_streak_15m is not None and _ha_streak_15m <= 1):
        _wyckoff_phase = "DISTRIBUTION (UpThrust detected)"
        _wyckoff_bias = "SHORT — UpThrust signal"
    elif _bbw_15m and isinstance(_bbw_15m, (int, float)) and _bbw_15m < 200 and abs(_ha_streak_15m) <= 1:
        _wyckoff_phase = "COIL/ACCUMULATION (tight range)"
        _wyckoff_bias = "band-edge mean-reversion or pre-breakout"

    # VP position hint
    _vp_hint = ""
    if _poc and _p15:
        try:
            _fp = float(_p15)
            _poc_f = float(_poc)
            if _vah and _val_vp:
                _vah_f = float(_vah)
                _val_f = float(_val_vp)
                if _fp > _vah_f:
                    _vp_hint = f"ABOVE value area (VAH={_vah_f:.0f}) — rejection→SHORT or acceptance→continuation"
                elif _fp < _val_f:
                    _vp_hint = f"BELOW value area (VAL={_val_f:.0f}) — rejection→LONG or acceptance→continuation"
                elif abs(_fp - _poc_f) < 50:
                    _vp_hint = f"AT POC ({_poc_f:.0f}) — equilibrium, wait for break"
                elif _fp > _poc_f:
                    _vp_hint = f"INSIDE VA above POC ({_poc_f:.0f}) — mild LONG bias, slow movement"
                else:
                    _vp_hint = f"INSIDE VA below POC ({_poc_f:.0f}) — mild SHORT bias, slow movement"
            else:
                dist_poc = _fp - _poc_f
                _vp_hint = f"POC at {_poc_f:.0f} ({'+' if dist_poc >= 0 else ''}{dist_poc:.0f}pts away)"
        except (TypeError, ValueError):
            pass

    wyckoff_block = (
        f"\nWYCKOFF/SMC CONTEXT:"
        f"\n  Phase hint:  {_wyckoff_phase} | Bias: {_wyckoff_bias}"
        f"\n  HA streaks:  15M={_ha_streak_15m} | 4H={_ha_streak_4h}"
        f"\n  Sweeps:      {'swept_LOW (Spring/bullish reversal)' if _swept_lo else 'swept_HIGH (UpThrust/bearish reversal)' if _swept_hi else 'none'}"
        + (f"\n  VP position: {_vp_hint}" if _vp_hint else "")
        + (f"\n  BB width:    {_bbw_15m:.0f}pts (15M) — {'COIL (<200 = tight range)' if isinstance(_bbw_15m, (int, float)) and _bbw_15m < 200 else 'normal/expanding'}" if _bbw_15m else "")
        + "\n  [Use this hint as your starting phase. Override only if a specific indicator directly contradicts it.]\n"
    )

    # Time-of-day context
    _utc_now = display_now().astimezone(timezone.utc) if hasattr(display_now(), 'astimezone') else datetime.now(timezone.utc)
    _hour_utc = _utc_now.hour
    _min_utc = _utc_now.minute
    _session_name = market_context.get("session_name", "?")
    _mins_into_session = None
    _session_char = ""
    _session_starts = {"Tokyo": 0, "London": 8, "New York": 16}
    if _session_name in _session_starts:
        _sess_start_h = _session_starts[_session_name]
        _mins_into_session = (_hour_utc - _sess_start_h) * 60 + _min_utc
        if _session_name == "Tokyo" and _mins_into_session < 30:
            _session_char = " ⚠ OPENING 30MIN (breakout bias, wide spread risk, widen SL)"
        elif _session_name == "Tokyo" and _mins_into_session > 120:
            _session_char = " (late Tokyo — mean-reversion bias, low volume)"
        elif _session_name == "London" and _mins_into_session < 90:
            _session_char = " ⚡ LONDON OPEN (high quality — directional moves, tight spread)"
        elif _session_name == "New York" and _mins_into_session < 60:
            _session_char = " (NY open — US macro correlation active)"
    _time_context = f"SESSION: {_session_name}{_session_char}"
    if _mins_into_session is not None:
        _time_context += f" | {_mins_into_session}min into session"

    return (
        f"Japan 225 CFD analysis — {now}\n"
        f"{_time_context}\n"
        f"{position_block}"
        f"{prescreen_block}{secondary_block}{local_conf_block}"
        f"\nTIMEFRAME SNAPSHOT:\n{_fmt_indicators(indicators)}\n"
        f"{range_block}\n"
        f"{wyckoff_block}"
        f"\nRECENT SCANS (last 5):\n{_fmt_recent_scans(recent_scans)}\n"
        f"\nRECENT TRADES (last 5):\n{_fmt_recent_trades(recent_trades or [])}\n"
        f"\nMARKET CONTEXT: session={market_context.get('session_name','?')} | "
        f"trading_mode={market_context.get('trading_mode','?')}\n"
        f"\nWEB RESEARCH:\n{_fmt_web_research(web_research)}\n"
        + (("\n" + live_edge_block) if live_edge_block else "")
        + learnings_str
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
                    timeout: int = 180, **kwargs) -> tuple[str, dict]:
        """
        Invoke Claude Code CLI with OAuth credentials (no API key).
        Combines system + user prompt so Claude has full context in --print mode.
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

        # --tools "" disables all tools → Sonnet responds directly from prompt data (no file reads/commands)
        # This cuts response time from 60-180s to 10-30s by eliminating CLAUDE.md loading + tool calls.
        effort = kwargs.get("effort", "low")
        cmd = [CLAUDE_BIN, "--model", model, "--print", "--dangerously-skip-permissions",
               "--no-session-persistence", "--effort", effort, "--tools", ""]

        # Write stdout to a unique temp file so the result survives bot restart.
        # With KillMode=process + start_new_session, the Claude subprocess
        # continues after bot is killed and finishes writing to this file.
        pending_file = PROJECT_ROOT / "storage" / "data" / f"ai_pending_{uuid.uuid4().hex[:8]}.txt"
        start = time.time()
        try:
            with open(pending_file, "w") as stdout_f:
                result = subprocess.run(
                    cmd,
                    input=full_prompt,
                    stdout=stdout_f,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    env=env,
                    cwd=str(PROJECT_ROOT),
                    start_new_session=True,
                )
            elapsed = time.time() - start
            output = pending_file.read_text().strip() if pending_file.exists() else ""
            pending_file.unlink(missing_ok=True)
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
        open_positions_context: dict = None,
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
            open_positions_context=open_positions_context,
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
        open_positions_context: dict = None,
    ) -> dict:
        user_prompt = build_scan_prompt(
            indicators, recent_scans, market_context, web_research,
            prescreen_direction=prescreen_direction,
            local_confidence=local_confidence,
            live_edge_block=live_edge_block,
            failed_criteria=failed_criteria,
            recent_trades=recent_trades,
            open_positions_context=open_positions_context,
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
            f'"effective_rr": 0.0, '
            f'"key_levels": {{"support": [], "resistance": []}}, '
            f'"trend_observation": "...", "warnings": [], "edge_factors": [], '
            f'"counter_signal": null, "counter_reasoning": null, '
            f'"reasoning_short": "compact 3-5 sentence verdict covering structure + signal + risk + call"}}'
        )

        system_prompt = build_system_prompt()
        raw, tokens = self._run_claude(model, system_prompt, user_prompt, timeout=180)

        default = {
            "setup_found": False,
            "confidence": 0,
            "reasoning": "Parse error — CLI returned unparseable output",
            "confidence_breakdown": {},
            "warnings": [],
            "edge_factors": [],
        }
        result = _parse_json(raw, default)

        # Retry once on parse error — use normal effort (low effort can produce incomplete JSON)
        if not raw.strip() or (result.get("reasoning", "").startswith("Parse error") and not result.get("setup_found")):
            logger.warning("AI returned empty/unparseable output — retrying with normal effort")
            raw2, tokens2 = self._run_claude(model, system_prompt, user_prompt,
                                             timeout=180, effort="medium")
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
        recent_opus_decision: dict = None,
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
            "(Sonnet) rejected it.\n"
            "Your job: evaluate BOTH directions for a quick scalp.\n\n"
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
            "ATR VOLATILITY RULE: ATR14 is in the indicators. If 15M ATR14 > 120pts, price moves\n"
            "  that many points PER CANDLE on average. A 60-80pt SL will be eaten by noise.\n"
            "  In volatile markets: push SL toward the upper bound (120pts), widen TP accordingly.\n"
            "  Tokyo session regularly shows ATR 140-220pts — account for this in SL placement.\n\n"
            "R:R REQUIREMENT: (TP - 7) / (SL + 7) >= 1.5  (7pt spread adjustment)\n\n"
            "RULES:\n"
            "- If Sonnet's rejection is about imminent risk (events, gap), respect it for BOTH.\n"
            "- Oversold RSI<30 + daily bullish = bounce to BB mid is high-probability LONG scalp.\n"
            "- Deeply oversold 4H RSI<25 = snap-back bounce even in bear market.\n"
            "- Extended move (price far from EMA50) = reversion likely toward the mean.\n"
            "- Be decisive. Pick the direction with better R:R and clearer structure.\n"
            "- Explain WHERE SL/TP are placed and WHY."
        )

        context_block = (
            f"SONNET CONFIDENCE: {ai_confidence}% (rejected)\n"
            f"SONNET REASONING: {ai_reasoning}\n\n"
        )

        # Directional consistency: show recent Opus decision to prevent flip-flopping
        consistency_block = ""
        if recent_opus_decision:
            prev_dir = recent_opus_decision.get("direction", "?")
            prev_viable = recent_opus_decision.get("viable", False)
            prev_reason = recent_opus_decision.get("reasoning", "")[:200]
            prev_conf = recent_opus_decision.get("confidence", 0)
            # Compute actual elapsed time
            try:
                prev_ts = datetime.fromisoformat(recent_opus_decision["timestamp"])
                elapsed_min = int((datetime.now() - prev_ts).total_seconds() / 60)
            except Exception:
                elapsed_min = 0
            if prev_viable:
                consistency_block = (
                    f"\nYOUR PREVIOUS CALL ({elapsed_min} min ago): {prev_dir} scalp, {prev_conf}% confidence.\n"
                    f"Reasoning: {prev_reason}\n"
                    f"CONSISTENCY RULE: Only flip direction if there is a CLEAR structural shift "
                    f"(broken support/resistance, new candle pattern, RSI divergence crossing threshold). "
                    f"Do NOT flip just because indicators moved a few points. Conviction matters.\n\n"
                )
            else:
                consistency_block = (
                    f"\nYOUR PREVIOUS CALL ({elapsed_min} min ago): Rejected both directions.\n"
                    f"Reasoning: {prev_reason}\n"
                    f"Only approve now if conditions MATERIALLY changed.\n\n"
                )

        user_prompt = (
            f"PRIMARY SETUP: {primary_direction} {setup_type}\n"
            f"LOCAL CONFIDENCE: {local_confidence}% (passed threshold)\n"
            f"{context_block}"
            f"{consistency_block}"
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
            timeout=120, effort="low",
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


    def evaluate_opposite(
        self,
        indicators: dict,
        opposite_direction: str,
        opposite_local_conf: dict,
        sonnet_rejection_reasoning: str,
        sonnet_key_levels: dict,
        recent_scans: list,
        market_context: dict,
        web_research: dict,
        recent_trades: list = None,
        live_edge_block: str = None,
        recent_opus_decision: dict = None,
    ) -> dict:
        """
        Opus evaluates the OPPOSITE direction as a SWING trade after Sonnet rejected primary.
        Full context (same as Sonnet scan), full SL/TP freedom, same confidence thresholds.

        Gate: only called when opposite direction had a detected setup AND local conf >= 60%.

        Returns: {setup_found, direction, confidence, entry, stop_loss, take_profit,
                  setup_type, reasoning, effective_rr, warnings, edge_factors}
        """
        primary_direction = "LONG" if opposite_direction == "SHORT" else "SHORT"

        system_prompt = (
            "You are a swing-trade evaluator for Japan 225 Cash CFD ($1/pt, spread ~7pts).\n"
            "The primary AI (Sonnet) evaluated the market and rejected the "
            f"{primary_direction} direction.\n"
            f"Your job: evaluate the OPPOSITE direction ({opposite_direction}) as a SWING trade.\n\n"
            f"CRITICAL: You are evaluating {opposite_direction} ONLY. Do NOT evaluate {primary_direction}.\n"
            "This is a full swing trade evaluation — NOT a scalp. Use proper swing SL/TP from structure.\n\n"
            "SL/TP PLACEMENT FROM STRUCTURE:\n"
            "- SL: Place at the nearest structural invalidation (swing low for LONG, swing high for SHORT).\n"
            "  Use BB bands, pivot S/R levels, fib levels, PDH/PDL, EMA50. No bounds — AI picks from chart.\n"
            "- TP: Place at the next structural obstacle (BB upper for LONG, BB lower for SHORT).\n"
            "  Use pivots R1/R2 for LONG, S1/S2 for SHORT, swing highs/lows, VWAP, EMA50.\n\n"
            "ATR VOLATILITY RULE: ATR14 is shown for each timeframe. If 15M ATR14 > 120pts,\n"
            "  market is VOLATILE. SL must be at least 1× ATR from entry to survive noise.\n"
            "  Tokyo session regularly shows ATR 140-220pts — set SL accordingly.\n\n"
            f"CONFIDENCE THRESHOLDS: LONG requires >= {MIN_CONFIDENCE}%. SHORT requires >= {MIN_CONFIDENCE_SHORT}%.\n"
            "R:R REQUIREMENT: (TP_dist - 7) / (SL_dist + 7) >= 1.5  (7pt spread adjustment)\n\n"
            "MEAN-REVERSION BOUNCE RULES (if evaluating LONG bounce):\n"
            "  Bearish HA streak, price below EMA50, 4H bearish = EXPECTED for bounce setups.\n"
            "  Evaluate bounce QUALITY: wick rejection, pattern, sweep, volume surge, RSI divergence.\n\n"
            "BREAKDOWN / MOMENTUM SHORT RULES (if evaluating SHORT breakdown):\n"
            "  Daily still 'bullish' is EXPECTED during breakdowns — daily EMA lags big selloffs.\n"
            "  4H+15M aligned bearish (HA streak ≤-2, below EMA50, RSI<50) = approve breakdown SHORT.\n\n"
            "OVERSOLD SHORTING PROHIBITION: If 4H RSI < 32, do NOT approve SHORT.\n"
            "OVERBOUGHT LONGING PROHIBITION: If 4H RSI > 68, do NOT approve LONG.\n\n"
            "WARNING SEVERITY RULE: 4+ warnings → confidence < 70%. 6+ warnings → confidence < 60%.\n\n"
            "Use Sonnet's rejection reasoning as CONTEXT — it tells you why the primary direction\n"
            "failed, which often contains the structural case FOR the opposite direction.\n"
            "Sonnet's key levels (support/resistance) are provided — use them for SL/TP placement.\n\n"
            "REASONING_SHORT: Fill reasoning_short with a compact punchy paragraph (~3-5 sentences, ~400-500 chars).\n"
            "Cover: verdict + structure + decisive signal + SL/TP/RR summary + final call.\n"
            "Format: '[APPROVE/REJECT] [direction]. [Structure]. [Signal that decided it]. [SL=X TP=Y RR=Z]. [Final call].'\n"
            "Example: 'APPROVE LONG. 4H/15M swept_low + bullish_engulfing at BB lower — textbook reversal. Sonnet correctly flagged CRASH DAY bounce. SL=53500 TP=55000 RR=2.1. Entry confirmed.' "
            "reasoning field still holds your full analysis.\n"
        )

        indicator_block = _fmt_indicators(indicators)

        # Opposite direction local confidence breakdown (may be None for counter-signal triggers)
        _olc = opposite_local_conf or {}
        opp_criteria = _olc.get("criteria", {})
        opp_passed = [k for k, v in opp_criteria.items() if v]
        opp_failed = [k for k, v in opp_criteria.items() if not v]
        opp_conf_block = (
            f"\nOPPOSITE ({opposite_direction}) LOCAL SCORE: {_olc.get('score', 'N/A (counter-signal trigger)')} "
            f"PASS:{','.join(opp_passed) or 'none'} FAIL:{','.join(opp_failed) or 'none'}\n"
        )

        # Sonnet's key levels
        support_levels = sonnet_key_levels.get("support", [])
        resistance_levels = sonnet_key_levels.get("resistance", [])
        key_levels_block = (
            f"\nSONNET KEY LEVELS:\n"
            f"  Support: {support_levels}\n"
            f"  Resistance: {resistance_levels}\n"
        )

        # Directional consistency: show recent Opus decision to prevent flip-flopping
        consistency_block = ""
        if recent_opus_decision:
            prev_dir = recent_opus_decision.get("direction", "?")
            prev_viable = recent_opus_decision.get("viable", False)
            prev_reason = recent_opus_decision.get("reasoning", "")[:200]
            prev_conf = recent_opus_decision.get("confidence", 0)
            try:
                prev_ts = datetime.fromisoformat(recent_opus_decision["timestamp"])
                elapsed_min = int((datetime.now() - prev_ts).total_seconds() / 60)
            except Exception:
                elapsed_min = 0
            if prev_viable:
                consistency_block = (
                    f"\nYOUR PREVIOUS CALL ({elapsed_min} min ago): {prev_dir} swing, {prev_conf}% confidence.\n"
                    f"Reasoning: {prev_reason}\n"
                    f"CONSISTENCY RULE: Only flip direction if there is a CLEAR structural shift "
                    f"(broken support/resistance, new candle pattern, RSI divergence crossing threshold). "
                    f"Do NOT flip just because indicators moved a few points. Conviction matters.\n\n"
                )
            else:
                consistency_block = (
                    f"\nYOUR PREVIOUS CALL ({elapsed_min} min ago): No viable setup found.\n"
                    f"Reasoning: {prev_reason}\n"
                    f"Only approve now if conditions MATERIALLY changed.\n\n"
                )

        # Prompt learnings from closed trades
        learnings_block = load_prompt_learnings()
        learnings_str = f"\n{learnings_block}\n" if learnings_block else ""

        # Web research summary
        web_str = _fmt_web_research(web_research)
        recent_trades_str = _fmt_recent_trades(recent_trades or [])
        recent_scans_str = _fmt_recent_scans(recent_scans)

        from config.settings import display_now, DISPLAY_TZ_LABEL
        now = display_now().strftime(f"%Y-%m-%d %H:%M {DISPLAY_TZ_LABEL}")

        user_prompt = (
            f"Japan 225 CFD analysis — {now}\n\n"
            f"TASK: Evaluate {opposite_direction} swing trade (Sonnet rejected {primary_direction}).\n\n"
            f"SONNET REJECTION REASONING:\n{sonnet_rejection_reasoning}\n"
            f"{opp_conf_block}"
            f"{key_levels_block}"
            f"{consistency_block}"
            f"\nTIMEFRAME SNAPSHOT:\n{indicator_block}\n"
            f"\nRECENT SCANS (last 5):\n{recent_scans_str}\n"
            f"\nRECENT TRADES (last 5):\n{recent_trades_str}\n"
            f"\nMARKET CONTEXT: session={market_context.get('session_name', '?')} | "
            f"trading_mode={market_context.get('trading_mode', '?')}\n"
            f"\nWEB RESEARCH:\n{web_str}\n"
            + (("\n" + live_edge_block) if live_edge_block else "")
            + learnings_str
            + f"\nEvaluate ONLY {opposite_direction}. Do NOT evaluate {primary_direction}.\n"
            f"Output ONLY valid JSON:\n"
            f'{{"setup_found": true, "direction": "{opposite_direction}", "confidence": 72, '
            f'"entry": 54200.0, "stop_loss": 54050.0, "take_profit": 54600.0, '
            f'"setup_type": "bb_lower_bounce", "reasoning": "...", "effective_rr": 2.1, '
            f'"warnings": [], "edge_factors": [], '
            f'"reasoning_short": "APPROVE LONG — swept_low + engulfing at BB lower; SL below sweep, TP at BB mid. RR 2.1."}}\n'
            f"or if no valid setup:\n"
            f'{{"setup_found": false, "direction": null, "confidence": 0, '
            f'"entry": null, "stop_loss": null, "take_profit": null, '
            f'"setup_type": null, "reasoning": "No viable {opposite_direction} setup because...", '
            f'"effective_rr": 0.0, "warnings": [], "edge_factors": [], '
            f'"reasoning_short": "REJECT — [key reason in one sentence]."}}'
        )

        raw, tokens = self._run_claude(
            OPUS_MODEL, system_prompt, user_prompt,
            timeout=150, effort="low",
        )

        default = {
            "setup_found": False,
            "direction": None,
            "confidence": 0,
            "reasoning": "Opus opposite eval returned unparseable output",
            "warnings": [],
            "edge_factors": [],
        }
        result = _parse_json(raw, default)

        # Validate direction match
        if result.get("setup_found") and result.get("direction") != opposite_direction:
            logger.warning(
                f"Opus opposite eval returned wrong direction: "
                f"expected {opposite_direction}, got {result.get('direction')} — setting setup_found=False"
            )
            result["setup_found"] = False
            result["reasoning"] = (
                f"Direction mismatch: expected {opposite_direction}, got {result.get('direction')}. "
                + result.get("reasoning", "")
            )

        result["_model"] = OPUS_MODEL
        result["_cost"] = 0.0
        result["_tokens"] = tokens

        logger.info(
            f"Opus opposite eval ({opposite_direction}): found={result.get('setup_found')}, "
            f"conf={result.get('confidence', 'N/A')}%, "
            f"reason={result.get('reasoning', '')[:120]}"
        )
        return result


    def evaluate_open_position(
        self,
        direction: str,
        entry: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        phase: str,
        time_in_trade_min: float,
        recent_prices: list,
        setup_type: str = "unknown",
        lots: float = 1.0,
        entry_context: dict = None,
        current_indicators: dict = None,
    ) -> dict:
        """
        Opus-powered position evaluator. Runs every 2 minutes on open positions.
        Replaces MILD/MODERATE adverse alerts with intelligent assessment.

        Returns: {recommendation, confidence, adverse_risk, tp_probability, reasoning, tighten_sl_to}
        recommendation: HOLD | CLOSE_NOW | TIGHTEN_SL
        adverse_risk: NONE | LOW | MEDIUM | HIGH | CRITICAL
        """
        from config.settings import (
            ADVERSE_MILD_PTS, ADVERSE_MODERATE_PTS, ADVERSE_SEVERE_PTS, CONTRACT_SIZE,
        )

        pnl_pts = (current_price - entry) if direction == "LONG" else (entry - current_price)
        pnl_dollars = pnl_pts * lots * CONTRACT_SIZE
        sl_dist_remaining = abs(current_price - stop_loss)
        tp_dist_remaining = abs(take_profit - current_price)

        def _backoff_sample(prices: list, n_samples: int = 30) -> list:
            """Quadratic index spacing: dense at recent end, sparse at older end."""
            n = len(prices)
            if n <= n_samples:
                return prices
            indices = set()
            for i in range(n_samples):
                frac = (i / (n_samples - 1)) ** 2  # 0..1 quadratic — bunches samples near i=0 (oldest)
                idx = int((n - 1) * (1 - frac))    # map to array: frac=0 → newest, frac=1 → oldest
                indices.add(max(0, min(n - 1, idx)))
            return [prices[i] for i in sorted(indices)]

        # Rich momentum analysis from price buffer
        ec = entry_context or {}
        raw_count = len(recent_prices)
        recent_prices = _backoff_sample(recent_prices)  # dense-recent, sparse-old
        if recent_prices and len(recent_prices) > 1:
            n = len(recent_prices)
            # Overall trend
            overall_move = recent_prices[-1] - recent_prices[0]
            trend_dir = "bullish" if overall_move > 0 else "bearish" if overall_move < 0 else "flat"
            # Worst adverse excursion
            if direction == "LONG":
                worst_price = min(recent_prices)
                worst_adverse = max(0, entry - worst_price)
            else:
                worst_price = max(recent_prices)
                worst_adverse = max(0, worst_price - entry)
            # Velocity: compare first third vs last third (momentum acceleration/deceleration)
            third = max(1, n // 3)
            early_avg = sum(recent_prices[:third]) / third
            late_avg = sum(recent_prices[-third:]) / third
            velocity = late_avg - early_avg
            vel_dir = "accelerating toward TP" if (
                (direction == "LONG" and velocity > 0) or (direction == "SHORT" and velocity < 0)
            ) else "decelerating / reversing"
            # HH/HL or LH/LL structure (simple: compare midpoint halves)
            mid = n // 2
            first_half_high = max(recent_prices[:mid])
            first_half_low  = min(recent_prices[:mid])
            second_half_high = max(recent_prices[mid:])
            second_half_low  = min(recent_prices[mid:])
            if second_half_high > first_half_high and second_half_low > first_half_low:
                structure = "HH+HL (bullish structure)"
            elif second_half_high < first_half_high and second_half_low < first_half_low:
                structure = "LH+LL (bearish structure)"
            elif second_half_high > first_half_high and second_half_low < first_half_low:
                structure = "expanding range"
            else:
                structure = "contracting / sideways"
            total_secs = raw_count * 2
            price_summary = (
                f"  Span: {raw_count} readings = {total_secs // 60}min {total_secs % 60}s "
                f"(shown: {n} samples, exponential backoff — recent=dense, old=sparse)\n"
                f"  Range: {min(recent_prices):.0f}–{max(recent_prices):.0f} ({max(recent_prices)-min(recent_prices):.0f}pt spread)\n"
                f"  Overall move: {overall_move:+.0f}pts ({trend_dir})\n"
                f"  Velocity (early→late avg): {velocity:+.0f}pts — {vel_dir}\n"
                f"  Price structure: {structure}\n"
                f"  Worst adverse from entry: {worst_adverse:.0f}pts\n"
            )
        else:
            price_summary = "  Insufficient price history (<2 readings).\n"
            worst_adverse = 0

        # Entry conditions snapshot
        entry_block = ""
        if ec:
            entry_block = (
                f"\nENTRY CONTEXT (conditions when trade was opened):\n"
                f"  Setup: {ec.get('setup_type', 'unknown')} | Session: {ec.get('session', '?')} | "
                f"Confidence: {ec.get('confidence', '?')}% | R:R at entry: 1:{ec.get('rr', '?')}\n"
                f"  SL at entry: {ec.get('sl_pts', '?')}pts | TP at entry: {ec.get('tp_pts', '?')}pts\n"
                f"  RSI 15M: {ec.get('rsi_15m', '?')} | RSI 4H: {ec.get('rsi_4h', '?')} | "
                f"Above VWAP: {ec.get('above_vwap', '?')} | Daily bullish: {ec.get('daily_bullish', '?')}\n"
                f"  HA bullish: {ec.get('ha_bullish', '?')} | HA streak: {ec.get('ha_streak', '?')} | "
                f"Volume ratio: {ec.get('volume_ratio', '?')}\n"
                f"  Swing high: {ec.get('swing_high_20', '?')} | Swing low: {ec.get('swing_low_20', '?')}\n"
                f"  AI reasoning at entry: {ec.get('ai_reasoning', 'not available')}\n"
            )

        # Aggregate raw 2s readings into 1-minute OHLC candles
        ohlc_block = ""
        if recent_prices and raw_count >= 30:  # need at least 1 min of data
            readings_per_min = 30  # 30 × 2s = 1 minute
            # Use the FULL original buffer (before backoff sampling) by reconstructing from raw_count
            # We have sampled_prices here; rebuild minute candles from sampled as best-effort
            candles = []
            bucket_size = max(1, len(recent_prices) // max(1, raw_count // readings_per_min))
            i = 0
            while i < len(recent_prices):
                bucket = recent_prices[i:i + bucket_size]
                if bucket:
                    mins_ago = int((len(recent_prices) - i) * (raw_count / len(recent_prices)) * 2 / 60)
                    candles.append(
                        f"  T-{mins_ago:02d}min: O={bucket[0]:.0f} H={max(bucket):.0f} "
                        f"L={min(bucket):.0f} C={bucket[-1]:.0f} "
                        f"({'▲' if bucket[-1] >= bucket[0] else '▼'} {abs(bucket[-1]-bucket[0]):.0f}pts)"
                    )
                i += bucket_size
            if candles:
                ohlc_block = (
                    f"\n1-MINUTE OHLC CANDLES SINCE OPEN ({len(candles)} candles, oldest→newest):\n"
                    + "\n".join(candles) + "\n"
                )

        # Current live indicator snapshot
        ci = current_indicators or {}
        current_ind_block = ""
        if ci:
            current_ind_block = (
                f"\nCURRENT MARKET INDICATORS (live, fetched now):\n"
                f"  RSI 15M: {ci.get('rsi_15m', '?')} | RSI 4H: {ci.get('rsi_4h', '?')}\n"
                f"  EMA9 dist: {ci.get('ema9_dist', '?')}pts | EMA50 dist: {ci.get('ema50_dist', '?')}pts\n"
                f"  Above VWAP: {ci.get('above_vwap', '?')} | VWAP: {ci.get('vwap', '?')}\n"
                f"  BB upper: {ci.get('bb_upper', '?')} | BB mid: {ci.get('bb_mid', '?')} | BB lower: {ci.get('bb_lower', '?')}\n"
                f"  HA bullish: {ci.get('ha_bullish', '?')} | HA streak: {ci.get('ha_streak', '?')}\n"
                f"  Daily bullish: {ci.get('daily_bullish', '?')}\n"
            )

        system_prompt = (
            "You are monitoring an open Japan 225 Cash CFD position ($1/pt, ~7pt spread).\n"
            "Give a cold, honest assessment. No bias toward holding — if setup is broken, say CLOSE_NOW.\n\n"
            "RECOMMENDATIONS:\n"
            "  HOLD: conditions still support the thesis. Price neutral or moving toward TP.\n"
            "  CLOSE_NOW: setup materially invalidated. Price reversing, TP unlikely, risk growing.\n"
            "    Only recommend CLOSE_NOW with confidence >= 60%.\n"
            "  TIGHTEN_SL: position profitable, protect gains by moving SL closer.\n"
            "    Specify tighten_sl_to = new distance from entry (e.g. 50 means SL 50pts from entry).\n\n"
            "ADVERSE RISK LEVELS:\n"
            "  NONE: price moving toward TP, no adverse pressure.\n"
            "  LOW: minor pullback (<60pts), within normal noise.\n"
            "  MEDIUM: meaningful adverse (60-120pts), watch closely.\n"
            "  HIGH: serious adverse (120-175pts), TP probability declining.\n"
            "  CRITICAL: within SL distance, immediate risk of loss.\n\n"
            "PHASE CONTEXT:\n"
            "  INITIAL: SL+TP set. Breakeven not yet locked.\n"
            "  BREAKEVEN: SL at entry — protected from loss. Be more patient.\n"
            "  RUNNER: Trailing stop active — only CLOSE_NOW on clear hard reversal.\n\n"
            "EXTREME DAY WARNING: If intraday moves are violent (2000+pts range), snaps are fast.\n"
            "OUTPUT: JSON only. No preamble."
        )

        setup_display = ec.get("setup_type", setup_type) if ec else setup_type
        user_prompt = (
            f"POSITION:\n"
            f"  Direction: {direction} | Setup: {setup_display} | Phase: {phase}\n"
            f"  Entry: {entry:.0f} | Current: {current_price:.0f} | P&L: {pnl_pts:+.0f}pts (${pnl_dollars:+.2f})\n"
            f"  SL: {stop_loss:.0f} ({sl_dist_remaining:.0f}pts away) | TP: {take_profit:.0f} ({tp_dist_remaining:.0f}pts away)\n"
            f"  Time open: {time_in_trade_min:.0f}min | Lots: {lots}\n"
            f"{entry_block}"
            f"{current_ind_block}"
            f"\nPRICE TRAJECTORY SINCE TRADE OPEN ({raw_count} readings = {raw_count//4} MINUTE candles"
            f", trade age {time_in_trade_min:.0f}min):\n"
            f"{price_summary}"
            f"{ohlc_block}"
            f"\nADVERSE THRESHOLDS:\n"
            f"  MILD={ADVERSE_MILD_PTS}pts | MODERATE={ADVERSE_MODERATE_PTS}pts | SEVERE={ADVERSE_SEVERE_PTS}pts\n\n"
            f"KEY QUESTION: Given the entry setup, current candle structure, live indicators, and full price "
            f"trajectory since open — is the original thesis still valid? Will price continue to TP, or is "
            f"the setup broken/stalling?\n\n"
            f"Output ONLY valid JSON:\n"
            f'{{"recommendation": "HOLD", "confidence": 72, "adverse_risk": "LOW", '
            f'"tp_probability": 0.65, "reasoning": "...", "tighten_sl_to": null}}'
        )

        raw, tokens = self._run_claude(
            OPUS_MODEL, system_prompt, user_prompt,
            timeout=90, effort="low",
        )

        default = {
            "recommendation": "HOLD",
            "confidence": 50,
            "adverse_risk": "LOW",
            "tp_probability": 0.5,
            "reasoning": "Opus position eval returned unparseable output — defaulting to HOLD.",
            "tighten_sl_to": None,
        }
        result = _parse_json(raw, default)
        result["_model"] = OPUS_MODEL

        logger.info(
            f"Opus position eval: {result.get('recommendation')} ({result.get('confidence')}%) | "
            f"adverse={result.get('adverse_risk')} | tp_prob={result.get('tp_probability')} | "
            f"{result.get('reasoning', '')[:120]}"
        )
        return result


# ─── Prompt learnings (unchanged) ─────────────────────────────────────────────

def load_prompt_learnings(data_dir: str = None) -> str:
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
