"""Конфигурация сервиса. Значения берутся из окружения / .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from datetime import date

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "backend" / ".env")


class Settings:
    """Глобальные настройки расчёта и интеграций."""

    def __init__(self) -> None:
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.data_source: str = os.getenv("DATA_SOURCE", "excel").strip().lower()
        default_dir = "." if self.data_source == "excel" else "data_1c"
        data_dir = Path(os.getenv("DATA_DIR", default_dir).strip()).expanduser()
        self.data_dir = str(data_dir if data_dir.is_absolute() else PROJECT_ROOT / data_dir)
        configured_date = os.getenv("DATA_AS_OF", "").strip()
        self.data_as_of = date.fromisoformat(configured_date) if configured_date else None
        # Допущения до подтверждения поставщиками, явно показаны в метаданных.
        self.iek_lead_time_days = int(os.getenv("IEK_LEAD_TIME_DAYS", "21"))
        self.systeme_lead_time_days = int(os.getenv("SYSTEME_LEAD_TIME_DAYS", "35"))
        if min(self.iek_lead_time_days, self.systeme_lead_time_days) < 0:
            raise ValueError("Срок поставки не может быть отрицательным")

        # Параметры пополнения по умолчанию
        self.service_level: float = float(os.getenv("SERVICE_LEVEL", "0.95"))
        self.review_period_days: int = int(os.getenv("REVIEW_PERIOD_DAYS", "14"))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
