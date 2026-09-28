"""State management layer for Kraken Micro-Trader.

Stores state in Amazon DynamoDB when running on AWS Lambda.
Falls back to a local JSON file (`local_state.json`) when running in local dev/test mode.
"""

from decimal import Decimal
import json
import logging
import os
import time
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    ClientError = Exception

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

LOCAL_STATE_FILE = "local_state.json"


class StateStore:
    """Interface for saving and loading bot state."""

    def __init__(self, table_name: str, bot_id: str, aws_region: str = "eu-west-2"):
        self.table_name = table_name
        self.bot_id = bot_id
        self.aws_region = aws_region
        self.use_dynamodb = (boto3 is not None) and (os.getenv("AWS_LAMBDA_FUNCTION_NAME") is not None)

        if self.use_dynamodb:
            dynamodb = boto3.resource("dynamodb", region_name=self.aws_region)
            self.table = dynamodb.Table(self.table_name)
        else:
            logger.info("Running locally: using %s for state persistence.", LOCAL_STATE_FILE)

    def load_state(self) -> dict[str, Any]:
        """Load current bot state."""
        default_state: dict[str, Any] = {
            "bot_id": self.bot_id,
            "state": "IDLE",  # IDLE, BUY_PLACED, SELL_PLACED, PAUSED
            "active_order_id": None,
            "last_buy_price": "0.0",
            "last_buy_volume": "0.0",
            "target_sell_price": "0.0",
            "target_buy_price": "0.0",
            "trades_completed": 0,
            "total_realized_profit_gbp": "0.0",
            "order_placed_at": 0,
            "updated_at": int(time.time()),
        }

        if self.use_dynamodb:
            try:
                response = self.table.get_item(Key={"bot_id": self.bot_id})
                item = response.get("Item")
                if item:
                    # Convert DynamoDB Decimals to strings or python types
                    return {**default_state, **item}
                return default_state
            except ClientError as e:
                logger.error("Failed to read from DynamoDB: %s", e)
                return default_state

        # Local JSON fallback
        if os.path.exists(LOCAL_STATE_FILE):
            try:
                with open(LOCAL_STATE_FILE, "r", encoding="utf-8") as f:
                    return {**default_state, **json.load(f)}
            except Exception as e:
                logger.warning("Could not read %s, using defaults: %s", LOCAL_STATE_FILE, e)

        return default_state

    def save_state(self, state: dict[str, Any]) -> None:
        """Save updated bot state."""
        state["updated_at"] = int(time.time())
        state["bot_id"] = self.bot_id

        if self.use_dynamodb:
            try:
                # DynamoDB item serialization
                item = {}
                for k, v in state.items():
                    if isinstance(v, float):
                        item[k] = Decimal(str(v))
                    else:
                        item[k] = v
                self.table.put_item(Item=item)
            except ClientError as e:
                logger.error("Failed to save state to DynamoDB: %s", e)
                raise
            return

        # Local file write
        try:
            with open(LOCAL_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            logger.error("Failed to save local state: %s", e)
