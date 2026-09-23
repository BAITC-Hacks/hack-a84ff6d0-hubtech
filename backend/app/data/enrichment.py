"""Attach validated public product descriptions to an existing internal catalog.

Only a local JSON snapshot is read. Scraped strings are descriptive data, never
instructions or inputs to financial/stock calculations. Exact internal SKU is
the only matching key in schema version 1; supplier article and known brand are
guards against an accidental match, not alternate lookup keys.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pandas as pd

from app.data.adapter import Dataset


PRODUCT_COLUMNS = (
    "product_category", "product_subcategory", "product_url",
    "product_attributes", "catalog_fetched_at", "product_brand",
)
_HOSTS = {"ekt.kz", "www.ekt.kz"}
_BRAND_SUPPLIERS = {
    "iek": "IEK", "иэк": "IEK", "iek group": "IEK", "иэк групп": "IEK",
    "itk": "IEK", "oni": "IEK", "generica": "IEK",
    "systeme electric": "SYSTEME", "system electric": "SYSTEME",
    "systemelectric": "SYSTEME", "systemeelectric": "SYSTEME",
    "schneider electric": "SYSTEME", "schneiderelectric": "SYSTEME",
    "систэм электрик": "SYSTEME", "шнайдер электрик": "SYSTEME",
}


def _text(value, *, required=False, limit=2000) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid text field")
    text = " ".join(value.split())
    if required and not text:
        raise ValueError("Required text field is blank")
    return text


def _sku(value) -> str:
    # Do not silently normalize web codes into an internal SKU. The crawler
    # must store the exact visible code, retaining leading zeros and suffixes.
    if not isinstance(value, str) or not value or len(value) > 250:
        raise ValueError("Invalid SKU")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Whitespace/control character in SKU")
    return value


def _timestamp(value) -> str:
    text = _text(value, required=True, limit=80)
    if "T" not in text and " " not in text:
        raise ValueError("Timestamp requires a time")
    datetime.fromisoformat(text.replace("Z", "+00:00"))
    return text


def _url(value, *, product=False) -> str:
    text = _text(value, required=True, limit=4000)
    parts = urlsplit(text)
    if (parts.scheme != "https" or parts.hostname not in _HOSTS
            or parts.username or parts.password or parts.port not in (None, 443)
            or any(char.isspace() for char in text) or "\\" in text):
        raise ValueError("URL must belong to HTTPS ekt.kz")
    path = unquote(parts.path)
    if product:
        if not path.startswith("/catalog/") or path == "/catalog/" or ".." in path.split("/"):
            raise ValueError("Expected a catalog product URL")
    elif path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Invalid catalog source")
    return text


def _product(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Product must be an object")
    result = {"sku": _sku(value.get("sku")),
              "supplier_sku": _text(value.get("supplier_sku")),
              "brand": _text(value.get("brand")),
              "name": _text(value.get("name"), required=True),
              "product_category": _text(value.get("product_category"), required=True),
              "product_subcategory": _text(value.get("product_subcategory")),
              "product_url": _url(value.get("product_url"), product=True),
              "fetched_at": _timestamp(value.get("fetched_at"))}
    attributes = value.get("product_attributes")
    if not isinstance(attributes, dict) or len(attributes) > 200:
        raise ValueError("Invalid product attributes")
    result["product_attributes"] = {
        _text(key, required=True, limit=250): _text(val, limit=10000)
        for key, val in attributes.items()
    }
    return result


def _base_text(value) -> str:
    return " ".join(str(value).split()) if pd.notna(value) else ""


def enrich_catalog(dataset: Dataset, path: str | Path, *, enabled: bool = True) -> Dataset:
    """Return a separate descriptive catalog, preserving every base data field.

    Missing/corrupt snapshots are optional-data failures: base calculations keep
    working and receive a visible warning. Invalid products and duplicate web
    SKUs are excluded; counters make partial coverage inspectable.
    """
    if dataset.source == "synthetic":
        return dataset
    catalog = dataset.catalog.copy(deep=True)
    for column in PRODUCT_COLUMNS:
        catalog[column] = ([{} for _ in range(len(catalog))]
                           if column == "product_attributes" else "")
    enriched = replace(dataset, catalog=catalog, metadata=deepcopy(dataset.metadata),
                       warnings=list(dataset.warnings))
    report = {
        "status": "disabled" if not enabled else "missing",
        "source": "https://ekt.kz", "fetched_at": None,
        "total_catalog": len(catalog), "web_products": 0, "valid_products": 0,
        "matched": 0, "unmatched": len(catalog), "conflicts": 0,
        "invalid_products": 0, "duplicate_skus": 0, "duplicate_products": 0,
        "unknown_brand_matches": 0,
        "crawl_quarantined": 0, "crawl_failures": 0, "crawl_coverage": None,
        "conflict_reasons": {"supplier_sku": 0, "brand": 0, "duplicate_web_sku": 0,
                             "duplicate_base_sku": 0},
    }
    enriched.metadata["catalog_enrichment"] = report
    if not enabled:
        return enriched
    if catalog.empty:
        report["status"] = "empty_catalog"
        return enriched
    try:
        with Path(path).open(encoding="utf-8") as snapshot:
            payload = json.load(snapshot)
    except FileNotFoundError:
        enriched.warnings.append("Каталог ekt.kz пока не загружен: дополнительные товарные категории и карточки недоступны.")
        return enriched
    except (OSError, UnicodeError, ValueError):
        report["status"] = "invalid"
        enriched.warnings.append("Не удалось прочитать локальный каталог ekt.kz; расчёт продолжен по исходным данным.")
        return enriched
    try:
        if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1 or not isinstance(payload.get("products"), list)):
            raise ValueError("Unsupported catalog schema")
        report["source"] = _url(payload.get("source"))
        report["fetched_at"] = _timestamp(payload.get("fetched_at"))
    except (TypeError, ValueError):
        report["status"] = "invalid"
        enriched.warnings.append("Формат локального каталога ekt.kz некорректен; расчёт продолжен по исходным данным.")
        return enriched

    products = payload["products"]
    report["web_products"] = len(products)
    # Expose bounded facts about the crawl, never arbitrary error text, URLs,
    # or instructions embedded in its diagnostics.
    crawl = payload.get("crawl")
    if isinstance(crawl, dict):
        quarantine = set()
        raw_quarantine = crawl.get("quarantined_skus")
        if isinstance(raw_quarantine, list):
            for sku in raw_quarantine:
                try:
                    quarantine.add(_sku(sku))
                except (TypeError, ValueError):
                    continue
        report["crawl_quarantined"] = len(quarantine & set(catalog["sku"]))
        failures = crawl.get("failures")
        report["crawl_failures"] = len(failures) if isinstance(failures, list) else 0
        coverage = crawl.get("coverage")
        if isinstance(coverage, str) and coverage in {"partial", "complete"}:
            report["crawl_coverage"] = coverage
        if report["crawl_quarantined"]:
            enriched.warnings.append(
                f"При сборе каталога ekt.kz исключено {report['crawl_quarantined']} "
                "позиций из текущего ассортимента из-за противоречивых кодов или артикулов."
            )
    # Count raw, well-formed SKU keys before validating other fields. An invalid
    # duplicate must not make its competing product appear uniquely identified.
    sku_counts = Counter()
    for row in products:
        try:
            sku_counts[_sku(row.get("sku"))] += 1
        except (AttributeError, TypeError, ValueError):
            pass
    duplicates = {sku for sku, count in sku_counts.items() if count > 1}
    report["duplicate_skus"] = len(duplicates)
    report["duplicate_products"] = sum(sku_counts[sku] for sku in duplicates)
    lookup = {}
    for row in products:
        try:
            product = _product(row)
        except (TypeError, ValueError):
            report["invalid_products"] += 1
            continue
        report["valid_products"] += 1
        if product["sku"] not in duplicates:
            lookup[product["sku"]] = product

    base_duplicates = set(catalog.loc[catalog["sku"].duplicated(keep=False), "sku"])
    for index, row in catalog.iterrows():
        sku = row["sku"]
        reasons = []
        if sku in duplicates:
            reasons.append("duplicate_web_sku")
        if sku in base_duplicates and sku in lookup:
            reasons.append("duplicate_base_sku")
        product = lookup.get(sku)
        if product:
            article = _base_text(row.get("supplier_sku"))
            if article and product["supplier_sku"] and article.casefold() != product["supplier_sku"].casefold():
                reasons.append("supplier_sku")
            brand_supplier = _BRAND_SUPPLIERS.get(product["brand"].casefold())
            base_supplier = _base_text(row.get("supplier_id"))
            if brand_supplier and base_supplier in {"IEK", "SYSTEME"} and base_supplier != brand_supplier:
                reasons.append("brand")
        if reasons:
            report["conflicts"] += 1
            for reason in reasons:
                report["conflict_reasons"][reason] += 1
            continue
        if not product:
            continue
        for column in ("product_category", "product_subcategory", "product_url", "product_attributes"):
            catalog.at[index, column] = deepcopy(product[column])
        catalog.at[index, "catalog_fetched_at"] = product["fetched_at"]
        catalog.at[index, "product_brand"] = product["brand"]
        report["matched"] += 1
        if product["brand"] and product["brand"].casefold() not in _BRAND_SUPPLIERS:
            report["unknown_brand_matches"] += 1
    report["unmatched"] = len(catalog) - report["matched"]
    report["status"] = ("partial" if report["invalid_products"] or report["duplicate_skus"] or report["crawl_quarantined"]
                        or report["conflicts"] or report["unmatched"] else "loaded")
    if report["conflicts"] or report["invalid_products"] or report["duplicate_skus"]:
        enriched.warnings.append(
            f"Каталог ekt.kz: неоднозначных сопоставлений — {report['conflicts']}, "
            f"некорректных карточек — {report['invalid_products']}, "
            f"повторяющихся кодов — {report['duplicate_skus']}. Эти карточки не применены."
        )
    return enriched
