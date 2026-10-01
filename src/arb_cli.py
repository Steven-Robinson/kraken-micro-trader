"""Interactive CLI for Kraken Triangular Arbitrage Scanner & Monitor.

Usage:
    python3 src/arb_cli.py --scan               # Single snapshot scan of all live triangles
    python3 src/arb_cli.py --monitor            # Continuous live monitoring loop
    python3 src/arb_cli.py --all-pairs          # Scan all 78 triangles (not just priority assets)
    python3 src/arb_cli.py --capital 100        # Override simulated trade capital
"""

import argparse
from decimal import Decimal
import os
import sys
import time

# Ensure src/ is on python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kraken_client import KrakenClient
from arbitrage.scanner import ArbitrageScanner
from arbitrage.capital import calculate_trade_capital
from config import DYNAMODB_TABLE, AWS_REGION, DRY_RUN

# Color codes
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"


def get_live_portfolio_capital(client: KrakenClient) -> tuple[Decimal, Decimal, Decimal]:
    """Fetch live Kraken equity and calculate dynamic trade size."""
    try:
        balances = client.get_balance()
        current_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0.00")))
        tb = client.get_trade_balance(asset="ZGBP")
        current_equity = Decimal(str(tb.get("eb", current_cash)))
        trade_size = calculate_trade_capital(current_cash, current_equity)
        return current_cash, current_equity, trade_size
    except Exception:
        # Fallback for unauthenticated or test runs
        return Decimal("50.00"), Decimal("50.00"), Decimal("45.00")


def print_banner(current_cash: Decimal, current_equity: Decimal, trade_size: Decimal):
    print(f"\n{BOLD}{CYAN}============================================================{RESET}")
    print(f"       {BOLD}KRAKEN TRIANGULAR ARBITRAGE SCANNER{RESET}")
    print(f"{BOLD}{CYAN}============================================================{RESET}")
    print(f"  Account Equity:    {BOLD}£{current_equity:.2f}{RESET} (Cash: £{current_cash:.2f})")
    fee_buf = max(Decimal("2.00"), current_equity * Decimal("0.03"))
    print(f"  Fee Buffer Held:   {BOLD}£{fee_buf:.2f}{RESET} (Permanent Reserve)")
    print(f"  Scaled Trade Size: {BOLD}{GREEN}£{trade_size:.2f}{RESET} (Dynamically Scaled)")
    print(f"{BOLD}{CYAN}------------------------------------------------------------{RESET}")


def run_scan(scanner: ArbitrageScanner, trade_size: Decimal, priority_only: bool = True):
    print(f"{DIM}Scanning live Kraken order books...{RESET}")
    opps_maker = scanner.scan(trade_capital_gbp=trade_size, priority_only=priority_only, execution_type="MAKER")
    opps_taker = scanner.scan(trade_capital_gbp=trade_size, priority_only=priority_only, execution_type="TAKER")

    all_opps = opps_maker + opps_taker

    if not all_opps:
        print(f"  {YELLOW}No net-profitable opportunities found (> +0.10% net after fees).{RESET}")
        print(f"  {DIM}Order books are currently at tight synthetic equilibrium.{RESET}\n")
        return

    print(f"\n{BOLD}{GREEN}Found {len(all_opps)} Net-Profitable Opportunities:{RESET}")
    for opp in all_opps[:10]:
        color = GREEN if opp.net_profit_pct >= Decimal("0.30") else YELLOW
        direction_label = "FORWARD (GBP->A->B->GBP)" if opp.direction == "FORWARD" else "REVERSE (GBP->B->A->GBP)"
        print(f"\n  • {BOLD}{opp.route.asset_a} <-> {opp.route.asset_b}{RESET} ({opp.route.cross_pair}) [{opp.execution_type}]")
        print(f"    Cycle:       {direction_label}")
        print(f"    Gross Spread: {opp.gross_profit_pct:+.3f}% (Hurdle rate: 0.75% Maker / 1.20% Taker)")
        print(f"    Net Profit:  {BOLD}{color}{opp.net_profit_pct:+.3f}%{RESET} (Estimated: {BOLD}{GREEN}+£{opp.expected_profit_gbp:.4f}{RESET} on £{trade_size:.2f})")
        print(f"    Prices:      Leg1=£{opp.leg1_price} | Cross={opp.cross_price} | Leg3=£{opp.leg3_price}")

    print(f"\n{BOLD}{CYAN}============================================================{RESET}\n")


def run_monitor(scanner: ArbitrageScanner, client: KrakenClient, interval_sec: int = 5, priority_only: bool = True):
    print(f"{GREEN}Starting live monitor loop (Interval: {interval_sec}s). Press Ctrl+C to stop.{RESET}\n")
    iteration = 0
    try:
        while True:
            iteration += 1
            now_str = time.strftime("%H:%M:%S")
            current_cash, current_equity, trade_size = get_live_portfolio_capital(client)
            opps = scanner.scan(trade_capital_gbp=trade_size, priority_only=priority_only, execution_type="MAKER")
            
            if opps:
                top = opps[0]
                color = GREEN if top.net_profit_pct >= Decimal("0.30") else YELLOW
                print(
                    f"[{now_str}] #{iteration:04d} -> {BOLD}{GREEN}OPPORTUNITY!{RESET} "
                    f"{top.route.asset_a}-{top.route.asset_b} ({top.direction}) "
                    f"Net: {BOLD}{color}{top.net_profit_pct:+.3f}%{RESET} (+£{top.expected_profit_gbp:.4f} on £{trade_size:.2f})"
                )
            else:
                print(f"[{now_str}] #{iteration:04d} Scanning {len(scanner.routes)} routes... (Max net: < +0.10%)", end="\r")

            time.sleep(interval_sec)
    except KeyboardInterrupt:
        print("\nMonitor stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="Kraken Triangular Arbitrage Scanner")
    parser.add_argument("--scan", action="store_true", help="Run a single live snapshot scan")
    parser.add_argument("--monitor", action="store_true", help="Run continuous real-time monitor loop")
    parser.add_argument("--all-pairs", action="store_true", help="Scan all 78 triangles (not just priority assets)")
    parser.add_argument("--capital", type=float, help="Override trade capital in GBP")
    parser.add_argument("--interval", type=int, default=5, help="Monitor polling interval in seconds (default: 5)")
    args = parser.parse_args()

    client = KrakenClient()
    current_cash, current_equity, trade_size = get_live_portfolio_capital(client)

    if args.capital:
        trade_size = Decimal(str(args.capital))

    print_banner(current_cash, current_equity, trade_size)

    scanner = ArbitrageScanner(client)
    scanner.initialize_routes()

    priority_only = not args.all_pairs

    if args.monitor:
        run_monitor(scanner, client, interval_sec=args.interval, priority_only=priority_only)
    else:
        run_scan(scanner, trade_size, priority_only=priority_only)


if __name__ == "__main__":
    main()
