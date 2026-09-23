#!/usr/bin/env python3
"""Проверка объяснимой декомпозиции и кандидатов на крупные заказы двух поставщиков."""
import argparse
import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from train_iek_model import (ROOT, CANDIDATES, UNITS, read_csv, attach_history,
                            validate_inputs, metric_table, fit_model, predict_model)
from explainable_demand import DAMPING_OPTIONS, fit_decomposition, predict_components
from detect_exceptional_orders import detect_candidates


def scores_for(frame):
    table = metric_table(frame)
    valid = table[table.unit.isin(UNITS) & table.wape.notna()]
    return valid.groupby("model").wape.mean().to_dict()


def select_damping(validation):
    if not validation.split.eq("validation").all():
        raise ValueError("Силу тренда можно выбирать только на validation")
    scores = scores_for(validation)
    names = [f"decomposition_damping_{d:g}" for d in DAMPING_OPTIONS]
    if set(scores) != set(names):
        raise ValueError("Не все варианты декомпозиции оценены")
    name = min(names, key=lambda n: (scores[n], names.index(n)))
    return DAMPING_OPTIONS[names.index(name)], scores


def decompose_folds(panel, data, split, damping_options):
    frames = []
    for month in sorted(data.loc[data.split.eq(split), "target_month"].unique()):
        month = pd.Timestamp(month)
        print(f"Декомпозиция {split}: {month.date()}", flush=True)
        bundle = fit_decomposition(panel, month)
        evaluation = data[data.target_month.eq(month)]
        for damping in damping_options:
            predicted = predict_components(bundle, evaluation, damping)
            if not np.isfinite(predicted.predicted_qty).all():
                raise ValueError("Нет прогноза для части сопоставимых тестовых строк")
            predicted["actual_qty"] = evaluation.target_qty.to_numpy()
            predicted["residual_qty"] = predicted.actual_qty - predicted.predicted_qty
            predicted["split"] = split
            predicted["model"] = f"decomposition_damping_{damping:g}"
            predicted["trained_before"] = month
            frames.append(predicted)
    if not frames:
        raise ValueError(f"Нет периода {split}")
    return pd.concat(frames, ignore_index=True)


def predict_selected(bundle, features):
    if bundle["name"] == "decomposition":
        return predict_components(bundle, features, bundle["damping"]).predicted_qty.to_numpy()
    return predict_model(bundle, features)


def verify_baselines(backtest, data):
    keys = ["sku", "target_month"]
    for split in ("validation", "test"):
        expected = data[data.split.eq(split)].set_index(keys).target_qty.sort_index()
        for model in CANDIDATES:
            subset = backtest[backtest.split.eq(split) & backtest.model.eq(model)]
            if subset.duplicated(keys).any():
                raise ValueError("Повтор baseline-строки")
            actual = subset.set_index(keys).actual_qty.sort_index()
            if not expected.index.equals(actual.index) or not np.allclose(expected, actual):
                raise ValueError("Сохранённые базовые прогнозы относятся к другой выборке")
    if not np.isfinite(backtest.predicted_qty).all():
        raise ValueError("Некорректный сохранённый прогноз")
    if not (pd.to_datetime(backtest.trained_through) < backtest.target_month).all():
        raise ValueError("Нарушены временные границы базового теста")


def run(supplier):
    if supplier not in ("iek", "systeme"):
        raise ValueError("Неизвестный поставщик")
    folder = ROOT / "data/ml" / supplier
    old_forecasts = ROOT / "data/forecasts" / supplier
    output = old_forecasts / "decomposition"
    model_dir = ROOT / "models" / supplier / "decomposition_experiment"
    prep = json.loads((folder / "preparation_report.json").read_text())
    baseline_report = json.loads((old_forecasts / "training_report.json").read_text())
    for source in prep["sources"].values():
        if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("Исходные данные изменились: повторите подготовку и базовую проверку")
    for name, digest in baseline_report["prepared_file_hashes"].items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Подготовленные данные отличаются от базового теста")
    panel = read_csv(folder / "monthly_panel.csv", ["month"])
    data = read_csv(folder / "training_data.csv", ["target_month"])
    features = read_csv(folder / "prediction_features.csv", ["target_month"])
    validate_inputs(data, features, panel, prep)
    baselines = read_csv(old_forecasts / "backtest_predictions.csv", ["target_month"])
    verify_baselines(baselines, data)
    validation = decompose_folds(panel, data, "validation", DAMPING_OPTIONS)
    damping, tuning_scores = select_damping(validation)
    chosen = validation[validation.damping.eq(damping)].copy()
    chosen["model"] = "decomposition"
    # Фиксируем вариант и общее решение по validation до просмотра нового test.
    comparison_validation = pd.concat([baselines[baselines.split.eq("validation")], chosen], ignore_index=True)
    validation_scores = scores_for(comparison_validation)
    priority = [*CANDIDATES, "decomposition"]
    selected_name = min(priority, key=lambda name: (validation_scores[name], priority.index(name)))
    print(f"{supplier}: damping={damping}; выбран на validation {selected_name}", flush=True)
    test = decompose_folds(panel, data, "test", (damping,))
    test["model"] = "decomposition"
    comparison = pd.concat([baselines, chosen, test], ignore_index=True)
    metrics = metric_table(comparison)
    final_bundle = fit_decomposition(panel, prep["prediction_month"])
    final_bundle.update(damping=damping, supplier_id=supplier.upper())
    components = predict_components(final_bundle, features, damping)
    components["model"] = "decomposition"
    components["supplier_id"] = supplier.upper()
    parameters, seasonal = [], []
    for sku, fitted in final_bundle["products"].items():
        parameters.append({k: v for k, v in fitted.items() if k != "seasonal_factors"})
        for month, factor in enumerate(fitted["seasonal_factors"], 1):
            seasonal.append(dict(sku=sku, month_number=month, seasonal_factor=factor,
                                 status=fitted["seasonality_status"], trained_before=prep["prediction_month"]))
    if selected_name == "decomposition":
        selected_bundle = final_bundle
    else:
        iterations = baseline_report.get("max_iter", baseline_report.get("hyperparameters", {}).get("max_iter", 160))
        selected_bundle = fit_model(selected_name, attach_history(data, panel), iterations)
        selected_bundle.update(supplier_id=supplier.upper(), trained_before=prep["prediction_month"])
    prediction = attach_history(features, panel)
    selected_forecast = prediction[["sku", "unit", "target_month"]].copy()
    sufficient = prediction.history_observed_months.ge(3)
    selected_forecast["predicted_qty"] = np.nan
    selected_forecast.loc[sufficient, "predicted_qty"] = predict_selected(selected_bundle, prediction.loc[sufficient])
    selected_forecast["status"] = np.where(sufficient, "forecast", "insufficient_history")
    selected_forecast["model"] = selected_name
    selected_forecast["supplier_id"] = supplier.upper()
    transactions = pd.read_csv(folder / "transactions.csv", parse_dates=["date"],
        dtype={"sku": str, "unit": str, "warehouse": str, "document_number": str, "document_type": str},
        keep_default_na=False, na_values=[""])
    reconciliation = pd.read_csv(folder / "reconciliation.csv", dtype={"sku": str}, parse_dates=["month"])
    print(f"{supplier}: проверка размеров документов", flush=True)
    candidates, history, candidate_report = detect_candidates(transactions, panel, reconciliation, prep["as_of"])
    diagnostics = []
    for (model, unit), group in comparison[comparison.split.eq("test")].groupby(["model", "unit"]):
        positive = group[group.actual_qty.gt(0)]
        ape = (positive.predicted_qty - positive.actual_qty).abs() / positive.actual_qty
        diagnostics.append(dict(model=model, unit=unit, rows=len(group), positive_rows=len(positive),
                                median_ape=float(ape.median()) if len(ape) else None,
                                share_within_20pct=float(ape.le(0.2).mean()) if len(ape) else None))
    summary = dict(supplier_id=supplier.upper(), as_of=prep["as_of"], selected_model=selected_name,
        decomposition_damping=damping, damping_validation_scores=tuning_scores,
        validation_scores=validation_scores, test_diagnostics=diagnostics,
        formula="max(0, level_qty + trend_per_month * months_from_level_anchor) * seasonal_factor",
        stockout_compensation=False, exceptional_orders=candidate_report,
        forecast_rows=len(components), forecast_available=int(components.predicted_qty.notna().sum()),
        seasonality_status_counts=pd.DataFrame(parameters).seasonality_status.value_counts().to_dict(),
        trend_status_counts=pd.DataFrame(parameters).trend_status.value_counts().to_dict(),
        settings=final_bundle["settings"],
        prepared_file_hashes=baseline_report["prepared_file_hashes"],
        baseline_predictions_sha256=hashlib.sha256((old_forecasts / "backtest_predictions.csv").read_bytes()).hexdigest(),
        warnings=[
            "Месячные матрицы — источник количества. Транзакции не прибавляются к объёмам.",
            "Кандидаты на исключительные заказы отмечены для проверки, исключённый объём равен нулю.",
            "Совпадение месячной суммы не подтверждает область склада, клиента или разовость заказа.",
            "Пропуски не заменены нулями; точность относится к известным неотрицательным целям.",
            "Готовые коэффициенты Excel не используются. Сезонность пересчитывается из прошлого в каждом временном шаге.",
            "Две полные известные сезонные истории требуются для оценки всех 12 коэффициентов; иначе коэффициенты равны 1.",
            "Границы test уже использовались в предыдущем исследовании. Этот тест — повторное сравнение, а не новый независимый финальный holdout.",
            "Сила тренда и победитель выбираются только по validation. Для окончательного подтверждения нужны новые месяцы.",
            "Сентябрь прогнозируется целиком. Страховой запас и заказы здесь не рассчитываются.",
            "Предыдущие результаты сохранены; новые результаты находятся в отдельной папке decomposition.",
        ])
    output.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    tables = dict(decomposition_validation=validation, decomposition_backtest=pd.concat([chosen, test], ignore_index=True),
                  comparison_metrics=metrics, comparison_predictions=comparison,
                  decomposition_forecast=components, selected_forecast=selected_forecast,
                  fitted_components=pd.DataFrame(parameters), seasonal_factors=pd.DataFrame(seasonal),
                  exceptional_order_candidates=candidates, demand_history=history)
    for name, table in tables.items():
        table.to_csv(output / f"{name}.csv", index=False, encoding="utf-8-sig", na_rep="")
    for name, bundle in [("decomposition", final_bundle), ("selected_model", selected_bundle)]:
        with (model_dir / f"{name}.pkl").open("wb") as f:
            pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = [f"# Декомпозиция спроса: {supplier.upper()}", "",
             f"Выбранный вариант по validation: **{selected_name}**. Сила тренда декомпозиции: **{damping:g}**.", "",
             "Формула: `max(0, уровень + тренд × число месяцев от опорного момента) × сезонный коэффициент`.", "",
             "| Вариант | Ед. | Строк test | WAPE | MAE | Bias |", "|---|---|---:|---:|---:|---:|"]
    for row in metrics[metrics.split.eq("test")].itertuples():
        lines.append(f"| {row.model} | {row.unit} | {row.rows} | {row.wape:.2%} | {row.mae:.2f} | {row.bias:.2%} |")
    lines += ["", f"Прогноз доступен для {summary['forecast_available']} из {len(components)} товаров.", "",
              f"Подозрительных позиций «товар–документ»: {len(candidates)}. Автоматически исключено: 0.", "",
              "## Ограничения", ""] + ["- " + warning for warning in summary["warnings"]]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supplier", choices=["iek", "systeme", "all"], default="all")
    args = parser.parse_args()
    for name in (("iek", "systeme") if args.supplier == "all" else (args.supplier,)):
        result = run(name)
        print(json.dumps({key: result[key] for key in ("supplier_id", "selected_model", "decomposition_damping", "forecast_available")}, ensure_ascii=False, indent=2))
