"""Restore only lost demand on explicitly observed out-of-stock dates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class MonthlyStockoutResult:
    monthly: pd.Series
    lost_demand_uplift: float
    warnings: list[str] = field(default_factory=list)


def build_adjusted_monthly(
    monthly_qty: pd.Series, stockout_periods: pd.DataFrame, history_end: date,
    regular_tx: pd.DataFrame | None = None,
    reconciled_months: pd.Index | None = None,
) -> MonthlyStockoutResult:
    """Estimate missing monthly volume without fabricating daily observations.

    Only complete months and explicitly supplied inclusive intervals are used.
    For partial outages, estimate from observed volume / available calendar days.
    Reconciled transactions allow observed sales during outages to be subtracted
    from the estimated loss. Otherwise the month-only estimate explicitly assumes
    sales occurred on available days. Overlapping intervals are counted once.

    A fully unavailable month uses the median observed available-day rate of up
    to three *earlier* usable months. Imputed full months never feed that fallback.
    Without earlier observations no uplift is invented. Caller scopes SKU/warehouse.
    """
    monthly = monthly_qty.copy()
    monthly.index = pd.to_datetime(monthly.index).to_period("M").to_timestamp()
    monthly = monthly.groupby(level=0).sum().sort_index().clip(lower=0.0)
    cutoff = (pd.Timestamp(history_end).normalize() + pd.Timedelta(days=1)).replace(day=1)
    monthly = monthly[monthly.index < cutoff]
    if monthly.empty or stockout_periods.empty:
        return MonthlyStockoutResult(monthly, 0.0)
    idx = pd.date_range(monthly.index.min(), monthly.index.max() + pd.offsets.MonthEnd(0))
    mask = pd.Series(False, index=idx)
    for row in stockout_periods.itertuples(index=False):
        start = pd.to_datetime(row.start, errors="coerce")
        end = pd.to_datetime(row.end, errors="coerce")
        if pd.isna(start) or pd.isna(end) or start > end:
            continue
        mask.loc[(idx >= start.normalize()) & (idx <= end.normalize())] = True
    if not mask.any():
        return MonthlyStockoutResult(monthly, 0.0)

    reconciled = set(reconciled_months if reconciled_months is not None else [])
    observed = pd.Series(0.0, index=idx)
    if regular_tx is not None and not regular_tx.empty:
        observed = (regular_tx.assign(date=pd.to_datetime(regular_tx["date"]).dt.normalize())
                    .groupby("date")["qty"].sum().clip(lower=0.0).reindex(idx, fill_value=0.0))
    adjusted = monthly.copy()
    prior_rates: list[float] = []
    estimated_months, fallback_months, unsupported_months = [], [], []
    for month, qty in monthly.items():
        month_mask = mask.loc[month:month + pd.offsets.MonthEnd(0)]
        unavailable = int(month_mask.sum())
        available = month.days_in_month - unavailable
        if available:
            # Only reconciled totals can justify using transaction timing.
            observed_out = float(observed.loc[month_mask.index[month_mask]].sum()) if month in reconciled else 0.0
            healthy_qty = max(0.0, float(qty) - observed_out)
            rate = healthy_qty / available
            prior_rates.append(rate)
            if unavailable:
                adjusted.loc[month] += max(0.0, rate * unavailable - observed_out)
                if month not in reconciled:
                    estimated_months.append(month.strftime("%Y-%m"))
        elif prior_rates:
            expected = float(pd.Series(prior_rates[-3:]).median()) * month.days_in_month
            adjusted.loc[month] = max(float(qty), expected)
            fallback_months.append(month.strftime("%Y-%m"))
        else:
            unsupported_months.append(month.strftime("%Y-%m"))
    warnings = [
        "Компенсация stockout рассчитана в месячных объёмах по явно переданным интервалам; "
        "дневные продажи не восстанавливались как наблюдения."
    ]
    if estimated_months:
        warnings.append("Для месяцев " + ", ".join(estimated_months) +
                        " компенсация предполагает, что месячные продажи пришлись на доступные дни; "
                        "согласованных транзакций для проверки продаж в stockout нет.")
    if fallback_months:
        warnings.append("Полный stockout в " + ", ".join(fallback_months) +
                        ": использована медиана доступного среднесуточного спроса до 3 предшествующих месяцев; "
                        "сезонные изменения внутри отсутствующего месяца не наблюдались.")
    if unsupported_months:
        warnings.append("Полный stockout в " + ", ".join(unsupported_months) +
                        " не компенсирован: нет предшествующих месяцев с доступными днями и подтверждённым объёмом.")
    return MonthlyStockoutResult(adjusted, float((adjusted - monthly).sum()), warnings)


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
