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
    fallback_percent: Decimal = Decimal("0.4"),
    spread_ratio: Decimal = Decimal("0.50"),
    capture_ratio: Decimal = Decimal("0.75"),
    min_target: Decimal = Decimal("0.35"),
    max_target: Decimal = Decimal("0.80"),
) -> tuple[Decimal, dict[str, Any]]:
    """Compute dynamic profit target from recent 24-hour hourly candlestick data.

    Formula:
        1. Extract the High and Low over the last 24 hours.
        2. Calculate the 24h Volatility Spread % = ((24h High - 24h Low) / 24h Low) * 100.
        3. Take 50% of the spread (half-spread: typical mean-to-extreme oscillation amplitude).
        4. Target = 75% of that half-spread (capturing the core intraday wave without needing to break daily highs).
        5. Clamp Target between min_target (0.35% to protect fees) and max_target (0.80% to keep cycles fast).

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

        # 24-hour High/Low percentage range (full spread)
        swing_24h_pct = ((high_24h - low_24h) / low_24h) * Decimal("100")

        # Dynamic target: 50% of spread, then 75% of that
        half_spread_pct = swing_24h_pct * spread_ratio
        raw_target = half_spread_pct * capture_ratio

        # Clamp between safety limits
        clamped_target = max(min_target, min(raw_target, max_target))
        final_target = clamped_target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        stats = {
            "current_price": str(current_close),
            "high_24h": str(high_24h),
            "low_24h": str(low_24h),
            "swing_24h_pct": f"{swing_24h_pct:.2f}%",
            "half_spread_pct": f"{half_spread_pct:.2f}%",
            "raw_target": f"{raw_target:.2f}%",
            "final_target": f"{final_target:.2f}%",
        }

        logger.info(
            "Volatility Analysis: 24h Low=£%s, 24h High=£%s (Spread: %s, 50%%: %s). Dynamic Target: %s (Clamped to %s)",
            low_24h,
            high_24h,
            stats["swing_24h_pct"],
            stats["half_spread_pct"],
            stats["raw_target"],
            stats["final_target"],
        )

        return final_target, stats

    except Exception as e:
        logger.error("Failed to compute volatility target: %s. Using fallback %s%%", e, fallback_percent)
        return fallback_percent, {"error": str(e)}


def check_market_velocity(
    candles: list[list[Any]],
    lookback_hours: int = 2,
    threshold_percent: Decimal = Decimal("1.2"),
) -> tuple[bool, Decimal, dict[str, Any]]:
    """Check if price is rapidly moving in one direction over recent hours.

    Evaluates both multi-hour trend velocity and immediate single-hour impulse.
    Intelligently classifies direction:
      - FALLING_KNIFE: Rapid downside dump (> threshold% drop) -> Protects against buying falling knives.
      - PARABOLIC_PUMP: Rapid upside spike (> threshold% surge) -> Prevents FOMO buying at the peak before pullback.
      - FLASH_WICK: Extreme single-candle volatility spike.

    Args:
        candles: Hourly OHLC candle array from Kraken get_ohlc(pair, interval=60).
        lookback_hours: Number of recent 1-hour candles to evaluate (default 2h).
        threshold_percent: Max allowed directional change % before triggering pause.

    Returns:
        tuple: (is_high_velocity: bool, roc_percent: Decimal, stats: dict)
    """
    if not candles or len(candles) < lookback_hours:
        return False, Decimal("0.0"), {"reason": "insufficient_candles"}

    try:
        recent = candles[-lookback_hours:]
        # Start price is the open price of the candle `lookback_hours` ago
        start_price = Decimal(str(recent[0][1]))
        # Current price is the latest close
        current_price = Decimal(str(recent[-1][4]))

        if start_price <= Decimal("0"):
            return False, Decimal("0.0"), {"reason": "invalid_start_price"}

        roc_percent = (((current_price - start_price) / start_price) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        abs_roc = abs(roc_percent)

        # Immediate 1-hour candle analysis
        latest_candle = recent[-1]
        latest_open = Decimal(str(latest_candle[1]))
        latest_high = Decimal(str(latest_candle[2]))
        latest_low = Decimal(str(latest_candle[3]))
        latest_close = Decimal(str(latest_candle[4]))

        latest_range_pct = Decimal("0")
        if latest_low > 0:
            latest_range_pct = (((latest_high - latest_low) / latest_low) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        # 1-hour directional drop check (flash drop)
        one_hour_roc = Decimal("0")
        if latest_open > 0:
            one_hour_roc = (((latest_close - latest_open) / latest_open) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        # Directional classification
        velocity_type = "CALM"
        is_high_velocity = False

        if roc_percent <= -threshold_percent or one_hour_roc <= -(threshold_percent * Decimal("0.80")):
            velocity_type = "FALLING_KNIFE"
            is_high_velocity = True
        elif roc_percent >= threshold_percent or one_hour_roc >= (threshold_percent * Decimal("0.90")):
            velocity_type = "PARABOLIC_PUMP"
            is_high_velocity = True
        elif latest_range_pct > (threshold_percent * Decimal("1.30")):
            velocity_type = "FLASH_WICK"
            is_high_velocity = True

        stats = {
            "start_price": str(start_price),
            "current_price": str(current_price),
            "lookback_hours": lookback_hours,
            "roc_percent": str(roc_percent),
            "one_hour_roc": str(one_hour_roc),
            "latest_range_pct": str(latest_range_pct),
            "threshold_percent": str(threshold_percent),
            "velocity_type": velocity_type,
            "is_high_velocity": is_high_velocity,
        }

        if is_high_velocity:
            logger.info(
                "Velocity Guard Triggered [%s]: Price moved %+.2f%% over %dh (1h: %+.2f%%, threshold: %.2f%%, 1h range: %.2f%%)",
                velocity_type, roc_percent, lookback_hours, one_hour_roc, threshold_percent, latest_range_pct
            )

        return is_high_velocity, roc_percent, stats

    except Exception as e:
        logger.error("Error computing market velocity: %s", e)
        return False, Decimal("0.0"), {"error": str(e)}

