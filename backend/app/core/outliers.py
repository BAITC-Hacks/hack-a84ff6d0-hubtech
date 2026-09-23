"""MH #4 — выявление и исключение разовых крупных заказов (опт одному клиенту).

Регулярную потребность нельзя считать по «сырым» продажам: единичный оптовый
отгруз одному клиенту раздувает средний спрос. Здесь такие транзакции
детектируются устойчивым (robust) методом и исключаются из ряда регулярного
спроса. Возвращаем очищенные транзакции и статистику исключений — для
объяснимости.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class OutlierResult:
    regular: pd.DataFrame       # транзакции регулярного спроса
    excluded_units: float       # сколько единиц исключено
    excluded_orders: int        # сколько транзакций исключено


def exclude_bulk_orders(tx: pd.DataFrame) -> OutlierResult:
    """Отсекает аномально крупные разовые продажи.

    Критерий (комбинированный, устойчив к выбросам):
      * qty выше верхней границы Тьюки Q3 + 3*IQR (экстремальный выброс), И
      * qty >= 8x медианы (защита от ложных срабатываний на обычной вариации).
    Дополнительно ловим концентрацию: одна транзакция, покрывающая >40% всего
    объёма по артикулу, всегда считается разовой оптовой.
    """
    if tx.empty:
        return OutlierResult(tx.copy(), 0.0, 0)

    qty = tx["qty"].to_numpy(dtype=float)
    total = qty.sum()
    median = np.median(qty)
    q1, q3 = np.percentile(qty, [25, 75])
    iqr = q3 - q1
    upper_fence = q3 + 3.0 * iqr

    is_extreme = (qty > upper_fence) & (qty >= 8.0 * max(median, 1.0))
    is_concentrated = qty > 0.40 * total if total > 0 else np.zeros_like(qty, bool)
    is_bulk = is_extreme | is_concentrated

    regular = tx.loc[~is_bulk].copy()
    excluded_units = float(qty[is_bulk].sum())
    excluded_orders = int(is_bulk.sum())
    return OutlierResult(regular, excluded_units, excluded_orders)
