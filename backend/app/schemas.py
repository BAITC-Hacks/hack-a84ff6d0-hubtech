"""Pydantic-схемы: доменные объекты и контракты API."""
from __future__ import annotations

from datetime import date
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


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
    ignored_in_transit: float = 0.0
    stock_as_of: Optional[date] = None
    forecast_source: str = "legacy"
    forecast_model: Optional[str] = None
    forecast_model_version: Optional[str] = None
    forecast_month: Optional[date] = None
    forecast_monthly_qty: Optional[float] = None


class OrderLine(BaseModel):
    line_id: str = ""
    sku: str
    supplier_sku: Optional[str] = None
    name: str
    category: str
    warehouse: Optional[str] = None
    unit: str = "ед."
    supplier_id: str
    supplier_name: str
    recommended_qty: float
    urgency: str            # high | medium | low
    days_of_cover: float
    rationale: Rationale
    explanation: str        # человекочитаемое обоснование (LLM или шаблон)
    pack_size: float = 1.0
    min_order_qty: float = 0.0
    warnings: List[str] = Field(default_factory=list)


class SupplierGroup(BaseModel):
    supplier_id: str
    supplier_name: str
    lead_time_days: int
    total_units: float
    lines: List[OrderLine]


class RecommendationResponse(BaseModel):
    calculation_id: str = ""
    generated_at: str
    as_of: Optional[date] = None
    data_source: str = "synthetic"
    warnings: List[str] = Field(default_factory=list)
    data_quality: dict[str, Any] = Field(default_factory=dict)
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


class ExportLine(BaseModel):
    """Решение менеджера по строке сохранённого расчёта."""

    model_config = ConfigDict(extra="forbid")
    line_id: str = Field(min_length=1, max_length=1024)
    quantity: float = Field(ge=0, allow_inf_nan=False, strict=True)
    approved: bool = Field(default=False, strict=True)


class ExportRequest(BaseModel):
    """Экспортирует снимок расчёта и правки, не выполняя новый прогноз."""

    model_config = ConfigDict(extra="forbid")
    calculation_id: str = Field(min_length=1, max_length=64)
    lines: List[ExportLine] = Field(min_length=1, max_length=100000)
    approved_only: bool = Field(default=False, strict=True)
