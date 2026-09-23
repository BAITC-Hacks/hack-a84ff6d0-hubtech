"""Версионированный контракт обмена с backend."""
from datetime import date
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

SupplierId = Literal["IEK", "SYSTEME"]
FiniteQuantity = Annotated[float, Field(ge=0, allow_inf_nan=False)]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ForecastRequest(Contract):
    supplier_id: SupplierId
    forecast_month: date = Field(description="Первый день полного прогнозного месяца: YYYY-MM-01")
    skus: list[StrictStr] = Field(min_length=1, max_length=500, description="Уникальные коды 1С, включая ведущие нули и суффиксы")

    @field_validator("forecast_month", mode="before")
    @classmethod
    def validate_month(cls, value):
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            value = date.fromisoformat(value)
        if type(value) is not date or value.day != 1:
            raise ValueError("Нужна дата первого дня месяца в формате YYYY-MM-01")
        return value

    @field_validator("skus")
    @classmethod
    def validate_skus(cls, values):
        values = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("Код товара должен содержать от 1 до 128 символов")
        if len(set(values)) != len(values):
            raise ValueError("Коды товаров не должны повторяться")
        return values


class DecompositionExplanation(Contract):
    level_qty: FiniteQuantity
    seasonal_factor: FiniteQuantity
    trend_per_month: FiniteNumber = Field(description="Уже учитывает damping; повторно умножать не нужно")
    months_from_level_anchor: FiniteNumber
    trend_contribution_qty: FiniteNumber
    damping: FiniteQuantity
    warnings: list[str] = Field(default_factory=list)


class ForecastItem(Contract):
    sku: str
    unit: str | None
    status: Literal["forecast", "insufficient_history", "unknown_sku"]
    predicted_qty: FiniteQuantity | None
    history_observed_months: int | None = Field(ge=0)
    explanation: DecompositionExplanation | None = None


class ForecastResponse(Contract):
    supplier_id: SupplierId
    model: str
    model_version: str
    data_as_of: date
    forecast_month: date
    trained_before: date = Field(description="Исключительная граница истории обучения")
    forecast_scope: Literal["full_month"] = "full_month"
    warnings: list[str] = Field(default_factory=list)
    items: list[ForecastItem]


class ModelInfo(Contract):
    supplier_id: SupplierId
    available: bool
    model: str | None = None
    model_version: str | None = None
    data_as_of: date | None = None
    forecast_month: date | None = None
    trained_before: date | None = None
    sku_count: int = 0
    forecast_available: int = 0
    min_history: int | None = None


class ModelsResponse(Contract):
    models: list[ModelInfo]


class HealthResponse(Contract):
    status: Literal["ok", "degraded"]
    models: dict[SupplierId, Literal["ready", "unavailable"]]


class ErrorDetail(Contract):
    code: Literal["model_unavailable", "unsupported_month"]
    message: str


class ErrorResponse(Contract):
    detail: ErrorDetail


class ValidationIssue(BaseModel):
    """Стандартная ошибка проверки запроса FastAPI/Pydantic."""
    loc: list[str | int]
    msg: str
    type: str
    input: Any = None
    ctx: dict[str, Any] | None = None


class ValidationErrorResponse(Contract):
    detail: list[ValidationIssue]
