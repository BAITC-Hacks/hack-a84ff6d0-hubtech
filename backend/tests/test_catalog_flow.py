"""Enrichment filters must preserve accounting categories, forecast and snapshot export."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pandas as pd

from app.core.recommend import generate_recommendations
from app.data.enrichment import enrich_catalog
from app.main import app
from tests.test_calculation import dataset
from tests.test_catalog_enrichment import product, FETCHED_AT


class CatalogFlowTests(unittest.TestCase):
    def test_api_filters_and_snapshot_export_retain_reference_without_changing_demand(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ORDER_DB_PATH": str(Path(folder) / "orders.sqlite3")}):
            ds = dataset()
            ds.source = 'excel'
            ds.catalog = pd.DataFrame([dict(sku='S1', name='Название 1С', category='Внутренняя 3',
                                           supplier_id='SUP', supplier_sku='CTG12-110-K04-050-R', unit='м')])
            before = generate_recommendations(ds, explain=False)
            path = Path(folder) / 'catalog.json'
            path.write_text(json.dumps(dict(schema_version=1, source='https://ekt.kz', fetched_at=FETCHED_AT,
                                             products=[product('S1')]), ensure_ascii=False))
            enriched = enrich_catalog(ds, path)
            client = TestClient(app)
            with patch('app.api.routes._load', return_value=enriched):
                meta = client.get('/api/meta').json()
                self.assertEqual(meta['categories'], ['Внутренняя 3'])
                self.assertEqual(meta['product_categories'], ['Кабеленесущие системы'])
                response = client.post('/api/recommend', json={'explain': False, 'product_category': 'Кабеленесущие системы'})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                line = result['groups'][0]['lines'][0]
                self.assertEqual(line['recommended_qty'], before.groups[0].lines[0].recommended_qty)
                self.assertEqual(line['rationale'], before.groups[0].lines[0].rationale.model_dump(mode='json'))
                self.assertEqual(line['name'], 'Название 1С')
                self.assertEqual(line['category'], 'Внутренняя 3')
                self.assertEqual(line['product_attributes']['Диаметр'], '110 мм')
                excluded = client.post('/api/recommend', json={'explain': False, 'product_category': 'Другая группа'}).json()
                self.assertEqual(excluded['groups'], [])
            path.unlink()  # Reference changes cannot alter an already saved order.
            with patch('app.api.routes._load', side_effect=AssertionError('export cannot reload sources')):
                exported = client.post('/api/recommend/export', json={
                    'calculation_id': result['calculation_id'], 'approved_only': True,
                    'lines': [{'line_id': line['line_id'], 'quantity': line['recommended_qty'] + 1, 'approved': True}]})
            self.assertEqual(exported.status_code, 200)
            book = load_workbook(io.BytesIO(exported.content), data_only=True)
            values = list(book['Заказ поставщикам'].values)
            row = dict(zip(values[0], values[1]))
            self.assertEqual(row['Категория'], 'Внутренняя 3')
            self.assertEqual(row['Товарная группа ekt.kz'], 'Кабеленесущие системы')
            self.assertEqual(row['К заказу, ед'], line['recommended_qty'] + 1)
            self.assertEqual(row['Карточка ekt.kz'], line['product_url'])
            book.close()
