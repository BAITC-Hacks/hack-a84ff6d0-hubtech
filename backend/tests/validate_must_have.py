"""Проверка 5 Must-have кейса ЭКТ на синтетических данных.

Запуск:  ./.venv/bin/python -m tests.validate_must_have
Скрипт печатает PASS/FAIL по каждому обязательному требованию.
"""
from __future__ import annotations

import copy

import pandas as pd

from app.core.recommend import generate_recommendations
from app.data.adapter import get_data_source


def _line_index(resp):
    """sku -> OrderLine по всему ответу."""
    out = {}
    for g in resp.groups:
        for ln in g.lines:
            out[ln.sku] = ln
    return out


def main() -> None:
    ds = get_data_source().load()
    base = generate_recommendations(ds, explain=False)
    base_lines = _line_index(base)
    results: list[tuple[str, bool, str]] = []

    # ---- MH #1: изменение источника (товары в пути) меняет результат ----
    target = next(iter(base_lines))
    ds2 = copy.deepcopy(ds)
    extra = base_lines[target].rationale.forecast_demand  # заведомо ощутимый объём
    new_row = pd.DataFrame([{
        "sku": target, "warehouse": ds.stock[ds.stock.sku == target]["warehouse"].iloc[0],
        "qty": extra, "eta": pd.Timestamp.today().date(),
    }])
    ds2.in_transit = pd.concat([ds2.in_transit, new_row], ignore_index=True)
    resp2 = _line_index(generate_recommendations(ds2, explain=False))
    before = base_lines[target].recommended_qty
    after = resp2[target].recommended_qty if target in resp2 else 0.0
    ok1 = after < before
    results.append(("MH#1 учёт всех источников (товары в пути ↓ потребность)",
                    ok1, f"{target}: было {before:g} → стало {after:g}"))

    # ---- MH #2: сезонность отражается в прогнозе (не просто среднее) ----
    seasonal = [ln for ln in base_lines.values() if abs(ln.rationale.seasonality_factor - 1.0) >= 0.05]
    ok2 = len(seasonal) > 0
    ex = seasonal[0] if seasonal else None
    results.append(("MH#2 учёт сезонности",
                    ok2, f"{ex.sku} сезонность ×{ex.rationale.seasonality_factor:g}" if ex else "нет сезонных позиций"))

    # ---- MH #3: компенсация упущенного спроса при stockout ----
    uplifted = [ln for ln in base_lines.values() if ln.rationale.lost_demand_uplift > 0]
    ok3 = len(uplifted) > 0
    ex3 = uplifted[0] if uplifted else None
    results.append(("MH#3 компенсация упущенного спроса (stockout)",
                    ok3, f"{ex3.sku} +{ex3.rationale.lost_demand_uplift:g} ед." if ex3 else "нет позиций со stockout"))

    # ---- MH #4: разовый крупный заказ не раздувает регулярную потребность ----
    ds3 = copy.deepcopy(ds)
    sku4 = target
    row = ds.sales[ds.sales.sku == sku4].iloc[0]
    huge = pd.DataFrame([{
        "date": row["date"], "sku": sku4, "name": row["name"], "category": row["category"],
        "qty": base_lines[sku4].rationale.avg_daily_demand * 400,  # гигантский разовый опт
        "price": row["price"], "client_id": "CL-BULK-TEST", "warehouse": row["warehouse"],
    }])
    ds3.sales = pd.concat([ds3.sales, huge], ignore_index=True)
    resp3 = _line_index(generate_recommendations(ds3, explain=False))
    before4 = base_lines[sku4].recommended_qty
    after4 = resp3[sku4].recommended_qty if sku4 in resp3 else before4
    excluded = resp3[sku4].rationale.excluded_bulk_orders if sku4 in resp3 else 0
    # регулярная потребность не должна существенно вырасти (допуск 15%)
    ok4 = (after4 <= before4 * 1.15) and (excluded >= 1)
    results.append(("MH#4 исключение разовых крупных заказов",
                    ok4, f"{sku4}: без опта {before4:g}, с оптом {after4:g}, исключено заказов: {excluded}"))

    # ---- MH #5: группировка по поставщикам + обоснование по каждой строке ----
    has_groups = len(base.groups) > 0
    every_line_explained = all(
        ln.explanation.strip() for g in base.groups for ln in g.lines
    )
    ok5 = has_groups and every_line_explained
    results.append(("MH#5 группировка по поставщикам + обоснование",
                    ok5, f"{len(base.groups)} поставщиков, {base.sku_count} строк, все с обоснованием: {every_line_explained}"))

    # ---- отчёт ----
    print("\n=== Проверка Must-have (кейс ЭКТ) ===")
    all_ok = True
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"[{mark}] {name}\n       {detail}")
    print("\nИТОГ:", "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ✅" if all_ok else "ЕСТЬ ПРОВАЛЫ ❌")
    if not all_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
