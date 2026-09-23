"""MH #1 — базовый расчёт потребности в пополнении по каждому артикулу.

Классическая модель точки заказа с покрытием на горизонт (срок поставки +
период проверки) и страховым запасом по уровню сервиса. Учитывает ВСЕ источники:
прогноз спроса (сезонность+тренд), текущий остаток, товары в пути, кратность
партии и минимальную партию поставщика. Изменение любого источника меняет
результат — что и требует проверка MH #1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

from app.core.forecasting import Forecast


@dataclass
class Need:
    horizon_days: int
    forecast_demand: float      # спрос за горизонт
    safety_stock: float
    raw_need: float             # до округления/минимальной партии
    recommended_qty: float      # итог к заказу
    days_of_cover: float
    urgency: str


def _z(service_level: float) -> float:
    sl = min(max(service_level, 0.5), 0.999)
    return float(norm.ppf(sl))


def compute_need(
    fc: Forecast,
    on_hand: float,
    in_transit: float,
    lead_time_days: int,
    review_period_days: int,
    service_level: float,
    pack_size: float = 1.0,
    min_order_qty: float = 0.0,
) -> Need:
    horizon_days = int(lead_time_days) + int(review_period_days)
    forecast_demand = fc.avg_daily_demand * horizon_days
    safety_stock = _z(service_level) * fc.sigma_daily * math.sqrt(max(horizon_days, 1))

    raw_need = forecast_demand + safety_stock - on_hand - in_transit
    qty = max(0.0, raw_need)

    # кратность партии
    if qty > 0 and pack_size and pack_size > 1:
        qty = math.ceil(qty / pack_size) * pack_size
    else:
        qty = math.ceil(qty)

    # минимальная партия поставщика
    if qty > 0 and min_order_qty and qty < min_order_qty:
        qty = min_order_qty

    # покрытие и срочность
    daily = fc.avg_daily_demand if fc.avg_daily_demand > 0 else 1e-9
    days_of_cover = (on_hand + in_transit) / daily

    if qty <= 0:
        urgency = "low"
    elif days_of_cover < lead_time_days:
        urgency = "high"
    elif days_of_cover < horizon_days:
        urgency = "medium"
    else:
        urgency = "low"

    return Need(
        horizon_days=horizon_days,
        forecast_demand=round(forecast_demand, 2),
        safety_stock=round(safety_stock, 2),
        raw_need=round(raw_need, 2),
        recommended_qty=round(qty, 2),
        days_of_cover=round(days_of_cover, 1),
        urgency=urgency,
    )
