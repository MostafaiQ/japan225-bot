"""
Tests for IGClient streaming price state machine.

No live IG connection — uses __new__ to bypass __init__ + direct attribute setup.
Covers all 5 streaming edge cases documented in MEMORY.md.
"""
import time
import sys
import pytest
from unittest.mock import MagicMock, patch

# Stub out trading_ig and its sub-packages before any import
for _mod in ("trading_ig", "trading_ig.rest", "trading_ig.config",
             "trading_ig.streaming", "lightstreamer", "lightstreamer.client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_ig():
    """Create an IGClient with no real __init__ — no IG connection, no disk I/O."""
    from core.ig_client import IGClient
    client = IGClient.__new__(IGClient)
    # Replicate exactly what __init__ sets
    client.ig = None
    client.authenticated = False
    client.last_auth_time = None
    client._request_count = 0
    client._last_request_time = 0
    client._candle_cache = {}
    client._cache_full_fetch_done = {}
    client._lightstreamer_endpoint = None
    client._ls_client = None
    client._streaming_price = None
    client._streaming_price_ts = 0.0
    client._tick_candles = []
    return client


# ── get_streaming_price ───────────────────────────────────────────────────────

class TestGetStreamingPrice:
    """test_get_streaming_price_* from MEMORY.md streaming test notes."""

    def test_fresh_price_returned(self):
        """Fresh price (just received) is returned directly."""
        client = _make_ig()
        client._streaming_price = 54000.0
        client._streaming_price_ts = time.monotonic()  # right now
        result = client.get_streaming_price()
        assert result == 54000.0

    def test_stale_price_returns_none(self):
        """Price older than STREAMING_STALE_SECONDS (10s) triggers REST fallback → None."""
        from config.settings import STREAMING_STALE_SECONDS
        client = _make_ig()
        client._streaming_price = 54000.0
        client._streaming_price_ts = time.monotonic() - (STREAMING_STALE_SECONDS + 10)
        result = client.get_streaming_price()
        assert result is None

    def test_none_price_returns_none(self):
        """_streaming_price=None always returns None even with a fresh timestamp."""
        client = _make_ig()
        client._streaming_price = None
        client._streaming_price_ts = time.monotonic()
        result = client.get_streaming_price()
        assert result is None


# ── stop_streaming ────────────────────────────────────────────────────────────

class TestStopStreaming:
    """test_stop_streaming_* from MEMORY.md streaming test notes."""

    def test_noop_when_no_client(self):
        """stop_streaming() is safe to call when _ls_client is None — no exception."""
        client = _make_ig()
        client._ls_client = None
        # Must not raise
        client.stop_streaming()
        assert client._streaming_price is None
        assert client._streaming_price_ts == 0.0

    def test_disconnects_and_clears_state(self):
        """stop_streaming() calls disconnect() on the LS client and clears all state."""
        client = _make_ig()
        mock_ls = MagicMock()
        client._ls_client = mock_ls
        client._streaming_price = 54000.0
        client._streaming_price_ts = time.monotonic()

        client.stop_streaming()

        mock_ls.disconnect.assert_called_once()
        assert client._ls_client is None
        assert client._streaming_price is None
        assert client._streaming_price_ts == 0.0


# ── start_streaming ───────────────────────────────────────────────────────────

class TestStartStreaming:
    """test_start_streaming_* from MEMORY.md streaming test notes."""

    def test_no_endpoint_returns_false(self):
        """No lightstreamer endpoint saved → start_streaming() returns False immediately."""
        client = _make_ig()
        client._lightstreamer_endpoint = None
        result = client.start_streaming()
        assert result is False

    def test_no_session_tokens_returns_false(self):
        """Missing CST/X-SECURITY-TOKEN → start_streaming() returns False (REST polling used)."""
        client = _make_ig()
        client._lightstreamer_endpoint = "https://push.ls.example.com"
        mock_ig = MagicMock()
        mock_ig.session.headers = {}   # no CST, no X-SECURITY-TOKEN
        client.ig = mock_ig
        result = client.start_streaming()
        assert result is False
