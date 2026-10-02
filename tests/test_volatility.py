"""Unit tests for Dynamic Volatility & Velocity Guard."""

from decimal import Decimal
import unittest

from volatility import compute_volatility_target, check_market_velocity


class TestVolatilityEngine(unittest.TestCase):
    def test_check_market_velocity_insufficient_candles(self):
        candles = [[1700000000, "2000", "2010", "1990", "2005", "2002", "10", 5]]
        is_high, roc, stats = check_market_velocity(candles, lookback_hours=3)
        self.assertFalse(is_high)
        self.assertEqual(roc, Decimal("0.0"))
        self.assertIn("reason", stats)

    def test_check_market_velocity_calm_market(self):
        # 3 candles with very calm price action: 2000 -> 2004 (+0.2%)
        candles = [
            [1700000000, "2000.0", "2005.0", "1995.0", "2001.0", "2000.5", "10", 5],
            [1700003600, "2001.0", "2006.0", "1998.0", "2003.0", "2002.5", "10", 5],
            [1700007200, "2003.0", "2007.0", "2001.0", "2004.0", "2004.0", "10", 5],
        ]
        is_high, roc, stats = check_market_velocity(candles, lookback_hours=3, threshold_percent=Decimal("1.8"))
        self.assertFalse(is_high)
        self.assertEqual(roc, Decimal("0.20"))
        self.assertFalse(stats["is_high_velocity"])

    def test_check_market_velocity_rapid_rally(self):
        # 3 candles with a sharp rally: 2000 -> 2060 (+3.0% > 1.8% threshold)
        candles = [
            [1700000000, "2000.0", "2020.0", "1995.0", "2015.0", "2010.5", "10", 5],
            [1700003600, "2015.0", "2040.0", "2010.0", "2038.0", "2025.5", "10", 5],
            [1700007200, "2038.0", "2065.0", "2035.0", "2060.0", "2050.0", "10", 5],
        ]
        is_high, roc, stats = check_market_velocity(candles, lookback_hours=3, threshold_percent=Decimal("1.8"))
        self.assertTrue(is_high)
        self.assertEqual(roc, Decimal("3.00"))
        self.assertTrue(stats["is_high_velocity"])

    def test_check_market_velocity_rapid_plunge(self):
        # 3 candles with a sharp plunge: 2000 -> 1940 (-3.0% > 1.8% threshold)
        candles = [
            [1700000000, "2000.0", "2005.0", "1980.0", "1985.0", "1990.0", "10", 5],
            [1700003600, "1985.0", "1990.0", "1955.0", "1960.0", "1970.0", "10", 5],
            [1700007200, "1960.0", "1965.0", "1935.0", "1940.0", "1950.0", "10", 5],
        ]
        is_high, roc, stats = check_market_velocity(candles, lookback_hours=3, threshold_percent=Decimal("1.8"))
        self.assertTrue(is_high)
        self.assertEqual(roc, Decimal("-3.00"))
        self.assertTrue(stats["is_high_velocity"])

    def test_check_market_velocity_single_candle_flash_shock(self):
        # Open-to-close across 3h is flat, but latest 1h candle has a 3.0% flash wick
        candles = [
            [1700000000, "2000.0", "2005.0", "1995.0", "2001.0", "2000.5", "10", 5],
            [1700003600, "2001.0", "2006.0", "1998.0", "2002.0", "2002.5", "10", 5],
            [1700007200, "2002.0", "2060.0", "1999.0", "2005.0", "2030.0", "10", 5],
        ]
        is_high, roc, stats = check_market_velocity(candles, lookback_hours=3, threshold_percent=Decimal("1.8"))
        self.assertTrue(is_high)
        self.assertTrue(stats["is_high_velocity"])


if __name__ == "__main__":
    unittest.main()
