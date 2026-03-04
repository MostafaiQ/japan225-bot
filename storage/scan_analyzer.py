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

logger = logging.getLogger(__name__)

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "storage" / "data" / "trading.db"
OUTPUT_MD = BASE_DIR / "storage" / "data" / "scan_analysis.md"
OUTPUT_LOG = BASE_DIR / "storage" / "data" / "scan_analysis.log"

# --- Thresholds ---
MISSED_MOVE_THRESHOLD = 150  # pts — flag if price moved 150+ pts in expected direction
LOOKBACK_HOURS = 24          # analyse scans from last 24h
PRICE_COMPARE_WINDOWS = [    # minutes after rejection to check price
    (30, "30min"),
    (60, "1hr"),
    (120, "2hr"),
]


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


def _find_price_after(scans: list[dict], rejection_time: str, minutes: int) -> float | None:
    """Find the price from a scan record closest to rejection_time + minutes."""
    try:
        t0 = datetime.fromisoformat(rejection_time)
    except (ValueError, TypeError):
        return None

    target = t0 + timedelta(minutes=minutes)
    target_minus = target - timedelta(minutes=5)  # 5-min tolerance window
    target_plus = target + timedelta(minutes=5)

    best = None
    best_delta = timedelta(hours=999)
    for s in scans:
        try:
            ts = datetime.fromisoformat(s["timestamp"])
        except (ValueError, TypeError):
            continue
        if target_minus <= ts <= target_plus:
            delta = abs(ts - target)
            if delta < best_delta and s.get("price"):
                best = s["price"]
                best_delta = delta

    return best


def _compute_missed_moves(scans: list[dict]) -> list[dict]:
    """For each rejection scan, compute price movement in expected direction."""
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
        price = scan["price"]
        direction = _infer_expected_direction(scan)
        if not direction:
            continue

        reason = _parse_rejection_reason(scan)
        rsi = _extract_rsi(scan)
        ts = scan["timestamp"]

        # Check price at each window
        moves = {}
        max_move = 0
        max_window = ""
        for window_min, window_label in PRICE_COMPARE_WINDOWS:
            later_price = _find_price_after(scans, ts, window_min)
            if later_price is not None:
                if direction == "LONG":
                    move = later_price - price
                else:
                    move = price - later_price
                moves[window_label] = round(move, 1)
                if move > max_move:
                    max_move = move
                    max_window = window_label
            else:
                moves[window_label] = None

        results.append({
            "timestamp": ts,
            "price": price,
            "rsi": rsi,
            "direction": direction,
            "reason": reason,
            "action": scan.get("action_taken", ""),
            "moves": moves,
            "max_move": round(max_move, 1),
            "max_window": max_window,
            "is_missed": max_move >= MISSED_MOVE_THRESHOLD,
        })

    return results


def _build_reason_summary(missed_data: list[dict]) -> dict[str, dict]:
    """Aggregate missed-move stats by rejection reason."""
    summary: dict[str, dict] = {}
    for item in missed_data:
        reason = item["reason"]
        if reason not in summary:
            summary[reason] = {"count": 0, "moves": [], "missed_count": 0}
        summary[reason]["count"] += 1
        if item["max_move"] > 0:
            summary[reason]["moves"].append(item["max_move"])
        if item["is_missed"]:
            summary[reason]["missed_count"] += 1
    return summary


def _build_rsi_buckets(missed_data: list[dict]) -> dict[str, dict]:
    """Bucket rejections by RSI range."""
    buckets = {
        "20-35": {"count": 0, "moves": []},
        "35-45": {"count": 0, "moves": []},
        "45-55": {"count": 0, "moves": []},
        "55-65": {"count": 0, "moves": []},
        "65-75": {"count": 0, "moves": []},
        "75+":   {"count": 0, "moves": []},
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
        if item["max_move"] > 0:
            buckets[bucket]["moves"].append(item["max_move"])
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

    # Compute missed moves for full 24h
    missed_data = _compute_missed_moves(scans)
    missed_moves = [m for m in missed_data if m["is_missed"]]

    # Biggest missed by direction
    long_missed = [m for m in missed_moves if m["direction"] == "LONG"]
    short_missed = [m for m in missed_moves if m["direction"] == "SHORT"]
    biggest_long = max(long_missed, key=lambda x: x["max_move"]) if long_missed else None
    biggest_short = max(short_missed, key=lambda x: x["max_move"]) if short_missed else None

    lines = [
        f"# Scan Analysis — Updated {now}",
        "",
        "## Last 2 Hours",
        f"- Scans: {recent_total} | No setup: {recent_no_setup} | AI rejected: {recent_ai_rejected} | Traded: {recent_traded}",
    ]

    if biggest_long:
        lines.append(
            f"- Biggest missed LONG: +{biggest_long['max_move']:.0f}pts "
            f"(rejected at {biggest_long['price']:.0f}, "
            f"+{biggest_long['max_move']:.0f}pts within {biggest_long['max_window']})"
        )
    else:
        lines.append("- Biggest missed LONG: none")

    if biggest_short:
        lines.append(
            f"- Biggest missed SHORT: +{biggest_short['max_move']:.0f}pts "
            f"(rejected at {biggest_short['price']:.0f}, "
            f"+{biggest_short['max_move']:.0f}pts within {biggest_short['max_window']})"
        )
    else:
        lines.append("- Biggest missed SHORT: none")

    # Missed moves table
    lines += [
        "",
        f"## Missed Moves (rejections where price moved {MISSED_MOVE_THRESHOLD}+ pts in expected direction)",
    ]

    if missed_moves:
        lines.append("| Time  | Price | RSI  | Direction | Rejection | Max Move | Window |")
        lines.append("|-------|-------|------|-----------|-----------|----------|--------|")
        for m in sorted(missed_moves, key=lambda x: -x["max_move"])[:20]:  # Top 20
            rsi_str = f"{m['rsi']:.1f}" if m.get("rsi") else "N/A"
            lines.append(
                f"| {_format_time(m['timestamp'])} | {m['price']:.0f} | {rsi_str} | "
                f"{m['direction']} | {m['reason']} | +{m['max_move']:.0f}pts | {m['max_window']} |"
            )
    else:
        lines.append("*No missed moves detected.*")

    # Rejection pattern summary
    reason_summary = _build_reason_summary(missed_data)
    lines += [
        "",
        f"## Rejection Pattern Summary ({LOOKBACK_HOURS}h)",
        "| Reason | Count | Avg Move After | Missed (150+pts) | Action Needed? |",
        "|--------|-------|----------------|-------------------|----------------|",
    ]
    for reason, data in sorted(reason_summary.items(), key=lambda x: -x[1]["count"]):
        avg_move = sum(data["moves"]) / len(data["moves"]) if data["moves"] else 0
        missed_ct = data["missed_count"]
        if missed_ct > 2 and avg_move > 120:
            action = "YES — gate too tight"
        elif missed_ct > 0 and avg_move > 80:
            action = "MAYBE — review"
        else:
            action = "No"
        lines.append(
            f"| {reason} | {data['count']} | +{avg_move:.0f}pts | {missed_ct} | {action} |"
        )

    # RSI at rejection
    rsi_buckets = _build_rsi_buckets(missed_data)
    lines += [
        "",
        "## RSI at Rejection",
        "| RSI Range | Rejections | Avg Move After | Should Trade? |",
        "|-----------|-----------|----------------|---------------|",
    ]
    for bucket, data in rsi_buckets.items():
        if data["count"] == 0:
            continue
        avg_move = sum(data["moves"]) / len(data["moves"]) if data["moves"] else 0
        if avg_move >= 150:
            should = "YES — missed!"
        elif avg_move >= 80:
            should = "Some valid"
        else:
            should = "No"
        lines.append(
            f"| {bucket} | {data['count']} | +{avg_move:.0f}pts | {should} |"
        )

    # 24h totals
    lines += [
        "",
        f"## 24h Totals",
        f"- Total scans: {total}",
        f"- No setup: {no_setup} | AI rejected: {ai_rejected} | Low conf: {low_conf} | Traded: {traded} | Other: {other}",
        f"- Missed moves (150+pts): {len(missed_moves)}",
        "",
    ]

    return "\n".join(lines)


def run():
    """Main entry point — run the analysis and write output."""
    if not DB_PATH.exists():
        logger.warning(f"Database not found at {DB_PATH}")
        return

    conn = _connect_db()
    try:
        scans = _get_scans(conn, LOOKBACK_HOURS)
    finally:
        conn.close()

    if not scans:
        logger.info("No scans in the last 24h — nothing to analyze.")
        return

    report = generate_report(scans)

    # Write main report (overwrite)
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(report)

    # Append one-line summary to log
    missed_data = _compute_missed_moves(scans)
    missed_count = sum(1 for m in missed_data if m["is_missed"])
    summary_line = (
        f"{datetime.now().isoformat()} | scans={len(scans)} "
        f"rejections={len(missed_data)} missed={missed_count}\n"
    )
    with open(OUTPUT_LOG, "a") as f:
        f.write(summary_line)

    logger.info(f"Scan analysis written to {OUTPUT_MD} ({len(scans)} scans, {missed_count} missed)")
    print(f"Scan analysis: {len(scans)} scans, {missed_count} missed moves. Report: {OUTPUT_MD}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    run()
