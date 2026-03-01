#!/usr/bin/env python3
"""
Japan 225 Bot — Health Check
Run at the start of every session instead of manual commands.
Usage: python3 healthcheck.py
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "storage", "data")
W = 60

def h(title):
    print(f"\n{'─' * W}")
    print(f"  {title}")
    print(f"{'─' * W}")

def ok(msg):  print(f"  ✓  {msg}")
def warn(msg): print(f"  ⚠  {msg}")
def err(msg):  print(f"  ✗  {msg}")

def svc_status(name):
    r = subprocess.run(
        ["systemctl", "is-active", name],
        capture_output=True, text=True
    )
    active = r.stdout.strip() == "active"
    r2 = subprocess.run(
        ["systemctl", "show", name, "--property=ActiveEnterTimestamp"],
        capture_output=True, text=True
    )
    since = r2.stdout.strip().replace("ActiveEnterTimestamp=", "") or "unknown"
    return active, since

def run_tests():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no", "--no-header"],
        capture_output=True, text=True, cwd=ROOT
    )
    lines = (r.stdout + r.stderr).strip().splitlines()
    # Last line: e.g. "234 passed in 1.21s" or "1 failed, 233 passed in 1.21s"
    summary = lines[-1] if lines else "unknown"
    passed = "failed" not in summary
    return passed, summary

def git_status():
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()
    ahead = subprocess.run(
        ["git", "rev-list", "--count", "HEAD@{upstream}..HEAD"],
        capture_output=True, text=True, cwd=ROOT
    ).stdout.strip() or "0"
    return branch, dirty, ahead

def db_stats():
    db_path = os.path.join(DATA, "trading.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # trades table columns
        cur.execute("PRAGMA table_info(trades)")
        cols = [r[1] for r in cur.fetchall()]
        # count all trades
        cur.execute("SELECT COUNT(*) FROM trades")
        total = cur.fetchone()[0]
        # closed trades
        result_col = "result" if "result" in cols else ("outcome" if "outcome" in cols else None)
        if result_col:
            cur.execute(f"""
                SELECT
                    COUNT(*) as closed,
                    SUM(CASE WHEN {result_col}='WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN {result_col}='LOSS' THEN 1 ELSE 0 END) as losses,
                    ROUND(AVG(pnl),2) as avg_pnl
                FROM trades WHERE closed_at IS NOT NULL
            """)
            row = dict(cur.fetchone())
        else:
            row = {"closed": 0, "wins": 0, "losses": 0, "avg_pnl": None}
        # recent trades
        cur.execute("""
            SELECT direction, entry_price, exit_price, pnl, result, session, setup_type
            FROM trades WHERE closed_at IS NOT NULL
            ORDER BY id DESC LIMIT 5
        """) if result_col else None
        recent = [dict(r) for r in cur.fetchall()] if result_col else []
        conn.close()
        return {"total": total, **row, "recent": recent}
    except Exception as e:
        return {"error": str(e)}

def bot_state():
    path = os.path.join(DATA, "bot_state.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def overrides():
    path = os.path.join(DATA, "dashboard_overrides.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def chat_costs():
    path = os.path.join(DATA, "chat_costs.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        total = sum(e.get("cost_usd", 0) for e in data) if isinstance(data, list) else 0
        return {"total": total, "calls": len(data) if isinstance(data, list) else 0}
    except Exception:
        return None

def recent_errors():
    r = subprocess.run(
        ["journalctl", "-u", "japan225-bot", "--no-pager", "-n", "200",
         "--output=short", "--since", "1 hour ago"],
        capture_output=True, text=True
    )
    lines = r.stdout.splitlines()
    errors = [l for l in lines if any(k in l for k in ["ERROR", "CRITICAL", "IG auth failed", "503", "Exception"])]
    return errors[-5:]  # last 5 errors only

# ─────────────────────────────────────────────────────────────
print("=" * W)
print(f"  JAPAN 225 BOT — HEALTH CHECK")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print("=" * W)

# SERVICES
h("SERVICES")
for svc in ["japan225-bot", "japan225-dashboard", "japan225-ngrok"]:
    active, since = svc_status(svc)
    status = f"since {since}" if active else "INACTIVE"
    (ok if active else err)(f"{svc:<28} {status}")

# TESTS
h("TESTS")
passed, summary = run_tests()
(ok if passed else err)(summary)

# GIT
h("GIT")
branch, dirty, ahead = git_status()
ok(f"Branch: {branch}")
if dirty:
    warn(f"Uncommitted changes:\n{dirty}")
else:
    ok("Working tree clean")
if ahead != "0":
    warn(f"{ahead} commit(s) ahead of origin — push needed")
else:
    ok("Up to date with origin")

# BOT STATE
h("BOT STATE")
state = bot_state()
if state is None:
    warn("bot_state.json missing (IG likely down / first boot)")
else:
    ig = state.get("ig_connected", False)
    (ok if ig else warn)(f"IG connected: {ig}")
    pos = state.get("position_open", False)
    phase = state.get("current_phase", "N/A")
    print(f"  {'Position open:':<20} {pos}  (phase: {phase})")
    paused = state.get("scanning_paused", False)
    (warn if paused else ok)(f"Scanning paused: {paused}")
    last = state.get("last_scan", "N/A")
    print(f"  {'Last scan:':<20} {last}")

# LIVE TRADES
h("LIVE TRADES")
stats = db_stats()
if stats is None:
    warn("DB not found")
elif "error" in stats:
    err(f"DB error: {stats['error']}")
else:
    closed = stats.get("closed") or 0
    wins   = stats.get("wins") or 0
    losses = stats.get("losses") or 0
    avg    = stats.get("avg_pnl")
    wr     = (wins / closed * 100) if closed > 0 else 0
    line = f"{closed} closed  |  {wins}W {losses}L  |  WR {wr:.0f}%  |  Avg PnL: {avg or 'N/A'}"
    print(f"  {line}")
    if stats.get("recent"):
        print("  Recent trades:")
        for t in stats["recent"]:
            r = t.get("result", "?")
            d = t.get("direction", "?")
            pnl = t.get("pnl", 0)
            stype = t.get("setup_type", "?")
            sess = t.get("session", "?")
            sign = "+" if (pnl or 0) >= 0 else ""
            print(f"    {d:<5} {r:<5} {sign}{pnl:>7.1f}pts  [{stype} / {sess}]")

# OVERRIDES
h("CONFIG OVERRIDES (dashboard_overrides.json)")
ov = overrides()
if not ov:
    ok("None active (using settings.py defaults)")
else:
    for k, v in ov.items():
        print(f"  {k:<35} = {v}")

# COSTS
h("CHAT / AI COSTS")
print("  Dashboard chat: Claude Code CLI (costs in Anthropic console, not tracked here)")

# ERRORS
h("RECENT ERRORS (last hour)")
errors = recent_errors()
if not errors:
    ok("No errors in last hour")
else:
    for e in errors:
        print(f"  {e}")

print(f"\n{'=' * W}\n")
