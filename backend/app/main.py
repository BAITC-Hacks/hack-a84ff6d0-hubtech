"""Точка входа FastAPI: сервис автоматического формирования заказов поставщикам.

Кейс ТОО «Электрокомплект» (HackAlem AI, трек 05 · Логистика).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router

app = FastAPI(
    title="ЭКТ · Автозаказы поставщикам",
    description="Рекомендованные заказы для пополнения склада на основе прогноза спроса.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/")
def root() -> dict:
    return {"service": "ekt-replenishment", "docs": "/docs"}
