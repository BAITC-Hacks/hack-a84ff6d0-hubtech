"""Calculate replenishment independently for each SKU and warehouse.

Historical daily observations stop on as_of - 1 day. Monthly matrices are the
quantity authority when present; transactions only identify exceptional orders.
Supplier lead time + review period defines the same half-open forecast and ETA
window [as_of, as_of + horizon_days) throughout the calculation.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import numpy as np
import pandas as pd

from app.config import get_settings
from app.core.explain import build_explanation
from app.core.forecasting import forecast_demand, forecast_monthly_demand
from app.core.outliers import exclude_bulk_orders
from app.core.replenishment import compute_need
from app.core.stockout import build_adjusted_series
from app.data.adapter import Dataset
from app.schemas import OrderLine, Rationale, RecommendationResponse, SupplierGroup


def _supplier_map(ds: Dataset) -> dict:
    return {
        "suppliers": ds.suppliers.drop_duplicates("supplier_id").set_index("supplier_id").to_dict("index"),
        "link": ds.sku_suppliers.drop_duplicates("sku").set_index("sku").to_dict("index"),
    }


def _groups(frame: pd.DataFrame) -> dict:
    if frame.empty or not {"sku", "warehouse"}.issubset(frame.columns):
        return {}
    return dict(tuple(frame.groupby(["sku", "warehouse"], sort=False, observed=True)))


def _number(value, fallback=0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else fallback
    except (TypeError, ValueError):
        return fallback


def _clean_text(value, fallback="") -> str:
    return str(value) if pd.notna(value) else fallback


def generate_recommendations(
    ds: Dataset, warehouse: Optional[str] = None, category: Optional[str] = None,
    service_level: Optional[float] = None, review_period_days: Optional[int] = None,
    explain: bool = False,
) -> RecommendationResponse:
    settings = get_settings()
    service_level = settings.service_level if service_level is None else service_level
    review_period_days = settings.review_period_days if review_period_days is None else review_period_days
    today = getattr(ds, "as_of", None) or date.today()
    today = pd.Timestamp(today).date()
    history_end = today - timedelta(days=1)
    source = getattr(ds, "source", "synthetic")
    metadata = getattr(ds, "metadata", {})
    source_end = pd.to_datetime(metadata.get("transaction_end"), errors="coerce")
    if source_end is not None and not pd.isna(source_end):
        history_end = min(history_end, source_end.date())
    complete_month_cutoff = pd.Timestamp(history_end + timedelta(days=1)).replace(day=1)
    response_warnings = list(getattr(ds, "warnings", []))
    if (today - history_end).days > 1:
        response_warnings.append(f"Последний подтверждённый день истории: {history_end.isoformat()}. Период после него не считается нулевыми продажами.")
    smap = _supplier_map(ds)
    empty = pd.DataFrame()

    sales = ds.sales.copy()
    if not sales.empty:
        sales["date"] = pd.to_datetime(sales["date"]).dt.normalize()
        sales = sales[sales["date"] < pd.Timestamp(today)]
    monthly = getattr(ds, "monthly_sales", empty).copy()
    if not monthly.empty:
        monthly["month"] = pd.to_datetime(monthly["month"]).dt.to_period("M").dt.to_timestamp()
        # An unfinished month's total is not comparable with a full month's rate.
        monthly = monthly[monthly["month"] < complete_month_cutoff]
    transit = ds.in_transit.copy()
    if not transit.empty:
        transit["eta"] = pd.to_datetime(transit["eta"], errors="coerce").dt.normalize()

    catalog_frame = getattr(ds, "catalog", empty)
    catalog = (catalog_frame.drop_duplicates("sku").set_index("sku").to_dict("index")
               if not catalog_frame.empty else {})
    # Group once: scanning a 250k-row transaction table per SKU is quadratic.
    sales_groups, monthly_groups = _groups(sales), _groups(monthly)
    stock_groups, transit_groups = _groups(ds.stock), _groups(transit)
    stockout_groups = _groups(ds.stockouts)
    keys = set(sales_groups) | set(monthly_groups) | set(stock_groups) | set(transit_groups)
    seasonality = getattr(ds, "seasonality", empty)
    seasonal_by_supplier = {
        supplier: group.set_index("month")["factor"].to_dict()
        for supplier, group in seasonality.groupby("supplier_id", sort=False)
    } if not seasonality.empty else {}
    lines_by_supplier: dict[str, list[OrderLine]] = {}
    reconciliation = {"matched_sku_months": 0, "mismatched_sku_months": 0,
                      "bulk_units_not_deducted_due_to_mismatch": 0.0}

    for sku, wh in sorted(keys, key=lambda key: (str(key[0]), str(key[1]))):
        if warehouse and wh != warehouse:
            continue
        tx = sales_groups.get((sku, wh), empty)
        info = catalog.get(sku, {})
        tx_info = tx.iloc[0] if not tx.empty else {}
        name = _clean_text(info.get("name", tx_info.get("name", sku)), str(sku))
        cat = _clean_text(info.get("category", tx_info.get("category", "Без категории")), "Без категории")
        if category and cat != category:
            continue
        warnings: list[str] = []
        link = smap["link"].get(sku, {})
        supplier_id = link.get("supplier_id", info.get("supplier_id", "SUP-00"))
        sup = smap["suppliers"].get(supplier_id, {})
        lead_time = max(0, int(_number(sup.get("lead_time_days"), 14)))
        horizon_days = max(1, lead_time + review_period_days)
        horizon_end = pd.Timestamp(today) + pd.Timedelta(days=horizon_days)
        pack_size = _number(link.get("pack_size"), 1.0)
        if pack_size <= 0:
            pack_size = 1.0
        min_oq = max(0.0, _number(link.get("min_order_qty"), _number(sup.get("min_order_qty"))))
        factors = seasonal_by_supplier.get(supplier_id)
        outl = exclude_bulk_orders(tx)
        uplift = 0.0
        excluded_units, excluded_orders = outl.excluded_units, outl.excluded_orders
        month_rows = monthly_groups.get((sku, wh), empty)
        if not month_rows.empty:
            month_qty = month_rows.groupby("month")["qty"].sum().clip(lower=0.0)
            excluded_units, excluded_orders = 0.0, 0
            # A document can only be deducted from a monthly total after proving
            # both reports represent the same volume. Clamping an incompatible
            # bulk amount to the monthly total would silently erase real demand.
            matched = pd.Series(False, index=month_qty.index)
            if not tx.empty:
                tx_month = tx["qty"].clip(lower=0.0).groupby(tx["date"].dt.to_period("M").dt.to_timestamp()).sum()
                overlap = month_qty.index.intersection(tx_month.index)
                matched.loc[overlap] = np.isclose(month_qty.loc[overlap], tx_month.loc[overlap], rtol=1e-8, atol=1e-6)
                mismatch_count = int((~matched.loc[overlap]).sum())
                reconciliation["matched_sku_months"] += int(matched.sum())
                reconciliation["mismatched_sku_months"] += mismatch_count
                if mismatch_count:
                    warnings.append(f"Суммы транзакций и месячного отчёта расходятся в {mismatch_count} мес. Использованы месячные итоги; крупные заказы в несогласованных месяцах не вычитались.")
            if not outl.excluded.empty:
                excluded = outl.excluded.copy()
                excluded["month"] = excluded["date"].dt.to_period("M").dt.to_timestamp()
                bulk_month = excluded.groupby("month")["qty"].sum().reindex(month_qty.index, fill_value=0.0)
                deduction = bulk_month.where(matched, 0.0).clip(upper=month_qty)
                reconciliation["bulk_units_not_deducted_due_to_mismatch"] += float(bulk_month.where(~matched, 0.0).sum())
                month_qty = (month_qty - deduction).clip(lower=0.0)
                excluded_units = float(deduction.sum())
                effective = excluded[excluded["month"].isin(deduction[deduction > 0].index)]
                excluded_orders = int(effective["_bulk_order_key"].nunique())
            fc = forecast_monthly_demand(month_qty, today, horizon_days, factors)
            warnings.append("Спрос рассчитан по завершённым месяцам; помесячные итоги — основной источник объёма, транзакции используются для поиска крупных заказов.")
            warnings.append("Дневная вариативность из месячных итогов неизвестна: страховой запас использует оценку Пуассона и разброс месячных среднесуточных значений.")
            last_month_end = (month_qty.index.max() + pd.offsets.MonthEnd(0)).date()
            if (history_end - last_month_end).days > 31:
                warnings.append(f"История продаж заканчивается {last_month_end.isoformat()}; прогноз использует устаревший период.")
        elif not tx.empty:
            start = tx["date"].min().date()
            adj = build_adjusted_series(outl.regular, stockout_groups.get((sku, wh), empty), start, history_end)
            uplift = adj.lost_demand_uplift
            fc = forecast_demand(adj.daily, today, horizon_days, factors)
            if source == "excel":
                warnings.append("Помесячная история отсутствует: прогноз основан на доступных транзакциях, полнота истории не подтверждена.")
        else:
            # Catalog/stock-only positions carry no observed demand signal.
            continue
        if factors:
            warnings.append("Сезонность взята из таблицы поставщика и нормирована к среднегодовому уровню.")

        stock_rows = stock_groups.get((sku, wh), empty)
        stock_as_of = None
        if not stock_rows.empty and "as_of" in stock_rows:
            stock_dates = pd.to_datetime(stock_rows["as_of"], errors="coerce").dt.normalize()
            eligible = stock_dates.isna() | (stock_dates <= pd.Timestamp(today))
            stock_rows = stock_rows.loc[eligible]
            valid_dates = stock_dates.loc[eligible].dropna()
            if not valid_dates.empty:
                stock_as_of = valid_dates.min().date()
        stock_known = not stock_rows.empty and stock_rows["on_hand"].notna().all()
        on_hand = _number(stock_rows["on_hand"].sum()) if not stock_rows.empty else 0.0
        if not stock_known:
            warnings.append("Остаток неизвестен или неполон; неизвестная часть принята за 0. Перед заказом подтвердите фактический остаток.")
        if stock_as_of and stock_as_of < history_end:
            warnings.append(f"Остаток на {stock_as_of.isoformat()} устарел для даты расчёта; требуется сверка.")

        incoming = transit_groups.get((sku, wh), empty)
        in_transit, ignored, arrivals = 0.0, 0.0, []
        if not incoming.empty:
            positive = incoming[incoming["qty"] > 0].copy()
            eligible = positive["eta"].ge(pd.Timestamp(today)) & positive["eta"].lt(horizon_end)
            if "source_as_of" in positive:
                snapshot_dates = pd.to_datetime(positive["source_as_of"], errors="coerce").dt.normalize()
                after_calculation = snapshot_dates.gt(pd.Timestamp(today))
                eligible &= ~after_calculation
                if after_calculation.any():
                    warnings.append("Снимок товаров в пути датирован позже даты расчёта и не используется для исторического расчёта.")
            included = positive.loc[eligible]
            in_transit = float(included["qty"].sum())
            ignored = float(positive.loc[~eligible, "qty"].sum())
            arrivals = [((eta.date() - today).days, float(qty))
                        for eta, qty in included.groupby("eta")["qty"].sum().items()]
            if ignored:
                warnings.append(f"Из товаров в пути не вычтено {ignored:g}: ETA просрочена, неизвестна или за пределами горизонта. Требуется подтверждение поставки.")
        need = compute_need(fc, on_hand, in_transit, lead_time, review_period_days,
                            service_level, pack_size, min_oq, arrivals=arrivals)
        if need.recommended_qty <= 0 and need.urgency == "low":
            continue
        if need.recommended_qty <= 0:
            warnings.append("Объёма ожидаемых поставок достаточно, но до прихода возможен дефицит. Ускорьте поставку или переместите запас; дополнительный объём не требуется.")
        rationale = Rationale(
            avg_daily_demand=fc.avg_daily_demand, seasonality_factor=fc.seasonality_factor,
            trend_factor=fc.trend_factor, horizon_days=need.horizon_days,
            forecast_demand=need.forecast_demand, safety_stock=need.safety_stock,
            on_hand=on_hand, in_transit=in_transit, ignored_in_transit=ignored,
            stock_as_of=stock_as_of, lost_demand_uplift=round(uplift, 2),
            excluded_bulk_units=round(excluded_units, 2), excluded_bulk_orders=excluded_orders,
            raw_need=need.raw_need,
        )
        unit = _clean_text(info.get("unit", "")).strip()
        if not unit:
            unit = "ед. (не указана)"
            warnings.append("Единица измерения не указана в источнике. Перед заказом подтвердите единицу измерения у поставщика.")
        explanation = build_explanation(name, rationale, need.recommended_qty, need.urgency, use_llm=explain, unit=unit)
        line = OrderLine(
            line_id=f"{sku}::{wh}", sku=str(sku), supplier_sku=_clean_text(info.get("supplier_sku")) or None,
            name=name, category=cat, warehouse=str(wh), supplier_id=supplier_id,
            supplier_name=sup.get("name", "—"), unit=unit,
            pack_size=pack_size, min_order_qty=min_oq, recommended_qty=need.recommended_qty,
            urgency=need.urgency, days_of_cover=need.days_of_cover,
            rationale=rationale, explanation=explanation, warnings=warnings,
        )
        lines_by_supplier.setdefault(supplier_id, []).append(line)

    groups: list[SupplierGroup] = []
    urgency_rank = {"high": 0, "medium": 1, "low": 2}
    for supplier_id, lines in lines_by_supplier.items():
        lines.sort(key=lambda line: (urgency_rank[line.urgency], -line.recommended_qty))
        sup = smap["suppliers"].get(supplier_id, {})
        groups.append(SupplierGroup(
            supplier_id=supplier_id, supplier_name=sup.get("name", "—"),
            lead_time_days=int(_number(sup.get("lead_time_days"), 14)),
            total_units=round(sum(line.recommended_qty for line in lines), 4), lines=lines,
        ))
    groups.sort(key=lambda group: -group.total_units)
    if reconciliation["mismatched_sku_months"]:
        response_warnings.append("Обнаружены расхождения объёмов между транзакциями и месячными отчётами. Месячные итоги сохранены; исключение крупных заказов выполнено только для согласованных месяцев.")
    return RecommendationResponse(
        calculation_id=uuid4().hex,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        as_of=today, data_source=source, warnings=response_warnings,
        data_quality={**metadata, "calculation_reconciliation": reconciliation}, warehouse=warehouse, category=category,
        service_level=service_level, review_period_days=review_period_days,
        sku_count=sum(len(group.lines) for group in groups), groups=groups,
    )
