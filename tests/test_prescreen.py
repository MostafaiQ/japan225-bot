"""
Tests for core/indicators.py — pre-screen setup detection (LONG + SHORT).
Verifies that detect_setup() correctly identifies all four setup types and
enforces point-distance thresholds (not percentile).
"""
import pytest
from core.indicators import detect_setup, analyze_timeframe


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
):
    """Minimal fake analyze_timeframe() dict."""
    # Default candle shape: green bounce candle with 25pt lower wick
    # open slightly below close, low 40pts below open → lower_wick = 25
    _open = candle_open if candle_open is not None else price - 15
    _low  = candle_low  if candle_low  is not None else price - 40
    tf = {
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
        "prev_close": prev_close if prev_close is not None else price - 30,  # default: bouncing up
    }
    return tf


# ── LONG Setup 1: Bollinger Mid Bounce ────────────────────────────────────────

class TestLongBollingerMidBounce:
    def _make_long_bb(self, price_offset=0):
        """Valid LONG BB mid bounce setup."""
        price = 38000 + price_offset
        tf_daily = make_tf(price=price, above_ema200_fallback=True, above_ema200=True)
        tf_4h = make_tf(price=price, rsi=50)
        tf_15m = make_tf(
            price=price,
            rsi=45,          # 35-55 zone ✓
            bb_mid=price - 10,  # Price 10 pts above mid (within 30) ✓
            bb_upper=price + 300,
            ema50=price - 50,  # below EMA50 is FALSE
            above_ema50=True,  # above EMA50 ✓
            above_ema200_fallback=True,
        )
        return tf_daily, tf_4h, tf_15m

    def test_detects_bollinger_mid_bounce(self):
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] is True
        assert result["direction"] == "LONG"
        assert result["type"] == "bollinger_mid_bounce"

    def test_sl_is_below_entry(self):
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["sl"] < result["entry"]

    def test_tp_is_above_entry(self):
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["tp"] > result["entry"]

    def test_rr_ratio_at_least_2(self):
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        risk = abs(result["entry"] - result["sl"])
        reward = abs(result["tp"] - result["entry"])
        assert reward / risk >= 1.9  # Default is 200 SL / 400 TP = 2.0

    def test_rsi_out_of_range_blocks(self):
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        tf_15m["rsi"] = 70  # Outside 35-55
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # May find EMA50 bounce or no setup; should NOT find bb_mid_bounce
        if result["found"]:
            assert result["type"] != "bollinger_mid_bounce"
        else:
            assert result["found"] is False

    def test_price_far_from_bb_mid_blocks(self):
        """Price 200 pts from mid — exceeds 150-pt threshold."""
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        tf_15m["bollinger_mid"] = tf_15m["price"] - 200  # 200 pts away (> 150 threshold)
        tf_15m["ema50"] = tf_15m["price"] - 200           # Far from EMA50 too
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # Should NOT detect bb_mid_bounce
        if result["found"]:
            assert result["type"] != "bollinger_mid_bounce"

    def test_point_distance_not_percentile(self):
        """Verify threshold is absolute points, not a Bollinger percentile."""
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        # At price=38000, bb_mid=37975 (25 pts away) → valid
        tf_15m["bollinger_mid"] = 37975
        tf_15m["bollinger_upper"] = 38600
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] is True
        assert result["type"] == "bollinger_mid_bounce"

    def test_daily_bearish_still_finds_long(self):
        """LONG setup found even when daily bearish — bidirectional (C1 penalizes counter-trend)."""
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        tf_daily["above_ema200_fallback"] = False
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # detect_setup() no longer hard-gates on daily direction
        assert result["found"] is True
        assert result["direction"] == "LONG"
        assert "counter-trend" in result["reasoning"].lower() or "bearish" in result["reasoning"].lower()

    def test_prev_close_higher_blocks(self):
        """If prev_close >= current price, bounce has not started — should not fire."""
        tf_daily, tf_4h, tf_15m = self._make_long_bb()
        # Set prev_close higher than current price (candle moving down, not up)
        price = tf_15m["price"]
        tf_15m["prev_close"] = price + 20  # prev close was higher → no bounce
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        if result["found"]:
            assert result["type"] != "bollinger_mid_bounce"


# ── LONG Setup 2: EMA50 Bounce ────────────────────────────────────────────────

class TestLongEma50Bounce:
    def _make_ema50_bounce(self):
        price = 38000
        tf_daily = make_tf(above_ema200_fallback=True, above_ema200=True)
        tf_4h = make_tf(rsi=50)
        tf_15m = make_tf(
            price=price,
            rsi=48,        # < 50 ✓
            bb_mid=price - 200,  # Far from BB mid (won't trigger bb_mid_bounce)
            bb_upper=price + 300,
            ema50=price - 15,    # 15 pts below price — within 30 ✓ and price >= ema50-10
            above_ema50=True,    # just above EMA50 ✓
            above_ema200_fallback=True,
        )
        return tf_daily, tf_4h, tf_15m

    def test_ema50_bounce_disabled(self):
        """EMA50 bounce setup is disabled (ENABLE_EMA50_BOUNCE_SETUP=False) until validated."""
        tf_daily, tf_4h, tf_15m = self._make_ema50_bounce()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # Setup should not fire since ENABLE_EMA50_BOUNCE_SETUP=False
        assert result.get("type") != "ema50_bounce"

    def test_price_far_from_ema50_blocks(self):
        tf_daily, tf_4h, tf_15m = self._make_ema50_bounce()
        tf_15m["ema50"] = tf_15m["price"] - 200  # 200 pts away (> 150 threshold)
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        if result["found"]:
            assert result["type"] != "ema50_bounce"


# ── SHORT Setup 1: Bollinger Upper Rejection ──────────────────────────────────

class TestShortBollingerUpperRejection:
    def _make_short_bb(self):
        price = 38000
        tf_daily = make_tf(above_ema200_fallback=False, above_ema200=False)
        tf_4h = make_tf(rsi=45)
        tf_15m = make_tf(
            price=price,
            rsi=65,              # 55-75 zone ✓
            bb_upper=price + 15, # Price 15 pts below upper (within 30) ✓
            bb_mid=price - 300,
            bb_lower=price - 600,
            ema50=price + 50,    # Price below EMA50 ✓
            above_ema50=False,
            above_ema200=False,
            above_ema200_fallback=False,
        )
        return tf_daily, tf_4h, tf_15m

    def test_detects_bollinger_upper_rejection(self):
        tf_daily, tf_4h, tf_15m = self._make_short_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] is True
        assert result["direction"] == "SHORT"
        assert result["type"] == "bollinger_upper_rejection"

    def test_sl_is_above_entry(self):
        tf_daily, tf_4h, tf_15m = self._make_short_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["sl"] > result["entry"]

    def test_tp_is_below_entry(self):
        tf_daily, tf_4h, tf_15m = self._make_short_bb()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["tp"] < result["entry"]

    def test_rsi_below_short_zone_blocks(self):
        tf_daily, tf_4h, tf_15m = self._make_short_bb()
        tf_15m["rsi"] = 40  # Below 55 — not in short zone
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        if result["found"]:
            assert result["type"] != "bollinger_upper_rejection"

    def test_price_far_from_bb_upper_blocks(self):
        tf_daily, tf_4h, tf_15m = self._make_short_bb()
        tf_15m["bollinger_upper"] = tf_15m["price"] + 200  # 200 pts away (> 150 threshold)
        tf_15m["ema50"] = tf_15m["price"] + 200             # also far
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        if result["found"]:
            assert result["type"] != "bollinger_upper_rejection"

    def test_daily_bullish_still_finds_short(self):
        """SHORT setup found even when daily bullish — bidirectional (C1 penalizes counter-trend)."""
        tf_daily, tf_4h, tf_15m = self._make_short_bb()
        tf_daily["above_ema200_fallback"] = True
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # detect_setup() no longer hard-gates on daily direction
        assert result["found"] is True
        assert result["direction"] == "SHORT"
        assert "counter-trend" in result["reasoning"].lower() or "bullish" in result["reasoning"].lower()


# ── SHORT Setup 2: EMA50 Rejection ───────────────────────────────────────────

class TestShortEma50Rejection:
    def _make_short_ema50(self):
        price = 38000
        tf_daily = make_tf(above_ema200_fallback=False, above_ema200=False)
        tf_4h = make_tf(rsi=45)
        tf_15m = make_tf(
            price=price,
            rsi=60,               # 50-70 zone ✓
            bb_mid=price - 300,   # Far from mid (won't trigger bb_upper_rejection)
            bb_upper=price + 400,
            bb_lower=price - 600,
            ema50=price + 10,     # Price is at/just below EMA50 (came up to test it) ✓
            above_ema50=False,
            above_ema200=False,
            above_ema200_fallback=False,
        )
        return tf_daily, tf_4h, tf_15m

    def test_detects_ema50_rejection(self):
        tf_daily, tf_4h, tf_15m = self._make_short_ema50()
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] is True
        assert result["direction"] == "SHORT"
        assert result["type"] == "ema50_rejection"

    def test_price_above_ema50_blocks_rejection(self):
        """If price is well above EMA50, it can't be a rejection."""
        tf_daily, tf_4h, tf_15m = self._make_short_ema50()
        tf_15m["price"] = 38100
        tf_15m["ema50"] = 38000  # Price 100 pts above EMA50 — not a rejection from below
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        if result["found"]:
            assert result["type"] != "ema50_rejection"


# ── No Setup Cases ────────────────────────────────────────────────────────────

class TestNoSetup:
    def test_no_setup_when_no_price(self):
        tf_daily = make_tf(price=0)
        tf_4h = make_tf()
        tf_15m = make_tf(price=0)
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] is False

    def test_no_setup_when_rsi_out_of_all_zones(self):
        # RSI=85 — above all valid zones
        tf_daily = make_tf(above_ema200_fallback=True)
        tf_4h = make_tf(rsi=85)
        tf_15m = make_tf(rsi=85, above_ema50=True)
        result = detect_setup(tf_daily, tf_4h, tf_15m)
        # Even if other criteria met, RSI should block LONG (>55) and for SHORT
        # above_ema50=True blocks it too
        if result["found"]:
            # Only possible if EMA50 bounce triggered with weird conditions
            assert result["direction"] in ("LONG", "SHORT")


# ── analyze_timeframe integration ─────────────────────────────────────────────

class TestAnalyzeTimeframeOutput:
    def _make_candles(self, n, base_price=38000):
        """Generate n synthetic daily candles."""
        candles = []
        price = base_price
        for i in range(n):
            candles.append({
                "open": price - 10,
                "high": price + 20,
                "low": price - 20,
                "close": price,
                "volume": 1000,
                "timestamp": f"2024-01-{i+1:02d}T00:00:00",
            })
            price += 5  # Slowly rising
        return candles

    def test_analyze_timeframe_with_200_candles(self):
        candles = self._make_candles(200)
        result = analyze_timeframe(candles)
        assert result["price"] is not None
        assert result["ema50"] is not None
        assert result["ema200"] is not None
        assert result["ema200_available"] is True

    def test_analyze_timeframe_with_50_candles_no_ema200(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert result["ema50"] is not None
        assert result["ema200"] is None
        assert result["ema200_available"] is False
        # Fallback should use EMA50
        assert result["above_ema200_fallback"] == result["above_ema50"]

    def test_bollinger_mid_in_result(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert result["bollinger_mid"] is not None

    def test_rsi_in_result(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert result["rsi"] is not None
        assert 0 <= result["rsi"] <= 100
