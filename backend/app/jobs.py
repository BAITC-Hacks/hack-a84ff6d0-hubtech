"""Durable queue and calculation execution. Only a worker creates async orders."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from uuid import uuid4

from fastapi import HTTPException

from app.core.explain import explanation_budget
from app.core.recommend import generate_recommendations
from app.data.adapter import get_data_source
from app.repositories import create_order, now
from app.schemas import RecommendRequest
from app.security import public_user
from app.storage import connection

log = logging.getLogger(__name__)


def worker_available(db=None) -> bool:
    if db is None:
        with connection() as opened:
            return worker_available(opened)
    row = db.execute("SELECT heartbeat FROM worker_state WHERE name='main'").fetchone()
    return row is not None and row["heartbeat"] > time.time() - int(os.getenv("WORKER_STALE_SECONDS", "30"))


def public_job(row) -> dict:
    return {key: row[key] for key in ("id", "status", "created_at", "updated_at", "order_id", "error")}


def authorized_job(db, job_id: str, user: dict):
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Задание не найдено.")
    if row["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "Нет доступа к этому заданию.")
    return row


def enqueue(request: RecommendRequest, user: dict, key: str | None) -> dict:
    key = key or uuid4().hex
    if not 1 <= len(key) <= 128 or not key.isascii() or any(ord(char) < 33 for char in key):
        raise HTTPException(422, "Некорректный идентификатор повторного запроса.")
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with connection(write=True) as db:
        if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user["id"],)).fetchone():
            raise HTTPException(401, "Учётная запись отключена.")
        existing = db.execute("SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?", (user["id"], key)).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                raise HTTPException(409, "Этот идентификатор уже использован для других параметров расчёта.")
            return {"job_id": existing["id"], "status": existing["status"]}
        if not worker_available(db):
            raise HTTPException(503, "Сервис расчёта временно недоступен. Повторите попытку позже.")
        queued = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if queued >= int(os.getenv("JOB_QUEUE_LIMIT", "20")):
            raise HTTPException(429, "Очередь расчётов заполнена. Дождитесь завершения текущих заданий.")
        own_active = db.execute("SELECT 1 FROM jobs WHERE owner_id=? AND status IN ('queued','running')", (user["id"],)).fetchone()
        if own_active:
            raise HTTPException(409, "У вас уже есть незавершённый расчёт. Дождитесь результата или отмените его.")
        job_id, created = uuid4().hex, now()
        db.execute("INSERT INTO jobs(id,owner_id,idempotency_key,request_hash,request,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                   (job_id, user["id"], key, digest, payload, created, created))
    return {"job_id": job_id, "status": "queued"}


def get_job(job_id: str, user: dict) -> dict:
    with connection() as db:
        return public_job(authorized_job(db, job_id, user))


def list_jobs(user: dict) -> dict:
    with connection() as db:
        condition, params = ("", ()) if user["role"] == "admin" else (" WHERE owner_id=?", (user["id"],))
        rows = db.execute("SELECT * FROM jobs" + condition + " ORDER BY created_at DESC LIMIT 100", params).fetchall()
    return {"items": [public_job(row) for row in rows]}


def cancel(job_id: str, user: dict) -> dict:
    with connection(write=True) as db:
        row = authorized_job(db, job_id, user)
        if row["status"] in ("queued", "running"):
            db.execute("UPDATE jobs SET status='cancelled',updated_at=?,error=NULL WHERE id=?", (now(), job_id))
        return public_job(authorized_job(db, job_id, user))


def claim(worker_id: str) -> str | None:
    with connection(write=True) as db:
        lease = db.execute("SELECT worker_id FROM worker_state WHERE name='main'").fetchone()
        if lease is None or lease["worker_id"] != worker_id or not worker_available(db):
            return None
        row = db.execute("SELECT j.id FROM jobs j JOIN users u ON u.id=j.owner_id WHERE j.status='queued' AND u.active=1 ORDER BY j.created_at,j.id LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE jobs SET status='running',worker_id=?,updated_at=?,started_at=? WHERE id=?", (worker_id, now(), time.time(), row["id"]))
        return row["id"]


def fail_job(job_id: str, worker_id: str, message: str) -> None:
    with connection(write=True) as db:
        db.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=? AND worker_id=? AND status='running'",
                   (message, now(), job_id, worker_id))


def execute_job(job_id: str, worker_id: str) -> None:
    """Runs in a disposable subprocess; cancel/timeout cannot commit a late order."""
    try:
        with connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND worker_id=? AND status='running'", (job_id, worker_id)).fetchone()
        if job is None:
            return
        request = RecommendRequest.model_validate_json(job["request"])
        ds = get_data_source().load()
        with explanation_budget():
            calculation = generate_recommendations(ds, **request.model_dump())
        with connection(write=True) as db:
            current = db.execute("SELECT * FROM jobs WHERE id=? AND worker_id=? AND status='running'", (job_id, worker_id)).fetchone()
            owner = db.execute("SELECT * FROM users WHERE id=? AND active=1", (job["owner_id"],)).fetchone()
            if current is None or owner is None:
                return
            if time.time() - current["started_at"] >= int(os.getenv("JOB_TIMEOUT_SECONDS", "600")):
                db.execute("UPDATE jobs SET status='failed',updated_at=?,error=? WHERE id=?",
                           (now(), "Расчёт превысил допустимое время. Уменьшите выборку и повторите попытку.", job_id))
                return
            order_id = create_order(db, calculation, public_user(owner))
            db.execute("UPDATE jobs SET status='completed',order_id=?,updated_at=? WHERE id=?", (order_id, now(), job_id))
    except (OSError, ValueError) as error:
        log.error("job_id=%s error_type=%s", job_id, type(error).__name__)
        fail_job(job_id, worker_id, "Не удалось прочитать данные. Проверьте выгрузки и параметры расчёта.")
    except BaseException as error:
        log.error("job_id=%s error_type=%s", job_id, type(error).__name__)
        fail_job(job_id, worker_id, "Не удалось выполнить расчёт. Повторите попытку или обратитесь к администратору.")
