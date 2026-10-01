"""Dynamic capital sizing and scaling for triangular arbitrage on Kraken."""

from decimal import Decimal, ROUND_DOWN
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Minimum absolute GBP fee buffer permanently held in cash
DEFAULT_MIN_FEE_BUFFER_GBP = Decimal("2.00")
# Percentage of total equity reserved for fees as capital scales
DEFAULT_FEE_BUFFER_PERCENT = Decimal("3.0")
# Hard safety floor: Minimum trade size on Kraken (ordermin buffer)
ABSOLUTE_MIN_TRADE_GBP = Decimal("5.00")
# Max trade size per single triangular loop to prevent moving thin order books
DEFAULT_MAX_TRADE_GBP = Decimal("250.00")


def calculate_trade_capital(
    current_cash: Decimal,
    total_equity: Decimal,
    min_fee_buffer: Decimal = DEFAULT_MIN_FEE_BUFFER_GBP,
    fee_buffer_percent: Decimal = DEFAULT_FEE_BUFFER_PERCENT,
    max_trade_size: Decimal = DEFAULT_MAX_TRADE_GBP,
) -> Decimal:
    """Calculate the safe, scaled trade size for a triangular cycle.

    Formula:
        1. Calculate fee buffer: max(min_fee_buffer, total_equity * fee_buffer_percent / 100)
        2. Tradeable pool = current_cash - fee_buffer
        3. Size = clamp(ABSOLUTE_MIN_TRADE_GBP, Tradeable pool, max_trade_size)

    This ensures that as the user adds monthly capital (e.g. £50 -> £200 -> £1,000):
        - The fee buffer scales smoothly with total equity.
        - The trade size scales up proportionally, maximizing compounding.
        - The trade size never exceeds max_trade_size to prevent market slippage.
    """
    fee_buffer = max(min_fee_buffer, total_equity * (fee_buffer_percent / Decimal("100")))
    tradeable_cash = max(Decimal("0.00"), current_cash - fee_buffer)

    if tradeable_cash < ABSOLUTE_MIN_TRADE_GBP:
        logger.info(
            "Available tradeable cash (£%s) is below Kraken minimum £%s (Cash: £%s, Fee Buffer: £%s)",
            tradeable_cash, ABSOLUTE_MIN_TRADE_GBP, current_cash, fee_buffer
        )
        return Decimal("0.00")

    trade_size = min(tradeable_cash, max_trade_size).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    logger.debug(
        "Scaled Triangular Trade Size: £%s (Cash: £%s, Equity: £%s, Fee Buffer: £%s)",
        trade_size, current_cash, total_equity, fee_buffer
    )

    return trade_size
