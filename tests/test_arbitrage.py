"""Unit tests for Triangular Arbitrage calculations, routing, and capital scaling."""

from decimal import Decimal
import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure src/ is on python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from arbitrage.capital import calculate_trade_capital, ABSOLUTE_MIN_TRADE_GBP
from arbitrage.routes import TriangleRoute
from arbitrage.scanner import ArbitrageScanner, ArbitrageOpportunity


class TestArbitrageCapital(unittest.TestCase):

    def test_fee_buffer_preservation_and_micro_capital(self):
        """On £50 starting capital, reserves £2.00 fee buffer and scales to £45."""
        trade_size = calculate_trade_capital(
            current_cash=Decimal("50.00"),
            total_equity=Decimal("50.00"),
            min_fee_buffer=Decimal("2.00"),
            fee_buffer_percent=Decimal("3.0"),
        )
        self.assertEqual(trade_size, Decimal("48.00"))

    def test_monthly_scaling_with_larger_capital(self):
        """As monthly deposits scale equity to £200, fee buffer grows to 3% (£6.00)."""
        trade_size = calculate_trade_capital(
            current_cash=Decimal("200.00"),
            total_equity=Decimal("200.00"),
            min_fee_buffer=Decimal("2.00"),
            fee_buffer_percent=Decimal("3.0"),
        )
        # £200 - £6.00 fee buffer = £194.00
        self.assertEqual(trade_size, Decimal("194.00"))

    def test_max_trade_size_clamping_for_large_capital(self):
        """When equity grows to £1,000, trade size is clamped at max_trade_size to prevent slippage."""
        trade_size = calculate_trade_capital(
            current_cash=Decimal("1000.00"),
            total_equity=Decimal("1000.00"),
            max_trade_size=Decimal("250.00"),
        )
        self.assertEqual(trade_size, Decimal("250.00"))

    def test_insufficient_cash_returns_zero(self):
        """When available cash is below absolute minimum (£5.00), returns 0.00."""
        trade_size = calculate_trade_capital(
            current_cash=Decimal("4.00"),
            total_equity=Decimal("50.00"),
        )
        self.assertEqual(trade_size, Decimal("0.00"))


class TestArbitrageScannerMath(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.scanner = ArbitrageScanner(
            client=self.mock_client,
            maker_fee=Decimal("0.0025"),
            taker_fee=Decimal("0.0040"),
            min_net_profit_pct=Decimal("0.10"),
        )

        # Mock a clean Triangle: GBP <-> SOL <-> ETH <-> GBP
        self.test_route = TriangleRoute(
            asset_a="SOL",
            asset_b="ETH",
            leg1_pair="SOLGBP",
            cross_pair="SOLETH",
            leg3_pair="ETHGBP",
            cross_is_a_base=True,
        )
        self.scanner.routes = [self.test_route]
        self.scanner._initialized = True

    def test_profitable_forward_maker_opportunity(self):
        """Simulate prices where Forward Maker spread yields net profit."""
        # Prices:
        # Leg 1: SOL/GBP bid = 88.00, ask = 88.10
        # Cross: SOL/ETH bid = 0.045, ask = 0.045
        # Leg 3: ETH/GBP bid = 2000.00, ask = 2005.00
        # Forward Maker: (1 / 88.00) * 0.045 * 2005.00 = 1.02528 (+2.528% gross)
        # Net fee factor: (1 - 0.0025)^3 = 0.992518
        # Net = 1.02528 * 0.992518 = 1.01761 (+1.761% net!)
        self.scanner.fetch_tickers = MagicMock(return_value={
            "SOLGBP": {"b": ["88.00"], "a": ["88.10"]},
            "SOLETH": {"b": ["0.0449"], "a": ["0.0450"]},
            "ETHGBP": {"b": ["2000.00"], "a": ["2005.00"]},
        })

        opps = self.scanner.scan(trade_capital_gbp=Decimal("40.00"), priority_only=False, execution_type="MAKER")

        self.assertGreater(len(opps), 0)
        top = opps[0]
        self.assertEqual(top.direction, "FORWARD")
        self.assertGreater(top.net_profit_pct, Decimal("1.00"))
        self.assertGreater(top.expected_profit_gbp, Decimal("0.40"))

    def test_unprofitable_opportunity_is_filtered_out(self):
        """When prices are in exact equilibrium, net return is negative after fees and must be filtered."""
        # Normal equilibrium prices where gross is ~0.00%
        self.scanner.fetch_tickers = MagicMock(return_value={
            "SOLGBP": {"b": ["88.00"], "a": ["88.02"]},
            "SOLETH": {"b": ["0.0440"], "a": ["0.04401"]},
            "ETHGBP": {"b": ["2000.00"], "a": ["2000.50"]},
        })

        opps = self.scanner.scan(trade_capital_gbp=Decimal("40.00"), priority_only=False, execution_type="MAKER")
        self.assertEqual(len(opps), 0)


if __name__ == "__main__":
    unittest.main()
