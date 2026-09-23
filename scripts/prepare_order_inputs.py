#!/usr/bin/env python3
"""Подготовка проверяемого снимка входов заказа IEK. Количество заказа не рассчитывается.

python scripts/prepare_order_inputs.py
Неизвестные значения остаются пустыми, исторический остаток не подменяет свободный.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from prepare_ml_data import ROOT, FILES, prepare, read_transit


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("supplier_id") != "IEK":
        raise ValueError("Этот снимок предназначен для IEK")
    if not isinstance(config.get("warehouse"), str) or not config["warehouse"].strip():
        raise ValueError("Не задан склад")
    for key in ("lead_time_days", "review_period_days", "stock_max_age_days", "transit_max_age_days"):
        val = config.get(key)
        if type(val) is not int or val < (1 if key == "review_period_days" else 0):
            raise ValueError(f"Некорректный параметр {key}")
    if not isinstance(config.get("service_level"), (float, int)) or not .5 <= config["service_level"] < 1:
        raise ValueError("service_level должен быть от 0.5 до 1 (не включая 1)")
    for key in ("parameters_confirmed", "sales_semantics_confirmed"):
        if type(config.get(key)) is not bool:
            raise ValueError(f"{key} должен быть true/false")
    for key in ("blank_sales", "negative_sales"):
        if config.get(key) not in ("missing", "zero"):
            raise ValueError(f"Некорректное правило {key}")
    date = pd.Timestamp(config["as_of"])
    if pd.isna(date):
        raise ValueError("Не задана дата расчёта")
    config["as_of"] = date.normalize().date().isoformat()
    return config


def csv(path, required, numeric=(), dates=()):
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"{path}: отсутствуют поля {sorted(missing)}")
    for column in ("sku", "unit", "warehouse", "order_unit", "transit_unit"):
        if column in frame:
            frame[column] = frame[column].str.strip()
    if "sku" in frame and frame.sku.eq("").any():
        raise ValueError(f"{path}: пустой код товара")
    for column in numeric:
        if column not in frame:
            frame[column] = np.nan
        raw = frame[column]
        values = pd.to_numeric(raw, errors="coerce")
        invalid = raw.notna() & raw.astype(str).str.strip().ne("") & (~np.isfinite(values))
        if invalid.any():
            raise ValueError(f"{path}: некорректное число в {column}")
        frame[column] = values
    for column in dates:
        raw = frame[column]
        values = pd.to_datetime(raw, errors="coerce")
        if (raw.ne("") & values.isna()).any():
            raise ValueError(f"{path}: некорректная дата в {column}")
        frame[column] = values
    return frame


def positive(value):
    return pd.notna(value) and np.isfinite(value) and value > 0


def bool_column(values):
    normalized = values.astype(str).str.strip().str.casefold()
    if not normalized.isin(["true", "false"]).all():
        raise ValueError("В подготовленных данных ожидалось логическое значение true/false")
    return normalized.eq("true")


def require_unique(frame, columns, label):
    if frame.duplicated(columns).any():
        raise ValueError(f"{label}: повтор ключа {columns}; нужна сверка")


def groups(frame):
    return dict(tuple(frame.groupby("sku", sort=False))) if not frame.empty else {}


def current_stock_row(rows, warehouse, unit, as_of, max_age):
    result = dict(on_hand=np.nan, stock_as_of=pd.NaT, stock_status="missing")
    if rows is None or rows.empty:
        return result
    rows = rows[rows.warehouse.eq(warehouse)]
    if rows.empty:
        result["stock_status"] = "warehouse_mismatch"
        return result
    if rows.as_of.isna().any():
        result["stock_status"] = "date_missing"
        return result
    past = rows[rows.as_of.dt.normalize() <= as_of]
    if past.empty:
        result["stock_status"] = "future_snapshot"
        return result
    row = past.sort_values("as_of").iloc[-1]
    result["stock_as_of"] = row.as_of
    if not unit or row.unit != unit:
        result["stock_status"] = "unit_mismatch"
    elif pd.isna(row.free_stock):
        result["stock_status"] = "quantity_missing"
    elif row.free_stock < 0:
        result["stock_status"] = "negative_quantity"
    elif (as_of - row.as_of.normalize()).days > max_age:
        result["stock_status"] = "stale"
    else:
        result.update(on_hand=float(row.free_stock), stock_status="available")
    return result


def build_snapshot(config, products, monthly, moq, transit, comparison, seasonality,
                   transit_catalog, transit_snapshot_date, cutoff, current_stock=None, purchase_rules=None):
    as_of = pd.Timestamp(config["as_of"])
    horizon = config["lead_time_days"] + config["review_period_days"]
    horizon_end = as_of + pd.Timedelta(days=horizon)
    cutoff = min(pd.Timestamp(cutoff), as_of).to_period("M").to_timestamp()
    require_unique(products, ["sku"], "Каталог")
    require_unique(moq, ["sku"], "MOQ")
    for frame, columns, label in [(current_stock, ["sku", "warehouse", "as_of"], "Остатки"),
                                  (purchase_rules, ["sku"], "Правила заказа")]:
        if frame is not None:
            require_unique(frame, columns, label)
            extra = set(frame.sku) - set(products.sku)
            if extra:
                raise ValueError(f"{label}: коды вне каталога: {sorted(extra)[:10]}")
    monthly_groups, transit_groups = groups(monthly), groups(transit)
    comparison_groups = groups(comparison[comparison.month < cutoff])
    stocks = groups(current_stock) if current_stock is not None else {}
    rules = purchase_rules.set_index("sku").to_dict("index") if purchase_rules is not None else {}
    minimums = moq.set_index("sku").min_order_qty.to_dict()
    snapshot_age = (as_of - transit_snapshot_date).days
    transit_snapshot_available = 0 <= snapshot_age <= config["transit_max_age_days"]
    records, schedule = [], []
    for product in products.itertuples(index=False):
        sku, unit = product.sku, product.unit
        missing, review = [], []
        history = monthly_groups.get(sku, monthly.iloc[:0])
        complete = history[history.month < cutoff]
        known_sales = complete.sales_qty_raw.notna() & complete.sales_qty_raw.ge(0)
        raw_history_months = int(known_sales.sum())
        negative_months = int(complete.sales_qty_raw.lt(0).sum())
        blank_months = int(complete.sales_status.eq("blank").sum())
        # Модельное количество учитывает только явно выбранную политику подготовки.
        usable_months = int(complete.target_qty.notna().sum())
        if usable_months == 0:
            missing.append("sales_history")
        if not unit:
            missing.append("stock_unit")
        if not config["parameters_confirmed"]:
            review.append("parameters_unconfirmed")
        if not config["sales_semantics_confirmed"]:
            review.append("sales_semantics_unconfirmed")
        if negative_months:
            review.append("negative_monthly_sales")
        raw_stock = history[(history.month <= as_of) & history.stock_qty_raw.notna() & history.stock_qty_raw.ge(0)]
        historic_qty, historic_date = np.nan, pd.NaT
        if not raw_stock.empty:
            last_stock = raw_stock.sort_values("month").iloc[-1]
            historic_qty, historic_date = last_stock.stock_qty_raw, last_stock.month
        stock = current_stock_row(stocks.get(sku), config["warehouse"], unit, as_of, config["stock_max_age_days"])
        if stock["stock_status"] != "available":
            missing.append("current_free_stock")
        rule = rules.get(sku, {})
        order_unit = str(rule.get("order_unit", "") or "").strip()
        order_factor = rule.get("stock_units_per_order_unit", np.nan)
        pack = rule.get("pack_size", np.nan)
        minimum = rule.get("min_order_qty", np.nan)
        minimum_source = "purchase_rules"
        if pd.isna(minimum):
            minimum = minimums.get(sku, np.nan)
            minimum_source = "moq_file" if positive(minimum) else "unknown"
        if not positive(minimum):
            missing.append("min_order_qty")
        if not positive(pack):
            missing.append("pack_size")
        if not order_unit or not positive(order_factor):
            missing.append("purchase_unit_conversion")
        if order_unit == unit and positive(order_factor) and order_factor != 1:
            raise ValueError(f"{sku}: для одинаковых складской и закупочной единиц коэффициент должен быть 1")
        transit_unit = str(rule.get("transit_unit", "") or "").strip()
        transit_factor = rule.get("stock_units_per_transit_unit", np.nan)
        if transit_unit == unit and positive(transit_factor) and transit_factor != 1:
            raise ValueError(f"{sku}: для одинаковых единиц пути и склада коэффициент должен быть 1")
        receipts = transit_groups.get(sku, transit.iloc[:0])
        eligible_qty, in_transit_base, has_unusable_receipt = 0.0, 0.0, False
        receipt_count = 0
        raw_receipts_complete = True
        for receipt in receipts.itertuples(index=False):
            if pd.isna(receipt.eta):
                bucket = "eta_unknown"
            elif receipt.eta < as_of:
                bucket = "overdue"
            elif receipt.eta >= horizon_end:
                bucket = "after_horizon"
            else:
                bucket = "in_horizon"
            qty_valid = pd.notna(receipt.qty_raw) and receipt.qty_raw >= 0
            unit_known = bool(transit_unit) and positive(transit_factor) and bool(unit)
            qty_base = receipt.qty_raw * transit_factor if qty_valid and unit_known else np.nan
            reasons = []
            if not transit_snapshot_available:
                reasons.append("snapshot_unavailable")
            if bucket != "in_horizon":
                reasons.append(bucket)
            if not qty_valid:
                reasons.append("quantity_invalid")
            if not unit_known:
                reasons.append("unit_conversion_unknown")
            schedule.append(dict(sku=sku, shipment=receipt.shipment, source_as_of=transit_snapshot_date,
                                 eta=receipt.eta, qty_raw=receipt.qty_raw, transit_unit=transit_unit,
                                 qty_stock_units=qty_base, eta_bucket=bucket, usable=not reasons,
                                 reasons=";".join(reasons)))
            if bucket in {"overdue", "eta_unknown"}:
                review.append("unresolved_receipt_dates")
            if bucket in {"in_horizon", "eta_unknown"}:
                receipt_count += 1
                if qty_valid:
                    eligible_qty += receipt.qty_raw
                if not qty_valid or bucket == "eta_unknown":
                    raw_receipts_complete = False
                if not qty_valid or not unit_known or bucket == "eta_unknown":
                    has_unusable_receipt = True
                else:
                    in_transit_base += qty_base
        covered = sku in transit_catalog
        if not covered or not transit_snapshot_available or has_unusable_receipt:
            in_transit_base = np.nan
            missing.append("in_transit")
        comparison_rows = comparison_groups.get(sku, comparison.iloc[:0])
        mismatch_count = int((comparison_rows.comparable & ~comparison_rows.matches_signed).sum())
        if mismatch_count:
            review.append("monthly_transaction_mismatch")
        status = "incomplete" if missing else "review" if review else "ready"
        records.append(dict(
            sku=sku, supplier_id="IEK", supplier_sku=product.supplier_sku, name=product.product_name,
            unit=unit, warehouse=config["warehouse"], as_of=as_of, horizon_days=horizon,
            lead_time_days=config["lead_time_days"], review_period_days=config["review_period_days"],
            service_level=config["service_level"], **stock,
            historical_opening_stock=historic_qty, historical_stock_as_of=historic_date,
            history_observed_months=raw_history_months, history_usable_months=usable_months,
            blank_sales_months=blank_months, negative_sales_months=negative_months,
            last_complete_month=cutoff - pd.DateOffset(months=1),
            min_order_qty=minimum, min_order_qty_source=minimum_source, pack_size=pack,
            order_unit=order_unit, stock_units_per_order_unit=order_factor,
            reported_in_transit_qty=eligible_qty if covered and transit_snapshot_available and raw_receipts_complete else np.nan,
            in_transit=in_transit_base, receipts_in_horizon=receipt_count,
            in_transit_catalog=covered, transit_snapshot_as_of=transit_snapshot_date,
            source_mismatch_months=mismatch_count, input_status=status,
            ready_for_calculation=status == "ready", missing_fields=";".join(missing),
            review_reasons=";".join(sorted(set(review))),
        ))
    rows = pd.DataFrame(records)
    schedule = pd.DataFrame(schedule, columns=["sku", "shipment", "source_as_of", "eta", "qty_raw", "transit_unit",
                                               "qty_stock_units", "eta_bucket", "usable", "reasons"])
    counts = Counter(field for fields in rows.missing_fields for field in fields.split(";") if field)
    report = {
        "configuration": config, "horizon_days": horizon,
        "horizon_start": as_of.date().isoformat(), "horizon_end_exclusive": horizon_end.date().isoformat(),
        "products": len(rows), "status_counts": rows.input_status.value_counts().to_dict(),
        "missing_field_counts": dict(counts), "current_stock_statuses": rows.stock_status.value_counts().to_dict(),
        "products_by_unit": rows.unit.replace("", "unknown").value_counts().to_dict(),
        "receipt_rows": len(schedule), "seasonality_months": len(seasonality),
        "notes": [
            "Это проверенный снимок входов; прогноз, страховой запас и заказ рассчитываются на следующем этапе.",
            "Исторический начальный остаток не считается текущим свободным остатком; неизвестное не заменяется нулём.",
            "Для товара в каталоге актуального файла пути отсутствие строк поставки означает отсутствие зарегистрированных поступлений в этом отчёте.",
            "ETA «поступление до» трактуется как крайняя плановая дата, а не подтверждённое фактическое поступление.",
            "Готовая сезонность сохранена отдельно; её использование в прогнозе требует проверки на истории без утечки будущего.",
            "Минимум отгрузки IEK не доказывает кратность упаковки или равенство закупочной и складской единиц.",
            "Точность прогноза и возможность восстановления упущенного спроса этим отчётом не подтверждаются.",
        ],
    }
    return rows, schedule, report


def run(config_path, input_dir, prepared_dir, output_dir, stock_path=None, rules_path=None, refresh=True):
    config = load_config(config_path)
    if refresh:
        prepare(input_dir, prepared_dir, config["as_of"], config["blank_sales"], config["negative_sales"])
    metadata = json.loads((prepared_dir / "preparation_report.json").read_text(encoding="utf-8"))
    if metadata["as_of"] != config["as_of"] or any(metadata["policies"][key] != config[key] for key in ("blank_sales", "negative_sales")):
        raise ValueError("Подготовленные данные не соответствуют настройкам. Запустите без --reuse-prepared")
    for kind, filename in FILES.items():
        digest = hashlib.sha256((input_dir / filename).read_bytes()).hexdigest()
        if metadata["sources"][kind]["sha256"] != digest:
            raise ValueError(f"Изменился источник {filename}. Запустите без --reuse-prepared")
    products = csv(prepared_dir / "products.csv", ["sku", "product_name", "unit", "supplier_sku"])
    monthly = csv(prepared_dir / "monthly_panel.csv", ["sku", "month", "sales_status", "stock_status", "sales_qty_raw", "stock_qty_raw", "target_qty"],
                  numeric=["sales_qty_raw", "stock_qty_raw", "target_qty"], dates=["month"])
    moq = csv(prepared_dir / "moq.csv", ["sku", "min_order_qty"], numeric=["min_order_qty"])
    transit = csv(prepared_dir / "in_transit.csv", ["sku", "shipment", "eta", "qty_raw"], numeric=["qty_raw"], dates=["eta"])
    comparison = csv(prepared_dir / "reconciliation.csv", ["sku", "month", "comparable", "matches_signed"], dates=["month"])
    for column in ("comparable", "matches_signed"):
        comparison[column] = bool_column(comparison[column])
    seasonality = csv(prepared_dir / "provided_seasonality.csv", ["month_number", "provided_factor"], numeric=["month_number", "provided_factor"])
    catalog = {}
    read_transit(input_dir / FILES["transit"], catalog, [])
    # Дата снимка извлекается из имени исходника, даже когда нет строк поступлений.
    snapshot_date = pd.Timestamp(datetime.strptime(re.search(r"\d{2}\.\d{2}\.\d{4}", FILES["transit"])[0], "%d.%m.%Y"))
    stock = None
    if stock_path:
        stock = csv(stock_path, ["sku", "warehouse", "unit", "as_of", "free_stock"], numeric=["free_stock"], dates=["as_of"])
    rules = None
    if rules_path:
        rules = csv(rules_path, ["sku", "order_unit", "stock_units_per_order_unit", "pack_size"],
                    numeric=["stock_units_per_order_unit", "pack_size", "min_order_qty", "stock_units_per_transit_unit"])
        if "transit_unit" not in rules:
            rules["transit_unit"] = ""
        for column in ["stock_units_per_order_unit", "pack_size", "min_order_qty", "stock_units_per_transit_unit"]:
            if (rules[column].notna() & rules[column].le(0)).any():
                raise ValueError(f"{rules_path}: {column} должен быть положительным или пустым")
    rows, schedule, report = build_snapshot(config, products, monthly, moq, transit, comparison, seasonality,
                                             set(catalog), snapshot_date, metadata["data_cutoff_exclusive"], stock, rules)
    report["sources"] = metadata["sources"]
    report["additional_sources"] = {name: {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                    for name, path in [("current_stock", stock_path), ("purchase_rules", rules_path)] if path}
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in [("order_inputs", rows), ("arrival_schedule", schedule), ("provided_seasonality", seasonality)]:
        frame.to_csv(output_dir / f"{name}.csv", index=False, encoding="utf-8-sig", na_rep="")
    (output_dir / "input_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/iek_replenishment.json")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "IEK")
    parser.add_argument("--prepared-dir", type=Path, default=ROOT / "data/ml/iek")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/orders/iek")
    parser.add_argument("--current-stock", type=Path)
    parser.add_argument("--purchase-rules", type=Path)
    parser.add_argument("--reuse-prepared", action="store_true", help="Не пересоздавать подготовленные данные; проверить SHA-256 источников")
    args = parser.parse_args()
    try:
        report = run(args.config, args.input_dir, args.prepared_dir, args.output_dir,
                     args.current_stock, args.purchase_rules, not args.reuse_prepared)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Ошибка: {error}\n")
    print(json.dumps({key: report[key] for key in ("horizon_days", "products", "status_counts", "missing_field_counts")}, ensure_ascii=False, indent=2))
    print(f"Снимок входов: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
