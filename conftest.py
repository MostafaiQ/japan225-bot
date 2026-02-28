# conftest.py — project-wide pytest configuration
import sys
from unittest.mock import MagicMock


def _stub_module(name):
    """Create a MagicMock that handles attribute and submodule access."""
    mod = MagicMock()
    mod.__name__ = name
    mod.__package__ = name.split(".")[0]
    mod.__spec__ = None
    mod.__all__ = []
    return mod


# ── Stub out unavailable system dependencies ───────────────────────────────
# These packages require IG API, Anthropic, or Telegram credentials and
# cannot be installed in a plain test environment. We stub them so that
# test files importing monitor.py / ig_client.py / telegram_bot.py
# do not raise ImportError.

# --- trading_ig ---
_trading_ig = _stub_module("trading_ig")
_trading_ig.IGService = MagicMock()
sys.modules.setdefault("trading_ig", _trading_ig)
sys.modules.setdefault("trading_ig.rest", _stub_module("trading_ig.rest"))
sys.modules.setdefault("trading_ig.streaming", _stub_module("trading_ig.streaming"))

# --- anthropic ---
_anthropic = _stub_module("anthropic")
_anthropic.Anthropic = MagicMock()
_anthropic.APIError = Exception
sys.modules.setdefault("anthropic", _anthropic)

# --- telegram ---
_telegram = _stub_module("telegram")
_parse_mode = MagicMock()
_parse_mode.MARKDOWN = "Markdown"
_parse_mode.MARKDOWN_V2 = "MarkdownV2"
_parse_mode.HTML = "HTML"
_telegram_constants = _stub_module("telegram.constants")
_telegram_constants.ParseMode = _parse_mode
_telegram.constants = _telegram_constants
_telegram.InlineKeyboardButton = MagicMock()
_telegram.InlineKeyboardMarkup = MagicMock()
_telegram.Update = MagicMock()
_telegram.Bot = MagicMock()
sys.modules.setdefault("telegram", _telegram)
sys.modules.setdefault("telegram.constants", _telegram_constants)

_telegram_ext = _stub_module("telegram.ext")
_telegram_ext.Application = MagicMock()
_telegram_ext.CommandHandler = MagicMock()
_telegram_ext.CallbackQueryHandler = MagicMock()
_telegram_ext.ContextTypes = MagicMock()
sys.modules.setdefault("telegram.ext", _telegram_ext)

# --- yfinance ---
sys.modules.setdefault("yfinance", _stub_module("yfinance"))

# --- httpx (may not be installed in test env) ---
try:
    import httpx  # noqa: F401
except ImportError:
    sys.modules.setdefault("httpx", _stub_module("httpx"))

# --- python-dotenv ---
try:
    import dotenv  # noqa: F401
except ImportError:
    _dotenv = _stub_module("dotenv")
    _dotenv.load_dotenv = lambda *a, **kw: None
    sys.modules.setdefault("dotenv", _dotenv)
