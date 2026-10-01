"""Triangular arbitrage scanner and opportunity detector for Kraken."""

from dataclasses import dataclass
from decimal import Decimal
import logging
import time
from typing import Any

from arbitrage.routes import TriangleRoute, PairInfo, PRIORITY_ASSETS

logger = logging.getLogger(__name__)

# Kraken default Pro fees
DEFAULT_MAKER_FEE = Decimal("0.0025")  # 0.25%
DEFAULT_TAKER_FEE = Decimal("0.0040")  # 0.40%


@dataclass
class ArbitrageOpportunity:
    route: TriangleRoute
    direction: str          # "FORWARD" (GBP -> A -> B -> GBP) or "REVERSE" (GBP -> B -> A -> GBP)
    execution_type: str     # "MAKER" or "TAKER"
    gross_multiplier: Decimal
    gross_profit_pct: Decimal
    net_multiplier: Decimal
    net_profit_pct: Decimal
    expected_profit_gbp: Decimal
    leg1_price: Decimal
    cross_price: Decimal
    leg3_price: Decimal
    timestamp: int

    def __str__(self) -> str:
        arrow = "->" if self.direction == "FORWARD" else "<-"
        return (
            f"[{self.execution_type}] {self.direction} GBP {arrow} {self.route.asset_a} {arrow} {self.route.asset_b} {arrow} GBP "
            f"| Gross: {self.gross_profit_pct:+.3f}% | Net: {self.net_profit_pct:+.3f}% | Est. Net: +£{self.expected_profit_gbp:.4f}"
        )


class ArbitrageScanner:
    """Discovers and evaluates live triangular arbitrage opportunities across Kraken order books."""

    def __init__(
        self,
        client: Any,
        maker_fee: Decimal = DEFAULT_MAKER_FEE,
        taker_fee: Decimal = DEFAULT_TAKER_FEE,
        min_net_profit_pct: Decimal = Decimal("0.10"),  # min +0.10% net profit after all 3 fees
    ):
        self.client = client
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.min_net_profit_pct = min_net_profit_pct

        self.pair_details: dict[str, PairInfo] = {}
        self.routes: list[TriangleRoute] = []
        self._initialized = False

    def initialize_routes(self) -> None:
        """Fetch asset pair topology from Kraken and map all viable triangles."""
        logger.info("Initializing Kraken triangular arbitrage topology...")
        raw_pairs = self.client._request("/0/public/AssetPairs")

        gbp_assets: dict[str, str] = {}
        # 1. Map GBP pairs
        for pair_name, info in raw_pairs.items():
            if info.get("status") != "online":
                continue
            wsname = info.get("wsname", "")
            if wsname.endswith("/GBP"):
                base = wsname.split("/")[0]
                gbp_assets[base] = pair_name
                self.pair_details[pair_name] = PairInfo(
                    pair_id=pair_name,
                    wsname=wsname,
                    base=base,
                    quote="GBP",
                    ordermin=Decimal(str(info.get("ordermin", "0"))),
                    pair_decimals=int(info.get("pair_decimals", 2)),
                    lot_decimals=int(info.get("lot_decimals", 8)),
                )

        # 2. Map cross pairs and form triangles
        routes = []
        for pair_name, info in raw_pairs.items():
            if info.get("status") != "online":
                continue
            wsname = info.get("wsname", "")
            if "/" not in wsname:
                continue
            base, quote = wsname.split("/")
            if base in gbp_assets and quote in gbp_assets:
                self.pair_details[pair_name] = PairInfo(
                    pair_id=pair_name,
                    wsname=wsname,
                    base=base,
                    quote=quote,
                    ordermin=Decimal(str(info.get("ordermin", "0"))),
                    pair_decimals=int(info.get("pair_decimals", 2)),
                    lot_decimals=int(info.get("lot_decimals", 8)),
                )
                routes.append(
                    TriangleRoute(
                        asset_a=base,
                        asset_b=quote,
                        leg1_pair=gbp_assets[base],
                        cross_pair=pair_name,
                        leg3_pair=gbp_assets[quote],
                        cross_is_a_base=True,
                    )
                )

        self.routes = routes
        self._initialized = True
        logger.info("Topology initialized: %d GBP assets, %d total triangular routes mapped.", len(gbp_assets), len(routes))

    def get_needed_pairs(self, priority_only: bool = False) -> list[str]:
        """Return unique list of pairs needed to query all routes."""
        needed = set()
        for r in self.routes:
            if priority_only and r.asset_a not in PRIORITY_ASSETS and r.asset_b not in PRIORITY_ASSETS:
                continue
            needed.add(r.leg1_pair)
            needed.add(r.cross_pair)
            needed.add(r.leg3_pair)
        return sorted(list(needed))

    def fetch_tickers(self, pair_list: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-query Kraken ticker endpoint."""
        tickers: dict[str, dict[str, Any]] = {}
        batch_size = 30
        for i in range(0, len(pair_list), batch_size):
            batch = pair_list[i : i + batch_size]
            res = self.client._request("/0/public/Ticker", data={"pair": ",".join(batch)})
            tickers.update(res)
        return tickers

    def scan(
        self,
        trade_capital_gbp: Decimal = Decimal("40.00"),
        priority_only: bool = True,
        execution_type: str = "MAKER",
    ) -> list[ArbitrageOpportunity]:
        """Scan all routes and return profitable arbitrage opportunities.

        Args:
            trade_capital_gbp: Amount of GBP to deploy per loop.
            priority_only: Filter to priority liquid assets (e.g. SOL, ETH, BTC, BCH, AAVE).
            execution_type: 'MAKER' (assumes post-only quotes) or 'TAKER' (market fills).
        """
        if not self._initialized:
            self.initialize_routes()

        fee = self.maker_fee if execution_type == "MAKER" else self.taker_fee
        fee_factor = (Decimal("1") - fee) ** 3  # 3-leg fee deduction

        needed_pairs = self.get_needed_pairs(priority_only=priority_only)
        tickers = self.fetch_tickers(needed_pairs)
        now = int(time.time())

        opportunities: list[ArbitrageOpportunity] = []

        for route in self.routes:
            if priority_only and route.asset_a not in PRIORITY_ASSETS and route.asset_b not in PRIORITY_ASSETS:
                continue

            t1 = tickers.get(route.leg1_pair)
            tc = tickers.get(route.cross_pair)
            t3 = tickers.get(route.leg3_pair)
            if not (t1 and tc and t3):
                continue

            # Bid / Ask prices
            p1_bid = Decimal(str(t1["b"][0]))
            p1_ask = Decimal(str(t1["a"][0]))
            pc_bid = Decimal(str(tc["b"][0]))
            pc_ask = Decimal(str(tc["a"][0]))
            p3_bid = Decimal(str(t3["b"][0]))
            p3_ask = Decimal(str(t3["a"][0]))

            if p1_bid <= 0 or pc_bid <= 0 or p3_bid <= 0:
                continue
            if p1_ask <= 0 or pc_ask <= 0 or p3_ask <= 0:
                continue

            # -----------------------------------------------------------------
            # 1. FORWARD CYCLE: GBP -> Asset A -> Asset B -> GBP
            # -----------------------------------------------------------------
            if execution_type == "TAKER":
                # Buy A (ask), Sell A for B (bid), Sell B for GBP (bid)
                fwd_gross = (Decimal("1") / p1_ask) * pc_bid * p3_bid
                l1_p, lc_p, l3_p = p1_ask, pc_bid, p3_bid
            else:
                # Maker quotes: Buy A at bid, Sell A for B at ask, Sell B for GBP at ask
                fwd_gross = (Decimal("1") / p1_bid) * pc_ask * p3_ask
                l1_p, lc_p, l3_p = p1_bid, pc_ask, p3_ask

            fwd_net = fwd_gross * fee_factor
            fwd_net_pct = (fwd_net - Decimal("1")) * Decimal("100")

            if fwd_net_pct >= self.min_net_profit_pct:
                gross_pct = (fwd_gross - Decimal("1")) * Decimal("100")
                est_profit = (trade_capital_gbp * (fwd_net - Decimal("1"))).quantize(Decimal("0.0001"))
                opportunities.append(
                    ArbitrageOpportunity(
                        route=route,
                        direction="FORWARD",
                        execution_type=execution_type,
                        gross_multiplier=fwd_gross,
                        gross_profit_pct=gross_pct,
                        net_multiplier=fwd_net,
                        net_profit_pct=fwd_net_pct,
                        expected_profit_gbp=est_profit,
                        leg1_price=l1_p,
                        cross_price=lc_p,
                        leg3_price=l3_p,
                        timestamp=now,
                    )
                )

            # -----------------------------------------------------------------
            # 2. REVERSE CYCLE: GBP -> Asset B -> Asset A -> GBP
            # -----------------------------------------------------------------
            if execution_type == "TAKER":
                # Buy B (ask), Buy A with B (ask), Sell A for GBP (bid)
                rev_gross = (Decimal("1") / p3_ask) * (Decimal("1") / pc_ask) * p1_bid
                r3_p, rc_p, r1_p = p3_ask, pc_ask, p1_bid
            else:
                # Maker quotes: Buy B at bid, Buy A with B at bid, Sell A for GBP at ask
                rev_gross = (Decimal("1") / p3_bid) * (Decimal("1") / pc_bid) * p1_ask
                r3_p, rc_p, r1_p = p3_bid, pc_bid, p1_ask

            rev_net = rev_gross * fee_factor
            rev_net_pct = (rev_net - Decimal("1")) * Decimal("100")

            if rev_net_pct >= self.min_net_profit_pct:
                gross_pct = (rev_gross - Decimal("1")) * Decimal("100")
                est_profit = (trade_capital_gbp * (rev_net - Decimal("1"))).quantize(Decimal("0.0001"))
                opportunities.append(
                    ArbitrageOpportunity(
                        route=route,
                        direction="REVERSE",
                        execution_type=execution_type,
                        gross_multiplier=rev_gross,
                        gross_profit_pct=gross_pct,
                        net_multiplier=rev_net,
                        net_profit_pct=rev_net_pct,
                        expected_profit_gbp=est_profit,
                        leg1_price=r1_p,
                        cross_price=rc_p,
                        leg3_price=r3_p,
                        timestamp=now,
                    )
                )

        # Sort opportunities descending by net profit percentage
        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities
