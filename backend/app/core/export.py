"""Экспорт сформированного заказа в Excel (совместимость с учётной системой 1С).

Плоская таблица со всеми полями заказа и раскладкой расчёта — её можно
импортировать/сверять в 1С. Возвращает байты .xlsx.
"""
from __future__ import annotations

import io

import pandas as pd

from app.schemas import RecommendationResponse


def to_excel_bytes(resp: RecommendationResponse) -> bytes:
    rows = []
    for g in resp.groups:
        for ln in g.lines:
            r = ln.rationale
            rows.append({
                "Поставщик": g.supplier_name,
                "ID поставщика": g.supplier_id,
                "Срок поставки, дн": g.lead_time_days,
                "Артикул": ln.sku,
                "Наименование": ln.name,
                "Категория": ln.category,
                "К заказу, ед": ln.recommended_qty,
                "Срочность": ln.urgency,
                "Покрытие, дн": ln.days_of_cover,
                "Прогноз спроса (горизонт)": r.forecast_demand,
                "Средний спрос/день": r.avg_daily_demand,
                "Сезонность ×": r.seasonality_factor,
                "Тренд ×": r.trend_factor,
                "Страховой запас": r.safety_stock,
                "Остаток": r.on_hand,
                "В пути": r.in_transit,
                "Упущенный спрос +": r.lost_demand_uplift,
                "Исключено опт (ед)": r.excluded_bulk_units,
                "Исключено опт (шт)": r.excluded_bulk_orders,
                "Обоснование": ln.explanation,
            })
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Заказ поставщикам")
    buf.seek(0)
    return buf.read()
