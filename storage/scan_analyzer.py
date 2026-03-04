"""
Automated Scan Analyzer — Missed-Move Tracking

Runs via cron every 2 hours. Reads SQLite scans table, compares rejection
prices to subsequent prices, identifies missed opportunities, and writes
a pre-computed analysis to storage/data/scan_analysis.md.

Usage:
    python -m storage.scan_analyzer
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from scipy.stats import binomtest

logger = logging.getLogger(__name__)

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "storage" / "data" / "trading.db"
OUTPUT_MD = BASE_DIR / "storage" / "data" / "scan_analysis.md"
OUTPUT_LOG = BASE_DIR / "storage" / "data" / "scan_analysis.log"

# --- Thresholds ---
SL_DISTANCE = 150            # pts — default SL; trade is stopped out if price moves this far adverse
TP_DISTANCE = 400            # pts — default TP; true missed move only if this is reached BEFORE SL
NEAR_MISS_THRESHOLD = 300    # pts — almost a winner: reached this without SL hit but fell short of TP
LOOKBACK_HOURS = 24          # default for cron; use --all flag to run over full history
PRICE_SEQUENCE_WINDOW = 120  # minutes ahead to walk for outcome classification


def _connect_db() -> sqlite3.Connection:
    """Read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _get_scans(conn: sqlite3.Connection, hours: int) -> list[dict]:
    """Fetch all scans from the last N hours."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT * FROM scans WHERE timestamp > ? ORDER BY timestamp ASC",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _parse_rejection_reason(scan: dict) -> str:
    """Extract the primary rejection reason from a scan record."""
    action = scan.get("action_taken", "")
    analysis_raw = scan.get("analysis", "{}")
    try:
        analysis = json.loads(analysis_raw) if isinstance(analysis_raw, str) else analysis_raw
    except (json.JSONDecodeError, TypeError):
        analysis = {}

    reasoning = analysis.get("reasoning", "") if isinstance(analysis, dict) else ""

    if action == "no_setup":
        # Parse diagnostic from reasoning string
        if "RSI=" in reasoning and "OUT" in reasoning:
            return "RSI out of range"
        if "BB_mid=" in reasoning and "FAR" in reasoning:
            return "BB mid too far"
        if "bounce=NO" in reasoning:
            return "bounce=NO"
        return "no_setup(other)"

    if action.startswith("ai_rejected"):
        return f"AI rejected {action.split('_')[-1].upper()}"
    if action.startswith("low_conf"):
        return f"low confidence"
    if action.startswith("event_block"):
        return "event block"
    if action.startswith("friday_block"):
        return "friday block"

    return action


def _extract_rsi(scan: dict) -> float | None:
    """Extract RSI from the scan's analysis reasoning string."""
    analysis_raw = scan.get("analysis", "{}")
    try:
        analysis = json.loads(analysis_raw) if isinstance(analysis_raw, str) else analysis_raw
    except (json.JSONDecodeError, TypeError):
        return None

    reasoning = analysis.get("reasoning", "") if isinstance(analysis, dict) else ""
    # Match RSI=XX.X pattern
    m = re.search(r"RSI=(\d+\.?\d*)", reasoning)
    if m:
        return float(m.group(1))
    return None


def _infer_expected_direction(scan: dict) -> str | None:
    """Infer the direction the market 'should' have gone based on the rejection context."""
    action = scan.get("action_taken", "")
    # Explicit direction in action_taken
    if "_long" in action:
        return "LONG"
    if "_short" in action:
        return "SHORT"
    # For no_setup: check analysis for daily trend hints
    analysis_raw = scan.get("analysis", "{}")
    try:
        analysis = json.loads(analysis_raw) if isinstance(analysis_raw, str) else analysis_raw
    except (json.JSONDecodeError, TypeError):
        return None
    reasoning = analysis.get("reasoning", "") if isinstance(analysis, dict) else ""
    if "Daily=bullish" in reasoning:
        return "LONG"
    if "Daily=bearish" in reasoning:
        return "SHORT"
    return None


def _get_price_sequence(scans: list[dict], from_time: str) -> list[tuple]:
    """Return (datetime, price) pairs in chronological order from from_time to from_time + PRICE_SEQUENCE_WINDOW."""
    try:
        t0 = datetime.fromisoformat(from_time)
    except (ValueError, TypeError):
        return []
    t_end = t0 + timedelta(minutes=PRICE_SEQUENCE_WINDOW)
    result = []
    for s in scans:
        try:
            ts = datetime.fromisoformat(s["timestamp"])
        except (ValueError, TypeError):
            continue
        if t0 < ts <= t_end and s.get("price"):
            result.append((ts, float(s["price"])))
    return sorted(result, key=lambda x: x[0])


def _binomial_significance(true_missed: int, total: int, null_rate: float = 0.20) -> str:
    """One-sided binomial test: is the miss rate significantly HIGHER than null_rate?

    The null hypothesis is that this gate's miss rate equals the overall base rate (null_rate).
    A 'Gate too tight' verdict should only be trusted when p < 0.05.
    Bonferroni note: with 4 gates tested simultaneously, effective threshold is p < 0.0125.
    """
    if total < 20:
        return f"⚠ n={total} (need ≥20 to test)"
    result = binomtest(true_missed, total, null_rate, alternative="greater")
    p = result.pvalue
    if p < 0.01:
        return f"p={p:.3f} ✓ SIGNIFICANT"
    elif p < 0.05:
        return f"p={p:.3f} ~ marginal"
    else:
        return f"p={p:.3f} ✗ NOT SIGNIFICANT (noise)"


def _get_intraday_regime(scans: list[dict], rejection_ts: str) -> str:
    """Detect trend regime by comparing rejection price to price 4 hours earlier.

    BULL  = price up >300pts in last 4h (strong uptrend)
    BEAR  = price down >300pts in last 4h (strong downtrend)
    NEUTRAL = no strong trend
    """
    try:
        t_now = datetime.fromisoformat(rejection_ts)
        t_ref = t_now - timedelta(hours=4)
    except (ValueError, TypeError):
        return "UNKNOWN"

    # Find price closest to 4h ago
    ref_price = None
    best_delta = timedelta(hours=999)
    for s in scans:
        try:
            ts = datetime.fromisoformat(s["timestamp"])
        except (ValueError, TypeError):
            continue
        if abs(ts - t_ref) < timedelta(minutes=20) and s.get("price"):
            delta = abs(ts - t_ref)
            if delta < best_delta:
                ref_price = float(s["price"])
                best_delta = delta

    if ref_price is None:
        return "UNKNOWN"

    # Find current price from the rejection scan
    cur_price = None
    for s in scans:
        try:
            ts = datetime.fromisoformat(s["timestamp"])
        except (ValueError, TypeError):
            continue
        if abs(ts - t_now) < timedelta(minutes=3) and s.get("price"):
            cur_price = float(s["price"])
            break

    if cur_price is None:
        return "UNKNOWN"

    move = cur_price - ref_price
    if move > 300:
        return "BULL"
    elif move < -300:
        return "BEAR"
    else:
        return "NEUTRAL"


def _get_session_label(iso_str: str) -> str:
    """Map UTC timestamp to trading session label."""
    try:
        dt = datetime.fromisoformat(iso_str)
        h = dt.hour
        if 0 <= h < 7:
            return "Tokyo"
        elif 7 <= h < 15:
            return "London"
        elif 15 <= h < 22:
            return "NY"
        else:
            return "Off"
    except (ValueError, TypeError):
        return "?"


def _classify_trade_outcome(entry_price: float, direction: str, price_seq: list[tuple]) -> dict:
    """Walk price sequence chronologically — first threshold hit wins.

    Returns outcome: 'true_missed' (TP hit before SL), 'thank_god' (SL hit before TP),
    or 'small_move' (neither threshold reached).
    Also returns: max_favorable, max_adverse, adverse_before_tp (drawdown on the way to TP),
    time_to_tp_min (minutes until TP was hit), near_miss (300-399pts favorable, no SL hit).
    """
    first_sl_idx = None
    first_tp_idx = None
    max_favorable = 0.0
    max_adverse = 0.0
    adverse_before_tp = 0.0
    time_to_tp_min = None
    t0 = price_seq[0][0] if price_seq else None

    for i, (ts, price) in enumerate(price_seq):
        if direction == "LONG":
            favorable = price - entry_price
            adverse = entry_price - price
        else:
            favorable = entry_price - price
            adverse = price - entry_price

        if favorable > max_favorable:
            max_favorable = favorable
        if adverse > max_adverse:
            max_adverse = adverse

        if first_sl_idx is None and adverse >= SL_DISTANCE:
            first_sl_idx = i
        if first_tp_idx is None and favorable >= TP_DISTANCE:
            first_tp_idx = i
            adverse_before_tp = max_adverse  # worst drawdown seen before TP was hit
            if t0 is not None:
                time_to_tp_min = round((ts - t0).total_seconds() / 60)

        if first_sl_idx is not None and first_tp_idx is not None:
            break

    if first_tp_idx is not None and (first_sl_idx is None or first_tp_idx < first_sl_idx):
        outcome = "true_missed"
    elif first_sl_idx is not None:
        outcome = "thank_god"
    else:
        outcome = "small_move"

    near_miss = (
        outcome != "thank_god"
        and NEAR_MISS_THRESHOLD <= max_favorable < TP_DISTANCE
    )

    return {
        "outcome": outcome,
        "near_miss": near_miss,
        "max_favorable": round(max_favorable, 1),
        "max_adverse": round(max_adverse, 1),
        "adverse_before_tp": round(adverse_before_tp, 1),
        "time_to_tp_min": time_to_tp_min,
    }


def _compute_missed_moves(scans: list[dict]) -> list[dict]:
    """For each rejection scan, classify outcome by walking the subsequent price sequence."""
    rejections = [
        s for s in scans
        if s.get("action_taken") in (
            "no_setup", "ai_rejected_long", "ai_rejected_short",
            "low_conf_long", "low_conf_short",
        )
        and s.get("price")
    ]

    results = []
    for scan in rejections:
        price = float(scan["price"])
        direction = _infer_expected_direction(scan)
        if not direction:
            continue

        reason = _parse_rejection_reason(scan)
        rsi = _extract_rsi(scan)
        ts = scan["timestamp"]

        price_seq = _get_price_sequence(scans, ts)
        classification = _classify_trade_outcome(price, direction, price_seq)

        results.append({
            "timestamp": ts,
            "price": price,
            "rsi": rsi,
            "direction": direction,
            "reason": reason,
            "action": scan.get("action_taken", ""),
            "session": _get_session_label(ts),
            "regime": _get_intraday_regime(scans, ts),
            "outcome": classification["outcome"],
            "near_miss": classification["near_miss"],
            "max_favorable": classification["max_favorable"],
            "max_adverse": classification["max_adverse"],
            "adverse_before_tp": classification["adverse_before_tp"],
            "time_to_tp_min": classification["time_to_tp_min"],
            # Legacy alias so run() summary line still works
            "is_missed": classification["outcome"] == "true_missed",
        })

    return results


def _build_regime_summary(missed_data: list[dict]) -> dict[str, dict]:
    """Conditional probability: given regime, what is the miss rate?
    This is the core quant insight — base rates are noisy, conditional rates are signal.
    """
    regimes = ["BULL", "BEAR", "NEUTRAL", "UNKNOWN"]
    summary = {r: {"count": 0, "true_missed": 0, "thank_god": 0} for r in regimes}
    for item in missed_data:
        r = item.get("regime", "UNKNOWN")
        if r not in summary:
            summary[r] = {"count": 0, "true_missed": 0, "thank_god": 0}
        summary[r]["count"] += 1
        summary[r][item.get("outcome", "small_move")] = summary[r].get(item.get("outcome", "small_move"), 0) + 1
    return summary


def _build_session_summary(missed_data: list[dict]) -> dict[str, dict]:
    """Aggregate outcomes by session."""
    sessions = ["Tokyo", "London", "NY", "Off"]
    summary = {s: {"count": 0, "true_missed": 0, "thank_god": 0, "near_miss": 0} for s in sessions}
    for item in missed_data:
        s = item.get("session", "Off")
        if s not in summary:
            summary[s] = {"count": 0, "true_missed": 0, "thank_god": 0, "near_miss": 0}
        summary[s]["count"] += 1
        summary[s][item.get("outcome", "small_move")] = summary[s].get(item.get("outcome", "small_move"), 0) + 1
        if item.get("near_miss"):
            summary[s]["near_miss"] += 1
    return summary


def _build_reason_summary(missed_data: list[dict]) -> dict[str, dict]:
    """Aggregate outcome stats by rejection reason."""
    summary: dict[str, dict] = {}
    for item in missed_data:
        reason = item["reason"]
        if reason not in summary:
            summary[reason] = {"count": 0, "true_missed": 0, "thank_god": 0, "small_move": 0}
        summary[reason]["count"] += 1
        outcome = item.get("outcome", "small_move")
        summary[reason][outcome] = summary[reason].get(outcome, 0) + 1
    return summary


def _build_rsi_buckets(missed_data: list[dict]) -> dict[str, dict]:
    """Bucket rejections by RSI range, tracking true_missed vs thank_god outcomes."""
    buckets = {
        "20-35": {"count": 0, "true_missed": 0, "thank_god": 0},
        "35-45": {"count": 0, "true_missed": 0, "thank_god": 0},
        "45-55": {"count": 0, "true_missed": 0, "thank_god": 0},
        "55-65": {"count": 0, "true_missed": 0, "thank_god": 0},
        "65-75": {"count": 0, "true_missed": 0, "thank_god": 0},
        "75+":   {"count": 0, "true_missed": 0, "thank_god": 0},
    }
    for item in missed_data:
        rsi = item.get("rsi")
        if rsi is None:
            continue
        if rsi < 35:
            bucket = "20-35"
        elif rsi < 45:
            bucket = "35-45"
        elif rsi < 55:
            bucket = "45-55"
        elif rsi < 65:
            bucket = "55-65"
        elif rsi < 75:
            bucket = "65-75"
        else:
            bucket = "75+"
        buckets[bucket]["count"] += 1
        outcome = item.get("outcome", "small_move")
        if outcome in buckets[bucket]:
            buckets[bucket][outcome] += 1
    return buckets


def _format_time(iso_str: str) -> str:
    """Extract HH:MM from ISO timestamp, converted to Kuwait time (UTC+3)."""
    try:
        from config.settings import DISPLAY_TZ
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            from datetime import timezone as tz
            dt = dt.replace(tzinfo=tz.utc)
        return dt.astimezone(DISPLAY_TZ).strftime("%H:%M")
    except (ValueError, TypeError):
        return "??:??"


def generate_report(scans: list[dict]) -> str:
    """Generate the full scan analysis markdown report."""
    from config.settings import display_now
    now = display_now().strftime("%Y-%m-%dT%H:%M:%S")

    # Categorize scans
    total = len(scans)
    no_setup = sum(1 for s in scans if s.get("action_taken") == "no_setup")
    ai_rejected = sum(1 for s in scans if (s.get("action_taken") or "").startswith("ai_rejected"))
    traded = sum(1 for s in scans if (s.get("action_taken") or "").startswith("pending"))
    low_conf = sum(1 for s in scans if (s.get("action_taken") or "").startswith("low_conf"))
    other = total - no_setup - ai_rejected - traded - low_conf

    # Last 2 hours subset
    two_hrs_ago = (datetime.now() - timedelta(hours=2)).isoformat()
    recent = [s for s in scans if s.get("timestamp", "") > two_hrs_ago]
    recent_total = len(recent)
    recent_no_setup = sum(1 for s in recent if s.get("action_taken") == "no_setup")
    recent_ai_rejected = sum(1 for s in recent if (s.get("action_taken") or "").startswith("ai_rejected"))
    recent_traded = sum(1 for s in recent if (s.get("action_taken") or "").startswith("pending"))

    # Compute all outcomes for full 24h
    missed_data = _compute_missed_moves(scans)
    true_missed = [m for m in missed_data if m["outcome"] == "true_missed"]
    thank_god = [m for m in missed_data if m["outcome"] == "thank_god"]

    lines = [
        f"# Scan Analysis — Updated {now}",
        f"# SL={SL_DISTANCE}pts | TP={TP_DISTANCE}pts — outcome = first threshold hit in {PRICE_SEQUENCE_WINDOW}min window",
        "",
        "## Last 2 Hours",
        f"- Scans: {recent_total} | No setup: {recent_no_setup} | AI rejected: {recent_ai_rejected} | Traded: {recent_traded}",
    ]

    near_misses = [m for m in missed_data if m.get("near_miss")]

    # --- TRUE MISSED section ---
    lines += [
        "",
        f"## True Missed Moves — TP ({TP_DISTANCE}pts) hit BEFORE SL ({SL_DISTANCE}pts)",
        f"*Trade would have PROFITED. Shows drawdown endured on the way to TP.*",
    ]
    if true_missed:
        lines.append("| Time  | Session | Price | RSI  | Dir | Rejection | Favorable | Drawdown | Time-to-TP |")
        lines.append("|-------|---------|-------|------|-----|-----------|-----------|----------|------------|")
        for m in sorted(true_missed, key=lambda x: -x["max_favorable"])[:25]:
            rsi_str = f"{m['rsi']:.1f}" if m.get("rsi") else "N/A"
            ttp = f"{m['time_to_tp_min']}min" if m.get("time_to_tp_min") is not None else "?"
            lines.append(
                f"| {_format_time(m['timestamp'])} | {m.get('session','?')} | {m['price']:.0f} | {rsi_str} | "
                f"{m['direction']} | {m['reason']} | +{m['max_favorable']:.0f}pts | "
                f"-{m['adverse_before_tp']:.0f}pts | {ttp} |"
            )
    else:
        lines.append("*No true missed moves.*")

    # --- NEAR MISS section ---
    lines += [
        "",
        f"## Near Misses — {NEAR_MISS_THRESHOLD}–{TP_DISTANCE-1}pts favorable, SL never hit",
        f"*Almost won. Reached {NEAR_MISS_THRESHOLD}pts+ in right direction but stopped short of TP.*",
    ]
    if near_misses:
        lines.append("| Time  | Session | Price | RSI  | Dir | Rejection | Favorable | Adverse |")
        lines.append("|-------|---------|-------|------|-----|-----------|-----------|---------|")
        for m in sorted(near_misses, key=lambda x: -x["max_favorable"])[:15]:
            rsi_str = f"{m['rsi']:.1f}" if m.get("rsi") else "N/A"
            lines.append(
                f"| {_format_time(m['timestamp'])} | {m.get('session','?')} | {m['price']:.0f} | {rsi_str} | "
                f"{m['direction']} | {m['reason']} | +{m['max_favorable']:.0f}pts | -{m['max_adverse']:.0f}pts |"
            )
    else:
        lines.append("*No near misses.*")

    # --- THANK GOD section ---
    lines += [
        "",
        f"## Thank God Rejections — SL ({SL_DISTANCE}pts) hit BEFORE TP ({TP_DISTANCE}pts)",
        f"*Rejections that SAVED a loss.*",
    ]
    if thank_god:
        lines.append("| Time  | Session | Price | RSI  | Dir | Rejection | Adverse | Favorable |")
        lines.append("|-------|---------|-------|------|-----|-----------|---------|-----------|")
        for m in sorted(thank_god, key=lambda x: -x["max_adverse"])[:20]:
            rsi_str = f"{m['rsi']:.1f}" if m.get("rsi") else "N/A"
            lines.append(
                f"| {_format_time(m['timestamp'])} | {m.get('session','?')} | {m['price']:.0f} | {rsi_str} | "
                f"{m['direction']} | {m['reason']} | -{m['max_adverse']:.0f}pts | +{m['max_favorable']:.0f}pts |"
            )
    else:
        lines.append("*No thank-god rejections detected.*")

    # --- Session breakdown ---
    session_summary = _build_session_summary(missed_data)
    lines += [
        "",
        "## Session Breakdown",
        "| Session | Analyzed | True Missed | Thank God | Near Miss | Miss Rate |",
        "|---------|----------|-------------|-----------|-----------|-----------|",
    ]
    for sess in ["Tokyo", "London", "NY", "Off"]:
        data = session_summary.get(sess, {})
        cnt = data.get("count", 0)
        if cnt == 0:
            continue
        tm = data.get("true_missed", 0)
        tg = data.get("thank_god", 0)
        nm = data.get("near_miss", 0)
        miss_rate = f"{tm/cnt*100:.0f}%" if cnt else "0%"
        lines.append(f"| {sess} | {cnt} | {tm} | {tg} | {nm} | {miss_rate} |")

    # --- Rejection pattern summary with significance testing ---
    reason_summary = _build_reason_summary(missed_data)
    # Overall base miss rate (null hypothesis for each gate)
    overall_miss_rate = len(true_missed) / len(missed_data) if missed_data else 0.20
    lines += [
        "",
        f"## Rejection Pattern Summary — with Significance Testing",
        f"*Null hypothesis: each gate's miss rate = overall base rate ({overall_miss_rate:.0%}). "
        f"Bonferroni-corrected threshold with {len(reason_summary)} gates: p < {0.05/max(len(reason_summary),1):.3f}*",
        "| Reason | Total | True Missed | Near Miss | Thank God | Miss Rate | p-value | Verdict |",
        "|--------|-------|-------------|-----------|-----------|-----------|---------|---------|",
    ]
    for reason, data in sorted(reason_summary.items(), key=lambda x: -x[1]["count"]):
        tm = data["true_missed"]
        tg = data["thank_god"]
        sm = data["small_move"]
        nm = sum(1 for m in missed_data if m["reason"] == reason and m.get("near_miss"))
        miss_pct = f"{tm/data['count']*100:.0f}%" if data["count"] else "0%"
        sig = _binomial_significance(tm, data["count"], null_rate=overall_miss_rate)
        net = tm - tg
        if net > 2 and "SIGNIFICANT" in sig:
            verdict = "Gate too tight ✓"
        elif net > 2:
            verdict = "Looks tight — needs more data"
        elif net < -2:
            verdict = "Gate is protecting"
        elif tm > 0:
            verdict = "Mixed"
        else:
            verdict = "OK"
        lines.append(f"| {reason} | {data['count']} | {tm} | {nm} | {tg} | {miss_pct} | {sig} | {verdict} |")

    # --- RSI at rejection ---
    rsi_buckets = _build_rsi_buckets(missed_data)
    lines += [
        "",
        "## RSI at Rejection",
        "| RSI Range | Count | True Missed | Thank God | Net | Verdict |",
        "|-----------|-------|-------------|-----------|-----|---------|",
    ]
    for bucket, data in rsi_buckets.items():
        if data["count"] == 0:
            continue
        tm = data["true_missed"]
        tg = data["thank_god"]
        net = tm - tg
        if net > 2:
            verdict = "Gate too tight"
        elif net < -2:
            verdict = "Gate protecting you"
        elif tm > 0:
            verdict = "Mixed"
        else:
            verdict = "OK"
        lines.append(f"| {bucket} | {data['count']} | {tm} | {tg} | {net:+d} | {verdict} |")

    # --- Regime conditional probability (core quant insight) ---
    regime_summary = _build_regime_summary(missed_data)
    lines += [
        "",
        "## Regime × Outcome (Conditional Probability)",
        "*P(miss | regime) is the KEY signal. Base rate alone is noise — Blitzstein Ch.1*",
        "*Regime = intraday trend: BULL/BEAR = price moved 300+pts in 4h, NEUTRAL = flat*",
        "| Regime | Analyzed | True Missed | Thank God | Miss Rate | Signal Quality |",
        "|--------|----------|-------------|-----------|-----------|----------------|",
    ]
    for regime in ["BULL", "BEAR", "NEUTRAL", "UNKNOWN"]:
        data = regime_summary.get(regime, {})
        cnt = data.get("count", 0)
        if cnt == 0:
            continue
        tm = data.get("true_missed", 0)
        tg = data.get("thank_god", 0)
        miss_rate = f"{tm/cnt*100:.0f}%" if cnt else "0%"
        sig = _binomial_significance(tm, cnt, null_rate=overall_miss_rate)
        lines.append(f"| {regime} | {cnt} | {tm} | {tg} | {miss_rate} | {sig} |")

    # --- Per-day breakdown ---
    from collections import defaultdict
    days: dict = defaultdict(lambda: {"tm": 0, "tg": 0, "nm": 0, "total": 0})
    for m in missed_data:
        day = m["timestamp"][:10]
        days[day]["total"] += 1
        days[day][m["outcome"][:2]] = days[day].get(m["outcome"][:2], 0) + 1
        if m.get("near_miss"):
            days[day]["nm"] += 1

    # Use per-day true/thank keys properly
    per_day: dict = defaultdict(lambda: {"true_missed": 0, "thank_god": 0, "near_miss": 0, "total": 0})
    for m in missed_data:
        day = m["timestamp"][:10]
        per_day[day]["total"] += 1
        per_day[day][m["outcome"]] = per_day[day].get(m["outcome"], 0) + 1
        if m.get("near_miss"):
            per_day[day]["near_miss"] += 1

    lines += [
        "",
        f"## Per-Day Breakdown (sample size context — need 20+ days before conclusions are valid)",
        "| Date | Analyzed | True Missed | Thank God | Near Miss | Miss Rate | Note |",
        "|------|----------|-------------|-----------|-----------|-----------|------|",
    ]
    for day in sorted(per_day.keys()):
        d = per_day[day]
        cnt = d["total"]
        tm = d["true_missed"]
        tg = d["thank_god"]
        nm = d["near_miss"]
        rate = f"{tm/cnt*100:.0f}%" if cnt else "0%"
        # Flag extreme days (today's crash/rally inflates numbers)
        note = "EXTREME DAY" if day == "2026-03-04" else ""
        lines.append(f"| {day} | {cnt} | {tm} | {tg} | {nm} | {rate} | {note} |")

    # --- Totals ---
    small_move_count = len(missed_data) - len(true_missed) - len(thank_god)
    lines += [
        "",
        "## Totals",
        f"- Total scans in window: {total}",
        f"- No setup: {no_setup} | AI rejected: {ai_rejected} | Low conf: {low_conf} | Traded: {traded} | Other: {other}",
        f"- Rejections analyzed: {len(missed_data)} | True missed: {len(true_missed)} | Near miss: {len(near_misses)} | Thank god: {len(thank_god)} | Small move: {small_move_count}",
        f"- True miss rate: {len(true_missed)/len(missed_data)*100:.0f}%" if missed_data else "- True miss rate: N/A",
        f"- WARNING: Only {len(per_day)} day(s) of data. Minimum 20 days needed for statistically valid conclusions.",
        "",
    ]

    return "\n".join(lines)


def _get_all_scans(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all scans ever recorded."""
    rows = conn.execute("SELECT * FROM scans ORDER BY timestamp ASC").fetchall()
    return [dict(r) for r in rows]


def run(full_history: bool = False):
    """Main entry point — run the analysis and write output.

    Args:
        full_history: If True, analyze all scans ever recorded instead of last 24h.
    """
    if not DB_PATH.exists():
        logger.warning(f"Database not found at {DB_PATH}")
        return

    conn = _connect_db()
    try:
        scans = _get_all_scans(conn) if full_history else _get_scans(conn, LOOKBACK_HOURS)
    finally:
        conn.close()

    if not scans:
        logger.info("No scans found — nothing to analyze.")
        return

    report = generate_report(scans)

    # Write main report (overwrite)
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(report)

    # Append one-line summary to log
    missed_data = _compute_missed_moves(scans)
    true_missed_count = sum(1 for m in missed_data if m["outcome"] == "true_missed")
    thank_god_count = sum(1 for m in missed_data if m["outcome"] == "thank_god")
    mode = "full-history" if full_history else "24h"
    summary_line = (
        f"{datetime.now().isoformat()} | mode={mode} scans={len(scans)} "
        f"rejections={len(missed_data)} true_missed={true_missed_count} thank_god={thank_god_count}\n"
    )
    with open(OUTPUT_LOG, "a") as f:
        f.write(summary_line)

    logger.info(f"Scan analysis written to {OUTPUT_MD} ({len(scans)} scans [{mode}], {true_missed_count} true missed, {thank_god_count} thank god)")
    print(f"Scan analysis [{mode}]: {len(scans)} scans | true_missed={true_missed_count} thank_god={thank_god_count} | Report: {OUTPUT_MD}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    full_history = "--all" in sys.argv
    run(full_history=full_history)
