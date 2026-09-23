"""Restore only lost demand on explicitly observed out-of-stock dates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass
class StockoutResult:
    daily: pd.Series
    lost_demand_uplift: float


def build_adjusted_series(
    regular_tx: pd.DataFrame, stockout_periods: pd.DataFrame,
    start: date, end: date,
) -> StockoutResult:
    """Inclusive history boundaries; callers scope SKU and warehouse first.

    Intraday transactions are summed on their calendar day. Expected demand uses
    the same day of week and, where available, month; higher observed demand is
    preserved. Uplift is the difference from sales, never the whole replacement.
    """
    idx = pd.date_range(start=start, end=end, freq="D")
    if regular_tx.empty:
        return StockoutResult(pd.Series(0.0, index=idx), 0.0)
    daily = (
        regular_tx.assign(date=pd.to_datetime(regular_tx["date"]).dt.normalize())
        .groupby("date")["qty"].sum().reindex(idx, fill_value=0.0).clip(lower=0.0)
    )
    mask = pd.Series(False, index=idx)
    for row in stockout_periods.itertuples(index=False):
        start_day, end_day = pd.Timestamp(row.start).normalize(), pd.Timestamp(row.end).normalize()
        if pd.isna(start_day) or pd.isna(end_day):
            continue
        mask.loc[(idx >= start_day) & (idx <= end_day)] = True
    healthy = daily[~mask]
    if healthy.empty:
        return StockoutResult(daily, 0.0)
    dow = healthy.groupby(healthy.index.dayofweek).mean().to_dict()
    month_dow = healthy.groupby([healthy.index.month, healthy.index.dayofweek]).agg(["mean", "count"])
    adjusted = daily.copy()
    for day in idx[mask.to_numpy()]:
        key = (day.month, day.dayofweek)
        expected = dow.get(day.dayofweek, float(healthy.mean()))
        if key in month_dow.index and month_dow.loc[key, "count"] >= 2:
            expected = float(month_dow.loc[key, "mean"])
        adjusted.loc[day] = max(float(daily.loc[day]), expected)
    return StockoutResult(adjusted, float((adjusted - daily).sum()))
