"""Проверки HTTP-клиента и использования ML в формуле заказа."""
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pandas as pd
from fastapi.testclient import TestClient

from app.core.ml_client import ForecastBatch, fetch_forecasts
from app.core.recommend import generate_recommendations
from app.main import app
from tests.test_calculation import dataset

AS_OF = date(2026, 9, 23)
HTTP_CLIENT = httpx.Client


def payload(skus, **changes):
    result = dict(supplier_id='IEK', model='boosting_squared', model_version='v1',
                  data_as_of='2026-09-23', forecast_month='2026-09-01',
                  trained_before='2026-09-01', forecast_scope='full_month',
                  items=[dict(sku=sku, unit='шт', status='forecast', predicted_qty=600.) for sku in skus])
    result.update(changes)
    return result


def fixture(warehouses=('A',)):
    ds = dataset(as_of=AS_OF, warehouses=warehouses)
    ds.source = 'excel'
    ds.suppliers['supplier_id'] = 'IEK'
    ds.sku_suppliers['supplier_id'] = 'IEK'
    ds.catalog = pd.DataFrame([dict(sku='S1', name='Товар', category='C', unit='шт', supplier_id='IEK')])
    return ds


def settings():
    return SimpleNamespace(ml_api_enabled=True, ml_api_url='http://ml.test', ml_api_timeout=1,
                           service_level=.5, review_period_days=2)


class ClientTests(unittest.TestCase):
    def fetch(self, handler, requests=None):
        transport = httpx.MockTransport(handler)
        with patch('app.core.ml_client.httpx.Client', side_effect=lambda **kw: HTTP_CLIENT(transport=transport, **kw)):
            return fetch_forecasts(requests or {'IEK': ['001']}, AS_OF, settings())

    def test_batches_and_supplier_isolation(self):
        sizes = []
        def handler(request):
            data = json.loads(request.content)
            sizes.append(len(data['skus']))
            if data['supplier_id'] == 'SYSTEME':
                return httpx.Response(503)
            return httpx.Response(200, json=payload(data['skus']))
        predictions, warnings = self.fetch(handler, {'IEK': [str(i) for i in range(501)], 'SYSTEME': ['001']})
        self.assertEqual(sizes, [500, 1, 1])
        self.assertEqual(len(predictions), 501)
        self.assertEqual(len(warnings), 1)
        self.assertIn('SYSTEME', warnings[0])

    def test_invalid_metadata_and_items_trigger_explicit_fallback(self):
        cases = [dict(supplier_id='SYSTEME'), dict(data_as_of='2026-09-22'),
                 dict(trained_before='2026-08-01'), dict(forecast_month='2026-10-01'),
                 dict(forecast_scope='remaining_month'), dict(items=[]),
                 dict(items=[dict(sku='001', unit='шт', status='forecast', predicted_qty=None)]),
                 dict(items=[dict(sku='001', unit='шт', status='forecast', predicted_qty=-1)])]
        for changes in cases:
            with self.subTest(changes=changes):
                predictions, warnings = self.fetch(lambda req: httpx.Response(200, json=payload(['001'], **changes)))
                self.assertFalse(predictions)
                self.assertEqual(len(warnings), 1)

    def test_timeout_is_not_zero_demand(self):
        def handler(request):
            raise httpx.ReadTimeout('timeout', request=request)
        predictions, warnings = self.fetch(handler)
        self.assertEqual(predictions, {})
        self.assertTrue(warnings)

    def test_model_version_change_discards_entire_supplier(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            data = json.loads(request.content)
            return httpx.Response(200, json=payload(data['skus'], model_version=str(count)))
        predictions, warnings = self.fetch(handler, {'IEK': [str(i) for i in range(501)]})
        self.assertFalse(predictions)
        self.assertTrue(warnings)


class RecommendationIntegrationTests(unittest.TestCase):
    def calculate(self, ds=None, result=None, **kwargs):
        ds = fixture() if ds is None else ds
        batch = ForecastBatch.model_validate(payload(['S1']) if result is None else result)
        fetched = {('IEK', 'S1'): (batch.items[0], batch)}
        with patch('app.core.recommend.get_settings', return_value=settings()), \
             patch('app.core.recommend.fetch_forecasts', return_value=(fetched, [])) as client:
            response = generate_recommendations(ds, explain=False, **kwargs)
        return response, client

    def test_monthly_forecast_drives_actual_order_formula(self):
        response, client = self.calculate()
        line = response.groups[0].lines[0]
        self.assertEqual(line.rationale.forecast_source, 'ml_api')
        self.assertEqual(line.rationale.forecast_model_version, 'v1')
        self.assertEqual(line.rationale.forecast_monthly_qty, 600)
        self.assertEqual(line.rationale.avg_daily_demand, 20)
        self.assertEqual(line.rationale.forecast_demand, 140)  # 600 / 30 * (5 + 2)
        self.assertEqual(line.recommended_qty, 140)
        self.assertEqual(line.rationale.excluded_bulk_units, 0)
        self.assertEqual(line.rationale.lost_demand_uplift, 0)
        self.assertTrue(any('приближённо' in w for w in line.warnings))
        client.assert_called_once()

    def test_unknown_and_unit_mismatch_use_labeled_legacy_forecast(self):
        for item in [dict(sku='S1', unit='шт', status='insufficient_history', predicted_qty=None),
                     dict(sku='S1', unit='м', status='forecast', predicted_qty=600)]:
            with self.subTest(item=item):
                response, _ = self.calculate(result=payload(['S1'], items=[item]))
                line = response.groups[0].lines[0]
                self.assertEqual(line.rationale.forecast_source, 'legacy')
                self.assertEqual(line.recommended_qty, 70)
                self.assertTrue(any('прежний алгоритм' in w for w in line.warnings))
                self.assertEqual(response.data_quality['ml_api']['fallback'], 1)

    def test_real_zero_is_used_and_creates_no_order(self):
        result = payload(['S1'])
        result['items'][0]['predicted_qty'] = 0
        response, _ = self.calculate(result=result)
        self.assertEqual(response.groups, [])
        self.assertEqual(response.data_quality['ml_api']['used'], 1)

    def test_multiple_warehouses_do_not_duplicate_monthly_total(self):
        response, client = self.calculate(ds=fixture(('A', 'B')), warehouse='A')
        client.assert_not_called()
        self.assertEqual(response.groups[0].lines[0].rationale.forecast_source, 'legacy')
        self.assertEqual(response.groups[0].lines[0].recommended_qty, 70)

    def test_synthetic_data_does_not_call_real_models(self):
        ds = fixture()
        ds.source = 'synthetic'
        response, client = self.calculate(ds=ds)
        client.assert_not_called()
        self.assertEqual(response.groups[0].lines[0].rationale.forecast_source, 'legacy')

    def test_existing_recommend_endpoint_returns_ml_provenance(self):
        batch = ForecastBatch.model_validate(payload(['S1']))
        with patch('app.api.routes._load', return_value=fixture()), \
             patch('app.api.routes.save_snapshot', side_effect=lambda response: response), \
             patch('app.core.recommend.get_settings', return_value=settings()), \
             patch('app.core.recommend.fetch_forecasts', return_value=({('IEK', 'S1'): (batch.items[0], batch)}, [])):
            with TestClient(app) as client:
                response = client.post('/api/recommend', json={'explain': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['groups'][0]['lines'][0]['rationale']['forecast_source'], 'ml_api')


if __name__ == '__main__':
    unittest.main()
