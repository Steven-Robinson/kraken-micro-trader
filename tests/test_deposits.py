"""Unit tests for deposit detection and dynamic capital allocation."""

from decimal import Decimal
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure src/ is on python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from handler import check_for_new_deposits, resolve_pair_capital, sync_portfolio_metadata


class TestDepositDetection(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.dry_run = False

    @patch("handler.send_alert")
    @patch("handler.StateStore")
    def test_new_deposit_triggers_email_alert(self, mock_store_cls, mock_send_alert):
        """When cash and equity both increase by £50+, an SNS alert should be dispatched."""
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store

        # Previous baseline in DynamoDB: £1.81 cash, £49.56 equity
        mock_store.load_state.return_value = {
            "last_known_cash": "1.81",
            "last_known_equity": "49.56",
        }

        # User transferred £50 from bank account
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("51.81")}
        self.mock_client.get_trade_balance.return_value = {"eb": Decimal("99.56")}

        check_for_new_deposits(self.mock_client, "test_table", "eu-west-1", num_pairs=2)

        # Verify alert was sent with correct details
        self.assertTrue(mock_send_alert.called)
        subject, message = mock_send_alert.call_args[0]
        self.assertIn("New Deposit Detected!", subject)
        self.assertIn("+£50.00", subject)
        self.assertIn("Deposit Amount: +£50.00", message)
        self.assertIn("Total Available Cash: £51.81", message)
        self.assertIn("Total Portfolio Equity: £99.56", message)

        # Verify DynamoDB state was updated with new baseline
        saved_state = mock_store.save_state.call_args[0][0]
        self.assertEqual(saved_state["last_known_cash"], "51.81")
        self.assertEqual(saved_state["last_known_equity"], "99.56")

    @patch("handler.send_alert")
    @patch("handler.StateStore")
    def test_sell_fill_does_not_trigger_false_deposit_alert(self, mock_store_cls, mock_send_alert):
        """When a sell fills, cash jumps up by £48 but equity only gains £0.24 profit; must NOT trigger deposit alert."""
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store

        # Baseline: holding SOL
        mock_store.load_state.return_value = {
            "last_known_cash": "1.81",
            "last_known_equity": "49.56",
        }

        # Sell filled: cash increases by £48.24, equity only increases by £0.24
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("50.05")}
        self.mock_client.get_trade_balance.return_value = {"eb": Decimal("49.80")}

        check_for_new_deposits(self.mock_client, "test_table", "eu-west-1", num_pairs=2)

        # Verify NO false deposit alert was sent
        self.assertFalse(mock_send_alert.called)

    @patch("handler.send_alert")
    @patch("handler.StateStore")
    def test_market_price_swing_does_not_trigger_deposit_alert(self, mock_store_cls, mock_send_alert):
        """When crypto price surges, equity increases but cash stays constant; must NOT trigger deposit alert."""
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store

        mock_store.load_state.return_value = {
            "last_known_cash": "1.81",
            "last_known_equity": "49.56",
        }

        # SOL pumped: equity is £60.00, but cash is still £1.81
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("1.81")}
        self.mock_client.get_trade_balance.return_value = {"eb": Decimal("60.00")}

        check_for_new_deposits(self.mock_client, "test_table", "eu-west-1", num_pairs=2)

        self.assertFalse(mock_send_alert.called)

    def test_dynamic_capital_and_fee_buffer_split(self):
        """Verify dynamic capital splits tradeable pool across pairs and protects fee buffer."""
        # On £80 total equity (£0 ring-fenced Arb):
        # 3% fee buffer (£2.40) applied -> £77.60 tradeable pool
        self.mock_client.get_balance.return_value = {"ZGBP": Decimal("80.00")}
        self.mock_client.get_trade_balance.return_value = {"eb": Decimal("80.00")}

        # Single pair: (£80 - £2.40 fee) / 1 = £77.60
        cap_1 = resolve_pair_capital(self.mock_client, num_pairs=1, fallback_capital=Decimal("48.00"))
        self.assertEqual(cap_1, Decimal("77.60"))

        # Two pairs: (£80 - £2.40 fee) / 2 = £38.80
        cap_2 = resolve_pair_capital(self.mock_client, num_pairs=2, fallback_capital=Decimal("24.00"))
        self.assertEqual(cap_2, Decimal("38.80"))

        # Three pairs: (£80 - £2.40 fee) / 3 = £25.86
        cap_3 = resolve_pair_capital(self.mock_client, num_pairs=3, fallback_capital=Decimal("16.00"))
        self.assertEqual(cap_3, Decimal("25.86"))

        # On £200 total equity:
        # 3% fee buffer (£6.00) applied -> (£200 - £6.00) / 2 = £97.00 per pair
        self.mock_client.get_trade_balance.return_value = {"eb": Decimal("200.00")}
        cap_scaled = resolve_pair_capital(self.mock_client, num_pairs=2, fallback_capital=Decimal("24.00"))
        self.assertEqual(cap_scaled, Decimal("97.00"))


if __name__ == "__main__":
    unittest.main()
