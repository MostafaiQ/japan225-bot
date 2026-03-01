"""
Dashboard chat backend — powered by Claude Code CLI.

Spawns `claude --print --dangerously-skip-permissions` as a subprocess.
Full tool access: Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch.
Same as talking to Claude Code directly in the terminal.

Conversation history is passed as context at the top of each prompt so
Claude Code has full awareness of what was said before.
"""
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()

# How many history turns (user+assistant pairs) to keep as context
MAX_HISTORY_TURNS = 10

# claude binary — same one running this codebase
CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"


def chat(message: str, history: list[dict]) -> str:
    """
    Send message to Claude Code CLI. Returns the full response text.

    history: list of {"role": "user"|"assistant", "content": str}
    """
    prompt = _build_prompt(message, history)

    env = {**os.environ}
    # Strip CLAUDECODE so the subprocess doesn't think it's nested
    env.pop("CLAUDECODE", None)

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=180,  # 3 min max — complex tasks need time
        )
        response = result.stdout.strip()
        if not response:
            stderr = result.stderr.strip()
            response = stderr if stderr else "(no response)"
        return response

    except subprocess.TimeoutExpired:
        return "Claude Code timed out (3 min limit). Break the task into smaller steps."
    except FileNotFoundError:
        return f"Claude Code binary not found at {CLAUDE_BIN}. Check installation."
    except Exception as e:
        return f"Error spawning Claude Code: {e}"


def _build_prompt(message: str, history: list[dict]) -> str:
    """
    Prepend recent conversation history so Claude Code has context.
    Format: Human/Assistant turns, then the new message at the end.
    """
    recent = [
        h for h in history[-(MAX_HISTORY_TURNS * 2):]
        if isinstance(h.get("content"), str) and h["content"].strip()
    ]

    if not recent:
        return message

    lines = ["--- Conversation history (oldest → newest) ---"]
    for msg in recent:
        role = "Human" if msg.get("role") == "user" else "Assistant"
        lines.append(f"\n{role}: {msg['content'].strip()}")

    lines.append("\n--- New message ---")
    lines.append(f"\nHuman: {message}")

    return "\n".join(lines)
