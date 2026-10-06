"""Unit tests for Multi-Rung Micro-Grid engine."""

from decimal import Decimal
import time
import unittest
from unittest.mock import MagicMock

from grid_engine import initialize_grid_state, process_grid_pair, DEFAULT_RUNGS_CONFIG, get_rungs_config


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
        # Rung 1 buy price: 2000 * (1 - 0.003) = 1994.00
        self.assertEqual(rungs["1"]["last_buy_price"], "1994.00")

        self.assertEqual(rungs["2"]["state"], "BUY_PLACED")
        self.assertEqual(rungs["2"]["active_order_id"], "TXID-BUY-R2")
        # Rung 2 buy price: 2000 * (1 - 0.009) = 1982.00
        self.assertEqual(rungs["2"]["last_buy_price"], "1982.00")

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
        # Target sell price markup: 1988 * (1 + (0.40% target + 0.5% fee)) = 1988 * 1.009 = 2005.89
        self.assertEqual(rungs["1"]["target_sell_price"], "2005.89")

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
            "order_placed_at": Decimal(str(int(time.time() - 1800))),
        }
        state["rungs"]["2"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R2",
            "discount_pct": "1.40",
            "target_pct": "1.20",
            "last_buy_price": "1972.00",
            "last_buy_volume": "0.0063",
            "target_sell_price": "0.0",
            "order_placed_at": Decimal(str(int(time.time() - 1800))),
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

    def test_auto_adopt_unmanaged_crypto(self):
        """Verify unmanaged/orphaned crypto in wallet is automatically adopted into a Maker Limit Sell."""
        state = initialize_grid_state("ETHGBP")
        # Both rungs start IDLE
        self.assertEqual(state["rungs"]["1"]["state"], "IDLE")

        self.mock_client.get_ticker.return_value = {
            "bid": Decimal("2050.00"),
            "ask": Decimal("2051.00"),
            "last": Decimal("2050.50"),
        }
        # Account holds 0.0147 ETH and £10 cash
        self.mock_client.get_balance.return_value = {
            "ZGBP": Decimal("10.00"),
            "XETH": Decimal("0.0147"),
        }
        self.mock_client.get_open_orders.return_value = {}
        self.mock_client.place_limit_order.return_value = "TXID-ADOPTED-SELL"

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

        # Rung 1 should have adopted the 0.0147 ETH into SELL_PLACED
        rungs = res["rungs"]
        self.assertEqual(rungs["1"]["state"], "SELL_PLACED")
        self.assertEqual(rungs["1"]["active_order_id"], "TXID-ADOPTED-SELL")
        self.assertEqual(rungs["1"]["last_buy_volume"], "0.0147")
        # Target sell price must be at least best ask
        self.assertGreaterEqual(Decimal(rungs["1"]["target_sell_price"]), Decimal("2051.00"))
        # An alert should have been sent
        self.assertTrue(any("Auto-Adopted" in sub for sub, _ in self.alerts))

    def test_rung_1_auto_cancels_on_upward_drift(self):
        """Verify Rung 1 buy order cancels when market drifts > 1.5% above it."""
        state = initialize_grid_state("ETHGBP")
        state["rungs"]["1"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R1",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "2000.00",
            "last_buy_volume": "0.0062",
            "target_sell_price": "0.0",
            "order_placed_at": Decimal(str(int(time.time() - 1800))),  # only 30m old
        }
        # Market has rallied to £2040.00 (+2.0% above £2000.00 limit buy > 1.5% limit)
        self.mock_client.get_ticker.return_value = {"bid": Decimal("2040.00"), "ask": Decimal("2041.00"), "last": Decimal("2040.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("20.00")}
        self.mock_client.query_order.return_value = {"status": "open"}
        self.mock_client.get_open_orders.return_value = {}

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

        # Order should have been cancelled due to drift
        self.mock_client.cancel_order.assert_called_with("TXID-BUY-R1")

    def test_undersized_order_auto_cancels_to_resize(self):
        """Verify undersized order (<60% target) cancels when cash is now available."""
        state = initialize_grid_state("ETHGBP")
        # Target per rung is £12.50 (50% of £25.00). An order with £2.00 is <60% target.
        state["rungs"]["1"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-UNDERSIZED",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "2000.00",
            "last_buy_volume": "0.0010",  # 2000 * 0.0010 = £2.00 (undersized!)
            "target_sell_price": "0.0",
            "order_placed_at": Decimal(str(int(time.time() - 600))),
        }
        self.mock_client.get_ticker.return_value = {"bid": Decimal("2005.00"), "ask": Decimal("2006.00"), "last": Decimal("2005.50")}
        # Lots of cash available now (£50.00)
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        self.mock_client.query_order.return_value = {"status": "open"}
        self.mock_client.get_open_orders.return_value = {}

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

        # Order should be cancelled to re-deploy at full size
        self.mock_client.cancel_order.assert_called_with("TXID-UNDERSIZED")

    def test_get_rungs_config_thresholds(self):
        """Verify dynamic rung configuration returns appropriate rungs based on capital."""
        # 1. Under £45 -> 2 rungs
        cfg_2 = get_rungs_config(Decimal("25.00"))
        self.assertEqual(len(cfg_2), 2)
        self.assertEqual(cfg_2[0]["weight"] + cfg_2[1]["weight"], Decimal("1.00"))

        # 2. £45 to £80 -> 3 rungs
        cfg_3 = get_rungs_config(Decimal("50.00"))
        self.assertEqual(len(cfg_3), 3)
        self.assertEqual(cfg_3[0]["rung_id"], "1")
        self.assertEqual(cfg_3[1]["rung_id"], "2")
        self.assertEqual(cfg_3[2]["rung_id"], "3")
        self.assertEqual(cfg_3[0]["weight"] + cfg_3[1]["weight"] + cfg_3[2]["weight"], Decimal("1.00"))
        # Rung 3 targets a deeper dip
        self.assertEqual(cfg_3[2]["discount_pct"], Decimal("1.50"))

        # 3. £80+ -> 4 rungs
        cfg_4 = get_rungs_config(Decimal("100.00"))
        self.assertEqual(len(cfg_4), 4)
        total_weight = sum(c["weight"] for c in cfg_4)
        self.assertEqual(total_weight, Decimal("1.00"))

    def test_dynamic_scaling_activates_rung_3_when_capital_reaches_50(self):
        """Verify that when pair capital increases to £50, Rung 3 is automatically created and placed."""
        state = initialize_grid_state("ETHGBP", pair_capital=Decimal("25.00"))
        # Initially only 2 rungs
        self.assertIn("1", state["rungs"])
        self.assertIn("2", state["rungs"])
        self.assertNotIn("3", state["rungs"])

        # Set Rung 1 & 2 as BUY_PLACED
        state["rungs"]["1"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R1",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "1988.00",
            "last_buy_volume": "0.0062",
            "order_placed_at": Decimal(str(int(time.time()))),
        }
        state["rungs"]["2"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-BUY-R2",
            "discount_pct": "1.40",
            "target_pct": "1.20",
            "last_buy_price": "1972.00",
            "last_buy_volume": "0.0063",
            "order_placed_at": Decimal(str(int(time.time()))),
        }

        # Market ticker
        self.mock_client.get_ticker.return_value = {"bid": Decimal("2000.00"), "ask": Decimal("2001.00"), "last": Decimal("2000.50")}
        # Lots of free cash available (£60.00)
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("60.00")}
        self.mock_client.query_order.return_value = {"status": "open"}
        self.mock_client.get_open_orders.return_value = {}
        self.mock_client.place_limit_order.return_value = "TXID-BUY-R3"

        # Process with expanded capital £50.00 (triggers 3 rungs)
        res = process_grid_pair(
            self.pair,
            self.mock_client,
            self.mock_store,
            state,
            self.pair_decimals,
            self.lot_decimals,
            self.ordermin,
            Decimal("50.00"),
            self.send_alert_fn,
        )

        # Rung 3 should now exist and be BUY_PLACED
        rungs = res["rungs"]
        self.assertIn("3", rungs)
        self.assertEqual(rungs["3"]["state"], "BUY_PLACED")
        self.assertEqual(rungs["3"]["active_order_id"], "TXID-BUY-R3")
        # Rung 3 discount is 1.50%: 2000 * (1 - 0.015) = 1970.00
        self.assertEqual(rungs["3"]["last_buy_price"], "1970.00")

    def test_dynamic_scaling_preserves_sell_order_when_capital_downscales(self):
        """Verify that if capital drops, a decommissioned rung in SELL_PLACED is preserved until fill."""
        state = initialize_grid_state("ETHGBP", pair_capital=Decimal("50.00"))
        state["rungs"]["3"] = {
            "state": "SELL_PLACED",
            "active_order_id": "TXID-SELL-R3",
            "discount_pct": "2.80",
            "target_pct": "1.80",
            "last_buy_price": "1944.00",
            "last_buy_volume": "0.0064",
            "target_sell_price": "1984.82",
            "order_placed_at": Decimal(str(int(time.time()))),
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("1990.00"), "ask": Decimal("1991.00"), "last": Decimal("1990.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("20.00")}
        # Rung 3 sell fills!
        self.mock_client.query_order.return_value = {
            "status": "closed",
            "price": Decimal("1984.82"),
            "vol_exec": Decimal("0.0064"),
        }

        # Run with down-scaled capital £25.00 (which only defines rungs 1 and 2)
        res = process_grid_pair(
            self.pair,
            self.mock_client,
            self.mock_store,
            state,
            self.pair_decimals,
            self.lot_decimals,
            self.ordermin,
            Decimal("25.00"),
            self.send_alert_fn,
        )

        # Rung 3 should have banked its profit and reset to IDLE!
        self.assertEqual(res["rungs"]["3"]["state"], "IDLE")
        self.assertEqual(res["trades_completed"], 1)
        self.assertGreater(Decimal(res["total_profit_gbp"]), Decimal("0.10"))

    def test_velocity_guard_pauses_buy_placement_on_idle_rung(self):
        """Verify Velocity Guard pauses placing new buy orders during a rapid price surge."""
        state = initialize_grid_state("ETHGBP")
        self.assertEqual(state["rungs"]["1"]["state"], "IDLE")

        self.mock_client.get_ticker.return_value = {"bid": Decimal("2060.00"), "ask": Decimal("2061.00"), "last": Decimal("2060.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        self.mock_client.get_open_orders.return_value = {}

        # 1. Rapid rally candles: 2000 -> 2060 (+3.0% > 1.8% threshold)
        surge_candles = [
            [1700000000, "2000.0", "2020.0", "1995.0", "2015.0", "2010.5", "10", 5],
            [1700003600, "2015.0", "2040.0", "2010.0", "2038.0", "2025.5", "10", 5],
            [1700007200, "2038.0", "2065.0", "2035.0", "2060.0", "2050.0", "10", 5],
        ]
        self.mock_client.get_ohlc.return_value = surge_candles

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

        # Velocity Guard must be active; orders must NOT be placed
        self.mock_client.place_limit_order.assert_not_called()
        self.assertTrue(res["rungs"]["1"].get("velocity_paused"))
        self.assertEqual(res["rungs"]["1"]["state"], "IDLE")

        # 2. Market stabilizes: recent candles flatten (2058 -> 2060, +0.1%)
        calm_candles = [
            [1700000000, "2058.0", "2062.0", "2055.0", "2059.0", "2058.5", "10", 5],
            [1700003600, "2059.0", "2063.0", "2056.0", "2060.0", "2059.5", "10", 5],
            [1700007200, "2060.0", "2064.0", "2058.0", "2060.0", "2060.0", "10", 5],
        ]
        self.mock_client.get_ohlc.return_value = calm_candles
        self.mock_client.place_limit_order.side_effect = ["TXID-BUY-R1", "TXID-BUY-R2"]

        res2 = process_grid_pair(
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

        # Now that velocity slowed down, buy orders are placed!
        self.assertEqual(res2["rungs"]["1"]["state"], "BUY_PLACED")
        self.assertEqual(res2["rungs"]["1"]["active_order_id"], "TXID-BUY-R1")
        self.assertFalse(res2["rungs"]["1"].get("velocity_paused"))

    def test_partial_fill_on_buy_cancellation_transitions_to_sell(self):
        """Verify that when a buy order is cancelled (e.g. drift or timeout) with a partial fill, it immediately creates a limit sell."""
        state = initialize_grid_state("ETHGBP")
        state["rungs"]["1"] = {
            "state": "BUY_PLACED",
            "active_order_id": "TXID-PARTIAL-BUY",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "2000.00",
            "last_buy_volume": "0.0100",
            "target_sell_price": "0.0",
            "order_placed_at": Decimal(str(int(time.time() - 36000))),  # 10h old (>4h stale!)
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("2005.00"), "ask": Decimal("2006.00"), "last": Decimal("2005.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("20.00")}
        # Query order returns status open with vol_exec = 0.0040 (partial fill!)
        self.mock_client.query_order.return_value = {
            "status": "open",
            "price": Decimal("2000.00"),
            "vol_exec": Decimal("0.0040"),
            "cost": Decimal("8.00"),
        }
        self.mock_client.place_limit_order.return_value = "TXID-PARTIAL-SELL"

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

        # The partial fill must be adopted into SELL_PLACED immediately!
        self.mock_client.cancel_order.assert_called_with("TXID-PARTIAL-BUY")
        self.assertEqual(res["rungs"]["1"]["state"], "SELL_PLACED")
        self.assertEqual(res["rungs"]["1"]["active_order_id"], "TXID-PARTIAL-SELL")
        self.assertEqual(res["rungs"]["1"]["last_buy_volume"], "0.0040")

    def test_external_sell_cancel_resets_to_idle(self):
        """Verify that when a sell order is cancelled externally, the rung resets to IDLE for auto-adoption."""
        state = initialize_grid_state("ETHGBP")
        state["rungs"]["1"] = {
            "state": "SELL_PLACED",
            "active_order_id": "TXID-EXT-CANCEL",
            "discount_pct": "0.60",
            "target_pct": "1.00",
            "last_buy_price": "2000.00",
            "last_buy_volume": "0.0050",
            "target_sell_price": "2030.00",
            "order_placed_at": Decimal(str(int(time.time() - 3600))),
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("2020.00"), "ask": Decimal("2021.00"), "last": Decimal("2020.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("20.00")}
        self.mock_client.query_order.return_value = {"status": "canceled"}

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

        # Rung should reset to IDLE so held crypto can be adopted
        self.assertEqual(res["rungs"]["1"]["state"], "IDLE")
        self.assertIsNone(res["rungs"]["1"]["active_order_id"])

    def test_post_fill_cooldown_prevents_immediate_rebuy(self):
        """Verify that a rung with an active post-fill cooldown does not immediately place a buy order."""
        state = initialize_grid_state("ETHGBP")
        # Rung 1 has an active cooldown for another 300 seconds
        state["rungs"]["1"]["state"] = "IDLE"
        state["rungs"]["1"]["cooldown_until"] = int(time.time()) + 300

        self.mock_client.get_ticker.return_value = {"bid": Decimal("2000.00"), "ask": Decimal("2001.00"), "last": Decimal("2000.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.00")}
        self.mock_client.place_limit_order.return_value = "TXID-BUY-R2"

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

        # Rung 1 should remain in IDLE (cooling down), only Rung 2 should have placed an order
        self.assertEqual(res["rungs"]["1"]["state"], "IDLE")
        self.assertIsNone(res["rungs"]["1"].get("active_order_id"))
        self.assertEqual(res["rungs"]["2"]["state"], "BUY_PLACED")

    def test_open_sell_reprices_to_new_tight_margin(self):
        """Verify that an open sell order placed with old wide markup re-prices closer to market."""
        state = initialize_grid_state("ETHGBP")
        # Open sell order with old wide target (e.g. £2030.00 when cost was £2000.00)
        state["rungs"]["1"] = {
            "state": "SELL_PLACED",
            "active_order_id": "TXID-OLD-SELL",
            "discount_pct": "0.30",
            "target_pct": "0.40",
            "last_buy_price": "2000.00",
            "last_buy_volume": "0.0062",
            "target_sell_price": "2030.00",  # old wide target
            "order_placed_at": Decimal(str(int(time.time() - 3600))),
        }

        self.mock_client.get_ticker.return_value = {"bid": Decimal("2005.00"), "ask": Decimal("2006.00"), "last": Decimal("2005.50")}
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("20.00")}
        self.mock_client.query_order.return_value = {"status": "open"}
        self.mock_client.place_limit_order.return_value = "TXID-NEW-REPRICED-SELL"

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

        # Old sell order should be cancelled and new re-priced sell order placed at +0.90% (£2018.00)
        self.mock_client.cancel_order.assert_called_with("TXID-OLD-SELL")
        self.assertEqual(res["rungs"]["1"]["active_order_id"], "TXID-NEW-REPRICED-SELL")
        # 2000 * (1 + (0.40% + 0.50% fee)) = 2000 * 1.009 = 2018.00
        self.assertEqual(res["rungs"]["1"]["target_sell_price"], "2018.00")


if __name__ == "__main__":
    unittest.main()
