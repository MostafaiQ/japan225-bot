"""
Unit tests for core modules.
Run with: python -m pytest tests/ -v
No API credentials needed - tests pure math and logic.
"""
import pytest
import math
from core.indicators import (
    ema, sma, bollinger_bands, rsi, vwap,
    analyze_timeframe, detect_higher_lows, detect_setup,
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
            "prev_close": 38450,      # Previous candle lower → bounce_starting=True
            "bollinger_mid": 38490,   # Price 10 pts from mid (within 150-pt threshold)
            "bollinger_upper": 38700,
            "bollinger_lower": 38300,
            "rsi": 42,                # In 35-48 LONG zone (RSI_ENTRY_HIGH_BOUNCE=48)
            "above_ema50": True,
            "above_ema200": True,
            "ema50": 38450,
        }

        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] == True
        assert result["type"] == "bollinger_mid_bounce"
        assert result["direction"] == "LONG"

    def test_bearish_daily_rejects(self):
        """Should not find a LONG setup when daily trend is bearish/unknown."""
        tf_daily = {"above_ema200_fallback": False, "above_ema200": False}
        tf_4h = {"rsi": 50}
        tf_15m = {"price": 38000}

        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] == False
        # No LONG setup when daily is bearish — result may be a SHORT or no setup

    def test_overbought_4h_rejects(self):
        """4H RSI overbought reduces LONG quality but doesn't hard-block;
        with missing 15M data there still should be no setup detected."""
        tf_daily = {"above_ema200_fallback": True, "above_ema200": True}
        tf_4h = {"rsi": 80}   # Overbought — reduces quality but no hard block
        tf_15m = {"price": 38000}  # Missing BB/RSI/EMA50 → cannot form setup

        result = detect_setup(tf_daily, tf_4h, tf_15m)
        assert result["found"] == False


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
