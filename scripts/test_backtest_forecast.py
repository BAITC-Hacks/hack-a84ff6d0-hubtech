"""Temporal leakage, comparable metric populations, and source-policy regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from openpyxl import Workbook

from backtest_forecast import (
    MODELS, apply_policy, evaluate_sales, evaluation_months, forecast_monthly_demand,
    metric_values, predict_at_origin, read_unit_catalog, summarize,
)


class TemporalBacktestTests(unittest.TestCase):
    def sales(self):
        months = pd.date_range("2024-01-01", "2026-09-01", freq="MS")
        return pd.DataFrame({"sku": "001_", "month": months,
                             "sales_qty_raw": np.arange(1, len(months) + 1, dtype=float),
                             "sales_status": "observed"})

    def test_future_and_target_do_not_change_predictions(self):
        sales = self.sales()
        months = evaluation_months("2026-08")
        before = evaluate_sales(sales, {"001_": "шт"}, "IEK", months, "unknown_blanks")
        boundary = pd.Timestamp("2026-07-01")
        sales.loc[sales.month >= boundary, "sales_qty_raw"] = 1000000
        seen_histories = []

        def spy(history, start, days, seasonal_factors=None):
            self.assertTrue((history.index < pd.Timestamp(start)).all())
            self.assertIsNone(seasonal_factors)
            seen_histories.append(history.copy())
            return forecast_monthly_demand(history, start, days, seasonal_factors=seasonal_factors)

        with patch("backtest_forecast.forecast_monthly_demand", side_effect=spy):
            after = evaluate_sales(sales, {"001_": "шт"}, "IEK", months, "unknown_blanks")
        columns = ["target_month", *MODELS, "history_observed_months", "history_last_month"]
        past = before.target_month.le("2026-07-01")
        pd.testing.assert_frame_equal(before.loc[past, columns], after.loc[past, columns])
        self.assertNotEqual(before.iloc[-1].current, after.iloc[-1].current)
        self.assertEqual(len(seen_histories), 8)

    def test_cutoff_at_prediction_boundary_and_exact_calendar_baselines(self):
        history = pd.Series([10.0, 20.0, 99999.0], index=pd.to_datetime(["2025-07-01", "2026-05-01", "2026-07-01"]))
        result = predict_at_origin(history, pd.Timestamp("2026-07-01"))
        self.assertTrue(np.isnan(result["last_month"]))
        self.assertEqual(result["same_month_last_year"], 10)
        without_target = predict_at_origin(history.iloc[:-1], pd.Timestamp("2026-07-01"))
        self.assertEqual(result["current"], without_target["current"])

    def test_validation_test_are_disjoint_chronological(self):
        months = evaluation_months("2026-08")
        self.assertEqual([m.strftime("%Y-%m") for m, split in months if split == "validation"],
                         [f"2026-{month:02}" for month in range(1, 7)])
        self.assertEqual([m.strftime("%Y-%m") for m, split in months if split == "test"], ["2026-07", "2026-08"])
        with self.assertRaises(ValueError):
            evaluation_months("2026-08", test_months=0)

    def test_no_history_is_unavailable_and_one_month_is_reported_short(self):
        sales = self.sales().iloc[-2:].copy()
        evaluated = evaluate_sales(sales, {"001_": "шт"}, "IEK",
                                    [(pd.Timestamp("2026-08-01"), "validation"),
                                     (pd.Timestamp("2026-09-01"), "test")], "unknown_blanks")
        self.assertTrue(evaluated.iloc[0][list(MODELS)].isna().all())
        self.assertEqual(evaluated.iloc[1].history_observed_months, 1)
        self.assertTrue(evaluated.iloc[1].short_history)
        self.assertTrue(np.isfinite(evaluated.iloc[1].current))


class MetricTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({"sku": ["a", "a", "b"], "unit": ["шт"] * 3,
                             "actual": [0.0, 10.0, 20.0], "current": [5.0, 8.0, 22.0],
                             "last_month": [np.nan, 8.0, 22.0],
                             "same_month_last_year": [0.0, 9.0, np.nan],
                             "short_history": [True, False, False], "history_observed_months": [1, 13, 15],
                             "raw_status": ["observed"] * 3, "supplier": ["IEK"] * 3,
                             "scenario": ["unknown_blanks"] * 3, "split": ["test"] * 3})

    def test_mae_wape_bias_and_zero_actual_are_not_dropped(self):
        result = metric_values(self.frame(), "current")
        self.assertEqual(result["mae_qty"], 3)
        self.assertEqual(result["wape_percent"], 30)
        self.assertAlmostEqual(result["bias_percent"], 100 * 5 / 30)
        self.assertEqual(result["zero_actual_mae_qty"], 5)
        self.assertEqual(result["macro_sku_wape_skus"], 2)
        self.assertAlmostEqual(result["macro_sku_wape_percent"], 40)

    def test_all_zero_actual_is_undefined_wape_but_mean_error_is_visible(self):
        frame = self.frame().iloc[:1]
        result = metric_values(frame, "current")
        self.assertIsNone(result["wape_percent"])
        self.assertIsNone(result["bias_percent"])
        self.assertEqual(result["mae_qty"], 5)
        self.assertEqual(result["mean_error_qty"], 5)
        self.assertEqual(result["macro_sku_wape_skus"], 0)

    def test_comparisons_use_identical_rows_and_units_never_mix(self):
        frame = self.frame()
        metric_rows, coverage = summarize(frame)
        paired = metric_rows[metric_rows.cohort.eq("paired_last_month")]
        self.assertEqual(paired.rows.tolist(), [2, 2])
        self.assertEqual(paired.actual_total_qty.tolist(), [30, 30])
        common = metric_rows[metric_rows.cohort.eq("common_all_models")]
        self.assertEqual(common.rows.tolist(), [1, 1, 1])
        self.assertEqual(coverage[0]["known_targets"], 3)
        frame.loc[0, "unit"] = "м"
        with self.assertRaisesRegex(ValueError, "одной известной"):
            metric_values(frame, "current")
        grouped, _ = summarize(frame)
        self.assertEqual(set(grouped.unit), {"шт", "м"})
        frame["unit"] = ""
        grouped, unknown = summarize(frame)
        self.assertTrue(grouped.empty)
        self.assertEqual(unknown[0]["unknown_unit_excluded"], 3)


class PolicyAndSourceTests(unittest.TestCase):
    def test_blank_negative_invalid_absent_and_real_zero_are_distinct(self):
        sales = pd.DataFrame({"sales_qty_raw": [0, 3, np.nan, -2, np.nan, np.nan],
                              "sales_status": ["observed", "observed", "blank", "negative", "invalid", "not_in_source"]})
        strict = apply_policy(sales, "unknown_blanks")
        operational = apply_policy(sales, "operational_zero_assumption")
        self.assertEqual(strict.iloc[:2].tolist(), [0, 3])
        self.assertTrue(strict.iloc[2:].isna().all())
        self.assertEqual(operational.iloc[:4].tolist(), [0, 3, 0, 0])
        self.assertTrue(operational.iloc[4:].isna().all())

    def test_systeme_unit_header_preserves_codes_and_rejects_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "units.xlsx"
            workbook = Workbook()
            workbook.active.append(["Номенклатура", "Номенклатура.Код", "Ед.изм", "янв. 2024"])
            workbook.active.append(["Кабель", "001_", "м", 99999])
            workbook.save(path)
            workbook.close()
            catalog = {}
            read_unit_catalog(path, catalog)
            self.assertEqual(catalog["001_"]["unit"], "м")
            self.assertNotIn("stock", str(catalog))
            catalog["001_"]["unit"] = "шт"
            with self.assertRaisesRegex(ValueError, "несовместимые единицы"):
                read_unit_catalog(path, catalog)


if __name__ == "__main__":
    unittest.main()
