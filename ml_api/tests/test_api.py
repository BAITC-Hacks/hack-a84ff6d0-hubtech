"""HTTP-контракт API: без загрузки реальных моделей и датасетов."""

import unittest

from fastapi.testclient import TestClient

from ml_api.app import create_app
from ml_api.service import ModelUnavailable, UnsupportedMonth


class FakeForecastService:
    """Минимальный сервис для проверки HTTP-границы отдельно от ML."""

    def __init__(self):
        self.load_calls = 0
        self.forecast_calls = 0
        self.last_request = None
        self.error = None

    def load(self):
        self.load_calls += 1

    def health(self):
        return {
            "status": "degraded",
            "models": {"IEK": "ready", "SYSTEME": "unavailable"},
        }

    def models(self):
        return {
            "models": [
                {
                    "supplier_id": "IEK",
                    "available": True,
                    "model": "boosting_squared",
                    "model_version": "test-version",
                    "data_as_of": "2026-09-23",
                    "forecast_month": "2026-09-01",
                    "trained_before": "2026-09-01",
                    "sku_count": 3,
                    "forecast_available": 2,
                    "min_history": 3,
                },
                {
                    "supplier_id": "SYSTEME",
                    "available": False,
                    "model": None,
                    "model_version": None,
                    "data_as_of": None,
                    "forecast_month": None,
                    "trained_before": None,
                    "sku_count": 0,
                    "forecast_available": 0,
                    "min_history": None,
                },
            ]
        }

    def forecast(self, request):
        self.forecast_calls += 1
        self.last_request = request
        if self.error is not None:
            raise self.error

        known = {
            "0001": ("м", "forecast", 12.5, 20),
            "cold": ("шт", "insufficient_history", None, 2),
            "zero": ("шт", "forecast", 0.0, 20),
        }
        items = []
        for sku in request.skus:
            unit, status, qty, history = known.get(
                sku, (None, "unknown_sku", None, None)
            )
            items.append(
                {
                    "sku": sku,
                    "unit": unit,
                    "status": status,
                    "predicted_qty": qty,
                    "history_observed_months": history,
                    "explanation": None,
                }
            )
        return {
            "supplier_id": request.supplier_id,
            "model": "boosting_squared",
            "model_version": "test-version",
            "data_as_of": "2026-09-23",
            "forecast_month": request.forecast_month.isoformat(),
            "trained_before": "2026-09-01",
            "forecast_scope": "full_month",
            "warnings": ["Тестовый прогноз полного месяца"],
            "items": items,
        }


class ForecastApiTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeForecastService()
        self.app = create_app(service=self.service)

    def payload(self, **changes):
        result = {
            "supplier_id": "IEK",
            "forecast_month": "2026-09-01",
            "skus": ["0001", "cold", "missing", "zero"],
        }
        result.update(changes)
        return result

    def test_creation_is_lazy_and_lifespan_loads_once(self):
        self.assertEqual(self.service.load_calls, 0)
        with TestClient(self.app) as client:
            self.assertEqual(self.service.load_calls, 1)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/v1/models").status_code, 200)
            for _ in range(2):
                response = client.post("/v1/forecast", json=self.payload())
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.service.load_calls, 1)
        self.assertEqual(self.service.load_calls, 1)

    def test_mixed_response_keeps_order_leading_zeros_and_zero_vs_null(self):
        with TestClient(self.app) as client:
            response = client.post("/v1/forecast", json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["supplier_id"], "IEK")
        self.assertEqual(result["forecast_month"], "2026-09-01")
        self.assertEqual(result["data_as_of"], "2026-09-23")
        self.assertEqual(result["trained_before"], "2026-09-01")
        self.assertEqual(result["forecast_scope"], "full_month")
        self.assertEqual(result["model"], "boosting_squared")
        self.assertEqual(result["model_version"], "test-version")
        self.assertEqual(result["warnings"], ["Тестовый прогноз полного месяца"])
        items = result["items"]
        self.assertEqual([item["sku"] for item in items], self.payload()["skus"])
        self.assertEqual(items[0]["predicted_qty"], 12.5)
        self.assertEqual(items[0]["unit"], "м")
        self.assertEqual(items[0]["history_observed_months"], 20)
        self.assertEqual(items[1]["status"], "insufficient_history")
        self.assertIsNone(items[1]["predicted_qty"])
        self.assertEqual(items[1]["history_observed_months"], 2)
        self.assertEqual(items[2]["status"], "unknown_sku")
        self.assertIsNone(items[2]["predicted_qty"])
        self.assertIsNone(items[2]["unit"])
        self.assertIsNone(items[2]["history_observed_months"])
        self.assertEqual(items[3]["status"], "forecast")
        self.assertEqual(items[3]["predicted_qty"], 0.0)
        self.assertTrue(all(item["explanation"] is None for item in items))

    def test_health_and_inventory_expose_partial_availability(self):
        with TestClient(self.app) as client:
            health = client.get("/health")
            models = client.get("/v1/models")
        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(health.json(), self.service.health())
        self.assertEqual(models.status_code, 200, models.text)
        entries = {item["supplier_id"]: item for item in models.json()["models"]}
        self.assertTrue(entries["IEK"]["available"])
        self.assertEqual(entries["IEK"]["forecast_month"], "2026-09-01")
        self.assertFalse(entries["SYSTEME"]["available"])
        self.assertIsNone(entries["SYSTEME"]["model"])

    def test_skus_are_stripped_before_service_call(self):
        with TestClient(self.app) as client:
            response = client.post(
                "/v1/forecast", json=self.payload(skus=[" 0001 ", " zero\t"])
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.last_request.skus, ["0001", "zero"])
        self.assertEqual(
            [item["sku"] for item in response.json()["items"]], ["0001", "zero"]
        )

    def test_rejects_noncanonical_or_non_month_start_dates(self):
        invalid = [
            "2026-09-02", "2026-9-01", "2026-09-1", "01.09.2026",
            "2026-09-01T00:00:00", "2026-13-01", "2026-02-30", "",
            1788220800, None,
        ]
        with TestClient(self.app) as client:
            for month in invalid:
                with self.subTest(forecast_month=month):
                    response = client.post(
                        "/v1/forecast", json=self.payload(forecast_month=month)
                    )
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.service.forecast_calls, 0)

    def test_rejects_invalid_skus_and_duplicates_after_stripping(self):
        invalid = [
            [], [str(index) for index in range(501)], [123], [True], [None],
            [""], [" \t"], ["x" * 129], ["0001", "0001"],
            ["0001", " 0001 "], "0001",
        ]
        with TestClient(self.app) as client:
            for skus in invalid:
                with self.subTest(skus=str(skus)[:100]):
                    response = client.post("/v1/forecast", json=self.payload(skus=skus))
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.service.forecast_calls, 0)

    def test_accepts_maximum_batch_and_sku_length(self):
        skus = ["x" * 128] + [f"sku-{index}" for index in range(499)]
        with TestClient(self.app) as client:
            response = client.post("/v1/forecast", json=self.payload(skus=skus))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["sku"] for item in response.json()["items"]], skus)

    def test_rejects_unknown_supplier_and_extra_fields(self):
        payloads = [
            self.payload(supplier_id="OTHER"),
            self.payload(supplier_id="iek"),
            self.payload(supplier_id=None),
            self.payload(horizon_days=35),
        ]
        with TestClient(self.app) as client:
            for payload in payloads:
                with self.subTest(payload=payload):
                    response = client.post("/v1/forecast", json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.service.forecast_calls, 0)

    def test_requires_all_request_fields(self):
        with TestClient(self.app) as client:
            for field in ("supplier_id", "forecast_month", "skus"):
                with self.subTest(field=field):
                    payload = self.payload()
                    del payload[field]
                    response = client.post("/v1/forecast", json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.service.forecast_calls, 0)

    def test_unavailable_model_is_503(self):
        self.service.error = ModelUnavailable("Модель SYSTEME недоступна")
        with TestClient(self.app) as client:
            response = client.post(
                "/v1/forecast", json=self.payload(supplier_id="SYSTEME")
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {
            "detail": {
                "code": "model_unavailable",
                "message": "Модель SYSTEME недоступна",
            }
        })

    def test_unsupported_month_is_422(self):
        self.service.error = UnsupportedMonth("Доступен только 2026-09-01")
        with TestClient(self.app) as client:
            response = client.post(
                "/v1/forecast", json=self.payload(forecast_month="2026-10-01")
            )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json(), {
            "detail": {
                "code": "unsupported_month",
                "message": "Доступен только 2026-09-01",
            }
        })


if __name__ == "__main__":
    unittest.main()
