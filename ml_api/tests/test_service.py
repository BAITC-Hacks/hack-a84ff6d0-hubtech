"""Проверка загрузки и прогнозирования на небольших синтетических артефактах."""

import hashlib
import json
from pathlib import Path
import pickle
import tempfile
import unittest

import numpy as np
import pandas as pd
import sklearn

from ml_api.runtime import FEATURES, attach_history, predict_components, predict_model
from ml_api.schemas import ForecastRequest
from ml_api.service import ForecastService, ModelUnavailable, UnsupportedMonth, load_snapshot
from explainable_demand import fit_decomposition
from prepare_ml_data import build_features


MONTH = "2026-09-01"
AS_OF = "2026-09-23"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path, frame):
    frame.to_csv(path, index=False, na_rep="")


def write_fixture(root, supplier_id="IEK", model_name="mean_3", min_history=3):
    """Три товара: обычный, с настоящими нулями и с одним известным месяцем."""
    folder = {"IEK": "iek", "SYSTEME": "systeme"}[supplier_id]
    prepared = root / "data/ml" / folder
    forecasts = root / "data/forecasts" / folder / "decomposition"
    models = root / "models" / folder / "decomposition_experiment"
    for directory in (prepared, forecasts, models):
        directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "prep": prepared / "preparation_report.json",
        "report": forecasts / "report.json",
        "model": models / "selected_model.pkl",
        "prediction_features.csv": prepared / "prediction_features.csv",
        "monthly_panel.csv": prepared / "monthly_panel.csv",
        "reference": forecasts / "selected_forecast.csv",
    }

    products = pd.DataFrame({"sku": ["0001", "zero", "cold"], "unit": ["шт"] * 3})
    months = pd.date_range("2026-04-01", periods=5, freq="MS")
    values = {"0001": [10., 12., 14., 16., 18.], "zero": [0.] * 5,
              "cold": [np.nan, np.nan, np.nan, np.nan, 7.]}
    sales = pd.DataFrame([
        {"sku": sku, "month": month, "sales_qty_raw": qty,
         "sales_status": "blank" if pd.isna(qty) else "observed"}
        for sku, quantities in values.items()
        for month, qty in zip(months, quantities)
    ])
    stock = sales[["sku", "month"]].assign(stock_qty_raw=100., stock_status="observed")
    panel, _, features = build_features(sales, stock, products, AS_OF, min_history=min_history)
    features = features.reset_index(drop=True)
    write_csv(paths["monthly_panel.csv"], panel)
    write_csv(paths["prediction_features.csv"], features)

    if model_name == "decomposition":
        bundle = fit_decomposition(panel, MONTH)
        bundle.update(supplier_id=supplier_id, damping=0.0)
    else:
        bundle = {"name": model_name, "supplier_id": supplier_id,
                  "trained_before": MONTH, "sklearn_version": sklearn.__version__}
    paths["model"].write_bytes(pickle.dumps(bundle))

    history_features = attach_history(features, panel)
    # Эксперимент до API допускал прогноз после трёх известных месяцев.
    eligible = history_features.history_observed_months.ge(3)
    reference = features[["sku", "unit", "target_month"]].copy()
    reference["predicted_qty"] = np.nan
    if model_name == "decomposition":
        forecast = predict_components(bundle, history_features.loc[eligible], bundle["damping"])
        reference.loc[eligible, "predicted_qty"] = forecast.predicted_qty.to_numpy()
    else:
        reference.loc[eligible, "predicted_qty"] = predict_model(bundle, history_features.loc[eligible])
    reference["status"] = np.where(eligible, "forecast", "insufficient_history")
    reference["supplier_id"] = supplier_id
    reference["model"] = model_name
    write_csv(paths["reference"], reference)

    write_json(paths["prep"], {
        "supplier_id": supplier_id, "as_of": AS_OF, "prediction_month": MONTH,
        "policies": {"blank_sales": "missing", "negative_sales": "missing", "min_history": min_history},
        "feature_columns": FEATURES, "warnings": ["Синтетические данные"],
    })
    write_json(paths["report"], {
        "supplier_id": supplier_id, "as_of": AS_OF, "selected_model": model_name,
        "decomposition_damping": 0.0, "warnings": [],
        "prepared_file_hashes": {
            name: hashlib.sha256(paths[name].read_bytes()).hexdigest()
            for name in ("prediction_features.csv", "monthly_panel.csv")
        },
    })
    return paths


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = write_fixture(self.root)

    def refresh_hash(self, name):
        report = read_json(self.paths["report"])
        report["prepared_file_hashes"][name] = hashlib.sha256(self.paths[name].read_bytes()).hexdigest()
        write_json(self.paths["report"], report)

    def test_load_preserves_codes_zero_and_unknown_history(self):
        snapshot = load_snapshot(self.root, "IEK")
        self.assertEqual(snapshot.info.model, "mean_3")
        self.assertEqual(snapshot.info.sku_count, 3)
        self.assertEqual(snapshot.info.forecast_available, 2)
        self.assertEqual(snapshot.info.min_history, 3)
        self.assertEqual(len(snapshot.info.model_version), 64)
        self.assertEqual(snapshot.items["0001"].predicted_qty, 16.)
        self.assertEqual(snapshot.items["0001"].history_observed_months, 5)
        self.assertIsNone(snapshot.items["0001"].explanation)
        self.assertEqual(snapshot.items["zero"].predicted_qty, 0.)
        self.assertEqual(snapshot.items["zero"].status, "forecast")
        self.assertIsNone(snapshot.items["cold"].predicted_qty)
        self.assertEqual(snapshot.items["cold"].status, "insufficient_history")

    def test_stricter_preparation_history_suppresses_old_experiment_forecast(self):
        prep = read_json(self.paths["prep"])
        prep["policies"]["min_history"] = 12
        write_json(self.paths["prep"], prep)
        snapshot = load_snapshot(self.root, "IEK")
        self.assertEqual(snapshot.info.min_history, 12)
        self.assertEqual(snapshot.info.forecast_available, 0)
        self.assertEqual(snapshot.items["0001"].history_observed_months, 5)
        self.assertTrue(all(item.predicted_qty is None for item in snapshot.items.values()))
        self.assertTrue(all(item.status == "insufficient_history" for item in snapshot.items.values()))

    def test_decomposition_formula_and_intrinsic_minimum_history(self):
        write_fixture(self.root, model_name="decomposition", min_history=1)
        snapshot = load_snapshot(self.root, "IEK")
        self.assertEqual(snapshot.info.min_history, 3)
        self.assertEqual(snapshot.items["cold"].status, "insufficient_history")
        for sku in ("0001", "zero"):
            with self.subTest(sku=sku):
                item = snapshot.items[sku]
                explanation = item.explanation
                self.assertIsNotNone(explanation)
                expected = max(0, explanation.level_qty + explanation.trend_per_month
                               * explanation.months_from_level_anchor) * explanation.seasonal_factor
                self.assertAlmostEqual(item.predicted_qty, expected)
                self.assertAlmostEqual(explanation.trend_contribution_qty,
                                       explanation.trend_per_month * explanation.months_from_level_anchor)

    def test_model_identity_and_training_boundary_mismatches_are_rejected(self):
        original = self.paths["model"].read_bytes()
        cases = [
            ("supplier_id", "SYSTEME", "поставщик в модели"),
            ("name", "seasonal_12", "не совпадает с отчётом"),
            ("trained_before", "2026-08-01", "граница обучения"),
            ("sklearn_version", "0.invalid", "Версия scikit-learn"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                bundle = pickle.loads(original)
                bundle[field] = value
                self.paths["model"].write_bytes(pickle.dumps(bundle))
                with self.assertRaisesRegex(ValueError, message):
                    load_snapshot(self.root, "IEK")
        self.paths["model"].write_bytes(original)

    def test_supplier_metadata_and_snapshot_date_mismatches_are_rejected(self):
        cases = [
            ("report", "supplier_id", "SYSTEME", "поставщик в отчёте"),
            ("prep", "supplier_id", "SYSTEME", "поставщик подготовленных"),
            ("report", "as_of", "2026-09-22", "Даты снимков"),
        ]
        for name, field, value, message in cases:
            with self.subTest(artifact=name, field=field):
                original = self.paths[name].read_bytes()
                document = read_json(self.paths[name])
                document[field] = value
                write_json(self.paths[name], document)
                with self.assertRaisesRegex(ValueError, message):
                    load_snapshot(self.root, "IEK")
                self.paths[name].write_bytes(original)

    def test_decomposition_damping_must_match_report(self):
        paths = write_fixture(self.root, model_name="decomposition")
        bundle = pickle.loads(paths["model"].read_bytes())
        bundle["damping"] = 0.5
        paths["model"].write_bytes(pickle.dumps(bundle))
        with self.assertRaisesRegex(ValueError, "коэффициент тренда"):
            load_snapshot(self.root, "IEK")

    def test_modified_prepared_csv_is_rejected_by_hash(self):
        for name in ("prediction_features.csv", "monthly_panel.csv"):
            with self.subTest(artifact=name):
                original = self.paths[name].read_bytes()
                self.paths[name].write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "Изменился"):
                    load_snapshot(self.root, "IEK")
                self.paths[name].write_bytes(original)

    def test_feature_panel_disagreement_is_rejected_even_with_updated_hash(self):
        features = pd.read_csv(self.paths["prediction_features.csv"], dtype={"sku": str})
        features.loc[features.sku.eq("0001"), "mean_3"] = 999.
        write_csv(self.paths["prediction_features.csv"], features)
        self.refresh_hash("prediction_features.csv")
        with self.assertRaisesRegex(ValueError, "Признак mean_3"):
            load_snapshot(self.root, "IEK")

    def test_history_count_is_checked_against_actual_past(self):
        for name, date_column in (("prediction_features.csv", "target_month"), ("monthly_panel.csv", "month")):
            frame = pd.read_csv(self.paths[name], dtype={"sku": str})
            frame.loc[frame.sku.eq("0001") & frame[date_column].eq(MONTH), "history_observed_months"] = 6
            write_csv(self.paths[name], frame)
            self.refresh_hash(name)
        with self.assertRaisesRegex(ValueError, "длина известной истории"):
            load_snapshot(self.root, "IEK")

    def test_reference_quantity_mismatch_is_rejected(self):
        reference = pd.read_csv(self.paths["reference"], dtype={"sku": str})
        reference.loc[reference.sku.eq("0001"), "predicted_qty"] = 999.
        write_csv(self.paths["reference"], reference)
        with self.assertRaisesRegex(ValueError, "не воспроизводит сохранённый прогноз"):
            load_snapshot(self.root, "IEK")

    def test_reference_metadata_mismatches_are_rejected(self):
        original = pd.read_csv(self.paths["reference"], dtype={"sku": str})
        cases = [
            ("supplier_id", "SYSTEME", "поставщик контрольного"),
            ("model", "seasonal_12", "алгоритм контрольного"),
            ("target_month", "2026-10-01", "месяц контрольного"),
            ("unit", "м", "единицы контрольного"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                reference = original.copy()
                reference[field] = value
                write_csv(self.paths["reference"], reference)
                with self.assertRaisesRegex(ValueError, message):
                    load_snapshot(self.root, "IEK")

    def test_reference_row_order_does_not_change_results(self):
        before = load_snapshot(self.root, "IEK")
        reference = pd.read_csv(self.paths["reference"], dtype={"sku": str})
        write_csv(self.paths["reference"], reference.iloc[::-1])
        after = load_snapshot(self.root, "IEK")
        self.assertEqual(before.items, after.items)

    def test_legacy_iek_prep_without_supplier_is_accepted_but_systeme_is_not(self):
        prep = read_json(self.paths["prep"])
        del prep["supplier_id"]
        write_json(self.paths["prep"], prep)
        self.assertEqual(load_snapshot(self.root, "IEK").info.supplier_id, "IEK")
        systeme = write_fixture(self.root, supplier_id="SYSTEME")
        prep = read_json(systeme["prep"])
        del prep["supplier_id"]
        write_json(systeme["prep"], prep)
        with self.assertRaisesRegex(ValueError, "поставщик подготовленных"):
            load_snapshot(self.root, "SYSTEME")


class ForecastServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        write_fixture(self.root)
        self.service = ForecastService(self.root)

    def request(self, supplier_id="IEK", month=MONTH, skus=None):
        return ForecastRequest(supplier_id=supplier_id, forecast_month=month,
                               skus=skus or ["zero", "missing", "0001", "cold"])

    def load_one_supplier(self):
        with self.assertLogs("ml_api.service", level="ERROR"):
            self.service.load()

    def test_partial_availability_keeps_valid_supplier_usable(self):
        self.load_one_supplier()
        self.assertEqual(self.service.health().status, "degraded")
        self.assertEqual(self.service.health().models, {"IEK": "ready", "SYSTEME": "unavailable"})
        available = {item.supplier_id: item.available for item in self.service.models().models}
        self.assertEqual(available, {"IEK": True, "SYSTEME": False})
        response = self.service.forecast(self.request())
        self.assertEqual([item.sku for item in response.items], ["zero", "missing", "0001", "cold"])
        self.assertEqual(response.items[0].predicted_qty, 0.)
        self.assertEqual(response.items[1].status, "unknown_sku")
        self.assertIsNone(response.items[1].predicted_qty)
        self.assertIsNone(response.items[1].history_observed_months)
        self.assertEqual(response.items[2].predicted_qty, 16.)
        self.assertEqual(response.items[3].status, "insufficient_history")
        self.assertEqual(response.forecast_scope, "full_month")
        with self.assertRaises(ModelUnavailable):
            self.service.forecast(self.request(supplier_id="SYSTEME"))

    def test_request_for_other_month_is_rejected(self):
        self.load_one_supplier()
        with self.assertRaises(UnsupportedMonth):
            self.service.forecast(self.request(month="2026-10-01"))

    def test_both_suppliers_ready_and_repeated_requests_are_stable(self):
        write_fixture(self.root, supplier_id="SYSTEME", model_name="decomposition")
        self.service.load()
        self.assertEqual(self.service.health().status, "ok")
        for supplier_id in ("IEK", "SYSTEME"):
            with self.subTest(supplier_id=supplier_id):
                request = self.request(supplier_id=supplier_id)
                self.assertEqual(self.service.forecast(request), self.service.forecast(request))


if __name__ == "__main__":
    unittest.main()
