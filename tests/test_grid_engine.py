"""Unit tests for Multi-Rung Micro-Grid engine."""

from decimal import Decimal
import time
import unittest
from unittest.mock import MagicMock

from grid_engine import initialize_grid_state, process_grid_pair, DEFAULT_RUNGS_CONFIG


class TestGridEngine(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_store = MagicMock()
        self.pair = "ETHGBP"
        self.pair_decimals = 2
        self.lot_decimals = 4
        self.ordermin = Decimal("0.001")
        self.total_capital = Decimal("25.00")
        self.alerts = []

        def mock_alert(sub, msg):
            self.alerts.append((sub, msg))

        self.send_alert_fn = mock_alert

    def test_initialize_grid_state(self):
        state = initialize_grid_state("ETHGBP")
        self.assertEqual(state["version"], "grid_v2")
        self.assertIn("1", state["rungs"])
        self.assertIn("2", state["rungs"])
        self.assertEqual(state["rungs"]["1"]["state"], "IDLE")
        self.assertEqual(state["rungs"]["2"]["state"], "IDLE")

    def test_process_grid_places_staggered_buys_when_idle(self):
        state = initialize_grid_state("ETHGBP")

        # Market ticker
        self.mock_client.get_ticker.return_value = {
            "bid": Decimal("2000.00"),
            "ask": Decimal("2001.00"),
            "last": Decimal("2000.50"),
        }
        # Free cash = £30.00 (well above £25.00 total capital + £2 buffer)
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("30.00")}

        # Place orders mock
        self.mock_client.place_limit_order.side_effect = ["TXID-BUY-R1", "TXID-BUY-R2"]

        res = process_grid_pair(
            self.pair,
            self.mock_client,
            self.mock_store,
            state,
            self.pair_decimals,
            self.lot_decimals,
            self.ordermin,
            self.total_capital,
            self.send_alert_fn,
        )

        # Both rungs should now be BUY_PLACED
        rungs = res["rungs"]
        self.assertEqual(rungs["1"]["state"], "BUY_PLACED")
        self.assertEqual(rungs["1"]["active_order_id"], "TXID-BUY-R1")
        # Rung 1 buy price: 2000 * (1 - 0.006) = 1988.00
        self.assertEqual(rungs["1"]["last_buy_price"], "1988.00")

        self.assertEqual(rungs["2"]["state"], "BUY_PLACED")
        self.assertEqual(rungs["2"]["active_order_id"], "TXID-BUY-R2")
        # Rung 2 buy price: 2000 * (1 - 0.014) = 1972.00
        self.assertEqual(rungs["2"]["last_buy_price"], "1972.00")

        self.assertTrue(self.mock_store.save_state.called)

    def test_process_grid_transitions_buy_to_sell_on_fill(self):
        state = initialize_grid_state("ETHGBP")
        state["rungs"]["1"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R1",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "1988.00",
            "last_buy_volume": "0.0062",
            "target_sell_price": "0.0",
            "order_placed_at": int(time.time()),
        }
        # Rung 2 still waiting
        state["rungs"]["2"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R2",
            "discount_pct": "1.40",
            "target_pct": "1.20",
            "last_buy_price": "1972.00",
            "last_buy_volume": "0.0063",
            "target_sell_price": "0.0",
            "order_placed_at": int(time.time()),
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("1995.00"), "ask": Decimal("1996.00"), "last": Decimal("1995.00")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("15.00")}

        # Rung 1 is closed (filled), Rung 2 is open
        def mock_query(txid):
            if txid == "TXID-BUY-R1":
                return {"status": "closed", "price": Decimal("1988.00"), "vol_exec": Decimal("0.0062")}
            return {"status": "open"}

        self.mock_client.query_order.side_effect = mock_query
        self.mock_client.place_limit_order.return_value = "TXID-SELL-R1"

        res = process_grid_pair(
            self.pair,
            self.mock_client,
            self.mock_store,
            state,
            self.pair_decimals,
            self.lot_decimals,
            self.ordermin,
            self.total_capital,
            self.send_alert_fn,
        )

        rungs = res["rungs"]
        # Rung 1 should now be SELL_PLACED
        self.assertEqual(rungs["1"]["state"], "SELL_PLACED")
        self.assertEqual(rungs["1"]["active_order_id"], "TXID-SELL-R1")
        # Target sell price markup: 1988 * (1 + (1.0% target + 0.5% fee)) = 1988 * 1.015 = 2017.82
        self.assertEqual(rungs["1"]["target_sell_price"], "2017.82")

        # Rung 2 should remain BUY_PLACED
        self.assertEqual(rungs["2"]["state"], "BUY_PLACED")

        # Verify alert sent for Rung 1 fill
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Rung 1 Buy Filled", self.alerts[0][0])

    def test_process_grid_banks_profit_and_resets_rung(self):
        state = initialize_grid_state("ETHGBP")
        state["rungs"]["1"] = {
            "state": "SELL_PLACED",
            "active_order_id": "TXID-SELL-R1",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "1988.00",
            "last_buy_volume": "0.0062",
            "target_sell_price": "2017.82",
            "order_placed_at": int(time.time()),
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("2020.00"), "ask": Decimal("2021.00"), "last": Decimal("2020.00")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("25.00")}

        # Sell order filled!
        self.mock_client.query_order.return_value = {
            "status": "closed",
            "price": Decimal("2017.82"),
            "vol_exec": Decimal("0.0062"),
        }

        res = process_grid_pair(
            self.pair,
            self.mock_client,
            self.mock_store,
            state,
            self.pair_decimals,
            self.lot_decimals,
            self.ordermin,
            self.total_capital,
            self.send_alert_fn,
        )

        # Rung 1 should reset to IDLE, trades_completed should increment, profit banked
        rungs = res["rungs"]
        self.assertEqual(rungs["1"]["state"], "IDLE")
        self.assertEqual(res["trades_completed"], 1)
        profit = Decimal(res["total_profit_gbp"])
        self.assertGreater(profit, Decimal("0.05"))

        # Verify alert sent
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Profit Banked", self.alerts[0][0])

    def test_process_grid_handles_decimal_order_placed_at_from_dynamodb(self):
        """Ensure DynamoDB Decimals for order_placed_at do not raise TypeError on float subtraction."""
        state = initialize_grid_state("ETHGBP")
        state["rungs"]["1"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R1",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "1988.00",
            "last_buy_volume": "0.0062",
            "target_sell_price": "0.0",
            "order_placed_at": Decimal("1790865385"),
        }
        state["rungs"]["2"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R2",
            "discount_pct": "1.40",
            "target_pct": "1.20",
            "last_buy_price": "1972.00",
            "last_buy_volume": "0.0063",
            "target_sell_price": "0.0",
            "order_placed_at": Decimal("1790865385"),
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("1995.00"), "ask": Decimal("1996.00"), "last": Decimal("1995.00")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("15.00")}
        self.mock_client.query_order.return_value = {"status": "open"}

        # Must not raise TypeError: unsupported operand type(s) for -: 'float' and 'decimal.Decimal'
        res = process_grid_pair(
            self.pair,
            self.mock_client,
            self.mock_store,
            state,
            self.pair_decimals,
            self.lot_decimals,
            self.ordermin,
            self.total_capital,
            self.send_alert_fn,
        )
        self.assertEqual(res["rungs"]["1"]["state"], "BUY_PLACED")
        self.assertEqual(res["rungs"]["2"]["state"], "BUY_PLACED")


if __name__ == "__main__":
    unittest.main()
