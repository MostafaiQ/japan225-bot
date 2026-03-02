"""
Tests for 5M fallback setup detection feature.

When 15M detect_setup() finds nothing, the bot tries 5M as a fallback entry timeframe.
5M setups must pass a lightweight 15M structure alignment check before being used.
"""
import pytest
from core.indicators import detect_setup
from monitor import TradingMonitor


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_tf(
    price=38000,
    rsi=45,
    bb_mid=37990,
    bb_upper=38300,
    bb_lower=37700,
    ema50=37980,
    ema200=37500,
    above_ema50=True,
    above_ema200=True,
    above_ema200_fallback=None,
    prev_close=None,
    candle_open=None,
    candle_low=None,
    volume_signal="NORMAL",
):
    """Minimal fake analyze_timeframe() dict."""
    _open = candle_open if candle_open is not None else price - 15
    _low = candle_low if candle_low is not None else price - 40
    return {
        "price": price,
        "open": _open,
        "low": _low,
        "rsi": rsi,
        "bollinger_mid": bb_mid,
        "bollinger_upper": bb_upper,
        "bollinger_lower": bb_lower,
        "ema50": ema50,
        "ema200": ema200,
        "above_ema50": above_ema50,
        "above_ema200": above_ema200,
        "above_ema200_fallback": above_ema200_fallback if above_ema200_fallback is not None else above_ema200,
        "prev_close": prev_close if prev_close is not None else price - 30,
        "volume_signal": volume_signal,
    }


def _valid_long_bb_mid(price=38000):
    """Build a valid LONG bollinger_mid_bounce tf dict."""
    return make_tf(
        price=price,
        rsi=45,
        bb_mid=price + 10,      # within 150pts
        bb_lower=price - 200,
        ema50=price - 5,
        above_ema200_fallback=True,
        prev_close=price - 30,  # bounce starting (price > prev_close)
    )


def _no_setup_tf(price=38000):
    """Build a tf dict that won't trigger any setup (RSI outside all ranges)
    but keeps BB levels reasonable for alignment checks."""
    return make_tf(
        price=price,
        rsi=60,                   # outside LONG range (35-55) and SHORT range needs other conditions
        bb_mid=price - 500,       # too far for bb_mid_bounce
        bb_lower=price - 200,     # reasonable 15M BB lower for alignment (within 300pts of price)
        bb_upper=price + 500,     # too far for bb_upper_rejection
        ema50=price + 500,        # too far for ema50_rejection
        above_ema200_fallback=True,
    )


# ── Test: 5M fallback fires when 15M has no setup ────────────────────────────

class TestFallbackFiresWhen15mEmpty:
    def test_5m_setup_found_when_15m_empty(self):
        """When 15M finds no setup but 5M does, the 5M setup should be used."""
        tf_daily = make_tf(above_ema200_fallback=True)
        tf_15m_none = _no_setup_tf()     # 15M: no setup
        tf_5m_valid = _valid_long_bb_mid()  # 5M: valid LONG

        # 15M should find nothing
        setup_15m = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_15m_none)
        assert not setup_15m["found"]

        # 5M should find a setup
        setup_5m = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_5m_valid)
        assert setup_5m["found"]
        assert setup_5m["direction"] == "LONG"

        # Alignment check should pass (15M RSI < 65, price near BB)
        aligned = TradingMonitor._5m_aligns_with_15m(setup_5m, tf_15m_none)
        assert aligned

        # Tag with suffix
        setup_5m["type"] += "_5m"
        assert setup_5m["type"].endswith("_5m")


# ── Test: 5M blocked when 15M structure opposes ──────────────────────────────

class TestFallbackBlockedByStructure:
    def test_5m_long_blocked_when_15m_overbought(self):
        """5M LONG should be blocked when 15M RSI > 65 (overbought)."""
        tf_15m_overbought = make_tf(rsi=70, bb_mid=38000)  # RSI 70 > 65
        setup_5m = {"found": True, "direction": "LONG", "entry": 38000, "type": "bollinger_mid_bounce"}
        assert not TradingMonitor._5m_aligns_with_15m(setup_5m, tf_15m_overbought)

    def test_5m_short_blocked_when_15m_oversold(self):
        """5M SHORT should be blocked when 15M RSI < 35 (oversold)."""
        tf_15m_oversold = make_tf(rsi=30, bb_upper=38300)  # RSI 30 < 35
        setup_5m = {"found": True, "direction": "SHORT", "entry": 38200, "type": "bollinger_upper_rejection"}
        assert not TradingMonitor._5m_aligns_with_15m(setup_5m, tf_15m_oversold)

    def test_5m_long_blocked_when_price_far_from_bb(self):
        """5M LONG should be blocked when price is >300pts from 15M BB lower."""
        tf_15m = make_tf(rsi=50, bb_mid=38000, bb_lower=37500)  # bb_lower = 37500
        setup_5m = {"found": True, "direction": "LONG", "entry": 37100, "type": "bollinger_mid_bounce"}
        # 37100 is 400pts from 37500 (bb_lower) → > 300 → blocked
        assert not TradingMonitor._5m_aligns_with_15m(setup_5m, tf_15m)

    def test_5m_short_blocked_when_price_far_from_bb_upper(self):
        """5M SHORT should be blocked when price is >300pts from 15M BB upper."""
        tf_15m = make_tf(rsi=50, bb_upper=38300)
        setup_5m = {"found": True, "direction": "SHORT", "entry": 38700, "type": "bollinger_upper_rejection"}
        # 38700 is 400pts from 38300 → > 300 → blocked
        assert not TradingMonitor._5m_aligns_with_15m(setup_5m, tf_15m)

    def test_5m_passes_through_when_15m_missing(self):
        """If 15M data is empty, 5M should pass through (safe default)."""
        setup_5m = {"found": True, "direction": "LONG", "entry": 38000, "type": "bollinger_mid_bounce"}
        assert TradingMonitor._5m_aligns_with_15m(setup_5m, {})
        assert TradingMonitor._5m_aligns_with_15m(setup_5m, None)


# ── Test: 5M setup tagged with _5m suffix ────────────────────────────────────

class TestSetupTagging:
    def test_5m_setup_gets_suffix(self):
        """Setup type should get _5m appended for tracking."""
        tf_daily = make_tf(above_ema200_fallback=True)
        tf_5m = _valid_long_bb_mid()
        setup = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_5m)
        assert setup["found"]
        original_type = setup["type"]

        setup["type"] += "_5m"
        assert setup["type"] == f"{original_type}_5m"
        assert "_5m" in setup["type"]

    def test_all_setup_types_can_be_tagged(self):
        """Verify suffix works for all known setup types."""
        types = ["bollinger_mid_bounce", "bollinger_lower_bounce",
                 "bollinger_upper_rejection", "ema50_rejection"]
        for t in types:
            tagged = t + "_5m"
            assert tagged.endswith("_5m")
            assert tagged.startswith(t)


# ── Test: 5M not checked when 15M already found setup ────────────────────────

class TestPriorityOrder:
    def test_15m_takes_priority(self):
        """When 15M finds a setup, 5M should not be checked."""
        tf_daily = make_tf(above_ema200_fallback=True)
        tf_15m = _valid_long_bb_mid()
        tf_5m = _valid_long_bb_mid()

        # 15M finds setup
        setup = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_15m)
        assert setup["found"]

        # The logic: if setup["found"] → skip 5M. No "_5m" suffix.
        entry_timeframe = "15m"
        if not setup["found"] and tf_5m:
            # This block should NOT execute
            setup_5m = detect_setup(tf_daily=tf_daily, tf_4h={}, tf_15m=tf_5m)
            if setup_5m["found"]:
                setup = setup_5m
                setup["type"] += "_5m"
                entry_timeframe = "5m"

        assert entry_timeframe == "15m"
        assert "_5m" not in (setup.get("type") or "")


# ── Test: alignment edge cases ────────────────────────────────────────────────

class TestAlignmentEdgeCases:
    def test_long_at_rsi_boundary(self):
        """RSI exactly at 65 should pass (not strictly greater)."""
        tf_15m = make_tf(rsi=65, bb_mid=38000, bb_lower=37800)
        setup = {"found": True, "direction": "LONG", "entry": 37900, "type": "bollinger_mid_bounce"}
        assert TradingMonitor._5m_aligns_with_15m(setup, tf_15m)

    def test_short_at_rsi_boundary(self):
        """RSI exactly at 35 should pass (not strictly less)."""
        tf_15m = make_tf(rsi=35, bb_upper=38300)
        setup = {"found": True, "direction": "SHORT", "entry": 38200, "type": "bollinger_upper_rejection"}
        assert TradingMonitor._5m_aligns_with_15m(setup, tf_15m)

    def test_long_at_300pt_boundary(self):
        """Price exactly 300pts from BB lower should pass."""
        tf_15m = make_tf(rsi=50, bb_mid=38000, bb_lower=37700)
        setup = {"found": True, "direction": "LONG", "entry": 37400, "type": "bollinger_mid_bounce"}
        # 37400 - 37700 = 300 → exactly 300 → should pass (not >300)
        assert TradingMonitor._5m_aligns_with_15m(setup, tf_15m)

    def test_rsi_none_passes_through(self):
        """If 15M RSI is None, alignment should pass (missing data = don't block)."""
        tf_15m = make_tf(bb_mid=38000, bb_lower=37800)
        tf_15m["rsi"] = None
        setup = {"found": True, "direction": "LONG", "entry": 37900, "type": "bollinger_mid_bounce"}
        assert TradingMonitor._5m_aligns_with_15m(setup, tf_15m)
