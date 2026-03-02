"""
Dashboard chat backend — powered by Claude Code CLI.

Spawns `claude --print --dangerously-skip-permissions` as a subprocess.
Full tool access: Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch.

Context strategy (deadlock prevention):
  - History: rolling 3-sentence summary of old turns + last 2 raw turns.
    History payload is capped at ~650 tokens regardless of conversation length.
  - Bot state: current bot_state.json injected at top of every prompt so
    Claude Code never needs to read files for basic status questions.
  - CLAUDE.md in project root instructs Claude Code to use digests, not raw files.

Usage tracking:
  - Every query is classified by intent and logged to chat_usage.json.
  - When a query type hits 5+ uses in a week, a new skill is auto-drafted.
"""
import json
import logging
import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
BOT_STATE_FILE = PROJECT_ROOT / "storage" / "data" / "bot_state.json"
CHAT_USAGE_FILE = PROJECT_ROOT / "storage" / "data" / "chat_usage.json"
SKILLS_DIR = Path.home() / ".claude" / "skills"

CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"
CHAT_COSTS_FILE = PROJECT_ROOT / "storage" / "data" / "chat_costs.json"

# Rough cost estimate: claude-sonnet-4-5 (what claude CLI uses by default)
# $3/M input tokens, $15/M output tokens. 1 token ≈ 4 chars.
_SONNET_INPUT_PER_CHAR  = 3.0  / 1_000_000 / 4   # $/char input
_SONNET_OUTPUT_PER_CHAR = 15.0 / 1_000_000 / 4   # $/char output

# Rolling summary: keep last 2 raw turns + a compressed summary of everything older.
# Summary is maintained as a single paragraph, updated after each assistant reply.
MAX_RAW_TURNS = 2        # raw turns always kept
SUMMARY_MAX_CHARS = 600  # ~150 tokens for the rolling summary

# Query intent classification (keyword → type)
QUERY_PATTERNS = {
    "trade_review":    ["review trade", "what happened", "why did it lose", "why did it win",
                        "last trade", "recent trade", "trade history"],
    "strategy_health": ["win rate", "how many trades", "performance", "strategy working",
                        "setup working", "edge", "profitable"],
    "cost_report":     ["api cost", "how much spent", "token usage", "cost report",
                        "spending", "claude cost"],
    "deploy_check":    ["deployment", "is it running", "systemd", "health check",
                        "services", "bot running", "monitor running"],
    "prompt_audit":    ["improve prompt", "what should i add", "make it smarter",
                        "prompt performance", "why approved", "why rejected"],
    "status":          ["what's happening", "current position", "position open",
                        "balance", "what is the bot doing", "status"],
}


def chat(message: str, history: list[dict]) -> str:
    """
    Send message to Claude Code CLI. Returns full response text.

    history: list of {"role": "user"|"assistant", "content": str}
              Last entry is expected to be a summary dict if history is long:
              {"role": "summary", "content": str}
    """
    _track_usage(message)
    prompt = _build_prompt(message, history)

    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    env.pop("ANTHROPIC_API_KEY", None)  # force OAuth (Claude Max subscription), not the trading API key

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=180,
        )
        response = result.stdout.strip()
        if not response:
            stderr = result.stderr.strip()
            if result.returncode != 0:
                response = f"Claude Code failed (exit {result.returncode}). {stderr[:300] if stderr else 'No output.'}"
            else:
                response = stderr if stderr else "(no response — Claude Code returned empty output)"
        _log_chat_cost(prompt, response)
        return response

    except subprocess.TimeoutExpired:
        return "Claude Code timed out (3 min limit). Try a more specific question."
    except FileNotFoundError:
        return f"Claude Code binary not found at {CLAUDE_BIN}. Check installation."
    except Exception as e:
        return f"Error spawning Claude Code: {e}"


def compress_history(history: list[dict]) -> list[dict]:
    """
    Compress history to: [optional summary turn] + last MAX_RAW_TURNS pairs.
    Call this after receiving an assistant reply, before saving to chat_history.json.
    Returns the new compressed history list.
    """
    # Separate summary entry (always first if present) from raw turns
    summary_entry = None
    raw_turns = []
    for h in history:
        if h.get("role") == "summary":
            summary_entry = h
        else:
            raw_turns.append(h)

    # If we have more raw turns than the keep limit, absorb oldest into summary
    pairs_to_keep = MAX_RAW_TURNS * 2  # user + assistant per pair
    if len(raw_turns) > pairs_to_keep:
        to_absorb = raw_turns[:-pairs_to_keep]
        raw_turns = raw_turns[-pairs_to_keep:]

        existing_summary = summary_entry["content"] if summary_entry else ""
        new_summary = _absorb_into_summary(existing_summary, to_absorb)
        summary_entry = {"role": "summary", "content": new_summary}

    result = []
    if summary_entry:
        result.append(summary_entry)
    result.extend(raw_turns)
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_prompt(message: str, history: list[dict]) -> str:
    """Build the full prompt: state snapshot + compressed history + new message."""
    parts = []

    # 1. Live bot state snapshot (replaces file reads for status questions)
    state_block = _load_bot_state_block()
    if state_block:
        parts.append(state_block)

    # 2. Compressed conversation history
    if history:
        history_block = _format_history(history)
        if history_block:
            parts.append(history_block)

    # 3. New message
    parts.append(f"Human: {message}")
    return "\n\n".join(parts)


def _load_bot_state_block() -> str:
    """Load bot_state.json as a compact status block (~300 tokens max)."""
    try:
        if not BOT_STATE_FILE.exists():
            return ""
        state = json.loads(BOT_STATE_FILE.read_text())
        pos = state.get("position", {})
        scan = state.get("last_scan", {})
        acct = state.get("account", {})

        lines = ["--- BOT STATE (live) ---"]
        lines.append(f"Status: {'POSITION OPEN' if pos.get('has_open') else 'FLAT'} | "
                     f"Mode: {state.get('trading_mode', '?')} | "
                     f"Paused: {state.get('scanning_paused', False)}")

        if pos.get("has_open"):
            lines.append(f"Position: {pos.get('direction')} @ {pos.get('entry_price')} | "
                         f"Phase: {pos.get('phase')} | "
                         f"SL: {pos.get('stop_level')} | TP: {pos.get('limit_level')}")

        if acct:
            lines.append(f"Balance: ${acct.get('balance', '?')} | "
                         f"P&L today: ${acct.get('daily_pnl', '?')} | "
                         f"Consec losses: {acct.get('consecutive_losses', 0)}")

        if scan:
            lines.append(f"Last scan: {scan.get('timestamp', '?')[:16]} | "
                         f"Session: {scan.get('session', '?')} | "
                         f"Setup: {scan.get('setup_found', False)}")

        return "\n".join(lines)
    except Exception:
        return ""


def _format_history(history: list[dict]) -> str:
    """Format compressed history for the prompt."""
    lines = ["--- Conversation history ---"]
    for entry in history:
        role = entry.get("role", "")
        content = str(entry.get("content", "")).strip()
        if not content:
            continue
        if role == "summary":
            lines.append(f"[Earlier context summary]: {content}")
        elif role == "user":
            lines.append(f"Human: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
    return "\n".join(lines)


def _absorb_into_summary(existing: str, turns: list[dict]) -> str:
    """
    Compress old turns into a brief summary paragraph.
    Keeps the summary under SUMMARY_MAX_CHARS by truncating oldest content.
    Does NOT call an LLM — pure text compression to avoid recursive cost.
    """
    # Build a compact representation of the turns being absorbed
    absorbed_lines = []
    for t in turns:
        role = t.get("role", "")
        content = str(t.get("content", "")).strip()
        if not content or role == "summary":
            continue
        # Truncate long entries to first 120 chars
        snippet = content[:120].replace("\n", " ")
        if len(content) > 120:
            snippet += "…"
        label = "User" if role == "user" else "Bot"
        absorbed_lines.append(f"{label}: {snippet}")

    new_part = " | ".join(absorbed_lines)

    # Combine with existing summary, truncate to budget
    combined = f"{existing} || {new_part}" if existing else new_part
    if len(combined) > SUMMARY_MAX_CHARS:
        combined = combined[-SUMMARY_MAX_CHARS:]
        # Don't cut mid-word
        combined = combined[combined.index(" ") + 1:] if " " in combined else combined

    return combined


def _log_chat_cost(prompt: str, response: str) -> None:
    """Estimate cost from char count and append to chat_costs.json (max 500 entries)."""
    try:
        cost = len(prompt) * _SONNET_INPUT_PER_CHAR + len(response) * _SONNET_OUTPUT_PER_CHAR
        est_tokens = len(prompt) // 4 + len(response) // 4
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "cost_usd": round(cost, 6),
            "input_chars": len(prompt),
            "output_chars": len(response),
            "est_tokens": est_tokens,
        }
        data: list = []
        if CHAT_COSTS_FILE.exists():
            try:
                data = json.loads(CHAT_COSTS_FILE.read_text())
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
        data.append(entry)
        if len(data) > 500:
            data = data[-500:]
        CHAT_COSTS_FILE.write_text(json.dumps(data))
    except Exception as e:
        logger.debug(f"_log_chat_cost failed (non-fatal): {e}")


def _track_usage(message: str) -> None:
    """Classify query intent and log to chat_usage.json. Auto-drafts skills at threshold."""
    try:
        intent = _classify_query(message)
        today = date.today().isoformat()

        usage: dict = {}
        if CHAT_USAGE_FILE.exists():
            usage = json.loads(CHAT_USAGE_FILE.read_text())

        week_key = _iso_week()
        usage.setdefault(week_key, {})
        usage[week_key].setdefault(intent, 0)
        usage[week_key][intent] += 1

        # Auto-draft skill if threshold hit
        count = usage[week_key][intent]
        if count in (5, 10, 20):  # trigger at 5, then log again at 10/20
            _maybe_draft_skill(intent, count)

        CHAT_USAGE_FILE.write_text(json.dumps(usage, indent=2))
    except Exception as e:
        logger.debug(f"Usage tracking failed (non-fatal): {e}")


def _classify_query(message: str) -> str:
    """Return intent type string for a message."""
    msg_lower = message.lower()
    for intent, keywords in QUERY_PATTERNS.items():
        if any(kw in msg_lower for kw in keywords):
            return intent
    return "other"


def _iso_week() -> str:
    """Return ISO week key like '2026-W09'."""
    today = date.today()
    return f"{today.year}-W{today.isocalendar()[1]:02d}"


def _maybe_draft_skill(intent: str, count: int) -> None:
    """
    When a query type reaches threshold uses in a week, auto-draft a skill file
    in ~/.claude/skills/ if one doesn't already exist for that intent.
    """
    skill_file = SKILLS_DIR / f"{intent}.md"
    if skill_file.exists():
        return  # Skill already exists, nothing to do

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    # Templates keyed by intent
    templates = {
        "trade_review": _skill_trade_review(),
        "strategy_health": _skill_strategy_health(),
        "cost_report": _skill_cost_report(),
        "deploy_check": _skill_deploy_check(),
        "prompt_audit": _skill_prompt_audit(),
    }

    content = templates.get(intent)
    if content:
        skill_file.write_text(content)
        logger.info(f"Auto-drafted skill: {skill_file} (triggered at {count} uses this week)")


# ── Skill templates ────────────────────────────────────────────────────────────

def _skill_trade_review() -> str:
    return """\
# /trade-review skill
# Auto-generated by claude_client.py usage tracker.
# Invoked when user asks to review recent trades.

Read storage/data/trading.db:
```bash
sqlite3 storage/data/trading.db "
SELECT trade_number, direction, setup_type, session,
       confidence, pnl, result, duration_minutes, phase_at_close
FROM trades ORDER BY id DESC LIMIT 10"
```

Then for the worst-performing recent trade, read its ai_analysis field:
```bash
sqlite3 storage/data/trading.db "
SELECT trade_number, pnl, ai_analysis, notes
FROM trades ORDER BY pnl ASC LIMIT 1"
```

Format output as:
1. Compact table of last 10 trades (number, direction, setup, session, conf, P&L, result)
2. Worst trade breakdown: what did AI say vs what actually happened?
3. One-sentence diagnosis of what context was missing from the AI's reasoning.
"""


def _skill_strategy_health() -> str:
    return """\
# /strategy-health skill
# Auto-generated. Invoked for performance/win-rate queries.

Query trade stats by setup type and session (last 20 closed trades):
```bash
sqlite3 storage/data/trading.db "
SELECT setup_type, session,
       COUNT(*) as trades,
       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
       ROUND(AVG(pnl), 2) as avg_pnl
FROM trades WHERE pnl IS NOT NULL
ORDER BY id DESC LIMIT 20" | sort
```

Backtest baselines: bb_mid_bounce=47% WR | bb_lower_bounce=45% | Tokyo=49% | London=44% | NY=48%

Flag any category >10% below baseline.
Recommend: if a setup type is consistently underperforming, raise MIN_CONFIDENCE for it.
"""


def _skill_cost_report() -> str:
    return """\
# /cost-report skill
# Auto-generated. Invoked for API cost queries.

```bash
sqlite3 storage/data/trading.db "
SELECT
  (SELECT COALESCE(SUM(api_cost),0) FROM scans) as scan_cost,
  (SELECT COALESCE(SUM(api_cost),0) FROM trades) as trade_cost,
  (SELECT COUNT(*) FROM scans WHERE api_cost > 0) as paid_scans,
  (SELECT COUNT(*) FROM trades) as total_trades"
```

Also read storage/data/chat_usage.json for dashboard chat usage by week.

Report: total API spend, cost per trade evaluation, Haiku/Sonnet/Opus split if available.
Note: Opus pricing is $15/$75 per million tokens (corrected 2026-03-01).
"""


def _skill_deploy_check() -> str:
    return """\
# /deploy-check skill
# Auto-generated. Pre-deployment health check.

Run these checks in order:
```bash
systemctl is-active japan225-bot japan225-dashboard japan225-ngrok
tail -20 /home/ubuntu/japan225-bot/logs/monitor.log
cd /home/ubuntu/japan225-bot && python -m pytest tests/ -q --tb=short 2>&1 | tail -5
git status --short
git log --oneline -3
```

Flag: any service not active, any test failure, any uncommitted changes to .py files.
Remind: never commit .env or *.db. Check that MEMORY.md was updated after last code change.
"""


def _skill_prompt_audit() -> str:
    return """\
# /prompt-audit skill
# Auto-generated. Reviews AI prompt performance against recent losses.

Step 1: Read prompt learnings if they exist:
```bash
cat storage/data/prompt_learnings.json 2>/dev/null || echo "No learnings yet"
```

Step 2: Get last 5 losing trades and their AI reasoning:
```bash
sqlite3 storage/data/trading.db "
SELECT trade_number, setup_type, session, confidence, pnl, ai_analysis
FROM trades WHERE pnl < 0 ORDER BY id DESC LIMIT 5"
```

Step 3: Identify patterns:
- What did Sonnet approve that it shouldn't have?
- What context was present in the data but not in the reasoning?
- Does the system prompt need a new rule?

Output: specific suggested addition to build_system_prompt() or build_scan_prompt() in ai/analyzer.py.
"""
