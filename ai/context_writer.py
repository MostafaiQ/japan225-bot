"""
Context folder writer — builds storage/context/*.md before every AI call.

These files give the Claude Code CLI subprocess richer, inspectable context
than raw inline JSON. They also serve as a human-readable audit trail of
exactly what information each AI tier had when it made its decision.

Updated by monitor.py immediately before the Haiku pre-gate call.
All three tiers (Haiku, Sonnet, Opus) benefit from the same snapshot.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CONTEXT_DIR = Path(__file__).parent.parent / "storage" / "context"


# ─── Public entry point ────────────────────────────────────────────────────────

def write_context(
    indicators: dict,
    market_context: dict,
    web_research: dict,
    recent_scans: list,
    recent_trades: list,
    live_edge_block: str = "",
    local_confidence: dict = None,
    prescreen_direction: str = None,
) -> None:
    """Write all context files. Called once before the Haiku pre-gate."""
    try:
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        _write_market_snapshot(indicators, market_context, local_confidence, prescreen_direction)
        _write_recent_activity(recent_scans, recent_trades)
        _write_macro(web_research)
        _write_live_edge(live_edge_block)
        logger.debug("Context files written to storage/context/")
    except Exception as e:
        logger.warning(f"context_writer failed (non-fatal): {e}")


# ─── Individual file writers ───────────────────────────────────────────────────

def _write_market_snapshot(
    indicators: dict,
    market_context: dict,
    local_confidence: dict,
    prescreen_direction: str,
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Market Snapshot — {now}",
        "",
        f"**Session:** {market_context.get('session_name', '?')}  "
        f"| **Mode:** {market_context.get('trading_mode', '?')}",
    ]

    if prescreen_direction:
        setup_type = market_context.get("prescreen_setup_type", "")
        reason = market_context.get("prescreen_reasoning", "")
        lines.append(f"**Pre-screen:** {prescreen_direction} {setup_type}")
        if reason:
            lines.append(f"> {reason}")

    if local_confidence:
        criteria = local_confidence.get("criteria", {})
        passed = [k for k, v in criteria.items() if v]
        failed = [k for k, v in criteria.items() if not v]
        lines += [
            "",
            f"**Local confidence:** {local_confidence.get('score', '?')}% "
            f"({local_confidence.get('passed_criteria', '?')}/{local_confidence.get('total_criteria', 10)} criteria)",
            f"✓ Passed: {', '.join(passed) or 'none'}",
            f"✗ Failed: {', '.join(failed) or 'none'}",
        ]

    lines += ["", "## Timeframe Indicators"]

    TF_KEYS = [
        ("Daily (D1)",       ["daily", "d1", "1d"]),
        ("4 Hour (4H)",      ["4h", "tf_4h", "4hour", "h4"]),
        ("15 Minute (15M)",  ["15m", "tf_15m", "15min", "m15"]),
    ]
    FIELDS = [
        ("price",           "Price"),
        ("rsi",             "RSI"),
        ("ema_50",          "EMA 50"),
        ("ema_200",         "EMA 200"),
        ("bb_upper",        "BB Upper"),
        ("bb_mid",          "BB Mid"),
        ("bb_lower",        "BB Lower"),
        ("volume_signal",   "Volume Signal"),
        ("volume_ratio",    "Volume Ratio"),
        ("swing_high_20",   "Swing High (20)"),
        ("swing_low_20",    "Swing Low (20)"),
        ("bounce_starting", "Bounce Starting"),
        ("above_ema50",     "Above EMA50"),
    ]

    for tf_label, tf_keys in TF_KEYS:
        tf = {}
        for k in tf_keys:
            if k in indicators and isinstance(indicators[k], dict):
                tf = indicators[k]
                break
        if not tf:
            continue
        lines.append(f"\n### {tf_label}")
        for key, label in FIELDS:
            val = tf.get(key)
            if val is not None and val != "":
                lines.append(f"- **{label}:** {val}")

    (CONTEXT_DIR / "market_snapshot.md").write_text("\n".join(lines))


def _write_recent_activity(recent_scans: list, recent_trades: list) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Recent Activity — {now}", ""]

    lines.append("## Recent Scans (last 15)")
    for s in (recent_scans or [])[-15:]:
        ts = str(s.get("timestamp", "?"))[:16]
        ses = s.get("session", "?")
        price = s.get("price", "?")
        action = s.get("action_taken", "?")
        conf = s.get("confidence", "")
        conf_str = f" conf={conf}%" if conf else ""
        lines.append(f"- `{ts}` {ses} {price} → **{action}**{conf_str}")

    lines.append("\n## Recent Closed Trades (last 10)")
    if not recent_trades:
        lines.append("- No closed trades yet.")
    else:
        for t in (recent_trades or [])[-10:]:
            pnl = t.get("pnl") or 0
            outcome = "WIN ✓" if pnl > 0 else "LOSS ✗"
            opened = str(t.get("opened_at", "?"))[:16]
            dur = t.get("duration_minutes")
            dur_str = f" {dur}min" if dur else ""
            lines.append(
                f"- `{opened}` {t.get('direction','?')} {t.get('setup_type','?')} "
                f"conf={t.get('confidence','?')}% → {outcome} ${pnl:+.0f}{dur_str}"
            )

        # Win rate summary by setup type
        wins_by_type: dict[str, list] = {}
        for t in recent_trades:
            st = t.get("setup_type", "unknown")
            pnl = t.get("pnl") or 0
            wins_by_type.setdefault(st, []).append(pnl > 0)
        lines.append("\n### Win Rates by Setup (recent trades)")
        for st, results in wins_by_type.items():
            wr = sum(results) / len(results) * 100
            lines.append(f"- {st}: {wr:.0f}% ({sum(results)}/{len(results)})")

    (CONTEXT_DIR / "recent_activity.md").write_text("\n".join(lines))


def _write_macro(web_research: dict) -> None:
    if not web_research:
        (CONTEXT_DIR / "macro.md").write_text("# Macro Context\n\nUnavailable.\n")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Macro Context — {now}", ""]

    vix = web_research.get("vix")
    jpy = web_research.get("usd_jpy")
    fg  = web_research.get("fear_greed")
    if vix: lines.append(f"**VIX:** {vix:.2f}  {'(elevated risk >25)' if vix > 25 else ''}")
    if jpy: lines.append(f"**USD/JPY:** {jpy:.2f}  (JPY strength = bearish Nikkei signal)")
    if fg:  lines.append(f"**Fear & Greed:** {fg}")

    news = web_research.get("nikkei_news") or []
    if news:
        lines.append("\n**Recent Headlines:**")
        for n in news[:4]:
            lines.append(f"- {n}")

    cal = web_research.get("economic_calendar") or []
    high = [e for e in cal if isinstance(e, dict) and e.get("impact") == "HIGH"]
    lines.append("\n**High-Impact Economic Events (next 8h):**")
    if high:
        for e in high[:5]:
            lines.append(f"- {e.get('time','?')} {e.get('country','?')} — {e.get('event','?')}")
    else:
        lines.append("- None scheduled.")

    (CONTEXT_DIR / "macro.md").write_text("\n".join(lines))


def _write_live_edge(live_edge_block: str) -> None:
    if not live_edge_block:
        (CONTEXT_DIR / "live_edge.md").write_text(
            "# Live Edge Stats\n\nNo trade history yet — bot is new.\n"
        )
        return
    content = f"# Live Edge Stats — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n\n{live_edge_block}\n"
    (CONTEXT_DIR / "live_edge.md").write_text(content)
