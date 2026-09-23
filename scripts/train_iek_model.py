#!/usr/bin/env python3
"""Обучение и временная проверка месячного прогноза только IEK."""
import os
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

import argparse
import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

from prepare_ml_data import FEATURES, build_features
from prepare_order_inputs import load_config

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ("mean_3", "seasonal_12", "boosting_squared", "boosting_poisson")
UNITS = ("шт", "м", "упак", "компл")
QUANTITIES = ("lag_1", "lag_2", "lag_3", "lag_6", "lag_12", "mean_3", "mean_6", "std_6", "opening_stock_lag_1")
OTHER_FEATURES = ("days_in_month", "observations_3", "observations_6", "missing_share_6", "zero_share_6", "history_observed_months", "history_calendar_months")


def history_means(panel):
    """Среднее всех известных прошлых месяцев, включая первые месяцы товара."""
    panel = panel.sort_values(["sku", "month"]).copy()
    panel["history_mean"] = panel.groupby("sku").target_qty.transform(lambda s: s.shift().expanding().mean())
    return panel[["sku", "month", "history_mean"]].rename(columns={"month": "target_month"})


def attach_history(frame, panel):
    return frame.merge(history_means(panel), on=["sku", "target_month"], how="left", validate="one_to_one")


def scale_for(frame):
    return frame.mean_6.fillna(frame.history_mean).clip(lower=1).fillna(1).to_numpy(float)


def model_features(frame):
    """Только прошлое; SKU служит ключом, а не порядковым числовым признаком."""
    scale = scale_for(frame)
    result = pd.DataFrame(index=frame.index)
    for name in QUANTITIES:
        result[name + "_relative"] = frame[name] / scale
    result["log_scale"] = np.log1p(scale)
    for name in OTHER_FEATURES:
        result[name] = frame[name]
    result["month_sin"] = np.sin(2 * np.pi * frame.month_number / 12)
    result["month_cos"] = np.cos(2 * np.pi * frame.month_number / 12)
    result["trend_ratio"] = frame.mean_3 / frame.mean_6.replace(0, np.nan)
    for unit in UNITS:
        result["unit_" + unit] = frame.unit.eq(unit).astype(float)
    result["unit_unknown"] = (~frame.unit.isin(UNITS)).astype(float)
    return result.replace([np.inf, -np.inf], np.nan).astype(float)


def fit_model(name, frame, max_iter=160):
    bundle = {"name": name, "sklearn_version": sklearn.__version__, "max_iter": max_iter}
    if name not in CANDIDATES:
        raise ValueError(f"Неизвестная модель: {name}")
    if name.startswith("boosting"):
        model = HistGradientBoostingRegressor(
            loss="poisson" if name.endswith("poisson") else "squared_error",
            max_iter=max_iter, max_leaf_nodes=15, min_samples_leaf=30,
            learning_rate=0.06, l2_regularization=10, early_stopping=False,
            random_state=42,
        )
        features = model_features(frame)
        target = frame.target_qty.to_numpy(float) / scale_for(frame)
        if name.endswith("poisson") and not target.sum() > 0:
            raise ValueError("Poisson требует хотя бы одного положительного значения")
        with threadpool_limits(limits=2):
            model.fit(features, target)
        bundle.update(estimator=model, features=list(features.columns))
    return bundle


def predict_model(bundle, frame):
    if frame.empty:
        return np.array([], dtype=float)
    name = bundle["name"]
    fallback = frame.mean_3.fillna(frame.mean_6).fillna(frame.history_mean)
    if name == "mean_3":
        result = fallback.to_numpy(float)
    elif name == "seasonal_12":
        result = frame.lag_12.fillna(fallback).to_numpy(float)
    else:
        features = model_features(frame)
        if list(features.columns) != bundle["features"]:
            raise ValueError("Признаки не соответствуют сохранённой модели")
        with threadpool_limits(limits=2):
            result = bundle["estimator"].predict(features) * scale_for(frame)
    return np.maximum(result, 0)


def metrics(actual, predicted):
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    valid = np.isfinite(actual) & np.isfinite(predicted)
    actual, predicted = actual[valid], predicted[valid]
    if not len(actual):
        return dict(rows=0, mae=None, wape=None, bias=None)
    error = predicted - actual
    denominator = float(np.abs(actual).sum())
    return dict(rows=len(actual), mae=float(np.abs(error).mean()),
                wape=float(np.abs(error).sum() / denominator) if denominator else None,
                bias=float(error.sum() / denominator) if denominator else None)


def metric_table(predictions):
    records = []
    for (split, model, unit), group in predictions.groupby(["split", "model", "unit"], dropna=False):
        records.append(dict(split=split, model=model, unit=unit or "unknown", **metrics(group.actual_qty, group.predicted_qty)))
    return pd.DataFrame(records)


def choose_model(predictions):
    """Тест не участвует в выборе. Равный вес единиц, без суммы метров со штуками."""
    table = metric_table(predictions[predictions.split.eq("validation")])
    eligible = table[table.unit.isin(UNITS) & table.wape.notna()]
    if eligible.empty:
        raise ValueError("Нет validation-метрик для известных единиц измерения")
    scores = eligible.groupby("model").wape.mean().to_dict()
    if set(scores) != set(CANDIDATES):
        raise ValueError("Не все кандидаты оценены на validation")
    winner = min(CANDIDATES, key=lambda name: (scores[name], CANDIDATES.index(name)))
    return winner, scores


def rolling_backtest(data, split, max_iter=160):
    results = []
    for month in sorted(data.loc[data.split.eq(split), "target_month"].unique()):
        history = data[data.target_month < month]
        evaluation = data[data.target_month.eq(month)]
        if history.empty:
            raise ValueError("Нет истории до проверяемого месяца")
        print(f"{split}: {pd.Timestamp(month).date()}, обучение {len(history)}, проверка {len(evaluation)}", flush=True)
        for name in CANDIDATES:
            fitted = fit_model(name, history, max_iter)
            frame = evaluation[["sku", "unit", "target_month", "history_observed_months", "missing_share_6"]].copy()
            frame["model"], frame["split"] = name, split
            frame["actual_qty"] = evaluation.target_qty.to_numpy()
            frame["predicted_qty"] = predict_model(fitted, evaluation)
            if not np.isfinite(frame.predicted_qty).all():
                raise ValueError(f"{name}: неизвестный прогноз при сравнении моделей")
            frame["residual_qty"] = frame.actual_qty - frame.predicted_qty
            frame["trained_through"] = history.target_month.max()
            results.append(frame)
    if not results:
        raise ValueError(f"Нет строк для {split}")
    return pd.concat(results, ignore_index=True)


def validate_inputs(data, prediction, panel, report):
    if report["policies"]["blank_sales"] != "missing" or report["policies"]["negative_sales"] != "missing":
        raise ValueError("Для IEK подтверждено сохранение неизвестных/отрицательных значений")
    if report["feature_columns"] != FEATURES:
        raise ValueError("Схема признаков изменилась; нужна повторная проверка обучения")
    for frame, date_column in [(data, "target_month"), (prediction, "target_month"), (panel, "month")]:
        if frame.duplicated(["sku", date_column]).any() or frame.sku.fillna("").eq("").any() or frame[date_column].isna().any():
            raise ValueError("Повтор или пустой ключ товар–месяц")
        if not frame[date_column].dt.day.eq(1).all():
            raise ValueError("Дата месячного ряда должна быть началом месяца")
    if set(data.split) != {"train", "validation", "test"}:
        raise ValueError("Нужны три временные части train/validation/test")
    previous_end = None
    for split in ("train", "validation", "test"):
        part = data[data.split.eq(split)]
        if previous_end is not None and part.target_month.min() <= previous_end:
            raise ValueError("Временные части пересекаются")
        previous_end = part.target_month.max()
    month = pd.Timestamp(report["prediction_month"])
    if not prediction.target_month.eq(month).all() or not data.target_month.lt(month).all():
        raise ValueError("Неполный месяц попал в обучение или неверен месяц прогноза")
    if not np.isfinite(data.target_qty).all() or data.target_qty.lt(0).any():
        raise ValueError("Цель должна быть известной и неотрицательной")
    if data.history_observed_months.lt(report["policies"]["min_history"]).any():
        raise ValueError("Недостаточная история в обучающей выборке")
    check = data[["sku", "target_month", "target_qty"]].merge(
        panel[["sku", "month", "target_qty"]], left_on=["sku", "target_month"], right_on=["sku", "month"],
        validate="one_to_one", how="left", suffixes=("_train", "_panel"))
    if not np.isclose(check.target_qty_train, check.target_qty_panel, equal_nan=False).all():
        raise ValueError("Цели панели и обучающей выборки расходятся")
    # CSV могут быть от разных запусков: проверяем соответствие сохранённых признаков.
    indexed_panel = panel.rename(columns={"month": "target_month"}).set_index(["sku", "target_month"])
    for frame in (data, prediction):
        indexed = frame.set_index(["sku", "target_month"])
        reference = indexed_panel.reindex(indexed.index)
        for feature in FEATURES:
            if feature == "sku":
                continue
            left, right = indexed[feature], reference[feature]
            same = left.fillna("").eq(right.fillna("")) if feature == "unit" else np.isclose(left, right, equal_nan=True)
            if not np.asarray(same).all():
                raise ValueError(f"Признак {feature} расходится с monthly_panel.csv")


def forecast_months(bundle, panel, products, first_features, as_of, horizon_days, min_history):
    """Рекурсивный прогноз. Неполные фактические продажи не подмешиваются."""
    start = first_features.target_month.iloc[0]
    as_of = pd.Timestamp(as_of)
    if horizon_days < 1:
        raise ValueError("Горизонт должен быть положительным")
    if start != as_of.to_period("M").to_timestamp():
        raise ValueError("Выгрузка устарела: прогнозный месяц не совпадает с месяцем расчёта")
    end = as_of + pd.Timedelta(days=horizon_days)
    last_month = (end - pd.Timedelta(days=1)).to_period("M").to_timestamp()
    work = panel[panel.month.lt(start)].copy()
    base_history = first_features.set_index("sku").history_observed_months
    monthly = []
    for month in pd.date_range(start, last_month, freq="MS"):
        if month == start:
            features = first_features.copy()
        else:
            sales = work[["sku", "month", "target_qty"]].rename(columns={"target_qty": "sales_qty_raw"})
            sales["sales_status"] = np.where(sales.sales_qty_raw.notna(), "observed", "blank")
            stock = work[["sku", "month", "stock_qty_raw", "stock_status"]]
            # Внутренние предсказания временно служат лагами; это не реальные наблюдения.
            rebuilt, _, features = build_features(sales, stock, products, month, min_history=min_history)
            features = attach_history(features, rebuilt)
        enough = features.sku.map(base_history).ge(min_history)
        qty = np.full(len(features), np.nan)
        qty[enough] = predict_model(bundle, features.loc[enough])
        rows = features[["sku", "unit", "target_month"]].copy()
        rows["predicted_qty"] = qty
        rows["status"] = np.where(enough, "forecast", "insufficient_history")
        rows["recursive"] = month > start
        rows["model"] = bundle["name"]
        rows["actual_history_months"] = features.sku.map(base_history)
        monthly.append(rows)
        addition = rows[["sku", "target_month", "predicted_qty"]].rename(columns={"target_month": "month", "predicted_qty": "target_qty"})
        addition["stock_qty_raw"], addition["stock_status"] = np.nan, "not_in_source"
        work = pd.concat([work, addition], ignore_index=True)
    monthly = pd.concat(monthly, ignore_index=True)
    month_end = monthly.target_month + pd.offsets.MonthBegin(1)
    overlap_start = monthly.target_month.clip(lower=as_of)
    overlap_end = month_end.clip(upper=end)
    monthly["horizon_overlap_days"] = (overlap_end - overlap_start).dt.days.clip(lower=0)
    monthly["horizon_contribution_qty"] = monthly.predicted_qty * monthly.horizon_overlap_days / monthly.target_month.dt.days_in_month
    horizon = monthly.groupby(["sku", "unit"], dropna=False).horizon_contribution_qty.agg(
        lambda s: s.sum() if s.notna().all() else np.nan).rename("forecast_horizon_qty").reset_index()
    horizon["supplier_id"], horizon["as_of"], horizon["horizon_days"] = "IEK", as_of, horizon_days
    horizon["forecast_status"] = np.where(horizon.forecast_horizon_qty.notna(), "approximate", "insufficient_history")
    horizon["assumption"] = "uniform_within_month;recursive_future_months;not_35day_backtested"
    return monthly, horizon


def read_csv(path, dates):
    return pd.read_csv(path, dtype={"sku": str, "unit": str}, keep_default_na=False, na_values=[""], parse_dates=dates).assign(
        unit=lambda f: f.unit.fillna(""))


def run(input_dir, output_dir, model_dir, config_path, max_iter=160):
    if input_dir.resolve() != (ROOT / "data/ml/iek").resolve() and "systeme" in str(input_dir).lower():
        raise ValueError("Этот скрипт предназначен только для IEK")
    report = json.loads((input_dir / "preparation_report.json").read_text())
    config = load_config(config_path)
    if config["supplier_id"] != "IEK" or config["as_of"] != report["as_of"]:
        raise ValueError("Конфигурация должна относиться к IEK и той же дате подготовки")
    if config.get("parameters_confirmed") is not True:
        raise ValueError("Параметры расчёта IEK не подтверждены")
    if any(config[key] != report["policies"][key] for key in ("blank_sales", "negative_sales")):
        raise ValueError("Правила подготовки не совпадают с конфигурацией IEK")
    if max_iter < 1:
        raise ValueError("max_iter должен быть положительным")
    for source in report["sources"].values():
        if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("Исходный Excel изменился: заново запустите prepare_ml_data.py")
    data = read_csv(input_dir / "training_data.csv", ["target_month"])
    prediction = read_csv(input_dir / "prediction_features.csv", ["target_month"])
    panel = read_csv(input_dir / "monthly_panel.csv", ["month"])
    products = read_csv(input_dir / "products.csv", [])
    validate_inputs(data, prediction, panel, report)
    data, prediction = attach_history(data, panel), attach_history(prediction, panel)
    validation = rolling_backtest(data, "validation", max_iter)
    winner, scores = choose_model(validation)
    print(f"Выбран на validation: {winner}; оценки {scores}", flush=True)
    test = rolling_backtest(data, "test", max_iter)
    predictions = pd.concat([validation, test], ignore_index=True)
    table = metric_table(predictions)
    # Только после оценки дообучаем финальные артефакты на всей завершённой истории.
    bundles = {name: fit_model(name, data, max_iter) for name in CANDIDATES}
    bundle = bundles[winner]
    bundle.update(supplier_id="IEK", trained_through=str(data.target_month.max().date()),
                  prediction_month=report["prediction_month"], preparation_policies=report["policies"],
                  minimum_history=report["policies"]["min_history"], selection_scores=scores)
    horizon_days = int(config["lead_time_days"]) + int(config["review_period_days"])
    monthly, horizon = forecast_months(bundle, panel, products, prediction, config["as_of"], horizon_days, report["policies"]["min_history"])
    evaluation_months = sorted(data.loc[data.split.ne("train"), "target_month"].unique())
    coverage = []
    for month in evaluation_months:
        source = panel[panel.month.eq(month)]
        evaluated = data[data.target_month.eq(month)]
        coverage.append(dict(month=str(pd.Timestamp(month).date()), catalog_rows=len(source),
                             known_nonnegative_targets=int(source.target_qty.notna().sum()), evaluated_rows=len(evaluated)))
    summary = dict(supplier_id="IEK", as_of=config["as_of"], winner=winner,
                   selection_metric="mean validation WAPE across known units; equal unit weights", validation_scores=scores,
                   splits=report["splits"], coverage=coverage, training_rows=len(data),
                   forecast_products=len(horizon), products_with_forecast=int(horizon.forecast_horizon_qty.notna().sum()),
                   model_features=list(model_features(data.head(1)).columns),
                   sklearn_version=sklearn.__version__, numpy_version=np.__version__, pandas_version=pd.__version__,
                   forecast_horizon_days=horizon_days, configuration=config,
                   preparation_report_sha256=hashlib.sha256((input_dir / "preparation_report.json").read_bytes()).hexdigest(),
                   hyperparameters=dict(max_iter=max_iter, max_leaf_nodes=15,
                   min_samples_leaf=30, learning_rate=0.06, l2_regularization=10, early_stopping=False, random_state=42),
                   prepared_file_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in input_dir.glob("*.csv")},
                   warnings=[
                       "Метрики относятся к известным неотрицательным продажам с достаточной историей. Неизвестный спрос не оценён; возможна систематическая ошибка отбора.",
                       "Из-за неизвестных пропусков нельзя надёжно оценить качество на месяцах без продаж или частоту нулевого спроса.",
                       "Проверка помесячная: перед каждым месяцем обучение расширяется только прошлым; июль становится доступен для прогноза августа.",
                       "Метрики по единицам: количества метров, штук и упаковок не складываются. Bias > 0 означает завышение.",
                       "Прогноз 35 дней приближённый: равномерное распределение внутри месяца и рекурсивный следующий месяц; такой горизонт отдельно не проверен.",
                       "Фактические продажи неполного сентября не используются. Октябрьские лаги включают прогноз сентября.",
                       "Страховой запас и сервис 95% этим обучением не подтверждены; месячные ошибки нельзя считать ошибками 35-дневного прогноза.",
                       "Это прогноз зарегистрированных продаж, а не восстановленный спрос с учётом stockout. Оптовые выбросы не удалены.",
                       "Нет текущих свободных остатков и подтверждённой кратности/пересчёта единиц; рекомендованный заказ ещё не рассчитан.",
                       "Готовая внешняя сезонность, MOQ, путь и будущие остатки не используются для обучения.",
                       "Systeme Electric не обрабатывается. Backend продолжает использовать существующий алгоритм до явного подключения новой модели.",
                   ])
    output_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    outputs = {"backtest_predictions": predictions, "metrics_by_unit": table,
               "monthly_forecast": monthly, "horizon_forecast": horizon}
    # Отдельные группы показывают ограничения на разреженной и короткой истории.
    segments = predictions.assign(history_segment=np.where(predictions.history_observed_months < 12, "short_history", "12plus_months"),
                                  missing_segment=np.where(predictions.missing_share_6 >= 0.5, "many_unknown_months", "few_unknown_months"))
    segment_records = []
    for kind in ("history_segment", "missing_segment"):
        for segment, group in segments.groupby(kind):
            section = metric_table(group).assign(segment_type=kind, segment=segment)
            segment_records.append(section)
    outputs["metrics_by_segment"] = pd.concat(segment_records, ignore_index=True)
    for name, frame in outputs.items():
        frame.to_csv(output_dir / f"{name}.csv", index=False, encoding="utf-8-sig", na_rep="")
    for name, fitted in bundles.items():
        with (model_dir / f"{name}.pkl").open("wb") as f:
            pickle.dump(fitted, f, protocol=pickle.HIGHEST_PROTOCOL)
    with (model_dir / "selected_model.pkl").open("wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    summary["model_hashes"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in model_dir.glob("*.pkl")}
    (output_dir / "training_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# Результаты обучения IEK", "", f"Дата данных: {config['as_of']}. Выбрана модель `{winner}`.", "",
             "Критерий выбора: среднее WAPE по известным единицам на validation. Тест при выборе не используется.", "",
             "| Период | Единица | Строк | WAPE модели | WAPE среднего за 3 месяца | Bias модели |",
             "|---|---|---:|---:|---:|---:|"]
    for row in table[table.model.eq(winner)].itertuples():
        baseline = table[table.model.eq("mean_3") & table.split.eq(row.split) & table.unit.eq(row.unit)].iloc[0]
        lines.append(f"| {row.split} | {row.unit} | {row.rows} | {row.wape:.1%} | {baseline.wape:.1%} | {row.bias:.1%} |")
    lines += ["", f"Прогноз доступен для {summary['products_with_forecast']} из {len(horizon)} товаров.", "",
              "WAPE — сумма абсолютных ошибок / сумма фактических количеств внутри одной единицы. Это не процент точности.",
              "Положительный Bias означает завышение, отрицательный — занижение.", "", "## Ограничения", ""]
    lines += ["- " + message for message in summary["warnings"]]
    (output_dir / "training_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=ROOT / "data/ml/iek")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/forecasts/iek")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models/iek")
    parser.add_argument("--config", type=Path, default=ROOT / "config/iek_replenishment.json")
    parser.add_argument("--max-iter", type=int, default=160)
    args = parser.parse_args()
    result = run(args.input_dir, args.output_dir, args.model_dir, args.config, args.max_iter)
    print(json.dumps({key: result[key] for key in ("winner", "validation_scores", "products_with_forecast")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
