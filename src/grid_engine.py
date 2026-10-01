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
    DYNAMIC_TARGET_ENABLED,
    MAX_PROFIT_TARGET_PERCENT,
    MAX_STALE_HOURS,
    MIN_FEE_BUFFER_GBP,
    MIN_PROFIT_TARGET_PERCENT,
    PROFIT_TARGET_PERCENT,
    ROUND_TRIP_FEE_PERCENT,
)
from kraken_client import KrakenClient
from state_store import StateStore
from volatility import compute_volatility_target

logger = logging.getLogger(__name__)

# Default specifications for a 2-Rung Micro-Grid
DEFAULT_RUNGS_CONFIG = [
    {
        "rung_id": "1",
        "weight": Decimal("0.50"),        # 50% of pair capital (~£12.50)
        "discount_pct": Decimal("0.60"),  # 0.60% below best bid (light noise)
        "target_pct": Decimal("1.00"),    # +1.00% target (+0.50% net after Maker fees)
    },
    {
        "rung_id": "2",
        "weight": Decimal("0.50"),        # 50% of pair capital (~£12.50)
        "discount_pct": Decimal("1.40"),  # 1.40% below best bid (deeper intraday dip)
        "target_pct": Decimal("1.20"),    # +1.20% target (+0.70% net after Maker fees)
    },
]


def _round_decimal(val: Decimal, places: int, rounding=ROUND_HALF_UP) -> Decimal:
    """Format decimal to specific decimal places."""
    q = Decimal(10) ** -places
    return val.quantize(q, rounding=rounding)


def initialize_grid_state(pair: str) -> dict[str, Any]:
    """Create an empty 2-rung micro-grid state structure."""
    return {
        "bot_id": f"{pair}-PINGPONG",
        "version": "grid_v2",
        "trades_completed": 0,
        "total_realized_profit_gbp": "0.0000",
        "rungs": {
            "1": {
                "state": "IDLE",
                "active_order_id": None,
                "discount_pct": str(DEFAULT_RUNGS_CONFIG[0]["discount_pct"]),
                "target_pct": str(DEFAULT_RUNGS_CONFIG[0]["target_pct"]),
                "last_buy_price": "0.0",
                "last_buy_volume": "0.0",
                "target_sell_price": "0.0",
                "order_placed_at": 0,
            },
            "2": {
                "state": "IDLE",
                "active_order_id": None,
                "discount_pct": str(DEFAULT_RUNGS_CONFIG[1]["discount_pct"]),
                "target_pct": str(DEFAULT_RUNGS_CONFIG[1]["target_pct"]),
                "last_buy_price": "0.0",
                "last_buy_volume": "0.0",
                "target_sell_price": "0.0",
                "order_placed_at": 0,
            },
        },
    }


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

    # Protect fee buffer from available free cash
    balances = client.get_balance()
    free_cash = balances.get("ZGBP", balances.get("GBP", Decimal("0")))
    available_cash = max(Decimal("0"), free_cash - MIN_FEE_BUFFER_GBP)

    # Optional dynamic volatility scaling for profit target
    dynamic_markup = Decimal("0.0")
    if DYNAMIC_TARGET_ENABLED:
        try:
            candles = client.get_ohlc(pair, interval=60)
            dyn_pct, _ = compute_volatility_target(
                candles,
                fallback_percent=PROFIT_TARGET_PERCENT,
                min_target=MIN_PROFIT_TARGET_PERCENT,
                max_target=MAX_PROFIT_TARGET_PERCENT,
            )
            # Scaling bonus if market volatility is elevated
            dynamic_markup = max(Decimal("0.0"), dyn_pct - PROFIT_TARGET_PERCENT)
        except Exception as e:
            logger.debug("[%s] Dynamic target calculation fallback: %s", pair, e)

    state_changed = False

    for cfg in DEFAULT_RUNGS_CONFIG:
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
                if order_age_hours > float(MAX_STALE_HOURS):
                    logger.info("[%s Rung %s] Buy order %s stale (%.1f h). Canceling to re-quote.", pair, rung_id, txid, order_age_hours)
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
