"""Kraken Triangular Arbitrage Package."""

from arbitrage.routes import TriangleRoute, PairInfo, PRIORITY_ASSETS
from arbitrage.capital import calculate_trade_capital
from arbitrage.scanner import ArbitrageScanner, ArbitrageOpportunity
from arbitrage.executor import ArbitrageExecutor

__all__ = [
    "TriangleRoute",
    "PairInfo",
    "PRIORITY_ASSETS",
    "calculate_trade_capital",
    "ArbitrageScanner",
    "ArbitrageOpportunity",
    "ArbitrageExecutor",
]

