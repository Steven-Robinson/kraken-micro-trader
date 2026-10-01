"""Execution engine for Kraken triangular arbitrage with atomic safeguards."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import logging
import os
import time
from typing import Any

from arbitrage.routes import TriangleRoute, PairInfo
from arbitrage.scanner import ArbitrageOpportunity
from kraken_client import KrakenClient, KrakenAPIError

try:
    import boto3
except ImportError:
    boto3 = None

logger = logging.getLogger(__name__)

DEFAULT_MIN_NET_PCT = Decimal("0.30")  # Minimum +0.30% net profit after fees
DEFAULT_MAX_SLIPPAGE_PCT = Decimal("0.20")  # Max 0.20% slippage on IOC limit orders
DEFAULT_COOLDOWN_SECONDS = 60.0


def send_sns_alert(subject: str, message: str) -> None:
    """Send trade/status alert via Amazon SNS if configured."""
    topic_arn = os.getenv("SNS_TOPIC_ARN")
    if not topic_arn or boto3 is None:
        return
    try:
        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "eu-west-1"))
        sns = boto3.client("sns", region_name=region)
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=message,
        )
        logger.info("Sent SNS alert: %s", subject)
    except Exception as e:
        logger.warning("Failed to send SNS alert: %s", e)


class ArbitrageExecutor:
    """Safely executes 3-legged triangular arbitrage loops on Kraken with atomic safeguards."""

    def __init__(
        self,
        client: KrakenClient,
        dry_run: bool = True,
        min_net_pct: Decimal = DEFAULT_MIN_NET_PCT,
        max_slippage_pct: Decimal = DEFAULT_MAX_SLIPPAGE_PCT,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ):
        self.client = client
        self.dry_run = dry_run
        self.min_net_pct = min_net_pct
        self.max_slippage_pct = max_slippage_pct
        self.cooldown_seconds = cooldown_seconds
        self.last_execution_time: float = 0.0

    def _resolve_pair_info(
        self, pair_key: str, pair_map: dict[str, PairInfo] | None = None
    ) -> PairInfo:
        """Resolve pair metadata from cached pair_map or client lookup."""
        if pair_map and pair_key in pair_map:
            return pair_map[pair_key]

        raw = self.client.get_pair_info(pair_key)
        return PairInfo(
            pair_id=pair_key,
            wsname=raw.get("wsname", pair_key),
            base=raw.get("base", ""),
            quote=raw.get("quote", ""),
            ordermin=Decimal(str(raw.get("ordermin", "0.01"))),
            pair_decimals=int(raw.get("pair_decimals", 2)),
            lot_decimals=int(raw.get("lot_decimals", 4)),
            altname=raw.get("altname", pair_key),
        )

    def execute_cycle(
        self,
        opp: ArbitrageOpportunity,
        trade_capital_gbp: Decimal,
        pair_map: dict[str, PairInfo] | None = None,
    ) -> dict[str, Any]:
        """Execute a triangular arbitrage cycle with atomic safety.

        Args:
            opp: The identified arbitrage opportunity.
            trade_capital_gbp: Scaled GBP amount to deploy (e.g. £30.00).
            pair_map: Optional mapping of wsname/altname -> PairInfo.

        Returns:
            Dict containing execution results, filled prices, and realized profit.
        """
        route = opp.route
        start_time = time.time()
        now = start_time

        # 1. Cooldown & Hurdle Check
        if (now - self.last_execution_time) < self.cooldown_seconds:
            remaining = round(self.cooldown_seconds - (now - self.last_execution_time), 1)
            logger.info("Arbitrage cycle skipped: Execution cooldown active (%ss remaining).", remaining)
            return {"status": "cooldown_active", "remaining_seconds": remaining}

        if opp.net_profit_pct < self.min_net_pct:
            logger.info(
                "Arbitrage cycle skipped: Net profit %+.3f%% is below minimum hurdle +%s%%.",
                opp.net_profit_pct,
                self.min_net_pct,
            )
            return {"status": "below_hurdle", "net_profit_pct": str(opp.net_profit_pct)}

        if trade_capital_gbp < Decimal("5.00"):
            logger.warning("Trade capital £%s is below minimum £5.00. Aborting.", trade_capital_gbp)
            return {"status": "capital_too_small"}

        # 2. Pre-flight Balance Verification (Live Mode)
        if not self.dry_run:
            try:
                balances = self.client.get_balance()
                free_gbp = balances.get("ZGBP", Decimal("0"))
                if free_gbp < trade_capital_gbp:
                    logger.error(
                        "Insufficient free GBP cash (£%s < £%s). Arbitrage loop aborted.",
                        free_gbp,
                        trade_capital_gbp,
                    )
                    return {"status": "insufficient_cash", "free_gbp": str(free_gbp)}
            except Exception as e:
                logger.error("Failed to verify account balance before arbitrage: %s", e)
                return {"status": "balance_check_failed", "error": str(e)}

        logger.info(
            "Initiating Triangular Arbitrage [%s] %s (Direction: %s, Capital: £%s, Expected Net: +£%s / %+.3f%%)",
            "DRY RUN" if self.dry_run else "LIVE",
            route.id,
            opp.direction,
            trade_capital_gbp,
            opp.expected_profit_gbp,
            opp.net_profit_pct,
        )

        # 3. Resolve Metadata for all 3 pairs
        leg1_info = self._resolve_pair_info(route.leg1_pair, pair_map)
        cross_info = self._resolve_pair_info(route.cross_pair, pair_map)
        leg3_info = self._resolve_pair_info(route.leg3_pair, pair_map)

        if opp.direction == "FORWARD":
            return self._execute_forward(
                opp=opp,
                trade_capital_gbp=trade_capital_gbp,
                leg1_info=leg1_info,
                cross_info=cross_info,
                leg3_info=leg3_info,
                start_time=start_time,
            )
        else:
            return self._execute_reverse(
                opp=opp,
                trade_capital_gbp=trade_capital_gbp,
                leg1_info=leg1_info,
                cross_info=cross_info,
                leg3_info=leg3_info,
                start_time=start_time,
            )

    def _execute_forward(
        self,
        opp: ArbitrageOpportunity,
        trade_capital_gbp: Decimal,
        leg1_info: PairInfo,
        cross_info: PairInfo,
        leg3_info: PairInfo,
        start_time: float,
    ) -> dict[str, Any]:
        """Forward Cycle: GBP -> Asset A (Leg 1) -> Asset B (Cross) -> GBP (Leg 3)."""
        route = opp.route
        slippage_factor = Decimal("1") + (self.max_slippage_pct / Decimal("100"))

        # --- LEG 1: Buy Asset A with GBP ---
        target_p1 = opp.leg1_price
        limit_p1 = (target_p1 * slippage_factor).quantize(
            Decimal("1e-" + str(leg1_info.pair_decimals)), rounding=ROUND_HALF_UP
        )
        vol_1 = (trade_capital_gbp / target_p1).quantize(
            Decimal("1e-" + str(leg1_info.lot_decimals)), rounding=ROUND_DOWN
        )

        if vol_1 < leg1_info.ordermin:
            logger.warning("Leg 1 volume %s %s is below ordermin %s. Aborting.", vol_1, leg1_info.base, leg1_info.ordermin)
            return {"status": "ordermin_not_met", "leg": 1}

        try:
            txid_1 = self.client.place_order(
                pair=leg1_info.altname,
                side="buy",
                ordertype="limit",
                volume=vol_1,
                price=limit_p1,
                time_in_force="IOC",
            )
            # Brief check on fill
            res_1 = self.client.query_order(txid_1)
            vol_1_filled = res_1.get("vol_exec", Decimal("0"))
            cost_gbp_1 = res_1.get("cost", trade_capital_gbp)
        except Exception as e:
            logger.error("Leg 1 order placement failed: %s", e)
            return {"status": "leg1_error", "error": str(e)}

        if vol_1_filled <= Decimal("0"):
            logger.info("Leg 1 did not fill on %s (IOC cancelled). Clean abort with £0 loss.", leg1_info.altname)
            return {"status": "leg1_unfilled", "route": route.id}

        logger.info("Leg 1 Filled! Received %s %s for £%s (txid: %s)", vol_1_filled, leg1_info.base, cost_gbp_1, txid_1)

        # --- LEG 2: Sell Asset A for Asset B on Cross Pair ---
        vol_2 = vol_1_filled.quantize(
            Decimal("1e-" + str(cross_info.lot_decimals)), rounding=ROUND_DOWN
        )
        target_pc = opp.cross_price
        limit_pc = (target_pc * (Decimal("1") - (self.max_slippage_pct / Decimal("100")))).quantize(
            Decimal("1e-" + str(cross_info.pair_decimals)), rounding=ROUND_DOWN
        )

        try:
            txid_2 = self.client.place_order(
                pair=cross_info.altname,
                side="sell",
                ordertype="limit",
                volume=vol_2,
                price=limit_pc,
                time_in_force="IOC",
            )
            res_2 = self.client.query_order(txid_2)
            vol_2_filled = res_2.get("vol_exec", Decimal("0"))
            proceeds_b = res_2.get("cost", Decimal("0"))
        except Exception as e:
            logger.error("Leg 2 order failed: %s. Triggering EMERGENCY RESCUE on %s...", e, leg1_info.altname)
            rescue_res = self._emergency_liquidate(leg1_info.altname, "sell", vol_1_filled, leg1_info.lot_decimals)
            return {"status": "emergency_rescued_leg1", "error": str(e), "rescue": rescue_res}

        if vol_2_filled <= Decimal("0") or proceeds_b <= Decimal("0"):
            logger.warning("Leg 2 IOC failed to fill on %s. Triggering EMERGENCY RESCUE on %s...", cross_info.altname, leg1_info.altname)
            rescue_res = self._emergency_liquidate(leg1_info.altname, "sell", vol_1_filled, leg1_info.lot_decimals)
            return {"status": "emergency_rescued_leg1", "rescue": rescue_res}

        logger.info("Leg 2 Filled! Sold %s %s -> Received %s %s (txid: %s)", vol_2_filled, route.asset_a, proceeds_b, route.asset_b, txid_2)

        # --- LEG 3: Sell Asset B for GBP on Leg 3 Pair ---
        vol_3 = proceeds_b.quantize(
            Decimal("1e-" + str(leg3_info.lot_decimals)), rounding=ROUND_DOWN
        )
        target_p3 = opp.leg3_price
        limit_p3 = (target_p3 * (Decimal("1") - (self.max_slippage_pct / Decimal("100")))).quantize(
            Decimal("1e-" + str(leg3_info.pair_decimals)), rounding=ROUND_DOWN
        )

        try:
            txid_3 = self.client.place_order(
                pair=leg3_info.altname,
                side="sell",
                ordertype="limit",
                volume=vol_3,
                price=limit_p3,
                time_in_force="IOC",
            )
            res_3 = self.client.query_order(txid_3)
            vol_3_filled = res_3.get("vol_exec", Decimal("0"))
            final_gbp = res_3.get("cost", Decimal("0"))
        except Exception as e:
            logger.error("Leg 3 order failed: %s. Triggering EMERGENCY RESCUE on %s...", e, leg3_info.altname)
            rescue_res = self._emergency_liquidate(leg3_info.altname, "sell", proceeds_b, leg3_info.lot_decimals)
            return {"status": "emergency_rescued_leg2", "error": str(e), "rescue": rescue_res}

        if vol_3_filled <= Decimal("0") or final_gbp <= Decimal("0"):
            logger.warning("Leg 3 IOC failed to fill on %s. Triggering EMERGENCY MARKET EXIT...", leg3_info.altname)
            rescue_res = self._emergency_liquidate(leg3_info.altname, "sell", proceeds_b, leg3_info.lot_decimals)
            final_gbp = rescue_res.get("cost", cost_gbp_1)

        # --- Cycle Successful Completion ---
        elapsed = time.time() - start_time
        net_profit_gbp = (final_gbp - cost_gbp_1).quantize(Decimal("0.0001"))
        net_profit_pct = (((final_gbp / cost_gbp_1) - Decimal("1")) * Decimal("100")).quantize(Decimal("0.001"))
        self.last_execution_time = time.time()

        logger.info(
            "🎉 [ARBITRAGE COMPLETE] %s FORWARD: Started with £%s -> Ended with £%s | Realized Net: %+.4f GBP (%+.3f%%) in %.2fs",
            route.id,
            cost_gbp_1,
            final_gbp,
            net_profit_gbp,
            net_profit_pct,
            elapsed,
        )

        send_sns_alert(
            subject=f"[Kraken Arb] Trade Executed: {route.id} (+£{net_profit_gbp})",
            message=(
                f"✅ Kraken Triangular Arbitrage Cycle Completed!\n\n"
                f"Route: {route.id} (FORWARD)\n"
                f"Capital Deployed: £{cost_gbp_1}\n"
                f"Final Returned: £{final_gbp}\n"
                f"Realized Net Profit: {net_profit_gbp:+.4f} GBP ({net_profit_pct:+.3f}%)\n"
                f"Execution Latency: {elapsed:.2f} seconds\n"
                f"Leg 1: {txid_1} ({leg1_info.altname})\n"
                f"Leg 2: {txid_2} ({cross_info.altname})\n"
                f"Leg 3: {txid_3} ({leg3_info.altname})\n"
                f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            ),
        )

        return {
            "status": "success",
            "route": route.id,
            "direction": "FORWARD",
            "starting_gbp": str(cost_gbp_1),
            "ending_gbp": str(final_gbp),
            "net_profit_gbp": str(net_profit_gbp),
            "net_profit_pct": str(net_profit_pct),
            "elapsed_seconds": round(elapsed, 2),
            "leg1_txid": txid_1,
            "leg2_txid": txid_2,
            "leg3_txid": txid_3,
        }

    def _execute_reverse(
        self,
        opp: ArbitrageOpportunity,
        trade_capital_gbp: Decimal,
        leg1_info: PairInfo,
        cross_info: PairInfo,
        leg3_info: PairInfo,
        start_time: float,
    ) -> dict[str, Any]:
        """Reverse Cycle: GBP -> Asset B (Leg 3) -> Asset A (Cross) -> GBP (Leg 1)."""
        route = opp.route
        slippage_factor = Decimal("1") + (self.max_slippage_pct / Decimal("100"))

        # --- LEG 1: Buy Asset B with GBP on leg3_pair ---
        target_p3 = opp.leg3_price
        limit_p3 = (target_p3 * slippage_factor).quantize(
            Decimal("1e-" + str(leg3_info.pair_decimals)), rounding=ROUND_HALF_UP
        )
        vol_1 = (trade_capital_gbp / target_p3).quantize(
            Decimal("1e-" + str(leg3_info.lot_decimals)), rounding=ROUND_DOWN
        )

        if vol_1 < leg3_info.ordermin:
            logger.warning("Leg 1 volume %s %s is below ordermin %s. Aborting.", vol_1, leg3_info.base, leg3_info.ordermin)
            return {"status": "ordermin_not_met", "leg": 1}

        try:
            txid_1 = self.client.place_order(
                pair=leg3_info.altname,
                side="buy",
                ordertype="limit",
                volume=vol_1,
                price=limit_p3,
                time_in_force="IOC",
            )
            res_1 = self.client.query_order(txid_1)
            vol_1_filled = res_1.get("vol_exec", Decimal("0"))
            cost_gbp_1 = res_1.get("cost", trade_capital_gbp)
        except Exception as e:
            logger.error("Leg 1 order placement failed: %s", e)
            return {"status": "leg1_error", "error": str(e)}

        if vol_1_filled <= Decimal("0"):
            logger.info("Leg 1 did not fill on %s (IOC cancelled). Clean abort with £0 loss.", leg3_info.altname)
            return {"status": "leg1_unfilled", "route": route.id}

        logger.info("Leg 1 Filled! Received %s %s for £%s (txid: %s)", vol_1_filled, leg3_info.base, cost_gbp_1, txid_1)

        # --- LEG 2: Buy Asset A using Asset B on Cross Pair ---
        target_pc = opp.cross_price
        limit_pc = (target_pc * slippage_factor).quantize(
            Decimal("1e-" + str(cross_info.pair_decimals)), rounding=ROUND_HALF_UP
        )
        vol_2 = (vol_1_filled / target_pc).quantize(
            Decimal("1e-" + str(cross_info.lot_decimals)), rounding=ROUND_DOWN
        )

        try:
            txid_2 = self.client.place_order(
                pair=cross_info.altname,
                side="buy",
                ordertype="limit",
                volume=vol_2,
                price=limit_pc,
                time_in_force="IOC",
            )
            res_2 = self.client.query_order(txid_2)
            vol_2_filled = res_2.get("vol_exec", Decimal("0"))
        except Exception as e:
            logger.error("Leg 2 order failed: %s. Triggering EMERGENCY RESCUE on %s...", e, leg3_info.altname)
            rescue_res = self._emergency_liquidate(leg3_info.altname, "sell", vol_1_filled, leg3_info.lot_decimals)
            return {"status": "emergency_rescued_leg3", "error": str(e), "rescue": rescue_res}

        if vol_2_filled <= Decimal("0"):
            logger.warning("Leg 2 IOC failed to fill on %s. Triggering EMERGENCY RESCUE on %s...", cross_info.altname, leg3_info.altname)
            rescue_res = self._emergency_liquidate(leg3_info.altname, "sell", vol_1_filled, leg3_info.lot_decimals)
            return {"status": "emergency_rescued_leg3", "rescue": rescue_res}

        logger.info("Leg 2 Filled! Bought %s %s on %s (txid: %s)", vol_2_filled, route.asset_a, cross_info.altname, txid_2)

        # --- LEG 3: Sell Asset A for GBP on Leg 1 Pair ---
        vol_3 = vol_2_filled.quantize(
            Decimal("1e-" + str(leg1_info.lot_decimals)), rounding=ROUND_DOWN
        )
        target_p1 = opp.leg1_price
        limit_p1 = (target_p1 * (Decimal("1") - (self.max_slippage_pct / Decimal("100")))).quantize(
            Decimal("1e-" + str(leg1_info.pair_decimals)), rounding=ROUND_DOWN
        )

        try:
            txid_3 = self.client.place_order(
                pair=leg1_info.altname,
                side="sell",
                ordertype="limit",
                volume=vol_3,
                price=limit_p1,
                time_in_force="IOC",
            )
            res_3 = self.client.query_order(txid_3)
            vol_3_filled = res_3.get("vol_exec", Decimal("0"))
            final_gbp = res_3.get("cost", Decimal("0"))
        except Exception as e:
            logger.error("Leg 3 order failed: %s. Triggering EMERGENCY RESCUE on %s...", e, leg1_info.altname)
            rescue_res = self._emergency_liquidate(leg1_info.altname, "sell", vol_2_filled, leg1_info.lot_decimals)
            return {"status": "emergency_rescued_leg1", "error": str(e), "rescue": rescue_res}

        if vol_3_filled <= Decimal("0") or final_gbp <= Decimal("0"):
            logger.warning("Leg 3 IOC failed to fill on %s. Triggering EMERGENCY MARKET EXIT...", leg1_info.altname)
            rescue_res = self._emergency_liquidate(leg1_info.altname, "sell", vol_2_filled, leg1_info.lot_decimals)
            final_gbp = rescue_res.get("cost", cost_gbp_1)

        # --- Cycle Successful Completion ---
        elapsed = time.time() - start_time
        net_profit_gbp = (final_gbp - cost_gbp_1).quantize(Decimal("0.0001"))
        net_profit_pct = (((final_gbp / cost_gbp_1) - Decimal("1")) * Decimal("100")).quantize(Decimal("0.001"))
        self.last_execution_time = time.time()

        logger.info(
            "🎉 [ARBITRAGE COMPLETE] %s REVERSE: Started with £%s -> Ended with £%s | Realized Net: %+.4f GBP (%+.3f%%) in %.2fs",
            route.id,
            cost_gbp_1,
            final_gbp,
            net_profit_gbp,
            net_profit_pct,
            elapsed,
        )

        send_sns_alert(
            subject=f"[Kraken Arb] Trade Executed: {route.id} (+£{net_profit_gbp})",
            message=(
                f"✅ Kraken Triangular Arbitrage Cycle Completed!\n\n"
                f"Route: {route.id} (REVERSE)\n"
                f"Capital Deployed: £{cost_gbp_1}\n"
                f"Final Returned: £{final_gbp}\n"
                f"Realized Net Profit: {net_profit_gbp:+.4f} GBP ({net_profit_pct:+.3f}%)\n"
                f"Execution Latency: {elapsed:.2f} seconds\n"
                f"Leg 1: {txid_1} ({leg3_info.altname})\n"
                f"Leg 2: {txid_2} ({cross_info.altname})\n"
                f"Leg 3: {txid_3} ({leg1_info.altname})\n"
                f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            ),
        )

        return {
            "status": "success",
            "route": route.id,
            "direction": "REVERSE",
            "starting_gbp": str(cost_gbp_1),
            "ending_gbp": str(final_gbp),
            "net_profit_gbp": str(net_profit_gbp),
            "net_profit_pct": str(net_profit_pct),
            "elapsed_seconds": round(elapsed, 2),
            "leg1_txid": txid_1,
            "leg2_txid": txid_2,
            "leg3_txid": txid_3,
        }

    def _emergency_liquidate(
        self,
        pair: str,
        side: str,
        volume: Decimal,
        lot_decimals: int,
    ) -> dict[str, Any]:
        """Emergency safeguard: Market-liquidates stranded asset back to GBP immediately."""
        safe_vol = volume.quantize(Decimal("1e-" + str(lot_decimals)), rounding=ROUND_DOWN)
        logger.critical(
            "🚨 [EMERGENCY LIQUIDATION INITIATED] Placing market %s for %s %s...",
            side,
            safe_vol,
            pair,
        )
        try:
            txid = self.client.place_order(
                pair=pair,
                side=side,
                ordertype="market",
                volume=safe_vol,
            )
            res = self.client.query_order(txid)
            cost = res.get("cost", Decimal("0"))
            send_sns_alert(
                subject=f"[Kraken Arb CRITICAL] Emergency Market Liquidation on {pair}",
                message=(
                    f"⚠️ An emergency liquidation was triggered to protect capital!\n\n"
                    f"Pair: {pair}\n"
                    f"Action: Market {side.upper()} {safe_vol}\n"
                    f"Proceeds: £{cost}\n"
                    f"Transaction ID: {txid}\n"
                    f"Status: Safe GBP cash restored.\n"
                ),
            )
            return {"status": "rescued", "txid": txid, "cost": cost}
        except Exception as e:
            logger.critical("Failed to execute emergency liquidation on %s: %s", pair, e)
            send_sns_alert(
                subject=f"[Kraken Arb CRITICAL ERROR] Emergency Liquidation FAILED on {pair}",
                message=(
                    f"❌ Emergency market exit failed for {pair}!\n"
                    f"Action: {side.upper()} {safe_vol}\n"
                    f"Error: {e}\n"
                    f"Immediate manual inspection required in Kraken Pro!\n"
                ),
            )
            return {"status": "failed", "error": str(e)}
