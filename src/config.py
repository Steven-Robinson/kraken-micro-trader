"""Configuration and environment parameter loader for Kraken Micro-Trader.

Supports loading credentials directly from environment variables (local dev)
or fetching them dynamically from AWS Systems Manager Parameter Store.
"""

from decimal import Decimal
import os

try:
    import boto3
except ImportError:
    boto3 = None


# --- General Trading Parameters ---
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
TRADING_PAIR = os.getenv("TRADING_PAIR", "SOLGBP")
BOT_ID = os.getenv("BOT_ID", f"{TRADING_PAIR}-PINGPONG")

# Capital to allocate per cycle in GBP (keep slightly below total balance for fee safety)
ALLOCATED_CAPITAL_GBP = Decimal(os.getenv("ALLOCATED_CAPITAL_GBP", "48.00"))

# Target gross profit percentage per round trip (e.g. 2.0%)
PROFIT_TARGET_PERCENT = Decimal(os.getenv("PROFIT_TARGET_PERCENT", "2.0"))

# Discount below current market price for limit buy orders (0.1% - 0.3% ensures Maker status)
BUY_DISCOUNT_PERCENT = Decimal(os.getenv("BUY_DISCOUNT_PERCENT", "0.2"))

# Hours before an unfilled limit buy order is considered stale and re-evaluated
MAX_STALE_HOURS = Decimal(os.getenv("MAX_STALE_HOURS", "24"))

# Estimated round-trip Maker fee (Kraken Pro: 0.25% buy + 0.25% sell = 0.50%)
ROUND_TRIP_FEE_PERCENT = Decimal(os.getenv("ROUND_TRIP_FEE_PERCENT", "0.50"))

# --- AWS Configuration ---
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")  # Matching user's primary region
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "kraken_bot_state")
SSM_PARAM_API_KEY = os.getenv("SSM_PARAM_API_KEY", "/crypto-bot/kraken-api-key")
SSM_PARAM_API_SECRET = os.getenv("SSM_PARAM_API_SECRET", "/crypto-bot/kraken-api-secret")


def get_kraken_credentials() -> tuple[str, str]:
    """Retrieve Kraken API key and secret.

    Checks environment variables first (KRAKEN_API_KEY, KRAKEN_API_SECRET),
    then falls back to AWS SSM Parameter Store.
    """
    env_key = os.getenv("KRAKEN_API_KEY")
    env_secret = os.getenv("KRAKEN_API_SECRET")

    if env_key and env_secret:
        return env_key, env_secret

    if DRY_RUN:
        return "mock_api_key", "mock_api_secret"

    if boto3 is None:
        raise RuntimeError("boto3 is required to fetch credentials from AWS SSM.")

    # Fetch from AWS SSM Parameter Store
    ssm = boto3.client("ssm", region_name=AWS_REGION)
    key_resp = ssm.get_parameter(Name=SSM_PARAM_API_KEY, WithDecryption=True)
    secret_resp = ssm.get_parameter(Name=SSM_PARAM_API_SECRET, WithDecryption=True)

    return key_resp["Parameter"]["Value"], secret_resp["Parameter"]["Value"]
