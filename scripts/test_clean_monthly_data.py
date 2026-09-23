"""Проверки правил очистки на небольших синтетических Excel-файлах."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from clean_monthly_data import clean_datasets


class CleaningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def book(self, name, rows):
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        path = self.root / name
        workbook.save(path)
        workbook.close()
        return path

    def sources(self, stock_rows, sales_rows):
        stock = self.book("stock.xlsx", [
            ["Отчёт по остаткам"],
            ["Номенклатура", "Ед.", "Номенклатура.Код", "янв. 2024", "февр. 2024", "Итого"],
            [None, None, None, "Количество", "Количество", "Количество"],
            [None, None, None, "нач. остаток", "нач. остаток", "нач. остаток"],
            *stock_rows,
            ["Итого", None, None, 999, 999, 999],
        ])
        sales = self.book("sales.xlsx", [
            ["Номенклатура", "Номенклатура.Код", "янв. 2024", "февр. 2024", "Итого"],
            [None, None, "Количество", "Количество", "Количество"],
            *sales_rows,
            ["Итого", None, 999, 999, 999],
        ])
        return stock, sales

    def test_preserves_missing_zero_negative_fractional_and_unmatched_codes(self):
        stock, sales = self.sources([
            ["  Кабель  тест ", "м", "001_", 0, 1.25, 999],
            ["Только остатки", "шт", "002_", 4, None, 999],
        ], [
            ["Кабель тест", "001_", None, -2, 999],
            ["Только продажи", "ярп40241", "1 234,5", 0, 999],
        ])
        original = (stock.read_bytes(), sales.read_bytes())
        report = clean_datasets(stock, sales, self.root / "out")
        data = pd.read_csv(self.root / "out/monthly_clean.csv", dtype={"product_code": str})
        self.assertEqual(report["output_rows"], 6)
        self.assertEqual(report["output_products"], 3)
        item = data[data.product_code == "001_"].sort_values("month")
        self.assertTrue(pd.isna(item.iloc[0].sales_qty))
        self.assertEqual(item.iloc[0].opening_stock_qty, 0)
        self.assertEqual(item.iloc[1].opening_stock_qty, 1.25)
        self.assertEqual(item.iloc[1].sales_qty, -2)
        self.assertEqual(item.iloc[0].product_name, "Кабель тест")
        only_sales = data[data.product_code == "ярп40241"].sort_values("month")
        self.assertFalse(only_sales.stock_row_present.any())
        self.assertTrue(only_sales.opening_stock_qty.isna().all())
        self.assertTrue(only_sales.unit.isna().all())
        self.assertEqual(only_sales.iloc[0].sales_qty, 1234.5)
        self.assertEqual(original, (stock.read_bytes(), sales.read_bytes()))

    def test_exact_duplicate_removed_invalid_quantity_reported(self):
        stock_row = ["Товар", "шт", "001_", 1, 2, 999]
        stock, sales = self.sources([stock_row, stock_row], [["Товар", "001_", "#N/A", 3, 999]])
        report = clean_datasets(stock, sales, self.root / "out")
        self.assertEqual(report["output_rows"], 2)
        self.assertEqual(report["stock"]["exact_duplicates_removed"], 1)
        self.assertEqual(report["sales"]["invalid_quantity_cells"], 1)
        issues = pd.read_csv(self.root / "out/quality_issues.csv")
        self.assertIn("invalid_quantity_kept_as_missing", issues.issue.tolist())

    def test_conflicting_duplicate_fails_without_output(self):
        stock, sales = self.sources([
            ["Товар", "шт", "001_", 1, 2, 999],
            ["Товар", "шт", "001_", 9, 2, 999],
        ], [["Товар", "001_", 1, 2, 999]])
        with self.assertRaisesRegex(ValueError, "разные записи"):
            clean_datasets(stock, sales, self.root / "out")
        self.assertFalse((self.root / "out/monthly_clean.csv").exists())


if __name__ == "__main__":
    unittest.main()
