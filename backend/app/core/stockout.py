"""MH #3 — оценка и компенсация упущенного спроса в периоды отсутствия товара.

В дни stockout продажи искусственно занижены (товара не было), поэтому по «сырым»
фактам спрос недооценивается. Мы строим непрерывный дневной ряд спроса и
замещаем дни stockout оценкой ожидаемого спроса (с учётом сезонности месяца).
Возвращаем скорректированный ряд и суммарный uplift — вклад упущенного спроса.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


@dataclass
class StockoutResult:
    daily: pd.Series            # непрерывный дневной спрос (скорректированный)
    lost_demand_uplift: float   # сколько единиц добавлено как упущенный спрос


def build_adjusted_series(
    regular_tx: pd.DataFrame,
    stockout_periods: pd.DataFrame,
    start: date,
    end: date,
) -> StockoutResult:
    """Строит дневной ряд спроса и компенсирует stockout-периоды."""
    idx = pd.date_range(start=start, end=end, freq="D")

    if regular_tx.empty:
        return StockoutResult(pd.Series(0.0, index=idx), 0.0)

    daily = (
        regular_tx.assign(date=pd.to_datetime(regular_tx["date"]))
        .groupby("date")["qty"].sum()
        .reindex(idx, fill_value=0.0)
    )

    # маска дней stockout
    stockout_mask = pd.Series(False, index=idx)
    for _, row in stockout_periods.iterrows():
        s = pd.to_datetime(row["start"])
        e = pd.to_datetime(row["end"])
        stockout_mask.loc[(idx >= s) & (idx <= e)] = True

    # базовый уровень спроса по «здоровым» дням (без stockout, будни)
    healthy = daily[~stockout_mask]
    weekday = pd.Series(idx.weekday < 5, index=idx)
    healthy_weekday = healthy[weekday[~stockout_mask].to_numpy()]
    base_level = float(healthy_weekday.mean()) if len(healthy_weekday) else float(daily.mean())
    if not np.isfinite(base_level) or base_level <= 0:
        base_level = float(daily.mean()) if daily.mean() > 0 else 0.0

    # сезонные коэффициенты по месяцам (по здоровым дням)
    if len(healthy) and healthy.mean() > 0:
        monthly = healthy.groupby(healthy.index.month).mean()
        seasonal = (monthly / healthy.mean()).to_dict()
    else:
        seasonal = {}

    uplift = 0.0
    adjusted = daily.copy()
    for d in idx[stockout_mask.to_numpy()]:
        factor = seasonal.get(d.month, 1.0)
        est = base_level * factor
        adjusted.loc[d] = est
        uplift += est

    return StockoutResult(adjusted, float(uplift))
