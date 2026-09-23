"""Обоснование по каждой строке заказа (часть MH #5 — объяснимость).

Если задан OPENAI_API_KEY — обоснование генерирует LLM (OpenAI/Codex) на основе
прозрачной раскладки расчёта. Без ключа сервис остаётся рабочим: используется
детерминированный шаблон на тех же числах (полная объяснимость сохраняется).
"""
from __future__ import annotations

from app.config import get_settings
from app.schemas import Rationale


def _template(name: str, r: Rationale, qty: float, urgency: str, unit: str = "ед.") -> str:
    recommendation = (f"«{name}»: рекомендуем заказать {qty:g} {unit}" if qty > 0 else
                      f"«{name}»: дополнительный заказ не требуется; проверьте сроки ожидаемых поставок.")
    parts = [
        recommendation,
        f"Прогноз спроса на {r.horizon_days} дн. — {r.forecast_demand:g} {unit}",
    ]
    if abs(r.seasonality_factor - 1.0) >= 0.05:
        direction = "выше" if r.seasonality_factor > 1 else "ниже"
        parts.append(f"сезонность {direction} среднего (×{r.seasonality_factor:g})")
    if abs(r.trend_factor - 1.0) >= 0.03:
        direction = "рост" if r.trend_factor > 1 else "спад"
        parts.append(f"тренд спроса — {direction} (×{r.trend_factor:g})")
    if r.lost_demand_uplift > 0:
        parts.append(f"учтён упущенный спрос за периоды отсутствия (+{r.lost_demand_uplift:g} {unit})")
    if r.excluded_bulk_orders > 0:
        parts.append(
            f"исключены разовые оптовые заказы ({r.excluded_bulk_orders} шт., "
            f"{r.excluded_bulk_units:g} {unit}), чтобы не завышать регулярную потребность"
        )
    parts.append(
        f"страховой запас {r.safety_stock:g} {unit}; вычтены остаток {r.on_hand:g} "
        f"и товары в пути {r.in_transit:g}."
    )
    if r.ignored_in_transit > 0:
        parts.append(f"Не вычтены поставки без подтверждённой даты в горизонте: {r.ignored_in_transit:g} {unit}")
    if r.stock_as_of:
        parts.append(f"Дата остатка: {r.stock_as_of.isoformat()}.")
    return " ".join(parts)


def build_explanation(name: str, r: Rationale, qty: float, urgency: str, use_llm: bool, unit: str = "ед.") -> str:
    """Возвращает человекочитаемое обоснование строки заказа."""
    base = _template(name, r, qty, urgency, unit)
    settings = get_settings()
    if not (use_llm and settings.llm_enabled):
        return base

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        prompt = (
            "Ты — ассистент менеджера отдела закупа. По раскладке расчёта дай краткое "
            "(1-2 предложения) деловое обоснование заказа на русском. Не выдумывай цифры, "
            "используй только данные ниже.\n"
            f"Товар: {name}\nРекомендовано к заказу: {qty:g} {unit}\nСрочность: {urgency}\n"
            f"Раскладка: {r.model_dump()}"
        )
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=160,
        )
        text = resp.choices[0].message.content
        return text.strip() if text else base
    except Exception:
        # Любой сбой LLM не должен ронять расчёт
        return base
