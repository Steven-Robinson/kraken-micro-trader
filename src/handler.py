"""Core Lambda Handler and State Machine Coordinator for Kraken Micro-Trader."""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import logging
import time
from typing import Any

from config import (
    ALLOCATED_CAPITAL_GBP,
    AWS_REGION,
    BOT_ID,
    BUY_DISCOUNT_PERCENT,
    DYNAMODB_TABLE,
    DRY_RUN,
    MAX_STALE_HOURS,
    PROFIT_TARGET_PERCENT,
    ROUND_TRIP_FEE_PERCENT,
    SNS_TOPIC_ARN,
    TRADING_PAIR,
    get_kraken_credentials,
)
from kraken_client import KrakenClient
from state_store import StateStore

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def send_alert(subject: str, message: str) -> None:
    """Send trade execution alert via Amazon SNS."""
    if not SNS_TOPIC_ARN:
        return
    try:
        import boto3
        sns = boto3.client("sns", region_name=AWS_REGION)
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],  # SNS subject max length is 100
            Message=message,
        )
        logger.info("Sent SNS alert: %s", subject)
    except Exception as e:
        logger.error("Failed to send SNS alert: %s", e)


def _round_decimal(val: Decimal, places: int, rounding=ROUND_HALF_UP) -> Decimal:
    """Format decimal to specific decimal places."""
    q = Decimal(10) ** -places
    return val.quantize(q, rounding=rounding)


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint triggered by EventBridge cron."""
    logger.info("Starting Kraken Micro-Trader iteration (DRY_RUN=%s, Pair=%s)", DRY_RUN, TRADING_PAIR)

    # 1. Setup client and state store
    api_key, api_secret = get_kraken_credentials()
    client = KrakenClient(api_key=api_key, api_secret=api_secret, dry_run=DRY_RUN)
    store = StateStore(table_name=DYNAMODB_TABLE, bot_id=BOT_ID, aws_region=AWS_REGION)

    state = store.load_state()
    current_state = state.get("state", "IDLE")
    logger.info("Current Bot State: %s", current_state)

    if current_state == "PAUSED":
        logger.warning("Bot is PAUSED. Halting execution.")
        return {"status": "paused"}

    # 2. Fetch pair precision rules from Kraken
    pair_info = client.get_pair_info(TRADING_PAIR)
    pair_decimals = pair_info["pair_decimals"]
    lot_decimals = pair_info["lot_decimals"]
    ordermin = pair_info["ordermin"]

    # 3. State Machine Router
    if current_state == "IDLE":
        _handle_idle(client, store, state, pair_decimals, lot_decimals, ordermin)
    elif current_state == "BUY_PLACED":
        _handle_buy_placed(client, store, state, pair_decimals, lot_decimals)
    elif current_state == "SELL_PLACED":
        _handle_sell_placed(client, store, state, pair_decimals, lot_decimals, ordermin)
    else:
        logger.error("Unknown state '%s'. Resetting to IDLE.", current_state)
        state["state"] = "IDLE"
        store.save_state(state)

    return {
        "status": "success",
        "bot_state": state.get("state"),
        "trades_completed": state.get("trades_completed", 0),
        "total_profit_gbp": state.get("total_realized_profit_gbp", "0.0"),
    }


def _handle_idle(
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair_decimals: int,
    lot_decimals: int,
    ordermin: Decimal,
) -> None:
    """IDLE state: Place initial Limit Buy order at a slight discount to current bid."""
    ticker = client.get_ticker(TRADING_PAIR)
    best_bid = ticker["bid"]
    logger.info("Market Ticker: Bid=£%s, Ask=£%s, Last=£%s", best_bid, ticker["ask"], ticker["last"])

    # Calculate buy price (e.g. 0.2% below current best bid to guarantee Maker status)
    discount_multiplier = Decimal("1") - (BUY_DISCOUNT_PERCENT / Decimal("100"))
    raw_buy_price = best_bid * discount_multiplier
    buy_price = _round_decimal(raw_buy_price, pair_decimals, rounding=ROUND_HALF_UP)

    # Calculate volume based on allocated GBP capital
    raw_volume = ALLOCATED_CAPITAL_GBP / buy_price
    volume = _round_decimal(raw_volume, lot_decimals, rounding=ROUND_DOWN)

    if volume < ordermin:
        logger.error("Calculated volume %s is below Kraken minimum %s. Increase capital.", volume, ordermin)
        return

    logger.info("Placing initial Limit Buy: %s %s @ £%s (Capital: £%s)", volume, TRADING_PAIR, buy_price, ALLOCATED_CAPITAL_GBP)
    try:
        txid = client.place_limit_order(TRADING_PAIR, "buy", volume, buy_price, post_only=True)
    except Exception as e:
        if "User Locked" in str(e):
            logger.warning("Kraken Account is currently in the UK FCA 24-hour cooling-off period. Will automatically retry every 2 minutes until unlocked.")
            return
        raise

    state["state"] = "BUY_PLACED"
    state["active_order_id"] = txid
    state["last_buy_price"] = str(buy_price)
    state["last_buy_volume"] = str(volume)
    state["order_placed_at"] = int(time.time())
    store.save_state(state)
    logger.info("State transitioned to BUY_PLACED (Order ID: %s)", txid)


def _handle_buy_placed(
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair_decimals: int,
    lot_decimals: int,
) -> None:
    """BUY_PLACED state: Monitor buy order fill. When filled, submit Limit Sell."""
    txid = state.get("active_order_id")
    if not txid:
        logger.error("No active_order_id found in BUY_PLACED. Resetting to IDLE.")
        state["state"] = "IDLE"
        store.save_state(state)
        return

    order_info = client.query_order(txid)
    status = order_info.get("status")
    logger.info("Buy Order %s status: %s", txid, status)

    if status == "closed":
        # Order is filled! Place the limit sell order at profit target
        actual_price = order_info["price"] if order_info.get("price", Decimal("0")) > 0 else Decimal(state["last_buy_price"])
        actual_volume = order_info["vol_exec"] if order_info.get("vol_exec", Decimal("0")) > 0 else Decimal(state["last_buy_volume"])

        # Target Sell Price = Buy Price * (1 + Profit Target % + Round Trip Fees %)
        total_markup = Decimal("1") + ((PROFIT_TARGET_PERCENT + ROUND_TRIP_FEE_PERCENT) / Decimal("100"))
        raw_sell_price = actual_price * total_markup
        sell_price = _round_decimal(raw_sell_price, pair_decimals, rounding=ROUND_HALF_UP)

        logger.info("Buy filled at £%s. Submitting Limit Sell: %s %s @ £%s (+%s%% net target)", actual_price, actual_volume, TRADING_PAIR, sell_price, PROFIT_TARGET_PERCENT)
        sell_txid = client.place_limit_order(TRADING_PAIR, "sell", actual_volume, sell_price, post_only=True)

        state["state"] = "SELL_PLACED"
        state["active_order_id"] = sell_txid
        state["target_sell_price"] = str(sell_price)
        state["order_placed_at"] = int(time.time())
        store.save_state(state)
        logger.info("State transitioned to SELL_PLACED (Order ID: %s)", sell_txid)

        send_alert(
            f"[Kraken Micro-Trader] Buy Order Filled: {actual_volume} {TRADING_PAIR} @ £{actual_price}",
            f"Your Limit Buy order on Kraken filled successfully!\n\n"
            f"• Pair: {TRADING_PAIR}\n"
            f"• Filled Amount: {actual_volume}\n"
            f"• Buy Price: £{actual_price}\n"
            f"• Total Cost: £{(actual_price * actual_volume):.2f}\n\n"
            f"Next Action: Limit Sell order queued at target price £{sell_price} (+{PROFIT_TARGET_PERCENT}% net target).\n"
            f"Active Order ID: {sell_txid}",
        )

    elif status == "open":
        # Check if the order has been sitting unfilled too long
        order_age_hours = (time.time() - state.get("order_placed_at", time.time())) / 3600
        if order_age_hours > float(MAX_STALE_HOURS):
            logger.warning("Buy order %s has been open for %.1f hours. Cancelling to replace.", txid, order_age_hours)
            client.cancel_order(txid)
            state["state"] = "IDLE"
            state["active_order_id"] = None
            store.save_state(state)
        else:
            logger.info("Buy order %s still open (Age: %.1f hours). Waiting.", txid, order_age_hours)

    elif status in ("canceled", "expired"):
        logger.warning("Buy order %s was %s. Returning to IDLE.", txid, status)
        state["state"] = "IDLE"
        state["active_order_id"] = None
        store.save_state(state)


def _handle_sell_placed(
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair_decimals: int,
    lot_decimals: int,
    ordermin: Decimal,
) -> None:
    """SELL_PLACED state: Monitor sell order fill. When filled, lock in profit and re-enter."""
    txid = state.get("active_order_id")
    if not txid:
        logger.error("No active_order_id found in SELL_PLACED. Resetting to IDLE.")
        state["state"] = "IDLE"
        store.save_state(state)
        return

    order_info = client.query_order(txid)
    status = order_info.get("status")
    logger.info("Sell Order %s status: %s", txid, status)

    if status == "closed":
        # Cycle complete! Realize profit
        sell_price = order_info["price"] if order_info.get("price", Decimal("0")) > 0 else Decimal(state["target_sell_price"])
        buy_price = Decimal(state.get("last_buy_price", "0"))
        volume = Decimal(state.get("last_buy_volume", "0"))

        gross_profit = (sell_price - buy_price) * volume
        fees = (buy_price * volume + sell_price * volume) * (ROUND_TRIP_FEE_PERCENT / Decimal("200"))
        net_profit = gross_profit - fees

        total_trades = state.get("trades_completed", 0) + 1
        total_profit = Decimal(state.get("total_realized_profit_gbp", "0.0")) + net_profit

        logger.info(">>> CYCLE COMPLETE! Net Profit: +£%s (Total Realized: £%s across %d trades) <<<", net_profit, total_profit, total_trades)

        # Calculate re-entry buy price (e.g. at a discount after selling high)
        reentry_discount = Decimal("1") - (PROFIT_TARGET_PERCENT / Decimal("100"))
        raw_reentry_price = sell_price * reentry_discount
        reentry_price = _round_decimal(raw_reentry_price, pair_decimals, rounding=ROUND_HALF_UP)

        raw_volume = ALLOCATED_CAPITAL_GBP / reentry_price
        reentry_volume = _round_decimal(raw_volume, lot_decimals, rounding=ROUND_DOWN)

        logger.info("Submitting new Limit Buy for next cycle: %s %s @ £%s", reentry_volume, TRADING_PAIR, reentry_price)
        buy_txid = client.place_limit_order(TRADING_PAIR, "buy", reentry_volume, reentry_price, post_only=True)

        state["state"] = "BUY_PLACED"
        state["active_order_id"] = buy_txid
        state["last_buy_price"] = str(reentry_price)
        state["last_buy_volume"] = str(reentry_volume)
        state["trades_completed"] = total_trades
        state["total_realized_profit_gbp"] = str(total_profit)
        state["order_placed_at"] = int(time.time())
        store.save_state(state)

        send_alert(
            f"[Kraken Micro-Trader] Profit Locked In! (+£{net_profit:.2f})",
            f"Your Limit Sell order on Kraken filled successfully! 🚀\n\n"
            f"• Sold: {volume} {TRADING_PAIR} @ £{sell_price}\n"
            f"• Entry Price: £{buy_price}\n"
            f"• Net Profit (After Fees): +£{net_profit:.4f}\n"
            f"• Total Realized Profit to Date: £{total_profit:.4f} across {total_trades} trade(s)\n\n"
            f"Next Action: Queued re-entry Limit Buy order at £{reentry_price} for {reentry_volume} {TRADING_PAIR}.\n"
            f"Active Order ID: {buy_txid}",
        )

    elif status == "open":
        logger.info("Sell order %s still open waiting for target price £%s.", txid, state.get("target_sell_price"))

    elif status in ("canceled", "expired"):
        logger.warning("Sell order %s was %s! Re-submitting at target price.", txid, status)
        target_price = Decimal(state.get("target_sell_price", "0"))
        volume = Decimal(state.get("last_buy_volume", "0"))
        if target_price > 0 and volume > 0:
            new_txid = client.place_limit_order(TRADING_PAIR, "sell", volume, target_price, post_only=True)
            state["active_order_id"] = new_txid
            store.save_state(state)
        else:
            state["state"] = "IDLE"
            store.save_state(state)
