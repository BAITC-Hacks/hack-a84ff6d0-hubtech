"""Проверки временных границ, пропусков и чтения IEK-выгрузок."""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from openpyxl import Workbook

from prepare_ml_data import (
    FEATURES, FILES, build_features, prepare, read_matrix, read_moq, read_transit, reconcile,
)


class FeatureTests(unittest.TestCase):
    def frames(self):
        months = pd.date_range("2024-01-01", "2026-09-01", freq="MS")
        products = pd.DataFrame([dict(sku="001_", unit="м"), dict(sku="ярп42", unit="шт")])
        sales = pd.DataFrame([dict(sku=sku, month=month, sales_qty_raw=float(i + offset), sales_status="observed")
                              for sku, offset in [("001_", 1), ("ярп42", 100)] for i, month in enumerate(months)])
        stock = pd.DataFrame([dict(sku=r.sku, month=r.month, stock_qty_raw=200.5, stock_status="observed")
                              for r in sales.itertuples()])
        return sales, stock, products

    def test_features_do_not_see_target_month_or_future(self):
        sales, stock, products = self.frames()
        _, before, _ = build_features(sales, stock, products, "2026-09-23")
        boundary = pd.Timestamp("2026-03-01")
        sales.loc[sales.month >= boundary, "sales_qty_raw"] = 999999
        stock.loc[stock.month >= boundary, "stock_qty_raw"] = 777777
        _, after, _ = build_features(sales, stock, products, "2026-09-23")
        left = before.loc[before.target_month <= boundary, ["target_month", *FEATURES]].reset_index(drop=True)
        right = after.loc[after.target_month <= boundary, ["target_month", *FEATURES]].reset_index(drop=True)
        pd.testing.assert_frame_equal(left, right)

    def test_lags_are_per_sku_and_calendar_month(self):
        sales, stock, products = self.frames()
        panel, train, predict = build_features(sales, stock, products, "2026-09-23")
        april = panel[(panel.sku == "001_") & (panel.month == "2024-04-01")].iloc[0]
        self.assertEqual(april.lag_1, 3)
        self.assertEqual(april.mean_3, 2)
        self.assertEqual(april.history_observed_months, 3)
        self.assertEqual(april.opening_stock_lag_1, 200.5)
        self.assertTrue(panel.loc[panel.month == "2024-01-01", "lag_1"].isna().all())
        self.assertEqual(train.target_month.max(), pd.Timestamp("2026-08-01"))
        self.assertTrue(predict.target_month.eq(pd.Timestamp("2026-09-01")).all())
        self.assertEqual(predict.loc[predict.sku == "001_", "lag_1"].iloc[0], 32)
        self.assertEqual(train.loc[train.split == "validation", "target_month"].min(), pd.Timestamp("2026-01-01"))
        self.assertEqual(train.loc[train.split == "test", "target_month"].min(), pd.Timestamp("2026-07-01"))

    def test_explicit_zero_policy_does_not_hide_invalid_or_absent_data(self):
        sales, stock, products = self.frames()
        sku = sales.sku.eq("001_")
        sales.loc[sku & sales.month.eq("2024-01-01"), ["sales_qty_raw", "sales_status"]] = [np.nan, "blank"]
        sales.loc[sku & sales.month.eq("2024-02-01"), ["sales_qty_raw", "sales_status"]] = [-4, "negative"]
        sales.loc[sku & sales.month.eq("2024-03-01"), ["sales_qty_raw", "sales_status"]] = [np.nan, "invalid"]
        sales = sales[~(sku & sales.month.eq("2024-04-01"))]
        panel, _, _ = build_features(sales, stock, products, "2026-09-23")
        unknown = panel[(panel.sku == "001_") & (panel.month < "2024-05-01")]
        self.assertTrue(unknown.target_qty.isna().all())
        zero_panel, _, _ = build_features(sales, stock, products, "2026-09-23", "zero", "zero")
        values = zero_panel[zero_panel.sku == "001_"].head(4)
        self.assertEqual(values.target_qty.iloc[:2].tolist(), [0, 0])
        self.assertTrue(values.target_qty.iloc[2:].isna().all())
        self.assertEqual(values.sales_qty_raw.iloc[1], -4)
        self.assertEqual(values.sales_status.iloc[3], "not_in_source")

    def test_reconciliation_preserves_mismatch_and_sign(self):
        sales = pd.DataFrame([dict(sku="001_", month=pd.Timestamp("2026-01-01"), sales_qty_raw=10, sales_status="observed")])
        transactions = pd.DataFrame([
            dict(sku="001_", date=pd.Timestamp("2026-01-03"), qty_raw=12, document_type="Расходная накладная"),
            dict(sku="001_", date=pd.Timestamp("2026-01-04"), qty_raw=-2, document_type="Расходная накладная"),
            dict(sku="001_", date=pd.Timestamp("2026-01-05"), qty_raw=999, document_type="Заказ покупателя"),
        ])
        row = reconcile(sales, transactions, "2026-02-01").iloc[0]
        self.assertTrue(row.matches_signed)
        self.assertFalse(row.matches_positive)
        self.assertEqual(row.transactions_signed_qty, 10)
        self.assertEqual(row.transactions_positive_qty, 12)
        transactions.loc[len(transactions)] = dict(sku="001_", date=pd.Timestamp("2026-01-06"),
                                                    qty_raw=np.nan, document_type="Расходная накладная")
        incomplete = reconcile(sales, transactions, "2026-02-01").iloc[0]
        self.assertFalse(incomplete.comparable)
        self.assertFalse(incomplete.matches_signed)
        self.assertEqual(incomplete.transaction_unknown_qty_rows, 1)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def book(self, filename, rows):
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        path = self.root / filename
        workbook.save(path)
        workbook.close()
        return path

    def test_moq_duplicate_same_constraint_different_name(self):
        rows = [["Код 1с", "Артикул поставщика", "Наименование", "Мин. разр. к отгр."],
                ["001_", "ART1", "Имя первое", 6], ["001_", "ART1", "Имя другое", 6],
                ["002_", "ART2", "Товар", "#N/A"]]
        path = self.book("moq.xlsx", rows)
        issues = []
        result = read_moq(path, {}, issues)
        self.assertEqual(len(result), 2)
        self.assertEqual(result.iloc[0].min_order_qty, 6)
        self.assertTrue(pd.isna(result.iloc[1].min_order_qty))
        self.assertFalse(result.iloc[1].moq_known)
        self.assertIn("duplicate_constraint_removed", [x["issue"] for x in issues])
        rows[2][-1] = 12
        with self.assertRaisesRegex(ValueError, "Конфликт MOQ"):
            read_moq(self.book("conflict.xlsx", rows), {}, [])

    def test_arrival_date_uses_eta_not_order_date(self):
        path = self.book(FILES["transit"], [
            ["Код 1с", "Артикул ИЭК", " Наименование", "Заказ от 31.08.2026 (поступление до 10.10.2026)"],
            ["001_", "A1", "Кабель", 305],
            [1, 1, None], [0, "Расширение 3кв 24"],
        ])
        issues = []
        frame = read_transit(path, {}, issues)
        self.assertEqual(len(frame), 1)
        self.assertEqual([i["issue"] for i in issues], ["non_product_note_row_excluded"] * 2)
        row = frame.iloc[0]
        self.assertEqual(row.eta, pd.Timestamp("2026-10-10"))
        self.assertEqual(row.source_as_of, pd.Timestamp("2026-09-22"))
        self.assertEqual(row.qty_raw, 305)
        self.assertFalse(row.unit_conversion_confirmed)

    def test_matrix_preserves_excel_date_headers(self):
        path = self.book("matrix.xlsx", [
            ["Номенклатура", "Номенклатура.Код", datetime(2024, 1, 1), "февр. 2024"],
            ["Товар", "001_", 7, 0],
        ])
        result = read_matrix(path, "sales", {}, [])
        self.assertEqual(result.month.tolist(), [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-02-01")])
        self.assertEqual(result.sales_qty_raw.tolist(), [7, 0])

    def test_transit_removes_only_confirmed_notes_preserves_real_zero_one_codes(self):
        path = self.book(FILES["transit"], [
            ["Код 1с", "Артикул ИЭК", "Наименование", "поступление до 10.10.2026"],
            ["0", "Расширение 3кв 24", None, 0], ["1", "1"],
            [0, "Расширение 3кв 24"], [1, 1],
            ["0", "ART0", "Товар ноль", 7], ["1", "ART1", "Товар один", 4],
        ])
        catalog, issues = {}, []
        result = read_transit(path, catalog, issues)
        self.assertEqual(result.sku.tolist(), ["0", "1"])
        self.assertEqual(catalog["0"]["product_name"], "Товар ноль")
        self.assertEqual(catalog["1"]["supplier_sku"], "ART1")
        self.assertEqual([i["issue"] for i in issues], ["non_product_note_row_excluded"] * 4)

    def test_transit_other_numeric_code_is_not_silently_dropped(self):
        path = self.book(FILES["transit"], [
            ["Код 1с", "Артикул ИЭК", "Наименование", "поступление до 10.10.2026"],
            [123, "REAL-ARTICLE"],
        ])
        with self.assertRaisesRegex(ValueError, "хранится числом"):
            read_transit(path, {}, [])

    def test_supplier_article_conflict_across_moq_and_transit_fails(self):
        moq_path = self.book(FILES["moq"], [
            ["Код 1с", "Артикул поставщика", "Наименование", "Мин. разр. к отгр."],
            ["001_", "ART-OLD", "Товар", 6],
        ])
        transit_path = self.book(FILES["transit"], [
            ["Код 1с", "Артикул ИЭК", "Наименование", "поступление до 10.10.2026"],
            ["001_", "ART-NEW", "Товар", 6],
        ])
        catalog = {}
        read_moq(moq_path, catalog, [])
        with self.assertRaisesRegex(ValueError, "несовместимые артикулы поставщика"):
            read_transit(transit_path, catalog, [])
        self.assertEqual(catalog["001_"]["supplier_sku"], "ART-OLD")

    def test_full_pipeline_source_files_unchanged_and_old_snapshot_stays_partial(self):
        months = [f"{m} {year}" for year in [2024, 2025] for m in "янв. февр. март апр. май июнь июль авг. сент. окт. нояб. дек.".split()]
        months += [f"{m} 2026" for m in "янв. февр. март апр. май июнь июль авг. сент.".split()]
        self.book(FILES["sales"], [["Номенклатура", "Номенклатура.Код", *months, "Итого"],
                                    [None, None, *(["Количество"] * 34)], ["Товар", "001_", *range(1, 34), 561],
                                    ["Итого", None, *([999] * 34)]])
        self.book(FILES["stock"], [["Номенклатура", "Ед.", "Номенклатура.Код", *months, "Итого"],
                                    [None, None, None, *(["Количество"] * 34)],
                                    [None, None, None, *(["нач. остаток"] * 34)],
                                    ["Товар", "шт", "001_", *([100] * 33), 100]])
        self.book(FILES["transactions"], [
            ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"],
            ["22.09.2026 12:00:00", "00001", "Расходная накладная 00001", "001_", "Товар", "шт", "Алматы", 3]])
        self.book(FILES["moq"], [["Код 1с", "Артикул поставщика", "Наименование", "Мин. разр. к отгр."],
                                 ["001_", "ART1", "Товар", 6]])
        self.book(FILES["transit"], [["Код 1с", "Артикул ИЭК", "Наименование", "поступление до 10.10.2026"],
                                     ["001_", "ART1", "Товар", 10]])
        self.book(FILES["seasonality"], [["Месяц", "СЕЗОННОСТЬ"],
                                         *[[m, 1.0] for m in "янв фев мар апр май июн июл авг сен окт ноя дек".split()]])
        originals = {name: (self.root / name).read_bytes() for name in FILES.values()}
        output = self.root / "result"
        report = prepare(self.root, output, "2026-11-01")
        self.assertEqual(report["prediction_month"], "2026-09-01")
        self.assertEqual(report["data_cutoff_exclusive"], "2026-09-23")
        self.assertEqual(report["counts"]["training_rows"], 29)
        self.assertEqual(report["counts"]["prediction_rows"], 1)
        self.assertEqual(report["splits"]["test"]["end"], "2026-08-01")
        self.assertEqual(report["scope"]["supplier"], "IEK")
        self.assertEqual(report["scope"]["transaction_warehouses"], ["Алматы"])
        self.assertFalse(report["scope"]["monthly_warehouse_binding_confirmed"])
        self.assertFalse(report["scope"]["runtime_csv_source_compatible"])
        self.assertEqual(report["target_profile"]["positive_targets"], 29)
        self.assertEqual(report["target_profile"]["zero_targets"], 0)
        self.assertFalse(report["target_profile"]["total_demand_validated"])
        self.assertEqual(json.loads((output / "preparation_report.json").read_text(encoding="utf-8"))["feature_columns"], FEATURES)
        train = pd.read_csv(output / "training_data.csv", dtype={"sku": str})
        self.assertEqual(train.sku.iloc[0], "001_")
        self.assertNotIn("provided_factor", train.columns)
        self.assertNotIn("min_order_qty", train.columns)
        for name, content in originals.items():
            self.assertEqual((self.root / name).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
