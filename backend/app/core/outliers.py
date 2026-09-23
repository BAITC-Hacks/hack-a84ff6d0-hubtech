"""Detect unusually large customer/day or document orders with sample protection."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class OutlierResult:
    regular: pd.DataFrame
    excluded_units: float
    excluded_orders: int
    excluded: pd.DataFrame = field(default_factory=pd.DataFrame)


def exclude_bulk_orders(tx: pd.DataFrame) -> OutlierResult:
    """Union customer/day and document/day detections; remove each row once.

    Each level needs five positive observations within the same SKU/warehouse.
    Missing identifiers remain individual rows, never one anonymous customer.
    Customer evidence takes precedence in the explanation/count when both
    levels flag a row; document IDs must not erase customer-level evidence.
    """
    if tx.empty:
        return OutlierResult(tx.copy(), 0.0, 0)
    work = tx.reset_index(drop=True).copy()
    day = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
    row_keys = pd.Series([f"row:{i}" for i in range(len(work))], index=work.index)
    client_keys, document_keys = row_keys.copy(), row_keys.copy()
    for column in ("client_id", "order_id", "document_id", "order_document", "document"):
        if column not in work:
            continue
        values = work[column].fillna("").astype(str).str.strip()
        present = values.ne("") & ~values.isin(["nan", "None", "<NA>"])
        keys = client_keys if column == "client_id" else document_keys
        keys.loc[present] = column + ":" + day.loc[present] + ":" + values.loc[present]
    positive = work["qty"].clip(lower=0.0)
    scope_columns = [column for column in ("sku", "warehouse") if column in work]
    scopes = work.groupby(scope_columns, dropna=False, sort=False).groups if scope_columns else {(): work.index}
    classified = pd.Series("", index=work.index, dtype=object)
    for scope, indexes in scopes.items():
        # Document first, then client: the stronger aggregate label wins while
        # the union of positive row indexes determines units, not summed flags.
        for keys in (document_keys, client_keys):
            scoped_keys = keys.loc[indexes]
            quantities = positive.loc[indexes].groupby(scoped_keys).sum()
            quantities = quantities[quantities > 0]
            if len(quantities) < 5:
                continue
            median = float(quantities.median())
            q1, q3 = np.percentile(quantities, [25, 75])
            extreme = ((quantities > q3 + 3.0 * (q3 - q1))
                       & (quantities >= 8.0 * max(median, 1e-9)))
            flagged = scoped_keys[scoped_keys.isin(quantities.index[extreme])].index
            if keys is client_keys:
                # Anonymous row fallbacks are not stronger customer evidence.
                flagged = flagged[classified.loc[flagged].eq("") | keys.loc[flagged].str.startswith("client_id:")]
            classified.loc[flagged] = repr(scope) + ":" + keys.loc[flagged]
    # Returns are not classified as positive bulk demand.
    bulk = classified.ne("") & (work["qty"] > 0)
    excluded = work.loc[bulk].copy()
    excluded["_bulk_order_key"] = classified.loc[bulk]
    return OutlierResult(work.loc[~bulk].copy(), float(excluded["qty"].sum()),
                         int(excluded["_bulk_order_key"].nunique()), excluded)
