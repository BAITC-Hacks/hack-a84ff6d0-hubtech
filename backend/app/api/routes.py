"""Authenticated API for calculations, durable orders and administration."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app import jobs, repositories as repo
from app.api.models import CreateUser, ExportOrder, Login, SaveDecisions, UpdateOrder, UpdateUser
from app.config import get_settings
from app.core.export import ExportValidationError, to_excel_bytes
from app.data.adapter import get_data_source, source_files_available
from app.schemas import ExportRequest, RecommendRequest, RecommendationResponse
from app.security import COOKIE_NAME, current_session, login, public_user, require_admin, secure_cookie
from app.storage import connection

router = APIRouter()


def _load():
    try:
        return get_data_source().load()
    except (OSError, ValueError) as error:
        logging.getLogger(__name__).error("data_load_failed error_type=%s", type(error).__name__)
        raise HTTPException(422, "Не удалось прочитать данные. Проверьте наличие выгрузок и настройки источника.") from None


def _xlsx(data: bytes, approved_only: bool) -> Response:
    filename = "approved_order.xlsx" if approved_only else "order_draft.xlsx"
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready():
    try:
        with connection() as db:
            db.execute("SELECT count(*) FROM users").fetchone()
            worker = jobs.worker_available(db)
        data = source_files_available()
        available = worker and data
        return JSONResponse({"status": "ok" if available else "unavailable", "worker": worker,
                             "data": data}, status_code=200 if available else 503)
    except (OSError, ValueError, sqlite3.Error):
        return JSONResponse({"status": "unavailable", "worker": False, "data": False}, status_code=503)


@router.post("/auth/login")
def sign_in(command: Login, request: Request, response: Response):
    return login(request, response, command.username, command.password)


@router.get("/auth/session")
def session_info(session=Depends(current_session)):
    return {key: session[key] for key in ("user", "csrf_token")}


@router.post("/auth/logout", status_code=204)
def sign_out(session=Depends(current_session)):
    with connection(write=True) as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure_cookie(), httponly=True, samesite="strict")
    return response


@router.get("/users")
def users(session=Depends(current_session)):
    require_admin(session)
    with connection() as db:
        return {"items": [public_user(row) for row in db.execute("SELECT * FROM users ORDER BY username")]}


@router.post("/users", status_code=201)
def add_user(command: CreateUser, session=Depends(current_session)):
    return repo.create_user(command.username, command.password, command.role, require_admin(session))


@router.patch("/users/{user_id}")
def change_user(user_id: str, command: UpdateUser, session=Depends(current_session)):
    return repo.update_user(user_id, command.model_dump(exclude_unset=True), require_admin(session))


@router.get("/meta")
def meta(session=Depends(current_session)) -> dict:
    ds = _load()
    catalog = ds.catalog if not ds.catalog.empty else ds.sales
    settings = get_settings()
    warehouses = {
        warehouse
        for frame in (ds.sales, ds.monthly_sales, ds.stock, ds.in_transit)
        if {"sku", "warehouse"}.issubset(frame.columns)
        for warehouse in frame["warehouse"].dropna().unique()
        if isinstance(warehouse, str) and warehouse.strip()
    }
    return {
        "warehouses": sorted(warehouses),
        "categories": sorted(catalog["category"].dropna().unique().tolist()),
        "product_categories": sorted(catalog["product_category"].dropna().loc[lambda s: s.ne("")].unique().tolist()) if "product_category" in catalog else [],
        "suppliers": ds.suppliers.to_dict("records"),
        "sku_count": int(catalog["sku"].nunique()), "data_source": ds.source,
        "as_of": ds.as_of or date.today(), "warnings": ds.warnings,
        "data_quality": repo.public_metadata(ds.metadata),
        "defaults": {"service_level": settings.service_level, "review_period_days": settings.review_period_days},
        "capabilities": {"llm_available": settings.llm_enabled},
    }


@router.post("/recommend", response_model=RecommendationResponse)
def recommend(req: RecommendRequest, session=Depends(current_session), prefer: str = Header(default=""),
              idempotency_key: str | None = Header(default=None)):
    queued = jobs.enqueue(req, session["user"], idempotency_key)
    if "respond-async" in prefer.lower():
        return JSONResponse(queued, status_code=202)
    deadline = time.monotonic() + int(os.getenv("JOB_TIMEOUT_SECONDS", "600"))
    while time.monotonic() < deadline:
        job = jobs.get_job(queued["job_id"], session["user"])
        if job["status"] == "completed":
            return RecommendationResponse.model_validate(repo.get_order(job["order_id"], session["user"])["calculation"])
        if job["status"] == "failed":
            raise HTTPException(422, job["error"] or "Расчёт завершился с ошибкой.")
        if job["status"] == "cancelled":
            raise HTTPException(409, "Расчёт отменён.")
        time.sleep(0.25)
    return JSONResponse({"job_id": queued["job_id"], "status": jobs.get_job(queued["job_id"], session["user"])["status"]}, status_code=202)


@router.post("/recommend/export")
def recommend_export(req: ExportRequest, session=Depends(current_session)) -> Response:
    with connection() as db:
        row = db.execute("SELECT id FROM orders WHERE calculation_id=?", (req.calculation_id,)).fetchone()
        if row is None:
            raise HTTPException(410, "Сохранённый расчёт недоступен. Выполните расчёт заново перед экспортом.")
        repo.authorized_order(db, row["id"], session["user"])
    detail = repo.get_order(row["id"], session["user"])
    try:
        # Retain validation errors for malformed legacy requests, but never let
        # this compatibility route bypass persisted decisions and audit history.
        snapshot = RecommendationResponse.model_validate(detail["calculation"])
        to_excel_bytes(snapshot, req)
    except ExportValidationError as error:
        raise HTTPException(422, str(error)) from None
    incoming = {line.line_id: line.model_dump() for line in req.lines}
    saved = {line["line_id"]: line for line in detail["decisions"]}
    if incoming != saved:
        raise HTTPException(409, "Сначала сохраните правки заказа и экспортируйте его текущую версию.")
    return _xlsx(repo.export_order(detail["id"], detail["revision"], req.approved_only, session["user"]), req.approved_only)


@router.get("/orders")
def orders(limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
           archived: bool = False, session=Depends(current_session)):
    return repo.list_orders(session["user"], limit, offset, archived)


@router.get("/orders/{order_id}")
def order(order_id: str, session=Depends(current_session)):
    return repo.get_order(order_id, session["user"])


@router.put("/orders/{order_id}")
def save_order(order_id: str, command: SaveDecisions, session=Depends(current_session)):
    return repo.save_decisions(order_id, command.revision, command.lines, session["user"])


@router.patch("/orders/{order_id}")
def change_order(order_id: str, command: UpdateOrder, session=Depends(current_session)):
    return repo.update_order(order_id, command.revision, command.model_dump(exclude_unset=True, exclude={"revision"}), session["user"])


@router.get("/orders/{order_id}/events")
def events(order_id: str, session=Depends(current_session)):
    with connection() as db:
        repo.authorized_order(db, order_id, session["user"])
        rows = db.execute("SELECT id,actor_name,created_at,action,changes FROM events WHERE order_id=? ORDER BY id DESC", (order_id,)).fetchall()
    return {"items": [{**dict(row), "changes": json.loads(row["changes"])} for row in rows]}


@router.post("/orders/{order_id}/export")
def export_saved_order(order_id: str, command: ExportOrder, session=Depends(current_session)):
    return _xlsx(repo.export_order(order_id, command.revision, command.approved_only, session["user"]), command.approved_only)


@router.get("/jobs")
def job_list(session=Depends(current_session)):
    return jobs.list_jobs(session["user"])


@router.get("/jobs/{job_id}")
def job(job_id: str, session=Depends(current_session)):
    return jobs.get_job(job_id, session["user"])


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, session=Depends(current_session)):
    return jobs.cancel(job_id, session["user"])
