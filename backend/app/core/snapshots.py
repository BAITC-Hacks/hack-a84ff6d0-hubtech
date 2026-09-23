"""Снимки расчёта: экспорт всегда использует показанные менеджеру данные."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.schemas import RecommendationResponse


RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_SNAPSHOTS = 50


class SnapshotNotFound(LookupError):
    pass


@contextmanager
def _connection():
    default = Path(__file__).resolve().parents[2] / ".cache" / "orders.sqlite3"
    path = Path(os.getenv("ORDER_DB_PATH", str(default))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS recommendations "
                "(id TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            yield connection
    finally:
        connection.close()


def save_snapshot(response: RecommendationResponse) -> RecommendationResponse:
    snapshot = response.model_copy(update={"calculation_id": uuid4().hex})
    now = time.time()
    with _connection() as connection:
        connection.execute(
            "INSERT INTO recommendations (id, created_at, payload) VALUES (?, ?, ?)",
            (snapshot.calculation_id, now, snapshot.model_dump_json()),
        )
        connection.execute(
            "DELETE FROM recommendations WHERE created_at < ?",
            (now - RETENTION_SECONDS,),
        )
        connection.execute(
            "DELETE FROM recommendations WHERE id NOT IN "
            "(SELECT id FROM recommendations ORDER BY created_at DESC LIMIT ?)",
            (MAX_SNAPSHOTS,),
        )
    return snapshot


def load_snapshot(calculation_id: str) -> RecommendationResponse:
    with _connection() as connection:
        row = connection.execute(
            "SELECT payload FROM recommendations WHERE id = ? AND created_at >= ?",
            (calculation_id, time.time() - RETENTION_SECONDS),
        ).fetchone()
    if row is None:
        raise SnapshotNotFound(calculation_id)
    return RecommendationResponse.model_validate_json(row[0])
