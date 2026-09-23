"""Regressions for observed data boundaries and replenishment constraints."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.core.forecasting import Forecast, _trend_slope_per_day, forecast_demand, forecast_monthly_demand
from app.core.outliers import exclude_bulk_orders
from app.core.recommend import generate_recommendations
from app.core.replenishment import compute_need
from app.core.stockout import build_adjusted_series
from app.data.adapter import Dataset


def dataset(as_of=date(2026, 1, 1), warehouses=("A",)):
    rows = []
    for warehouse in warehouses:
        for day in pd.date_range(as_of - timedelta(days=100), as_of - timedelta(days=1)):
            rows.append(dict(date=day, sku="S1", name="Товар", category="C", qty=10.0,
                             warehouse=warehouse, client_id="client"))
    return Dataset(
        sales=pd.DataFrame(rows),
        stock=pd.DataFrame([dict(sku="S1", warehouse=w, on_hand=0.0) for w in warehouses]),
        in_transit=pd.DataFrame(columns=["sku", "warehouse", "qty", "eta"]),
        suppliers=pd.DataFrame([dict(supplier_id="SUP", name="Supplier", lead_time_days=5, min_order_qty=0)]),
        sku_suppliers=pd.DataFrame([dict(sku="S1", supplier_id="SUP", pack_size=1.0)]),
        stockouts=pd.DataFrame(columns=["sku", "warehouse", "start", "end"]),
        as_of=as_of,
    )


def lines(ds, **kwargs):
    result = generate_recommendations(ds, explain=False, service_level=0.5, review_period_days=2, **kwargs)
    return {line.warehouse: line for group in result.groups for line in group.lines}


class CalculationTests(unittest.TestCase):
    def test_missing_unit_is_explicit_without_changing_known_units(self):
        for unit in ("", "  ", None, "м", "ед."):
            ds = dataset()
            ds.catalog = pd.DataFrame([dict(sku="S1", name="Товар", category="C", unit=unit)])
            line = lines(ds)["A"]
            if unit in ("м", "ед."):
                self.assertEqual(line.unit, unit)
                self.assertFalse(any("Единица измерения не указана" in warning for warning in line.warnings))
            else:
                self.assertEqual(line.unit, "ед. (не указана)")
                self.assertTrue(any("Единица измерения не указана" in warning for warning in line.warnings))

    def test_intraday_sales_are_grouped_and_today_is_excluded(self):
        ds = dataset()
        ds.sales["date"] += pd.Timedelta(hours=14)
        original = lines(ds)["A"]
        self.assertAlmostEqual(original.rationale.avg_daily_demand, 10)
        future = ds.sales.iloc[0].copy()
        future["date"], future["qty"] = pd.Timestamp(ds.as_of) + pd.Timedelta(hours=10), 1000000
        ds.sales = pd.concat([ds.sales, pd.DataFrame([future])], ignore_index=True)
        self.assertEqual(lines(ds)["A"].recommended_qty, original.recommended_qty)

    def test_warehouses_and_stockout_periods_are_independent(self):
        ds = dataset(warehouses=("A", "B"))
        start, end = ds.as_of - timedelta(days=20), ds.as_of - timedelta(days=15)
        dates = ds.sales["date"]
        ds.sales = ds.sales[~((ds.sales.warehouse == "A") & dates.between(pd.Timestamp(start), pd.Timestamp(end)))]
        ds.stockouts = pd.DataFrame([dict(sku="S1", warehouse="A", start=start, end=end)])
        result = lines(ds)
        self.assertEqual(set(result), {"A", "B"})
        self.assertGreater(result["A"].rationale.lost_demand_uplift, 0)
        self.assertEqual(result["B"].rationale.lost_demand_uplift, 0)
        self.assertEqual(result["B"].rationale.avg_daily_demand, 10)
        self.assertEqual(result["B"].recommended_qty, lines(ds, warehouse="B")["B"].recommended_qty)
        self.assertNotEqual(result["A"].line_id, result["B"].line_id)

    def test_eta_boundary_overdue_and_missing_supply(self):
        ds = dataset()
        ds.in_transit = pd.DataFrame([
            dict(sku="S1", warehouse="A", qty=20, eta=ds.as_of + timedelta(days=6)),
            dict(sku="S1", warehouse="A", qty=100, eta=ds.as_of + timedelta(days=7)),
            dict(sku="S1", warehouse="A", qty=100, eta=ds.as_of - timedelta(days=1)),
            dict(sku="S1", warehouse="A", qty=100, eta=None),
        ])
        line = lines(ds)["A"]
        self.assertEqual(line.rationale.horizon_days, 7)
        self.assertEqual(line.rationale.forecast_demand, 70)
        self.assertEqual(line.rationale.in_transit, 20)
        self.assertEqual(line.rationale.ignored_in_transit, 300)
        self.assertEqual(line.recommended_qty, 50)
        self.assertEqual(line.urgency, "high")
        self.assertEqual(line.days_of_cover, 0)

    def test_late_large_receipt_does_not_hide_near_shortage(self):
        ds = dataset()
        ds.stock["on_hand"] = 10
        ds.in_transit = pd.DataFrame([dict(sku="S1", warehouse="A", qty=1000,
                                          eta=ds.as_of + timedelta(days=4))])
        line = lines(ds)["A"]
        self.assertEqual(line.recommended_qty, 0)
        self.assertEqual(line.urgency, "high")
        self.assertEqual(line.days_of_cover, 1)
        self.assertTrue(any("Ускорьте" in warning for warning in line.warnings))

    def test_receipt_today_can_cover_demand(self):
        ds = dataset()
        ds.in_transit = pd.DataFrame([dict(sku="S1", warehouse="A", qty=1000, eta=ds.as_of)])
        self.assertEqual(lines(ds), {})

    def test_minimum_then_fractional_pack(self):
        fc = Forecast(1.0, 0.0, 1.0, 1.0, 1.0)
        need = compute_need(fc, 0, 0, 1, 1, 0.5, pack_size=0.25, min_order_qty=2.6)
        self.assertEqual(need.recommended_qty, 2.75)
        whole = compute_need(fc, 0, 0, 1, 1, 0.5, pack_size=10, min_order_qty=25)
        self.assertEqual(whole.recommended_qty, 30)

    def test_sku_minimum_overrides_supplier_default(self):
        ds = dataset()
        ds.suppliers["min_order_qty"] = 500
        ds.sku_suppliers["min_order_qty"] = 25
        ds.sku_suppliers["pack_size"] = 10
        self.assertEqual(lines(ds)["A"].recommended_qty, 70)
        ds.sku_suppliers["min_order_qty"] = 75
        self.assertEqual(lines(ds)["A"].recommended_qty, 80)

    def test_stockout_preserves_observed_demand_and_measures_delta(self):
        days = pd.date_range("2026-01-01", "2026-01-31")
        qty = [10 if day.weekday() < 5 else 0 for day in days]
        tx = pd.DataFrame(dict(date=days, qty=qty))
        tx.loc[tx.date == pd.Timestamp("2026-01-12"), "qty"] = 20
        tx.loc[tx.date == pd.Timestamp("2026-01-13"), "qty"] = 4
        so = pd.DataFrame([dict(start="2026-01-12", end="2026-01-13"),
                           dict(start="2026-01-17", end="2026-01-18")])
        result = build_adjusted_series(tx, so, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(result.daily.loc["2026-01-12"], 20)
        self.assertEqual(result.daily.loc["2026-01-13"], 10)
        self.assertEqual(result.daily.loc["2026-01-17"], 0)
        self.assertEqual(result.lost_demand_uplift, 6)

    def test_short_history_does_not_delete_sole_sale(self):
        for quantities in ([300], [10, 500], [10, 10, 10000]):
            tx = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=len(quantities)), "qty": quantities})
            result = exclude_bulk_orders(tx)
            self.assertEqual(result.excluded_orders, 0)
            self.assertEqual(len(result.regular), len(tx))

    def test_client_day_and_document_grouping(self):
        rows = [dict(date=pd.Timestamp("2026-01-01") + pd.Timedelta(days=i), qty=10, client_id="C")
                for i in range(10)]
        rows += [dict(date=pd.Timestamp("2026-01-20") + pd.Timedelta(hours=i), qty=20, client_id="B")
                 for i in range(10)]
        result = exclude_bulk_orders(pd.DataFrame(rows))
        self.assertEqual(result.excluded_orders, 1)
        self.assertEqual(result.excluded_units, 200)
        doc = pd.DataFrame(rows).drop(columns="client_id")
        doc["document_id"] = [f"R{i}" for i in range(10)] + ["BULK"] * 10
        self.assertEqual(exclude_bulk_orders(doc).excluded_units, 200)

    def test_trend_has_daily_rate_units(self):
        daily = pd.Series(np.arange(100, dtype=float) + 100, index=pd.date_range("2025-01-01", periods=100))
        self.assertAlmostEqual(_trend_slope_per_day(daily), 1.0, places=7)
        fc = forecast_demand(daily, date(2025, 4, 11), 7)
        self.assertAlmostEqual(fc.avg_daily_demand, 203, places=3)

    def test_forecast_uses_full_lead_time_plus_review_horizon(self):
        ds = dataset(as_of=date(2026, 1, 28))
        ds.suppliers["lead_time_days"] = 10
        ds.seasonality = pd.DataFrame([dict(supplier_id="SUP", month=month, factor=2 if month == 2 else 1)
                                       for month in range(1, 13)])
        line = lines(ds)["A"]
        fc = forecast_demand(pd.Series(10.0, index=pd.date_range("2025-10-20", "2026-01-27")),
                             ds.as_of, 12, {month: 2 if month == 2 else 1 for month in range(1, 13)})
        self.assertEqual(line.rationale.horizon_days, 12)
        self.assertAlmostEqual(line.rationale.forecast_demand, fc.avg_daily_demand * 12, places=2)
        self.assertGreater(line.rationale.seasonality_factor, 1.0)

    def test_authoritative_monthly_totals_not_added_to_transactions(self):
        ds = dataset(as_of=date(2026, 6, 1))
        months = pd.date_range("2026-01-01", periods=5, freq="MS")
        ds.monthly_sales = pd.DataFrame([dict(sku="S1", warehouse="A", month=month,
                                             qty=30 * month.days_in_month) for month in months])
        first = lines(ds)["A"]
        ds.sales["qty"] = 999  # no outlier; transaction magnitudes must not add to monthly totals
        second = lines(ds)["A"]
        self.assertEqual(first.recommended_qty, second.recommended_qty)
        self.assertAlmostEqual(second.rationale.avg_daily_demand, 30)
        self.assertTrue(any("Пуассона" in warning for warning in second.warnings))
        ds.monthly_sales["qty"] *= 2
        self.assertGreater(lines(ds)["A"].recommended_qty, first.recommended_qty)

    def test_supplier_seasonality_changes_monthly_forecast(self):
        months = pd.date_range("2026-01-01", periods=5, freq="MS")
        quantities = pd.Series(10 * months.days_in_month, index=months)
        plain = forecast_monthly_demand(quantities, date(2026, 6, 1), 10)
        seasonal = forecast_monthly_demand(quantities, date(2026, 6, 1), 10,
                                           {month: 2 if month == 6 else 1 for month in range(1, 13)})
        self.assertAlmostEqual(plain.avg_daily_demand, 10)
        self.assertAlmostEqual(seasonal.avg_daily_demand, 20)

    def test_partial_month_and_future_month_do_not_enter_history(self):
        ds = dataset(as_of=date(2026, 6, 13))
        months = pd.date_range("2026-01-01", periods=5, freq="MS")
        ds.monthly_sales = pd.DataFrame([dict(sku="S1", warehouse="A", month=month,
                                             qty=30 * month.days_in_month) for month in months])
        original = lines(ds)["A"]
        ds.monthly_sales = pd.concat([ds.monthly_sales, pd.DataFrame([
            dict(sku="S1", warehouse="A", month=pd.Timestamp("2026-06-01"), qty=1000000),
            dict(sku="S1", warehouse="A", month=pd.Timestamp("2026-07-01"), qty=1000000),
        ])], ignore_index=True)
        self.assertEqual(lines(ds)["A"].recommended_qty, original.recommended_qty)

    def test_detected_bulk_subtracted_once_from_monthly_total(self):
        ds = dataset(as_of=date(2026, 6, 1))
        ds.sales = ds.sales.iloc[:10].copy()
        ds.sales["date"] = pd.date_range("2026-05-01", periods=10)
        ds.sales["client_id"] = [f"C{i}" for i in range(10)]
        ds.sales.loc[ds.sales.index[-1], "qty"] = 600
        ds.monthly_sales = pd.DataFrame([dict(sku="S1", warehouse="A", month=pd.Timestamp("2026-05-01"), qty=690)])
        line = lines(ds)["A"]
        self.assertEqual(line.rationale.excluded_bulk_units, 600)
        self.assertEqual(line.rationale.excluded_bulk_orders, 1)
        self.assertAlmostEqual(line.rationale.avg_daily_demand, 90 / 31, places=3)
        self.assertEqual(line.rationale.lost_demand_uplift, 0)

    def test_incompatible_bulk_report_cannot_erase_authoritative_monthly_demand(self):
        ds = dataset(as_of=date(2026, 6, 1))
        ds.sales = ds.sales.iloc[:10].copy()
        ds.sales["date"] = pd.date_range("2026-05-01", periods=10)
        ds.sales["client_id"] = [f"C{i}" for i in range(10)]
        ds.sales.loc[ds.sales.index[-1], "qty"] = 6000
        ds.monthly_sales = pd.DataFrame([dict(sku="S1", warehouse="A", month=pd.Timestamp("2026-05-01"), qty=900)])
        line = lines(ds)["A"]
        self.assertEqual(line.rationale.excluded_bulk_units, 0)
        self.assertEqual(line.rationale.excluded_bulk_orders, 0)
        self.assertAlmostEqual(line.rationale.avg_daily_demand, 900 / 31, places=3)
        self.assertTrue(any("расходятся" in warning for warning in line.warnings))

    def test_future_calculation_does_not_complete_a_partial_source_month(self):
        ds = dataset(as_of=date(2026, 7, 1))
        ds.metadata["transaction_end"] = "2026-06-12"
        ds.monthly_sales = pd.DataFrame([
            dict(sku="S1", warehouse="A", month=pd.Timestamp("2026-05-01"), qty=310),
            dict(sku="S1", warehouse="A", month=pd.Timestamp("2026-06-01"), qty=90000),
        ])
        line = lines(ds)["A"]
        self.assertEqual(line.rationale.avg_daily_demand, 10)

    def test_future_transit_snapshot_does_not_leak_into_historical_calculation(self):
        ds = dataset()
        ds.in_transit = pd.DataFrame([dict(sku="S1", warehouse="A", qty=1000,
                                          eta=ds.as_of, source_as_of=ds.as_of + timedelta(days=1))])
        line = lines(ds)["A"]
        self.assertEqual(line.rationale.in_transit, 0)
        self.assertEqual(line.rationale.ignored_in_transit, 1000)
        self.assertEqual(line.recommended_qty, 70)

    def test_explanation_uses_catalog_unit(self):
        ds = dataset()
        ds.catalog = pd.DataFrame([dict(sku="S1", name="Кабель", category="C", unit="м", supplier_id="SUP")])
        line = lines(ds)["A"]
        self.assertEqual(line.unit, "м")
        self.assertIn("заказать 70 м", line.explanation)
        self.assertNotIn("ед.", line.explanation)

    def test_stock_dates_and_source_metadata_are_exposed(self):
        ds = dataset()
        ds.stock["as_of"] = date(2025, 12, 1)
        ds.source, ds.metadata = "excel", {"example": "provenance"}
        result = generate_recommendations(ds, explain=False)
        line = result.groups[0].lines[0]
        self.assertEqual(result.as_of, ds.as_of)
        self.assertEqual(result.data_source, "excel")
        self.assertEqual(result.data_quality["example"], "provenance")
        self.assertEqual(line.rationale.stock_as_of, date(2025, 12, 1))
        self.assertTrue(any("устарел" in warning for warning in line.warnings))


if __name__ == "__main__":
    unittest.main()
