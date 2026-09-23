"""Сезонность/тренд без будущего и проверка документов без автоматического вычитания."""
import pickle
import unittest

import numpy as np
import pandas as pd

from explainable_demand import fit_decomposition, predict_components
from detect_exceptional_orders import detect_candidates
from run_demand_decomposition import select_damping


class DecompositionTests(unittest.TestCase):
    def panel(self, y):
        return pd.DataFrame(dict(sku="001_", month=pd.date_range("2023-01-01", periods=len(y), freq="MS"), target_qty=y))

    def frame(self, month="2026-01-01"):
        return pd.DataFrame([dict(sku="001_", unit="шт", target_month=pd.Timestamp(month))])

    def test_linear_growth_is_exact_and_formula_reconstructs_prediction(self):
        panel = self.panel(10 + 2 * np.arange(36))
        bundle = fit_decomposition(panel, "2026-01-01")
        row = predict_components(bundle, self.frame(), 1).iloc[0]
        self.assertAlmostEqual(row.trend_per_month, 2)
        self.assertAlmostEqual(row.predicted_qty, 82)
        self.assertAlmostEqual(row.predicted_qty, max(0, row.level_qty + row.trend_contribution_qty) * row.seasonal_factor)
        flat = predict_components(bundle, self.frame(), 0).iloc[0]
        self.assertAlmostEqual(flat.predicted_qty, 78)

    def test_future_mutation_does_not_change_fitted_components(self):
        panel = self.panel(20 + np.arange(40))
        before = fit_decomposition(panel, "2026-01-01")
        panel.loc[panel.month.ge("2026-01-01"), "target_qty"] = 999999
        after = fit_decomposition(panel, "2026-01-01")
        self.assertEqual(before, after)
        with self.assertRaisesRegex(ValueError, "прошлое"):
            predict_components(before, self.frame("2025-12-01"), 1)

    def test_missing_months_keep_calendar_distance(self):
        panel = self.panel(10 + 2 * np.arange(36))
        panel.loc[panel.month.eq("2025-06-01"), "target_qty"] = np.nan
        row = predict_components(fit_decomposition(panel, "2026-01-01"), self.frame(), 1).iloc[0]
        self.assertAlmostEqual(row.trend_per_month, 2)
        self.assertAlmostEqual(row.predicted_qty, 82)

    def test_seasonal_factors_normalized_and_neutral_without_two_cycles(self):
        seasonal = np.array([.7, .8, .9, 1., 1.1, 1.2, 1.3, 1.2, 1.1, 1., .9, .8])
        full = fit_decomposition(self.panel(np.tile(seasonal, 3) * 100), "2026-01-01")
        fit = full["products"]["001_"]
        self.assertEqual(fit["seasonality_status"], "estimated_from_past")
        self.assertAlmostEqual(np.mean(fit["seasonal_factors"]), 1)
        self.assertGreater(fit["seasonal_factors"][6], fit["seasonal_factors"][0])
        short = fit_decomposition(self.panel(np.tile(seasonal, 1) * 100), "2024-01-01")
        self.assertEqual(short["products"]["001_"]["seasonal_factors"], [1.] * 12)

    def test_one_spike_does_not_become_stable_growth(self):
        y = np.full(36, 10.); y[-1] = 1000
        fit = fit_decomposition(self.panel(y), "2026-01-01")["products"]["001_"]
        self.assertEqual(fit["stable_trend_per_month"], 0)
        self.assertIn(fit["trend_status"], ["unstable_trend_disabled", "flat_or_unstable"])

    def test_unknown_is_not_zero_and_pickle_round_trip(self):
        panel = self.panel(np.full(36, np.nan))
        bundle = fit_decomposition(panel, "2026-01-01")
        self.assertTrue(predict_components(bundle, self.frame(), .5).predicted_qty.isna().all())
        panel.target_qty = 0.
        bundle = fit_decomposition(panel, "2026-01-01")
        restored = pickle.loads(pickle.dumps(bundle))
        self.assertEqual(predict_components(restored, self.frame(), .5).predicted_qty.iloc[0], 0.)

    def test_damping_selection_uses_validation_only(self):
        rows = [dict(model=f"decomposition_damping_{d:g}", split="validation", unit="шт", actual_qty=10,
                     predicted_qty=p) for d, p in [(0, 12), (.5, 10), (1, 14)]]
        self.assertEqual(select_damping(pd.DataFrame(rows))[0], .5)
        rows[-1]["split"] = "test"
        with self.assertRaises(ValueError): select_damping(pd.DataFrame(rows))


class ExceptionalOrderTests(unittest.TestCase):
    def frames(self):
        rows = [dict(sku="001_", unit="шт", warehouse="Алматы", date=pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                     document_number=str(i), document_type="Расходная накладная", qty_raw=2.) for i in range(8)]
        # Две строки одного документа должны стать одним заказом 40 шт.
        for _ in range(2):
            rows.append(dict(sku="001_", unit="шт", warehouse="Алматы", date=pd.Timestamp("2026-01-10"),
                             document_number="BULK", document_type="Расходная накладная", qty_raw=20.))
        panel = pd.DataFrame([dict(sku="001_", month=pd.Timestamp("2026-01-01"), sales_qty_raw=56., target_qty=56.)])
        reconciliation = panel.assign(comparable=True, matches_signed=True)
        return pd.DataFrame(rows), panel, reconciliation

    def test_grouping_thresholds_and_no_automatic_subtraction(self):
        tx, panel, reconciliation = self.frames()
        candidates, history, report = detect_candidates(tx, panel, reconciliation, "2026-02-01")
        self.assertEqual(len(candidates), 1)
        row = candidates.iloc[0]
        self.assertEqual(row.order_qty, 40)
        self.assertEqual(row.prior_orders, 8)
        self.assertEqual(row.threshold_8median, 16)
        self.assertEqual(row.threshold_iqr, 2)
        self.assertTrue(row.monthly_matches_signed)
        self.assertEqual(row.excluded_qty, 0)
        self.assertEqual(history.regular_sales_qty.iloc[0], 56)
        self.assertEqual(report["automatically_excluded_orders"], 0)

    def test_unknown_line_makes_whole_document_unusable(self):
        tx, panel, reconciliation = self.frames()
        tx.loc[tx.index[-1], "qty_raw"] = np.nan
        candidates, _, _ = detect_candidates(tx, panel, reconciliation, "2026-02-01")
        self.assertTrue(candidates.empty)

    def test_future_orders_do_not_change_past_threshold(self):
        tx, panel, reconciliation = self.frames()
        before, _, _ = detect_candidates(tx, panel, reconciliation, "2026-02-01")
        future = tx.iloc[[-1]].assign(date=pd.Timestamp("2026-03-01"), qty_raw=1e6, document_number="future")
        after, _, _ = detect_candidates(pd.concat([tx, future]), panel, reconciliation, "2026-02-01")
        pd.testing.assert_frame_equal(before, after)

    def test_mismatch_is_review_only_and_negative_lines_not_hidden(self):
        tx, panel, reconciliation = self.frames()
        reconciliation["matches_signed"] = False
        candidates, _, _ = detect_candidates(tx, panel, reconciliation, "2026-02-01")
        self.assertFalse(candidates.monthly_matches_signed.any())
        self.assertTrue(candidates.review_reason.eq("monthly_and_transactions_not_reconciled").all())
        tx.loc[tx.index[-1], "qty_raw"] = -1
        candidates, _, _ = detect_candidates(tx, panel, reconciliation, "2026-02-01")
        self.assertTrue(candidates.empty)


if __name__ == "__main__":
    unittest.main()
