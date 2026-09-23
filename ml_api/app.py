"""Запуск: uvicorn ml_api.app:app --host 127.0.0.1 --port 8020."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ml_api.schemas import (
    ErrorResponse, ForecastRequest, ForecastResponse, HealthResponse, ModelsResponse,
    ValidationErrorResponse,
)
from ml_api.service import ForecastService, ModelUnavailable, UnsupportedMonth


def create_app(service: ForecastService | None = None, root: Path | None = None) -> FastAPI:
    forecast_service = service if service is not None else ForecastService(root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # До принятия запросов проверяем артефакты и рассчитываем неизменный снимок.
        forecast_service.load()
        app.state.forecast_service = forecast_service
        yield

    app = FastAPI(
        title="Supplier Demand ML API",
        version="1.0.0",
        description="Прогноз продаж за полный месяц в единице товара. IEK и Systeme Electric.",
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse, summary="Доступность моделей")
    def health():
        return forecast_service.health()

    @app.get("/v1/models", response_model=ModelsResponse, summary="Модели и поддерживаемый месяц")
    def models():
        return forecast_service.models()

    @app.post(
        "/v1/forecast", response_model=ForecastResponse,
        summary="Прогноз по кодам товаров одного поставщика",
        responses={503: {"model": ErrorResponse, "description": "Модель недоступна"},
                   422: {"model": ErrorResponse | ValidationErrorResponse,
                         "description": "Невалидный запрос или неподдерживаемый месяц"}},
    )
    def forecast(request: ForecastRequest):
        try:
            return forecast_service.forecast(request)
        except ModelUnavailable as exc:
            raise HTTPException(503, detail={"code": "model_unavailable", "message": str(exc)}) from exc
        except UnsupportedMonth as exc:
            raise HTTPException(422, detail={"code": "unsupported_month", "message": str(exc)}) from exc

    return app


app = create_app()
