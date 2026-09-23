"""Fixed-date source sensitivity matrix, shared by unittest and Must-have report.

These are controlled behavioral checks, not a claim of measured forecast accuracy
or production data completeness. Category is a filter; growth comes from history.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import unittest

import pandas as pd

from app.core.recommend import generate_recommendations
from app.data.adapter import Dataset


def reference_dataset() -> Dataset:
    days = pd.date_range("2026-01-01", "2026-05-31")
    sales = pd.DataFrame(dict(date=days, sku="S1", name="Кабель", category="C",
                              qty=10., warehouse="A", client_id="C1"))
    monthly = (sales.assign(month=sales.date.dt.to_period("M").dt.to_timestamp())
               .groupby(["sku", "warehouse", "month"], as_index=False)["qty"].sum())
    return Dataset(
        sales=sales, monthly_sales=monthly,
        stock=pd.DataFrame([dict(sku="S1", warehouse="A", on_hand=0.)]),
        in_transit=pd.DataFrame(columns=["sku", "warehouse", "qty", "eta"]),
        suppliers=pd.DataFrame([dict(supplier_id="SUP", name="Supplier", lead_time_days=5, min_order_qty=0)]),
        sku_suppliers=pd.DataFrame([dict(sku="S1", supplier_id="SUP", pack_size=1.)]),
        catalog=pd.DataFrame([dict(sku="S1", name="Кабель", category="C", unit="м", supplier_id="SUP")]),
        stockouts=pd.DataFrame(columns=["sku", "warehouse", "start", "end"]),
        as_of=date(2026, 6, 1),
    )


def calculate(ds: Dataset, **kwargs):
    return generate_recommendations(ds, explain=False, service_level=0.5, review_period_days=2, **kwargs)


def only_line(ds: Dataset):
    response = calculate(ds)
    return next(line for group in response.groups for line in group.lines)


def source_sensitivity_matrix() -> list[tuple[str, bool, str]]:
    base = reference_dataset()
    original = only_line(base).recommended_qty
    checks = [("эталон: 10/день × 7 дней", original == 70, f"заказ {original:g}, ожидается 70 м")]

    def quantity_case(name, changed, expected):
        actual = only_line(changed).recommended_qty
        checks.append((name, actual == expected, f"70 → {actual:g}, ожидается {expected:g} м"))

    changed = deepcopy(base)
    changed.monthly_sales["qty"] *= 2
    quantity_case("основные месячные продажи ×2", changed, 140)
    changed = deepcopy(base)
    changed.sales["qty"] *= 2
    quantity_case("транзакции не прибавляются к месячному авторитету", changed, 70)
    changed.monthly_sales = changed.monthly_sales.iloc[0:0]
    quantity_case("дневные продажи ×2 при отсутствии месячных", changed, 140)
    changed = deepcopy(base)
    changed.stock["on_hand"] = 20
    quantity_case("текущий остаток +20", changed, 50)
    changed = deepcopy(base)
    changed.in_transit = pd.DataFrame([dict(sku="S1", warehouse="A", qty=20, eta="2026-06-01")])
    quantity_case("товар в пути +20 внутри горизонта", changed, 50)
    changed.in_transit["eta"] = "2026-06-08"
    quantity_case("ETA на исключённой границе горизонта", changed, 70)
    changed = deepcopy(base)
    changed.suppliers["lead_time_days"] = 12
    quantity_case("срок поставщика 5 → 12 дней", changed, 140)
    changed = deepcopy(base)
    changed.suppliers.loc[1] = dict(supplier_id="OTHER", name="Other", lead_time_days=12, min_order_qty=0)
    changed.sku_suppliers["supplier_id"] = "OTHER"
    quantity_case("связь SKU выбирает условия другого поставщика", changed, 140)
    changed = deepcopy(base)
    changed.sku_suppliers["pack_size"] = 16
    quantity_case("кратность упаковки 16", changed, 80)
    changed = deepcopy(base)
    changed.sku_suppliers["min_order_qty"] = 75
    quantity_case("MOQ товара 75", changed, 75)
    changed = deepcopy(base)
    changed.suppliers["min_order_qty"] = 90
    quantity_case("MOQ поставщика при отсутствии MOQ товара", changed, 90)
    selected, excluded = calculate(base, category="C"), calculate(base, category="other")
    checks.append(("категория — фильтр, не множитель потребности",
                   selected.sku_count == 1 and selected.groups[0].lines[0].recommended_qty == 70 and excluded.sku_count == 0,
                   f"категория C: {selected.sku_count} строк; other: {excluded.sku_count} строк"))
    changed = deepcopy(base)
    changed.monthly_stock = pd.DataFrame([dict(sku="S1", warehouse="A", month="2026-05-01", on_hand=0.)])
    line = only_line(changed)
    checks.append(("месячный нулевой остаток не выдумывает дни stockout",
                   line.recommended_qty == 70 and line.rationale.lost_demand_uplift == 0,
                   f"заказ {line.recommended_qty:g}, компенсация {line.rationale.lost_demand_uplift:g}"))
    return checks


class InputContractTests(unittest.TestCase):
    def test_source_sensitivity_matrix(self):
        for name, passed, detail in source_sensitivity_matrix():
            with self.subTest(source=name):
                self.assertTrue(passed, detail)

    def test_seasonality_changes_same_history_forecast_by_expected_amount(self):
        base = reference_dataset()
        original = only_line(base)
        base.seasonality = pd.DataFrame([dict(supplier_id="SUP", month=month, factor=2 if month == 6 else 1)
                                         for month in range(1, 13)])
        seasonal = only_line(base)
        self.assertEqual(original.recommended_qty, 70)
        self.assertEqual(seasonal.recommended_qty, 140)
        self.assertEqual(seasonal.rationale.avg_daily_demand, 20)

    def test_growth_is_measured_from_history_in_daily_rate_units(self):
        ds = reference_dataset()
        ds.as_of = date(2025, 4, 11)
        ds.monthly_sales = ds.monthly_sales.iloc[0:0]
        ds.sales = ds.sales.iloc[:100].copy()
        ds.sales["date"] = pd.date_range("2025-01-01", periods=100)
        ds.sales["qty"] = range(100, 200)
        line = only_line(ds)
        self.assertAlmostEqual(line.rationale.avg_daily_demand, 203, places=3)
        self.assertEqual(line.recommended_qty, 1421)
        self.assertGreater(line.rationale.trend_factor, 1)

    def test_bulk_and_stockout_are_each_applied_once_to_monthly_authority(self):
        ds = reference_dataset()
        ds.sales = ds.sales[~ds.sales.date.between("2026-05-01", "2026-05-20")]
        bulk = ds.sales.iloc[-1].copy()
        bulk["qty"], bulk["client_id"] = 1000., "BULK"
        ds.sales = pd.concat([ds.sales, pd.DataFrame([bulk])], ignore_index=True)
        ds.monthly_sales.loc[ds.monthly_sales.month.eq(pd.Timestamp("2026-05-01")), "qty"] = 1110
        ds.stockouts = pd.DataFrame([dict(sku="S1", warehouse="A", start="2026-05-01", end="2026-05-20")])
        line = only_line(ds)
        self.assertEqual(line.rationale.excluded_bulk_units, 1000)
        self.assertEqual(line.rationale.lost_demand_uplift, 200)
        self.assertEqual(line.recommended_qty, 70)


if __name__ == "__main__":
    unittest.main()
