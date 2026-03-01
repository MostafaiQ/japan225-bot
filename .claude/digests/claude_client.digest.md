# dashboard/services/claude_client.py — DIGEST
# Purpose: Dashboard chat backend. Spawns Claude Code CLI as a subprocess.
# Updated 2026-03-01: replaced Anthropic API agentic loop with `claude --print`

## How it works
Calls: `claude --print --dangerously-skip-permissions`
- stdin  = conversation history (Human/Assistant turns) + new message
- stdout = full response after Claude Code completes all internal tool use
- cwd    = PROJECT_ROOT (full project access)
- env    = CLAUDECODE stripped (prevents "nested session" error)

## Tools available (full Claude Code toolset)
Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Agent (spawn subagents)
No custom tool definitions needed — Claude Code handles everything natively.

## chat(message: str, history: list[dict]) -> str
- Formats history as "Human:/Assistant:" turns with separator header
- Keeps last MAX_HISTORY_TURNS=10 pairs (20 messages)
- timeout=180s (3 min)
- Returns stdout; falls back to stderr; falls back to "(no response)"
- Errors (timeout, binary not found) returned as readable strings

## _build_prompt(message, history) -> str
If history exists:
  --- Conversation history (oldest → newest) ---
  Human: ...
  Assistant: ...
  --- New message ---
  Human: <message>
If no history: just returns message directly.

## Constants
CLAUDE_BIN        = "/home/ubuntu/.local/bin/claude"
MAX_HISTORY_TURNS = 10

## Cost tracking
REMOVED. Claude Code CLI does not expose token counts.
/api/chat/costs returns {"note": "...", "today_usd": null, "total_usd": null}
Actual costs visible in Anthropic console.
