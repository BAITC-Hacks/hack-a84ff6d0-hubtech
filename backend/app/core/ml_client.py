"""Пакетный HTTP-клиент отдельного ML-сервиса, без sklearn в backend."""
from __future__ import annotations

from datetime import date
from typing import Literal

import httpx
from pydantic import BaseModel, Field


class Prediction(BaseModel):
    sku: str
    unit: str | None
    status: Literal['forecast', 'insufficient_history', 'unknown_sku']
    predicted_qty: float | None = Field(ge=0, allow_inf_nan=False)
    explanation: dict | None = None


class ForecastBatch(BaseModel):
    supplier_id: Literal['IEK', 'SYSTEME']
    model: str
    model_version: str
    data_as_of: date
    forecast_month: date
    trained_before: date
    forecast_scope: Literal['full_month']
    items: list[Prediction]


def fetch_forecasts(requests: dict[str, list[str]], as_of: date, settings):
    """Один клиент на расчёт, до 500 SKU в запросе. Сбой изолирован по поставщику."""
    predictions, warnings = {}, []
    month = as_of.replace(day=1)
    with httpx.Client(base_url=settings.ml_api_url, timeout=settings.ml_api_timeout) as client:
        for supplier, skus in requests.items():
            pending, version = {}, None
            try:
                for start in range(0, len(skus), 500):
                    batch = skus[start:start + 500]
                    response = client.post('/v1/forecast', json={
                        'supplier_id': supplier, 'forecast_month': month.isoformat(), 'skus': batch,
                    })
                    response.raise_for_status()
                    result = ForecastBatch.model_validate(response.json())
                    if (result.supplier_id != supplier or result.forecast_month != month
                            or result.trained_before != month or result.data_as_of != as_of):
                        raise ValueError('Несогласованные поставщик или даты ML-прогноза')
                    if version is not None and version != result.model_version:
                        raise ValueError('Модель изменилась между пакетами')
                    version = result.model_version
                    if len(result.items) != len(batch) or {item.sku for item in result.items} != set(batch):
                        raise ValueError('Ответ ML не соответствует запрошенным SKU')
                    for item in result.items:
                        if (item.status == 'forecast') != (item.predicted_qty is not None):
                            raise ValueError('Статус ML не соответствует количеству')
                        pending[(supplier, item.sku)] = (item, result)
                predictions.update(pending)
            except (httpx.HTTPError, ValueError):
                warnings.append(f'{supplier}: ML API недоступен либо его данные не соответствуют дате расчёта; используется прежний алгоритм backend.')
    return predictions, warnings
