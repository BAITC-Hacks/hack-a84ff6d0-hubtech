"""Shipped sample sources must reproduce dates, typed IDs, units and Excel output."""
from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook
from pandas.testing import assert_frame_equal

from app.core.export import ExportValidationError, to_excel_bytes
from app.core.recommend import generate_recommendations
from app.data.adapter import CsvDataSource, get_data_source
from app.data.synthetic import DEFAULT_AS_OF, SyntheticDataSource
from app.schemas import ExportLine, ExportRequest


SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample_csv"


def confirmed_export(response):
    decisions = [ExportLine(line_id=line.line_id, quantity=line.recommended_qty,
                            approved=line.recommended_qty > 0)
                 for group in response.groups for line in group.lines]
    request = ExportRequest(calculation_id=response.calculation_id, approved_only=True, lines=decisions)
    return load_workbook(BytesIO(to_excel_bytes(response, request)), data_only=False)


class SyntheticSampleTests(unittest.TestCase):
    def test_repeated_load_of_same_instance_reproduces_every_input(self):
        source = SyntheticDataSource()
        first, second = source.load(), source.load()
        self.assertEqual(first.as_of, date(2026, 9, 23))
        self.assertEqual(first.metadata, second.metadata)
        for name in ("sales", "stock", "stockouts", "in_transit", "catalog", "suppliers", "sku_suppliers"):
            with self.subTest(frame=name):
                assert_frame_equal(getattr(first, name), getattr(second, name))

    def test_factory_date_override_controls_sales_stock_and_transit(self):
        requested = date(2025, 2, 3)
        with patch("app.data.adapter.get_settings", return_value=SimpleNamespace(data_source="synthetic", data_as_of=requested)):
            data = get_data_source().load()
        self.assertEqual(data.as_of, requested)
        self.assertTrue(data.sales.date.ge(requested - timedelta(days=730)).all())
        self.assertTrue(data.sales.date.lt(requested).all())
        self.assertTrue(data.stock.as_of.eq(requested).all())
        self.assertTrue(data.in_transit.eta.ge(requested + timedelta(days=3)).all())
        self.assertTrue(data.in_transit.eta.lt(requested + timedelta(days=30)).all())
        self.assertTrue(data.in_transit.source_as_of.eq(requested).all())
        observed_warehouses = set(zip(data.stock.sku, data.stock.warehouse))
        self.assertTrue(set(zip(data.in_transit.sku, data.in_transit.warehouse)) <= observed_warehouses)

    def test_synthetic_known_units_allow_confirmed_excel(self):
        data = SyntheticDataSource().load()
        self.assertEqual(set(data.catalog.unit), {"м", "шт"})
        response = generate_recommendations(data, explain=False)
        self.assertGreater(response.sku_count, 0)
        workbook = confirmed_export(response)
        rows = list(workbook["Заказ поставщикам"].values)
        units_column = rows[0].index("Ед. изм.")
        self.assertTrue({row[units_column] for row in rows[1:]} <= {"м", "шт"})
        workbook.close()

    def test_short_synthetic_history_has_no_invalid_stockout_range(self):
        data = SyntheticDataSource(days=1).load()
        self.assertEqual(data.as_of, DEFAULT_AS_OF)
        self.assertTrue(data.stockouts.empty)
        with self.assertRaisesRegex(ValueError, "хотя бы один день"):
            SyntheticDataSource(days=0)


class CsvSampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        shutil.copytree(SAMPLE, self.root, dirs_exist_ok=True)

    def read_text_table(self, name):
        return pd.read_csv(self.root / name, dtype=str, keep_default_na=False)

    def test_shipped_csv_is_typed_dated_and_preserves_identifiers_in_excel(self):
        data = CsvDataSource(self.root).load()
        self.assertEqual(data.as_of, date(2026, 9, 23))
        self.assertEqual(set(data.sales.sku), {"00123_", "00007"})
        self.assertEqual(set(data.sales.client_id), {"00001", "00002"})
        self.assertEqual(set(data.suppliers.supplier_id), {"0001", "0002"})
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(data.sales.date))
        self.assertTrue(pd.api.types.is_numeric_dtype(data.sales.qty))
        response = generate_recommendations(data, explain=False, service_level=.5, review_period_days=2)
        lines = {line.sku: line for group in response.groups for line in group.lines}
        self.assertEqual(lines["00123_"].recommended_qty, 40)
        self.assertEqual(lines["00123_"].rationale.lost_demand_uplift, 30)
        self.assertEqual(lines["00007"].recommended_qty, 10)
        workbook = confirmed_export(response)
        rows = list(workbook["Заказ поставщикам"].values)
        columns = {name: i for i, name in enumerate(rows[0])}
        exported = {row[columns["Артикул"]]: row for row in rows[1:]}
        self.assertEqual(exported["00123_"][columns["Артикул поставщика"]], "000123")
        self.assertEqual(exported["00007"][columns["Ед. изм."]], "шт")
        self.assertTrue(all(row[columns["Утверждено"]] == "Да" for row in rows[1:]))
        workbook.close()

    def test_csv_factory_honors_explicit_date_without_rewriting_sources(self):
        settings = SimpleNamespace(data_source="csv", data_dir=str(self.root), data_as_of=date(2026, 9, 20))
        with patch("app.data.adapter.get_settings", return_value=settings):
            data = get_data_source().load()
        self.assertEqual(data.as_of, date(2026, 9, 20))
        self.assertEqual(data.sales.date.max().date(), date(2026, 9, 22))
        response = generate_recommendations(data, explain=False)
        self.assertTrue(all(line.rationale.in_transit == 0 for group in response.groups for line in group.lines))

    def test_optional_catalog_falls_back_to_explicit_sales_unit(self):
        (self.root / "catalog.csv").unlink()
        sales = self.read_text_table("sales.csv")
        sales["unit"] = sales.sku.map({"00123_": "м", "00007": "шт"})
        sales.to_csv(self.root / "sales.csv", index=False)
        data = CsvDataSource(self.root).load()
        self.assertEqual(set(data.catalog.unit), {"м", "шт"})
        workbook = confirmed_export(generate_recommendations(data, explain=False))
        workbook.close()

    def test_missing_unit_stays_unknown_and_cannot_be_confirmed(self):
        (self.root / "catalog.csv").unlink()
        data = CsvDataSource(self.root).load()
        response = generate_recommendations(data, explain=False)
        self.assertEqual(data.metadata["unknown_units"], 2)
        self.assertTrue(all(line.unit == "ед. (не указана)" for group in response.groups for line in group.lines))
        with self.assertRaisesRegex(ExportValidationError, "единицу измерения"):
            confirmed_export(response)

    def test_conflicting_units_are_not_silently_combined(self):
        sales = self.read_text_table("sales.csv")
        sales["unit"] = "шт"
        sales.to_csv(self.root / "sales.csv", index=False)
        with self.assertRaisesRegex(ValueError, "неоднозначная единица"):
            CsvDataSource(self.root).load()

    def test_invalid_numeric_date_and_required_schema_fail_with_source_name(self):
        original = self.read_text_table("sales.csv")
        for invalid in ("not-a-number", "inf", "", "NaN"):
            with self.subTest(quantity=invalid):
                changed = original.copy()
                changed.loc[0, "qty"] = invalid
                changed.to_csv(self.root / "sales.csv", index=False)
                with self.assertRaisesRegex(ValueError, "sales.csv: qty"):
                    CsvDataSource(self.root).load()
        changed = original.copy()
        changed.loc[0, "date"] = "not-a-date"
        changed.to_csv(self.root / "sales.csv", index=False)
        with self.assertRaisesRegex(ValueError, "sales.csv: date"):
            CsvDataSource(self.root).load()
        original.drop(columns="warehouse").to_csv(self.root / "sales.csv", index=False)
        with self.assertRaisesRegex(ValueError, "sales.csv: отсутствуют колонки warehouse"):
            CsvDataSource(self.root).load()

    def test_empty_optional_flow_tables_keep_datetime_contract(self):
        for name in ("stockouts.csv", "in_transit.csv"):
            self.read_text_table(name).iloc[:0].to_csv(self.root / name, index=False)
        data = CsvDataSource(self.root).load()
        self.assertTrue(data.stockouts.empty)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(data.stockouts.start))
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(data.in_transit.eta))

    def test_identifier_na_is_literal_not_a_missing_value(self):
        (self.root / "identifiers.csv").write_text("sku,warehouse,client_id,qty\nNA,0005,00002,10\n", encoding="utf-8")
        frame = CsvDataSource(self.root)._read("identifiers.csv")
        self.assertEqual(frame.sku.iloc[0], "NA")
        self.assertEqual(frame.warehouse.iloc[0], "0005")


if __name__ == "__main__":
    unittest.main()
