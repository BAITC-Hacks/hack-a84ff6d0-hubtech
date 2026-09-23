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
    """Use real document IDs, otherwise real client/day, otherwise individual rows.

    At least five positive order observations are required. A large share of an
    otherwise tiny sample is not evidence of a one-off bulk order.
    """
    if tx.empty:
        return OutlierResult(tx.copy(), 0.0, 0)
    work = tx.reset_index(drop=True).copy()
    day = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
    keys = pd.Series([f"row:{i}" for i in range(len(work))], index=work.index)
    for column in ("client_id", "order_id", "document_id", "order_document", "document"):
        if column not in work:
            continue
        values = work[column].fillna("").astype(str).str.strip()
        present = values.ne("") & ~values.isin(["nan", "None", "<NA>"])
        keys.loc[present] = column + ":" + day.loc[present] + ":" + values.loc[present]
    positive = work["qty"].clip(lower=0.0)
    quantities = positive.groupby(keys).sum()
    quantities = quantities[quantities > 0]
    if len(quantities) < 5:
        return OutlierResult(work, 0.0, 0)
    median = float(quantities.median())
    q1, q3 = np.percentile(quantities, [25, 75])
    extreme = (quantities > q3 + 3.0 * (q3 - q1)) & (quantities >= 8.0 * max(median, 1e-9))
    bulk_keys = quantities.index[extreme]
    # Returns are not classified as positive bulk demand.
    bulk = keys.isin(bulk_keys) & (work["qty"] > 0)
    excluded = work.loc[bulk].copy()
    excluded["_bulk_order_key"] = keys.loc[bulk]
    return OutlierResult(work.loc[~bulk].copy(), float(excluded["qty"].sum()), len(bulk_keys), excluded)
