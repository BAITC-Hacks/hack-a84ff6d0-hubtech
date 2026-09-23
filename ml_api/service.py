"""Проверка артефактов и расчёт снимка прогнозов при старте сервиса.

В запросах выбираются готовые результаты из памяти. Формулы остаются общими
с обучением; Excel, обучение и файловые операции в HTTP-обработчиках отсутствуют.
"""
from dataclasses import dataclass
from datetime import date
import hashlib
from io import BytesIO
import json
import logging
from pathlib import Path
import pickle

import numpy as np
import pandas as pd
import sklearn

from ml_api.runtime import FEATURES, CANDIDATES, attach_history, predict_components, predict_model, read_csv
from ml_api.schemas import (
    DecompositionExplanation, ForecastItem, ForecastRequest, ForecastResponse,
    HealthResponse, ModelInfo, ModelsResponse,
)

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
SUPPLIERS = {"IEK": "iek", "SYSTEME": "systeme"}


class ModelUnavailable(ValueError):
    """Нужный проверенный снимок не удалось загрузить."""


class UnsupportedMonth(ValueError):
    """Запрошен месяц, для которого нет подготовленных признаков."""


@dataclass(frozen=True)
class Snapshot:
    info: ModelInfo
    items: dict[str, ForecastItem]
    warnings: list[str]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_features(features, panel, prep, month):
    """Проверяем ключи, границу истории и согласованность CSV между собой."""
    require(prep["feature_columns"] == FEATURES, "Схема признаков изменилась")
    require(set(FEATURES).issubset(features.columns), "Отсутствуют признаки")
    for frame, column in ((features, "target_month"), (panel, "month")):
        require(not frame.empty, "Пустая таблица")
        require(frame.sku.notna().all() and frame.sku.str.strip().ne("").all(), "Пустой SKU")
        require(frame.sku.eq(frame.sku.str.strip()).all(), "Пробелы в ключах SKU")
        require(not frame.duplicated(["sku", column]).any(), "Повтор ключа товар–месяц")
        require(frame[column].notna().all() and frame[column].dt.day.eq(1).all(), "Неверная дата месяца")
    require(features.target_month.eq(month).all(), "Другой месяц в признаках")
    require(panel.month.le(month).all(), "В панели присутствуют будущие месяцы")
    require(set(features.sku) == set(panel.sku), "Каталоги панели и признаков различаются")
    reference = panel[panel.month.eq(month)].set_index("sku").reindex(features.sku)
    for name in FEATURES:
        if name == "sku":
            continue
        left, right = features[name].to_numpy(), reference[name].to_numpy()
        same = left == right if name == "unit" else np.isclose(left, right, equal_nan=True)
        require(np.asarray(same).all(), f"Признак {name} не совпадает с панелью")
    past = panel[panel.month.lt(month)]
    known = past.target_qty.dropna()
    require(np.isfinite(known).all() and known.ge(0).all(), "Недопустимая цель в истории")
    counts = past.groupby("sku").target_qty.count().reindex(features.sku, fill_value=0).to_numpy()
    require(np.array_equal(counts, features.history_observed_months.to_numpy()), "Неверная длина известной истории")


def verify_reference(reference, features, quantities, supplier_id, model, month, eligible):
    """Сверка с результатом того же эксперимента предотвращает смешение запусков."""
    require(not reference.sku.duplicated().any(), "Повтор SKU в контрольном прогнозе")
    require(set(reference.sku) == set(features.sku), "Другой каталог контрольного прогноза")
    require(reference.supplier_id.eq(supplier_id).all(), "Другой поставщик контрольного прогноза")
    require(reference.model.eq(model).all(), "Другой алгоритм контрольного прогноза")
    require(reference.target_month.eq(month).all(), "Другой месяц контрольного прогноза")
    reference = reference.set_index("sku").loc[features.sku].reset_index()
    require(reference.unit.eq(features.unit).all(), "Другие единицы контрольного прогноза")
    # Старый эксперимент использовал порог 3. API применяет более строгий порог
    # из preparation_report, поэтому сверяем только разрешённые сейчас строки.
    require(np.isclose(reference.predicted_qty[eligible], quantities[eligible],
                       rtol=1e-9, atol=1e-9, equal_nan=True).all(),
            "Модель не воспроизводит сохранённый прогноз")


def load_snapshot(root: Path, supplier_id: str) -> Snapshot:
    folder = SUPPLIERS[supplier_id]
    prepared = root / "data/ml" / folder
    forecasts = root / "data/forecasts" / folder / "decomposition"
    paths = {
        "model": root / "models" / folder / "decomposition_experiment/selected_model.pkl",
        "prep": prepared / "preparation_report.json",
        "report": forecasts / "report.json",
        "prediction_features.csv": prepared / "prediction_features.csv",
        "monthly_panel.csv": prepared / "monthly_panel.csv",
        "reference": forecasts / "selected_forecast.csv",
    }
    content = {name: path.read_bytes() for name, path in paths.items()}
    prep, report = (json.loads(content[name]) for name in ("prep", "report"))
    require(report["supplier_id"] == supplier_id, "Другой поставщик в отчёте эксперимента")
    # Старый preparation_report IEK не содержал supplier_id. Его фиксированный
    # каталог дополнительно проверяется по отчёту эксперимента и самой модели.
    require(prep.get("supplier_id", supplier_id if supplier_id == "IEK" else None) == supplier_id,
            "Другой поставщик подготовленных данных")
    require(prep["as_of"] == report["as_of"], "Даты снимков расходятся")
    month = pd.Timestamp(prep["prediction_month"])
    require(month == month.to_period("M").to_timestamp(), "Неверный месяц прогноза")
    as_of = date.fromisoformat(prep["as_of"])
    require(month.date() <= as_of, "Прогнозный месяц позже снимка подготовки")
    policies = prep["policies"]
    require(policies["blank_sales"] == "missing" and policies["negative_sales"] == "missing",
            "Изменилась согласованная семантика неизвестных продаж")
    min_history = policies["min_history"]
    require(type(min_history) is int and min_history >= 1, "Неверный порог истории")
    # Runtime не требует Excel, транзакций и обучающей выборки. Проверяем именно
    # два используемых CSV по контрольным суммам отчёта обучения.
    for name in ("prediction_features.csv", "monthly_panel.csv"):
        require(hashlib.sha256(content[name]).hexdigest() == report["prepared_file_hashes"][name],
                f"Изменился {name}; повторите подготовку и обучение")

    # Только наши локальные артефакты. Pickle нельзя принимать от HTTP-клиента.
    bundle = pickle.loads(content["model"])
    require(bundle["supplier_id"] == supplier_id, "Другой поставщик в модели")
    require(bundle["name"] == report["selected_model"], "Выбранная модель не совпадает с отчётом")
    require(bundle["name"] in (*CANDIDATES, "decomposition"), "Неизвестный алгоритм")
    require(bundle["trained_before"] == prep["prediction_month"], "Другая граница обучения")
    if bundle["name"] == "decomposition":
        require(bundle["damping"] == report["decomposition_damping"], "Другой коэффициент тренда")
        model_min_history = bundle["settings"]["min_history"]
        require(type(model_min_history) is int and model_min_history >= 3, "Неверный порог модели")
        min_history = max(min_history, model_min_history)
    else:
        require(bundle["sklearn_version"] == sklearn.__version__, "Версия scikit-learn не совпадает с обучением")

    features = read_csv(BytesIO(content["prediction_features.csv"]), ["target_month"])
    panel = read_csv(BytesIO(content["monthly_panel.csv"]), ["month"])
    validate_features(features, panel, prep, month)
    features = attach_history(features, panel)
    eligible = features.history_observed_months.ge(min_history).to_numpy()
    quantities = np.full(len(features), np.nan)
    components = None
    if eligible.any():
        if bundle["name"] == "decomposition":
            components = predict_components(bundle, features.loc[eligible], bundle["damping"])
            quantities[eligible] = components.predicted_qty.to_numpy()
            components = components.set_index("sku")
        else:
            quantities[eligible] = predict_model(bundle, features.loc[eligible])
    require(not np.isinf(quantities).any() and not (quantities < 0).any(), "Недопустимый прогноз")
    reference = read_csv(BytesIO(content["reference"]), ["target_month"])
    verify_reference(reference, features, quantities, supplier_id, bundle["name"], month, eligible)

    items = {}
    for row, predicted in zip(features.itertuples(), quantities):
        available = bool(np.isfinite(predicted))
        explanation = None
        if available and components is not None:
            values = components.loc[row.sku]
            explanation = DecompositionExplanation(
                **{key: float(values[key]) for key in (
                    "level_qty", "seasonal_factor", "trend_per_month", "months_from_level_anchor",
                    "trend_contribution_qty", "damping")},
                warnings=[warning for warning in values["warnings"].split(";") if warning],
            )
        items[row.sku] = ForecastItem(
            sku=row.sku, unit=row.unit or None,
            status="forecast" if available else "insufficient_history",
            predicted_qty=float(predicted) if available else None,
            history_observed_months=int(row.history_observed_months), explanation=explanation,
        )
    digest = hashlib.sha256()
    for name, value in sorted(content.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(value).digest())
    info = ModelInfo(
        supplier_id=supplier_id, available=True, model=bundle["name"],
        model_version=digest.hexdigest(), data_as_of=as_of,
        forecast_month=month.date(), trained_before=month.date(), sku_count=len(items),
        forecast_available=sum(item.status == "forecast" for item in items.values()),
        min_history=min_history,
    )
    warnings = list(dict.fromkeys([
        "Прогноз полного месяца; это не количество заказа и не остаток спроса до конца месяца.",
        *prep.get("warnings", []), *report.get("warnings", []),
    ]))
    return Snapshot(info=info, items=items, warnings=warnings)


class ForecastService:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else ROOT
        self._snapshots: dict[str, Snapshot] = {}

    def load(self):
        snapshots = {}
        for supplier_id in SUPPLIERS:
            try:
                snapshots[supplier_id] = load_snapshot(self.root, supplier_id)
            except Exception:
                # Недоступность одного поставщика не блокирует второго.
                # Подробности остаются в журнале сервера, а не в HTTP-ответе.
                LOGGER.exception("Не удалось загрузить модель %s", supplier_id)
        self._snapshots = snapshots

    def health(self) -> HealthResponse:
        return HealthResponse(
            status="ok" if len(self._snapshots) == len(SUPPLIERS) else "degraded",
            models={supplier: "ready" if supplier in self._snapshots else "unavailable"
                    for supplier in SUPPLIERS},
        )

    def models(self) -> ModelsResponse:
        return ModelsResponse(models=[
            self._snapshots[supplier].info if supplier in self._snapshots
            else ModelInfo(supplier_id=supplier, available=False)
            for supplier in SUPPLIERS
        ])

    def forecast(self, request: ForecastRequest) -> ForecastResponse:
        snapshot = self._snapshots.get(request.supplier_id)
        if snapshot is None:
            raise ModelUnavailable(f"Модель {request.supplier_id} недоступна. Проверьте /health и журнал сервиса.")
        info = snapshot.info
        if request.forecast_month != info.forecast_month:
            raise UnsupportedMonth(f"Поддерживается только месяц {info.forecast_month.isoformat()}.")
        items = [snapshot.items.get(sku) or ForecastItem(
            sku=sku, unit=None, status="unknown_sku", predicted_qty=None, history_observed_months=None,
        ) for sku in request.skus]
        return ForecastResponse(
            supplier_id=request.supplier_id, model=info.model, model_version=info.model_version,
            data_as_of=info.data_as_of, forecast_month=info.forecast_month,
            trained_before=info.trained_before, warnings=snapshot.warnings, items=items,
        )
