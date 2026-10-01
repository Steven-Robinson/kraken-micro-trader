"""Unit tests for Triangular Arbitrage Atomic Execution and Safeguards."""

from decimal import Decimal
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure src/ is on python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from arbitrage.executor import ArbitrageExecutor
from arbitrage.routes import TriangleRoute, PairInfo
from arbitrage.scanner import ArbitrageOpportunity
from kraken_client import KrakenClient


class TestArbitrageExecutor(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock(spec=KrakenClient)
        self.mock_client.dry_run = False

        self.route = TriangleRoute(
            asset_a="ALGO",
            asset_b="ETH",
            leg1_pair="ALGOGBP",
            cross_pair="ALGOETH",
            leg3_pair="ETHGBP",
            cross_is_a_base=True,
        )

        self.pair_map = {
            "ALGOGBP": PairInfo(
                pair_id="ALGOGBP", wsname="ALGO/GBP", base="ALGO", quote="GBP",
                ordermin=Decimal("10"), pair_decimals=5, lot_decimals=8, altname="ALGOGBP"
            ),
            "ALGOETH": PairInfo(
                pair_id="ALGOETH", wsname="ALGO/ETH", base="ALGO", quote="ETH",
                ordermin=Decimal("10"), pair_decimals=8, lot_decimals=8, altname="ALGOETH"
            ),
            "ETHGBP": PairInfo(
                pair_id="ETHGBP", wsname="ETH/GBP", base="ETH", quote="GBP",
                ordermin=Decimal("0.001"), pair_decimals=2, lot_decimals=8, altname="ETHGBP"
            ),
        }

        self.executor = ArbitrageExecutor(
            client=self.mock_client,
            dry_run=False,
            min_net_pct=Decimal("0.30"),
            max_slippage_pct=Decimal("0.20"),
            cooldown_seconds=60.0,
        )

        self.opp_forward = ArbitrageOpportunity(
            route=self.route,
            direction="FORWARD",
            execution_type="TAKER",
            gross_multiplier=Decimal("1.02"),
            gross_profit_pct=Decimal("2.0"),
            net_multiplier=Decimal("1.008"),
            net_profit_pct=Decimal("0.80"),
            expected_profit_gbp=Decimal("0.24"),
            leg1_price=Decimal("0.10"),
            cross_price=Decimal("0.00005"),
            leg3_price=Decimal("2040.00"),
            timestamp=int(time.time()),
        )

    def test_hurdle_guardrail_rejects_sub_hurdle_opportunity(self):
        """Opportunities with net profit below min hurdle (e.g. +0.15% < +0.30%) are rejected."""
        sub_hurdle_opp = ArbitrageOpportunity(
            route=self.route,
            direction="FORWARD",
            execution_type="TAKER",
            gross_multiplier=Decimal("1.01"),
            gross_profit_pct=Decimal("1.0"),
            net_multiplier=Decimal("1.0015"),
            net_profit_pct=Decimal("0.15"),
            expected_profit_gbp=Decimal("0.045"),
            leg1_price=Decimal("0.10"),
            cross_price=Decimal("0.00005"),
            leg3_price=Decimal("2040.00"),
            timestamp=int(time.time()),
        )
        res = self.executor.execute_cycle(sub_hurdle_opp, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "below_hurdle")
        self.mock_client.place_order.assert_not_called()

    def test_cooldown_guardrail_prevents_rapid_fire_orders(self):
        """Enforces 60-second cooldown between execution attempts."""
        self.executor.last_execution_time = time.time() - 10.0  # 10s ago (< 60s)
        res = self.executor.execute_cycle(self.opp_forward, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "cooldown_active")
        self.mock_client.place_order.assert_not_called()

    def test_insufficient_cash_aborts_before_any_orders(self):
        """Checks available GBP balance; aborts if free cash is below trade capital."""
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("15.00")}  # £15 < £30
        res = self.executor.execute_cycle(self.opp_forward, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "insufficient_cash")
        self.mock_client.place_order.assert_not_called()

    def test_leg1_unfilled_clean_abort_zero_loss(self):
        """When Leg 1 IOC limit order receives zero fills, cycle aborts cleanly with zero crypto held."""
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        self.mock_client.place_order.return_value = "TX-LEG1"
        self.mock_client.query_order.return_value = {
            "status": "canceled",
            "vol_exec": Decimal("0.0"),
            "cost": Decimal("0.0"),
        }

        res = self.executor.execute_cycle(self.opp_forward, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "leg1_unfilled")
        # Only Leg 1 order placed, no subsequent legs
        self.assertEqual(self.mock_client.place_order.call_count, 1)

    def test_successful_forward_cycle_complete(self):
        """Full 3-leg cycle completes successfully and calculates net realized profit."""
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        self.mock_client.place_order.side_effect = ["TX-1", "TX-2", "TX-3"]
        self.mock_client.query_order.side_effect = [
            # Leg 1 fill: 300 ALGO for £30.00
            {"status": "closed", "vol_exec": Decimal("300.0"), "cost": Decimal("30.00"), "fee": Decimal("0.12")},
            # Leg 2 fill: Sold 300 ALGO -> Received 0.015 ETH
            {"status": "closed", "vol_exec": Decimal("300.0"), "cost": Decimal("0.015"), "fee": Decimal("0.00006")},
            # Leg 3 fill: Sold 0.015 ETH -> Received £30.24 GBP
            {"status": "closed", "vol_exec": Decimal("0.015"), "cost": Decimal("30.24"), "fee": Decimal("0.12")},
        ]

        res = self.executor.execute_cycle(self.opp_forward, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["starting_gbp"], "30.00")
        self.assertEqual(res["ending_gbp"], "30.24")
        self.assertEqual(res["net_profit_gbp"], "0.2400")
        self.assertEqual(res["net_profit_pct"], "0.800")
        self.assertEqual(self.mock_client.place_order.call_count, 3)

    def test_emergency_rescue_when_leg2_fails(self):
        """When Leg 1 fills but Leg 2 fails, emergency rescue immediately market-sells Leg 1 asset back to GBP."""
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        self.mock_client.place_order.side_effect = ["TX-1", "TX-2", "TX-RESCUE"]
        self.mock_client.query_order.side_effect = [
            # Leg 1 fill: 300 ALGO
            {"status": "closed", "vol_exec": Decimal("300.0"), "cost": Decimal("30.00")},
            # Leg 2 failure: 0 ETH received
            {"status": "canceled", "vol_exec": Decimal("0.0"), "cost": Decimal("0.0")},
            # Emergency Rescue query: Sold 300 ALGO back to GBP
            {"status": "closed", "vol_exec": Decimal("300.0"), "cost": Decimal("29.95")},
        ]

        res = self.executor.execute_cycle(self.opp_forward, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "emergency_rescued_leg1")
        self.assertEqual(self.mock_client.place_order.call_count, 3)
        # Verify emergency liquidation call was placed as market sell on ALGOGBP
        last_call = self.mock_client.place_order.call_args_list[2]
        self.assertEqual(last_call.kwargs["pair"], "ALGOGBP")
        self.assertEqual(last_call.kwargs["side"], "sell")
        self.assertEqual(last_call.kwargs["ordertype"], "market")

    def test_successful_reverse_cycle_complete(self):
        """Reverse cycle (GBP -> ETH -> ALGO -> GBP) executes and calculates realized profit."""
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        opp_reverse = ArbitrageOpportunity(
            route=self.route,
            direction="REVERSE",
            execution_type="TAKER",
            gross_multiplier=Decimal("1.02"),
            gross_profit_pct=Decimal("2.0"),
            net_multiplier=Decimal("1.01"),
            net_profit_pct=Decimal("1.00"),
            expected_profit_gbp=Decimal("0.30"),
            leg1_price=Decimal("0.10"),
            cross_price=Decimal("0.00005"),
            leg3_price=Decimal("2000.00"),
            timestamp=int(time.time()),
        )

        self.mock_client.place_order.side_effect = ["TX-R1", "TX-R2", "TX-R3"]
        self.mock_client.query_order.side_effect = [
            # Leg 1 fill on ETHGBP: Bought 0.015 ETH for £30.00
            {"status": "closed", "vol_exec": Decimal("0.015"), "cost": Decimal("30.00")},
            # Leg 2 fill on ALGOETH: Bought 300 ALGO with 0.015 ETH
            {"status": "closed", "vol_exec": Decimal("300.0"), "cost": Decimal("0.015")},
            # Leg 3 fill on ALGOGBP: Sold 300 ALGO for £30.30 GBP
            {"status": "closed", "vol_exec": Decimal("300.0"), "cost": Decimal("30.30")},
        ]

        res = self.executor.execute_cycle(opp_reverse, trade_capital_gbp=Decimal("30.00"), pair_map=self.pair_map)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["direction"], "REVERSE")
        self.assertEqual(res["net_profit_gbp"], "0.3000")
        self.assertEqual(res["net_profit_pct"], "1.000")
        self.assertEqual(self.mock_client.place_order.call_count, 3)


if __name__ == "__main__":
    unittest.main()
