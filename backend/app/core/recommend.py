"""Оркестратор расчёта: связывает все Must-have и группирует заказ по поставщикам.

Поток по каждому артикулу:
  1) исключить разовые оптовые заказы (MH #4)
  2) построить дневной ряд и компенсировать stockout (MH #3)
  3) прогноз с сезонностью и трендом (MH #2)
  4) базовая потребность с учётом остатка/товаров в пути/партии (MH #1)
  5) сгруппировать по поставщикам + обоснование (MH #5)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pandas as pd

from app.config import get_settings
from app.core.explain import build_explanation
from app.core.forecasting import forecast_demand
from app.core.outliers import exclude_bulk_orders
from app.core.replenishment import compute_need
from app.core.stockout import build_adjusted_series
from app.data.adapter import Dataset
from app.schemas import (
    OrderLine,
    Rationale,
    RecommendationResponse,
    SupplierGroup,
)


def _supplier_map(ds: Dataset) -> dict:
    sup = ds.suppliers.set_index("supplier_id").to_dict("index")
    link = ds.sku_suppliers.set_index("sku").to_dict("index")
    return {"suppliers": sup, "link": link}


def generate_recommendations(
    ds: Dataset,
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    service_level: Optional[float] = None,
    review_period_days: Optional[int] = None,
    explain: bool = True,
) -> RecommendationResponse:
    settings = get_settings()
    service_level = service_level or settings.service_level
    review_period_days = review_period_days or settings.review_period_days

    sales = ds.sales.copy()
    sales["date"] = pd.to_datetime(sales["date"])
    if warehouse:
        sales = sales[sales["warehouse"] == warehouse]
    if category:
        sales = sales[sales["category"] == category]
    if sales.empty:
        return RecommendationResponse(
            generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            warehouse=warehouse, category=category,
            service_level=service_level, review_period_days=review_period_days,
            sku_count=0, groups=[],
        )

    start = sales["date"].min().date()
    end = date.today()
    smap = _supplier_map(ds)
    today = date.today()

    lines_by_supplier: dict[str, list[OrderLine]] = {}

    for sku, tx in sales.groupby("sku"):
        name = tx["name"].iloc[0]
        cat = tx["category"].iloc[0]

        # MH #4 — исключаем разовые оптовые заказы
        outl = exclude_bulk_orders(tx)

        # MH #3 — дневной ряд + компенсация stockout
        so = ds.stockouts[ds.stockouts["sku"] == sku] if not ds.stockouts.empty else ds.stockouts
        adj = build_adjusted_series(outl.regular, so, start, end)

        # MH #2 — прогноз с сезонностью и трендом
        fc = forecast_demand(adj.daily, horizon_start=today, horizon_days=review_period_days + 1)

        # остатки и товары в пути
        st = ds.stock[ds.stock["sku"] == sku]
        if warehouse:
            st = st[st["warehouse"] == warehouse]
        on_hand = float(st["on_hand"].sum()) if not st.empty else 0.0

        it = ds.in_transit[ds.in_transit["sku"] == sku] if not ds.in_transit.empty else ds.in_transit
        if warehouse and not it.empty:
            it = it[it["warehouse"] == warehouse]
        in_transit = float(it["qty"].sum()) if not it.empty else 0.0

        # поставщик и его условия
        link = smap["link"].get(sku, {})
        supplier_id = link.get("supplier_id", "SUP-00")
        pack_size = float(link.get("pack_size", 1.0))
        sup = smap["suppliers"].get(supplier_id, {})
        supplier_name = sup.get("name", "—")
        lead_time = int(sup.get("lead_time_days", 14))
        min_oq = float(sup.get("min_order_qty", 0.0))

        # MH #1 — базовая потребность
        need = compute_need(
            fc, on_hand=on_hand, in_transit=in_transit,
            lead_time_days=lead_time, review_period_days=review_period_days,
            service_level=service_level, pack_size=pack_size, min_order_qty=min_oq,
        )

        if need.recommended_qty <= 0:
            continue  # пополнение не требуется

        rationale = Rationale(
            avg_daily_demand=fc.avg_daily_demand,
            seasonality_factor=fc.seasonality_factor,
            trend_factor=fc.trend_factor,
            horizon_days=need.horizon_days,
            forecast_demand=need.forecast_demand,
            safety_stock=need.safety_stock,
            on_hand=on_hand,
            in_transit=in_transit,
            lost_demand_uplift=round(adj.lost_demand_uplift, 2),
            excluded_bulk_units=outl.excluded_units,
            excluded_bulk_orders=outl.excluded_orders,
            raw_need=need.raw_need,
        )
        explanation = build_explanation(
            name, rationale, need.recommended_qty, need.urgency, use_llm=explain
        )

        line = OrderLine(
            sku=sku, name=name, category=cat,
            supplier_id=supplier_id, supplier_name=supplier_name,
            recommended_qty=need.recommended_qty, urgency=need.urgency,
            days_of_cover=need.days_of_cover, rationale=rationale,
            explanation=explanation,
        )
        lines_by_supplier.setdefault(supplier_id, []).append(line)

    # MH #5 — группировка по поставщикам
    groups: list[SupplierGroup] = []
    urgency_rank = {"high": 0, "medium": 1, "low": 2}
    for supplier_id, lines in lines_by_supplier.items():
        lines.sort(key=lambda ln: (urgency_rank[ln.urgency], -ln.recommended_qty))
        sup = smap["suppliers"].get(supplier_id, {})
        groups.append(SupplierGroup(
            supplier_id=supplier_id,
            supplier_name=sup.get("name", "—"),
            lead_time_days=int(sup.get("lead_time_days", 14)),
            total_units=round(sum(ln.recommended_qty for ln in lines), 2),
            lines=lines,
        ))
    groups.sort(key=lambda g: -g.total_units)

    return RecommendationResponse(
        generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        warehouse=warehouse, category=category,
        service_level=service_level, review_period_days=review_period_days,
        sku_count=sum(len(g.lines) for g in groups), groups=groups,
    )
