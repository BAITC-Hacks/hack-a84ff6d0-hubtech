"""Password authentication, opaque sessions, CSRF and persistent login throttling."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response

from app.storage import connection

COOKIE_NAME = "umytpa_session"
SESSION_SECONDS = 8 * 60 * 60
PASSWORD_HASHER = PasswordHasher()
DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def public_user(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "active": bool(row["active"])}


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise HTTPException(422, "Пароль должен содержать от 12 до 256 символов.")
    return PASSWORD_HASHER.hash(password)


def secure_cookie() -> bool:
    return os.getenv("APP_ENV", "production").lower() != "development"


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    expected = os.getenv("APP_ORIGIN", "").rstrip("/") or str(request.base_url).rstrip("/")
    if origin.rstrip("/") != expected:
        raise HTTPException(403, "Запрос отправлен с недопустимого адреса.")


def current_session(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 512:
        raise HTTPException(401, "Войдите в систему, чтобы продолжить.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connection() as db:
        row = db.execute(
            "SELECT u.*, s.csrf_token, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (digest,),
        ).fetchone()
    if row is None or not row["active"] or row["expires_at"] <= time.time():
        raise HTTPException(401, "Сессия завершилась. Войдите в систему повторно.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_origin(request)
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), row["csrf_token"]):
            raise HTTPException(403, "Не удалось подтвердить запрос. Обновите страницу и повторите действие.")
    return {"user": public_user(row), "csrf_token": row["csrf_token"], "token_hash": digest}


def login(request: Request, response: Response, username: str, password: str) -> dict:
    check_origin(request)
    username = username.strip().lower()
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with connection(write=True) as db:
        db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 900,))
        ip_count = db.execute("SELECT count(*) FROM login_attempts WHERE ip=?", (ip,)).fetchone()[0]
        user_count = db.execute("SELECT count(*) FROM login_attempts WHERE username=?", (username,)).fetchone()[0]
        if ip_count >= 20 or user_count >= 5:
            raise HTTPException(429, "Слишком много попыток входа. Повторите через 15 минут.", headers={"Retry-After": "900"})
        db.execute("INSERT INTO login_attempts(ip,username,attempted_at) VALUES(?,?,?)", (ip, username, now))
        row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    try:
        valid = PASSWORD_HASHER.verify(row["password_hash"] if row else DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid or row is None or not row["active"]:
        raise HTTPException(401, "Неверное имя пользователя или пароль.")
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connection(write=True) as db:
        # The account may have been disabled or its password changed during verification.
        fresh = db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        if not fresh["active"] or fresh["password_hash"] != row["password_hash"]:
            raise HTTPException(401, "Неверное имя пользователя или пароль.")
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        old_cookie = request.cookies.get(COOKIE_NAME)
        if old_cookie:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(old_cookie.encode()).hexdigest(),))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest, row["id"], csrf, now + SESSION_SECONDS))
        db.execute("DELETE FROM login_attempts WHERE username=?", (username,))
        if PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (PASSWORD_HASHER.hash(password), row["id"]))
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=secure_cookie(), samesite="strict", path="/")
    return {"user": public_user(row), "csrf_token": csrf}


def require_admin(session: dict) -> dict:
    if session["user"]["role"] != "admin":
        raise HTTPException(403, "Это действие доступно только администратору.")
    return session["user"]
