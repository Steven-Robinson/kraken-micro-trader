"""Unit tests for StateStore DynamoDB serialization."""

from decimal import Decimal
import unittest

from state_store import _convert_floats_to_decimal


class TestStateStore(unittest.TestCase):
    def test_recursive_float_to_decimal_conversion(self):
        """Verify that nested floats at any depth are converted to Decimals."""
        raw_state = {
            "bot_id": "TEST-BOT",
            "cash_float": 12.34,
            "rungs": {
                "1": {
                    "price_float": 99.95,
                    "history": [1.1, 2.2, {"nested_rate": 3.3}],
                }
            },
            "clean_string": "hello",
            "clean_int": 42,
        }

        converted = _convert_floats_to_decimal(raw_state)

        self.assertIsInstance(converted["cash_float"], Decimal)
        self.assertEqual(converted["cash_float"], Decimal("12.34"))

        self.assertIsInstance(converted["rungs"]["1"]["price_float"], Decimal)
        self.assertEqual(converted["rungs"]["1"]["price_float"], Decimal("99.95"))

        self.assertIsInstance(converted["rungs"]["1"]["history"][0], Decimal)
        self.assertIsInstance(converted["rungs"]["1"]["history"][2]["nested_rate"], Decimal)
        self.assertEqual(converted["clean_string"], "hello")
        self.assertEqual(converted["clean_int"], 42)


if __name__ == "__main__":
    unittest.main()
