"""Слой-адаптер источника данных.

Движок расчёта работает с единым контейнером Dataset (набор DataFrame'ов).
Источником может быть синтетика (для разработки/демо) или выгрузка 1С в CSV.
Чтобы подключить реальную 1С, достаточно реализовать новый DataSource —
код движка менять не нужно.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from app.config import get_settings


@dataclass
class Dataset:
    """Единый контейнер входных данных для движка."""
    sales: pd.DataFrame          # date, sku, name, category, qty, price, client_id, warehouse
    stock: pd.DataFrame          # sku, warehouse, on_hand
    in_transit: pd.DataFrame     # sku, warehouse, qty, eta
    suppliers: pd.DataFrame      # supplier_id, name, lead_time_days, min_order_qty
    sku_suppliers: pd.DataFrame  # sku, supplier_id, pack_size
    stockouts: pd.DataFrame      # sku, warehouse, start, end


class DataSource(Protocol):
    def load(self) -> Dataset: ...


class CsvDataSource:
    """Источник из CSV-выгрузок 1С. Ожидает файлы в DATA_DIR:
    sales.csv, stock.csv, in_transit.csv, suppliers.csv,
    sku_suppliers.csv, stockouts.csv (с колонками как в Dataset).
    """

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir

    def _read(self, name: str, parse_dates: list[str] | None = None) -> pd.DataFrame:
        path = os.path.join(self.data_dir, name)
        return pd.read_csv(path, parse_dates=parse_dates or [])

    def load(self) -> Dataset:
        return Dataset(
            sales=self._read("sales.csv", parse_dates=["date"]),
            stock=self._read("stock.csv"),
            in_transit=self._read("in_transit.csv", parse_dates=["eta"]),
            suppliers=self._read("suppliers.csv"),
            sku_suppliers=self._read("sku_suppliers.csv"),
            stockouts=self._read("stockouts.csv", parse_dates=["start", "end"]),
        )


def get_data_source() -> DataSource:
    """Фабрика источника по настройке DATA_SOURCE."""
    settings = get_settings()
    if settings.data_source == "csv":
        return CsvDataSource(settings.data_dir)
    # По умолчанию — синтетика (импорт здесь, чтобы избежать циклов)
    from app.data.synthetic import SyntheticDataSource
    return SyntheticDataSource()
