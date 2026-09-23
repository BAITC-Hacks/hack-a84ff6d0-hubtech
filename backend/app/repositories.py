"""Transactional orders, decisions and append-only audit events."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from app.core.export import ExportValidationError, _validate_quantity, to_excel_bytes
from app.schemas import ExportLine, ExportRequest, RecommendationResponse
from app.security import hash_password, public_user
from app.storage import connection, database_path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def public_metadata(metadata: dict) -> dict:
    """Preserve business quality metrics, never disclose server paths or config."""
    allowed = {"counts", "rows", "as_of", "as_of_basis", "transaction_start", "transaction_end",
               "stock_as_of", "monthly_stock_fallback_skus", "unknown_stock_skus",
               "unknown_order_constraints", "calculation_reconciliation", "assumptions"}
    result = {key: value for key, value in metadata.items() if key in allowed}
    if isinstance(metadata.get("catalog_enrichment"), dict):
        safe_catalog_fields = {
            "status", "source", "fetched_at", "total_catalog", "web_products",
            "valid_products", "matched", "unmatched", "conflicts", "invalid_products",
            "duplicate_skus", "duplicate_products", "unknown_brand_matches",
            "crawl_quarantined", "crawl_failures", "crawl_coverage", "conflict_reasons",
        }
        result["catalog_enrichment"] = {
            key: value for key, value in metadata["catalog_enrichment"].items()
            if key in safe_catalog_fields
        }
    if isinstance(metadata.get("files"), list):
        result["files"] = [
            {**{k: item[k] for k in ("sheet", "header_row", "rows") if k in item},
             "file": re.split(r"[/\\]", str(item.get("file", "")))[-1]}
            for item in metadata["files"] if isinstance(item, dict)
        ]
    return result


def sanitize_calculation(calculation: RecommendationResponse) -> RecommendationResponse:
    return calculation.model_copy(update={"data_quality": public_metadata(calculation.data_quality)})


def event(db, order_id, actor: dict, action: str, changes) -> None:
    db.execute("INSERT INTO events(order_id,actor_id,actor_name,created_at,action,changes) VALUES(?,?,?,?,?,?)",
               (order_id, actor["id"], actor["username"], now(), action, json.dumps(changes, ensure_ascii=False)))


def create_user(username: str, password: str, role: str, actor: dict | None = None) -> dict:
    if role not in ("admin", "manager") or not re.fullmatch(r"[a-zA-Z0-9_.@-]{3,80}", username):
        raise HTTPException(422, "Укажите допустимое имя пользователя и роль.")
    password_hash = hash_password(password)
    with connection(write=True) as db:
        try:
            user_id = uuid4().hex
            db.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (user_id, username.lower(), password_hash, role, 1, now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Пользователь с таким именем уже существует.") from None
        user = public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
        event(db, None, actor or user, "user_created", {"user_id": user_id, "username": user["username"], "role": role})
        if role == "admin" and not db.execute("SELECT 1 FROM app_state WHERE key='legacy_imported'").fetchone():
            _import_legacy(db, user)
    return user


def _import_legacy(db, admin: dict) -> None:
    records = list(db.execute("SELECT * FROM recommendations"))
    legacy = Path(os.getenv("ORDER_DB_PATH") or Path(__file__).resolve().parents[1] / ".cache" / "orders.sqlite3")
    if legacy.exists() and legacy.resolve() != database_path().resolve():
        with sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True) as old:
            old.row_factory = sqlite3.Row
            if old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendations'").fetchone():
                records += list(old.execute("SELECT * FROM recommendations"))
    for record in records:
        try:
            calculation = RecommendationResponse.model_validate_json(record["payload"])
        except ValueError:
            # An invalid historical record remains in its original table for manual recovery.
            continue
        if db.execute("SELECT 1 FROM orders WHERE calculation_id=?", (calculation.calculation_id or record["id"],)).fetchone():
            continue
        calculation = calculation.model_copy(update={"calculation_id": calculation.calculation_id or record["id"]})
        create_order(db, calculation, admin, archived=True, imported=True)
    db.execute("INSERT INTO app_state VALUES('legacy_imported',?)", (now(),))


def update_user(user_id: str, changes: dict, actor: dict) -> dict:
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(422, "Укажите изменения учётной записи.")
    password_hash = hash_password(changes["password"]) if "password" in changes else None
    with connection(write=True) as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден.")
        role, active = changes.get("role", row["role"]), changes.get("active", bool(row["active"]))
        if row["role"] == "admin" and row["active"] and (role != "admin" or not active):
            admins = db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
            if admins <= 1:
                raise HTTPException(409, "Нельзя отключить или понизить роль последнего администратора.")
        db.execute("UPDATE users SET role=?,active=?,password_hash=? WHERE id=?",
                   (role, int(active), password_hash or row["password_hash"], user_id))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit = {key: value for key, value in changes.items() if key != "password"}
        if password_hash:
            audit["password_reset"] = True
        event(db, None, actor, "user_updated", {"user_id": user_id, **audit})
        if not active:
            db.execute("UPDATE jobs SET status='cancelled',updated_at=?,error=? WHERE owner_id=? AND status IN ('queued','running')",
                       (now(), "Задание отменено после отключения учётной записи.", user_id))
        return public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def create_order(db, calculation: RecommendationResponse, owner: dict, *, archived=False, imported=False) -> str:
    calculation = sanitize_calculation(calculation)
    if not calculation.calculation_id:
        calculation = calculation.model_copy(update={"calculation_id": uuid4().hex})
    order_id = uuid4().hex
    created = now()
    title = f"Заказ от {str(calculation.as_of or created[:10])}"
    db.execute("INSERT INTO orders VALUES(?,?,?,?,?,1,?,?,?)",
               (order_id, calculation.calculation_id, owner["id"], title, calculation.model_dump_json(), int(archived), created, created))
    # Compatibility for legacy export clients; durable orders are never pruned.
    db.execute("INSERT OR IGNORE INTO recommendations VALUES(?,?,?)",
               (calculation.calculation_id, time.time(), calculation.model_dump_json()))
    decisions = []
    for group in calculation.groups:
        for line in group.lines:
            decisions.append((order_id, line.line_id, line.recommended_qty, 0))
    db.executemany("INSERT INTO decisions VALUES(?,?,?,?)", decisions)
    event(db, order_id, owner, "legacy_imported" if imported else "created",
          {"calculation_id": calculation.calculation_id, "positions": len(decisions), "archived": bool(archived)})
    return order_id


def authorized_order(db, order_id: str, user: dict):
    row = db.execute("SELECT o.*,u.username AS owner_name FROM orders o JOIN users u ON u.id=o.owner_id WHERE o.id=?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Заказ не найден.")
    if row["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "Нет доступа к этому заказу.")
    return row


def check_revision(row, revision: int) -> None:
    if row["revision"] != revision:
        raise HTTPException(409, "Заказ изменён в другом окне. Обновите его перед сохранением.")


def order_detail(db, row) -> dict:
    result = {key: row[key] for key in ("id", "title", "revision", "owner_id", "owner_name", "created_at", "updated_at")}
    result["archived"] = bool(row["archived"])
    result["calculation"] = sanitize_calculation(RecommendationResponse.model_validate_json(row["calculation"])).model_dump(mode="json")
    result["decisions"] = [{"line_id": d["line_id"], "quantity": d["quantity"], "approved": bool(d["approved"])}
                           for d in db.execute("SELECT * FROM decisions WHERE order_id=? ORDER BY line_id", (row["id"],))]
    return result


def get_order(order_id: str, user: dict) -> dict:
    with connection() as db:
        db.execute("BEGIN")
        return order_detail(db, authorized_order(db, order_id, user))


def list_orders(user: dict, limit=50, offset=0, archived=False) -> dict:
    where, params = "o.archived=?", [int(archived)]
    if user["role"] != "admin":
        where += " AND o.owner_id=?"
        params.append(user["id"])
    with connection() as db:
        db.execute("BEGIN")
        total = db.execute(f"SELECT count(*) FROM orders o WHERE {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT o.id,o.title,o.owner_id,u.username AS owner_name,o.revision,o.archived,o.created_at,o.updated_at,"
            f"(SELECT count(*) FROM decisions d WHERE d.order_id=o.id AND d.quantity>0) AS positions,"
            f"(SELECT count(*) FROM decisions d WHERE d.order_id=o.id AND d.approved=1 AND d.quantity>0) AS approved "
            f"FROM orders o JOIN users u ON u.id=o.owner_id WHERE {where} ORDER BY o.updated_at DESC,o.id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {"items": [{**dict(row), "archived": bool(row["archived"])} for row in rows], "total": total}


def save_decisions(order_id: str, revision: int, lines: list[ExportLine], user: dict) -> dict:
    with connection(write=True) as db:
        row = authorized_order(db, order_id, user)
        check_revision(row, revision)
        if row["archived"]:
            raise HTTPException(409, "Сначала восстановите заказ из архива.")
        calculation = RecommendationResponse.model_validate_json(row["calculation"])
        known = {line.line_id: line for group in calculation.groups for line in group.lines}
        incoming = {decision.line_id: decision for decision in lines}
        if len(incoming) != len(lines) or set(incoming) != set(known):
            raise HTTPException(422, "Состав позиций не совпадает с сохранённым расчётом.")
        previous = {line["line_id"]: line for line in db.execute("SELECT * FROM decisions WHERE order_id=?", (order_id,))}
        changes = []
        for decision in lines:
            try:
                _validate_quantity(known[decision.line_id], decision)
            except ExportValidationError as error:
                raise HTTPException(422, str(error)) from None
            old = previous[decision.line_id]
            quantity_changed = not math.isclose(old["quantity"], decision.quantity, rel_tol=0, abs_tol=1e-8)
            approved = bool(decision.approved and decision.quantity > 0 and not quantity_changed)
            if quantity_changed or approved != bool(old["approved"]):
                changes.append({"line_id": decision.line_id, "before": {"quantity": old["quantity"], "approved": bool(old["approved"])},
                                "after": {"quantity": decision.quantity, "approved": approved}})
            db.execute("UPDATE decisions SET quantity=?,approved=? WHERE order_id=? AND line_id=?",
                       (decision.quantity, int(approved), order_id, decision.line_id))
        if changes:
            db.execute("UPDATE orders SET revision=revision+1,updated_at=? WHERE id=?", (now(), order_id))
            event(db, order_id, user, "decisions_updated", changes)
        return order_detail(db, authorized_order(db, order_id, user))


def update_order(order_id: str, revision: int, changes: dict, user: dict) -> dict:
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(422, "Укажите изменения заказа.")
    with connection(write=True) as db:
        row = authorized_order(db, order_id, user)
        check_revision(row, revision)
        title = changes.get("title", row["title"]).strip()
        if not title:
            raise HTTPException(422, "Название заказа не может быть пустым.")
        archived = int(changes.get("archived", bool(row["archived"])))
        db.execute("UPDATE orders SET title=?,archived=?,revision=revision+1,updated_at=? WHERE id=?", (title, archived, now(), order_id))
        event(db, order_id, user, "order_updated", {"before": {"title": row["title"], "archived": bool(row["archived"])},
                                                     "after": {"title": title, "archived": bool(archived)}})
        return order_detail(db, authorized_order(db, order_id, user))


def export_order(order_id: str, revision: int, approved_only: bool, user: dict) -> bytes:
    # Capture a consistent revision without holding a database lock during XLSX generation.
    detail = get_order(order_id, user)
    check_revision(detail, revision)
    if not detail["decisions"]:
        raise HTTPException(422, "Нет позиций для экспорта.")
    request = ExportRequest(calculation_id=detail["calculation"]["calculation_id"], approved_only=approved_only,
                            lines=detail["decisions"])
    try:
        data = to_excel_bytes(RecommendationResponse.model_validate(detail["calculation"]), request,
                              order_metadata={"ID заказа": detail["id"], "Версия заказа": revision,
                                              "Владелец заказа": detail["owner_name"],
                                              "Название заказа": detail["title"],
                                              "Сохранён": detail["updated_at"]})
    except ExportValidationError as error:
        raise HTTPException(422, str(error)) from None
    with connection(write=True) as db:
        row = authorized_order(db, order_id, user)
        check_revision(row, revision)
        event(db, order_id, user, "exported", {"revision": revision, "approved_only": approved_only})
    return data
