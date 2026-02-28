# dashboard/services/claude_client.py — DIGEST
# Purpose: Agentic Claude chat client embedded in the dashboard.
#          Behaves like Claude Code: reads/edits/writes files, runs shell commands, pushes to git.

## Constants
MODEL          = "claude-sonnet-4-6"
MAX_TURNS      = 10   # history window (pairs)
MAX_ITERATIONS = 20   # max tool-use loops per request

## Cost optimisations
- Prompt caching: system prompt has cache_control:ephemeral → 10% on cache hits
- Token-efficient-tools beta header: ~14% savings on tool definition tokens
  Header: "anthropic-beta": "token-efficient-tools-2025-02-19"

## Tools available to Claude
read_file(path)                     → reads up to 14 000 chars, truncates with notice
edit_file(path, old_string, new_string) → exact string replacement (minimal diff); fails if old_string not unique
write_file(path, content)           → full file write; for new files or complete rewrites only
run_command(command)                → shell via subprocess, cwd=PROJECT_ROOT, timeout=25s, last 7000 chars
search_code(pattern, path, flags)   → grep -rn, first 6000 chars

Blocked: .env, .env.example, *.db files (enforced in _safe_path())
Path safety: all paths resolved and verified to stay inside PROJECT_ROOT

## System prompt (_build_system)
Loaded on every call. Contains:
- MEMORY.md content
- Core digests: settings, monitor, database (always)
- Keyword-triggered digests (e.g. "session" → session.digest.md, "risk" → risk_manager.digest.md)
- Behaviour rules: read-before-edit, minimal diffs, git add/commit/push after changes,
  restart bot after Python fixes, cite file:line

## Agentic loop (chat(message, history) → str)
1. Build system prompt with relevant digests
2. Slice last MAX_TURNS*2 history entries (text-only, tool calls not preserved)
3. Loop up to MAX_ITERATIONS:
   - If stop_reason == end_turn: return final text (with action log prepended if any)
   - If stop_reason == tool_use: execute tools, append results, continue
4. Returns "Reached iteration limit…" if loop exhausts

## Response format
If Claude used tools, response is prepended with:
  **Actions taken:**
  - `tool_name(detail)`
  ---
  <final text>
