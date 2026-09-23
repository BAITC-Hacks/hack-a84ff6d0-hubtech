"""Demand coverage, dated supply, safety stock and purchase constraints."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from scipy.stats import norm

from app.core.forecasting import Forecast


@dataclass
class Need:
    horizon_days: int
    forecast_demand: float
    safety_stock: float
    raw_need: float
    recommended_qty: float
    days_of_cover: float
    urgency: str


def _z(service_level: float) -> float:
    return float(norm.ppf(min(max(service_level, 0.5), 0.999)))


def _days_until_shortage(daily: float, on_hand: float, arrivals: list[tuple[int, float]]) -> float:
    """Receipts arrive at the start of ETA day; return first uncovered interval."""
    if daily <= 0:
        return 0.0
    available = max(0.0, on_hand)
    last_offset = 0.0
    for offset, quantity in sorted(arrivals):
        offset = max(0, offset)
        span = offset - last_offset
        if available / daily + 1e-9 < span:
            return last_offset + available / daily
        available = max(0.0, available - daily * span) + max(0.0, quantity)
        last_offset = float(offset)
    return last_offset + available / daily


def compute_need(
    fc: Forecast, on_hand: float, in_transit: float, lead_time_days: int,
    review_period_days: int, service_level: float, pack_size: float = 1.0,
    min_order_qty: float = 0.0, arrivals: list[tuple[int, float]] | None = None,
) -> Need:
    horizon_days = max(1, int(lead_time_days) + int(review_period_days))
    demand = fc.avg_daily_demand * horizon_days
    safety = _z(service_level) * fc.sigma_daily * math.sqrt(horizon_days)
    raw_need = demand + safety - on_hand - in_transit
    qty = max(0.0, raw_need)
    # Apply MOQ first; then round up to the pack multiple, including fractions.
    if qty > 0:
        minimum = max(qty, min_order_qty)
        pack = Decimal(str(pack_size if math.isfinite(pack_size) and pack_size > 0 else 1.0))
        qty = float((Decimal(str(minimum)) / pack).to_integral_value(rounding=ROUND_CEILING) * pack)
    # Without ETA evidence, in-transit stock cannot postpone the first shortage.
    cover = _days_until_shortage(fc.avg_daily_demand, on_hand, arrivals or [])
    if fc.avg_daily_demand <= 0:
        urgency = "low"
    elif cover < lead_time_days:
        urgency = "high"
    elif cover < horizon_days:
        urgency = "medium"
    else:
        urgency = "low"
    return Need(horizon_days, round(demand, 2), round(safety, 2), round(raw_need, 2),
                qty, round(cover, 1), urgency)
