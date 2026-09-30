"""Quick status inspector for Kraken Micro-Trader.

Displays current state, active orders, prices, and realized profit across all active pairs.
Works with both local simulation state and deployed AWS DynamoDB.
"""

from decimal import Decimal
import json
import os
import subprocess
import sys

# Ensure src/ is on python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import TRADING_PAIRS, DYNAMODB_TABLE, AWS_REGION


def get_live_dynamo_state(bot_id: str):
    """Fetch DynamoDB state for a specific bot_id using AWS CLI."""
    try:
        cmd = [
            "aws", "dynamodb", "get-item",
            "--table-name", DYNAMODB_TABLE,
            "--key", json.dumps({"bot_id": {"S": bot_id}}),
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


def get_portfolio_metadata():
    """Fetch DynamoDB state for PORTFOLIO-METADATA."""
    try:
        cmd = [
            "aws", "dynamodb", "get-item",
            "--table-name", DYNAMODB_TABLE,
            "--key", json.dumps({"bot_id": {"S": "PORTFOLIO-METADATA"}}),
            "--region", AWS_REGION,
            "--output", "json"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        item = data.get("Item", {})
        if not item:
            return None
        return {
            "last_known_cash": Decimal(item.get("last_known_cash", {}).get("S", "0.0")),
            "last_known_equity": Decimal(item.get("last_known_equity", {}).get("S", "0.0")),
        }
    except Exception:
        return None



def display_pair_status(pair: str):
    """Display individual status card for a trading pair."""
    bot_id = f"{pair}-PINGPONG"
    state = get_live_dynamo_state(bot_id)
    if not state:
        from state_store import StateStore
        store = StateStore(table_name=DYNAMODB_TABLE, bot_id=bot_id, aws_region=AWS_REGION)
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

    print(f"\n{BOLD}{CYAN}------------------------------------------------------------{RESET}")
    print(f"  Trading Pair:      {BOLD}{pair}{RESET} ({bot_id})")

    state_color = GREEN if "PLACED" in current_state else YELLOW
    print(f"  Current State:     {BOLD}{state_color}{current_state}{RESET}")

    if current_state == "SELL_PLACED":
        entry = Decimal(last_buy)
        target = Decimal(target_sell)
        vol = Decimal(last_vol)
        gain_pct = ((target - entry) / entry * 100) if entry > 0 else Decimal(0)
        net_pending = ((target - entry) * vol) - ((entry * vol + target * vol) * Decimal("0.0025"))
        print(f"  Cycle Progress:    {BOLD}Phase 2/2 (Holding {pair[:3]}, Limit Sell Active){RESET}")
        print(f"  Active Sell Order: {active_order}")
        print(f"  Entry Buy Price:   £{last_buy} ({last_vol} {pair[:3]})")
        print(f"  Target Sell Price: £{target_sell}")
        print(f"  Pending Profit:    {BOLD}{GREEN}+£{net_pending:.2f} (+{gain_pct:.2f}%){RESET} (locks in on fill)")
    elif current_state == "BUY_PLACED":
        print(f"  Cycle Progress:    {BOLD}Phase 1/2 (Holding Cash, Limit Buy Active){RESET}")
        print(f"  Active Buy Order:  {active_order}")
        print(f"  Target Buy Price:  £{last_buy} ({last_vol} {pair[:3]})")
    else:
        print(f"  Active Order ID:   {active_order}")

    print(f"  Completed Cycles:  {BOLD}{trades}{RESET}")
    profit_color = GREEN if profit >= 0 else "\033[91m"
    print(f"  Realized Profit:   {BOLD}{profit_color}+£{profit:.4f}{RESET}")

    # Fetch live 24h market volatility for transparency
    try:
        from kraken_client import KrakenClient
        from volatility import compute_volatility_target
        client = KrakenClient()
        candles = client.get_ohlc(pair, interval=60)
        target_pct, stats = compute_volatility_target(candles)
        print(f"  24h Volatility:    {BOLD}{stats.get('swing_24h_pct', 'N/A')}{RESET} (£{stats.get('low_24h', '0')} - £{stats.get('high_24h', '0')})")
        print(f"  Dynamic Target:    {BOLD}{GREEN}+{target_pct}%{RESET} (Range: 0.8% - 3.0%)")
    except Exception:
        pass

    return trades, profit


def display_status():
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{CYAN}============================================================{RESET}")
    print(f"             {BOLD}KRAKEN MICRO-TRADER STATUS{RESET}")
    print(f"{BOLD}{CYAN}============================================================{RESET}")
    print(f"  Active Pairs:      {BOLD}{', '.join(TRADING_PAIRS)}{RESET}")

    meta = get_portfolio_metadata()
    if meta:
        cash = meta["last_known_cash"]
        equity = meta["last_known_equity"]
        fee_buffer = max(Decimal("2.00"), equity * Decimal("0.03"))
        tradeable = max(Decimal("0.0"), equity - fee_buffer)
        per_pair = tradeable / Decimal(len(TRADING_PAIRS))
        print(f"  Portfolio Equity:  {BOLD}£{equity:.2f}{RESET} (Cash: £{cash:.2f})")
        print(f"  Fee Buffer Held:   {BOLD}£{fee_buffer:.2f}{RESET} (Permanent Reserve)")
        print(f"  Tradeable Capital: {BOLD}£{tradeable:.2f}{RESET} (£{per_pair:.2f}/pair across {len(TRADING_PAIRS)} pairs)")

    total_all_trades = 0
    total_all_profit = Decimal("0.0")

    for pair in TRADING_PAIRS:
        trades, profit = display_pair_status(pair)
        total_all_trades += trades
        total_all_profit += profit

    if len(TRADING_PAIRS) > 1:
        print(f"\n{BOLD}{CYAN}============================================================{RESET}")
        print(f"             {BOLD}PORTFOLIO TOTALS (ALL PAIRS){RESET}")
        print(f"{BOLD}{CYAN}============================================================{RESET}")
        print(f"  Total Cycles:      {BOLD}{total_all_trades}{RESET}")
        profit_color = GREEN if total_all_profit >= 0 else "\033[91m"
        print(f"  Combined Profit:   {BOLD}{profit_color}+£{total_all_profit:.4f}{RESET}")

    print(f"{BOLD}{CYAN}============================================================{RESET}\n")


if __name__ == "__main__":
    display_status()
