"""Экспорт сохранённого расчёта с количеством и утверждением менеджера."""
from __future__ import annotations

import io
import math

import pandas as pd
from openpyxl.styles import Font, PatternFill

from app.schemas import ExportLine, ExportRequest, OrderLine, RecommendationResponse


class ExportValidationError(ValueError):
    pass


def _validate_quantity(line: OrderLine, decision: ExportLine) -> None:
    quantity = decision.quantity
    if not math.isfinite(quantity) or quantity < 0:
        raise ExportValidationError(f"{line.sku}: количество должно быть неотрицательным числом.")
    if quantity == 0:
        if decision.approved:
            raise ExportValidationError(f"{line.sku}: нельзя утвердить строку с нулевым количеством.")
        return
    if quantity + 1e-8 < line.min_order_qty:
        raise ExportValidationError(
            f"{line.sku}: минимальное количество — {line.min_order_qty:g} {line.unit}"
        )
    pack = line.pack_size
    if pack > 0 and not math.isclose(quantity / pack, round(quantity / pack), abs_tol=1e-8, rel_tol=0):
        raise ExportValidationError(f"{line.sku}: количество должно быть кратно {pack:g} {line.unit}")


def _select_lines(response: RecommendationResponse, request: ExportRequest | None):
    lines = [line for group in response.groups for line in group.lines]
    if request is None:
        return [(line, ExportLine(line_id=line.line_id or line.sku,
                                  quantity=line.recommended_qty, approved=False))
                for line in lines if line.recommended_qty > 0]
    if request.calculation_id != response.calculation_id:
        raise ExportValidationError("Расчёт не соответствует запросу экспорта.")
    known = {line.line_id: line for line in lines}
    if len(known) != len(lines) or "" in known:
        raise ExportValidationError("Расчёт содержит неоднозначные строки. Выполните расчёт заново.")
    decisions = {decision.line_id: decision for decision in request.lines}
    if len(decisions) != len(request.lines):
        raise ExportValidationError("В запросе экспорта повторяются строки.")
    if set(decisions) != set(known):
        raise ExportValidationError("Состав строк изменился. Выполните расчёт заново.")
    selected = []
    for line in lines:
        decision = decisions[line.line_id]
        _validate_quantity(line, decision)
        if decision.quantity > 0 and (not request.approved_only or decision.approved):
            selected.append((line, decision))
    if not selected:
        message = ("Нет утверждённых позиций с положительным количеством."
                   if request.approved_only else "Нет позиций с положительным количеством для экспорта.")
        raise ExportValidationError(message)
    return selected


def to_excel_bytes(response: RecommendationResponse, request: ExportRequest | None = None) -> bytes:
    """Не загружает источники и не пересчитывает рекомендации."""
    selected = _select_lines(response, request)
    supplier_days = {group.supplier_id: group.lead_time_days for group in response.groups}
    urgency_names = {"high": "Срочно", "medium": "Средняя", "low": "Плановая"}
    rows = []
    for line, decision in selected:
        r = line.rationale
        rows.append({
            "Поставщик": line.supplier_name,
            "ID поставщика": line.supplier_id,
            "Срок поставки, дн": supplier_days[line.supplier_id],
            "Артикул": line.sku,
            "Артикул поставщика": line.supplier_sku or "",
            "Наименование": line.name,
            "Категория": line.category,
            "Склад": line.warehouse or response.warehouse or "Все",
            "Ед. изм.": line.unit,
            "Рекомендовано, ед": line.recommended_qty,
            "К заказу, ед": decision.quantity,
            "Утверждено": "Да" if decision.approved else "Нет",
            "Изменено менеджером": "Да" if not math.isclose(decision.quantity, line.recommended_qty) else "Нет",
            "Кратность": line.pack_size,
            "Минимальная партия": line.min_order_qty,
            "Срочность": urgency_names.get(line.urgency, line.urgency),
            "Покрытие, дн": line.days_of_cover,
            "Горизонт, дн": r.horizon_days,
            "Прогноз спроса (горизонт)": r.forecast_demand,
            "Средний спрос/день": r.avg_daily_demand,
            "Сезонность ×": r.seasonality_factor,
            "Тренд ×": r.trend_factor,
            "Страховой запас": r.safety_stock,
            "Остаток": r.on_hand,
            "Дата остатка": r.stock_as_of.isoformat() if r.stock_as_of else "Не указана",
            "В пути в пределах горизонта": r.in_transit,
            "В пути не учтено": r.ignored_in_transit,
            "Упущенный спрос +": r.lost_demand_uplift,
            "Исключено опт (ед)": r.excluded_bulk_units,
            "Исключено опт (шт)": r.excluded_bulk_orders,
            "Обоснование рекомендации": line.explanation,
            "Ограничения данных": "\n".join(line.warnings),
        })

    settings_rows = [
        ("ID расчёта", response.calculation_id),
        ("Сформирован", response.generated_at),
        ("Дата расчёта", response.as_of.isoformat() if response.as_of else "Не указана"),
        ("Источник данных", response.data_source),
        ("Склад расчёта", response.warehouse or "Все"),
        ("Категория расчёта", response.category or "Все"),
        ("Уровень сервиса", response.service_level),
        ("Период проверки, дн", response.review_period_days),
        ("Режим экспорта", "Только утверждённые" if request and request.approved_only else "Черновик"),
        ("Количество позиций", len(rows)),
    ]
    settings_rows.extend(("Ограничение / допущение", warning) for warning in response.warnings)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Заказ поставщикам")
        pd.DataFrame(settings_rows, columns=["Параметр", "Значение"]).to_excel(
            writer, index=False, sheet_name="Параметры расчёта"
        )
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="24364B")
            for column in sheet.columns:
                header = column[0]
                width = min(55, max(14, len(str(header.value or "")) + 3))
                sheet.column_dimensions[header.column_letter].width = width
                for cell in column[1:]:
                    # Текст из справочника, включая начинающийся с '=', остаётся текстом.
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
                    elif isinstance(cell.value, (int, float)):
                        cell.number_format = "0.####"
            if sheet.title == "Параметры расчёта":
                sheet.column_dimensions["A"].width = 30
                sheet.column_dimensions["B"].width = 110
    return buffer.getvalue()
