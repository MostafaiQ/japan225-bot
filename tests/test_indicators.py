"""
Unit tests for core modules.
Run with: python -m pytest tests/ -v
No API credentials needed - tests pure math and logic.
"""
import pytest
import math
from core.indicators import (
    ema, sma, bollinger_bands, rsi, vwap, heiken_ashi,
    analyze_timeframe, detect_higher_lows, detect_setup,
    pivot_points, detect_candlestick_patterns, analyze_body_trend,
)


class TestSMA:
    def test_basic(self):
        prices = [1, 2, 3, 4, 5]
        result = sma(prices, 3)
        assert result == [None, None, 2.0, 3.0, 4.0]
    
    def test_single_period(self):
        result = sma([5, 10, 15], 1)
        assert result == [5.0, 10.0, 15.0]
    
    def test_too_few_prices(self):
        result = sma([1, 2], 5)
        assert result == []


class TestEMA:
    def test_basic(self):
        prices = [22, 22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24]
        result = ema(prices, 5)
        assert len(result) == len(prices)
        assert result[4] is not None  # First EMA value at index period-1
        # EMA should be close to SMA for first value
        expected_first = sum(prices[:5]) / 5
        assert abs(result[4] - expected_first) < 0.01
    
    def test_empty(self):
        assert ema([], 5) == []
    
    def test_too_few(self):
        assert ema([1, 2], 5) == []


class TestBollingerBands:
    def test_basic(self):
        # 25 data points
        prices = [float(i) for i in range(100, 125)]
        bb = bollinger_bands(prices, 20, 2.0)
        
        assert len(bb["upper"]) == 25
        assert len(bb["mid"]) == 25
        assert len(bb["lower"]) == 25
        
        # Last values should exist
        assert bb["upper"][-1] is not None
        assert bb["mid"][-1] is not None
        assert bb["lower"][-1] is not None
        
        # Upper > Mid > Lower
        assert bb["upper"][-1] > bb["mid"][-1] > bb["lower"][-1]
    
    def test_constant_prices(self):
        """With constant prices, bands should collapse."""
        prices = [100.0] * 25
        bb = bollinger_bands(prices, 20, 2.0)
        # All bands should be equal (zero std dev)
        assert abs(bb["upper"][-1] - bb["lower"][-1]) < 0.01


class TestRSI:
    def test_uptrend(self):
        """In a pure uptrend, RSI should be near 100."""
        prices = [float(i) for i in range(100, 120)]
        result = rsi(prices, 14)
        assert result[-1] > 80  # Strong uptrend = high RSI
    
    def test_downtrend(self):
        """In a pure downtrend, RSI should be near 0."""
        prices = [float(i) for i in range(120, 100, -1)]
        result = rsi(prices, 14)
        assert result[-1] < 20  # Strong downtrend = low RSI
    
    def test_range(self):
        """RSI should always be between 0 and 100."""
        prices = [100 + (i % 7) * (-1 if i % 3 == 0 else 1) for i in range(50)]
        result = rsi(prices, 14)
        for val in result:
            if val is not None:
                assert 0 <= val <= 100


class TestVWAP:
    def test_basic(self):
        highs = [105, 110, 108]
        lows = [95, 100, 98]
        closes = [100, 105, 103]
        volumes = [1000, 2000, 1500]
        
        result = vwap(highs, lows, closes, volumes)
        assert len(result) == 3
        # First VWAP = typical price of first candle
        tp1 = (105 + 95 + 100) / 3
        assert abs(result[0] - tp1) < 0.01
    
    def test_zero_volume(self):
        """With zero volume, VWAP should be typical price."""
        result = vwap([100], [90], [95], [0])
        assert abs(result[0] - 95.0) < 0.01  # (100+90+95)/3


class TestHigherLows:
    def test_ascending_lows(self):
        # Zigzag pattern with higher lows - needs 2+ candles on each side of swing
        prices = [110, 105, 100, 98, 100, 105, 110, 108, 103, 101, 103, 108, 113, 110, 106, 104, 107, 112]
        # Swing lows at 98, 101, 104 -> ascending
        assert detect_higher_lows(prices, 3) == True
    
    def test_descending_lows(self):
        prices = [100, 95, 105, 90, 110, 85, 115]
        assert detect_higher_lows(prices, 3) == False
    
    def test_too_few_points(self):
        assert detect_higher_lows([100, 105], 5) == False


class TestDetectSetup:
    def test_bullish_setup(self):
        """Should find a Bollinger mid bounce when all conditions met.

        detect_setup() reads 'above_ema200_fallback' from tf_daily (not 'above_ema200').
        Price must be within 30 pts of BB mid (point distance, not percentile).
        """
        tf_daily = {
            "price": 38500,
            "above_ema200_fallback": True,  # Required key for trend detection
            "above_ema200": True,
            "rsi": 55,
        }
        tf_4h = {
            "price": 38500,
            "rsi": 55,  # Not overbought
        }
        tf_15m = {
            "price": 38500,
            "open": 38480,            # Opened below close → green candle
            "low": 38455,             # lower_wick = min(38480,38500) - 38455 = 25pts >= 20 ✓
            "prev_close": 38450,      # Previous candle lower → bounce confirmation
            "bollinger_mid": 38490,   # Price 10 pts from mid (within 150-pt threshold)
            "bollinger_upper": 38700,
            "bollinger_lower": 38300,
            "rsi": 42,                # In 35-65 LONG zone (RSI_ENTRY_HIGH_BOUNCE=65)
            "above_ema50": True,
            "above_ema200": True,
            "ema50": 38450,
        }

        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] == True
        assert result["type"] == "bollinger_mid_bounce"
        assert result["direction"] == "LONG"

    def test_bearish_daily_no_hard_block(self):
        """detect_setup() is bidirectional — no daily hard gate. Missing BB/RSI → no setup."""
        tf_daily = {"above_ema200_fallback": False, "above_ema200": False}
        tf_4h = {"rsi": 50}
        tf_15m = {"price": 38000}  # Missing BB/RSI/EMA50 → no setup possible

        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] == False

    def test_overbought_4h_rejects(self):
        """4H RSI overbought reduces LONG quality but doesn't hard-block;
        with missing 15M data there still should be no setup detected."""
        tf_daily = {"above_ema200_fallback": True, "above_ema200": True}
        tf_4h = {"rsi": 80}   # Overbought — reduces quality but no hard block
        tf_15m = {"price": 38000}  # Missing BB/RSI/EMA50 → cannot form setup

        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] == False


class TestHeikenAshi:
    def test_basic_calculation(self):
        opens  = [100, 102, 104, 103]
        highs  = [105, 107, 108, 106]
        lows   = [98,  100, 101, 100]
        closes = [103, 105, 106, 104]
        ha_o, ha_h, ha_l, ha_c = heiken_ashi(opens, highs, lows, closes)
        assert len(ha_o) == 4
        # First HA_close = (O+H+L+C)/4
        assert ha_c[0] == pytest.approx((100 + 105 + 98 + 103) / 4)
        # First HA_open = (O+C)/2
        assert ha_o[0] == pytest.approx((100 + 103) / 2)

    def test_second_candle_uses_prior_ha(self):
        opens  = [100, 102]
        highs  = [105, 107]
        lows   = [98,  100]
        closes = [103, 105]
        ha_o, ha_h, ha_l, ha_c = heiken_ashi(opens, highs, lows, closes)
        # Second HA_open = (prev_HA_open + prev_HA_close) / 2
        assert ha_o[1] == pytest.approx((ha_o[0] + ha_c[0]) / 2)
        # Second HA_close = (O+H+L+C)/4
        assert ha_c[1] == pytest.approx((102 + 107 + 100 + 105) / 4)

    def test_empty_input(self):
        ha_o, ha_h, ha_l, ha_c = heiken_ashi([], [], [], [])
        assert ha_o == [] and ha_c == []

    def test_single_candle(self):
        ha_o, ha_h, ha_l, ha_c = heiken_ashi([100], [110], [90], [105])
        assert len(ha_o) == 1
        assert ha_c[0] == pytest.approx((100 + 110 + 90 + 105) / 4)

    def test_bullish_candle(self):
        """Uptrend should produce HA bullish candles (close > open)."""
        opens  = [100, 102, 104, 106, 108]
        highs  = [105, 107, 109, 111, 113]
        lows   = [99,  101, 103, 105, 107]
        closes = [103, 105, 107, 109, 111]
        ha_o, ha_h, ha_l, ha_c = heiken_ashi(opens, highs, lows, closes)
        # Last HA candle should be bullish
        assert ha_c[-1] > ha_o[-1]


class TestAnalyzeTimeframeNewIndicators:
    """Test new indicators added in Phase 1: HA, FVG, Fibonacci, PDH/PDL, Sweep."""

    def _make_candles(self, n, base_price=38000):
        candles = []
        for i in range(n):
            price = base_price + (i % 50) * 10
            candles.append({
                "open": price,
                "high": price + 50,
                "low": price - 50,
                "close": price + 20,
                "volume": 1000 + i * 10,
                "timestamp": f"2026-02-27T{i:04d}",
            })
        return candles

    def test_ha_fields_present(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "ha_bullish" in result
        assert "ha_streak" in result
        assert isinstance(result["ha_bullish"], bool)
        assert isinstance(result["ha_streak"], int)

    def test_fvg_fields_present(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "fvg_bullish" in result
        assert "fvg_bearish" in result
        assert "fvg_level" in result

    def test_fibonacci_fields_present(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "fibonacci" in result
        assert "fib_near" in result
        if result["fibonacci"]:
            assert "fib_236" in result["fibonacci"]
            assert "fib_618" in result["fibonacci"]

    def test_pdh_pdl_fields(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "prev_candle_high" in result
        assert "prev_candle_low" in result
        assert result["prev_candle_high"] == candles[-2]["high"]
        assert result["prev_candle_low"] == candles[-2]["low"]

    def test_sweep_fields_present(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "swept_low" in result
        assert "swept_high" in result

    def test_bullish_fvg_detection(self):
        """Bullish FVG: candle[i-2].high < candle[i].low (gap up)."""
        candles = self._make_candles(50)
        # Create a bullish FVG at the end: candle[-3].high < candle[-1].low
        candles[-3]["high"] = 37000
        candles[-1]["low"] = 37100  # Gap: 37000 to 37100
        candles[-1]["close"] = 37200
        result = analyze_timeframe(candles)
        assert result["fvg_bullish"] is True
        assert result["fvg_level"] == pytest.approx((37000 + 37100) / 2, abs=1)

    def test_bearish_fvg_detection(self):
        """Bearish FVG: candle[i-2].low > candle[i].high (gap down)."""
        candles = self._make_candles(50)
        candles[-3]["low"] = 39000
        candles[-1]["high"] = 38900  # Gap: 39000 to 38900
        candles[-1]["close"] = 38800
        result = analyze_timeframe(candles)
        assert result["fvg_bearish"] is True

    def test_fibonacci_levels_computed(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        fib = result["fibonacci"]
        if fib:
            sh = result["swing_high_20"]
            sl = result["swing_low_20"]
            rng = sh - sl
            assert fib["fib_500"] == pytest.approx(sh - 0.5 * rng, abs=1)

    def test_swept_low_detection(self):
        """Swept low: last candle low < prev swing low but close > prev swing low."""
        candles = self._make_candles(50)
        # Set a clear swing low in the last 20 candles (excluding current)
        prev_swing_low = min(c["low"] for c in candles[-21:-1])
        candles[-1]["low"] = prev_swing_low - 100   # Dips below
        candles[-1]["close"] = prev_swing_low + 50   # Closes above
        result = analyze_timeframe(candles)
        assert result["swept_low"] is True

    def test_swept_high_detection(self):
        """Swept high: last candle high > prev swing high but close < prev swing high."""
        candles = self._make_candles(50)
        prev_swing_high = max(c["high"] for c in candles[-21:-1])
        candles[-1]["high"] = prev_swing_high + 100   # Pokes above
        candles[-1]["close"] = prev_swing_high - 50    # Closes below
        result = analyze_timeframe(candles)
        assert result["swept_high"] is True


class TestAnalyzeTimeframe:
    def test_with_enough_data(self):
        """Should calculate all indicators with 200+ candles."""
        candles = []
        for i in range(250):
            base = 38000 + (i % 50) * 10
            candles.append({
                "open": base,
                "high": base + 50,
                "low": base - 50,
                "close": base + 20,
                "volume": 1000 + i * 10,
                "timestamp": f"2026-02-27T{i:04d}",
            })

        result = analyze_timeframe(candles)
        assert "price" in result
        assert "bollinger_upper" in result
        assert "bollinger_mid" in result
        assert "bollinger_lower" in result
        assert "ema50" in result
        assert "ema200" in result
        assert "rsi" in result

        # All values should be numbers
        for key in ["price", "bollinger_upper", "bollinger_mid", "bollinger_lower", "ema50", "ema200", "rsi"]:
            assert result[key] is not None
            assert isinstance(result[key], (int, float))


# --- Risk Manager Tests ---
class TestRiskManager:
    """Test risk management rules."""
    
    def test_margin_check(self):
        """Verify margin calculation matches expected values."""
        from config.settings import calculate_margin
        
        # At price 59,500: 0.02 lots should be ~$5.95
        margin = calculate_margin(0.02, 59500)
        assert abs(margin - 5.95) < 0.1
        
        # At price 38,000: 0.05 lots
        margin = calculate_margin(0.05, 38000)
        expected = 0.05 * 1 * 38000 * 0.005  # = 9.50
        assert abs(margin - expected) < 0.01
    
    def test_profit_calculation(self):
        from config.settings import calculate_profit
        
        # 0.02 lots, 100 points
        profit = calculate_profit(0.02, 100)
        assert abs(profit - 2.0) < 0.01
        
        # 0.10 lots, 300 points
        profit = calculate_profit(0.10, 300)
        assert abs(profit - 30.0) < 0.01
    
    def test_lot_sizing(self):
        from config.settings import get_lot_size
        
        # With $20 balance at 38,000 price
        lots = get_lot_size(20, 38000)
        margin = lots * 1 * 38000 * 0.005
        assert margin <= 20 * 0.5  # Margin under 50%
        
        # With $100 balance
        lots = get_lot_size(100, 38000)
        margin = lots * 1 * 38000 * 0.005
        assert margin <= 100 * 0.5


class TestPivotPoints:
    def test_basic_calculation(self):
        pp = pivot_points(39000, 38000, 38500)
        assert pp["pp"] == pytest.approx((39000 + 38000 + 38500) / 3, abs=0.2)

    def test_all_7_levels_present(self):
        pp = pivot_points(39000, 38000, 38500)
        for key in ("pp", "r1", "r2", "r3", "s1", "s2", "s3"):
            assert key in pp
            assert isinstance(pp[key], float)

    def test_r_ordering(self):
        pp = pivot_points(39000, 38000, 38500)
        assert pp["r1"] < pp["r2"] < pp["r3"]

    def test_s_ordering(self):
        pp = pivot_points(39000, 38000, 38500)
        assert pp["s1"] > pp["s2"] > pp["s3"]

    def test_narrow_range(self):
        """When H==L==C, all levels collapse to the same value."""
        pp = pivot_points(38500, 38500, 38500)
        assert pp["pp"] == 38500.0
        assert pp["r1"] == 38500.0
        assert pp["s1"] == 38500.0


class TestCandlestickPatterns:
    def test_hammer(self):
        # Body ~3pts at top, lower wick ~12pts, upper wick ~1pt, range=16
        candles = [{"open": 98, "high": 102, "low": 86, "close": 101}]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "hammer"
        assert result["pattern_direction"] == "bullish"
        assert result["pattern_strength"] == "strong"

    def test_bullish_engulfing(self):
        candles = [
            {"open": 105, "high": 106, "low": 98, "close": 99},   # bearish
            {"open": 98,  "high": 110, "low": 97, "close": 108},  # bullish engulfs
        ]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "bullish_engulfing"
        assert result["pattern_direction"] == "bullish"

    def test_bearish_engulfing(self):
        candles = [
            {"open": 99,  "high": 106, "low": 98, "close": 105},  # bullish
            {"open": 106, "high": 107, "low": 95, "close": 96},   # bearish engulfs
        ]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "bearish_engulfing"
        assert result["pattern_direction"] == "bearish"

    def test_doji(self):
        candles = [{"open": 100.0, "high": 105, "low": 95, "close": 100.5}]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "doji"
        assert result["pattern_direction"] == "neutral"

    def test_marubozu(self):
        candles = [{"open": 100, "high": 110.5, "low": 99.5, "close": 110}]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "marubozu"
        assert result["pattern_direction"] == "bullish"

    def test_insufficient_candles(self):
        result = detect_candlestick_patterns([])
        assert result["pattern_name"] is None

    def test_no_pattern_flat(self):
        """Flat candle with no clear pattern should return None or a specific pattern."""
        candles = [{"open": 100, "high": 105, "low": 96, "close": 103}]
        result = detect_candlestick_patterns(candles)
        # Should return some pattern or None — just check it doesn't error
        assert isinstance(result, dict)
        assert "pattern_name" in result

    def test_morning_star(self):
        # c2 must have body/range < 0.3: body=0.5, range=4 → 0.125
        candles = [
            {"open": 110, "high": 111, "low": 100, "close": 101},    # bearish
            {"open": 100.5, "high": 102, "low": 98, "close": 100},   # small body (0.5/4=0.125)
            {"open": 101, "high": 112, "low": 100, "close": 111},    # bullish, closes above c1 midpoint (105.5)
        ]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "morning_star"
        assert result["pattern_direction"] == "bullish"

    def test_evening_star(self):
        # c2 must have body/range < 0.3: body=0.5, range=4 → 0.125
        candles = [
            {"open": 100, "high": 111, "low": 99, "close": 110},     # bullish
            {"open": 110.5, "high": 113, "low": 109, "close": 111},  # small body (0.5/4=0.125)
            {"open": 110, "high": 112, "low": 99, "close": 100},     # bearish, closes below c1 midpoint (105)
        ]
        result = detect_candlestick_patterns(candles)
        assert result["pattern_name"] == "evening_star"
        assert result["pattern_direction"] == "bearish"


class TestBodyTrend:
    def test_expanding(self):
        candles = [
            {"open": 100, "high": 102, "low": 99, "close": 101},
            {"open": 101, "high": 103, "low": 100, "close": 102},
            {"open": 102, "high": 106, "low": 101, "close": 105},
            {"open": 105, "high": 112, "low": 104, "close": 111},
            {"open": 111, "high": 120, "low": 110, "close": 119},
        ]
        result = analyze_body_trend(candles, lookback=5)
        assert result["body_trend"] == "expanding"

    def test_contracting(self):
        candles = [
            {"open": 100, "high": 120, "low": 99, "close": 119},
            {"open": 119, "high": 130, "low": 118, "close": 128},
            {"open": 128, "high": 132, "low": 127, "close": 130},
            {"open": 130, "high": 132, "low": 129, "close": 131},
            {"open": 131, "high": 132, "low": 130.5, "close": 131.5},
        ]
        result = analyze_body_trend(candles, lookback=5)
        assert result["body_trend"] == "contracting"

    def test_consecutive_green(self):
        candles = [
            {"open": 100, "high": 105, "low": 99, "close": 104},
            {"open": 104, "high": 109, "low": 103, "close": 108},
            {"open": 108, "high": 113, "low": 107, "close": 112},
        ]
        result = analyze_body_trend(candles, lookback=5)
        assert result["consecutive_direction"] == 3

    def test_consecutive_red(self):
        candles = [
            {"open": 112, "high": 113, "low": 107, "close": 108},
            {"open": 108, "high": 109, "low": 103, "close": 104},
            {"open": 104, "high": 105, "low": 99, "close": 100},
        ]
        result = analyze_body_trend(candles, lookback=5)
        assert result["consecutive_direction"] == -3

    def test_avg_body_size(self):
        candles = [
            {"open": 100, "high": 115, "low": 95, "close": 110},  # body=10
            {"open": 110, "high": 125, "low": 105, "close": 120},  # body=10
        ]
        result = analyze_body_trend(candles, lookback=5)
        assert result["avg_body_size"] == 10.0

    def test_wick_ratio(self):
        # Body=10, upper_wick=5, lower_wick=5 → wick_ratio=1.0
        candles = [
            {"open": 100, "high": 115, "low": 95, "close": 110},
            {"open": 100, "high": 115, "low": 95, "close": 110},
        ]
        result = analyze_body_trend(candles, lookback=5)
        assert result["wick_ratio"] == 1.0

    def test_insufficient_candles(self):
        result = analyze_body_trend([{"open": 100, "high": 110, "low": 90, "close": 105}])
        assert result["body_trend"] == "neutral"
        assert result["consecutive_direction"] == 0


class TestAnalyzeTimeframePhase2:
    """Test Phase 2 indicators: candlestick patterns, body trend in analyze_timeframe output."""

    def _make_candles(self, n, base_price=38000):
        candles = []
        for i in range(n):
            price = base_price + (i % 50) * 10
            candles.append({
                "open": price,
                "high": price + 50,
                "low": price - 50,
                "close": price + 20,
                "volume": 1000 + i * 10,
                "timestamp": f"2026-03-02T{i:04d}",
            })
        return candles

    def test_candlestick_fields_present(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "candlestick_pattern" in result
        assert "candlestick_direction" in result
        assert "candlestick_strength" in result

    def test_body_trend_fields_present(self):
        candles = self._make_candles(50)
        result = analyze_timeframe(candles)
        assert "body_trend" in result
        assert "consecutive_direction" in result
        assert "avg_body_size" in result
        assert "wick_ratio" in result


class TestDetectSetupPhase2Snapshot:
    """Test that Phase 2 keys appear in detect_setup indicators_snapshot."""

    def _base_tf_daily(self):
        return {
            "price": 38500, "above_ema200_fallback": True, "above_ema200": True, "rsi": 55,
            "prev_candle_high": 38600, "prev_candle_low": 38200, "prev_close": 38450,
        }

    def _base_tf_4h(self):
        return {"price": 38500, "rsi": 55}

    def _bb_mid_bounce_tf_15m(self):
        return {
            "price": 38500, "open": 38480, "low": 38455,
            "prev_close": 38450,
            "bollinger_mid": 38490, "bollinger_upper": 38700, "bollinger_lower": 38300,
            "rsi": 42, "above_ema50": True, "above_ema200": True, "ema50": 38450,
            "vwap": None, "above_vwap": None,
            "ha_bullish": None, "ha_streak": None,
            "fib_near": None, "fvg_bullish": False, "fvg_bearish": False,
            "swept_low": False, "swept_high": False,
            "candlestick_pattern": "hammer", "candlestick_direction": "bullish",
            "candlestick_strength": "strong",
            "body_trend": "contracting", "consecutive_direction": -2,
            "avg_body_size": 15.0, "wick_ratio": 1.2,
        }

    def test_pivots_in_snapshot(self):
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), self._bb_mid_bounce_tf_15m())
        snap = result["indicators_snapshot"]
        assert "pivots" in snap
        assert isinstance(snap["pivots"], dict)
        assert "pp" in snap["pivots"]

    def test_candlestick_in_snapshot(self):
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), self._bb_mid_bounce_tf_15m())
        snap = result["indicators_snapshot"]
        assert snap["candlestick_pattern"] == "hammer"
        assert snap["candlestick_direction"] == "bullish"

    def test_body_trend_in_snapshot(self):
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), self._bb_mid_bounce_tf_15m())
        snap = result["indicators_snapshot"]
        assert snap["body_trend"] == "contracting"
        assert snap["consecutive_direction"] == -2
        assert snap["wick_ratio"] == 1.2

    def test_pivot_confluence_in_reasoning(self):
        """Pivot near S1 should add confluence for LONG."""
        tf_daily = self._base_tf_daily()
        tf_15m = self._bb_mid_bounce_tf_15m()
        # Compute what S1 would be and set price near it
        pp_val = (tf_daily["prev_candle_high"] + tf_daily["prev_candle_low"] + tf_daily["prev_close"]) / 3
        s1_val = 2 * pp_val - tf_daily["prev_candle_high"]
        tf_15m["price"] = s1_val + 10  # Within 100pts of S1
        tf_15m["bollinger_mid"] = s1_val + 20  # Keep near mid
        tf_15m["prev_close"] = s1_val  # bounce_starting
        result = detect_setup(tf_daily, self._base_tf_4h(), tf_15m)
        if result["found"]:
            assert "pivot S1" in result["reasoning"]

    def test_candlestick_confluence_in_reasoning(self):
        """Bullish hammer + LONG should add confluence."""
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), self._bb_mid_bounce_tf_15m())
        assert result["found"]
        assert "hammer" in result["reasoning"]


class TestDetectSetupConfluence:
    """Test Phase 1 confluence/counter signals wired into detect_setup() reasoning."""

    def _base_tf_daily(self):
        return {"price": 38500, "above_ema200_fallback": True, "above_ema200": True, "rsi": 55}

    def _base_tf_4h(self):
        return {"price": 38500, "rsi": 55}

    def _bb_mid_bounce_tf_15m(self, **overrides):
        tf = {
            "price": 38500, "open": 38480, "low": 38455,
            "prev_close": 38450,
            "bollinger_mid": 38490, "bollinger_upper": 38700, "bollinger_lower": 38300,
            "rsi": 42, "above_ema50": True, "above_ema200": True, "ema50": 38450,
            # Phase 1 defaults — no signals
            "vwap": None, "above_vwap": None,
            "ha_bullish": None, "ha_streak": None,
            "fib_near": None, "fvg_bullish": False, "fvg_bearish": False,
            "swept_low": False, "swept_high": False,
        }
        tf.update(overrides)
        return tf

    def test_fib_near_adds_confluence(self):
        tf_15m = self._bb_mid_bounce_tf_15m(fib_near="fib_618")
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "Confluence:" in result["reasoning"]
        assert "Fib fib_618" in result["reasoning"]

    def test_swept_low_adds_confluence_for_long(self):
        tf_15m = self._bb_mid_bounce_tf_15m(swept_low=True)
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "swept low" in result["reasoning"]

    def test_swept_high_adds_counter_for_long(self):
        tf_15m = self._bb_mid_bounce_tf_15m(swept_high=True)
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "Caution:" in result["reasoning"]
        assert "swept high" in result["reasoning"]

    def test_fvg_bullish_adds_confluence_for_long(self):
        tf_15m = self._bb_mid_bounce_tf_15m(fvg_bullish=True)
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "bullish FVG" in result["reasoning"]

    def test_vwap_below_adds_confluence_for_long(self):
        tf_15m = self._bb_mid_bounce_tf_15m(vwap=38600, above_vwap=False)
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "below VWAP" in result["reasoning"]

    def test_ha_streak_adds_confluence_for_long(self):
        tf_15m = self._bb_mid_bounce_tf_15m(ha_bullish=True, ha_streak=3)
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "HA streak 3" in result["reasoning"]

    def test_no_phase1_data_no_confluence(self):
        """When all Phase 1 indicators are absent/default, no Confluence/Caution appended."""
        tf_15m = self._bb_mid_bounce_tf_15m()
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        assert result["found"]
        assert "Confluence:" not in result["reasoning"]
        assert "Caution:" not in result["reasoning"]

    def test_snapshot_has_phase1_keys(self):
        tf_15m = self._bb_mid_bounce_tf_15m(vwap=38600, above_vwap=False, fib_near="fib_500")
        result = detect_setup(self._base_tf_daily(), self._base_tf_4h(), tf_15m)
        snap = result["indicators_snapshot"]
        assert "vwap" in snap
        assert "above_vwap" in snap
        assert "ha_bullish" in snap
        assert "ha_streak" in snap
        assert "fib_near" in snap
        assert "fvg_bullish" in snap
        assert "swept_low" in snap
        assert "prev_candle_high" in snap


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
