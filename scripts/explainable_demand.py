#!/usr/bin/env python3
"""Прогноз по месячным матрицам: уровень, сезонность и устойчивый линейный тренд."""
import numpy as np
import pandas as pd

DAMPING_OPTIONS = (0.0, 0.5, 1.0)


def month_index(dates):
    dates = pd.DatetimeIndex(dates)
    return (dates.year * 12 + dates.month - 1).to_numpy(dtype=float)


def slope(x, y):
    if len(x) < 2 or np.ptp(x) == 0:
        return 0.0
    centered = x - np.mean(x)
    return float(np.dot(centered, y - np.mean(y)) / np.dot(centered, centered))


def seasonal_factors(x, y, months):
    """Только прошлое; не менее двух наблюдений каждого календарного месяца."""
    counts = np.bincount(months, minlength=13)[1:]
    neutral = np.ones(12, dtype=float)
    if len(y) < 24 or (counts < 2).any():
        return neutral, "insufficient_history"
    # Убираем начальную линейную оценку уровня перед оценкой сезонных отношений.
    trend = slope(x, y)
    fitted = np.mean(y) + trend * (x - np.mean(x))
    if np.mean(y) <= 0 or (fitted <= 0).any():
        return neutral, "nonpositive_reference_level"
    ratios = y / fitted
    factors = np.array([np.median(ratios[months == month]) for month in range(1, 13)])
    # Усадка к 1 защищает коэффициенты, оцененные всего по двум-трём годам.
    factors = 1 + counts / (counts + 2.0) * (factors - 1)
    factors = np.clip(factors, 0.25, 4.0)
    return factors / factors.mean(), "estimated_from_past"


def fit_sku(sku, dates, values, cutoff):
    valid = np.isfinite(values) & (values >= 0)
    dates = pd.DatetimeIndex(dates)[valid]
    y = np.asarray(values, dtype=float)[valid]
    result = dict(sku=sku, observed_months=len(y), seasonal_factors=[1.0] * 12,
                  seasonality_status="insufficient_history", level_qty=None,
                  anchor_month_index=None, slope_12=0.0, slope_6=0.0,
                  stable_trend_per_month=0.0, trend_status="insufficient_history",
                  latest_observed_month=str(dates.max().date()) if len(dates) else None,
                  level_window="none", leave_one_out_sign_share=None)
    if len(y) < 3:
        return result
    x = month_index(dates)
    months = dates.month.to_numpy()
    factors, seasonal_status = seasonal_factors(x, y, months)
    adjusted = y / factors[months - 1]
    cutoff_index = month_index([cutoff])[0]
    recent = x >= cutoff_index - 3
    window = "3_calendar_months"
    if not recent.any():
        recent = x >= cutoff_index - 6
        window = "6_calendar_months"
    if not recent.any():
        recent = np.ones(len(x), dtype=bool)
        window = "all_known_history_stale"
    level, anchor = float(np.mean(adjusted[recent])), float(np.mean(x[recent]))
    long_mask, short_mask = x >= cutoff_index - 12, x >= cutoff_index - 6
    long_x, long_y = x[long_mask], adjusted[long_mask]
    short_x, short_y = x[short_mask], adjusted[short_mask]
    applied, long_slope, short_slope, share = 0.0, 0.0, 0.0, None
    status = "insufficient_recent_history"
    if len(long_x) >= 8 and len(short_x) >= 4:
        long_slope, short_slope = slope(long_x, long_y), slope(short_x, short_y)
        tolerance = max(level, 1.0) * 1e-8
        if abs(long_slope) <= tolerance or abs(short_slope) <= tolerance:
            status = "flat_or_unstable"
        else:
            loo = np.array([slope(np.delete(long_x, i), np.delete(long_y, i)) for i in range(len(long_x))])
            share = float(np.mean(np.sign(loo) == np.sign(long_slope)))
            ratio = abs(short_slope / long_slope)
            if np.sign(long_slope) != np.sign(short_slope) or not 0.5 <= ratio <= 2.0 or share < 0.8:
                status = "unstable_trend_disabled"
            else:
                limit = level * 0.10
                applied = float(np.clip(long_slope, -limit, limit))
                status = "stable_capped" if not np.isclose(applied, long_slope) else "stable"
    result.update(seasonal_factors=factors.tolist(), seasonality_status=seasonal_status,
                  level_qty=level, anchor_month_index=anchor, slope_12=long_slope,
                  slope_6=short_slope, stable_trend_per_month=applied,
                  trend_status=status, level_window=window, leave_one_out_sign_share=share)
    return result


def fit_decomposition(panel, cutoff):
    cutoff = pd.Timestamp(cutoff).to_period("M").to_timestamp()
    if panel.duplicated(["sku", "month"]).any():
        raise ValueError("Повтор ключа товар–месяц в месячной панели")
    past = panel[panel.month < cutoff].sort_values(["sku", "month"])
    products = {}
    for sku, group in past.groupby("sku", sort=False):
        products[sku] = fit_sku(sku, group.month, group.target_qty.to_numpy(float), cutoff)
    return dict(name="decomposition", trained_before=str(cutoff.date()), products=products,
                settings=dict(min_history=3, seasonality_min_observations_per_month=2,
                              seasonal_shrinkage=2, slope_windows=[12, 6],
                              trend_min_observations=[8, 4], slope_ratio_range=[0.5, 2.0],
                              leave_one_out_min_sign_share=0.8, max_trend_share_per_month=0.10))


def predict_components(bundle, frame, damping):
    if damping not in DAMPING_OPTIONS:
        raise ValueError("Неизвестная сила тренда")
    if (frame.target_month < pd.Timestamp(bundle["trained_before"])).any():
        raise ValueError("Нельзя объяснять прошлое моделью, обученной на его будущем")
    records = []
    for row in frame.itertuples():
        record = dict(sku=row.sku, unit=row.unit, target_month=row.target_month,
                      predicted_qty=np.nan, level_qty=np.nan, seasonal_factor=1.0,
                      trend_per_month=0.0, months_from_level_anchor=np.nan,
                      trend_contribution_qty=np.nan, deseasonalized_forecast=np.nan,
                      damping=damping, status="insufficient_history", warnings="")
        fitted = bundle["products"].get(row.sku)
        if fitted is None or fitted["level_qty"] is None:
            records.append(record)
            continue
        offset = month_index([row.target_month])[0] - fitted["anchor_month_index"]
        trend = fitted["stable_trend_per_month"] * damping
        factor = fitted["seasonal_factors"][pd.Timestamp(row.target_month).month - 1]
        deseasonalized = max(0.0, fitted["level_qty"] + trend * offset)
        warnings = []
        if fitted["seasonality_status"] != "estimated_from_past":
            warnings.append("neutral_seasonality_insufficient_evidence")
        if fitted["trend_status"] not in ("stable", "stable_capped"):
            warnings.append(fitted["trend_status"])
        if fitted["level_window"] != "3_calendar_months":
            warnings.append(fitted["level_window"])
        record.update(predicted_qty=deseasonalized * factor, level_qty=fitted["level_qty"],
                      seasonal_factor=factor, trend_per_month=trend, months_from_level_anchor=offset,
                      trend_contribution_qty=trend * offset, deseasonalized_forecast=deseasonalized,
                      status="forecast", warnings=";".join(warnings))
        records.append(record)
    return pd.DataFrame(records)
