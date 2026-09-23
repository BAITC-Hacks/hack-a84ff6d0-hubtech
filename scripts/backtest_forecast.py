#!/usr/bin/env python3
"""Offline rolling-origin evaluation of the existing monthly forecast, without fitting.

Run from the repository root with the backend Python environment. See BACKTEST.md
for the target/missing-data policy, paired comparison and interpretation limits.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import sys

import numpy as np
import openpyxl
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.forecasting import forecast_monthly_demand  # noqa: E402
from app.data.excel import SUPPLIER_FILES  # noqa: E402
from prepare_ml_data import catalog_add, code, read_matrix, table, value  # noqa: E402
from clean_monthly_data import clean_text  # noqa: E402


MODELS = ("current", "last_month", "same_month_last_year")
SCENARIOS = ("unknown_blanks", "operational_zero_assumption")
METRIC_COLUMNS = [
    "supplier", "scenario", "split", "unit", "cohort", "model", "rows", "skus",
    "actual_total_qty", "prediction_total_qty", "mae_qty", "mean_error_qty",
    "wape_percent", "bias_percent", "zero_actual_rows", "zero_actual_mae_qty",
    "short_history_rows", "macro_sku_wape_percent", "macro_sku_wape_skus",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(path: Path, root: Path) -> dict:
    return {"path": str(path.relative_to(root)), "sha256": sha256(path),
            "size_bytes": path.stat().st_size}


def evaluation_months(last_complete_month, validation_months=6, test_months=2):
    if validation_months < 1 or test_months < 1:
        raise ValueError("Validation и test должны содержать хотя бы один месяц")
    last = pd.Timestamp(last_complete_month).to_period("M").to_timestamp()
    months = pd.date_range(end=last, periods=validation_months + test_months, freq="MS")
    return [(month, "validation" if i < validation_months else "test")
            for i, month in enumerate(months)]


def read_unit_catalog(path: Path, catalog: dict) -> None:
    """Read identity/units only; inventory quantities never enter the forecast."""
    with table(path, "Номенклатура.Код") as (headers, rows, _):
        ci, ni = headers.index("номенклатура.код"), headers.index("номенклатура")
        ui = next((headers.index(label) for label in ("ед.", "ед.изм") if label in headers), None)
        if ui is None:
            raise ValueError(f"{path.name}: нет единиц измерения")
        for _, row in rows:
            if clean_text(value(row, ni)).casefold() in {"итого", "всего"}:
                continue
            sku = code(value(row, ci))
            if sku:
                catalog_add(catalog, sku, value(row, ni), value(row, ui))


def apply_policy(sales: pd.DataFrame, scenario: str) -> pd.Series:
    """Explicit zeros stay zeros; malformed/absent cells never become zero."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Неизвестная политика: {scenario}")
    known = sales.sales_status.eq("observed") & sales.sales_qty_raw.ge(0)
    quantities = sales.sales_qty_raw.where(known).astype(float)
    if scenario == "operational_zero_assumption":
        quantities = quantities.mask(sales.sales_status.isin(["blank", "negative"]), 0.0)
    return quantities


def predict_at_origin(history: pd.Series, target_month: pd.Timestamp) -> dict:
    """Cut off future values again at this boundary, even if the caller passes them."""
    target_month = pd.Timestamp(target_month).to_period("M").to_timestamp()
    history = history.loc[history.index < target_month].dropna().sort_index()
    if not history.empty and (not np.isfinite(history).all() or history.lt(0).any()):
        raise ValueError("История прогноза должна содержать конечные неотрицательные значения")
    predictions = {model: np.nan for model in MODELS}
    if not history.empty:
        forecast = forecast_monthly_demand(
            history.copy(), target_month.date(), target_month.days_in_month,
            seasonal_factors=None,
        )
        predictions["current"] = forecast.avg_daily_demand * target_month.days_in_month
        predictions["last_month"] = history.get(target_month - pd.DateOffset(months=1), np.nan)
        predictions["same_month_last_year"] = history.get(target_month - pd.DateOffset(years=1), np.nan)
    return predictions


def evaluate_sales(sales, units, supplier, months, scenario):
    sales = sales.copy()
    sales["quantity"] = apply_policy(sales, scenario)
    if sales.duplicated(["sku", "month"]).any():
        raise ValueError("Для одного SKU есть повторяющийся месяц")
    records = []
    for sku, group in sales.groupby("sku", sort=True):
        group = group.set_index("month").sort_index()
        series = group.quantity
        for month, split in months:
            past = series.loc[series.index < month].dropna()
            target = group.loc[month] if month in group.index else None
            records.append({
                "supplier": supplier, "scenario": scenario, "split": split,
                "sku": sku, "unit": units.get(sku, ""), "target_month": month.date().isoformat(),
                "actual": float(target.quantity) if target is not None else np.nan,
                "raw_status": str(target.sales_status) if target is not None else "not_in_source",
                "history_observed_months": len(past), "short_history": len(past) < 3,
                "history_last_month": past.index[-1].date().isoformat() if len(past) else "",
                **predict_at_origin(series, month),
            })
    return pd.DataFrame(records)


def metric_values(frame: pd.DataFrame, model: str) -> dict:
    if frame.unit.nunique() != 1 or frame.unit.eq("").any():
        raise ValueError("Абсолютные метрики требуют одной известной единицы измерения")
    if frame.empty or frame[["actual", model]].isna().any().any():
        raise ValueError("Метрики требуют непустых пар факт/прогноз")
    error = frame[model] - frame.actual
    absolute = error.abs()
    actual_sum = float(frame.actual.sum())
    per_sku = pd.DataFrame({"sku": frame.sku, "absolute": absolute, "actual": frame.actual})
    per_sku = per_sku.groupby("sku").sum()
    eligible_skus = per_sku.actual.gt(0)
    sku_ratios = per_sku.loc[eligible_skus, "absolute"] / per_sku.loc[eligible_skus, "actual"]
    zero_targets = frame.actual.eq(0)
    return {
        "rows": len(frame), "skus": int(frame.sku.nunique()),
        "actual_total_qty": actual_sum, "prediction_total_qty": float(frame[model].sum()),
        "mae_qty": float(absolute.mean()), "mean_error_qty": float(error.mean()),
        "wape_percent": float(100 * absolute.sum() / actual_sum) if actual_sum else None,
        "bias_percent": float(100 * error.sum() / actual_sum) if actual_sum else None,
        "zero_actual_rows": int(zero_targets.sum()),
        "zero_actual_mae_qty": float(absolute[zero_targets].mean()) if zero_targets.any() else None,
        "short_history_rows": int(frame.short_history.sum()),
        "macro_sku_wape_percent": float(100 * sku_ratios.mean()) if len(sku_ratios) else None,
        "macro_sku_wape_skus": int(eligible_skus.sum()),
    }


def summarize(predictions: pd.DataFrame):
    metrics, coverage = [], []
    group_keys = ["supplier", "scenario", "split", "unit"]
    for keys, group in predictions.groupby(group_keys, dropna=False, sort=True):
        context = dict(zip(group_keys, keys))
        known = group.actual.notna()
        common = known & group[list(MODELS)].notna().all(axis=1)
        coverage.append({
            **context, "candidate_rows": len(group), "candidate_skus": int(group.sku.nunique()),
            "raw_target_status_counts": dict(Counter(group.raw_status)),
            "known_targets": int(known.sum()), "known_target_fraction": float(known.mean()),
            "zero_targets_after_policy": int(group.actual.eq(0).sum()),
            "positive_targets_after_policy": int(group.actual.gt(0).sum()),
            "unknown_unit_excluded": int(len(group) if keys[-1] == "" else 0),
            "known_targets_without_history": int((known & group.history_observed_months.eq(0)).sum()),
            "short_history_known_targets": int((known & group.short_history).sum()),
            "available_pairs": {model: int((known & group[model].notna()).sum()) for model in MODELS},
            "common_all_models_pairs": int(common.sum()),
            "common_all_models_fraction_of_candidates": float(common.mean()),
        })
        if keys[-1] == "":
            continue
        cohorts = {
            "available": None,
            "paired_last_month": ("current", "last_month"),
            "paired_same_month_last_year": ("current", "same_month_last_year"),
            "common_all_models": MODELS,
        }
        for cohort, models in cohorts.items():
            for model in models or MODELS:
                mask = known & group[list(models or (model,))].notna().all(axis=1)
                if mask.any():
                    metrics.append({**context, "cohort": cohort, "model": model,
                                    **metric_values(group.loc[mask], model)})
    return pd.DataFrame(metrics, columns=METRIC_COLUMNS), coverage


def run_backtest(input_root=ROOT, output_dir=None, as_of="2026-09-23",
                 last_complete_month="2026-08", validation_months=6, test_months=2,
                 suppliers=None, scenarios=SCENARIOS, write_predictions=False):
    input_root = Path(input_root).resolve()
    output_dir = Path(output_dir or ROOT / "data" / "evaluation")
    cutoff = pd.Timestamp(as_of).to_period("M").to_timestamp()
    months = evaluation_months(last_complete_month, validation_months, test_months)
    if months[-1][0] >= cutoff:
        raise ValueError("Последний полный месяц должен быть раньше месяца --as-of")
    suppliers = tuple(suppliers or SUPPLIER_FILES)
    all_predictions, sources, source_profiles = [], {}, {}
    for supplier in suppliers:
        spec = SUPPLIER_FILES[supplier]
        sales_path = input_root / spec["folder"] / spec["monthly_sales"]
        unit_path = input_root / spec["folder"] / spec["monthly_stock"]
        catalog, issues = {}, []
        sales = read_matrix(sales_path, "sales", catalog, issues)
        read_unit_catalog(unit_path, catalog)
        # An explicit completion boundary never extends a shorter matrix.
        if months[-1][0] > sales.month.max() or months[0][0] < sales.month.min():
            raise ValueError(f"{supplier}: период проверки не содержится в месячной матрице")
        units = {sku: product["unit"] for sku, product in catalog.items()}
        sources[supplier] = {"monthly_sales": source_manifest(sales_path, input_root),
                             "unit_metadata_only": source_manifest(unit_path, input_root)}
        source_profiles[supplier] = {
            "sales_skus": int(sales.sku.nunique()), "sales_cells": len(sales),
            "first_source_month": sales.month.min().date().isoformat(),
            "last_source_month": sales.month.max().date().isoformat(),
            "raw_sales_status_counts": dict(Counter(sales.sales_status)),
            "unit_counts_sales_skus": dict(Counter(units.get(sku, "") for sku in sales.sku.unique())),
            "stock_only_skus_not_evaluated": len(set(catalog) - set(sales.sku)),
            "issue_counts": dict(Counter(issue["issue"] for issue in issues)),
        }
        for scenario in scenarios:
            print(f"Backtest: {supplier}, {scenario}", flush=True)
            all_predictions.append(evaluate_sales(sales, units, supplier, months, scenario))
    predictions = pd.concat(all_predictions, ignore_index=True)
    metrics, coverage = summarize(predictions)
    implementation_paths = [Path(__file__), ROOT / "backend/app/core/forecasting.py",
                            ROOT / "backend/app/data/excel.py", ROOT / "scripts/prepare_ml_data.py",
                            ROOT / "scripts/clean_monthly_data.py"]
    report = {
        "schema_version": 1, "as_of": str(pd.Timestamp(as_of).date()),
        "last_complete_month": months[-1][0].strftime("%Y-%m"),
        "protocol": {
            "horizon": "one complete calendar month, rolling origin",
            "validation_months": [m.strftime("%Y-%m") for m, split in months if split == "validation"],
            "test_months": [m.strftime("%Y-%m") for m, split in months if split == "test"],
            "historical_input": "only known monthly quantities with month < target month",
            "external_seasonality": False, "llm": False, "model_fitting": False,
            "parameter_search": False, "forecast_code_modified_for_evaluation": False,
            "test_is_sequential": "July actual can enter August history after July completes",
            "target": "reported monthly net sales under explicit scenario policy; not latent demand",
            "baseline_last_month": "previous calendar month quantity, no fallback or day-length scaling",
            "baseline_same_month_last_year": "quantity of the same calendar month one year earlier, no fallback",
            "short_history": "current runs with >=1 known month; <3 reported separately; no history means unavailable",
            "units": "no combined raw MAE/WAPE across units; unit metadata does not enter prediction",
            "zero_actual": "retained in MAE; WAPE/bias null when sum(actual)=0; zero-target MAE reported",
            "comparison": "compare models within paired_* or common_all_models, not unequal available cohorts",
            "scenarios": {
                "unknown_blanks": "observed >=0 only; blanks/negative/invalid/absent remain unknown",
                "operational_zero_assumption": "present blank=0 and negative net month clipped to 0; invalid/absent remain unknown",
            },
            "evaluated_scenarios": list(scenarios),
        },
        "runtime": {"python": platform.python_version(), "pandas": pd.__version__,
                    "numpy": np.__version__, "openpyxl": openpyxl.__version__},
        "implementation": {str(p.relative_to(ROOT)): sha256(p) for p in implementation_paths},
        "sources": sources, "source_profiles": source_profiles, "coverage": coverage,
        "artifacts": {"metrics": "forecast_metrics.csv", "prediction_rows_written": bool(write_predictions),
                      "prediction_rows_evaluated": len(predictions), "metric_rows": len(metrics)},
        "limitations": [
            "No pre-existing point-in-time snapshots: retrospective matrices may contain later corrections and survivorship bias.",
            "Monthly report warehouse and unit mapping need partner confirmation; unit catalogue is current metadata, not a historical snapshot.",
            "The explicit last-complete-month boundary is a reproducible assumption, not proof of accounting completeness.",
            "Blank-as-zero is unconfirmed; unknown-only scoring selects months with known sales and is not whole-assortment accuracy.",
            "Launch/discontinuation dates are unavailable; blank-as-zero can create pre-launch zero months.",
            "Returns, stockouts and wholesale clients are not identified here; this scores forecast_monthly_demand directly, not the full ordering pipeline.",
            "Only two held-out months; no confidence interval, business ROI or general production accuracy is claimed.",
            "Separate validation/test is implemented; historical development may already have inspected these source files. This is not a blinded prospective trial.",
            "No ML model is trained. Algorithms are not selected or tuned by this script. Subsequent changes must report this test reuse and reserve a new future holdout.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_dir / "forecast_metrics.csv", index=False, encoding="utf-8", float_format="%.8f")
    if write_predictions:
        predictions.to_csv(output_dir / "forecast_predictions.csv", index=False, encoding="utf-8", float_format="%.8f")
        report["artifacts"]["predictions"] = "forecast_predictions.csv"
    (output_dir / "forecast_backtest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/evaluation")
    parser.add_argument("--as-of", default="2026-09-23")
    parser.add_argument("--last-complete-month", default="2026-08")
    parser.add_argument("--validation-months", type=int, default=6)
    parser.add_argument("--test-months", type=int, default=2)
    parser.add_argument("--supplier", choices=tuple(SUPPLIER_FILES), action="append")
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--write-predictions", action="store_true",
                        help="Сохранить подробный CSV; по умолчанию только компактные метрики/покрытие")
    args = parser.parse_args()
    try:
        report, _ = run_backtest(args.input_root, args.output_dir, args.as_of, args.last_complete_month,
                                 args.validation_months, args.test_months, args.supplier,
                                 tuple(args.scenario or SCENARIOS), args.write_predictions)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Ошибка: {error}\n")
    print(f"Готово: {report['artifacts']['metric_rows']} строк метрик; {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
