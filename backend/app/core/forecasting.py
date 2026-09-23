"""Explainable daily and monthly forecasts with seasonal adjustment and linear trend.

Monthly totals stay monthly observations: their distribution across individual days
is unknown. Forecast dates use [horizon_start, horizon_start + horizon_days).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


@dataclass
class Forecast:
    avg_daily_demand: float
    sigma_daily: float
    seasonality_factor: float
    trend_factor: float
    level_daily: float


def _external_seasonality(factors: dict[int, float] | None) -> dict[int, float]:
    valid = {int(k): float(v) for k, v in (factors or {}).items()
             if 1 <= int(k) <= 12 and np.isfinite(v) and v > 0}
    if not valid:
        return {}
    # A factor describes relative demand, so its annual mean must be one.
    mean = float(np.mean(list(valid.values())))
    return {month: value / mean for month, value in valid.items()}


def _month_seasonality(daily: pd.Series) -> dict[int, float]:
    # One occurrence of a season cannot distinguish a trend from seasonality.
    if len(daily) < 365:
        return {}
    monthly = daily.resample("MS").mean()
    return _monthly_seasonality(monthly)


def _monthly_seasonality(rates: pd.Series) -> dict[int, float]:
    if len(rates) < 12 or rates.mean() <= 0:
        return {}
    # Remove differences in the overall level between calendar years first.
    yearly_mean = rates.groupby(rates.index.year).transform("mean")
    ratios = rates.div(yearly_mean.replace(0, np.nan)).dropna()
    factors = ratios.groupby(ratios.index.month).mean().to_dict()
    # Zero observed seasonal demand is legitimate; only undefined ratios default.
    mean = float(np.mean(list(factors.values()))) if factors else 0.0
    return {int(k): float(v / mean) for k, v in factors.items()} if mean > 0 else {}


def _slope(values: pd.Series) -> float:
    if len(values) < 4:
        return 0.0
    x = (values.index - values.index[0]).total_seconds().to_numpy() / 86400.0
    if np.ptp(x) == 0:
        return 0.0
    return float(np.polyfit(x, values.to_numpy(dtype=float), 1)[0])


def _trend_slope_per_day(daily: pd.Series) -> float:
    """Change in *daily rate* per elapsed day, using weekly mean rates.

    Weekly sums fitted against week numbers would require division by 49, not 7.
    Actual day coordinates also avoid bias from the duration of partial weeks.
    Input must already be deseasonalized.
    """
    recent = daily.iloc[-180:]
    if recent.empty:
        return 0.0
    table = pd.DataFrame({"rate": recent, "day": recent.index.asi8 / 86400e9})
    weekly = table.resample("W").agg({"rate": "mean", "day": "mean"})
    if len(weekly) < 4:
        return 0.0
    return float(np.polyfit(weekly["day"] - weekly["day"].iloc[0], weekly["rate"], 1)[0])


def _project(
    level: float, slope: float, anchor: pd.Timestamp, seasonal: dict[int, float],
    horizon_start: date, horizon_days: int, sigma: float,
) -> Forecast:
    horizon = pd.date_range(horizon_start, periods=max(1, horizon_days), freq="D")
    offsets = (horizon - anchor).total_seconds().to_numpy() / 86400.0
    projected = np.maximum(level + slope * offsets, 0.0)
    factors = np.array([seasonal.get(d.month, 1.0) for d in horizon])
    rate = float(np.mean(projected * factors))
    trend = float(projected.mean() / level) if level > 0 else 1.0
    return Forecast(
        avg_daily_demand=round(rate, 4), sigma_daily=round(max(0.0, sigma), 4),
        seasonality_factor=round(float(factors.mean()), 4),
        trend_factor=round(trend, 4), level_daily=round(max(0.0, level), 4),
    )


def forecast_demand(
    daily: pd.Series, horizon_start: date, horizon_days: int,
    seasonal_factors: dict[int, float] | None = None,
) -> Forecast:
    if daily.empty:
        return Forecast(0.0, 0.0, 1.0, 1.0, 0.0)
    daily = daily.copy()
    daily.index = pd.to_datetime(daily.index).normalize()
    daily = daily.groupby(level=0).sum().asfreq("D").fillna(0.0).clip(lower=0.0)
    if daily.sum() <= 0:
        return Forecast(0.0, 0.0, 1.0, 1.0, 0.0)
    seasonal = _external_seasonality(seasonal_factors) or _month_seasonality(daily)
    factors = pd.Series([seasonal.get(d.month, 1.0) for d in daily.index], index=daily.index)
    deseasoned = daily.div(factors.replace(0, np.nan)).fillna(0.0)
    recent = deseasoned.iloc[-90:]
    level = float(recent.mean())
    anchor = recent.index[0] + (recent.index[-1] - recent.index[0]) / 2
    sigma = float(daily.iloc[-90:].std(ddof=1)) if len(recent) > 1 else 0.0
    return _project(level, _trend_slope_per_day(deseasoned), anchor, seasonal,
                    horizon_start, horizon_days, sigma)


def forecast_monthly_demand(
    monthly_qty: pd.Series, horizon_start: date, horizon_days: int,
    seasonal_factors: dict[int, float] | None = None,
) -> Forecast:
    """Forecast from completed month totals, without fabricating daily sales.

    Between-month rate variation is observable; intramonth variance is not.
    Safety uses the larger of that variation and a Poisson daily-demand baseline.
    The caller exposes this assumption in the recommendation's warnings.
    """
    if monthly_qty.empty:
        return Forecast(0.0, 0.0, 1.0, 1.0, 0.0)
    monthly_qty = monthly_qty.copy()
    monthly_qty.index = pd.to_datetime(monthly_qty.index).to_period("M").to_timestamp()
    monthly_qty = monthly_qty.groupby(level=0).sum().sort_index().clip(lower=0.0)
    # Partial/current months must not be treated as complete demand observations.
    cutoff = pd.Timestamp(horizon_start).to_period("M").to_timestamp()
    monthly_qty = monthly_qty[monthly_qty.index < cutoff]
    if monthly_qty.empty or monthly_qty.sum() <= 0:
        return Forecast(0.0, 0.0, 1.0, 1.0, 0.0)
    rates = monthly_qty / monthly_qty.index.days_in_month
    seasonal = _external_seasonality(seasonal_factors) or _monthly_seasonality(rates)
    factors = pd.Series([seasonal.get(d.month, 1.0) for d in rates.index], index=rates.index)
    deseasoned = rates.div(factors.replace(0, np.nan)).fillna(0.0)
    # Rates represent the whole month and are anchored at its midpoint.
    deseasoned.index += pd.to_timedelta((deseasoned.index.days_in_month - 1) / 2, unit="D")
    recent = deseasoned.iloc[-3:]
    level = float(recent.mean())
    anchor = pd.Timestamp(int(np.mean(recent.index.asi8)))
    result = _project(level, _slope(deseasoned.iloc[-6:]), anchor, seasonal,
                      horizon_start, horizon_days, 0.0)
    variation = float(deseasoned.iloc[-6:].std(ddof=1)) if len(deseasoned) > 1 else 0.0
    result.sigma_daily = round(max(np.sqrt(result.avg_daily_demand), variation), 4)
    return result
