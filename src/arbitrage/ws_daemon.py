"""Real-time WebSocket Triangular Arbitrage Daemon for Kraken.

Subscribes to Kraken WebSocket v2 live ticker/orderbook streams,
evaluates 3-legged arbitrage in microseconds upon every tick,
and executes orders when net profit crosses the fee hurdle.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import logging
import os
import signal
import sys
import time
from typing import Any

import websockets

# Ensure src/ is on python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage.routes import TriangleRoute, PairInfo, PRIORITY_ASSETS
from arbitrage.capital import calculate_trade_capital
from arbitrage.scanner import ArbitrageOpportunity
from arbitrage.executor import ArbitrageExecutor
from kraken_client import KrakenClient

try:
    import boto3
except ImportError:
    boto3 = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kraken-arb-daemon")

WS_URL = "wss://ws.kraken.com/v2"
DEFAULT_MIN_PROFIT_PCT = Decimal("0.30")  # Minimum +0.30% net profit after fees
MAKER_FEE = Decimal("0.0025")
TAKER_FEE = Decimal("0.0040")


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


class WebSocketArbDaemon:
    """High-speed real-time triangular arbitrage daemon."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        trade_capital: Decimal = Decimal("30.00"),
        dry_run: bool = True,
        min_net_pct: Decimal = DEFAULT_MIN_PROFIT_PCT,
        execution_mode: str = "TAKER",
    ):
        if not api_key or not api_secret:
            from config import get_kraken_credentials
            try:
                self.api_key, self.api_secret = get_kraken_credentials()
            except Exception as e:
                logger.warning("Could not fetch Kraken credentials from SSM / env: %s", e)
                self.api_key, self.api_secret = "mock_key", "mock_secret"
        else:
            self.api_key = api_key
            self.api_secret = api_secret

        self.trade_capital = trade_capital
        self.dry_run = dry_run
        self.min_net_pct = min_net_pct
        self.execution_mode = execution_mode.upper()

        self.client = KrakenClient(api_key=self.api_key, api_secret=self.api_secret, dry_run=dry_run)
        self.executor = ArbitrageExecutor(
            client=self.client,
            dry_run=dry_run,
            min_net_pct=min_net_pct,
        )

        self.routes: list[TriangleRoute] = []
        self.pair_details: dict[str, PairInfo] = {}
        self.symbol_to_wsname: dict[str, str] = {}
        self.wsname_to_symbol: dict[str, str] = {}

        # Real-time best bid/ask cache: wsname -> {'bid': Decimal, 'ask': Decimal, 'timestamp': float}
        self.book_cache: dict[str, dict[str, Decimal]] = {}
        self.running = False

        # Alert throttling (prevent flooding email)
        self.last_alert_time: float = 0.0
        self.alert_cooldown_seconds: float = 900.0  # At most 1 alert every 15 mins for observation opportunities

    def _maybe_alert(
        self,
        direction: str,
        route: TriangleRoute,
        net_pct: Decimal,
        profit: Decimal,
        p1: Decimal,
        pc: Decimal,
        p3: Decimal,
    ) -> None:
        now = time.time()
        if (now - self.last_alert_time) >= self.alert_cooldown_seconds:
            self.last_alert_time = now
            send_sns_alert(
                subject=f"[Kraken Arb] {route.asset_a}-{route.asset_b} Opportunity (+{net_pct:.2f}%)",
                message=(
                    f"⚡ Kraken Real-Time Triangular Arbitrage Opportunity!\n\n"
                    f"Route: {route.id} ({direction})\n"
                    f"Net Spread: {net_pct:+.3f}% (after fees)\n"
                    f"Projected Net Profit: +£{profit} on £{self.trade_capital}\n"
                    f"Prices: Leg1: £{p1}, Cross: {pc}, Leg3: £{p3}\n"
                    f"Execution Mode: {'DRY RUN' if self.dry_run else 'LIVE'} ({self.execution_mode})\n"
                    f"Hosting: AWS EC2 (t4g.nano Dublin eu-west-1)\n"
                ),
            )

    def initialize_routes(self) -> list[str]:
        """Fetch pairs, map triangles, cache PairInfo, and return symbols for WS v2."""
        raw_pairs = self.client._request("/0/public/AssetPairs")
        gbp_assets: dict[str, str] = {}

        # 1. Map GBP assets and pair details
        for pair_name, info in raw_pairs.items():
            if info.get("status") != "online":
                continue
            wsname = info.get("wsname", "")
            altname = info.get("altname", pair_name)
            p_info = PairInfo(
                pair_id=pair_name,
                wsname=wsname,
                base=info.get("base", wsname.split("/")[0] if "/" in wsname else ""),
                quote=info.get("quote", "GBP"),
                ordermin=Decimal(str(info.get("ordermin", "0.01"))),
                pair_decimals=int(info.get("pair_decimals", 2)),
                lot_decimals=int(info.get("lot_decimals", 8)),
                altname=altname,
            )
            self.pair_details[wsname] = p_info
            self.pair_details[altname] = p_info
            self.pair_details[pair_name] = p_info

            if wsname.endswith("/GBP"):
                base = wsname.split("/")[0]
                gbp_assets[base] = wsname

        needed_symbols = set()
        routes = []

        # 2. Map cross pairs and form viable triangular routes
        for pair_name, info in raw_pairs.items():
            if info.get("status") != "online":
                continue
            wsname = info.get("wsname", "")
            if "/" not in wsname:
                continue
            base, quote = wsname.split("/")
            if base in gbp_assets and quote in gbp_assets:
                # Filter to priority / high-interest assets
                if base not in PRIORITY_ASSETS and quote not in PRIORITY_ASSETS:
                    continue

                r = TriangleRoute(
                    asset_a=base,
                    asset_b=quote,
                    leg1_pair=gbp_assets[base],
                    cross_pair=wsname,
                    leg3_pair=gbp_assets[quote],
                    cross_is_a_base=True,
                )
                routes.append(r)
                needed_symbols.add(gbp_assets[base])
                needed_symbols.add(wsname)
                needed_symbols.add(gbp_assets[quote])

        self.routes = routes
        symbols_list = sorted(list(needed_symbols))
        logger.info(
            "Mapped %d priority triangles across %d WebSocket symbols (Metadata cached for %d pairs).",
            len(routes),
            len(symbols_list),
            len(self.pair_details),
        )
        return symbols_list

    def handle_ticker_update(self, data_list: list[dict[str, Any]]) -> None:
        """Update in-memory price cache and evaluate impacted triangles."""
        impacted_symbols = set()
        for item in data_list:
            symbol = item.get("symbol")
            bid = item.get("bid")
            ask = item.get("ask")
            if symbol and bid and ask:
                self.book_cache[symbol] = {
                    "bid": Decimal(str(bid)),
                    "ask": Decimal(str(ask)),
                    "timestamp": Decimal(str(time.time())),
                }
                impacted_symbols.add(symbol)

        # Evaluate routes containing the updated symbols
        for route in self.routes:
            if (
                route.leg1_pair in impacted_symbols
                or route.cross_pair in impacted_symbols
                or route.leg3_pair in impacted_symbols
            ):
                self.evaluate_route(route)

    def evaluate_route(self, route: TriangleRoute) -> None:
        """Microsecond evaluation of forward and reverse cycles with live execution."""
        t1 = self.book_cache.get(route.leg1_pair)
        tc = self.book_cache.get(route.cross_pair)
        t3 = self.book_cache.get(route.leg3_pair)
        if not (t1 and tc and t3):
            return

        p1_bid, p1_ask = t1["bid"], t1["ask"]
        pc_bid, pc_ask = tc["bid"], tc["ask"]
        p3_bid, p3_ask = t3["bid"], t3["ask"]

        if p1_bid <= 0 or pc_bid <= 0 or p3_bid <= 0:
            return
        if p1_ask <= 0 or pc_ask <= 0 or p3_ask <= 0:
            return

        fee_factor_taker = (Decimal("1") - TAKER_FEE) ** 3
        fee_factor_maker = (Decimal("1") - MAKER_FEE) ** 3
        now_ts = int(time.time())

        # ---------------------------------------------------------------------
        # 1. TAKER EVALUATION (Crossing the spread, immediate fill or cancel)
        # ---------------------------------------------------------------------
        # Forward Taker: Buy A at ask, Sell A for B at bid, Sell B for GBP at bid
        fwd_taker_gross = (Decimal("1") / p1_ask) * pc_bid * p3_bid
        fwd_taker_net = fwd_taker_gross * fee_factor_taker
        fwd_taker_net_pct = (fwd_taker_net - Decimal("1")) * Decimal("100")

        if fwd_taker_net_pct >= self.min_net_pct:
            profit = (self.trade_capital * (fwd_taker_net - Decimal("1"))).quantize(Decimal("0.0001"))
            logger.info(
                "🚀 [TAKER DISLOCATION] FORWARD %s | Net: %+.3f%% (+£%s on £%s) | Leg1: £%s, Cross: %s, Leg3: £%s",
                route.id, fwd_taker_net_pct, profit, self.trade_capital, p1_ask, pc_bid, p3_bid
            )
            opp = ArbitrageOpportunity(
                route=route,
                direction="FORWARD",
                execution_type="TAKER",
                gross_multiplier=fwd_taker_gross,
                gross_profit_pct=(fwd_taker_gross - 1) * 100,
                net_multiplier=fwd_taker_net,
                net_profit_pct=fwd_taker_net_pct,
                expected_profit_gbp=profit,
                leg1_price=p1_ask,
                cross_price=pc_bid,
                leg3_price=p3_bid,
                timestamp=now_ts,
            )
            self.executor.execute_cycle(opp, self.trade_capital, pair_map=self.pair_details)
            return

        # Reverse Taker: Buy B at ask, Buy A with B at ask, Sell A for GBP at bid
        rev_taker_gross = (Decimal("1") / p3_ask) * (Decimal("1") / pc_ask) * p1_bid
        rev_taker_net = rev_taker_gross * fee_factor_taker
        rev_taker_net_pct = (rev_taker_net - Decimal("1")) * Decimal("100")

        if rev_taker_net_pct >= self.min_net_pct:
            profit = (self.trade_capital * (rev_taker_net - Decimal("1"))).quantize(Decimal("0.0001"))
            logger.info(
                "🚀 [TAKER DISLOCATION] REVERSE %s | Net: %+.3f%% (+£%s on £%s) | Leg3: £%s, Cross: %s, Leg1: £%s",
                route.id, rev_taker_net_pct, profit, self.trade_capital, p3_ask, pc_ask, p1_bid
            )
            opp = ArbitrageOpportunity(
                route=route,
                direction="REVERSE",
                execution_type="TAKER",
                gross_multiplier=rev_taker_gross,
                gross_profit_pct=(rev_taker_gross - 1) * 100,
                net_multiplier=rev_taker_net,
                net_profit_pct=rev_taker_net_pct,
                expected_profit_gbp=profit,
                leg1_price=p1_bid,
                cross_price=pc_ask,
                leg3_price=p3_ask,
                timestamp=now_ts,
            )
            self.executor.execute_cycle(opp, self.trade_capital, pair_map=self.pair_details)
            return

        # ---------------------------------------------------------------------
        # 2. MAKER SPREAD OBSERVATION & EXECUTION
        # ---------------------------------------------------------------------
        # Forward Maker: Buy A at bid, Sell A for B at ask, Sell B for GBP at ask
        fwd_maker_gross = (Decimal("1") / p1_bid) * pc_ask * p3_ask
        fwd_maker_net = fwd_maker_gross * fee_factor_maker
        fwd_maker_net_pct = (fwd_maker_net - Decimal("1")) * Decimal("100")

        if fwd_maker_net_pct >= self.min_net_pct:
            profit = (self.trade_capital * (fwd_maker_net - Decimal("1"))).quantize(Decimal("0.0001"))
            logger.info(
                "⚡ [MAKER SPREAD] FORWARD %s-%s | Net: %+.3f%% (+£%s on £%s) | Leg1: £%s, Cross: %s, Leg3: £%s",
                route.asset_a, route.asset_b, fwd_maker_net_pct, profit, self.trade_capital, p1_bid, pc_ask, p3_ask
            )

            if self.execution_mode == "MAKER":
                opp = ArbitrageOpportunity(
                    route=route,
                    direction="FORWARD",
                    execution_type="MAKER",
                    gross_multiplier=fwd_maker_gross,
                    gross_profit_pct=(fwd_maker_gross - 1) * 100,
                    net_multiplier=fwd_maker_net,
                    net_profit_pct=fwd_maker_net_pct,
                    expected_profit_gbp=profit,
                    leg1_price=p1_bid,
                    cross_price=pc_ask,
                    leg3_price=p3_ask,
                    timestamp=now_ts,
                )
                self.executor.execute_cycle(opp, self.trade_capital, pair_map=self.pair_details)
                return

        # Reverse Maker: Buy B at bid, Buy A with B at bid, Sell A for GBP at ask
        rev_maker_gross = (Decimal("1") / p3_bid) * (Decimal("1") / pc_bid) * p1_ask
        rev_maker_net = rev_maker_gross * fee_factor_maker
        rev_maker_net_pct = (rev_maker_net - Decimal("1")) * Decimal("100")

        if rev_maker_net_pct >= self.min_net_pct:
            profit = (self.trade_capital * (rev_maker_net - Decimal("1"))).quantize(Decimal("0.0001"))
            logger.info(
                "⚡ [MAKER SPREAD] REVERSE %s-%s | Net: %+.3f%% (+£%s on £%s) | Leg3: £%s, Cross: %s, Leg1: £%s",
                route.asset_a, route.asset_b, rev_maker_net_pct, profit, self.trade_capital, p3_bid, pc_bid, p1_ask
            )

            if self.execution_mode == "MAKER":
                opp = ArbitrageOpportunity(
                    route=route,
                    direction="REVERSE",
                    execution_type="MAKER",
                    gross_multiplier=rev_maker_gross,
                    gross_profit_pct=(rev_maker_gross - 1) * 100,
                    net_multiplier=rev_maker_net,
                    net_profit_pct=rev_maker_net_pct,
                    expected_profit_gbp=profit,
                    leg1_price=p1_ask,
                    cross_price=pc_bid,
                    leg3_price=p3_bid,
                    timestamp=now_ts,
                )
                self.executor.execute_cycle(opp, self.trade_capital, pair_map=self.pair_details)

    async def run(self) -> None:
        """Main WebSocket async event loop with auto-reconnect."""
        symbols = self.initialize_routes()
        self.running = True

        # Send initial boot alert
        send_sns_alert(
            subject="[Kraken Micro-Trader] Triangular Arbitrage Daemon Active",
            message=(
                f"Kraken WebSocket Triangular Arbitrage Daemon is now running on AWS EC2 ({os.getenv('AWS_REGION', 'eu-west-1')}).\n\n"
                f"Subscribed Symbols: {len(symbols)} pairs across priority markets\n"
                f"Allocated Trade Capital: £{self.trade_capital}\n"
                f"Min Net Hurdle: +{self.min_net_pct}%\n"
                f"Execution Mode: {'DRY RUN' if self.dry_run else 'LIVE'} ({self.execution_mode})\n"
                f"Latency: Direct connection to Kraken matching engines (<15ms in Dublin)."
            ),
        )

        while self.running:
            try:
                logger.info("Connecting to Kraken WebSocket v2 at %s...", WS_URL)
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("WebSocket Connected! Subscribing to %d ticker feeds...", len(symbols))
                    sub_msg = {
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": symbols,
                        },
                    }
                    await ws.send(json.dumps(sub_msg))

                    while self.running:
                        raw_msg = await ws.recv()
                        msg = json.loads(raw_msg)
                        if msg.get("channel") == "ticker":
                            data = msg.get("data", [])
                            self.handle_ticker_update(data)
                        elif msg.get("channel") == "heartbeat":
                            pass

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("WebSocket connection closed: %s. Reconnecting in 3s...", e)
                await asyncio.sleep(3)
            except Exception as e:
                logger.error("WebSocket error: %s. Reconnecting in 5s...", e, exc_info=True)
                await asyncio.sleep(5)


def main():
    dry_run = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
    capital = Decimal(os.getenv("ARB_CAPITAL_GBP", "30.00"))
    min_net = Decimal(os.getenv("ARB_MIN_NET_PCT", "0.30"))
    exec_mode = os.getenv("ARB_EXECUTION_MODE", "TAKER")

    logger.info(
        "Initializing WebSocket Arb Daemon (DRY_RUN=%s, Trade Capital=£%s, Min Net Hurdle=+%s%%, Mode=%s)",
        dry_run, capital, min_net, exec_mode
    )
    daemon = WebSocketArbDaemon(
        trade_capital=capital,
        dry_run=dry_run,
        min_net_pct=min_net,
        execution_mode=exec_mode,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal (%s) received. Exiting...", signum)
        daemon.running = False
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        loop.run_until_complete(daemon.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Daemon stopped.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
