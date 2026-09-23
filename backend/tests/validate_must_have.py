"""Five behavioral Must-have checks with fixed dates and exact reference results.

Run: OPENAI_API_KEY='' ./.venv/bin/python -m tests.validate_must_have
These checks prove controlled calculation behavior, not real-world accuracy,
production readiness or acceptance by the organizer/company.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date

import pandas as pd

from tests.test_input_contract import calculate, only_line, reference_dataset, source_sensitivity_matrix


def main() -> None:
    results: list[tuple[str, bool, str]] = []
    matrix = source_sensitivity_matrix()
    results.append(("MH#1 матрица источников: объёмы, остаток, ETA, условия и фильтры",
                    all(ok for _, ok, _ in matrix),
                    "\n       ".join(f"{'PASS' if ok else 'FAIL'}: {name}: {detail}" for name, ok, detail in matrix)))

    # Same observations, isolated seasonality change: exact quantity, not just
    # presence of a factor. Growth uses a known linear daily rate in real units.
    seasonal = reference_dataset()
    before = only_line(seasonal)
    seasonal.seasonality = pd.DataFrame([
        dict(supplier_id="SUP", month=month, factor=2 if month == 6 else 1) for month in range(1, 13)
    ])
    after = only_line(seasonal)
    growing = reference_dataset()
    growing.as_of = date(2025, 4, 11)
    growing.monthly_sales = growing.monthly_sales.iloc[0:0]
    growing.sales = growing.sales.iloc[:100].copy()
    growing.sales["date"] = pd.date_range("2025-01-01", periods=100)
    growing.sales["qty"] = range(100, 200)
    trend = only_line(growing)
    ok = (before.recommended_qty == 70 and after.recommended_qty == 140
          and after.rationale.avg_daily_demand == 20
          and trend.rationale.avg_daily_demand == 203 and trend.recommended_qty == 1421)
    results.append(("MH#2 сезонность и рост меняют прогноз объяснимым образом", ok,
                    f"сезонный заказ {before.recommended_qty:g} → {after.recommended_qty:g} (70 → 140); "
                    f"линейный тренд: {trend.rationale.avg_daily_demand:g}/день, заказ {trend.recommended_qty:g} (203; 1421)"))

    # One reference data set, two source modes, same explicitly observed outage.
    monthly = reference_dataset()
    monthly.sales = monthly.sales[~monthly.sales.date.between("2026-05-01", "2026-05-20")]
    monthly.monthly_sales.loc[monthly.monthly_sales.month.eq(pd.Timestamp("2026-05-01")), "qty"] = 110
    monthly_before = only_line(monthly)
    daily = deepcopy(monthly)
    daily.monthly_sales = daily.monthly_sales.iloc[0:0]
    daily_before = only_line(daily)
    monthly.stockouts = pd.DataFrame([dict(sku="S1", warehouse="A", start="2026-05-01", end="2026-05-20")])
    daily.stockouts = monthly.stockouts.copy()
    monthly_after, daily_after = only_line(monthly), only_line(daily)
    ok = (monthly_before.recommended_qty == 40
          and monthly_after.recommended_qty == daily_after.recommended_qty == 70
          and daily_before.recommended_qty < daily_after.recommended_qty
          and monthly_after.rationale.lost_demand_uplift == daily_after.rationale.lost_demand_uplift == 200)
    results.append(("MH#3 stockout меняет количество в месячном и дневном режимах", ok,
                    f"месячный {monthly_before.recommended_qty:g} → {monthly_after.recommended_qty:g}; "
                    f"дневной {daily_before.recommended_qty:g} → {daily_after.recommended_qty:g}; "
                    f"компенсация {monthly_after.rationale.lost_demand_uplift:g}/{daily_after.rationale.lost_demand_uplift:g}, ожидается 200/200"))

    bulk = reference_dataset()
    bulk.sales["order_id"] = [f"D{i}" for i in range(len(bulk.sales))]
    extra = pd.DataFrame([dict(date=pd.Timestamp("2026-05-20"), sku="S1", name="Кабель", category="C",
                              qty=20., warehouse="A", client_id="BULK", order_id=f"B{i}") for i in range(10)])
    bulk.sales = pd.concat([bulk.sales, extra], ignore_index=True)
    bulk.monthly_sales.loc[bulk.monthly_sales.month.eq(pd.Timestamp("2026-05-01")), "qty"] += 200
    line = only_line(bulk)
    ok = (line.recommended_qty == 70 and line.rationale.excluded_bulk_units == 200
          and line.rationale.excluded_bulk_orders == 1)
    results.append(("MH#4 крупный клиент с 10 документами исключается один раз", ok,
                    f"заказ {line.recommended_qty:g} (70); исключено {line.rationale.excluded_bulk_units:g} (200), "
                    f"событий {line.rationale.excluded_bulk_orders} (1)"))

    grouped = reference_dataset()
    grouped.sales = pd.concat([grouped.sales, grouped.sales.assign(sku="S2", qty=20.)], ignore_index=True)
    grouped.monthly_sales = pd.concat([grouped.monthly_sales, grouped.monthly_sales.assign(sku="S2", qty=grouped.monthly_sales.qty * 2)], ignore_index=True)
    grouped.stock.loc[1] = dict(sku="S2", warehouse="A", on_hand=0.)
    grouped.catalog.loc[1] = dict(sku="S2", name="Второй кабель", category="C", unit="м", supplier_id="OTHER")
    grouped.suppliers.loc[1] = dict(supplier_id="OTHER", name="Other", lead_time_days=5, min_order_qty=0)
    grouped.sku_suppliers.loc[1] = dict(sku="S2", supplier_id="OTHER", pack_size=1.)
    response = calculate(grouped)
    groups = {group.supplier_id: group for group in response.groups}
    quantities = {line.sku: line.recommended_qty for group in response.groups for line in group.lines}
    explained = all(line.explanation.strip() and line.supplier_id == group.supplier_id
                    for group in response.groups for line in group.lines)
    ok = set(groups) == {"SUP", "OTHER"} and quantities == {"S1": 70, "S2": 140} and explained
    results.append(("MH#5 два поставщика, правильные строки и обоснования", ok,
                    f"групп {len(groups)} (2); количества {quantities}; все строки объяснены: {explained}"))

    print("\n=== Must-have: фиксированные поведенческие проверки ===")
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}")
    passed = sum(ok for _, ok, _ in results)
    print(f"\nИТОГ: {passed}/{len(results)} групп проверок пройдено. "
          "Это не измерение точности на реальных данных и не официальный допуск.")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
