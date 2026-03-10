"""
Conditional Probability Tracker — Quant Feedback Loop

Reads closed trades from SQLite, computes conditional win rates and Kelly fractions
per (session, direction, confidence_tier) combination.

Core insight from quantitative finance:
    P(win | RSI=55-65, session=London) is the signal.
    P(win) alone is noise.

Kelly criterion: f* = (p * b - q) / b
    where p = win rate, q = 1-p, b = avg_win / avg_loss
    We use quarter-Kelly (0.25 * f*) as a safety margin.

Writes: storage/data/probability_tracker.md
        storage/data/probability_tracker.json  (for dashboard consumption)

Usage:
    python -m storage.probability_tracker
    python -m storage.probability_tracker --min-trades 5  # lower threshold for early data
"""
import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from config.settings import BASE_DIR, DB_PATH

OUTPUT_MD = BASE_DIR / "storage" / "data" / "probability_tracker.md"
OUTPUT_JSON = BASE_DIR / "storage" / "data" / "probability_tracker.json"

# Minimum trades in a bucket before reporting a win rate
MIN_TRADES_FOR_ESTIMATE = 10
# Maximum Kelly fraction to use (safety cap — never bet >25% even at full Kelly)
MAX_KELLY_FRACTION = 0.25


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_closed_trades(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all closed trades with a clean result (TP_HIT or SL_HIT)."""
    rows = conn.execute(
        """
        SELECT direction, session, setup_type, confidence, pnl, result,
               entry_price, exit_price, stop_loss, take_profit, opened_at
        FROM trades
        WHERE result IN ('TP_HIT', 'SL_HIT', 'BREAKEVEN', 'TRAILING_STOP')
        ORDER BY id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _confidence_tier(confidence: int) -> str:
    if confidence >= 85:
        return "high (85+)"
    elif confidence >= 75:
        return "mid (75-84)"
    elif confidence >= 65:
        return "low (65-74)"
    else:
        return "below-threshold (<65)"


def _is_win(trade: dict) -> bool:
    return trade["result"] in ("TP_HIT",) or trade["pnl"] > 0


def _kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Kelly criterion: f* = (p*b - q) / b where b = avg_win/avg_loss."""
    if avg_loss <= 0 or win_rate <= 0:
        return 0.0
    b = avg_win / avg_loss
    q = 1 - win_rate
    f = (win_rate * b - q) / b
    # Quarter-Kelly for safety. Negative Kelly = don't trade this bucket.
    return round(max(0.0, min(f * 0.25, MAX_KELLY_FRACTION)), 4)


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. More accurate than normal approx for small n."""
    if n == 0:
        return 0.0, 1.0
    p_hat = wins / n
    denom = 1 + z**2 / n
    center = (p_hat + z**2 / (2 * n)) / denom
    margin = z * ((p_hat * (1 - p_hat) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return round(max(0, center - margin), 3), round(min(1, center + margin), 3)


def compute_conditionals(trades: list[dict]) -> dict:
    """Build conditional probability table keyed by (session, direction, confidence_tier)."""
    buckets: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "win_pts": [], "loss_pts": []})

    for t in trades:
        key = (
            t.get("session", "unknown").lower(),
            t.get("direction", "?").upper(),
            _confidence_tier(t.get("confidence", 0)),
        )
        is_win = _is_win(t)
        pts = abs(t.get("pnl", 0))  # crude proxy — actual pts = pnl / lots
        if is_win:
            buckets[key]["wins"] += 1
            buckets[key]["win_pts"].append(pts)
        else:
            buckets[key]["losses"] += 1
            buckets[key]["loss_pts"].append(pts)

    result = {}
    for key, data in buckets.items():
        n = data["wins"] + data["losses"]
        win_rate = data["wins"] / n if n > 0 else 0.0
        avg_win = sum(data["win_pts"]) / len(data["win_pts"]) if data["win_pts"] else 0.0
        avg_loss = sum(data["loss_pts"]) / len(data["loss_pts"]) if data["loss_pts"] else 0.0
        lo, hi = _wilson_interval(data["wins"], n)
        kelly = _kelly_fraction(win_rate, avg_win, avg_loss)
        result[str(key)] = {
            "session": key[0],
            "direction": key[1],
            "confidence_tier": key[2],
            "n": n,
            "wins": data["wins"],
            "losses": data["losses"],
            "win_rate": round(win_rate, 3),
            "win_rate_ci_95": [lo, hi],
            "avg_win_pnl": round(avg_win, 2),
            "avg_loss_pnl": round(avg_loss, 2),
            "kelly_quarter": kelly,
            "reliable": n >= MIN_TRADES_FOR_ESTIMATE,
        }
    return result


def generate_report(trades: list[dict], conditionals: dict) -> str:
    from config.settings import display_now
    now = display_now().strftime("%Y-%m-%dT%H:%M:%S")
    n_total = len(trades)
    n_wins = sum(1 for t in trades if _is_win(t))
    overall_wr = n_wins / n_total if n_total else 0

    lines = [
        f"# Probability Tracker — Updated {now}",
        f"# Applying: Conditional Probability (Blitzstein Ch.1), Kelly Criterion, Wilson CI",
        "",
        f"## ⚠ Sample Size Warning",
        f"- **Total clean trades: {n_total}** (need ≥50 per condition for reliable estimates)",
        f"- **Minimum for ANY conclusion: {MIN_TRADES_FOR_ESTIMATE} trades per bucket**",
        f"- Overall win rate: {overall_wr:.0%} ({n_wins}W / {n_total - n_wins}L)",
        f"- All estimates below have wide confidence intervals until sample grows",
        "",
        "## Conditional Win Rates by (Session × Direction × Confidence)",
        "*This is the quant signal. P(win | conditions) not P(win) globally.*",
        "*Wilson 95% CI shown — the true win rate likely lies in this range.*",
        "*Kelly quarter-fraction: safe position size multiplier given current evidence.*",
        "",
        "| Session | Dir | Confidence | n | Win Rate | 95% CI | Avg Win | Avg Loss | Kelly¼ | Reliable? |",
        "|---------|-----|------------|---|----------|--------|---------|----------|--------|-----------|",
    ]

    sorted_buckets = sorted(
        conditionals.values(),
        key=lambda x: (-x["n"], x["session"], x["direction"])
    )

    for b in sorted_buckets:
        ci = f"{b['win_rate_ci_95'][0]:.0%}–{b['win_rate_ci_95'][1]:.0%}"
        reliable = "✓" if b["reliable"] else f"✗ n={b['n']}"
        kelly_str = f"{b['kelly_quarter']:.2f}" if b["kelly_quarter"] > 0 else "0 (don't trade)"
        lines.append(
            f"| {b['session']} | {b['direction']} | {b['confidence_tier']} | {b['n']} | "
            f"{b['win_rate']:.0%} | {ci} | {b['avg_win_pnl']:.1f} | {b['avg_loss_pnl']:.1f} | "
            f"{kelly_str} | {reliable} |"
        )

    if not sorted_buckets:
        lines.append("*No clean trades yet (TP_HIT or SL_HIT). Run the bot longer.*")

    lines += [
        "",
        "## What This Will Tell You (once data grows)",
        "- Which session+direction combos have positive expected value",
        "- Which confidence tiers are worth trading (Kelly > 0)",
        "- Where the bot should size up vs size down",
        "- Whether the AI's confidence score is calibrated (high conf → high win rate?)",
        "",
        f"## Estimation Error Context (from the quant literature)",
        "- With n=10 trades and 60% win rate: 95% CI is 26%–88% — USELESS",
        "- With n=50 trades and 60% win rate: 95% CI is 46%–73% — starting to matter",
        "- With n=100 trades and 60% win rate: 95% CI is 50%–69% — actionable",
        "- **This is why we don't touch RSI thresholds on 3 days of data**",
        "",
    ]

    return "\n".join(lines)


def run(min_trades: int = MIN_TRADES_FOR_ESTIMATE):
    if not DB_PATH.exists():
        logger.warning(f"Database not found at {DB_PATH}")
        return

    conn = _connect_db()
    try:
        trades = _load_closed_trades(conn)
    finally:
        conn.close()

    conditionals = compute_conditionals(trades)
    report = generate_report(trades, conditionals)

    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(report)
    OUTPUT_JSON.write_text(json.dumps(conditionals, indent=2))

    logger.info(f"Probability tracker written ({len(trades)} trades, {len(conditionals)} buckets)")
    print(f"Probability tracker: {len(trades)} trades | {len(conditionals)} condition buckets | {OUTPUT_MD}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    run()
