"""Quick status inspector for Kraken Micro-Trader.

Displays current state, active orders, prices, and realized profit.
Works with both local simulation state and deployed AWS DynamoDB.
"""

from decimal import Decimal
import os
import sys

# Ensure src/ is on python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import BOT_ID, DYNAMODB_TABLE, AWS_REGION
import json
import subprocess


def get_live_dynamo_state():
    """Fetch DynamoDB state using AWS CLI (zero external python dependencies)."""
    try:
        cmd = [
            "aws", "dynamodb", "get-item",
            "--table-name", DYNAMODB_TABLE,
            "--key", json.dumps({"bot_id": {"S": BOT_ID}}),
            "--region", AWS_REGION,
            "--output", "json"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        item = data.get("Item", {})
        if not item:
            return None
        return {
            "state": item.get("state", {}).get("S", "IDLE"),
            "active_order_id": item.get("active_order_id", {}).get("S"),
            "last_buy_price": item.get("last_buy_price", {}).get("S", "0.0"),
            "last_buy_volume": item.get("last_buy_volume", {}).get("S", "0.0"),
            "target_sell_price": item.get("target_sell_price", {}).get("S", "0.0"),
            "trades_completed": int(item.get("trades_completed", {}).get("N", 0)),
            "total_realized_profit_gbp": item.get("total_realized_profit_gbp", {}).get("S", "0.0"),
        }
    except Exception:
        return None


def display_status():
    # Try AWS CLI query first
    state = get_live_dynamo_state()
    if not state:
        from state_store import StateStore
        store = StateStore(table_name=DYNAMODB_TABLE, bot_id=BOT_ID, aws_region=AWS_REGION)
        state = store.load_state()

    current_state = state.get("state", "IDLE")
    active_order = state.get("active_order_id") or "None"
    last_buy = state.get("last_buy_price", "0.0")
    last_vol = state.get("last_buy_volume", "0.0")
    target_sell = state.get("target_sell_price", "0.0")
    trades = state.get("trades_completed", 0)
    profit = Decimal(state.get("total_realized_profit_gbp", "0.0"))

    # Color codes
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{CYAN}============================================================{RESET}")
    print(f"             {BOLD}KRAKEN MICRO-TRADER STATUS{RESET}")
    print(f"{BOLD}{CYAN}============================================================{RESET}")
    print(f"  Bot ID:            {BOLD}{BOT_ID}{RESET}")
    
    state_color = GREEN if "PLACED" in current_state else YELLOW
    print(f"  Current State:     {BOLD}{state_color}{current_state}{RESET}")
    print(f"  Active Order ID:   {active_order}")
    print(f"  Last Entry Price:  £{last_buy}")
    print(f"  Holding Volume:    {last_vol}")
    print(f"  Target Sell Price: £{target_sell}")
    print(f"------------------------------------------------------------")
    print(f"  Trades Completed:  {BOLD}{trades}{RESET}")
    profit_color = GREEN if profit >= 0 else "\033[91m"
    print(f"  Realized Profit:   {BOLD}{profit_color}+£{profit:.4f}{RESET}")
    print(f"{BOLD}{CYAN}============================================================{RESET}\n")


if __name__ == "__main__":
    display_status()
