"""
Claude chat client for the dashboard.
Injects MEMORY.md + core digests as system context.
Dynamically adds extra digests based on keywords in the user message.
History capped at 10 turns (enforced by frontend, double-checked here).
"""
import os
import re
from pathlib import Path
import anthropic

PROJECT_ROOT = Path(__file__).parent.parent.parent
MEMORY_PATH  = PROJECT_ROOT / "MEMORY.md"
DIGESTS_DIR  = PROJECT_ROOT / ".claude" / "digests"

# Always-injected digests
CORE_DIGESTS = ["settings", "monitor", "database"]

# Keyword → digest mapping for dynamic injection
KEYWORD_MAP = {
    "indicator":  "indicators",
    "setup":      "indicators",
    "detect":     "indicators",
    "session":    "session",
    "momentum":   "momentum",
    "confidence": "confidence",
    "analyzer":   "analyzer",
    "sonnet":     "analyzer",
    "opus":       "analyzer",
    "risk":       "risk_manager",
    "lot":        "risk_manager",
    "exit":       "exit_manager",
    "breakeven":  "exit_manager",
    "trailing":   "exit_manager",
    "telegram":   "telegram_bot",
    "ig ":        "ig_client",
    "position":   "ig_client",
}

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10


def _load(path: Path) -> str:
    try:
        return path.read_text()
    except Exception:
        return ""


def _build_system(user_msg: str) -> str:
    memory = _load(MEMORY_PATH)

    # Core digests
    digest_names = set(CORE_DIGESTS)
    # Dynamic digests from keywords
    msg_lower = user_msg.lower()
    for kw, name in KEYWORD_MAP.items():
        if kw in msg_lower:
            digest_names.add(name)

    digests = []
    for name in sorted(digest_names):
        path = DIGESTS_DIR / f"{name}.digest.md"
        content = _load(path)
        if content:
            digests.append(f"### {name}.digest.md\n{content}")

    parts = [
        "You are Claude, assistant for the Japan 225 trading bot dashboard.",
        "You have full context of the bot's architecture and codebase.",
        "",
        "## MEMORY.md",
        memory,
        "",
        "## Module Digests",
        "\n\n".join(digests),
        "",
        "Answer concisely. When referencing code, cite file:line if known.",
        "If asked to write code fixes, output a unified diff.",
    ]
    return "\n".join(parts)


def chat(message: str, history: list[dict]) -> str:
    """
    Send a message to Claude with full bot context.
    history: list of {role: user|assistant, content: str}
    Returns the assistant reply string.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    system = _build_system(message)

    # Enforce 10-turn cap
    msgs = history[-(MAX_TURNS * 2):]
    msgs.append({"role": "user", "content": message})

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=msgs,
    )
    return response.content[0].text
