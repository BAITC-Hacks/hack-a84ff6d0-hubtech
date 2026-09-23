"""Public catalog extraction is bounded and preserves literal item identities."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scripts.sync_ekt_catalog import (
    BASE, RestrictedRedirect, main, merge_product, parse_page, parse_sitemap,
    public_url, robots_allowed, robots_rules,
)

URL = "https://ekt.kz/catalog/kabelenesushchie_sistemy/"
STAMP = "2026-09-23T12:00:00+00:00"


def listing(sku='00123_', path='/catalog/kabelenesushchie_sistemy/tube/', article='X1'):
    return f'''<h1>Трубы</h1><div class="product-card"><a href="{path}">
      <h3 class="product-title">Труба</h3></a><div class="product-article">Код товара {sku}</div>
      <div class="product-post_article">Арт. поставщика {article}</div></div>'''


def card(sku):
    return f'''<h1>Труба</h1><div class="tab_item_chars__item">
      <div class="tab_item_chars__item__name">Артикул:</div>
      <div class="tab_item_chars__item__value">{sku}</div></div>'''


def fake_reader(detail):
    sitemap = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{URL}</loc><priority>0.9</priority></url></urlset>'
    pages = {BASE + '/robots.txt': 'User-agent: *\nAllow: /', BASE + '/sitemap.xml': sitemap,
             URL: listing(), URL + 'tube/': detail}
    reader = Mock(requests=0, cache_hits=0, legacy_cache_hits=0, delay=1,
                  redirect=SimpleNamespace(rules=[]))

    def get(url, cacheable=True):
        result = pages[url]
        if isinstance(result, Exception):
            raise result
        return result, STAMP
    reader.get.side_effect = get
    return reader


class EktSyncTests(unittest.TestCase):
    def test_listing_preserves_code_and_ignores_price_and_foreign_link(self):
        card = '''<div class="product-card"><a href="/catalog/kabelenesushchie_sistemy/tube/">
          <h3 class="product-title">Труба</h3></a>
          <div class="product-article">Код\n товара 00123_</div>
          <div class="product-article product-post_article">Арт. поставщика CTG-10</div>
          <div class="price">999 ₸</div></div>'''
        products, detail = parse_page('<h1>Трубы</h1>' + card + card.replace('/catalog/kabelenesushchie_sistemy/tube/', 'https://elsewhere.test/tube/'), URL, STAMP)
        self.assertFalse(detail)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['sku'], '00123_')
        self.assertEqual(products[0]['supplier_sku'], 'CTG-10')
        self.assertEqual(products[0]['product_subcategory'], 'Трубы')
        self.assertNotIn('price', products[0])

    def test_card_extracts_properties_and_keeps_zeroes(self):
        properties = {'Артикул': '0001_', 'Артикул поставщика': 'X1', 'Торговая марка': 'IEK',
                      'Категория': 'Трубы', 'Материал': 'ПНД', 'Метражный Товар': 'Да'}
        html = '<h1>Труба</h1>' + ''.join(
            f'<div class="tab_item_chars__item"><div class="tab_item_chars__item__name">{key}:</div>'
            f'<div class="tab_item_chars__item__value">{value}</div></div>' for key, value in properties.items())
        products, detail = parse_page(html, URL + 'tube/', STAMP)
        self.assertTrue(detail)
        self.assertEqual(products[0]['sku'], '0001_')
        self.assertEqual(products[0]['brand'], 'IEK')
        self.assertEqual(products[0]['product_attributes'], {'Материал': 'ПНД', 'Метражный Товар': 'Да'})

    def test_robots_wildcards_and_allow_precedence(self):
        rules = robots_rules('User-agent: *\nDisallow: */arkhiv/*\nDisallow: /catalog/private/\nAllow: /catalog/private/open/\n')
        self.assertFalse(robots_allowed(URL + 'arkhiv/old/', rules))
        self.assertFalse(robots_allowed('https://ekt.kz/catalog/private/item/', rules))
        self.assertTrue(robots_allowed('https://ekt.kz/catalog/private/open/item/', rules))
        self.assertTrue(robots_allowed(URL, rules))

    def test_no_search_queries_private_paths_or_foreign_redirect_targets(self):
        for url in ('http://ekt.kz/catalog/a/', 'https://ekt.kz.evil.test/catalog/a/',
                    URL + '?PAGEN_1=2', 'https://ekt.kz/personal/', 'https://x@ekt.kz/catalog/a/',
                    'https://ekt.kz:999/catalog/a/', 'https://ekt.kz/catalog/a/#b'):
            with self.subTest(url=url):
                self.assertFalse(public_url(url))
        self.assertTrue(public_url(URL))

    def test_sitemap_limits_scope_to_category_candidates(self):
        xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
            f'<url><loc>{url}</loc><priority>{priority}</priority></url>' for url, priority in
            [(URL, '.9'), (URL + 'tube/', '0.8'), ('https://ekt.kz/news/', '0.9'),
             ('https://ekt.kz/catalog/novinki/', '0.9'), (URL + 'iek/', '0.9')]) + '</urlset>'
        self.assertEqual(parse_sitemap(xml), [URL + 'iek/'])
        with self.assertRaises(ValueError):
            parse_sitemap('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')

    def test_decoded_traversal_is_rejected_before_robots_and_fetch(self):
        rules = [(False, '/personal/'), (False, '/auth/')]
        for path in ('/catalog/../personal/', '/catalog/%2e%2e/auth/',
                     '/catalog/%252e%252e/auth/', '/catalog/a%2f..%2f..%2fpersonal/',
                     '/catalog/a/%5c../personal/', '/catalog/a/%00test/'):
            with self.subTest(path=path):
                self.assertFalse(public_url(BASE + path))
                self.assertFalse(robots_allowed(BASE + path, rules))

    def test_even_same_host_redirect_is_rejected_without_following(self):
        for target in (URL + 'different/', 'https://evil.example/', '/catalog/else/'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                RestrictedRedirect().redirect_request(None, None, 302, '', {}, target)

    def test_crosslinked_listing_uses_product_url_category(self):
        rows, _ = parse_page(listing(path='/catalog/kabel_provod/cable/'), URL, STAMP)
        self.assertEqual(rows[0]['product_category'], 'Кабель / Провод')
        self.assertEqual(rows[0]['product_subcategory'], '')
        self.assertEqual(rows[0]['discovery_url'], URL)

    def test_conflicting_duplicate_identity_is_quarantined_permanently(self):
        initial = parse_page(listing(), URL, STAMP)[0][0]
        for change in ({'product_url': URL + 'other/'}, {'supplier_sku': 'DIFFERENT'}):
            products, quarantined, failures = {}, set(), []
            merge_product(products, quarantined, failures, deepcopy(initial))
            merge_product(products, quarantined, failures, {**initial, **change})
            merge_product(products, quarantined, failures, deepcopy(initial))
            self.assertEqual(products, {})
            self.assertEqual(quarantined, {'00123_'})
            self.assertEqual(len(failures), 1)

    def test_same_identity_keeps_richer_details_and_known_article(self):
        initial = parse_page(listing(), URL, STAMP)[0][0]
        products, quarantined, failures = {}, set(), []
        merge_product(products, quarantined, failures, initial)
        detail = {**initial, 'source_kind': 'product', 'supplier_sku': '',
                  'product_attributes': {'Материал': 'ПНД'}}
        merge_product(products, quarantined, failures, detail)
        merge_product(products, quarantined, failures, initial)
        self.assertEqual(products['00123_']['source_kind'], 'product')
        self.assertEqual(products['00123_']['supplier_sku'], 'X1')
        self.assertEqual(products['00123_']['product_attributes'], {'Материал': 'ПНД'})
        self.assertFalse(quarantined)

    def run_sync(self, path, reader):
        dataset = SimpleNamespace(catalog={'sku': ['00123_']})
        with patch('scripts.sync_ekt_catalog.ExcelDataSource') as source, \
             patch('scripts.sync_ekt_catalog.Reader', return_value=reader), \
             patch('scripts.sync_ekt_catalog.get_settings', return_value=SimpleNamespace(data_dir='.')), \
             redirect_stdout(io.StringIO()):
            source.return_value.load.return_value = dataset
            return main(['--output', str(path), '--max-pages', '1', '--max-details', '1'])

    def test_confirmed_wrong_detail_sku_removes_inherited_and_listing_product(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'catalog.json'
            inherited = parse_page(listing(), URL, STAMP)[0][0]
            path.write_text(json.dumps({'schema_version': 1, 'products': [inherited]}))
            self.assertEqual(self.run_sync(path, fake_reader(card('OTHER'))), 0)
            snapshot = json.loads(path.read_text())
            self.assertEqual(snapshot['products'], [])
            self.assertEqual(snapshot['crawl']['quarantined_skus'], ['00123_'])
            self.assertIn('другой код', snapshot['crawl']['failures'][0]['error'])

    def test_conflicting_old_duplicates_are_not_silently_inherited(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'catalog.json'
            product = parse_page(listing(), URL, STAMP)[0][0]
            path.write_text(json.dumps({'schema_version': 1, 'products': [product, {**product, 'supplier_sku': 'BAD'}]}))
            self.assertEqual(self.run_sync(path, fake_reader(card('00123_'))), 0)
            snapshot = json.loads(path.read_text())
            self.assertEqual(snapshot['products'], [])
            self.assertEqual(snapshot['crawl']['quarantined_skus'], ['00123_'])

    def test_access_denial_aborts_without_replacing_previous_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'catalog.json'
            previous = json.dumps({'schema_version': 1, 'products': []})
            path.write_text(previous)
            for code in (401, 403, 429):
                reader = fake_reader(HTTPError(URL + 'tube/', code, 'denied', {}, None))
                with self.subTest(code=code), self.assertRaises(RuntimeError):
                    self.run_sync(path, reader)
                self.assertEqual(path.read_text(), previous)
