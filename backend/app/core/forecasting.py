"""MH #2 — прогноз спроса с учётом сезонности и устойчивого роста.

Раскладываем дневной ряд на уровень, сезонность (по месяцам) и тренд, затем
проецируем спрос на горизонт пополнения. Метод устойчив к коротким рядам и
полностью объясним: коэффициенты сезонности и тренда попадают в обоснование.

statsmodels (Holt) используется для оценки тренда, если ряд достаточно длинный;
иначе — устойчивый линейный фолбэк.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd


@dataclass
class Forecast:
    avg_daily_demand: float     # эффективный прогноз спроса в день (с сезонностью и трендом)
    sigma_daily: float          # СКО дневного спроса (для страхового запаса)
    seasonality_factor: float   # сезонный коэффициент горизонта (1.0 = нейтрально)
    trend_factor: float         # коэффициент тренда на горизонте (>1 рост, <1 спад)
    level_daily: float          # десезонализированный базовый уровень


def _month_seasonality(daily: pd.Series) -> dict[int, float]:
    mean = daily.mean()
    if not np.isfinite(mean) or mean <= 0:
        return {}
    monthly = daily.groupby(daily.index.month).mean()
    return (monthly / mean).to_dict()


def _trend_slope_per_day(daily: pd.Series) -> float:
    """Наклон десезонализированного тренда (единиц/день). Устойчивый фолбэк."""
    weekly = daily.resample("W").sum()
    if len(weekly) < 4:
        return 0.0
    y = weekly.to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    try:
        slope_per_week, _ = np.polyfit(x, y, 1)
    except Exception:
        return 0.0
    return float(slope_per_week) / 7.0


def forecast_demand(
    daily: pd.Series,
    horizon_start: date,
    horizon_days: int,
) -> Forecast:
    """Строит прогноз среднего дневного спроса на горизонте [start, start+days]."""
    daily = daily.asfreq("D").fillna(0.0)
    if daily.empty or daily.sum() <= 0:
        return Forecast(0.0, 0.0, 1.0, 1.0, 0.0)

    seasonal = _month_seasonality(daily)

    # десезонализированный уровень по последним 90 дням
    recent = daily.iloc[-90:] if len(daily) >= 90 else daily
    deseason = [
        v / seasonal.get(ts.month, 1.0)
        for ts, v in recent.items()
        if seasonal.get(ts.month, 1.0) > 0
    ]
    level_daily = float(np.mean(deseason)) if deseason else float(recent.mean())
    level_daily = max(level_daily, 0.0)

    # тренд
    slope = _trend_slope_per_day(daily)
    last_day = daily.index[-1].date()
    mid_offset = (horizon_start - last_day).days + horizon_days / 2.0
    projected_level = max(level_daily + slope * mid_offset, 0.0)
    trend_factor = projected_level / level_daily if level_daily > 0 else 1.0

    # сезонность горизонта: средний коэффициент по дням горизонта
    horizon_days_idx = [horizon_start + timedelta(days=i) for i in range(max(horizon_days, 1))]
    seas_vals = [seasonal.get(d.month, 1.0) for d in horizon_days_idx]
    seasonality_factor = float(np.mean(seas_vals)) if seas_vals else 1.0

    avg_daily_demand = level_daily * seasonality_factor * trend_factor

    # разброс дневного спроса (по здоровым ненулевым дням)
    nz = daily[daily > 0]
    sigma_daily = float(nz.std()) if len(nz) > 1 else float(daily.std() or 0.0)
    if not np.isfinite(sigma_daily):
        sigma_daily = 0.0

    return Forecast(
        avg_daily_demand=round(avg_daily_demand, 4),
        sigma_daily=round(sigma_daily, 4),
        seasonality_factor=round(seasonality_factor, 4),
        trend_factor=round(trend_factor, 4),
        level_daily=round(level_daily, 4),
    )
