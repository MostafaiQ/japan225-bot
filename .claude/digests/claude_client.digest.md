# dashboard/services/claude_client.py — DIGEST (updated 2026-03-01)
# Dashboard chat backend. Spawns claude --print subprocess.
# Deadlock fix: rolling history summary + bot_state injection + CLAUDE.md auto-load.

## How it works
Calls: `claude --print --dangerously-skip-permissions`
- stdin  = bot_state snapshot + compressed history + new message
- stdout = full response after Claude Code completes all internal tool use
- cwd    = PROJECT_ROOT (CLAUDE.md auto-loaded from here)
- env    = CLAUDECODE stripped (prevents "nested session" error)
Tools available: Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Agent

## Constants
CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"
MAX_RAW_TURNS = 2        # raw turns always kept
SUMMARY_MAX_CHARS = 600  # ~150 tokens max for rolling summary
BOT_STATE_FILE  = storage/data/bot_state.json
CHAT_USAGE_FILE = storage/data/chat_usage.json
CHAT_COSTS_FILE = storage/data/chat_costs.json
SKILLS_DIR = ~/.claude/skills/

## chat(message, history) -> str
1. _track_usage(message) — classify + log intent
2. _build_prompt() — state snapshot + compressed history + message
3. subprocess.run claude --print. timeout=180s.
4. _log_chat_cost(prompt, response) — estimate cost from char count, append to chat_costs.json

## _log_chat_cost(prompt, response) -> None
Estimates cost: Sonnet $3/M input, $15/M output, 1 token ≈ 4 chars.
Appends {ts, cost_usd, input_chars, output_chars} to chat_costs.json (max 500 entries).
GET /api/chat/costs now returns real today/total estimates.

## compress_history(history) -> list[dict]
Call from chat router after each response, before saving to chat_history.json.
Keeps last MAX_RAW_TURNS pairs raw. Absorbs older into rolling text summary.
History entry {"role": "summary"|"user"|"assistant", "content": str}
Total history payload: capped ~650 tokens forever.

## _build_prompt(message, history) -> str
1. bot_state.json snapshot (~300 tokens) — eliminates 70% of file reads for status questions
2. Compressed history (~350 tokens max)
3. New message

## _track_usage(message) -> None
Intent classification via QUERY_PATTERNS keywords → chat_usage.json (by ISO week).
Calls _maybe_draft_skill(intent, count) at 5/10/20 uses this week.

## _maybe_draft_skill(intent, count) -> None
Creates ~/.claude/skills/<intent>.md from template if not exists.
Auto-drafted: trade_review | strategy_health | cost_report | deploy_check | prompt_audit

## QUERY_PATTERNS
trade_review | strategy_health | cost_report | deploy_check | prompt_audit | status | other
