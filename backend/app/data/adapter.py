"""Слой-адаптер источника данных.

Движок расчёта работает с единым контейнером Dataset (набор DataFrame'ов).
Источником могут быть Excel-выгрузки партнёра, CSV или синтетика для демо.
Чтобы подключить реальную 1С, достаточно реализовать новый DataSource —
код движка менять не нужно.
"""
from __future__ import annotations

import os
import math
from pathlib import Path
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
    Необязательный catalog.csv задаёт sku/name/category/unit и supplier_sku.
    Единица из sales.unit используется только при отсутствии значения каталога.
    """

    REQUIRED_COLUMNS = {
        "sales.csv": ("date", "sku", "warehouse", "qty"),
        "stock.csv": ("sku", "warehouse", "on_hand"),
        "in_transit.csv": ("sku", "warehouse", "qty", "eta"),
        "suppliers.csv": ("supplier_id", "name", "lead_time_days", "min_order_qty"),
        "sku_suppliers.csv": ("sku", "supplier_id", "pack_size"),
        "stockouts.csv": ("sku", "warehouse", "start", "end"),
        "catalog.csv": ("sku", "name", "category", "unit"),
    }

    def __init__(self, data_dir: str, as_of: date | None = None) -> None:
        self.data_dir = data_dir
        self.as_of = as_of

    def _read(self, name: str, parse_dates: list[str] | None = None) -> pd.DataFrame:
        path = os.path.join(self.data_dir, name)
        identifiers = ("sku", "supplier_id", "client_id", "order_id", "document_id",
                       "order_document", "document", "supplier_sku", "warehouse")
        frame = pd.read_csv(path, parse_dates=parse_dates or [], keep_default_na=False,
                            dtype={column: "string" for column in (*identifiers, "unit", "name", "category")})
        for column in identifiers:
            if column in frame:
                frame[column] = frame[column].str.strip().replace("", pd.NA)
        return frame

    def _validated(self, name: str) -> pd.DataFrame:
        frame = self._read(name)
        missing = set(self.REQUIRED_COLUMNS[name]) - set(frame.columns)
        if missing:
            raise ValueError(f"{name}: отсутствуют колонки {', '.join(sorted(missing))}")
        for column in ("sku", "supplier_id", "warehouse"):
            if column in self.REQUIRED_COLUMNS[name] and frame[column].isna().any():
                raise ValueError(f"{name}: {column} не может быть пустым")
        for column in ("qty", "on_hand", "lead_time_days", "min_order_qty", "pack_size"):
            if column not in frame:
                continue
            raw = frame[column].replace("", pd.NA)
            numeric = pd.to_numeric(raw, errors="coerce")
            invalid = (raw.notna() & numeric.isna()) | (numeric.notna() & ~numeric.map(lambda n: math.isfinite(n) if pd.notna(n) else True))
            if invalid.any() or (column != "on_hand" and numeric.isna().any()):
                raise ValueError(f"{name}: {column} должен содержать конечные числа")
            if column in {"lead_time_days", "min_order_qty"} and numeric.lt(0).any():
                raise ValueError(f"{name}: {column} не может быть отрицательным")
            if column == "lead_time_days" and numeric.mod(1).ne(0).any():
                raise ValueError(f"{name}: срок должен быть целым числом дней")
            if column == "pack_size" and numeric.le(0).any():
                raise ValueError(f"{name}: кратность должна быть положительной")
            frame[column] = numeric.astype(float)
        for column in ("date", "eta", "start", "end", "as_of", "source_as_of"):
            if column not in frame:
                continue
            raw = frame[column].replace("", pd.NA)
            parsed = pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce")
            if (raw.notna() & parsed.isna()).any() or (column in {"date", "start", "end"} and parsed.isna().any()):
                raise ValueError(f"{name}: {column} должен содержать даты YYYY-MM-DD")
            frame[column] = parsed.dt.normalize()
        if name == "stockouts.csv" and frame["end"].lt(frame["start"]).any():
            raise ValueError("stockouts.csv: конец интервала предшествует началу")
        key = "supplier_id" if name == "suppliers.csv" else "sku"
        if name in {"suppliers.csv", "sku_suppliers.csv", "catalog.csv"} and frame[key].duplicated().any():
            raise ValueError(f"{name}: неоднозначные повторяющиеся {key}")
        return frame

    def _catalog(self, sales: pd.DataFrame, links: pd.DataFrame) -> pd.DataFrame:
        path = Path(self.data_dir) / "catalog.csv"
        explicit = self._validated("catalog.csv") if path.is_file() else pd.DataFrame(columns=["sku", "name", "category", "unit"])
        records = explicit.set_index("sku").to_dict("index")
        for sku, rows in sales.groupby("sku", sort=False):
            current = records.setdefault(sku, {})
            units = {str(value).strip() for value in rows.get("unit", pd.Series(dtype=object)).dropna() if str(value).strip()}
            catalog_unit = str(current.get("unit", "")).strip()
            if len(units) > 1 or (catalog_unit and units and catalog_unit not in units):
                raise ValueError(f"CSV: неоднозначная единица измерения SKU {sku}")
            current["unit"] = catalog_unit or next(iter(units), "")
            for column, default in (("name", sku), ("category", "Без категории")):
                if not str(current.get(column, "")).strip():
                    values = [str(value).strip() for value in rows.get(column, pd.Series(dtype=object)).dropna() if str(value).strip()]
                    current[column] = values[0] if values else default
        suppliers = links.set_index("sku")["supplier_id"].to_dict()
        for sku, record in records.items():
            record["supplier_id"] = suppliers.get(sku, record.get("supplier_id", "SUP-00"))
        return pd.DataFrame([{"sku": sku, **record} for sku, record in records.items()],
                            columns=list(dict.fromkeys(["sku", "name", "category", "unit", "supplier_id", *explicit.columns])))

    def load(self) -> Dataset:
        frames = {name: self._validated(name) for name in self.REQUIRED_COLUMNS if name != "catalog.csv"}
        sales, links = frames["sales.csv"], frames["sku_suppliers.csv"]
        catalog = self._catalog(sales, links)
        latest = sales["date"].max()
        if self.as_of is None and pd.isna(latest):
            raise ValueError("DATA_AS_OF требуется для CSV без истории продаж")
        as_of = self.as_of or (latest + pd.Timedelta(days=1)).date()
        unknown_units = int(catalog["unit"].fillna("").str.strip().eq("").sum())
        warnings = ([f"CSV: единица измерения не указана для {unknown_units} SKU; подтвердите её в catalog.csv или sales.csv."]
                    if unknown_units else [])
        return Dataset(
            sales=sales, stock=frames["stock.csv"], in_transit=frames["in_transit.csv"],
            suppliers=frames["suppliers.csv"], sku_suppliers=links, stockouts=frames["stockouts.csv"],
            catalog=catalog, source="csv", as_of=as_of, warnings=warnings,
            metadata={"transaction_end": latest.date().isoformat() if pd.notna(latest) else None,
                      "files": list(frames) + (["catalog.csv"] if (Path(self.data_dir) / "catalog.csv").is_file() else []),
                      "unknown_units": unknown_units},
        )


def get_data_source() -> DataSource:
    """Фабрика источника по настройке DATA_SOURCE."""
    settings = get_settings()
    if settings.data_source == "csv":
        return CsvDataSource(settings.data_dir, as_of=settings.data_as_of)
    if settings.data_source == "excel":
        from app.data.excel import ExcelDataSource
        return ExcelDataSource(settings.data_dir, as_of=settings.data_as_of,
                               iek_lead_time_days=settings.iek_lead_time_days,
                               systeme_lead_time_days=settings.systeme_lead_time_days)
    if settings.data_source != "synthetic":
        raise ValueError("DATA_SOURCE должен быть excel, csv или synthetic")
    # Импорт здесь, чтобы избежать циклов.
    from app.data.synthetic import SyntheticDataSource
    return SyntheticDataSource(as_of=settings.data_as_of)


def source_files_available() -> bool:
    """Cheap readiness check; workbook parsing/quality is checked on actual load.

    The optional EKT reference never blocks availability of accounting inputs.
    No filenames or server paths are exposed through the readiness response.
    """
    settings = get_settings()
    if settings.data_source == "synthetic":
        return True
    root = Path(settings.data_dir)
    if settings.data_source == "csv":
        paths = [root / name for name in (
            'sales.csv', 'stock.csv', 'in_transit.csv', 'suppliers.csv',
            'sku_suppliers.csv', 'stockouts.csv',
        )]
    elif settings.data_source == "excel":
        from app.data.excel import SUPPLIER_FILES
        paths = [root / spec['folder'] / spec[kind]
                 for spec in SUPPLIER_FILES.values()
                 for kind in ('sales', 'monthly_sales', 'monthly_stock', 'moq', 'transit', 'seasonality')]
    else:
        return False
    try:
        for path in paths:
            with path.open('rb') as handle:
                if not handle.read(1):
                    return False
    except OSError:
        return False
    return True
