"""Dynamic Volatility & Profit Target Engine for Kraken Micro-Trader.

Analyzes real-time 24-hour candlestick data from Kraken to dynamically compute
the optimal profit target based on prevailing market volatility (ATR / Price Swing).
"""

from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def compute_volatility_target(
    candles: list[list[Any]],
    fallback_percent: Decimal = Decimal("4.0"),
    swing_capture_ratio: Decimal = Decimal("0.75"),
    min_target: Decimal = Decimal("2.5"),
    max_target: Decimal = Decimal("6.0"),
) -> tuple[Decimal, dict[str, Any]]:
    """Compute dynamic profit target from recent 24-hour hourly candlestick data.

    Formula:
        1. Extract the High and Low over the last 24 hours.
        2. Calculate the 24h Volatility Swing % = ((24h High - 24h Low) / 24h Low) * 100.
        3. Target = 75% of the 24h Swing (capturing the core oscillation wave).
        4. Clamp Target between min_target (2.5% to protect fees) and max_target (6.0% to avoid stranded orders).

    Returns:
        tuple of (clamped_target_percent, stats_dict)
    """
    if not candles or len(candles) < 24:
        logger.warning("Insufficient candle history (%d candles). Using fallback target %s%%", len(candles) if candles else 0, fallback_percent)
        return fallback_percent, {"reason": "insufficient_data"}

    try:
        last_24 = candles[-24:]
        high_24h = max(Decimal(str(c[2])) for c in last_24)
        low_24h = min(Decimal(str(c[3])) for c in last_24)
        current_close = Decimal(str(last_24[-1][4]))

        if low_24h <= 0:
            return fallback_percent, {"reason": "invalid_low"}

        # 24-hour High/Low percentage range
        swing_24h_pct = ((high_24h - low_24h) / low_24h) * Decimal("100")

        # Dynamic target based on the capture ratio (default 75% of daily swing)
        raw_target = swing_24h_pct * swing_capture_ratio

        # Clamp between safety limits
        clamped_target = max(min_target, min(raw_target, max_target))
        final_target = clamped_target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        stats = {
            "current_price": str(current_close),
            "high_24h": str(high_24h),
            "low_24h": str(low_24h),
            "swing_24h_pct": f"{swing_24h_pct:.2f}%",
            "raw_target": f"{raw_target:.2f}%",
            "final_target": f"{final_target:.2f}%",
        }

        logger.info(
            "Volatility Analysis: 24h Low=£%s, 24h High=£%s (Swing: %s). Dynamic Target: %s (Clamped to %s)",
            low_24h,
            high_24h,
            stats["swing_24h_pct"],
            stats["raw_target"],
            stats["final_target"],
        )

        return final_target, stats

    except Exception as e:
        logger.error("Failed to compute volatility target: %s. Using fallback %s%%", e, fallback_percent)
        return fallback_percent, {"error": str(e)}
