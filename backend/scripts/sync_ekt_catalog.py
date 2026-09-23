"""Collect a bounded public ekt.kz reference snapshot for existing 1C SKUs.

Run from backend: python -m scripts.sync_ekt_catalog --help.
Only exact SKU matches are retained. No sales, quantities, prices or customer
data are sent to the site. Query/search/private URLs are never requested.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.config import PROJECT_ROOT, get_settings
from app.data.excel import ExcelDataSource, normalize_code, normalize_text

BASE = "https://ekt.kz"
USER_AGENT = "EKT-Replenishment-Catalog/1.0 (public product reference; bounded requests)"
SECTIONS = {
    "kabel_provod": "Кабель / Провод",
    "svetilniki_lampy": "Светильники / Лампы",
    "nizkovoltnaya_apparatura": "Низковольтная аппаратура",
    "kabelenesushchie_sistemy": "Кабеленесущие системы",
    "izdeliya_dlya_montazha_i_instrument": "Изделия для монтажа и инструмент",
    "prochee_oborudovanie": "Прочее оборудование",
    "shkafy_shchity": "Шкафы / Щиты",
    "rozetki_vyklyuchateli_korobki": "Розетки/Выключатели/Коробки",
    "avtomatizatsiya": "Автоматизация",
    "videonablyudenie_skud_signalizatsiya": "Видеонаблюдение / СКУД / Сигнализация",
    "instrument_kip": "Инструмент / КИП",
}


def public_url(url: str) -> bool:
    try:
        value = urlsplit(url)
        path = value.path
        for _ in range(10):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        else:
            return False
        if (any(part in {".", ".."} for part in path.split("/"))
                or "\\" in path or any(char.isspace() or ord(char) < 32 for char in path)):
            return False
        return (value.scheme == "https" and value.netloc == "ekt.kz"
                and not value.query and not value.fragment and "\\" not in url
                and (value.path.startswith("/catalog/") or value.path in {"/robots.txt", "/sitemap.xml"}))
    except ValueError:
        return False


def section(url: str) -> str:
    parts = urlsplit(url).path.split("/")
    return SECTIONS.get(parts[2], "") if len(parts) > 2 else ""


def robots_rules(text: str) -> list[tuple[bool, str]]:
    """Read the wildcard group, including '*' and '$' patterns used by EKT."""
    groups, agents, rules = [], [], []
    for line in text.splitlines():
        field, sep, value = line.split("#", 1)[0].partition(":")
        if not sep:
            continue
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if rules:
                groups.append((agents, rules)); agents, rules = [], []
            agents.append(value.lower())
        elif field in {"allow", "disallow"} and value:
            rules.append((field == "allow", value))
    groups.append((agents, rules))
    specific = [r for a, rs in groups if any(n != "*" and n in USER_AGENT.lower() for n in a) for r in rs]
    return specific or [r for a, rs in groups if "*" in a for r in rs]


def robots_allowed(url: str, rules: list[tuple[bool, str]]) -> bool:
    if not public_url(url):
        return False
    target = unquote(urlsplit(url).path)
    matches = []
    for allow, pattern in rules:
        final = pattern.endswith("$")
        body = pattern[:-1] if final else pattern
        expression = "^" + ".*".join(re.escape(part) for part in body.split("*")) + ("$" if final else "")
        if re.search(expression, target):
            matches.append((len(body.replace("*", "")), allow))
    return max(matches, default=(0, True))[1]


def parse_sitemap(content: str) -> list[str]:
    root = ElementTree.fromstring(content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # The site's priority=.9 entries describe category pages. Special offers,
    # news, translated and archive branches do not define a product taxonomy.
    urls = []
    for item in root.findall("s:url", ns):
        url = item.findtext("s:loc", "", ns)
        priority = item.findtext("s:priority", "", ns)
        if priority == "0.9" and public_url(url) and section(url):
            urls.append(url)
    if not urls:
        raise ValueError("Карта сайта не содержит ожидаемых разделов; предыдущий справочник сохранён")
    return sorted(set(urls), key=lambda u: (
        not bool(re.search(r"iek|schneider|systeme", u)),
        u.count("/"), u))


def parse_page(content: str, url: str, fetched_at: str) -> tuple[list[dict], bool]:
    soup = BeautifulSoup(content, "html.parser")
    title = soup.select_one("h1")
    title = normalize_text(title.get_text(" ", strip=True)) if title else ""
    group = section(url)
    properties = {}
    for item in soup.select(".tab_item_chars__item"):
        name = item.select_one(".tab_item_chars__item__name")
        value = item.select_one(".tab_item_chars__item__value")
        if name and value:
            properties[normalize_text(name.get_text()).rstrip(":")] = normalize_text(value.get_text(" ", strip=True))
    sku = normalize_code(properties.get("Артикул"))
    if sku and title and group:
        attributes = {key: value for key, value in properties.items()
                      if key not in {"Артикул", "Артикул поставщика", "Торговая марка", "Категория", "Новинка"}}
        return [{"sku": sku, "supplier_sku": normalize_code(properties.get("Артикул поставщика")),
                 "brand": properties.get("Торговая марка", ""), "name": title,
                 "product_category": group, "product_subcategory": properties.get("Категория", ""),
                 "product_url": url, "product_attributes": attributes,
                 "fetched_at": fetched_at, "source_kind": "product", "discovery_url": url}], True
    products = []
    for card in soup.select(".product-card"):
        code = card.select_one(".product-article:not(.product-post_article)")
        heading = card.select_one(".product-title")
        link = heading.find_parent("a") if heading else None
        article = card.select_one(".product-post_article")
        if not code or not heading or not link:
            continue
        sku = normalize_code(re.sub(r"^Код\s+товара\s*", "", code.get_text(" ", strip=True)))
        product_url = urljoin(BASE, link.get("href", ""))
        product_group = section(product_url)
        if not sku or not public_url(product_url) or not product_group:
            continue
        products.append({"sku": sku,
                         "supplier_sku": normalize_code(re.sub(r"^Арт\.\s*поставщика\s*", "", article.get_text(" ", strip=True))) if article else "",
                         "brand": "", "name": normalize_text(heading.get_text(" ", strip=True)),
                         "product_category": product_group,
                         "product_subcategory": title if product_group == group and title != group else "",
                         "product_url": product_url, "product_attributes": {},
                         "fetched_at": fetched_at, "source_kind": "listing", "discovery_url": url})
    return products, False


class RestrictedRedirect(HTTPRedirectHandler):
    def __init__(self):
        self.rules = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Перенаправление отклонено: требуется прямая ссылка публичного каталога")


class Reader:
    def __init__(self, delay: float, timeout: float, cache: Path, refresh: bool):
        self.delay, self.timeout, self.cache, self.refresh = max(0.5, delay), timeout, cache, refresh
        self.cache.mkdir(parents=True, exist_ok=True)
        self.last_request = 0.0
        self.requests = self.cache_hits = self.legacy_cache_hits = 0
        self.redirect = RestrictedRedirect()
        self.opener = build_opener(self.redirect)

    def get(self, url: str, cacheable=True) -> tuple[str, str]:
        if not public_url(url) or not robots_allowed(url, self.redirect.rules):
            raise ValueError("Адрес не разрешён для сбора справочника")
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if cacheable and not self.refresh and path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
            saved = json.loads(path.read_text())
            if saved["url"] == url and saved.get("final_url", url) == url:
                self.cache_hits += 1
                if "final_url" not in saved:
                    # Retain the existing bounded crawl, but do not claim its
                    # old cache records prove that no redirect occurred.
                    self.legacy_cache_hits += 1
                return saved["body"], saved["fetched_at"]
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.requests += 1
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/plain"})
        with self.opener.open(request, timeout=self.timeout) as response:
            if response.geturl() != url:
                raise ValueError("Фактический адрес страницы не совпадает с запрошенным")
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Страница превышает допустимый размер")
        body, stamp = raw.decode("utf-8", errors="replace"), datetime.now(timezone.utc).isoformat(timespec="seconds")
        if cacheable:
            path.write_text(json.dumps({"url": url, "final_url": url, "body": body, "fetched_at": stamp}, ensure_ascii=False))
        return body, stamp


def quarantine(products: dict, quarantined: set, failures: list, sku: str, url: str, reason: str):
    products.pop(sku, None)
    quarantined.add(sku)
    failures.append({"sku": sku, "url": url, "error": reason})


def merge_product(products: dict, quarantined: set, failures: list, item: dict):
    """Same card in many listings is fine; conflicting identity never wins."""
    sku = item["sku"]
    if sku in quarantined:
        return
    previous = products.get(sku)
    if previous:
        old_article = normalize_code(previous.get("supplier_sku")).casefold()
        new_article = normalize_code(item.get("supplier_sku")).casefold()
        if (previous.get("product_url") != item.get("product_url")
                or (old_article and new_article and old_article != new_article)):
            quarantine(products, quarantined, failures, sku, item["product_url"],
                       "Один код соответствует разным карточкам или артикулам поставщика")
            return
    if not previous or item.get("source_kind") == "product" or previous.get("source_kind") != "product":
        if previous and previous.get("supplier_sku") and not item.get("supplier_sku"):
            item = {**item, "supplier_sku": previous["supplier_sku"]}
        products[sku] = item


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=120, help="Лимит страниц разделов (1..650)")
    parser.add_argument("--max-details", type=int, default=80, help="Лимит карточек совпавших SKU (0..1000)")
    parser.add_argument("--delay", type=float, default=1.0, help="Интервал запросов, не менее 0.5 сек")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/ekt/catalog.json")
    parser.add_argument("--refresh", action="store_true", help="Повторно запросить страницы вместо недельного кеша")
    args = parser.parse_args(argv)
    if not 1 <= args.max_pages <= 650 or not 0 <= args.max_details <= 1000 or args.timeout <= 0:
        parser.error("Недопустимые лимиты или timeout")
    settings = get_settings()
    ds = ExcelDataSource(settings.data_dir).load()
    targets = set(ds.catalog["sku"])
    reader = Reader(args.delay, args.timeout, PROJECT_ROOT / "backend/.cache/ekt", args.refresh)
    reader.redirect.rules = robots_rules(reader.get(BASE + "/robots.txt", cacheable=False)[0])
    urls = parse_sitemap(reader.get(BASE + "/sitemap.xml")[0])
    products = {}
    failures, observed, quarantined, details_done = [], set(), set(), 0
    if args.output.exists():
        old = json.loads(args.output.read_text())
        if old.get("schema_version") != 1:
            raise ValueError("Неизвестная версия предыдущего справочника")
        quarantined.update(set(old.get("crawl", {}).get("quarantined_skus", [])) & targets)
        for item in old["products"]:
            if item["sku"] in targets:
                merge_product(products, quarantined, failures, item)
    sections_done = 0

    def read(url):
        try:
            html, stamp = reader.get(url)
            return parse_page(html, url, stamp)
        except HTTPError as error:
            if error.code in {401, 403, 429}:
                raise RuntimeError(f"Сайт ограничил доступ (HTTP {error.code}); сбор остановлен") from error
            failures.append({"url": url, "error": f"HTTP {error.code}"})
        except (URLError, TimeoutError, ValueError) as error:
            failures.append({"url": url, "error": type(error).__name__})
        return [], False

    for url in urls[:args.max_pages]:
        if not robots_allowed(url, reader.redirect.rules):
            continue
        found, _ = read(url)
        sections_done += 1
        for item in found:
            if item["sku"] not in targets:
                continue
            observed.add(item["sku"])
            merge_product(products, quarantined, failures, item)
        if sections_done % 10 == 0:
            print(f"Разделы: {sections_done}; совпадений кода: {len(observed)}; ошибок: {len(failures)}", flush=True)
    candidates = sorted((p for p in products.values() if p["sku"] in observed),
                        key=lambda p: (p.get("source_kind") == "product", p["sku"]))
    for previous in candidates[:args.max_details]:
        found, is_detail = read(previous["product_url"])
        details_done += 1
        if is_detail and len(found) == 1 and found[0]["sku"] == previous["sku"]:
            merge_product(products, quarantined, failures, found[0])
        elif is_detail and found:
            quarantine(products, quarantined, failures, previous["sku"], previous["product_url"],
                       "Карточка содержит другой код: " + str(found[0]["sku"]))
        else:
            failures.append({"url": previous["product_url"], "error": "Карточка не подтвердила исходный код"})
        if details_done % 20 == 0:
            print(f"Карточки: {details_done}; всего сохранено: {len(products)}", flush=True)
    if not observed:
        raise ValueError("Совпадений нет; предыдущий справочник не заменён")
    snapshot = {"schema_version": 1, "source": BASE,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "products": sorted(products.values(), key=lambda p: p["sku"]),
                "crawl": {"scope": "existing_excel_skus", "coverage": "partial",
                          "target_count": len(targets), "observed_this_run": len(observed),
                          "category_pages_available": len(urls), "category_pages_read": sections_done,
                          "detail_pages_read": details_done, "http_requests": reader.requests,
                          "cache_hits": reader.cache_hits, "max_pages": args.max_pages,
                          "legacy_cache_hits": reader.legacy_cache_hits,
                          "quarantined_skus": sorted(quarantined),
                          "max_details": args.max_details, "delay_seconds": reader.delay,
                          "robots_checked": True, "failures": failures}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, args.output)
    print(json.dumps({"output": str(args.output), "products": len(products),
                      "observed": len(observed), "quarantined": len(quarantined),
                      "failures": len(failures), "http_requests": reader.requests,
                      "cache_hits": reader.cache_hits}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
