"""Shared durable SQLite store. Migrations are atomic and safe to repeat."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import PROJECT_ROOT

SCHEMA_VERSION = 1


def database_path() -> Path:
    default = PROJECT_ROOT / "backend" / ".cache" / "orders.sqlite3"
    return Path(os.getenv("APP_DB_PATH") or os.getenv("ORDER_DB_PATH") or default).expanduser()


@contextmanager
def connection(write: bool = False):
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    try:
        if write:
            db.execute("BEGIN IMMEDIATE")
        yield db
        if write:
            db.commit()
    except BaseException:
        if write:
            db.rollback()
        raise
    finally:
        db.close()


def migrate() -> None:
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Database schema is newer than this application")
            if version < 1:
                statements = [
                    "CREATE TABLE IF NOT EXISTS recommendations (id TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)",
                    "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','manager')), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)",
                    "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf_token TEXT NOT NULL, expires_at REAL NOT NULL)",
                    "CREATE INDEX sessions_user ON sessions(user_id)",
                    "CREATE TABLE login_attempts (id INTEGER PRIMARY KEY, ip TEXT NOT NULL, username TEXT NOT NULL, attempted_at REAL NOT NULL)",
                    "CREATE INDEX login_attempts_time ON login_attempts(attempted_at)",
                    "CREATE TABLE orders (id TEXT PRIMARY KEY, calculation_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL, calculation TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                    "CREATE INDEX orders_owner_updated ON orders(owner_id, updated_at DESC)",
                    "CREATE TABLE decisions (order_id TEXT NOT NULL REFERENCES orders(id), line_id TEXT NOT NULL, quantity REAL NOT NULL, approved INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(order_id,line_id))",
                    "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT REFERENCES orders(id), actor_id TEXT REFERENCES users(id), actor_name TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL, changes TEXT NOT NULL)",
                    "CREATE INDEX events_order ON events(order_id,id)",
                    "CREATE TABLE jobs (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')), worker_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at REAL, order_id TEXT REFERENCES orders(id), error TEXT, UNIQUE(owner_id,idempotency_key))",
                    "CREATE INDEX jobs_queue ON jobs(status,created_at)",
                    "CREATE TABLE worker_state (name TEXT PRIMARY KEY, worker_id TEXT NOT NULL, heartbeat REAL NOT NULL)",
                    "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                ]
                for statement in statements:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")
            db.commit()
        except BaseException:
            db.rollback()
            raise
