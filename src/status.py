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


def _deser_dynamo(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            out[k] = int(v["N"]) if "." not in v["N"] else Decimal(v["N"])
        elif "M" in v:
            out[k] = _deser_dynamo(v["M"])
        elif "BOOL" in v:
            out[k] = v["BOOL"]
    return out


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
        parsed = _deser_dynamo(item)
        parsed.setdefault("state", "IDLE")
        parsed.setdefault("trades_completed", 0)
        parsed.setdefault("total_realized_profit_gbp", "0.0")
        return parsed
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

    if state.get("version") == "grid_v2":
        rungs = state.get("rungs", {})
        sorted_rids = sorted(rungs.keys(), key=lambda x: int(x) if x.isdigit() else 99)
        active_count = len(sorted_rids)
        print(f"  Strategy Mode:     {BOLD}{CYAN}{active_count}-RUNG DYNAMIC MICRO-GRID (Compounding Intraday Dips){RESET}")
        for rid in sorted_rids:
            rdata = rungs.get(rid, {})
            r_state = rdata.get("state", "IDLE")
            r_color = GREEN if "PLACED" in r_state else YELLOW
            r_order = rdata.get("active_order_id") or "None"
            r_buy = rdata.get("last_buy_price", "0.0")
            r_vol = rdata.get("last_buy_volume", "0.0")
            r_target = rdata.get("target_sell_price", "0.0")
            r_disc = rdata.get("discount_pct", "0.0")
            r_gain = rdata.get("target_pct", "0.0")

            if r_state == "SELL_PLACED":
                entry = Decimal(r_buy)
                target = Decimal(r_target)
                vol = Decimal(r_vol)
                gain_pct = ((target - entry) / entry * 100) if entry > 0 else Decimal(0)
                net_pending = ((target - entry) * vol) - ((entry * vol + target * vol) * Decimal("0.0025"))
                print(f"  • Rung {rid}:            {BOLD}{r_color}SELL_PLACED{RESET} (Holding {r_vol} {pair[:3]} @ £{entry:.2f})")
                print(f"    Target Sell:     £{target:.2f} | Pending Profit: {BOLD}{GREEN}+£{net_pending:.2f} (+{gain_pct:.2f}%){RESET}")
                print(f"    Active Order:    {r_order}")
            elif r_state == "BUY_PLACED":
                entry = Decimal(r_buy)
                disc = Decimal(r_disc)
                gain = Decimal(r_gain)
                print(f"  • Rung {rid}:            {BOLD}{r_color}BUY_PLACED{RESET} @ £{entry:.2f} (-{disc:.2f}% dip) | Target: +{gain:.2f}%")
                print(f"    Active Order:    {r_order}")
            else:
                if rdata.get("velocity_paused"):
                    roc = Decimal(str(rdata.get("velocity_roc_pct", "0.0")))
                    print(f"  • Rung {rid}:            {BOLD}{YELLOW}COOLING_DOWN{RESET} (Velocity {roc:.2f}%/3h > 1.80% threshold - waiting for market to stabilize)")
                else:
                    print(f"  • Rung {rid}:            {BOLD}{YELLOW}IDLE{RESET} (Queued to deploy buy order)")
    else:
        state_color = GREEN if "PLACED" in current_state else YELLOW
        print(f"  Strategy Mode:     {BOLD}LEGACY SINGLE-ORDER{RESET} (Auto-migrates to Grid upon Sell Fill)")
        print(f"  Current State:     {BOLD}{state_color}{current_state}{RESET}")

        if current_state == "SELL_PLACED":
            entry = Decimal(last_buy)
            target = Decimal(target_sell)
            vol = Decimal(last_vol)
            gain_pct = ((target - entry) / entry * 100) if entry > 0 else Decimal(0)
            net_pending = ((target - entry) * vol) - ((entry * vol + target * vol) * Decimal("0.0025"))
            print(f"  Cycle Progress:    {BOLD}Phase 2/2 (Holding {pair[:3]}, Limit Sell Active){RESET}")
            print(f"  Active Sell Order: {active_order}")
            print(f"  Entry Buy Price:   £{entry:.2f} ({last_vol} {pair[:3]})")
            print(f"  Target Sell Price: £{target:.2f}")
            print(f"  Pending Profit:    {BOLD}{GREEN}+£{net_pending:.2f} (+{gain_pct:.2f}%){RESET} (locks in on fill)")
        elif current_state == "BUY_PLACED":
            entry = Decimal(last_buy)
            print(f"  Cycle Progress:    {BOLD}Phase 1/2 (Holding Cash, Limit Buy Active){RESET}")
            print(f"  Active Buy Order:  {active_order}")
            print(f"  Target Buy Price:  £{entry:.2f} ({last_vol} {pair[:3]})")
        else:
            print(f"  Active Order ID:   {active_order}")

    print(f"  Completed Cycles:  {BOLD}{trades}{RESET}")
    profit_color = GREEN if profit >= 0 else "\033[91m"
    print(f"  Realized Profit:   {BOLD}{profit_color}+£{profit:.2f}{RESET}")

    # Fetch live 24h market volatility for transparency
    try:
        from kraken_client import KrakenClient
        from volatility import compute_volatility_target
        client = KrakenClient()
        candles = client.get_ohlc(pair, interval=60)
        target_pct, stats = compute_volatility_target(candles)
        low_p = Decimal(str(stats.get('low_24h', '0')))
        high_p = Decimal(str(stats.get('high_24h', '0')))
        print(f"  24h Volatility:    {BOLD}{stats.get('swing_24h_pct', 'N/A')}{RESET} (£{low_p:.2f} - £{high_p:.2f})")
        print(f"  Dynamic Target:    {BOLD}{GREEN}+{Decimal(str(target_pct)):.2f}%{RESET} (Range: 0.80% - 1.20%)")
    except Exception:
        pass

    return trades, profit


def display_status():
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{CYAN}============================================================{RESET}")
    print(f"               {BOLD}KRAKEN MICRO-TRADER STATUS{RESET}")
    print(f"{BOLD}{CYAN}============================================================{RESET}")
    print(f"  Active Strategy:   {BOLD}Serverless Multi-Rung Dynamic Micro-Grid (AWS Lambda £0.00/mo){RESET}")
    print(f"  Trading Pairs:     {BOLD}{', '.join(TRADING_PAIRS)}{RESET}")

    meta = get_portfolio_metadata()
    if meta:
        cash = meta["last_known_cash"]
        equity = meta["last_known_equity"]
        fee_buffer = max(Decimal("2.00"), equity * Decimal("0.03"))
        tradeable = max(Decimal("0.0"), equity - fee_buffer)
        per_pair = tradeable / Decimal(len(TRADING_PAIRS))
        print(f"  Total Portfolio:   {BOLD}£{equity:.2f}{RESET} (Free Cash: £{cash:.2f})")
        print(f"  Fee Buffer Held:   {BOLD}£{fee_buffer:.2f}{RESET} (Permanent 3% / min £2.00 Safety Reserve)")
        print(f"  Tradeable Pool:    {BOLD}£{tradeable:.2f}{RESET} (£{per_pair:.2f}/pair across {len(TRADING_PAIRS)} pairs)")

    # Ping-Pong / Micro-Grid Pair Cards
    total_all_trades = 0
    total_all_profit = Decimal("0.0")

    for pair in TRADING_PAIRS:
        trades, profit = display_pair_status(pair)
        total_all_trades += trades
        total_all_profit += profit

    # Summary
    if len(TRADING_PAIRS) > 1:
        print(f"\n{BOLD}{CYAN}============================================================{RESET}")
        print(f"               {BOLD}PORTFOLIO REALIZED TOTALS{RESET}")
        print(f"{BOLD}{CYAN}============================================================{RESET}")
        print(f"  Total Cycles:      {BOLD}{total_all_trades}{RESET}")
        profit_color = GREEN if total_all_profit >= 0 else "\033[91m"
        print(f"  Realized Profit:   {BOLD}{profit_color}+£{total_all_profit:.2f}{RESET}")

    print(f"{BOLD}{CYAN}============================================================{RESET}\n")


if __name__ == "__main__":
    display_status()
