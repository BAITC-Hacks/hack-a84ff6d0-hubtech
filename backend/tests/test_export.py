"""Экспорт должен сохранять показанные рекомендации и решения менеджера."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pydantic import ValidationError

from app.core.export import ExportValidationError, to_excel_bytes
from app.core.snapshots import load_snapshot, save_snapshot
from app.main import app
from app.repositories import create_order, create_user
from app.storage import connection, migrate
from app.schemas import (
    ExportLine, ExportRequest, OrderLine, Rationale, RecommendationResponse, SupplierGroup,
)


def example_response():
    rationale = Rationale(
        avg_daily_demand=1, seasonality_factor=1, trend_factor=1,
        horizon_days=14, forecast_demand=14, safety_stock=6, on_hand=0,
        in_transit=0, lost_demand_uplift=0, excluded_bulk_units=0,
        excluded_bulk_orders=0, raw_need=20, stock_as_of=date(2026, 9, 22),
    )
    lines = [OrderLine(
        line_id=f"SUP:WH-{i}:001", sku="001", supplier_sku="CAT-001", name='=literal name',
        category="Кабель", warehouse=f"WH-{i}", unit="м", supplier_id="SUP",
        supplier_name="Поставщик", recommended_qty=20, pack_size=5, min_order_qty=10,
        urgency="high", days_of_cover=0, rationale=rationale, explanation="Расчёт 20 м.",
    ) for i in (1, 2)]
    return RecommendationResponse(
        generated_at="2026-09-23T00:00:00Z", as_of=date(2026, 9, 23), data_source="excel",
        service_level=.95, review_period_days=7, sku_count=2,
        warnings=["Срок поставки принят как допущение."],
        groups=[SupplierGroup(supplier_id="SUP", supplier_name="Поставщик", lead_time_days=7,
                              total_units=40, lines=lines)],
    )


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        setting = patch.dict(os.environ, {"ORDER_DB_PATH": self.directory.name + "/orders.sqlite3",
                                          "APP_DB_PATH": self.directory.name + "/orders.sqlite3",
                                          "APP_ENV": "development", "APP_ORIGIN": ""})
        setting.start()
        self.addCleanup(setting.stop)
        self.snapshot = save_snapshot(example_response())
        migrate()
        self.user = create_user("export-admin", "export-test-password", "admin")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        signed_in = self.client.post("/api/auth/login", json={"username": "export-admin", "password": "export-test-password"})
        self.client.headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]

    def request(self, quantities=(25, 30), approved=(True, False), approved_only=False):
        return ExportRequest(
            calculation_id=self.snapshot.calculation_id, approved_only=approved_only,
            lines=[ExportLine(line_id=line.line_id, quantity=quantity, approved=approval)
                   for line, quantity, approval in zip(self.snapshot.groups[0].lines, quantities, approved)],
        )

    def rows(self, data):
        wb = load_workbook(io.BytesIO(data), data_only=False)
        self.addCleanup(wb.close)
        sheet = wb["Заказ поставщикам"]
        values = list(sheet.values)
        return wb, [dict(zip(values[0], row)) for row in values[1:]]

    def test_snapshot_is_persisted_and_independent(self):
        restored = load_snapshot(self.snapshot.calculation_id)
        self.assertEqual(restored.model_dump(), self.snapshot.model_dump())
        self.snapshot.groups[0].lines[0].recommended_qty = 999
        self.assertEqual(load_snapshot(restored.calculation_id).groups[0].lines[0].recommended_qty, 20)

    def test_export_uses_snapshot_without_loading_data_or_recalculating(self):
        with patch("app.api.routes._load", side_effect=AssertionError("must not reload")), \
             patch("app.jobs.generate_recommendations", side_effect=AssertionError("must not recalculate")):
            result = self.client.post("/api/recommend/export", json=self.request().model_dump())
        self.assertEqual(result.status_code, 200, result.text[:300] if result.status_code != 200 else "")
        wb, rows = self.rows(result.content)
        self.assertEqual([r["К заказу, ед"] for r in rows], [25, 30])
        self.assertEqual([r["Рекомендовано, ед"] for r in rows], [20, 20])
        self.assertEqual([r["Склад"] for r in rows], ["WH-1", "WH-2"])
        self.assertEqual([r["Утверждено"] for r in rows], ["Да", "Нет"])
        self.assertEqual(wb["Заказ поставщикам"]["F2"].data_type, "s")
        self.assertEqual(rows[0]["Наименование"], "=literal name")
        self.assertEqual(dict(list(wb["Параметры расчёта"].values)[1:])["ID расчёта"], self.snapshot.calculation_id)

    def test_approved_only_and_zero_override(self):
        _, rows = self.rows(to_excel_bytes(self.snapshot, self.request(approved_only=True)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["К заказу, ед"], 25)
        _, rows = self.rows(to_excel_bytes(self.snapshot, self.request(quantities=(0, 30), approved=(False, False))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Склад"], "WH-2")

    def test_invalid_quantities_and_empty_selection_rejected(self):
        cases = [
            self.request(quantities=(11, 20)),
            self.request(quantities=(5, 20)),
            self.request(quantities=(0, 20)),
            self.request(approved=(False, False), approved_only=True),
            self.request(quantities=(0, 0), approved=(False, False)),
        ]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(ExportValidationError):
                to_excel_bytes(self.snapshot, request)
        for value in (-1, float("nan"), float("inf"), True, "25"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ExportLine(line_id="line", quantity=value)

    def test_duplicate_unknown_and_missing_ids_rejected(self):
        for mode in ("duplicate", "unknown", "missing"):
            request = self.request()
            if mode == "duplicate":
                request.lines[1].line_id = request.lines[0].line_id
            elif mode == "unknown":
                request.lines[1].line_id = "unknown"
            else:
                request.lines.pop()
            with self.subTest(mode=mode):
                result = self.client.post("/api/recommend/export", json=request.model_dump())
                self.assertEqual(result.status_code, 422)

    def test_missing_snapshot_errors_but_durable_order_does_not_expire(self):
        request = self.request()
        request.calculation_id = "unknown"
        self.assertEqual(self.client.post("/api/recommend/export", json=request.model_dump()).status_code, 410)
        with patch("app.core.snapshots.RETENTION_SECONDS", -1):
            result = self.client.post("/api/recommend/export", json=self.request().model_dump())
        self.assertEqual(result.status_code, 200)

    def test_recommend_returns_saved_calculation_id(self):
        with connection(write=True) as db:
            order_id = create_order(db, example_response(), self.user)
        with patch("app.api.routes.jobs.enqueue", return_value={"job_id": "job", "status": "queued"}), \
             patch("app.api.routes.jobs.get_job", return_value={"status": "completed", "order_id": order_id}):
            result = self.client.post("/api/recommend", json={"explain": False})
        self.assertEqual(result.status_code, 200)
        calculation_id = result.json()["calculation_id"]
        self.assertTrue(calculation_id)
        self.assertEqual(load_snapshot(calculation_id).sku_count, 2)


if __name__ == "__main__":
    unittest.main()
