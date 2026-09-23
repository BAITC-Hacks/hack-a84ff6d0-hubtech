"""Warehouse filters reflect accounting inputs, without inventing locations."""
from datetime import date
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import pandas as pd

from app.data.adapter import Dataset
from app.main import app
from app.security import current_session


def dataset():
    return Dataset(
        sales=pd.DataFrame([dict(sku="S1", warehouse="Алматы", name="Товар", category="C",
                                 date=pd.Timestamp("2026-08-01"), qty=10)]),
        stock=pd.DataFrame([dict(sku="S1", warehouse="Алматы", on_hand=2)]),
        in_transit=pd.DataFrame([dict(sku="S1", warehouse="Алматы", qty=3,
                                      eta=pd.Timestamp("2026-09-25"))]),
        suppliers=pd.DataFrame([dict(supplier_id="SUP", name="Поставщик",
                                     lead_time_days=5, min_order_qty=1)]),
        sku_suppliers=pd.DataFrame([dict(sku="S1", supplier_id="SUP", pack_size=1)]),
        stockouts=pd.DataFrame(columns=["sku", "warehouse", "start", "end"]),
        monthly_sales=pd.DataFrame([dict(sku="S1", warehouse="Алматы",
                                         month=pd.Timestamp("2026-08-01"), qty=10)]),
        as_of=date(2026, 9, 23),
        source="excel",
    )


class WarehouseMetadataTests(unittest.TestCase):
    def metadata(self, ds):
        # Authentication is covered in test_service; this tests metadata only.
        with patch.dict(app.dependency_overrides, {current_session: lambda: {'user': {'id': 'metadata-test'}}}):
            with patch('app.main.migrate'), TestClient(app) as client, patch("app.api.routes._load", return_value=ds):
                response = client.get("/api/meta")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_current_almaty_inputs_still_offer_one_warehouse(self):
        self.assertEqual(self.metadata(dataset())["warehouses"], ["Алматы"])

    def test_union_includes_monthly_stock_and_transit_only_warehouses(self):
        ds = dataset()
        ds.monthly_sales.loc[0, "warehouse"] = "Астана"
        ds.stock.loc[0, "warehouse"] = "СКЛАД-002"
        ds.in_transit.loc[0, "warehouse"] = "  WH-03  "
        # A distinct warehouse may have monthly history but no transactions.
        # Blank values never become filter choices; exact nonblank keys remain
        # unchanged because the engine matches them without trimming aliases.
        for name in ("sales", "monthly_sales", "stock", "in_transit"):
            frame = getattr(ds, name)
            invalid = pd.DataFrame({"sku": ["S1"] * 5,
                                    "warehouse": [None, pd.NA, float("nan"), "", " \t "]})
            setattr(ds, name, pd.concat([frame, invalid], ignore_index=True))
        before = ds.stock.copy(deep=True)
        self.assertEqual(self.metadata(ds)["warehouses"],
                         sorted(["Алматы", "Астана", "СКЛАД-002", "  WH-03  "]))
        pd.testing.assert_frame_equal(ds.stock, before)

    def test_unrelated_history_and_empty_frames_do_not_invent_options(self):
        ds = dataset()
        ds.monthly_sales = pd.DataFrame()
        ds.stock = pd.DataFrame()
        ds.in_transit = pd.DataFrame()
        ds.monthly_stock = pd.DataFrame([dict(sku="S1", warehouse="Архивный склад")])
        ds.stockouts = pd.DataFrame([dict(sku="S1", warehouse="Только stockout")])
        self.assertEqual(self.metadata(ds)["warehouses"], ["Алматы"])


if __name__ == "__main__":
    unittest.main()
