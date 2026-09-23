#!/usr/bin/env python3
"""Кандидаты на исключительные заказы. Продажи автоматически не исключаются."""
import numpy as np
import pandas as pd


def detect_candidates(transactions, panel, reconciliation, as_of, min_history=8, window_days=180):
    if min_history < 3 or window_days < 1:
        raise ValueError("Некорректное окно сравнения заказов")
    tx = transactions.copy()
    tx["date"] = pd.to_datetime(tx.date)
    tx = tx[tx.document_type.eq("Расходная накладная") & tx.date.lt(pd.Timestamp(as_of)) & tx.date.notna()].copy()
    tx["day"] = tx.date.dt.normalize()
    tx["document_key"] = tx.document_number.fillna("").astype(str).str.strip()
    no_document = tx.document_key.eq("")
    # Строку без номера нельзя объявлять отдельным подтвержденным документом.
    ungrouped = int(no_document.sum())
    tx = tx[~no_document].copy()
    tx["warehouse"] = tx.warehouse.fillna("")
    tx["unit"] = tx.unit.fillna("")
    keys = ["sku", "unit", "warehouse", "day", "document_key"]
    grouped = tx.groupby(keys, dropna=False, sort=True)
    orders = grouped.agg(qty=("qty_raw", lambda s: s.sum(min_count=1)),
                         line_count=("qty_raw", "size"), known_lines=("qty_raw", "count"),
                         negative_lines=("qty_raw", lambda s: int(s.lt(0).sum()))).reset_index()
    # Не смешиваем возврат/неизвестную строку с положительным размером заказа.
    orders["usable"] = orders.line_count.eq(orders.known_lines) & orders.negative_lines.eq(0) & orders.qty.gt(0)
    candidates = []
    for (sku, unit, warehouse), group in orders[orders.usable].groupby(["sku", "unit", "warehouse"], sort=False):
        group = group.sort_values(["day", "document_key"]).reset_index(drop=True)
        days = group.day.to_numpy(dtype="datetime64[ns]")
        quantities = group.qty.to_numpy(float)
        for row in group.itertuples():
            # Все документы текущего дня исключены из базы сравнения.
            left = np.searchsorted(days, np.datetime64(row.day - pd.Timedelta(days=window_days)), side="left")
            right = np.searchsorted(days, np.datetime64(row.day), side="left")
            prior = quantities[left:right]
            if len(prior) < min_history:
                continue
            q1, q3 = np.quantile(prior, [0.25, 0.75])
            median = float(np.median(prior))
            threshold_iqr, threshold_median = float(q3 + 3 * (q3 - q1)), 8 * median
            if row.qty > threshold_iqr and row.qty >= threshold_median:
                candidates.append(dict(sku=sku, unit=unit, warehouse=warehouse, day=row.day,
                    document_number=row.document_key, order_qty=row.qty,
                    prior_orders=len(prior), prior_window_days=window_days,
                    historical_median=median, threshold_iqr=threshold_iqr,
                    threshold_8median=threshold_median,
                    reason="above_Q3_plus_3IQR_and_at_least_8_medians",
                    excluded_qty=0.0, exclusion_status="review_only"))
    columns = ["sku", "unit", "warehouse", "day", "document_number", "order_qty", "prior_orders",
               "prior_window_days", "historical_median", "threshold_iqr", "threshold_8median",
               "reason", "excluded_qty", "exclusion_status"]
    result = pd.DataFrame(candidates, columns=columns)
    result["month"] = pd.to_datetime(result.day).dt.to_period("M").dt.to_timestamp()
    match = reconciliation[["sku", "month", "comparable", "matches_signed", "sales_qty_raw"]].copy()
    match["month"] = pd.to_datetime(match.month)
    for name in ("comparable", "matches_signed"):
        match[name] = match[name].astype(str).str.lower().eq("true")
    result = result.merge(match, on=["sku", "month"], how="left", validate="many_to_one")
    result["monthly_matches_signed"] = result.matches_signed.eq(True) & result.comparable.eq(True)
    result["review_reason"] = np.where(result.monthly_matches_signed,
        "quantity_matches_but_scope_customer_and_exception_not_confirmed",
        "monthly_and_transactions_not_reconciled")
    history = panel[["sku", "month", "sales_qty_raw", "target_qty"]].copy()
    quantities = result.groupby(["sku", "month"]).order_qty.sum().rename("candidate_order_qty").reset_index()
    history = history.merge(quantities, on=["sku", "month"], how="left", validate="one_to_one")
    history["candidate_order_qty"] = history.candidate_order_qty.fillna(0.0)
    history["excluded_qty"] = 0.0
    history["regular_sales_qty"] = history.sales_qty_raw  # Никакой автоматической корректировки.
    audit = dict(transaction_rows_without_document=ungrouped, grouped_sku_documents=len(orders),
                 sku_documents_not_usable=int((~orders.usable).sum()),
                 candidates=len(result), matched_monthly_candidates=int(result.monthly_matches_signed.sum()),
                 automatically_excluded_orders=0, client_id_available=False,
                 settings=dict(min_prior_orders=min_history, prior_days=window_days,
                               iqr_multiplier=3, median_multiplier=8))
    return result, history, audit
