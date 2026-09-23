"""Проверки входов закупки: остатки, даты, единицы, отсутствие подстановок."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from prepare_order_inputs import build_snapshot, csv, load_config


class OrderInputTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(supplier_id="IEK", as_of="2026-09-23", warehouse="Алматы", lead_time_days=21,
                           review_period_days=14, service_level=.95, parameters_confirmed=True,
                           sales_semantics_confirmed=True, blank_sales="missing", negative_sales="missing",
                           stock_max_age_days=1, transit_max_age_days=7)
        self.products = pd.DataFrame([dict(sku="001_", product_name="Кабель", unit="м", supplier_sku="ART1")])
        self.monthly = pd.DataFrame([dict(sku="001_", month=pd.Timestamp("2026-08-01"), sales_qty_raw=30,
                                         stock_qty_raw=100, target_qty=30, sales_status="observed", stock_status="observed"),
                                    dict(sku="001_", month=pd.Timestamp("2026-09-01"), sales_qty_raw=999,
                                         stock_qty_raw=90, target_qty=float("nan"), sales_status="observed", stock_status="observed")])
        self.moq = pd.DataFrame([dict(sku="001_", min_order_qty=5)])
        self.transit = pd.DataFrame([dict(sku="001_", shipment="Поставка 1", eta=pd.Timestamp("2026-09-30"), qty_raw=20)])
        self.comparison = pd.DataFrame(columns=["sku", "month", "comparable", "matches_signed"]).astype({"month": "datetime64[ns]", "comparable": bool, "matches_signed": bool})
        self.seasonality = pd.DataFrame([dict(month_number=i, provided_factor=1.) for i in range(1, 13)])
        self.stock = pd.DataFrame([dict(sku="001_", warehouse="Алматы", unit="м", as_of=pd.Timestamp("2026-09-22"), free_stock=10.)])
        self.rules = pd.DataFrame([dict(sku="001_", order_unit="м", stock_units_per_order_unit=1., pack_size=1.,
                                       min_order_qty=float("nan"), transit_unit="м", stock_units_per_transit_unit=1.)])

    def build(self, stock=True, rules=True, covered=True, snapshot="2026-09-22"):
        return build_snapshot(self.config, self.products, self.monthly, self.moq, self.transit,
                              self.comparison, self.seasonality, {"001_"} if covered else set(),
                              pd.Timestamp(snapshot), "2026-09-23", self.stock if stock else None,
                              self.rules if rules else None)

    def test_complete_input_keeps_dates_quantities_and_confirmed_parameters(self):
        rows, schedule, report = self.build()
        row = rows.iloc[0]
        self.assertEqual(row.input_status, "ready")
        self.assertEqual(row.on_hand, 10)
        self.assertEqual(row.historical_opening_stock, 90)
        self.assertEqual(row.in_transit, 20)
        self.assertEqual(row.horizon_days, 35)
        self.assertEqual(row.history_observed_months, 1)
        self.assertEqual(report["horizon_end_exclusive"], "2026-10-28")
        self.assertTrue(schedule.usable.all())

    def test_historical_balance_never_substitutes_for_current_stock(self):
        rows, _, _ = self.build(stock=False)
        self.assertTrue(pd.isna(rows.iloc[0].on_hand))
        self.assertEqual(rows.iloc[0].historical_opening_stock, 90)
        self.assertIn("current_free_stock", rows.iloc[0].missing_fields)
        self.stock["free_stock"] = 0.
        rows, _, _ = self.build()
        self.assertEqual(rows.iloc[0].on_hand, 0)
        self.assertTrue(rows.iloc[0].ready_for_calculation)

    def test_invalid_stock_snapshots_are_not_used(self):
        cases = [("2026-09-01", "м", "stale"), ("2026-09-24", "м", "future_snapshot"),
                 ("2026-09-22", "шт", "unit_mismatch")]
        for date, unit, expected in cases:
            with self.subTest(expected=expected):
                self.stock.loc[0, "as_of"] = pd.Timestamp(date)
                self.stock.loc[0, "unit"] = unit
                row = self.build()[0].iloc[0]
                self.assertEqual(row.stock_status, expected)
                self.assertTrue(pd.isna(row.on_hand))

    def test_moq_is_not_pack_multiple_and_conversion_is_not_invented(self):
        row = self.build(rules=False)[0].iloc[0]
        self.assertEqual(row.min_order_qty, 5)
        self.assertTrue(pd.isna(row.pack_size))
        self.assertTrue(pd.isna(row.in_transit))
        self.assertIn("purchase_unit_conversion", row.missing_fields)
        self.rules.loc[0, "order_unit"] = "бухта"
        self.rules.loc[0, "stock_units_per_order_unit"] = 305.
        row = self.build()[0].iloc[0]
        self.assertEqual(row.in_transit, 20)  # Путь в метрах, заказ в бухтах.
        self.assertEqual(row.stock_units_per_order_unit, 305)

    def test_arrival_window_and_missing_coverage(self):
        self.transit = pd.concat([self.transit, pd.DataFrame([
            dict(sku="001_", shipment="На границе", eta=pd.Timestamp("2026-10-28"), qty_raw=100),
            dict(sku="001_", shipment="Просрочено", eta=pd.Timestamp("2026-09-22"), qty_raw=500),
        ])], ignore_index=True)
        rows, schedule, _ = self.build()
        self.assertEqual(rows.iloc[0].in_transit, 20)
        self.assertEqual(schedule.eta_bucket.tolist(), ["in_horizon", "after_horizon", "overdue"])
        self.assertEqual(rows.iloc[0].input_status, "review")
        self.assertTrue(pd.isna(self.build(covered=False)[0].iloc[0].in_transit))
        self.assertTrue(pd.isna(self.build(snapshot="2026-09-24")[0].iloc[0].in_transit))
        self.transit = self.transit.iloc[:0]
        self.assertEqual(self.build()[0].iloc[0].in_transit, 0)

    def test_unknown_sales_semantics_and_conflicts_require_review(self):
        self.config["sales_semantics_confirmed"] = False
        self.comparison = pd.DataFrame([dict(sku="001_", month=pd.Timestamp("2026-08-01"), comparable=True, matches_signed=False)])
        row = self.build()[0].iloc[0]
        self.assertEqual(row.input_status, "review")
        self.assertIn("sales_semantics_unconfirmed", row.review_reasons)
        self.assertIn("monthly_transaction_mismatch", row.review_reasons)

    def test_unknown_receipt_quantity_is_not_reported_as_zero(self):
        self.transit["qty_raw"] = float("nan")
        row = self.build()[0].iloc[0]
        self.assertTrue(pd.isna(row.in_transit))
        self.assertTrue(pd.isna(row.reported_in_transit_qty))
        self.assertIn("in_transit", row.missing_fields)

    def test_duplicate_current_stock_does_not_double_count(self):
        self.stock = pd.concat([self.stock, self.stock], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "повтор ключа"):
            self.build()

    def test_csv_keeps_leading_zero_and_rejects_invalid_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stock.csv"
            path.write_text("sku,free_stock\n00001,0\n", encoding="utf-8")
            frame = csv(path, ["sku", "free_stock"], numeric=["free_stock"])
            self.assertEqual(frame.sku.iloc[0], "00001")
            self.assertEqual(frame.free_stock.iloc[0], 0)
            path.write_text("sku,free_stock\n00001,unknown\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "некорректное число"):
                csv(path, ["sku", "free_stock"], numeric=["free_stock"])

    def test_config_validates_quantities_and_confirmation_types(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            self.assertEqual(load_config(path)["lead_time_days"], 21)
            bad = copy.deepcopy(self.config)
            bad["parameters_confirmed"] = "false"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "true/false"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
