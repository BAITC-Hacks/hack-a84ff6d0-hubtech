"""Small actual XLSX fixtures plus an opt-in smoke test of partner workbooks."""
from __future__ import annotations

from datetime import date
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pandas as pd

from app.config import PROJECT_ROOT, Settings
from app.data.excel import ExcelDataSource, MONTH_NAMES, SUPPLIER_FILES, normalize_code, parse_eta, parse_number
from app.data.excel import _cached_load, _Importer


def write_fixture(path, rows, dimension="A1:B2"):
    """Minimal XLSX, including cached formula/error cells and stale dimensions.

    Fixture XML avoids an authoring dependency and makes the saved formula
    values explicit: libraries usually cannot generate cached Excel results.
    """
    def col_name(index):
        result = ""
        while index:
            index, mod = divmod(index - 1, 26)
            result = chr(65 + mod) + result
        return result

    body = []
    for number, row in enumerate(rows, 1):
        cells = []
        for index, value in enumerate(row, 1):
            if value is None:
                continue
            address = f"{col_name(index)}{number}"
            if isinstance(value, dict):
                cells.append(f'<c r="{address}"><f>{escape(value["formula"])}</f><v>{value["cached"]}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{address}"><v>{value}</v></c>')
            elif value.startswith("#"):
                cells.append(f'<c r="{address}" t="e"><v>{escape(value)}</v></c>')
            else:
                cells.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        body.append(f'<row r="{number}">{"".join(cells)}</row>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as book:
        book.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        book.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="{dimension}"/><sheetData>{"".join(body)}</sheetData></worksheet>')


def fixture_sources(root):
    for supplier_id, spec in SUPPLIER_FILES.items():
        sku = "0001_" if supplier_id == "IEK" else "0100_"
        other = "0002_" if supplier_id == "IEK" else "0101_"
        path = lambda kind: root / spec["folder"] / spec[kind]
        write_fixture(path("sales"), [
            ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"],
            ["22.09.2026 16:10:12", 1, "Расход", f" {sku}\u00a0", "Товар", "шт", "Алматы", "1\u00a0234,5"],
            ["21.09.2026 09:01:01", 2, "Корректировка", sku, "Товар", "шт", "Алматы", -20],
            ["not a date", 3, "Расход", sku, "Товар", "шт", "Алматы", 5],
            [None, None, None, None, "Итого", None, None, 1214.5],
        ])
        write_fixture(path("monthly_sales"), [
            ["Номенклатура", "Номенклатура.Код", "янв. 2026", "сент. 2026", "Итого"],
            ["Товар", sku, None, "10,5", 10.5],
            ["Второй", other, -5, "#N/A", -5],
            ["Итого", None, -5, 10.5, 5.5],
        ])
        write_fixture(path("monthly_stock"), [
            ["Номенклатура", "Ед.", "Номенклатура.Код", "янв. 2026", "сент. 2026"],
            ["Товар", "шт", sku, 100, 30],
            ["Второй", "шт", other, None, None],
        ])
        if supplier_id == "IEK":
            write_fixture(path("moq"), [
                ["№", "Код 1с", "Артикул поставщика", "Наименование", "Мин. разр. к отгр."],
                [1, sku, "IEK-01", "Товар", {"formula": 'VLOOKUP(B2,[1]Sheet1!A:B,2,FALSE)', "cached": 6}],
                [2, other, "IEK-02", "Второй", "#N/A"],
            ])
            write_fixture(path("transit"), [
                ["Код 1с", "Артикул ИЭК", "Наименование", "РФ от 31 августа 2026 г. (поступление до 10.10.2026)", "поступление до 30.09.2026"],
                [sku, "IEK-01", "Товар", 12, "24,5"],
            ])
        else:
            write_fixture(path("moq"), [
                ["№", "Номенклатура", "Номенклатура.Код", "Артикул", "Кратность"],
                [1, "Товар", sku, "SE-01", 10], [2, "Второй", other, "SE-02", 0],
            ])
            write_fixture(path("transit"), [
                [None, "СКЛАДЫ"],
                ["Код 1с", "Артикул поставщика", "Наименование", "Категория 2026", "Свободный остаток", "СЭ в пути 24.09"],
                [sku, "SE-01", "Товар", "7", {"formula": "99-9", "cached": 90}, 20],
            ])
        write_fixture(path("seasonality"), [
            [None], [None], ["Месяц", "СЕЗОННОСТЬ"],
            *[[month, {"formula": "1+0", "cached": 1}] for month in MONTH_NAMES],
        ])


class ExcelImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture_sources(self.root)

    def test_normalization_and_real_format_import(self):
        dataset = ExcelDataSource(self.root).load()
        self.assertEqual(dataset.as_of, date(2026, 9, 23))
        self.assertEqual(dataset.source, "excel")
        self.assertEqual(len(dataset.metadata["files"]), 12)
        self.assertEqual(len(dataset.sales), 2)
        self.assertEqual(dataset.sales.qty.sum(), 2469)
        self.assertEqual(set(dataset.sales.date.dt.hour), {0})
        self.assertEqual(set(dataset.sales.sku), {"0001_", "0100_"})
        self.assertTrue(dataset.sales.client_id.isna().all())
        self.assertEqual(set(dataset.sales.order_id), {"1"})
        self.assertTrue(dataset.stockouts.empty)
        self.assertEqual(dataset.metadata["counts"]["negative_transactions"], 2)
        self.assertEqual(dataset.metadata["counts"]["invalid_transaction_dates"], 2)
        self.assertEqual(dataset.metadata["unknown_stock_skus"], 2)
        self.assertEqual(len(dataset.seasonality), 24)  # Beyond deliberately stale dimensions.
        self.assertEqual(dataset.monthly_sales.query("sku == '0002_'").iloc[0].raw_qty, -5)
        self.assertEqual(dataset.monthly_sales.query("sku == '0002_'").iloc[0].qty, 0)
        self.assertEqual(len(dataset.monthly_sales.query("sku == '0002_'")), 1)  # Error is not zero.

    def test_dates_stock_and_order_constraints(self):
        dataset = ExcelDataSource(self.root, iek_lead_time_days=17, systeme_lead_time_days=31).load()
        stock = dataset.stock.set_index("sku")
        self.assertEqual(stock.loc["0001_", "as_of"], pd.Timestamp("2026-09-01"))
        self.assertEqual(stock.loc["0001_", "source"], "monthly_opening_balance")
        self.assertEqual(stock.loc["0100_", "on_hand"], 90)
        self.assertEqual(stock.loc["0100_", "as_of"], pd.Timestamp("2026-09-22"))
        self.assertEqual(set(dataset.in_transit.eta), {pd.Timestamp("2026-09-24"), pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-10")})
        constraints = dataset.sku_suppliers.set_index("sku")
        self.assertEqual(constraints.loc["0001_", "min_order_qty"], 6)
        self.assertEqual(constraints.loc["0001_", "pack_size"], 1)
        self.assertEqual(constraints.loc["0100_", "pack_size"], 10)
        self.assertFalse(constraints.loc["0002_", "constraint_known"])
        self.assertEqual(dataset.suppliers.lead_time_days.tolist(), [17, 31])
        self.assertEqual(dataset.catalog.set_index("sku").loc["0100_", "category"], "Категория 7")
        historical = ExcelDataSource(self.root, as_of=date(2026, 9, 1)).load()
        self.assertEqual(historical.stock.set_index("sku").loc["0100_", "on_hand"], 30)

    def test_cached_results_are_isolated_and_files_invalidate_cache(self):
        source = ExcelDataSource(self.root)
        dataset = source.load()
        dataset.sales.loc[:, "qty"] = 0
        dataset.metadata["assumptions"]["warehouse"] = "changed"
        fresh = source.load()
        self.assertEqual(fresh.sales.qty.sum(), 2469)
        self.assertEqual(fresh.metadata["assumptions"]["warehouse"], "Алматы")
        spec = SUPPLIER_FILES["IEK"]
        path = self.root / spec["folder"] / spec["moq"]
        write_fixture(path, [["№", "Код 1с", "Артикул поставщика", "Наименование", "Мин. разр. к отгр."],
                             [1, "0001_", "IEK-01", "Товар", 24]])
        self.assertEqual(source.load().sku_suppliers.set_index("sku").loc["0001_", "min_order_qty"], 24)

    def test_concurrent_cold_import_only_reads_workbooks_once(self):
        _cached_load.cache_clear()
        original = _Importer.load
        def slow_load(importer):
            time.sleep(0.05)
            return original(importer)
        with patch.object(_Importer, "load", autospec=True, side_effect=slow_load) as loader:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: ExcelDataSource(self.root).load(), range(4)))
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(len(results), 4)
        self.assertIsNot(results[0], results[1])

    def test_missing_workbook_does_not_fall_back_to_demo(self):
        spec = SUPPLIER_FILES["IEK"]
        (self.root / spec["folder"] / spec["moq"]).unlink()
        with self.assertRaisesRegex(ValueError, "Не найдены исходные Excel"):
            ExcelDataSource(self.root).load()

    def test_helpers_and_paths(self):
        self.assertEqual(normalize_code(" 00\u00a012_ "), "0012_")
        self.assertEqual(parse_number(" 1\u202f234,50 "), 1234.5)
        self.assertIsNone(parse_number("#N/A"))
        self.assertIsNone(parse_number("NaN"))
        self.assertEqual(parse_eta("поступление до 01.10.2026", date(2026, 9, 22)), date(2026, 10, 1))
        with patch.dict(os.environ, {"DATA_SOURCE": "excel", "DATA_DIR": ".", "DATA_AS_OF": ""}):
            self.assertEqual(Path(Settings().data_dir), PROJECT_ROOT)

    def test_skip_only_confirmed_iek_transit_notes_preserving_real_codes(self):
        spec = SUPPLIER_FILES["IEK"]
        write_fixture(self.root / spec["folder"] / spec["transit"], [
            ["Код 1с", "Артикул ИЭК", "Наименование", "поступление до 30.09.2026"],
            [0, "Расширение 3кв 24"], [1, 1, None],
            ["0", "REAL-0", "Настоящий товар с кодом 0", 2],
            ["1", "REAL-1", "Настоящий товар с кодом 1", 3],
            ["00123_", "ARTICLE", "Товар с ведущими нулями", 4],
            ["BR-AK20-1-K35_", "ARTICLE-2", "Товар с буквенным кодом", 5],
        ])
        data = ExcelDataSource(self.root).load()
        self.assertEqual(data.metadata["counts"]["ignored_transit_note_rows"], 2)
        catalog = data.catalog.set_index("sku")
        self.assertEqual(catalog.loc["0", "supplier_sku"], "REAL-0")
        self.assertEqual(catalog.loc["1", "supplier_sku"], "REAL-1")
        self.assertIn("00123_", catalog.index)
        self.assertIn("BR-AK20-1-K35_", catalog.index)
        self.assertEqual(data.in_transit[data.in_transit.sku == "0"].qty.sum(), 2)

    @unittest.skipUnless(os.getenv("RUN_REAL_EXCEL_TESTS") == "1", "Set RUN_REAL_EXCEL_TESTS=1 for partner workbook smoke test")
    def test_partner_workbooks(self):
        data = ExcelDataSource(PROJECT_ROOT).load()
        self.assertEqual(data.as_of, date(2026, 9, 23))
        self.assertEqual(len(data.metadata["files"]), 12)
        self.assertGreater(len(data.sales), 200000)
        self.assertGreater(len(data.catalog), 3000)
        self.assertEqual(len(data.seasonality), 24)
        self.assertFalse(data.catalog.sku.duplicated().any())
        self.assertTrue(data.stockouts.empty)
        self.assertNotIn("0", set(data.catalog.sku))
        self.assertNotIn("1", set(data.catalog.sku))
        self.assertEqual(data.metadata["counts"]["ignored_transit_note_rows"], 2)


if __name__ == "__main__":
    unittest.main()
