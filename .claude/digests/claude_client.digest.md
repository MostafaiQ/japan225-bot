# dashboard/services/claude_client.py — DIGEST (updated 2026-03-03)
# Dashboard chat backend. 3-tier model selection + rich context injection.

## How it works
Calls: `claude --print --dangerously-skip-permissions --model <tier> --effort <level>`
- stdin  = bot_state snapshot + ops context + compressed history + new message
- stdout = full response after Claude Code completes all internal tool use
- cwd    = PROJECT_ROOT (CLAUDE.md auto-loaded from here)
- env    = CLAUDECODE stripped (prevents "nested session" error)
Tools available: Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Agent

## 3-Tier Model Selection (_pick_tier)
| Tier | Model | Effort | Timeout | Triggers |
|------|-------|--------|---------|----------|
| 1 (fast) | haiku | low | 60s | status, balance, position, pnl, time, cost, simple info (<200 chars) |
| 2 (moderate) | sonnet | high | 180s | everything not matching tier 1 or 3 |
| 3 (deep) | opus | high | 600s | fix, solve, error, bug, push, commit, change/update/write code, deploy, debug, traceback |

_DEEP_KEYWORDS triggers Opus. haiku_patterns triggers Haiku (only if msg < 200 chars).
Default fallback = Sonnet.

## Constants
CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"
MAX_RAW_TURNS = 2        # raw turns always kept
SUMMARY_MAX_CHARS = 600  # ~150 tokens max for rolling summary
BOT_STATE_FILE  = storage/data/bot_state.json
CHAT_USAGE_FILE = storage/data/chat_usage.json
CHAT_COSTS_FILE = storage/data/chat_costs.json
DB_FILE         = storage/data/trading.db
SKILLS_DIR = ~/.claude/skills/

## chat(message, history) -> str
1. _track_usage(message) — classify + log intent
2. _pick_tier(message) → (model, effort, timeout)
3. _build_prompt() — state snapshot + ops context + safety rule + compressed history + message
4. subprocess.run claude --print --model <tier> --effort <level>. timeout per tier.
   start_new_session=True — own process group, dashboard restart won't SIGTERM the subprocess.
5. _log_chat_cost(prompt, response) — estimate cost from char count

## _build_prompt(message, history) -> str
1. _load_bot_state_block() — bot_state.json (~300 tokens): position, balance, last scan, session, next scan
2. _load_ops_context() — pre-computed operational context (~400 tokens):
   - Service status: systemctl is-active japan225-bot/dashboard/ngrok
   - Recent errors: last 5 ERROR/CRITICAL lines from journalctl (30 min window)
   - Recent scan activity: last 8 meaningful log lines (1 hour window)
   - Recent trades: last 3 from SQLite DB
3. Compressed history (~350 tokens max)
4. Safety rule: "NEVER restart japan225-dashboard — you are running inside it"
5. New message

## _load_ops_context() -> str
Runs 4 subprocess calls with 3s timeout each (~100ms total typical):
- systemctl is-active (3 services)
- journalctl errors (30 min)
- journalctl scan activity (1 hour)
- sqlite3 recent trades (last 3)
Eliminates 70-90% of tool calls the AI would otherwise make for status/operational queries.

## _log_chat_cost(prompt, response) -> None
Blended estimate: $9/M input, $45/M output, 1 token ≈ 4 chars.
Appends {ts, cost_usd, input_chars, output_chars, est_tokens} to chat_costs.json (max 500 entries).

## compress_history(history) -> list[dict]
Call from chat router after each response, before saving to chat_history.json.
Keeps last MAX_RAW_TURNS pairs raw. Absorbs older into rolling text summary.
History entry {"role": "summary"|"user"|"assistant", "content": str}
Total history payload: capped ~650 tokens forever.

## _track_usage(message) -> None
Intent classification via QUERY_PATTERNS keywords → chat_usage.json (by ISO week).
Calls _maybe_draft_skill(intent, count) at 5/10/20 uses this week.

## _maybe_draft_skill(intent, count) -> None
Creates ~/.claude/skills/<intent>.md from template if not exists.
Auto-drafted: trade_review | strategy_health | cost_report | deploy_check | prompt_audit

## QUERY_PATTERNS
trade_review | strategy_health | cost_report | deploy_check | prompt_audit | status | other

## Frontend tier badge
POST /api/chat returns {job_id, status, tier}. GET /api/chat/status returns {status, response, tier}.
Frontend shows colored badge in thinking bubble: green=Haiku, purple=Sonnet, amber=Opus.
