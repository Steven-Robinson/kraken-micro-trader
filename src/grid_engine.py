"""Multi-Rung Micro-Grid Engine for Kraken Micro-Trader.

Divides pair capital into staggered micro-rungs that harvest intraday
oscillations with post-only Maker limit orders, guaranteeing zero taker fees
and compounding frequent small profits.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import logging
import time
from typing import Any, Callable

from config import (
    BUY_DISCOUNT_PERCENT,
    DYNAMIC_RUNGS_ENABLED,
    DYNAMIC_TARGET_ENABLED,
    MAX_PROFIT_TARGET_PERCENT,
    MAX_STALE_HOURS,
    MIN_FEE_BUFFER_GBP,
    MIN_PROFIT_TARGET_PERCENT,
    PROFIT_TARGET_PERCENT,
    ROUND_TRIP_FEE_PERCENT,
    RUNG_1_MAX_DRIFT_PERCENT,
    RUNG_1_MAX_STALE_HOURS,
    RUNG_2_MAX_DRIFT_PERCENT,
    RUNG_2_MAX_STALE_HOURS,
    RUNG_3_MAX_DRIFT_PERCENT,
    RUNG_3_MAX_STALE_HOURS,
    RUNG_3_MIN_PAIR_CAPITAL_GBP,
    RUNG_4_MAX_DRIFT_PERCENT,
    RUNG_4_MAX_STALE_HOURS,
    RUNG_4_MIN_PAIR_CAPITAL_GBP,
    UNDERSIZED_THRESHOLD_PERCENT,
    VELOCITY_FILTER_ENABLED,
    VELOCITY_LOOKBACK_HOURS,
    VELOCITY_MAX_CHANGE_PERCENT,
)
from kraken_client import KrakenClient
from state_store import StateStore
from volatility import compute_volatility_target, check_market_velocity

logger = logging.getLogger(__name__)


def get_rungs_config(pair_capital: Decimal) -> list[dict[str, Any]]:
    """Determine dynamic rung layout and allocations based on pair capital.

    Thresholds:
      • Under £45.00/pair (<~£135 total): 2 rungs (50%/50%)
      • £45.00 - £80.00/pair (~£135-£240 total): 3 rungs (40%/35%/25%)
      • £80.00+/pair (>=~£240 total): 4 rungs (30%/30%/20%/20%)
    """
    if not DYNAMIC_RUNGS_ENABLED or pair_capital < RUNG_3_MIN_PAIR_CAPITAL_GBP:
        return [
            {
                "rung_id": "1",
                "weight": Decimal("0.50"),
                "discount_pct": Decimal("0.60"),
                "target_pct": Decimal("1.00"),
                "stale_hours": RUNG_1_MAX_STALE_HOURS,
                "drift_pct": RUNG_1_MAX_DRIFT_PERCENT,
            },
            {
                "rung_id": "2",
                "weight": Decimal("0.50"),
                "discount_pct": Decimal("1.40"),
                "target_pct": Decimal("1.20"),
                "stale_hours": RUNG_2_MAX_STALE_HOURS,
                "drift_pct": RUNG_2_MAX_DRIFT_PERCENT,
            },
        ]
    elif pair_capital < RUNG_4_MIN_PAIR_CAPITAL_GBP:
        return [
            {
                "rung_id": "1",
                "weight": Decimal("0.40"),
                "discount_pct": Decimal("0.60"),
                "target_pct": Decimal("1.00"),
                "stale_hours": RUNG_1_MAX_STALE_HOURS,
                "drift_pct": RUNG_1_MAX_DRIFT_PERCENT,
            },
            {
                "rung_id": "2",
                "weight": Decimal("0.35"),
                "discount_pct": Decimal("1.50"),
                "target_pct": Decimal("1.30"),
                "stale_hours": RUNG_2_MAX_STALE_HOURS,
                "drift_pct": RUNG_2_MAX_DRIFT_PERCENT,
            },
            {
                "rung_id": "3",
                "weight": Decimal("0.25"),
                "discount_pct": Decimal("2.80"),
                "target_pct": Decimal("1.80"),
                "stale_hours": RUNG_3_MAX_STALE_HOURS,
                "drift_pct": RUNG_3_MAX_DRIFT_PERCENT,
            },
        ]
    else:
        return [
            {
                "rung_id": "1",
                "weight": Decimal("0.30"),
                "discount_pct": Decimal("0.60"),
                "target_pct": Decimal("1.00"),
                "stale_hours": RUNG_1_MAX_STALE_HOURS,
                "drift_pct": RUNG_1_MAX_DRIFT_PERCENT,
            },
            {
                "rung_id": "2",
                "weight": Decimal("0.30"),
                "discount_pct": Decimal("1.50"),
                "target_pct": Decimal("1.30"),
                "stale_hours": RUNG_2_MAX_STALE_HOURS,
                "drift_pct": RUNG_2_MAX_DRIFT_PERCENT,
            },
            {
                "rung_id": "3",
                "weight": Decimal("0.20"),
                "discount_pct": Decimal("3.00"),
                "target_pct": Decimal("2.00"),
                "stale_hours": RUNG_3_MAX_STALE_HOURS,
                "drift_pct": RUNG_3_MAX_DRIFT_PERCENT,
            },
            {
                "rung_id": "4",
                "weight": Decimal("0.20"),
                "discount_pct": Decimal("5.00"),
                "target_pct": Decimal("3.00"),
                "stale_hours": RUNG_4_MAX_STALE_HOURS,
                "drift_pct": RUNG_4_MAX_DRIFT_PERCENT,
            },
        ]


# Default specifications for baseline fallback
DEFAULT_RUNGS_CONFIG = get_rungs_config(Decimal("25.00"))


def _round_decimal(val: Decimal, places: int, rounding=ROUND_HALF_UP) -> Decimal:
    """Format decimal to specific decimal places."""
    q = Decimal(10) ** -places
    return val.quantize(q, rounding=rounding)


def initialize_grid_state(pair: str, pair_capital: Decimal = Decimal("25.00")) -> dict[str, Any]:
    """Create an empty micro-grid state structure matching pair capital."""
    cfgs = get_rungs_config(pair_capital)
    rungs = {}
    for cfg in cfgs:
        rungs[cfg["rung_id"]] = {
            "state": "IDLE",
            "active_order_id": None,
            "discount_pct": str(cfg["discount_pct"]),
            "target_pct": str(cfg["target_pct"]),
            "last_buy_price": "0.0",
            "last_buy_volume": "0.0",
            "target_sell_price": "0.0",
            "order_placed_at": 0,
        }
    return {
        "bot_id": f"{pair}-PINGPONG",
        "version": "grid_v2",
        "trades_completed": 0,
        "total_realized_profit_gbp": "0.0000",
        "rungs": rungs,
    }


def _get_base_asset_keys(pair: str) -> list[str]:
    """Map trading pair to Kraken wallet asset keys."""
    p = pair.upper()
    if "ETH" in p:
        return ["XETH", "ETH"]
    if "SOL" in p:
        return ["SOL"]
    if "XBT" in p or "BTC" in p:
        return ["XXBT", "XBT", "BTC"]
    return [pair.replace("GBP", "").replace("ZGBP", "")]


def process_grid_pair(
    pair: str,
    client: KrakenClient,
    store: StateStore,
    state: dict[str, Any],
    pair_decimals: int,
    lot_decimals: int,
    ordermin: Decimal,
    total_pair_capital: Decimal,
    send_alert_fn: Callable[[str, str], None],
) -> dict[str, Any]:
    """Execute the multi-rung grid state machine for a trading pair."""
    rungs = state.setdefault("rungs", {})
    if not rungs:
        init_state = initialize_grid_state(pair)
        rungs = init_state["rungs"]
        state["rungs"] = rungs
        state["version"] = "grid_v2"

    ticker = client.get_ticker(pair)
    best_bid = ticker["bid"]

    # Protect fee buffer from available free cash, accounting for cash already locked in open buy orders
    balances = client.get_balance()
    total_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0")))

    committed_buy_cash = Decimal("0.0")
    try:
        if hasattr(client, "get_open_orders"):
            open_orders = client.get_open_orders()
            for _, order in open_orders.items():
                descr = order.get("descr", {})
                if descr.get("type") == "buy":
                    rem_vol = Decimal(str(order.get("vol", "0"))) - Decimal(str(order.get("vol_exec", "0")))
                    price = Decimal(str(descr.get("price", "0")))
                    committed_buy_cash += (rem_vol * price)
    except Exception as e:
        logger.debug("[%s] Could not calculate committed buy cash: %s", pair, e)

    truly_free_cash = max(Decimal("0"), total_cash - committed_buy_cash)
    available_cash = max(Decimal("0"), truly_free_cash - MIN_FEE_BUFFER_GBP)

    # Fetch recent candles for volatility targeting and velocity guard
    dynamic_markup = Decimal("0.0")
    candles = None
    if DYNAMIC_TARGET_ENABLED or VELOCITY_FILTER_ENABLED:
        try:
            candles = client.get_ohlc(pair, interval=60)
            if DYNAMIC_TARGET_ENABLED and candles:
                dyn_pct, _ = compute_volatility_target(
                    candles,
                    fallback_percent=PROFIT_TARGET_PERCENT,
                    min_target=MIN_PROFIT_TARGET_PERCENT,
                    max_target=MAX_PROFIT_TARGET_PERCENT,
                )
                # Scaling bonus if market volatility is elevated
                dynamic_markup = max(Decimal("0.0"), dyn_pct - PROFIT_TARGET_PERCENT)
        except Exception as e:
            logger.debug("[%s] Candles or dynamic target calculation fallback: %s", pair, e)

    state_changed = False

    # Determine dynamic rungs configuration based on allocated pair capital
    rungs_cfg = get_rungs_config(total_pair_capital)
    active_rung_ids = {cfg["rung_id"] for cfg in rungs_cfg}

    # Handle decommissioned rungs if capital ever down-scales:
    # 1. If BUY_PLACED on a decommissioned rung, cancel the buy order to free up cash.
    # 2. If SELL_PLACED, KEEP IT in rungs_cfg until the sell fills at profit!
    for r_id, r_data in list(rungs.items()):
        if r_id not in active_rung_ids:
            if r_data.get("state") == "BUY_PLACED":
                old_buy_txid = r_data.get("active_order_id")
                if old_buy_txid and not client.dry_run:
                    try:
                        client.cancel_order(old_buy_txid)
                        logger.info("[%s Decommission Rung %s] Cancelled buy order %s due to capital down-scaling", pair, r_id, old_buy_txid)
                    except Exception as e:
                        logger.warning("[%s Decommission Rung %s] Could not cancel buy %s: %s", pair, r_id, old_buy_txid, e)
                r_data["state"] = "IDLE"
                r_data["active_order_id"] = None
                state_changed = True
            elif r_data.get("state") == "SELL_PLACED":
                rungs_cfg.append({
                    "rung_id": r_id,
                    "weight": Decimal("0"),
                    "discount_pct": Decimal(r_data.get("discount_pct", "2.80")),
                    "target_pct": Decimal(r_data.get("target_pct", "1.80")),
                    "stale_hours": Decimal("48.0"),
                    "drift_pct": Decimal("5.0"),
                })

    # -------------------------------------------------------------
    # 0. AUTO-ADOPTION / INVENTORY RECONCILIATION
    # Detect unmanaged/orphaned crypto in wallet and place Limit Sell
    # -------------------------------------------------------------
    try:
        held_crypto = Decimal("0")
        for asset_key in _get_base_asset_keys(pair):
            held_crypto += Decimal(str(balances.get(asset_key, "0")))

        open_orders = client.get_open_orders()
        committed_sell_volume = Decimal("0")
        if isinstance(open_orders, dict):
            for _, order in open_orders.items():
                descr = order.get("descr", {})
                order_pair = descr.get("pair", "")
                is_match = (
                    order_pair == pair
                    or order_pair in (f"X{pair[:3]}ZGBP", f"{pair[:3]}GBP")
                    or (pair.startswith("SOL") and "SOL" in order_pair)
                    or (pair.startswith("ETH") and "ETH" in order_pair)
                    or (pair.startswith("BTC") and ("XBT" in order_pair or "BTC" in order_pair))
                )
                if is_match and descr.get("type") == "sell":
                    rem_vol = Decimal(str(order.get("vol", "0"))) - Decimal(str(order.get("vol_exec", "0")))
                    committed_sell_volume += rem_vol

        uncommitted_crypto = max(Decimal("0"), held_crypto - committed_sell_volume)

        if uncommitted_crypto >= ordermin:
            # Find an available rung to adopt this crypto into SELL_PLACED
            target_rung_id = None
            for cfg in rungs_cfg:
                r_id = cfg["rung_id"]
                if rungs.get(r_id, {}).get("state") == "IDLE":
                    target_rung_id = r_id
                    break

            if not target_rung_id:
                for cfg in rungs_cfg:
                    r_id = cfg["rung_id"]
                    if rungs.get(r_id, {}).get("state") == "BUY_PLACED":
                        target_rung_id = r_id
                        old_buy_txid = rungs[r_id].get("active_order_id")
                        if old_buy_txid and not client.dry_run:
                            try:
                                client.cancel_order(old_buy_txid)
                                logger.info("[%s Auto-Adopt] Cancelled buy order %s on Rung %s", pair, old_buy_txid, r_id)
                            except Exception as e:
                                logger.warning("[%s Auto-Adopt] Could not cancel buy %s: %s", pair, old_buy_txid, e)
                        break

            if target_rung_id:
                target_rung = rungs.setdefault(target_rung_id, {})
                last_buy = Decimal(str(target_rung.get("last_buy_price", "0")))
                target_pct = Decimal(target_rung.get("target_pct", "1.00")) + dynamic_markup

                best_ask = ticker.get("ask", best_bid)
                tick_step = Decimal(10) ** -pair_decimals

                if last_buy > Decimal("0"):
                    total_markup = Decimal("1") + ((target_pct + ROUND_TRIP_FEE_PERCENT) / Decimal("100"))
                    raw_target = last_buy * total_markup
                    target_sell_price = max(best_ask, _round_decimal(raw_target, pair_decimals, rounding=ROUND_HALF_UP))
                else:
                    target_sell_price = max(best_ask, best_bid + tick_step)

                # Ensure post-only validity: limit sell must strictly exceed best_bid
                if target_sell_price <= best_bid:
                    target_sell_price = best_bid + tick_step

                sell_volume = _round_decimal(uncommitted_crypto, lot_decimals, rounding=ROUND_DOWN)

                if sell_volume >= ordermin:
                    try:
                        logger.info(
                            "[%s Auto-Adopt] Adopting unmanaged %s %s into Rung %s. Submitting Limit Sell @ £%s",
                            pair, sell_volume, pair, target_rung_id, target_sell_price
                        )
                        sell_txid = client.place_limit_order(pair, "sell", sell_volume, target_sell_price, post_only=True)
                        target_rung["state"] = "SELL_PLACED"
                        target_rung["active_order_id"] = sell_txid
                        target_rung["last_buy_volume"] = str(sell_volume)
                        if last_buy <= Decimal("0"):
                            target_rung["last_buy_price"] = str(_round_decimal(target_sell_price / (Decimal("1") + (target_pct / Decimal("100"))), pair_decimals))
                        target_rung["target_sell_price"] = str(target_sell_price)
                        target_rung["applied_target_pct"] = str(target_pct)
                        target_rung["order_placed_at"] = int(time.time())
                        state_changed = True
                        store.save_state(state)

                        send_alert_fn(
                            f"[Kraken Micro-Trader] 🔄 Auto-Adopted {sell_volume} {pair} @ £{target_sell_price}",
                            f"Discovered {sell_volume} unmanaged {pair} in account!\n\n"
                            f"• Auto-adopted into Rung {target_rung_id}\n"
                            f"• Placed Maker Limit Sell @ £{target_sell_price}\n"
                            f"• Order ID: {sell_txid}\n\n"
                            f"This position is now actively working to harvest profit and unfreeze cash.",
                        )
                    except Exception as e:
                        logger.error("[%s Auto-Adopt] Failed to place limit sell for unmanaged crypto: %s", pair, e)
    except Exception as e:
        logger.warning("[%s Auto-Adopt] Error during reconciliation check: %s", pair, e)

    for cfg in rungs_cfg:
        rung_id = cfg["rung_id"]
        rung = rungs.setdefault(rung_id, {
            "state": "IDLE",
            "active_order_id": None,
            "discount_pct": str(cfg["discount_pct"]),
            "target_pct": str(cfg["target_pct"]),
            "last_buy_price": "0.0",
            "last_buy_volume": "0.0",
            "target_sell_price": "0.0",
            "order_placed_at": 0,
        })

        rung_state = rung.get("state", "IDLE")
        rung_capital = (total_pair_capital * cfg["weight"]).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        discount_pct = Decimal(rung.get("discount_pct", str(cfg["discount_pct"])))
        base_target_pct = Decimal(rung.get("target_pct", str(cfg["target_pct"])))
        target_pct = base_target_pct + dynamic_markup

        # -------------------------------------------------------------
        # 1. RUNG IDLE -> Submit Maker Limit Buy
        # -------------------------------------------------------------
        if rung_state == "IDLE":
            # Check Velocity Guard: if price is moving rapidly in one direction, pause placing buys
            if VELOCITY_FILTER_ENABLED and candles:
                is_rapid, roc_pct, _ = check_market_velocity(
                    candles,
                    lookback_hours=VELOCITY_LOOKBACK_HOURS,
                    threshold_percent=VELOCITY_MAX_CHANGE_PERCENT,
                )
                if is_rapid:
                    logger.info(
                        "[%s Rung %s] ⏳ Velocity Guard Active: Price moved %+.2f%% in last %dh (threshold: %.2f%%). Pausing buy placement until market stabilizes.",
                        pair, rung_id, roc_pct, VELOCITY_LOOKBACK_HOURS, float(VELOCITY_MAX_CHANGE_PERCENT)
                    )
                    rung["velocity_paused"] = True
                    rung["velocity_roc_pct"] = str(roc_pct)
                    state_changed = True
                    continue
                else:
                    if rung.get("velocity_paused"):
                        rung["velocity_paused"] = False
                        rung.pop("velocity_roc_pct", None)
                        state_changed = True

            raw_buy_price = best_bid * (Decimal("1") - (discount_pct / Decimal("100")))
            buy_price = _round_decimal(raw_buy_price, pair_decimals, rounding=ROUND_HALF_UP)

            effective_rung_capital = min(rung_capital, available_cash)
            raw_volume = effective_rung_capital / buy_price
            volume = _round_decimal(raw_volume, lot_decimals, rounding=ROUND_DOWN)

            if volume < ordermin:
                logger.warning(
                    "[%s Rung %s] Volume %s (Capital: £%s, Avail: £%s) < ordermin %s. Waiting.",
                    pair, rung_id, volume, effective_rung_capital, available_cash, ordermin
                )
                continue

            try:
                logger.info(
                    "[%s Rung %s] Placing Limit Buy: %s %s @ £%s (-%s%% discount, £%s cap)",
                    pair, rung_id, volume, pair, buy_price, discount_pct, effective_rung_capital
                )
                txid = client.place_limit_order(pair, "buy", volume, buy_price, post_only=True)
                rung["state"] = "BUY_PLACED"
                rung["active_order_id"] = txid
                rung["last_buy_price"] = str(buy_price)
                rung["last_buy_volume"] = str(volume)
                rung["order_placed_at"] = int(time.time())
                available_cash = max(Decimal("0"), available_cash - effective_rung_capital)
                state_changed = True
                store.save_state(state)
            except Exception as e:
                logger.error("[%s Rung %s] Failed to place limit buy: %s", pair, rung_id, e)

        # -------------------------------------------------------------
        # 2. RUNG BUY_PLACED -> Check Fill -> Submit Limit Sell
        # -------------------------------------------------------------
        elif rung_state == "BUY_PLACED":
            txid = rung.get("active_order_id")
            if not txid:
                rung["state"] = "IDLE"
                state_changed = True
                store.save_state(state)
                continue

            order_info = client.query_order(txid)
            status = order_info.get("status")

            if status == "closed":
                actual_price = order_info["price"] if order_info.get("price", Decimal("0")) > 0 else Decimal(rung["last_buy_price"])
                actual_volume = order_info["vol_exec"] if order_info.get("vol_exec", Decimal("0")) > 0 else Decimal(rung["last_buy_volume"])

                # Target Sell = Buy Price * (1 + target_pct + round_trip_fee)
                total_markup = Decimal("1") + ((target_pct + ROUND_TRIP_FEE_PERCENT) / Decimal("100"))
                raw_sell_price = actual_price * total_markup
                sell_price = _round_decimal(raw_sell_price, pair_decimals, rounding=ROUND_HALF_UP)

                logger.info(
                    "[%s Rung %s] Buy filled @ £%s. Placing Limit Sell: %s %s @ £%s (+%s%%)",
                    pair, rung_id, actual_price, actual_volume, pair, sell_price, target_pct
                )
                sell_txid = client.place_limit_order(pair, "sell", actual_volume, sell_price, post_only=True)

                rung["state"] = "SELL_PLACED"
                rung["active_order_id"] = sell_txid
                rung["last_buy_price"] = str(actual_price)
                rung["last_buy_volume"] = str(actual_volume)
                rung["target_sell_price"] = str(sell_price)
                rung["applied_target_pct"] = str(target_pct)
                rung["order_placed_at"] = int(time.time())
                state_changed = True
                store.save_state(state)

                send_alert_fn(
                    f"[Kraken Micro-Trader] Rung {rung_id} Buy Filled: {pair} (+{target_pct}% Target)",
                    f"Rung {rung_id} on {pair} executed its limit buy!\n\n"
                    f"• Filled: {actual_volume} {pair[:3]} @ £{actual_price}\n"
                    f"• Cost: £{(actual_price * actual_volume):.2f}\n"
                    f"• Limit Sell Placed: £{sell_price} (+{target_pct}% target)\n"
                    f"• Sell Order ID: {sell_txid}",
                )

            elif status == "open":
                order_age_hours = (time.time() - float(rung.get("order_placed_at", time.time()))) / 3600
                order_buy_price = Decimal(str(rung.get("last_buy_price", "0")))
                order_vol = Decimal(str(rung.get("last_buy_volume", "0")))
                order_capital = order_buy_price * order_vol

                # 1. Asymmetric Stale Timeout
                stale_threshold = cfg.get("stale_hours", Decimal("24.0"))
                is_stale = order_age_hours > float(stale_threshold)

                # 2. Price Drift Detection: Has market rallied away above our limit buy?
                drift_threshold = cfg.get("drift_pct", Decimal("3.5"))
                has_drifted = False
                drift_pct = Decimal("0")
                if order_buy_price > Decimal("0") and best_bid > Decimal("0"):
                    drift_pct = ((best_bid - order_buy_price) / order_buy_price) * Decimal("100")
                    if drift_pct > drift_threshold:
                        has_drifted = True

                # 3. Capital Undersize Detection: Was order starved of capital (<60% target) but cash is now free?
                is_undersized = False
                if rung_capital > Decimal("0"):
                    capital_ratio = order_capital / rung_capital
                    if capital_ratio < UNDERSIZED_THRESHOLD_PERCENT and available_cash >= (rung_capital * Decimal("0.80")):
                        is_undersized = True

                cancel_reason = None
                if is_stale:
                    cancel_reason = f"Stale ({order_age_hours:.1f}h > {stale_threshold}h limit)"
                elif has_drifted:
                    cancel_reason = f"Price drift (Market +{drift_pct:.2f}% above order > {drift_threshold}% limit)"
                elif is_undersized:
                    cancel_reason = f"Undersized capital (£{order_capital:.2f} vs target £{rung_capital:.2f}) and cash is now available"

                if cancel_reason:
                    logger.info(
                        "[%s Rung %s] Auto-rebalancing buy order %s: %s. Canceling to re-quote at market.",
                        pair, rung_id, txid, cancel_reason
                    )
                    client.cancel_order(txid)
                    rung["state"] = "IDLE"
                    rung["active_order_id"] = None
                    state_changed = True
                    store.save_state(state)

            elif status in ("canceled", "expired"):
                logger.warning("[%s Rung %s] Buy order %s was %s. Resetting to IDLE.", pair, rung_id, txid, status)
                rung["state"] = "IDLE"
                rung["active_order_id"] = None
                state_changed = True
                store.save_state(state)

        # -------------------------------------------------------------
        # 3. RUNG SELL_PLACED -> Check Fill -> Realize Profit -> Reset
        # -------------------------------------------------------------
        elif rung_state == "SELL_PLACED":
            txid = rung.get("active_order_id")
            if not txid:
                rung["state"] = "IDLE"
                state_changed = True
                continue

            order_info = client.query_order(txid)
            status = order_info.get("status")

            if status == "closed":
                sell_price = order_info["price"] if order_info.get("price", Decimal("0")) > 0 else Decimal(rung["target_sell_price"])
                buy_price = Decimal(rung.get("last_buy_price", "0"))
                volume = Decimal(rung.get("last_buy_volume", "0"))

                gross_profit = (sell_price - buy_price) * volume
                fees = (buy_price * volume + sell_price * volume) * (ROUND_TRIP_FEE_PERCENT / Decimal("200"))
                net_profit = (gross_profit - fees).quantize(Decimal("0.0001"))

                total_trades = state.get("trades_completed", 0) + 1
                total_profit = (Decimal(state.get("total_realized_profit_gbp", "0.0")) + net_profit).quantize(Decimal("0.0001"))

                state["trades_completed"] = total_trades
                state["total_realized_profit_gbp"] = str(total_profit)

                logger.info(
                    "🎉 [%s Rung %s PROFIT BANKED] Net: +£%s | Total: +£%s (%d fills)",
                    pair, rung_id, net_profit, total_profit, total_trades
                )

                # Reset rung to IDLE to immediately re-enter on next cycle
                rung["state"] = "IDLE"
                rung["active_order_id"] = None
                state_changed = True
                store.save_state(state)

                send_alert_fn(
                    f"[Kraken Micro-Trader] 💰 Profit Banked on {pair} (+£{net_profit:.2f})",
                    f"Rung {rung_id} on {pair} hit its limit sell target!\n\n"
                    f"• Pair: {pair}\n"
                    f"• Net Profit Realized: +£{net_profit:.4f}\n"
                    f"• Lifetime Pair Profit: +£{total_profit:.4f} across {total_trades} cycles\n"
                    f"• Sold: {volume} {pair[:3]} @ £{sell_price}\n\n"
                    f"The rung has reset and will place a fresh buy order on the next iteration.",
                )

    if state_changed:
        store.save_state(state)

    return {
        "pair": pair,
        "version": "grid_v2",
        "rungs": rungs,
        "trades_completed": state.get("trades_completed", 0),
        "total_profit_gbp": state.get("total_realized_profit_gbp", "0.0"),
    }
