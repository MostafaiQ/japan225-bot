"""
Claude agentic chat client for the dashboard.

Cost optimizations:
  - Prompt caching on system prompt (pays 10% on cache hits)
  - token-efficient-tools beta header (~14% savings on tool tokens)
  - claude-sonnet-4-6 (same price as 4.5, better quality)

Tools: read_file, edit_file, write_file, run_command, search_code
  - edit_file = minimal diff (prefer over write_file for bug fixes)
  - run_command = full shell access, includes git for committing fixes
  - After any file change: git add <file> → commit → push

History: text-only in/out. Tool calls are internal to each request.
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import anthropic

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
MEMORY_PATH  = PROJECT_ROOT / "MEMORY.md"
DIGESTS_DIR  = PROJECT_ROOT / ".claude" / "digests"

MODEL          = "claude-sonnet-4-6"   # same price as 4.5, better quality
MAX_TURNS      = 10
MAX_ITERATIONS = 20

CORE_DIGESTS   = ["settings", "monitor", "database"]
_COSTS_PATH    = PROJECT_ROOT / "storage" / "data" / "chat_costs.json"

# Pricing: claude-sonnet-4-6  (USD / 1M tokens)
_PRICE = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}

# Module-level client singleton (reuses HTTP connection pool across requests)
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            default_headers={"anthropic-beta": "token-efficient-tools-2025-02-19"},
        )
    return _client


# System-prompt cache: avoid re-reading disk on every request when files unchanged
_system_cache: dict = {"key": None, "result": None}


def _calc_cost(usage) -> float:
    return (
        getattr(usage, "input_tokens",                0) * _PRICE["input"]       / 1_000_000 +
        getattr(usage, "output_tokens",               0) * _PRICE["output"]      / 1_000_000 +
        getattr(usage, "cache_creation_input_tokens", 0) * _PRICE["cache_write"] / 1_000_000 +
        getattr(usage, "cache_read_input_tokens",     0) * _PRICE["cache_read"]  / 1_000_000
    )


def _log_cost(usage, iteration: int):
    """Append one cost entry to chat_costs.json (max 500 kept). Atomic write."""
    try:
        _COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        entries = json.loads(_COSTS_PATH.read_text()) if _COSTS_PATH.exists() else []
        entries.append({
            "ts":          datetime.now(timezone.utc).isoformat(),
            "model":       MODEL,
            "input":       getattr(usage, "input_tokens",                0),
            "output":      getattr(usage, "output_tokens",               0),
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
            "cache_read":  getattr(usage, "cache_read_input_tokens",     0),
            "cost_usd":    round(_calc_cost(usage), 6),
            "iteration":   iteration,
        })
        tmp = _COSTS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries[-500:]))
        tmp.replace(_COSTS_PATH)  # atomic on Linux
    except Exception:
        pass  # never let logging break the chat

KEYWORD_MAP = {
    "indicator":  "indicators",
    "setup":      "indicators",
    "detect":     "indicators",
    "session":    "session",
    "momentum":   "momentum",
    "confidence": "confidence",
    "analyzer":   "analyzer",
    "risk":       "risk_manager",
    "lot":        "risk_manager",
    "exit":       "exit_manager",
    "breakeven":  "exit_manager",
    "trailing":   "exit_manager",
    "telegram":   "telegram_bot",
    "ig ":        "ig_client",
    "position":   "ig_client",
    # Dashboard modules
    "dashboard":  "dashboard",
    "router":     "dashboard",
    "frontend":   "dashboard",
    "ngrok":      "dashboard",
    "config":     "dashboard",
    "override":   "dashboard",
    "claude_client": "claude_client",
    "agentic":    "claude_client",
    "chat":       "claude_client",
}

BLOCKED_FILES = {".env", ".env.example"}

# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the project. Always read a file before editing it. "
            "Can read source code, logs, config, digests, MEMORY.md, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project root. E.g. 'monitor.py', 'core/indicators.py'"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "edit_file",
        "description": (
            "Edit a file by replacing an exact string with a new string. "
            "ALWAYS prefer this over write_file for bug fixes — minimal diffs only. "
            "old_string must be unique in the file. Read the file first to get exact text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string", "description": "Path relative to project root"},
                "old_string": {"type": "string", "description": "Exact text to find (must be unique in file)"},
                "new_string": {"type": "string", "description": "Replacement text"}
            },
            "required": ["path", "old_string", "new_string"]
        }
    },
    {
        "name": "write_file",
        "description": (
            "Write a complete file. Use ONLY for new files or total rewrites. "
            "For bug fixes, use edit_file instead. Blocked: .env, *.db"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Path relative to project root"},
                "content": {"type": "string", "description": "Full file content"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the project directory. "
            "Use for: checking logs (journalctl), service status (systemctl), "
            "git operations, running tests, grepping with complex patterns. "
            "After editing/writing files always run: "
            "git add <file> && git commit -m 'fix: ...' && git push origin main"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command. E.g. 'journalctl -u japan225-bot -n 50 --no-pager'"
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "search_code",
        "description": "Search for a pattern in project files using grep -rn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
                "path":    {"type": "string", "description": "File or directory (relative). Default: entire project", "default": "."},
                "flags":   {"type": "string", "description": "Extra grep flags e.g. '-i' for case-insensitive", "default": ""}
            },
            "required": ["pattern"]
        }
    }
]

# ── Tool executor ─────────────────────────────────────────────────────────────

def _run(cmd, **kw):
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, **kw)


def _safe_path(rel: str):
    """Resolve path and verify it stays inside project root."""
    p = (PROJECT_ROOT / rel.lstrip("/")).resolve()
    if not str(p).startswith(str(PROJECT_ROOT)):
        raise ValueError("Path outside project root")
    if p.name in BLOCKED_FILES or p.suffix == ".db":
        raise ValueError(f"{p.name} is blocked")
    return p


def _execute_tool(name: str, inp: dict) -> str:
    try:
        if name == "read_file":
            p = _safe_path(inp["path"])
            if not p.exists():
                return f"File not found: {inp['path']}"
            txt = p.read_text(errors="replace")
            if len(txt) > 14000:
                txt = txt[:14000] + f"\n\n[truncated — {len(txt)} chars total]"
            return txt

        elif name == "edit_file":
            p = _safe_path(inp["path"])
            if not p.exists():
                return f"File not found: {inp['path']}"
            content   = p.read_text(errors="replace")
            old, new  = inp["old_string"], inp["new_string"]
            count     = content.count(old)
            if count == 0:
                return "old_string not found — read the file first to get exact text"
            if count > 1:
                return f"old_string appears {count} times — make it more specific"
            p.write_text(content.replace(old, new, 1))
            return f"Edited {inp['path']} — replaced {len(old)} chars with {len(new)} chars"

        elif name == "write_file":
            p = _safe_path(inp["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(inp["content"])
            return f"Written {inp['path']} ({len(inp['content'])} chars)"

        elif name == "run_command":
            r = _run(inp["command"], shell=True, timeout=25)
            out = ((r.stdout or "") + (r.stderr or "")).strip() or "(no output)"
            return out[-7000:] if len(out) > 7000 else out  # keep tail

        elif name == "search_code":
            flags = (inp.get("flags") or "").split()
            path  = (inp.get("path") or ".").lstrip("/") or "."
            r = _run(["grep", "-rn"] + flags + [inp["pattern"], path], timeout=10)
            out = r.stdout.strip() or "(no matches)"
            return out[:6000] + "\n[truncated]" if len(out) > 6000 else out

        return f"Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return "Command timed out (25s)"
    except Exception as e:
        return f"Error: {e}"


# ── System prompt (cached) ────────────────────────────────────────────────────

def _load(path: Path) -> str:
    try:
        return path.read_text()
    except Exception:
        return ""


def _build_system(user_msg: str) -> list[dict]:
    """Returns system as a list with cache_control for prompt caching.
    Result is cached by (digest set, file mtimes) to avoid disk re-reads."""
    global _system_cache

    digest_names = set(CORE_DIGESTS)
    for kw, name in KEYWORD_MAP.items():
        if kw in user_msg.lower():
            digest_names.add(name)

    paths = [MEMORY_PATH] + [DIGESTS_DIR / f"{n}.digest.md" for n in sorted(digest_names)]
    mtime_sum = sum(p.stat().st_mtime for p in paths if p.exists())
    cache_key = (frozenset(digest_names), mtime_sum)

    if _system_cache["key"] == cache_key:
        return _system_cache["result"]

    memory = _load(MEMORY_PATH)

    digests = []
    for name in sorted(digest_names):
        c = _load(DIGESTS_DIR / f"{name}.digest.md")
        if c:
            digests.append(f"### {name}.digest.md\n{c}")

    text = "\n".join([
        "You are Claude, embedded in the Japan 225 trading bot dashboard as a fully agentic assistant.",
        "You have the SAME capabilities as Claude Code: you can read files, edit files, write files,",
        "run shell commands, search code, and push changes to GitHub.",
        "",
        "## Behaviour rules (non-negotiable)",
        "1. **Read before editing**: always call read_file before edit_file or write_file.",
        "2. **Minimal diffs**: use edit_file (exact string replacement) for bug fixes.",
        "   Only use write_file for brand-new files or complete rewrites.",
        "3. **After any file change**: run `git add <specific-file> && git commit -m 'fix: ...' && git push origin main`.",
        "   Stage specific files only — never `git add -A` or `git add .`.",
        "   Never commit .env or *.db files.",
        "4. **Always act, never instruct**: the user is REMOTE and cannot run commands themselves.",
        "   For ANY question about bot state, errors, or 'why is X not working' — run the commands",
        "   yourself (journalctl, systemctl status, cat logs, etc.) BEFORE writing a single word of answer.",
        "   NEVER write 'you should run...', 'try running...', 'check if...', or 'restart with...'.",
        "   If you catch yourself about to give the user instructions — stop and do it yourself instead.",
        "5. **Actually fix it**: when asked to fix a bug, investigate it, make the code change, commit, push,",
        "   and restart the service. Don't describe the fix — implement it.",
        "6. **Restart after fixes**: if you changed Python code, run `sudo systemctl restart japan225-bot`.",
        "7. **Be concise**: summarise what you found and what you changed. No need to repeat full file contents.",
        "8. **Cite locations**: reference file:line when discussing code.",
        "9. **Approval before big actions** (same rule as Claude Code — not less, not more):",
        "   JUST DO IT (no need to ask) for: reading files, checking logs, small bug fixes (1-5 lines),",
        "   adding a log line, restarting a service after a fix, checking service status.",
        "   ASK FIRST before: large rewrites (>20 lines changed), architectural changes, deleting files,",
        "   force-pushing git, changing credentials/secrets, closing/killing live trades,",
        "   anything that affects real money or is hard to reverse.",
        "   Use judgment — if an action is small, safe, and reversible: just do it.",
        "   If it is large, risky, irreversible, or touches live trading: confirm with the user first.",
        "",
        "## Project memory",
        memory,
        "",
        "## Module digests",
        "\n\n".join(digests),
    ])

    # Mark with cache_control so Anthropic caches this on repeated calls
    result = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    _system_cache.update({"key": cache_key, "result": result})
    return result


# ── Main chat function ────────────────────────────────────────────────────────

def chat(message: str, history: list[dict]) -> str:
    """
    Agentic loop with tool use. Returns final text response.
    Uses prompt caching + token-efficient-tools beta for cost savings.
    """
    client = _get_client()
    system = _build_system(message)

    msgs = [
        {"role": h["role"], "content": h["content"]}
        for h in history[-(MAX_TURNS * 2):]
        if isinstance(h.get("content"), str)
    ]
    msgs.append({"role": "user", "content": message})

    actions = []

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=msgs,
        )

        _log_cost(response.usage, _)

        if response.stop_reason == "end_turn":
            text = " ".join(
                b.text for b in response.content if hasattr(b, "text")
            ).strip() or "(no response)"

            if actions:
                log = "\n".join(f"- `{a}`" for a in actions)
                text = f"**Actions taken:**\n{log}\n\n---\n\n{text}"

            return text

        if response.stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": response.content})

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = _execute_tool(block.name, block.input)
                detail = (
                    block.input.get("path") or
                    block.input.get("command", "")[:55] or
                    block.input.get("pattern", "")
                )
                actions.append(f"{block.name}({detail})")
                results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

            msgs.append({"role": "user", "content": results})

        else:
            break

    return "Reached iteration limit — the task may need to be broken into smaller steps."
