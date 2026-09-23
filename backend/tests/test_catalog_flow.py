"""Catalog data survives authenticated jobs, durable orders and snapshot exports."""
from copy import deepcopy
import io
import json
from unittest.mock import patch

from openpyxl import load_workbook
import pandas as pd

from app import jobs
from app.core.recommend import generate_recommendations
from app.data.enrichment import enrich_catalog
from app.repositories import create_order, public_metadata
from app.storage import connection
from app.worker import register
from tests.api_support import AuthenticatedApiTest
from tests.test_calculation import dataset
from tests.test_catalog_enrichment import product, FETCHED_AT


class CatalogFlowTests(AuthenticatedApiTest):
    def setUp(self):
        super().setUp()
        self.worker_id = "catalog-test-worker"
        self.assertTrue(register(self.worker_id))
        self.source = dataset()
        self.source.source = "excel"
        self.source.catalog = pd.DataFrame([
            dict(sku="S1", name="Название 1С", category="Внутренняя 3", supplier_id="SUP",
                 supplier_sku="CTG12-110-K04-050-R", unit="м"),
            dict(sku="UNMATCHED", name="Без карточки", category="Внутренняя 3", supplier_id="SUP",
                 supplier_sku="", unit="м"),
        ])
        self.catalog_path = self.folder / "catalog.json"
        self.catalog_path.write_text(json.dumps({
            "schema_version": 1, "source": "https://ekt.kz", "fetched_at": FETCHED_AT,
            "products": [product("S1")],
        }, ensure_ascii=False), encoding="utf-8")
        self.enriched = enrich_catalog(self.source, self.catalog_path)

    def enqueue(self, payload, key):
        return self.client.post("/api/recommend", json=payload,
                                headers={"Prefer": "respond-async", "Idempotency-Key": key})

    def finish(self, job_id):
        self.assertEqual(jobs.claim(self.worker_id), job_id)
        with patch("app.jobs.get_data_source") as source:
            source.return_value.load.return_value = self.enriched
            jobs.execute_job(job_id, self.worker_id)
        response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(job["status"], "completed", job)
        return self.client.get(f"/api/orders/{job['order_id']}").json()

    def test_filters_queue_idempotency_and_immutable_export(self):
        before = generate_recommendations(self.source, explain=False)
        with patch("app.api.routes._load", return_value=self.enriched):
            response = self.client.get("/api/meta")
        self.assertEqual(response.status_code, 200, response.text)
        meta = response.json()
        self.assertEqual(meta["categories"], ["Внутренняя 3"])
        self.assertEqual(meta["product_categories"], ["Кабеленесущие системы"])
        self.assertIn("defaults", meta)
        self.assertIn("llm_available", meta["capabilities"])
        coverage = meta["data_quality"]["catalog_enrichment"]
        self.assertEqual((coverage["status"], coverage["matched"], coverage["unmatched"]), ("partial", 1, 1))
        params = {"explain": False, "warehouse": "A", "category": "Внутренняя 3",
                  "product_category": "Кабеленесущие системы"}
        queued = self.enqueue(params, "same-calculation")
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(self.enqueue(params, "same-calculation").json(), queued.json())
        conflict = self.enqueue({**params, "product_category": "Другая группа"}, "same-calculation")
        self.assertEqual(conflict.status_code, 409)
        detail = self.finish(queued.json()["job_id"])
        result = detail["calculation"]
        line = result["groups"][0]["lines"][0]
        self.assertEqual(result["product_category"], params["product_category"])
        self.assertEqual(line["recommended_qty"], before.groups[0].lines[0].recommended_qty)
        self.assertEqual(line["rationale"], before.groups[0].lines[0].rationale.model_dump(mode="json"))
        self.assertEqual(line["name"], "Название 1С")
        self.assertEqual(line["category"], "Внутренняя 3")
        self.assertEqual(line["product_attributes"]["Диаметр"], "110 мм")
        self.assertEqual(result["data_quality"]["catalog_enrichment"], coverage)
        self.assertTrue(any("только сопоставленные" in warning for warning in result["warnings"]))

        changed = [{"line_id": line["line_id"], "quantity": line["recommended_qty"] + 1, "approved": True}]
        saved = self.client.put(f"/api/orders/{detail['id']}", json={"revision": detail["revision"], "lines": changed})
        self.assertEqual(saved.status_code, 200, saved.text)
        detail = saved.json()
        self.assertFalse(detail["decisions"][0]["approved"])
        saved = self.client.put(f"/api/orders/{detail['id']}", json={"revision": detail["revision"], "lines": changed})
        self.assertEqual(saved.status_code, 200, saved.text)
        detail = saved.json()
        self.assertTrue(detail["decisions"][0]["approved"])
        self.catalog_path.unlink()
        self.enriched.catalog["product_category"] = "Изменённый справочник"
        self.enriched.metadata["catalog_enrichment"]["matched"] = 0
        with patch("app.api.routes._load", side_effect=AssertionError("must not reload source")), \
             patch("app.jobs.generate_recommendations", side_effect=AssertionError("must not recalculate")):
            for approved_only in (False, True):
                exported = self.client.post(f"/api/orders/{detail['id']}/export", json={
                    "revision": detail["revision"], "approved_only": approved_only,
                })
                self.assertEqual(exported.status_code, 200)
                book = load_workbook(io.BytesIO(exported.content), data_only=True)
                self.assertEqual(book.sheetnames, ["Заказ поставщикам", "Параметры расчёта"])
                values = list(book["Заказ поставщикам"].values)
                self.assertEqual(len(values[0]), 37)
                row = dict(zip(values[0], values[1]))
                self.assertEqual(row["Категория"], "Внутренняя 3")
                self.assertEqual(row["Товарная группа ekt.kz"], "Кабеленесущие системы")
                self.assertEqual(row["К заказу, ед"], changed[0]["quantity"])
                self.assertEqual(row["Карточка ekt.kz"], line["product_url"])
                self.assertEqual(row["Дата справочника ekt.kz"], line["catalog_fetched_at"])
                self.assertEqual(dict(list(book["Параметры расчёта"].values)[1:])["Товарная группа ekt.kz"], params["product_category"])
                book.close()
            fetched = self.client.get(f"/api/orders/{detail['id']}").json()
            self.assertEqual(fetched["calculation"], result)
        self.assertEqual(self.enqueue(params, "same-calculation").json()["job_id"], queued.json()["job_id"])
        with connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)

    def test_product_and_accounting_filters_are_independent(self):
        for index, filters in enumerate((
            {"category": "Другая категория", "product_category": "Кабеленесущие системы"},
            {"category": "Внутренняя 3", "product_category": "Другая группа"},
        )):
            queued = self.enqueue({"explain": False, **filters}, f"empty-{index}")
            self.assertEqual(queued.status_code, 202, queued.text)
            detail = self.finish(queued.json()["job_id"])
            self.assertEqual(detail["calculation"]["groups"], [])
            self.assertEqual(detail["calculation"]["product_category"], filters["product_category"])
            self.assertEqual(detail["decisions"], [])

    def test_older_saved_orders_without_product_fields_remain_readable_and_exportable(self):
        calculation = generate_recommendations(self.source, explain=False)
        with connection(write=True) as db:
            order_id = create_order(db, calculation, self.user)
            raw = json.loads(db.execute("SELECT calculation FROM orders WHERE id=?", (order_id,)).fetchone()[0])
            raw.pop("product_category", None)
            for group in raw["groups"]:
                for line in group["lines"]:
                    for key in ("product_category", "product_subcategory", "product_brand", "product_url", "product_attributes", "catalog_fetched_at"):
                        line.pop(key, None)
            db.execute("UPDATE orders SET calculation=? WHERE id=?", (json.dumps(raw), order_id))
        response = self.client.get(f"/api/orders/{order_id}")
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        line = detail["calculation"]["groups"][0]["lines"][0]
        self.assertIsNone(detail["calculation"]["product_category"])
        self.assertIsNone(line["product_category"])
        self.assertIsNone(line["product_url"])
        self.assertEqual(line["product_attributes"], {})
        exported = self.client.post(f"/api/orders/{order_id}/export", json={"revision": detail["revision"], "approved_only": False})
        self.assertEqual(exported.status_code, 200)
        book = load_workbook(io.BytesIO(exported.content), data_only=True)
        values = list(book["Заказ поставщикам"].values)
        row = dict(zip(values[0], values[1]))
        self.assertIsNone(row["Товарная группа ekt.kz"])
        self.assertEqual(row["К заказу, ед"], line["recommended_qty"])
        book.close()

    def test_public_catalog_coverage_rejects_arbitrary_data(self):
        original = deepcopy(self.enriched.metadata)
        report = self.enriched.metadata["catalog_enrichment"]
        report.update({"private_path": "C:/private/data.xlsx", "error": "private diagnostic", "matched": True,
                       "unmatched": -1, "web_products": 2 ** 54, "fetched_at": "C:/private/path", "source": "https://evil.example"})
        report["conflict_reasons"].update({"private": "do not expose", "brand": -1})
        cleaned = public_metadata(self.enriched.metadata)["catalog_enrichment"]
        self.assertEqual(cleaned["status"], "partial")
        for key in ("private_path", "error", "matched", "unmatched", "web_products", "fetched_at", "source"):
            self.assertNotIn(key, cleaned)
        self.assertNotIn("private", cleaned["conflict_reasons"])
        self.assertNotIn("brand", cleaned["conflict_reasons"])
        self.assertNotIn("private", json.dumps(cleaned))
        self.assertEqual(public_metadata(original)["catalog_enrichment"], original["catalog_enrichment"])
        self.assertEqual(public_metadata({"catalog_enrichment": {"status": [], "source": {}, "crawl_coverage": {}}})["catalog_enrichment"], {"fetched_at": None})
