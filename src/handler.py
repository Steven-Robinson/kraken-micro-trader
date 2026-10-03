"""Core Lambda Handler and State Machine Coordinator for Kraken Micro-Trader."""

from __future__ import annotations

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
    DYNAMIC_CAPITAL_ENABLED,
    DYNAMIC_TARGET_ENABLED,
    FEE_BUFFER_PERCENT,
    MAX_PROFIT_TARGET_PERCENT,
    MAX_STALE_HOURS,
    MIN_FEE_BUFFER_GBP,
    MIN_PROFIT_TARGET_PERCENT,
    PROFIT_TARGET_PERCENT,
    ROUND_TRIP_FEE_PERCENT,
    SNS_TOPIC_ARN,
    TRADING_PAIR,
    TRADING_PAIRS,
    get_kraken_credentials,
)
from kraken_client import KrakenClient
from grid_engine import initialize_grid_state, process_grid_pair
from state_store import StateStore
from volatility import compute_volatility_target

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


def check_for_new_deposits(
    client: KrakenClient,
    dynamodb_table: str,
    aws_region: str,
    num_pairs: int,
) -> None:
    """Detect new GBP bank deposits and notify the user via Amazon SNS."""
    if client.dry_run:
        return

    try:
        portfolio_store = StateStore(table_name=dynamodb_table, bot_id="PORTFOLIO-METADATA", aws_region=aws_region)
        meta = portfolio_store.load_state()

        balances = client.get_balance()
        current_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0")))

        tb = client.get_trade_balance(asset="ZGBP")
        current_equity = Decimal(str(tb.get("eb", current_cash)))

        last_cash_str = meta.get("last_known_cash")
        last_equity_str = meta.get("last_known_equity")
        if not last_cash_str or not last_equity_str:
            meta["last_known_cash"] = str(current_cash)
            meta["last_known_equity"] = str(current_equity)
            meta["updated_at"] = int(time.time())
            portfolio_store.save_state(meta)
            return

        last_cash = Decimal(last_cash_str)
        last_equity = Decimal(last_equity_str)
        cash_delta = current_cash - last_cash
        equity_delta = current_equity - last_equity

        # Detect new external cash deposit:
        # 1. Cash increased by at least £5.00
        # 2. Equity also increased proportionally (distinguishing from sell order fills where equity delta is only trade profit)
        is_external_deposit = cash_delta >= Decimal("5.00") and equity_delta >= (cash_delta * Decimal("0.70"))

        if is_external_deposit:
            fee_buffer = max(MIN_FEE_BUFFER_GBP, current_equity * (FEE_BUFFER_PERCENT / Decimal("100")))
            tradeable_pool = max(Decimal("0"), current_equity - fee_buffer)
            target_per_pair = (tradeable_pool / Decimal(num_pairs)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            logger.info(
                "New cash deposit detected: +£%s (Cash was: £%s, now: £%s; Equity was: £%s, now: £%s)",
                cash_delta, last_cash, current_cash, last_equity, current_equity
            )
            send_alert(
                f"[Kraken Micro-Trader] New Deposit Detected! (+£{cash_delta:.2f} Added to Trading)",
                f"We detected a new cash deposit on your Kraken account! 🎉\n\n"
                f"• Deposit Amount: +£{cash_delta:.2f}\n"
                f"• Total Available Cash: £{current_cash:.2f}\n"
                f"• Total Portfolio Equity: £{current_equity:.2f}\n"
                f"• Preserved Fee Buffer: £{fee_buffer:.2f}\n"
                f"• Updated Capital per Pair (N={num_pairs}): £{target_per_pair:.2f}\n\n"
                f"The bot has automatically added these funds to your trading pool. Future buy orders will trade with your scaled capital.",
            )

            # Update the baseline so we don't alert repeatedly
            meta["last_known_cash"] = str(current_cash)
            meta["last_known_equity"] = str(current_equity)
            meta["updated_at"] = int(time.time())
            portfolio_store.save_state(meta)

    except Exception as e:
        logger.warning("Error checking for deposits: %s", e)


def sync_portfolio_metadata(
    client: KrakenClient,
    dynamodb_table: str,
    aws_region: str,
) -> None:
    """Record latest portfolio cash and equity snapshot in DynamoDB."""
    if client.dry_run:
        return
    try:
        portfolio_store = StateStore(table_name=dynamodb_table, bot_id="PORTFOLIO-METADATA", aws_region=aws_region)
        balances = client.get_balance()
        current_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0")))
        tb = client.get_trade_balance(asset="ZGBP")
        current_equity = Decimal(str(tb.get("eb", current_cash)))

        portfolio_store.save_state({
            "last_known_cash": str(current_cash),
            "last_known_equity": str(current_equity),
            "updated_at": int(time.time()),
        })
    except Exception as e:
        logger.warning("Failed to sync portfolio metadata: %s", e)


def resolve_pair_capital(
    client: KrakenClient,
    num_pairs: int,
    fallback_capital: Decimal,
) -> Decimal:
    """Dynamically determine capital per active pair, reserving a permanent fee buffer.

    Formula:
        1. Fetch Total Portfolio Equity (eb = cash + crypto value) from TradeBalance.
        2. Calculate permanent fee buffer: max(MIN_FEE_BUFFER_GBP, Total Equity * FEE_BUFFER_PERCENT / 100).
        3. Tradeable pool = Total Equity - Fee Buffer.
        4. Target per pair = Tradeable pool / num_pairs.
    """
    safe_fallback = (fallback_capital / Decimal(num_pairs)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if not DYNAMIC_CAPITAL_ENABLED or client.dry_run:
        return safe_fallback

    try:
        tb = client.get_trade_balance(asset="ZGBP")
        balances = client.get_balance()
        free_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0")))
        total_equity = Decimal(str(tb.get("eb", free_cash)))

        # Permanent fee buffer: min £2.00, or 3% on larger balances
        fee_buffer = max(MIN_FEE_BUFFER_GBP, total_equity * (FEE_BUFFER_PERCENT / Decimal("100")))
        tradeable_pool = max(Decimal("0"), total_equity - fee_buffer)

        # Fair share per pair
        target_per_pair = (tradeable_pool / Decimal(num_pairs)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        logger.info(
            "Dynamic Capital Pool: Total Equity=£%s, Fee Buffer=£%s -> Target/Pair=£%s (N=%d)",
            total_equity,
            fee_buffer,
            target_per_pair,
            num_pairs,
        )

        return target_per_pair if target_per_pair > Decimal("5.00") else safe_fallback

    except Exception as e:
        logger.warning("Failed to calculate dynamic capital, using fallback £%s: %s", safe_fallback, e)
        return safe_fallback


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint triggered by EventBridge cron."""
    logger.info("Starting Kraken Micro-Trader iteration (DRY_RUN=%s, Pairs=%s)", DRY_RUN, TRADING_PAIRS)

    # 1. Setup Kraken client
    api_key, api_secret = get_kraken_credentials()
    client = KrakenClient(api_key=api_key, api_secret=api_secret, dry_run=DRY_RUN)

    # 2. Check for newly deposited funds from bank and alert user
    check_for_new_deposits(client, DYNAMODB_TABLE, AWS_REGION, len(TRADING_PAIRS))

    # 3. Determine dynamically allocated capital per pair with permanent fee buffer
    pair_capital = resolve_pair_capital(client, len(TRADING_PAIRS), ALLOCATED_CAPITAL_GBP)

    # 4. Iterate through all configured pairs
    results = {}
    for pair in TRADING_PAIRS:
        try:
            res = process_pair(pair, client, DYNAMODB_TABLE, AWS_REGION, pair_capital)
            results[pair] = res
        except Exception as e:
            logger.error("Error processing pair %s: %s", pair, e, exc_info=True)
            results[pair] = {"pair": pair, "status": "error", "error": str(e)}

    # 5. Sync portfolio cash and equity snapshot for next cycle deposit detection
    sync_portfolio_metadata(client, DYNAMODB_TABLE, AWS_REGION)

    # Backward-compatible summary fields
    primary_res = results.get(TRADING_PAIR, {})
    total_trades = sum(r.get("trades_completed", 0) for r in results.values() if isinstance(r, dict))

    return {
        "status": "success",
        "pairs": results,
        "bot_state": primary_res.get("bot_state", "IDLE"),
        "trades_completed": total_trades,
    }


def process_pair(
    pair: str,
    client: KrakenClient,
    dynamodb_table: str,
    aws_region: str,
    capital: Decimal,
) -> dict[str, Any]:
    """Process a single trading pair's state machine."""
    bot_id = f"{pair}-PINGPONG"
    store = StateStore(table_name=dynamodb_table, bot_id=bot_id, aws_region=aws_region)

    state = store.load_state()
    current_state = state.get("state", "IDLE")
    if state.get("version") == "grid_v2":
        rungs_summary = ", ".join(f"Rung {k}: {v.get('state')}" for k, v in state.get("rungs", {}).items())
        logger.info("[%s] Current Bot State: Grid v2 (%s) (Bot ID: %s)", pair, rungs_summary, bot_id)
    else:
        logger.info("[%s] Current Bot State: %s (Bot ID: %s)", pair, current_state, bot_id)

    if current_state == "PAUSED":
        logger.warning("[%s] Bot is PAUSED. Halting execution.", pair)
        return {"pair": pair, "status": "paused"}

    # Fetch pair precision rules from Kraken
    pair_info = client.get_pair_info(pair)
    pair_decimals = pair_info["pair_decimals"]
    lot_decimals = pair_info["lot_decimals"]
    ordermin = pair_info["ordermin"]

    # 1. Grid Engine Mode (v2)
    if state.get("version") == "grid_v2":
        return process_grid_pair(
            pair, client, store, state, pair_decimals, lot_decimals, ordermin, capital, send_alert
        )

    # 2. Seamless Migration for ETHGBP: cancel old single buy order and activate 2-Rung Micro-Grid immediately
    if pair == "ETHGBP" and state.get("state") in ("IDLE", "BUY_PLACED"):
        old_txid = state.get("active_order_id")
        if old_txid and not client.dry_run:
            try:
                client.cancel_order(old_txid)
                logger.info("[%s] Cancelled legacy single buy order %s to activate 2-Rung Micro-Grid.", pair, old_txid)
            except Exception as e:
                logger.warning("[%s] Could not cancel legacy buy order %s: %s", pair, old_txid, e)
        state = initialize_grid_state(pair)
        store.save_state(state)
        return process_grid_pair(
            pair, client, store, state, pair_decimals, lot_decimals, ordermin, capital, send_alert
        )

    # 3. Legacy State Machine Router (for active SOL and BTC sell orders)
    if current_state == "IDLE":
        _handle_idle(client, store, state, pair, pair_decimals, lot_decimals, ordermin, capital)
    elif current_state == "BUY_PLACED":
        _handle_buy_placed(client, store, state, pair, pair_decimals, lot_decimals)
    elif current_state == "SELL_PLACED":
        _handle_sell_placed(client, store, state, pair, pair_decimals, lot_decimals, ordermin, capital)
    else:
        logger.error("[%s] Unknown state '%s'. Resetting to IDLE.", pair, current_state)
        state["state"] = "IDLE"
        store.save_state(state)

    return {
        "pair": pair,
        "bot_state": state.get("state"),
        "trades_completed": state.get("trades_completed", 0),
        "total_profit_gbp": state.get("total_realized_profit_gbp", "0.0"),
    }


def _handle_idle(
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair: str,
    pair_decimals: int,
    lot_decimals: int,
    ordermin: Decimal,
    capital: Decimal,
) -> None:
    """IDLE state: Place initial Limit Buy order at a slight discount to current bid."""
    ticker = client.get_ticker(pair)
    best_bid = ticker["bid"]
    logger.info("[%s] Market Ticker: Bid=£%s, Ask=£%s, Last=£%s", pair, best_bid, ticker["ask"], ticker["last"])

    # Calculate buy price (e.g. 0.2% below current best bid to guarantee Maker status)
    discount_multiplier = Decimal("1") - (BUY_DISCOUNT_PERCENT / Decimal("100"))
    raw_buy_price = best_bid * discount_multiplier
    buy_price = _round_decimal(raw_buy_price, pair_decimals, rounding=ROUND_HALF_UP)

    # Strictly protect the permanent fee buffer from free cash
    balances = client.get_balance()
    free_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0")))
    tradeable_cash = max(Decimal("0"), free_cash - MIN_FEE_BUFFER_GBP)
    effective_capital = min(capital, tradeable_cash)

    # Calculate volume based on allocated GBP capital
    raw_volume = effective_capital / buy_price
    volume = _round_decimal(raw_volume, lot_decimals, rounding=ROUND_DOWN)

    if volume < ordermin:
        logger.warning(
            "[%s] Calculated volume %s (Capital: £%s, FreeCash: £%s) is below Kraken minimum %s. Waiting for funds/fill.",
            pair, volume, effective_capital, free_cash, ordermin
        )
        return

    logger.info("[%s] Placing initial Limit Buy: %s %s @ £%s (Capital: £%s, Fee Buffer Kept: £%s)", pair, volume, pair, buy_price, effective_capital, MIN_FEE_BUFFER_GBP)
    try:
        txid = client.place_limit_order(pair, "buy", volume, buy_price, post_only=True)
    except Exception as e:
        if "User Locked" in str(e):
            logger.warning("[%s] Kraken Account is currently in the UK FCA 24-hour cooling-off period. Will retry every 2 minutes.", pair)
            return
        raise

    state["state"] = "BUY_PLACED"
    state["active_order_id"] = txid
    state["last_buy_price"] = str(buy_price)
    state["last_buy_volume"] = str(volume)
    state["order_placed_at"] = int(time.time())
    store.save_state(state)
    logger.info("[%s] State transitioned to BUY_PLACED (Order ID: %s)", pair, txid)

    # Update cash baseline in portfolio metadata
    try:
        p_store = StateStore(table_name=store.table_name, bot_id="PORTFOLIO-METADATA", aws_region=store.aws_region)
        p_meta = p_store.load_state()
        new_balances = client.get_balance()
        p_meta["last_known_cash"] = str(new_balances.get("ZGBP", new_balances.get("GBP", Decimal("0"))))
        p_meta["updated_at"] = int(time.time())
        p_store.save_state(p_meta)
    except Exception:
        pass


def _handle_buy_placed(
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair: str,
    pair_decimals: int,
    lot_decimals: int,
) -> None:
    """BUY_PLACED state: Monitor buy order fill. When filled, submit Limit Sell."""
    txid = state.get("active_order_id")
    if not txid:
        logger.error("[%s] No active_order_id found in BUY_PLACED. Resetting to IDLE.", pair)
        state["state"] = "IDLE"
        store.save_state(state)
        return

    order_info = client.query_order(txid)
    status = order_info.get("status")
    logger.info("[%s] Buy Order %s status: %s", pair, txid, status)

    if status == "closed":
        # Order is filled! Place the limit sell order at profit target
        actual_price = order_info["price"] if order_info.get("price", Decimal("0")) > 0 else Decimal(state["last_buy_price"])
        actual_volume = order_info["vol_exec"] if order_info.get("vol_exec", Decimal("0")) > 0 else Decimal(state["last_buy_volume"])

        # Compute profit target: Dynamic Half-Spread Engine vs Fallback
        target_pct = PROFIT_TARGET_PERCENT
        vol_info = ""
        if DYNAMIC_TARGET_ENABLED:
            try:
                candles = client.get_ohlc(pair, interval=60)
                dynamic_pct, stats = compute_volatility_target(
                    candles,
                    fallback_percent=PROFIT_TARGET_PERCENT,
                    min_target=MIN_PROFIT_TARGET_PERCENT,
                    max_target=MAX_PROFIT_TARGET_PERCENT,
                )
                target_pct = dynamic_pct
                vol_info = (
                    f"\n• Market Volatility (24h Spread): {stats.get('swing_24h_pct', 'N/A')}\n"
                    f"• 24h High: £{stats.get('high_24h', 'N/A')} | 24h Low: £{stats.get('low_24h', 'N/A')}\n"
                    f"• Dynamic Target Applied: +{target_pct}%"
                )
            except Exception as e:
                logger.warning("[%s] Could not calculate dynamic volatility, using fallback %s%%: %s", pair, PROFIT_TARGET_PERCENT, e)

        # Target Sell Price = Buy Price * (1 + Profit Target % + Round Trip Fees %)
        total_markup = Decimal("1") + ((target_pct + ROUND_TRIP_FEE_PERCENT) / Decimal("100"))
        raw_sell_price = actual_price * total_markup
        sell_price = _round_decimal(raw_sell_price, pair_decimals, rounding=ROUND_HALF_UP)

        logger.info("[%s] Buy filled at £%s. Submitting Limit Sell: %s %s @ £%s (+%s%% net target)", pair, actual_price, actual_volume, pair, sell_price, target_pct)
        sell_txid = client.place_limit_order(pair, "sell", actual_volume, sell_price, post_only=True)

        state["state"] = "SELL_PLACED"
        state["active_order_id"] = sell_txid
        state["target_sell_price"] = str(sell_price)
        state["applied_profit_target_percent"] = str(target_pct)
        state["order_placed_at"] = int(time.time())
        store.save_state(state)
        logger.info("[%s] State transitioned to SELL_PLACED (Order ID: %s)", pair, sell_txid)

        send_alert(
            f"[Kraken Micro-Trader] Buy Order Filled: {actual_volume} {pair} @ £{actual_price}",
            f"Your Limit Buy order on Kraken filled successfully!\n\n"
            f"• Pair: {pair}\n"
            f"• Filled Amount: {actual_volume}\n"
            f"• Buy Price: £{actual_price}\n"
            f"• Total Cost: £{(actual_price * actual_volume):.2f}\n"
            f"{vol_info}\n\n"
            f"Next Action: Limit Sell order queued at target price £{sell_price} (+{target_pct}% target).\n"
            f"Active Order ID: {sell_txid}",
        )

    elif status == "open":
        # Check if the order has been sitting unfilled too long
        order_age_hours = (time.time() - float(state.get("order_placed_at", time.time()))) / 3600
        if order_age_hours > float(MAX_STALE_HOURS):
            logger.warning("[%s] Buy order %s has been open for %.1f hours. Cancelling to replace.", pair, txid, order_age_hours)
            client.cancel_order(txid)
            state["state"] = "IDLE"
            state["active_order_id"] = None
            store.save_state(state)
        else:
            logger.info("[%s] Buy order %s still open (Age: %.1f hours). Waiting.", pair, txid, order_age_hours)

    elif status in ("canceled", "expired"):
        logger.warning("[%s] Buy order %s was %s. Returning to IDLE.", pair, txid, status)
        state["state"] = "IDLE"
        state["active_order_id"] = None
        store.save_state(state)


def _handle_sell_placed(
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair: str,
    pair_decimals: int,
    lot_decimals: int,
    ordermin: Decimal,
    capital: Decimal,
) -> None:
    """SELL_PLACED state: Monitor sell order fill. When filled, lock in profit and re-enter."""
    txid = state.get("active_order_id")
    if not txid:
        logger.error("[%s] No active_order_id found in SELL_PLACED. Resetting to IDLE.", pair)
        state["state"] = "IDLE"
        store.save_state(state)
        return

    order_info = client.query_order(txid)
    status = order_info.get("status")
    logger.info("[%s] Sell Order %s status: %s", pair, txid, status)

    if status == "closed":
        # Cycle complete! Realize profit
        sell_price = order_info["price"] if order_info.get("price", Decimal("0")) > 0 else Decimal(state["target_sell_price"])
        buy_price = Decimal(state.get("last_buy_price", "0"))
        volume = Decimal(state.get("last_buy_volume", "0"))

        gross_profit = (sell_price - buy_price) * volume
        fees = (buy_price * volume + sell_price * volume) * (ROUND_TRIP_FEE_PERCENT / Decimal("200"))
        net_profit = gross_profit - fees

        total_trades = state.get("trades_completed", 0) + 1
        total_profit = (Decimal(state.get("total_realized_profit_gbp", "0.0")) + net_profit).quantize(Decimal("0.01"))

        logger.info("[%s] >>> CYCLE COMPLETE! Net Profit: +£%.2f (Total Realized: £%.2f across %d trades) <<<", pair, net_profit, total_profit, total_trades)

        # Update portfolio metadata with fresh cash after sell fill so it's not confused with a bank deposit
        try:
            p_store = StateStore(table_name=store.table_name, bot_id="PORTFOLIO-METADATA", aws_region=store.aws_region)
            p_meta = p_store.load_state()
            new_balances = client.get_balance()
            p_meta["last_known_cash"] = str(new_balances.get("ZGBP", new_balances.get("GBP", Decimal("0"))))
            p_meta["updated_at"] = int(time.time())
            p_store.save_state(p_meta)
        except Exception:
            pass

        # Upgrade pair to 2-Rung Micro-Grid for subsequent cycles!
        logger.info("[%s] Upgrading pair to 2-Rung Micro-Grid v2 after profitable cycle completion.", pair)
        grid_state = initialize_grid_state(pair)
        grid_state["trades_completed"] = total_trades
        grid_state["total_realized_profit_gbp"] = f"{total_profit:.2f}"
        store.save_state(grid_state)

        send_alert(
            f"[Kraken Micro-Trader] Profit Locked In on {pair}! (+£{net_profit:.2f})",
            f"Your Limit Sell order on Kraken filled successfully! 🚀\n\n"
            f"• Pair: {pair}\n"
            f"• Sold: {volume} {pair} @ £{sell_price:.2f}\n"
            f"• Entry Price: £{buy_price:.2f}\n"
            f"• Net Profit (After Fees): +£{net_profit:.2f}\n"
            f"• Total Realized Profit to Date: £{total_profit:.2f} across {total_trades} trade(s)\n\n"
            f"Next Action: Pair successfully upgraded to 2-Rung Micro-Grid! Staggered buy rungs will deploy on next iteration.",
        )

    elif status == "open":
        logger.info("[%s] Sell order %s still open waiting for target price £%s.", pair, txid, state.get("target_sell_price"))

    elif status in ("canceled", "expired"):
        logger.warning("[%s] Sell order %s was %s! Re-submitting at target price.", pair, txid, status)
        target_price = Decimal(state.get("target_sell_price", "0"))
        volume = Decimal(state.get("last_buy_volume", "0"))
        if target_price > 0 and volume > 0:
            new_txid = client.place_limit_order(pair, "sell", volume, target_price, post_only=True)
            state["active_order_id"] = new_txid
            store.save_state(state)
        else:
            state["state"] = "IDLE"
            store.save_state(state)
