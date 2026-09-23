"""Слой-адаптер источника данных.

Движок расчёта работает с единым контейнером Dataset (набор DataFrame'ов).
Источником могут быть Excel-выгрузки партнёра, CSV или синтетика для демо.
Чтобы подключить реальную 1С, достаточно реализовать новый DataSource —
код движка менять не нужно.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
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
    catalog: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["sku", "name", "category", "unit", "supplier_id"]))
    monthly_sales: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["sku", "warehouse", "month", "qty"]))
    monthly_stock: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["sku", "warehouse", "month", "on_hand"]))
    seasonality: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["supplier_id", "month", "factor"]))
    as_of: date | None = None
    source: str = "synthetic"
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


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
            source="csv",
        )


def get_data_source() -> DataSource:
    """Фабрика источника по настройке DATA_SOURCE."""
    settings = get_settings()
    if settings.data_source == "csv":
        return CsvDataSource(settings.data_dir)
    if settings.data_source == "excel":
        from app.data.excel import ExcelDataSource
        return ExcelDataSource(settings.data_dir, as_of=settings.data_as_of,
                               iek_lead_time_days=settings.iek_lead_time_days,
                               systeme_lead_time_days=settings.systeme_lead_time_days)
    if settings.data_source != "synthetic":
        raise ValueError("DATA_SOURCE должен быть excel, csv или synthetic")
    # Импорт здесь, чтобы избежать циклов.
    from app.data.synthetic import SyntheticDataSource
    return SyntheticDataSource()
