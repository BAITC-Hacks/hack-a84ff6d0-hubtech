"""Pydantic-схемы: доменные объекты и контракты API."""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------- Входные доменные объекты (то, что даёт 1С / синтетика) ----------

class Sale(BaseModel):
    date: date
    sku: str
    name: str
    category: str
    qty: float
    price: float
    client_id: str          # обезличенный ID клиента
    warehouse: str


class Stock(BaseModel):
    sku: str
    warehouse: str
    on_hand: float          # текущий остаток


class InTransit(BaseModel):
    sku: str
    warehouse: str
    qty: float
    eta: date               # ожидаемая дата прихода


class Supplier(BaseModel):
    supplier_id: str
    name: str
    lead_time_days: int
    min_order_qty: float = 0.0


class SkuSupplier(BaseModel):
    """Привязка артикула к поставщику и его условиям."""
    sku: str
    supplier_id: str
    pack_size: float = 1.0  # кратность партии


class StockoutPeriod(BaseModel):
    sku: str
    warehouse: str
    start: date
    end: date


# ---------- Выход движка ----------

class Rationale(BaseModel):
    """Прозрачная раскладка расчёта — основа объяснимости (MH #5)."""
    avg_daily_demand: float
    seasonality_factor: float
    trend_factor: float
    horizon_days: int
    forecast_demand: float
    safety_stock: float
    on_hand: float
    in_transit: float
    lost_demand_uplift: float
    excluded_bulk_units: float
    excluded_bulk_orders: int
    raw_need: float


class OrderLine(BaseModel):
    sku: str
    name: str
    category: str
    supplier_id: str
    supplier_name: str
    recommended_qty: float
    urgency: str            # high | medium | low
    days_of_cover: float
    rationale: Rationale
    explanation: str        # человекочитаемое обоснование (LLM или шаблон)


class SupplierGroup(BaseModel):
    supplier_id: str
    supplier_name: str
    lead_time_days: int
    total_units: float
    lines: List[OrderLine]


class RecommendationResponse(BaseModel):
    generated_at: str
    warehouse: Optional[str] = None
    category: Optional[str] = None
    service_level: float
    review_period_days: int
    sku_count: int
    groups: List[SupplierGroup]


# ---------- Параметры запроса расчёта ----------

class RecommendRequest(BaseModel):
    warehouse: Optional[str] = Field(default=None, description="Фильтр по складу")
    category: Optional[str] = Field(default=None, description="Фильтр по категории")
    service_level: Optional[float] = Field(default=None, ge=0.5, le=0.999)
    review_period_days: Optional[int] = Field(default=None, ge=1, le=120)
    explain: bool = Field(default=True, description="Генерировать LLM-обоснования")
