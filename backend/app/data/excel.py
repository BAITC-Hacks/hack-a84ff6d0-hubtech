"""Read-only import of the partner's 1C Excel exports.

The monthly quantity reports and dated inventory snapshots are kept separate
from transactions. No daily stockout intervals or customer identities are
inferred from sparse monthly reports. Formula cells use Excel's saved values;
the importer never executes formulas or follows external workbook links.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from functools import lru_cache
import math
from pathlib import Path
import re

import openpyxl
import pandas as pd

from app.data.adapter import Dataset


WAREHOUSE = "Алматы"
UNKNOWN_CATEGORY = "Категория не указана"
MONTH_NAMES = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
SUPPLIER_FILES = {
    "IEK": {
        "folder": "IEK", "name": "IEK",
        "sales": "Динамика продаж_2025-2026.xlsx",
        "monthly_sales": "Ежемесячные продажи в количественном выражении за последние 2 года.xlsx",
        "monthly_stock": "Ежемесячные остатки продукции за последние 2 года  ИЭК.xlsx",
        "moq": "MOQ  ИЭК.xlsx", "transit": "Путь ИЭК 22.09.2026.xlsx",
        "seasonality": "Сезонность ИЭК.xlsx",
    },
    "SYSTEME": {
        "folder": "Systeme electric", "name": "Systeme Electric",
        "sales": "Динамика продаж_Syseme Electric_2025-2026.xlsx",
        "monthly_sales": "Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx",
        "monthly_stock": "Ежемесячные остатки SystemElectric 2024-2026.xlsx",
        "moq": "MOQ SystemElectric.xlsx",
        "transit": "Товар в пути_SystemElectric на 22.09.2026.xlsx",
        "seasonality": "Сезонность SystemElectric 2024-2026.xlsx",
    },
}


def normalize_text(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def normalize_code(value) -> str:
    """Preserve textual leading zeroes and remove accidental whitespace."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return ""
        value = str(int(value)) if float(value).is_integer() else str(value)
    result = re.sub(r"\s+", "", normalize_text(value))
    if result.startswith("#") or result.casefold() in {"итого", "всего", "код", "номенклатура.код", "код1с"}:
        return ""
    return result


def parse_number(value) -> float | None:
    """Unknown/error is None; zero is a real observed value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(re.sub(r"\s+", "", str(value)).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_month(value) -> pd.Timestamp | None:
    if isinstance(value, (date, datetime)):
        return pd.Timestamp(value).normalize().replace(day=1)
    text = normalize_text(value).casefold()
    year = re.search(r"\b(20\d{2})\b", text)
    if not year:
        return None
    for index, name in enumerate(MONTH_NAMES, 1):
        if text.startswith(name):
            return pd.Timestamp(year=int(year.group(1)), month=index, day=1)
    return None


def parse_eta(header: str, source_date: date) -> date | None:
    """Prefer the receipt date over the earlier order date in an IEK header."""
    arrival = re.search(r"поступление\s+до\s+(\d{1,2}\.\d{1,2}\.\d{4})", header, re.I)
    if arrival:
        return datetime.strptime(arrival.group(1), "%d.%m.%Y").date()
    short = re.search(r"в\s+пути\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", header, re.I)
    if short:
        return date(int(short.group(3) or source_date.year), int(short.group(2)), int(short.group(1)))
    return None


def _value(row, index):
    return row[index] if index is not None and index < len(row) else None


def _column(headers, *names, required=True):
    for name in names:
        if name.casefold() in headers:
            return headers.index(name.casefold())
    if required:
        raise ValueError(f"В Excel нет обязательного столбца: {' / '.join(names)}")
    return None


@contextmanager
def _table(path: Path, anchor: str, audit: list[dict]):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        sheet = book.worksheets[0]
        # Partner files contain stale dimension records (e.g. A1:O19 while
        # seasonal October–December cells actually extend through row 22).
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        for row_number in range(1, 11):
            row = next(rows, ())
            headers = [normalize_text(v).casefold() for v in row]
            if anchor.casefold() in headers:
                audit.append({"file": str(path), "sheet": sheet.title, "header_row": row_number,
                              "sheets": [{"name": s.title, "state": s.sheet_state} for s in book.worksheets]})
                yield headers, rows
                return
        raise ValueError(f"Не найдена шапка '{anchor}' в {path.name}")
    finally:
        book.close()


class _Importer:
    def __init__(self, root: Path, as_of: date | None, lead_times: tuple[int, int]):
        self.root, self.as_of, self.lead_times = root, as_of, lead_times
        self.catalog: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.counts = Counter()
        self.warnings = [
            "Клиенты в выгрузках отсутствуют: крупные продажи определяются по транзакциям, без объединения по клиенту.",
            "Точных дат отсутствия товара нет: месячные остатки не превращаются в интервалы stockout, скрытый спрос не восстанавливается.",
            "Сводные месячные отчёты и товары в пути отнесены к Алматы по области предоставленных выгрузок; распределение по отдельным складам не задано.",
            "Пустые ячейки месячных продаж трактуются как нулевые продажи в разреженной выгрузке 1С; пустые остатки остаются неизвестными.",
            "Отрицательные транзакции исключены из спроса как возвраты/корректировки; отрицательный итог месяца ограничен нулём с сохранением исходного raw_qty.",
            "Категории IEK отсутствуют; категории Systeme Electric перенесены из столбца «Категория 2026» без расшифровки.",
            "Коэффициенты сезонности прочитаны из сохранённых значений Excel по агрегированным продажам поставщика; соответствие спросу конкретного артикула — допущение.",
            "Формулы Excel используются по сохранённым значениям; внешние ссылки не обновляются.",
        ]
        self.metadata = {"files": self.audit, "assumptions": {
            "warehouse": WAREHOUSE, "lead_time_days": dict(zip(SUPPLIER_FILES, lead_times)),
            "lead_times_confirmed": False, "monthly_sales_blank": "zero",
            "monthly_stock_blank": "unknown", "negative_transactions": "exclude_from_demand",
            "negative_monthly_sales": "clip_net_total_at_zero_preserve_raw_qty",
            "unknown_order_constraint": "operational_default_1_requires_confirmation",
        }}

    def path(self, supplier_id: str, kind: str) -> Path:
        spec = SUPPLIER_FILES[supplier_id]
        return self.root / spec["folder"] / spec[kind]

    def product(self, supplier_id, sku, name=None, unit=None, supplier_sku=None, category=None):
        existing = self.catalog.get(sku)
        if existing and existing["supplier_id"] != supplier_id:
            raise ValueError(f"Код 1С {sku} встречается у двух поставщиков; требуется явное сопоставление")
        item = self.catalog.setdefault(sku, {"sku": sku, "name": sku, "category": UNKNOWN_CATEGORY,
                                             "unit": "", "supplier_id": supplier_id, "supplier_sku": ""})
        for key, value in {"name": name, "unit": unit, "supplier_sku": supplier_sku, "category": category}.items():
            value = normalize_text(value)
            if value:
                item[key] = value

    def sales(self, supplier_id) -> pd.DataFrame:
        records = []
        with _table(self.path(supplier_id, "sales"), "Дата", self.audit) as (headers, rows):
            indices = {key: _column(headers, *names) for key, names in {
                "date": ("Дата",), "sku": ("Код",), "name": ("Номенклатура",),
                "unit": ("Ед.",), "warehouse": ("Склад",), "qty": ("Количество",)}.items()}
            order_col = _column(headers, "Номер", required=False)
            document_col = _column(headers, "Документ", required=False)
            for row in rows:
                sku = normalize_code(_value(row, indices["sku"]))
                if not sku:
                    continue
                qty = parse_number(_value(row, indices["qty"]))
                if qty is None:
                    self.counts["invalid_transaction_qty"] += 1
                    continue
                self.product(supplier_id, sku, _value(row, indices["name"]), _value(row, indices["unit"]))
                if qty <= 0:
                    self.counts["negative_transactions" if qty < 0 else "zero_transactions"] += 1
                    if qty < 0:
                        self.counts["negative_transaction_units"] += abs(qty)
                    continue
                warehouse = normalize_text(_value(row, indices["warehouse"]))
                if not warehouse:
                    self.counts["transactions_missing_warehouse"] += 1
                    warehouse = WAREHOUSE
                records.append({"date": _value(row, indices["date"]), "sku": sku, "qty": qty,
                                "warehouse": warehouse, "client_id": None, "price": None,
                                "order_id": normalize_code(_value(row, order_col)) or normalize_text(_value(row, document_col)) or None})
        frame = pd.DataFrame(records, columns=["date", "sku", "qty", "warehouse", "client_id", "price", "order_id"])
        frame["date"] = pd.to_datetime(frame["date"], format="mixed", dayfirst=True, errors="coerce").dt.normalize()
        self.counts["invalid_transaction_dates"] += int(frame["date"].isna().sum())
        return frame.dropna(subset=["date"])

    def monthly(self, supplier_id, kind) -> pd.DataFrame:
        records = []
        metric = "qty" if kind == "monthly_sales" else "on_hand"
        with _table(self.path(supplier_id, kind), "Номенклатура.Код", self.audit) as (headers, rows):
            code_col = _column(headers, "Номенклатура.Код")
            name_col = _column(headers, "Номенклатура")
            unit_col = _column(headers, "Ед.", "Ед.изм", required=False)
            article_col = _column(headers, "Артикул", required=False)
            months = [(i, parse_month(h)) for i, h in enumerate(headers) if parse_month(h) is not None]
            if not months:
                raise ValueError(f"Не найдены месяцы в {self.path(supplier_id, kind).name}")
            for row in rows:
                sku = normalize_code(_value(row, code_col))
                if not sku:
                    continue
                self.product(supplier_id, sku, _value(row, name_col), _value(row, unit_col), _value(row, article_col))
                for index, month in months:
                    raw = _value(row, index)
                    number = parse_number(raw)
                    if number is None:
                        if raw is None or normalize_text(raw) == "":
                            if metric == "qty":
                                number = 0.0
                                self.counts["monthly_sales_blank_cells"] += 1
                            else:
                                self.counts["monthly_stock_blank_cells"] += 1
                                continue
                        else:
                            self.counts[f"invalid_{kind}_cells"] += 1
                            continue
                    if number < 0:
                        self.counts[f"negative_{kind}_cells"] += 1
                    records.append({"sku": sku, "warehouse": WAREHOUSE, "month": month,
                                    metric: max(0.0, number), "raw_qty": number})
        frame = pd.DataFrame(records, columns=["sku", "warehouse", "month", metric, "raw_qty"])
        if frame.duplicated(["sku", "warehouse", "month"]).any():
            raise ValueError(f"Повторяющиеся коды/месяцы в {self.path(supplier_id, kind).name}")
        return frame

    def constraints(self, supplier_id) -> dict[str, dict]:
        result = {}
        with _table(self.path(supplier_id, "moq"), "№", self.audit) as (headers, rows):
            code_col = _column(headers, "Код 1с", "Номенклатура.Код")
            name_col = _column(headers, "Наименование", "Номенклатура")
            article_col = _column(headers, "Артикул поставщика", "Артикул")
            qty_col = _column(headers, "Мин. разр. к отгр.", "Кратность")
            for row in rows:
                sku = normalize_code(_value(row, code_col))
                if not sku:
                    continue
                self.product(supplier_id, sku, _value(row, name_col), supplier_sku=_value(row, article_col))
                qty = parse_number(_value(row, qty_col))
                valid = qty is not None and qty >= 1 and qty.is_integer()
                if not valid:
                    self.counts["invalid_order_constraints"] += 1
                result[sku] = {"pack_size": int(qty) if valid and supplier_id == "SYSTEME" else 1,
                               "min_order_qty": int(qty) if valid else 1,
                               "constraint_known": valid}
        return result

    def transit(self, supplier_id):
        transit, stock = [], []
        path = self.path(supplier_id, "transit")
        date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", path.name)
        if not date_match:
            raise ValueError(f"Не найдена дата снимка в имени {path.name}")
        source_date = datetime.strptime(date_match.group(), "%d.%m.%Y").date()
        with _table(path, "Код 1с", self.audit) as (headers, rows):
            code_col = _column(headers, "Код 1с")
            name_col = _column(headers, "Наименование")
            article_col = _column(headers, "Артикул поставщика", "Артикул ИЭК")
            stock_col = _column(headers, "Свободный остаток", required=False)
            category_col = _column(headers, "Категория 2026", required=False)
            dates = [(i, parse_eta(h, source_date)) for i, h in enumerate(headers) if parse_eta(h, source_date)]
            if not dates:
                raise ValueError(f"Не найдены даты поступления в {path.name}")
            for row in rows:
                sku = normalize_code(_value(row, code_col))
                if not sku:
                    continue
                name = normalize_text(_value(row, name_col))
                article = normalize_text(_value(row, article_col))
                # Two non-product marker rows in the IEK transit report use
                # codes 0/1 with no name or quantities. Keep real products with
                # those codes: neither a numeric-only filter nor a blanket ban.
                note = (supplier_id == "IEK" and not name
                        and ((sku == "0" and article.casefold().startswith("расширение "))
                             or (sku == "1" and article == "1"))
                        and all(parse_number(_value(row, col)) in (None, 0) for col, _ in dates))
                if note:
                    self.counts["ignored_transit_note_rows"] += 1
                    continue
                category = normalize_text(_value(row, category_col))
                self.product(supplier_id, sku, _value(row, name_col), supplier_sku=_value(row, article_col),
                             category=f"Категория {category}" if category else None)
                if stock_col is not None:
                    number = parse_number(_value(row, stock_col))
                    if number is not None:
                        if number < 0:
                            self.counts["negative_snapshot_stock"] += 1
                        stock.append({"sku": sku, "warehouse": WAREHOUSE, "on_hand": max(0.0, number),
                                      "as_of": pd.Timestamp(source_date), "source": "free_stock_snapshot"})
                    else:
                        self.counts["unknown_snapshot_stock"] += 1
                for index, eta in dates:
                    raw = _value(row, index)
                    number = parse_number(raw)
                    if number is not None and number > 0:
                        transit.append({"sku": sku, "warehouse": WAREHOUSE, "qty": number,
                                        "eta": pd.Timestamp(eta), "source_as_of": pd.Timestamp(source_date)})
                    elif raw is not None and (number is None or number < 0):
                        self.counts["invalid_transit_cells"] += 1
        return transit, stock

    def seasonal(self, supplier_id):
        records = []
        with _table(self.path(supplier_id, "seasonality"), "СЕЗОННОСТЬ", self.audit) as (headers, rows):
            month_col = _column(headers, "Месяц")
            factor_col = _column(headers, "Сезонность")
            for row in rows:
                month = normalize_text(_value(row, month_col)).casefold()
                if month not in MONTH_NAMES:
                    continue
                factor = parse_number(_value(row, factor_col))
                if factor is not None and factor > 0:
                    records.append({"supplier_id": supplier_id, "month": MONTH_NAMES.index(month) + 1, "factor": factor})
                if len(records) == 12:
                    break
        if len(records) != 12:
            raise ValueError(f"Нет 12 сохранённых коэффициентов сезонности для {supplier_id}")
        return records

    def load(self) -> Dataset:
        sales, monthly_sales, monthly_stock = [], [], []
        transit, snapshots, factors = [], [], []
        constraints = {}
        for supplier_id in SUPPLIER_FILES:
            sales.append(self.sales(supplier_id))
            monthly_sales.append(self.monthly(supplier_id, "monthly_sales"))
            monthly_stock.append(self.monthly(supplier_id, "monthly_stock"))
            constraints.update(self.constraints(supplier_id))
            arrivals, stock = self.transit(supplier_id)
            transit.extend(arrivals)
            snapshots.extend(stock)
            factors.extend(self.seasonal(supplier_id))
        sales = pd.concat(sales, ignore_index=True)
        if sales.empty:
            raise ValueError("В Excel не найдено продаж с корректной датой и положительным количеством")
        as_of = self.as_of or (sales["date"].max().date() + timedelta(days=1))
        catalog = pd.DataFrame(self.catalog.values()).sort_values("sku").reset_index(drop=True)
        sales = sales.merge(catalog[["sku", "name", "category"]], on="sku", validate="many_to_one")
        monthly_sales = pd.concat(monthly_sales, ignore_index=True)
        monthly_stock = pd.concat(monthly_stock, ignore_index=True)
        # Last observed opening balance is a dated fallback, not today's stock.
        eligible = monthly_stock[monthly_stock["month"] <= pd.Timestamp(as_of)]
        stock = eligible.sort_values("month").groupby(["sku", "warehouse"], as_index=False).tail(1).copy()
        stock = stock.rename(columns={"month": "as_of"}).drop(columns=["raw_qty"])
        stock["source"] = "monthly_opening_balance"
        dated = pd.DataFrame(snapshots, columns=["sku", "warehouse", "on_hand", "as_of", "source"])
        dated = dated[dated["as_of"] <= pd.Timestamp(as_of)]
        stock = pd.concat([stock, dated], ignore_index=True).sort_values("as_of")
        stock = stock.drop_duplicates(["sku", "warehouse"], keep="last").reset_index(drop=True)
        suppliers = pd.DataFrame([
            {"supplier_id": key, "name": spec["name"], "lead_time_days": lead, "min_order_qty": 1}
            for (key, spec), lead in zip(SUPPLIER_FILES.items(), self.lead_times)
        ])
        mappings = []
        for sku, product in self.catalog.items():
            values = constraints.get(sku, {"pack_size": 1, "min_order_qty": 1, "constraint_known": False})
            mappings.append({"sku": sku, "supplier_id": product["supplier_id"], **values})
        mappings = pd.DataFrame(mappings)
        unknown_constraints = int((~mappings["constraint_known"]).sum())
        missing_stock = len(set(catalog["sku"]) - set(stock["sku"]))
        fallback_stock = int((stock["source"] == "monthly_opening_balance").sum())
        stock_dates = sorted(stock["as_of"].dt.strftime("%Y-%m-%d").unique().tolist())
        self.warnings.extend([
            f"Сроки нового заказа приняты как допущения: IEK — {self.lead_times[0]} дн., Systeme Electric — {self.lead_times[1]} дн. Требуется подтверждение поставщиками.",
            f"Для {fallback_stock} позиций используются последние известные начальные остатки месяца с исходной датой; приход и расход после этой даты не восстанавливаются. Это устаревшие снимки, их нужно обновить перед заказом.",
            f"Для {missing_stock} позиций остаток неизвестен; отсутствие записи не подтверждает нулевой остаток.",
            f"Для {unknown_constraints} позиций нет корректного MOQ/кратности: временно применено 1, требуется подтверждение. IEK: минимальное количество; Systeme Electric: кратность упаковки.",
            f"Исключено {self.counts['negative_transactions']} отрицательных транзакций, {self.counts['zero_transactions']} нулевых; некорректные даты: {self.counts['invalid_transaction_dates']}.",
        ])
        if self.counts["negative_monthly_stock_cells"] or self.counts["negative_snapshot_stock"]:
            self.warnings.append("Отрицательные остатки ограничены нулём для расчёта доступности; исходные месячные значения сохранены в raw_qty.")
        invalid_cells = sum(count for key, count in self.counts.items() if key.startswith("invalid_"))
        if invalid_cells:
            self.warnings.append(f"Неиспользуемые некорректные значения/ошибки Excel: {invalid_cells}; подробные счётчики в метаданных импорта.")
        self.metadata.update({
            "counts": dict(self.counts), "as_of": as_of.isoformat(),
            "as_of_basis": "configured" if self.as_of else "day_after_latest_transaction",
            "transaction_start": sales["date"].min().date().isoformat(),
            "transaction_end": sales["date"].max().date().isoformat(),
            "stock_as_of": stock_dates, "monthly_stock_fallback_skus": fallback_stock,
            "unknown_stock_skus": missing_stock, "unknown_order_constraints": unknown_constraints,
            "rows": {"sales": len(sales), "catalog": len(catalog), "monthly_sales": len(monthly_sales),
                     "monthly_stock": len(monthly_stock), "stock": len(stock), "in_transit": len(transit)},
        })
        return Dataset(
            sales=sales, stock=stock, in_transit=pd.DataFrame(transit, columns=["sku", "warehouse", "qty", "eta", "source_as_of"]),
            suppliers=suppliers, sku_suppliers=mappings,
            stockouts=pd.DataFrame(columns=["sku", "warehouse", "start", "end"]),
            catalog=catalog, monthly_sales=monthly_sales, monthly_stock=monthly_stock,
            seasonality=pd.DataFrame(factors), as_of=as_of, source="excel",
            warnings=self.warnings, metadata=self.metadata,
        )


@lru_cache(maxsize=2)
def _cached_load(root: str, fingerprints: tuple, as_of: date | None, lead_times: tuple[int, int]):
    return _Importer(Path(root), as_of, lead_times).load()


class ExcelDataSource:
    def __init__(self, data_dir: str | Path, *, as_of: date | None = None,
                 iek_lead_time_days: int = 21, systeme_lead_time_days: int = 35):
        self.root = Path(data_dir).resolve()
        self.as_of = as_of
        self.lead_times = (iek_lead_time_days, systeme_lead_time_days)

    def load(self) -> Dataset:
        paths = [self.root / spec["folder"] / spec[kind]
                 for spec in SUPPLIER_FILES.values()
                 for kind in ("sales", "monthly_sales", "monthly_stock", "moq", "transit", "seasonality")]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise ValueError("Не найдены исходные Excel-файлы: " + ", ".join(missing))
        fingerprints = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
        # Recommendation filters and detection flags must not mutate cached frames.
        dataset = deepcopy(_cached_load(str(self.root), fingerprints, self.as_of, self.lead_times))
        # Enrichment is outside the workbook cache: a changed/missing JSON file
        # must immediately change coverage without rebuilding the XLSX dataset.
        from app.config import get_settings
        from app.data.enrichment import enrich_catalog
        settings = get_settings()
        return enrich_catalog(dataset, settings.ekt_catalog_path, enabled=settings.ekt_catalog_enabled)
