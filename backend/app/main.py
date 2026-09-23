"""Internal replenishment service, served behind a same-origin HTTPS proxy."""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.storage import migrate


# Uvicorn configures its own loggers, not the application's INFO level.
# Own a dedicated handler so request IDs are available with --no-access-log.
application_log = logging.getLogger("umytpa")
application_log.setLevel(logging.INFO)
application_log.propagate = False
if not application_log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    application_log.addHandler(handler)


@asynccontextmanager
async def lifespan(_app):
    migrate()
    yield


development = os.getenv("APP_ENV", "production").lower() == "development"
app = FastAPI(title="ЭКТ · Автозаказы поставщикам", version="1.0.0", lifespan=lifespan,
              docs_url="/docs" if development else None, redoc_url=None,
              openapi_url="/openapi.json" if development else None)

origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
if "*" in origins:
    raise RuntimeError("CORS_ORIGINS must contain explicit trusted origins, never '*'")
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH"],
                       allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Prefer"])


@app.middleware("http")
async def response_headers(request: Request, call_next):
    supplied_id = request.headers.get("X-Request-ID", "")
    request_id = supplied_id if re.fullmatch(r"[A-Za-z0-9-]{1,64}", supplied_id) else uuid4().hex
    request.state.request_id = request_id
    started = time.monotonic()
    logger = logging.getLogger("umytpa.requests")
    try:
        response = await call_next(request)
    except Exception as error:
        # Handle here: re-raising through ServerErrorMiddleware makes uvicorn
        # print the original exception, which may contain private source data.
        response = await unexpected_error(request, error)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    logger.info("request_id=%s method=%s route=%s status=%s duration_ms=%d",
                request_id, request.method, getattr(request.scope.get("route"), "path", "unmatched"),
                response.status_code, (time.monotonic() - started) * 1000)
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _error):
    return JSONResponse({"detail": "Проверьте формат и допустимые значения полей запроса."}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request, error):
    request_id = getattr(request.state, "request_id", uuid4().hex)
    application_log.error("request_id=%s error_type=%s", request_id, type(error).__name__)
    return JSONResponse({"detail": "Сервис временно недоступен. Повторите попытку или обратитесь к администратору."},
                        status_code=500, headers={"X-Request-ID": request_id, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


app.include_router(router, prefix="/api")


@app.get("/")
def root():
    return {"service": "ekt-replenishment"}
