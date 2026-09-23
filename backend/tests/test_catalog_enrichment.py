"""Offline catalog enrichment must never replace accounting data."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal

from app.config import PROJECT_ROOT, Settings
from app.data.adapter import CsvDataSource, Dataset
from app.data.enrichment import PRODUCT_COLUMNS, enrich_catalog
from app.data.excel import ExcelDataSource
from tests.test_excel_import import fixture_sources


FETCHED_AT = "2026-09-23T12:00:00+00:00"


def product(sku="00123_", **changes):
    result = {
        "sku": sku, "supplier_sku": "CTG12-110-K04-050-R", "brand": "IEK",
        "name": "Новое публичное название", "product_category": "Кабеленесущие системы",
        "product_subcategory": "Трубы", "product_url": "https://ekt.kz/catalog/test/tube/",
        "product_attributes": {"Диаметр": "110 мм", "Цвет": "красный"}, "fetched_at": FETCHED_AT,
    }
    result.update(changes)
    return result


def dataset():
    return Dataset(
        catalog=pd.DataFrame([
            {"sku": "00123_", "name": "Название 1С", "supplier_sku": "CTG12-110-K04-050-R",
             "supplier_id": "IEK", "category": "Категория не указана", "unit": "м"},
            {"sku": "00456_", "name": "Второй", "supplier_sku": "ATN000105",
             "supplier_id": "SYSTEME", "category": "Категория 3", "unit": "шт"},
        ]),
        sales=pd.DataFrame([{"sku": "00123_", "qty": 2, "price": None}]),
        stock=pd.DataFrame([{"sku": "00123_", "warehouse": "Алматы", "on_hand": 31}]),
        in_transit=pd.DataFrame([{"sku": "00123_", "qty": 50}]),
        suppliers=pd.DataFrame([{"supplier_id": "IEK", "lead_time_days": 21}]),
        sku_suppliers=pd.DataFrame([{"sku": "00123_", "min_order_qty": 50, "pack_size": 1}]),
        stockouts=pd.DataFrame(), source="excel", warnings=["Исходное предупреждение"],
        metadata={"base": {"known": 1}},
    )


class CatalogEnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.json"

    def save(self, products, **changes):
        payload = {"schema_version": 1, "source": "https://ekt.kz", "fetched_at": FETCHED_AT,
                   "products": products, "crawl": {"pages": len(products)}}
        payload.update(changes)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_matches_exact_sku_and_preserves_all_accounting_fields(self):
        self.save([product(), product("00456_", supplier_sku="ATN000105", brand="Schneider Electric")])
        source = dataset()
        before = deepcopy(source)
        enriched = enrich_catalog(source, self.path)
        self.assertEqual(enriched.catalog.loc[0, "product_category"], "Кабеленесущие системы")
        self.assertEqual(enriched.catalog.loc[1, "category"], "Категория 3")
        self.assertEqual(enriched.catalog.loc[0, "name"], "Название 1С")
        self.assertEqual(enriched.catalog.loc[0, "unit"], "м")
        self.assertEqual(enriched.catalog.loc[0, "product_attributes"]["Диаметр"], "110 мм")
        self.assertEqual(enriched.catalog.loc[1, "product_brand"], "Schneider Electric")
        self.assertEqual(enriched.metadata["catalog_enrichment"]["status"], "loaded")
        self.assertEqual(enriched.metadata["catalog_enrichment"]["matched"], 2)
        self.assertEqual(enriched.metadata["catalog_enrichment"]["unmatched"], 0)
        self.assertEqual(enriched.metadata["catalog_enrichment"]["fetched_at"], FETCHED_AT)
        assert_frame_equal(enriched.catalog[list(before.catalog.columns)], before.catalog)
        assert_frame_equal(source.catalog, before.catalog)
        self.assertEqual(source.metadata, before.metadata)
        self.assertEqual(source.warnings, before.warnings)
        for field in ("sales", "stock", "in_transit", "suppliers", "sku_suppliers", "stockouts"):
            assert_frame_equal(getattr(enriched, field), getattr(before, field))

    def test_codes_are_not_matched_after_removing_zeroes_or_suffixes(self):
        self.save([product("00123"), product("123_"), product("00456_", supplier_sku="ATN000105", brand="Systeme Electric")])
        enriched = enrich_catalog(dataset(), self.path)
        self.assertEqual(enriched.catalog.loc[0, "product_category"], "")
        self.assertEqual(enriched.metadata["catalog_enrichment"]["matched"], 1)
        self.assertEqual(enriched.metadata["catalog_enrichment"]["status"], "partial")

    def test_ambiguous_duplicate_is_rejected_even_if_one_row_is_invalid(self):
        self.save([product(), product(product_url="https://other.example/catalog/test/"),
                   product("00456_", supplier_sku="ATN000105", brand="Systeme Electric")])
        enriched = enrich_catalog(dataset(), self.path)
        report = enriched.metadata["catalog_enrichment"]
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["conflicts"], 1)
        self.assertEqual(report["duplicate_skus"], 1)
        self.assertEqual(report["invalid_products"], 1)
        self.assertEqual(report["conflict_reasons"]["duplicate_web_sku"], 1)
        self.assertEqual(enriched.catalog.loc[0, "product_category"], "")

    def test_article_or_known_brand_conflict_cannot_overwrite_fields(self):
        self.save([product(supplier_sku="DIFFERENT"),
                   product("00456_", supplier_sku="ATN000105", brand="IEK")])
        enriched = enrich_catalog(dataset(), self.path)
        report = enriched.metadata["catalog_enrichment"]
        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["conflicts"], 2)
        self.assertEqual(report["conflict_reasons"]["supplier_sku"], 1)
        self.assertEqual(report["conflict_reasons"]["brand"], 1)
        self.assertTrue(enriched.catalog.product_category.eq("").all())
        self.assertIn("неоднозначных сопоставлений", enriched.warnings[-1])

    def test_missing_corrupt_disabled_and_synthetic_fall_back(self):
        source = dataset()
        missing = enrich_catalog(source, self.path)
        self.assertEqual(missing.metadata["catalog_enrichment"]["status"], "missing")
        self.assertEqual(len(missing.warnings), 2)
        self.path.write_text("{broken", encoding="utf-8")
        corrupt = enrich_catalog(source, self.path)
        self.assertEqual(corrupt.metadata["catalog_enrichment"]["status"], "invalid")
        disabled = enrich_catalog(source, self.path, enabled=False)
        self.assertEqual(disabled.metadata["catalog_enrichment"]["status"], "disabled")
        self.assertEqual(disabled.warnings, source.warnings)
        source.source = "synthetic"
        self.assertIs(enrich_catalog(source, self.path), source)
        self.assertNotIn("catalog_enrichment", source.metadata)

    def test_crawl_metadata_exposes_only_validated_counts_for_current_assortment(self):
        self.save([product()], crawl={
            "coverage": "partial", "quarantined_skus": ["00456_", "00456_", "NOT-IN-CATALOG", 456, " bad "],
            "failures": [{"error": "Untrusted text", "url": "https://untrusted.example"}, "Other error"],
            "unsafe_extra": "Do not copy this into API",
        })
        enriched = enrich_catalog(dataset(), self.path)
        report = enriched.metadata["catalog_enrichment"]
        self.assertEqual(report["crawl_quarantined"], 1)
        self.assertEqual(report["crawl_failures"], 2)
        self.assertEqual(report["crawl_coverage"], "partial")
        self.assertNotIn("crawl", report)
        self.assertNotIn("unsafe_extra", report)
        self.assertNotIn("Untrusted text", json.dumps(report))
        self.assertIn("исключено 1", enriched.warnings[-1])
        self.save([product()], crawl={"coverage": {"bad": "value"}, "quarantined_skus": "00456_", "failures": {"error": "bad"}})
        report = enrich_catalog(dataset(), self.path).metadata["catalog_enrichment"]
        self.assertEqual(report["crawl_quarantined"], 0)
        self.assertEqual(report["crawl_failures"], 0)
        self.assertIsNone(report["crawl_coverage"])

    def test_rejects_invalid_schema_urls_types_and_attributes(self):
        for url in ("http://ekt.kz/catalog/x/", "https://ekt.kz.evil.test/catalog/x/",
                    "javascript:alert(1)", "https://evil@ekt.kz/catalog/x/",
                    "https://ekt.kz/catalog/../profile/", "https://ekt.kz/profile/"):
            with self.subTest(url=url):
                self.save([product(product_url=url)])
                self.assertEqual(enrich_catalog(dataset(), self.path).metadata["catalog_enrichment"]["invalid_products"], 1)
        for changes in ({"sku": 123}, {"product_attributes": {"Диаметр": 110}},
                        {"fetched_at": "bad"}, {"brand": ["IEK"]}, {"product_category": None}):
            with self.subTest(changes=changes):
                self.save([product(**changes)])
                self.assertEqual(enrich_catalog(dataset(), self.path).metadata["catalog_enrichment"]["invalid_products"], 1)
        self.save([product()], schema_version=2)
        self.assertEqual(enrich_catalog(dataset(), self.path).metadata["catalog_enrichment"]["status"], "invalid")
        self.save([product()], source="https://example.com")
        self.assertEqual(enrich_catalog(dataset(), self.path).metadata["catalog_enrichment"]["status"], "invalid")

    def test_new_snapshot_is_read_after_cached_excel_load(self):
        root = Path(self.temp.name) / "sources"
        fixture_sources(root)
        source = ExcelDataSource(root)
        settings = Settings()
        settings.ekt_catalog_path = str(self.path)
        settings.ekt_catalog_enabled = True
        with patch("app.config.get_settings", return_value=settings):
            self.save([product("0001_", supplier_sku="IEK-01", product_category="Первый раздел")])
            first = source.load()
            self.assertEqual(first.catalog.set_index("sku").loc["0001_", "product_category"], "Первый раздел")
            self.save([product("0001_", supplier_sku="IEK-01", product_category="Новый раздел")])
            second = source.load()
            self.assertEqual(second.catalog.set_index("sku").loc["0001_", "product_category"], "Новый раздел")
            self.path.unlink()
            missing = source.load()
            self.assertTrue(missing.catalog.product_category.eq("").all())
            self.assertEqual(missing.metadata["catalog_enrichment"]["status"], "missing")
        for field in ("sales", "stock", "in_transit", "sku_suppliers"):
            assert_frame_equal(getattr(first, field), getattr(second, field))

    def test_paths_and_csv_identifiers_preserve_leading_zeroes(self):
        with patch.dict(os.environ, {"EKT_CATALOG_PATH": "data/ekt/catalog.json", "EKT_CATALOG_ENABLED": "false"}):
            settings = Settings()
            self.assertFalse(settings.ekt_catalog_enabled)
            self.assertEqual(Path(settings.ekt_catalog_path), PROJECT_ROOT / "data/ekt/catalog.json")
        path = Path(self.temp.name) / "sales.csv"
        path.write_text("sku,supplier_id,client_id,order_id,supplier_sku,warehouse,qty\n00123,0001,00002,00003,00004,0005,10\n00123_,0001,00002,00003,00004,0005,20\n", encoding="utf-8")
        rows = CsvDataSource(self.temp.name)._read("sales.csv")
        self.assertEqual(rows.sku.tolist(), ["00123", "00123_"])
        self.assertEqual(rows.iloc[0].supplier_id, "0001")
        self.assertEqual(rows.iloc[0].client_id, "00002")
        self.assertEqual(rows.iloc[0].order_id, "00003")
        self.assertEqual(rows.iloc[0].supplier_sku, "00004")
        self.assertEqual(rows.iloc[0].warehouse, "0005")
        self.assertEqual(rows.qty.sum(), 30)


if __name__ == "__main__":
    unittest.main()
