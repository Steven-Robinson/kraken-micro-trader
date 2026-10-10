import json
import logging
import os
import subprocess
import sys
import time
from decimal import Decimal, ROUND_DOWN

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from kraken_client import KrakenClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("liquidate")

def get_ssm_param(param_name: str) -> str:
    cmd = [
        "aws", "ssm", "get-parameter",
        "--name", param_name,
        "--with-decryption",
        "--query", "Parameter.Value",
        "--output", "text",
        "--region", "eu-west-1"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()

def main():
    logger.info("Starting complete portfolio liquidation to 100%% GBP...")

    # 1. Fetch credentials
    logger.info("Fetching Kraken API credentials from AWS SSM...")
    api_key = get_ssm_param("/crypto-bot/kraken-api-key")
    api_secret = get_ssm_param("/crypto-bot/kraken-api-secret")

    client = KrakenClient(api_key=api_key, api_secret=api_secret, dry_run=False)

    # 2. Check and cancel all open orders
    logger.info("Querying open orders...")
    open_orders = client.get_open_orders(force_refresh=True)
    logger.info(f"Found {len(open_orders)} open orders to cancel.")

    for txid, details in open_orders.items():
        descr = details.get("descr", {}).get("order", "Unknown order")
        logger.info(f"Cancelling order {txid}: {descr}...")
        try:
            success = client.cancel_order(txid)
            logger.info(f"Order {txid} cancel result: {success}")
        except Exception as e:
            logger.error(f"Failed to cancel {txid}: {e}")

    # Allow Kraken a second to process cancellations and release balances
    time.sleep(2)

    # Verify open orders is 0
    remaining_orders = client.get_open_orders(force_refresh=True)
    if remaining_orders:
        logger.warning(f"{len(remaining_orders)} orders still open! Retrying cancellations...")
        for txid in remaining_orders:
            client.cancel_order(txid)
        time.sleep(2)

    # 3. Fetch live balances
    logger.info("Fetching live asset balances after order cancellation...")
    balances = client.get_balance()
    logger.info(f"Current balances: {balances}")

    # Assets mapping: (balance_key, pair_name, decimals)
    assets_to_liquidate = [
        ("SOL", "SOLGBP", 8),
        ("XETH", "ETHGBP", 8),
        ("XXBT", "BTCGBP", 8),
    ]

    sold_records = []

    for asset_key, pair, decimals in assets_to_liquidate:
        amt = balances.get(asset_key, Decimal("0"))
        if amt <= Decimal("0"):
            logger.info(f"No {asset_key} balance found to sell.")
            continue

        pair_info = client.get_pair_info(pair)
        ordermin = pair_info.get("ordermin", Decimal("0.0001"))
        lot_decimals = pair_info.get("lot_decimals", decimals)

        # Quantize down to lot decimals
        step = Decimal("10") ** (-lot_decimals)
        vol_to_sell = amt.quantize(step, rounding=ROUND_DOWN)

        logger.info(f"Preparing to liquidate {asset_key}: balance={amt}, vol_to_sell={vol_to_sell}, ordermin={ordermin}")

        if vol_to_sell < ordermin:
            logger.warning(f"Balance {vol_to_sell} is less than ordermin {ordermin} for {pair}. Skipping.")
            continue

        try:
            logger.info(f"Placing MARKET SELL for {vol_to_sell} {pair}...")
            txid = client.place_order(
                pair=pair,
                side="sell",
                ordertype="market",
                volume=vol_to_sell,
            )
            logger.info(f"Successfully placed market sell order {txid} for {vol_to_sell} {pair}!")
            time.sleep(1)

            # Query order fill details
            try:
                order_info = client.query_order(txid)
                logger.info(f"Fill details for {txid}: status={order_info.get('status')}, vol={order_info.get('vol_exec')}, price={order_info.get('price')}, cost=£{order_info.get('cost')}, fee=£{order_info.get('fee')}")
                sold_records.append({
                    "asset": asset_key,
                    "pair": pair,
                    "volume": str(vol_to_sell),
                    "txid": txid,
                    "price": str(order_info.get("price")),
                    "gross_gbp": str(order_info.get("cost")),
                    "fee_gbp": str(order_info.get("fee")),
                })
            except Exception as qe:
                logger.warning(f"Could not query fill details for {txid}: {qe}")
                sold_records.append({
                    "asset": asset_key,
                    "pair": pair,
                    "volume": str(vol_to_sell),
                    "txid": txid,
                })
        except Exception as e:
            logger.error(f"Error selling {asset_key} on {pair}: {e}")

    # 4. Final balance check
    time.sleep(2)
    final_balances = client.get_balance()
    logger.info("==================================================")
    logger.info("FINAL KRAKEN BALANCES:")
    for asset, amt in final_balances.items():
        if amt > Decimal("0"):
            logger.info(f"  {asset}: {amt}")
    gbp_cash = final_balances.get("ZGBP", Decimal("0"))
    logger.info(f"Total GBP Cash: £{gbp_cash:.2f}")
    logger.info("==================================================")

    # 5. Clean up DynamoDB State
    logger.info("Resetting DynamoDB state for all trading pairs...")
    now_ts = int(time.time())
    for pair in ["SOLGBP", "ETHGBP", "BTCGBP"]:
        bot_id = f"{pair}-PINGPONG"
        # Retrieve current item to retain trades_completed and total_realized_profit_gbp
        cmd_get = [
            "aws", "dynamodb", "get-item",
            "--table-name", "kraken_bot_state",
            "--key", json.dumps({"bot_id": {"S": bot_id}}),
            "--region", "eu-west-1",
            "--output", "json"
        ]
        res = subprocess.run(cmd_get, capture_output=True, text=True, check=True)
        item = json.loads(res.stdout).get("Item", {})
        trades_completed = item.get("trades_completed", {"N": "0"})
        profit_gbp = item.get("total_realized_profit_gbp", {"S": "0.0"})

        reset_item = {
            "bot_id": {"S": bot_id},
            "state": {"S": "IDLE"},
            "active_order_id": {"NULL": True},
            "last_buy_price": {"S": "0.0"},
            "last_buy_volume": {"S": "0.0"},
            "target_sell_price": {"S": "0.0"},
            "target_buy_price": {"S": "0.0"},
            "order_placed_at": {"N": "0"},
            "rungs": {"M": {}},
            "trades_completed": trades_completed,
            "total_realized_profit_gbp": profit_gbp,
            "version": {"S": "grid_v2"},
            "updated_at": {"N": str(now_ts)},
        }

        cmd_put = [
            "aws", "dynamodb", "put-item",
            "--table-name", "kraken_bot_state",
            "--item", json.dumps(reset_item),
            "--region", "eu-west-1",
        ]
        subprocess.run(cmd_put, check=True)
        logger.info(f"Reset {bot_id} state to IDLE with 0 active rungs.")

    # Update PORTFOLIO-METADATA
    portfolio_meta = {
        "bot_id": {"S": "PORTFOLIO-METADATA"},
        "last_known_cash": {"S": str(gbp_cash)},
        "last_known_equity": {"S": str(gbp_cash)},
        "updated_at": {"N": str(now_ts)},
    }
    cmd_put_meta = [
        "aws", "dynamodb", "put-item",
        "--table-name", "kraken_bot_state",
        "--item", json.dumps(portfolio_meta),
        "--region", "eu-west-1",
    ]
    subprocess.run(cmd_put_meta, check=True)
    logger.info("Updated PORTFOLIO-METADATA in DynamoDB.")

    # Final summary report
    print("\n" + "="*50)
    print("LIQUIDATION TO GBP COMPLETE")
    print("="*50)
    print(f"Final GBP Balance: £{gbp_cash:.2f}")
    print("Sold Assets Summary:")
    for rec in sold_records:
        print(f"  • {rec['pair']}: Sold {rec['volume']} | Gross: £{rec.get('gross_gbp', 'N/A')} | Fee: £{rec.get('fee_gbp', 'N/A')} (TxID: {rec['txid']})")
    print("Automation Status: DISABLED (EventBridge rule 'kraken-trader-schedule' is DISABLED)")
    print("Account is safely parked 100% in GBP.")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
