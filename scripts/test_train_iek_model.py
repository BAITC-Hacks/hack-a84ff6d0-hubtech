"""Проверки времени, сопоставимости метрик и рекурсивного прогноза IEK."""
import pickle
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from prepare_ml_data import FEATURES, build_features
from train_iek_model import (
    CANDIDATES, attach_history, choose_model, fit_model, forecast_months,
    metrics, metric_table, model_features, predict_model, rolling_backtest, validate_inputs,
)


class TrainingTests(unittest.TestCase):
    def fixture(self):
        months = pd.date_range("2024-01-01", "2026-09-01", freq="MS")
        products = pd.DataFrame([dict(sku="001_", unit="шт"), dict(sku="002_", unit="м"), dict(sku="cold_", unit="шт")])
        sales = pd.DataFrame([dict(sku=sku, month=m, sales_qty_raw=(30.0 if sku != "cold_" else np.nan),
                                   sales_status=("observed" if sku != "cold_" else "blank"))
                              for sku in products.sku for m in months])
        stock = sales[["sku", "month"]].assign(stock_qty_raw=100.0, stock_status="observed")
        panel, train, future = build_features(sales, stock, products, "2026-09-23")
        report = dict(policies=dict(blank_sales="missing", negative_sales="missing", min_history=3),
                      feature_columns=FEATURES, prediction_month="2026-09-01")
        return panel, train, future, products, report

    def test_history_mean_never_uses_current_or_future(self):
        panel, train, _, _, _ = self.fixture()
        before = attach_history(train, panel)
        panel.loc[panel.month >= "2026-01-01", "target_qty"] = 9999
        after = attach_history(train, panel)
        bound = before.target_month.le("2026-01-01")
        np.testing.assert_allclose(before.loc[bound, "history_mean"], after.loc[bound, "history_mean"])

    def test_test_labels_cannot_choose_winner(self):
        records = []
        for split in ("validation", "test"):
            for i, model in enumerate(CANDIDATES):
                records.append(dict(split=split, model=model, unit="шт", actual_qty=10.0,
                                    predicted_qty=10.0+i if split == "validation" else 999.0-i))
        frame = pd.DataFrame(records)
        first = choose_model(frame)
        frame.loc[frame.split.eq("test"), "actual_qty"] = 999999
        self.assertEqual(first, choose_model(frame))
        self.assertEqual(first[0], "mean_3")

    def test_metrics_keep_units_separate_and_define_bias(self):
        frame = pd.DataFrame([dict(split="test", model="mean_3", unit="м", actual_qty=1000., predicted_qty=1100.),
                              dict(split="test", model="mean_3", unit="шт", actual_qty=10., predicted_qty=5.)])
        result = metric_table(frame).set_index("unit")
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result.loc["м", "wape"], .1)
        self.assertAlmostEqual(result.loc["шт", "bias"], -.5)
        self.assertIsNone(metrics([0], [5])["wape"])
        self.assertEqual(metrics([0], [5])["mae"], 5)

    def test_each_rolling_fold_trains_strictly_before_month(self):
        panel, train, _, _, _ = self.fixture()
        data = attach_history(train, panel)
        observed_ends = []
        def fake_fit(name, history, max_iter):
            observed_ends.append(history.target_month.max())
            return {"name": "mean_3"}
        with patch("train_iek_model.fit_model", side_effect=fake_fit):
            results = rolling_backtest(data, "test", max_iter=2)
        self.assertTrue((results.trained_through < results.target_month).all())
        self.assertEqual(observed_ends[:4], [pd.Timestamp("2026-06-01")]*4)
        self.assertEqual(observed_ends[4:], [pd.Timestamp("2026-07-01")]*4)

    def test_model_serialization_and_nonnegative_predictions(self):
        panel, train, _, _, _ = self.fixture()
        data = attach_history(train, panel)
        data.loc[data.index[::5], "lag_1"] = np.nan
        for name in CANDIDATES:
            model = fit_model(name, data, max_iter=3)
            expected = predict_model(model, data)
            restored = pickle.loads(pickle.dumps(model))
            np.testing.assert_allclose(expected, predict_model(restored, data))
            self.assertTrue(np.isfinite(expected).all())
            self.assertTrue((expected >= 0).all())
        self.assertNotIn("target_qty", model_features(data).columns)
        self.assertNotIn("sku", model_features(data).columns)

    def test_future_keeps_cold_sku_unknown_and_prorates_exact_days(self):
        panel, train, future, products, _ = self.fixture()
        data, future = attach_history(train, panel), attach_history(future, panel)
        monthly, horizon = forecast_months(fit_model("mean_3", data), panel, products, future, "2026-09-23", 35, 3)
        warm = monthly[monthly.sku.eq("001_")]
        self.assertEqual(warm.horizon_overlap_days.tolist(), [8, 27])
        self.assertEqual(warm.recursive.tolist(), [False, True])
        self.assertTrue(warm.predicted_qty.eq(30).all())
        self.assertAlmostEqual(horizon.set_index("sku").loc["001_", "forecast_horizon_qty"], 30*8/30+30*27/31)
        self.assertTrue(monthly.loc[monthly.sku.eq("cold_"), "predicted_qty"].isna().all())
        self.assertTrue(pd.isna(horizon.set_index("sku").loc["cold_", "forecast_horizon_qty"]))
        self.assertTrue(warm.actual_history_months.eq(32).all())

    def test_partial_current_sales_do_not_affect_future(self):
        panel, train, future, products, _ = self.fixture()
        future = attach_history(future, panel)
        before = forecast_months({"name": "mean_3"}, panel, products, future, "2026-09-23", 35, 3)
        panel.loc[panel.month.eq("2026-09-01"), ["target_qty", "sales_qty_raw"]] = 999999
        after = forecast_months({"name": "mean_3"}, panel, products, future, "2026-09-23", 35, 3)
        pd.testing.assert_frame_equal(before[1], after[1])

    def test_stale_forecast_month_rejected(self):
        panel, _, future, products, _ = self.fixture()
        with self.assertRaisesRegex(ValueError, "устарела"):
            forecast_months({"name": "mean_3"}, panel, products, attach_history(future, panel), "2026-10-23", 35, 3)

    def test_invalid_splits_targets_duplicates_and_policies_rejected(self):
        panel, train, future, _, report = self.fixture()
        validate_inputs(train, future, panel, report)
        bad = train.copy()
        bad.loc[bad.index[-1], "target_qty"] = -1
        with self.assertRaises(ValueError): validate_inputs(bad, future, panel, report)
        with self.assertRaises(ValueError): validate_inputs(pd.concat([train, train.head(1)]), future, panel, report)
        bad = train.copy()
        bad.loc[bad.index[0], "split"] = "test"
        with self.assertRaises(ValueError): validate_inputs(bad, future, panel, report)
        bad = train.copy()
        bad.loc[bad.index[0], "lag_1"] = 999999
        with self.assertRaisesRegex(ValueError, "Признак"):
            validate_inputs(bad, future, panel, report)
        report["policies"]["blank_sales"] = "zero"
        with self.assertRaises(ValueError): validate_inputs(train, future, panel, report)


if __name__ == "__main__":
    unittest.main()
