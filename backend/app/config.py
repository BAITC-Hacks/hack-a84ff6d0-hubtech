"""Конфигурация сервиса. Значения берутся из окружения / .env."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Глобальные настройки расчёта и интеграций."""

    def __init__(self) -> None:
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.data_source: str = os.getenv("DATA_SOURCE", "synthetic").strip().lower()
        self.data_dir: str = os.getenv("DATA_DIR", "./data_1c").strip()

        # Параметры пополнения по умолчанию
        self.service_level: float = float(os.getenv("SERVICE_LEVEL", "0.95"))
        self.review_period_days: int = int(os.getenv("REVIEW_PERIOD_DAYS", "14"))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
