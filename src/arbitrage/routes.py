"""Triangular arbitrage route definitions and pair topology for Kraken."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PairInfo:
    pair_id: str          # e.g. "SOLGBP", "XETHZGBP"
    wsname: str           # e.g. "SOL/GBP", "ETH/GBP"
    base: str             # e.g. "SOL", "ETH"
    quote: str            # e.g. "GBP", "ETH"
    ordermin: Decimal     # minimum order volume
    pair_decimals: int    # price decimals
    lot_decimals: int     # volume decimals


@dataclass(frozen=True)
class TriangleRoute:
    asset_a: str          # Base asset of leg 1, e.g. "BCH", "SOL", "AAVE"
    asset_b: str          # Base or quote asset of cross pair, e.g. "ETH", "XBT"
    leg1_pair: str        # GBP <-> Asset A pair (e.g. "BCHGBP")
    cross_pair: str       # Asset A <-> Asset B pair (e.g. "BCHETH")
    leg3_pair: str        # GBP <-> Asset B pair (e.g. "ETHGBP")
    cross_is_a_base: bool # True if cross pair base is Asset A, False if Asset B

    @property
    def id(self) -> str:
        return f"{self.asset_a}-{self.asset_b}-GBP"


# Curated high-liquidity / tight-spread triangular assets on Kraken
PRIORITY_ASSETS = [
    "SOL", "ETH", "XBT", "BCH", "AAVE", "LINK", "LTC", "DOT", "ADA", "XRP", "FIL"
]
