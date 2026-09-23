"""Генератор реалистичных синтетических данных под профиль ЭКТ (электротовары).

Специально закладывает паттерны, которые обязан ловить движок (Must-have):
  * сезонность (освещение/обогрев зимой);
  * устойчивый рост спроса по части позиций;
  * периоды stockout (нулевые продажи из-за отсутствия товара);
  * разовые крупные заказы одному клиенту (опт) — должны исключаться.
Данные обезличены (client_id — синтетические идентификаторы).
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.data.adapter import Dataset

DEFAULT_AS_OF = date(2026, 9, 23)

CATEGORIES = {
    "Кабельная продукция": ["Кабель ВВГ", "Кабель ПВС", "Провод СИП"],
    "Освещение": ["Светильник LED", "Лампа LED", "Прожектор LED"],
    "Автоматика": ["Автомат 16А", "УЗО 25А", "Диф.автомат"],
    "Розетки и выключатели": ["Розетка", "Выключатель", "Рамка"],
    "Щитовое оборудование": ["Щит навесной", "Бокс DIN", "Шина N"],
}

SUPPLIERS = [
    ("SUP-01", "IEK GROUP", 21, 50),
    ("SUP-02", "Schneider KZ", 35, 20),
    ("SUP-03", "ЭКФ Астана", 14, 30),
    ("SUP-04", "Legrand Central Asia", 42, 10),
    ("SUP-05", "Местный дистрибьютор", 7, 0),
]

WAREHOUSES = ["MSK-01", "ALA-02"]


def _seasonal_factor(day: date, amplitude: float, peak_month: int) -> float:
    """Годовая сезонность: пик в peak_month."""
    phase = 2 * np.pi * (day.month - peak_month) / 12.0
    return 1.0 + amplitude * np.cos(phase)


class SyntheticDataSource:
    """Источник синтетических данных, совместимый с интерфейсом DataSource."""

    def __init__(self, days: int = 730, seed: int = 42, as_of: date | None = None) -> None:
        if days < 1:
            raise ValueError("Синтетическая история должна содержать хотя бы один день")
        self.days = days
        self.seed = seed
        self.as_of = as_of or DEFAULT_AS_OF
        self.rng = np.random.default_rng(seed)

    def load(self) -> Dataset:
        # Repeated loads of one source must reproduce the same fixture, too.
        self.rng = np.random.default_rng(self.seed)
        sku_defs = self._build_sku_catalog()
        sales, stockouts = self._build_sales(sku_defs)
        stock = self._build_stock(sku_defs)
        in_transit = self._build_in_transit(sku_defs)
        suppliers = pd.DataFrame(
            SUPPLIERS, columns=["supplier_id", "name", "lead_time_days", "min_order_qty"]
        )
        sku_suppliers = pd.DataFrame(
            [{"sku": s["sku"], "supplier_id": s["supplier_id"], "pack_size": s["pack_size"]}
             for s in sku_defs]
        )
        return Dataset(
            sales=sales,
            stock=stock,
            in_transit=in_transit,
            suppliers=suppliers,
            sku_suppliers=sku_suppliers,
            stockouts=stockouts,
            catalog=pd.DataFrame(sku_defs)[["sku", "name", "category", "unit", "supplier_id"]],
            as_of=self.as_of,
            source="synthetic",
            warnings=["Синтетические данные для проверки работы; не являются фактическими продажами или остатками компании."],
            metadata={"seed": self.seed, "history_days": self.days,
                      "transaction_end": (self.as_of - timedelta(days=1)).isoformat()},
        )

    # ---------- каталог SKU ----------
    def _build_sku_catalog(self) -> list[dict]:
        defs: list[dict] = []
        idx = 1
        for category, names in CATEGORIES.items():
            for name in names:
                sku = f"EKT-{idx:04d}"
                supplier_id = SUPPLIERS[self.rng.integers(0, len(SUPPLIERS))][0]
                base = float(self.rng.integers(3, 40))          # базовый дневной спрос
                # часть позиций сезонные (освещение — зимой, кабель — летом)
                if category == "Освещение":
                    amp, peak = 0.55, 12
                elif category == "Кабельная продукция":
                    amp, peak = 0.4, 7
                else:
                    amp, peak = float(self.rng.uniform(0.0, 0.2)), int(self.rng.integers(1, 13))
                trend = float(self.rng.choice([0.0, 0.0, 0.0004, 0.0008]))  # устойчивый рост
                defs.append({
                    "sku": sku, "name": name, "category": category,
                    "unit": "м" if category == "Кабельная продукция" else "шт",
                    "supplier_id": supplier_id,
                    "base": base, "amp": amp, "peak": peak, "trend": trend,
                    "price": round(float(self.rng.uniform(200, 15000)), 2),
                    "pack_size": float(self.rng.choice([1, 1, 5, 10])),
                })
                idx += 1
        return defs

    # ---------- продажи + stockout ----------
    def _build_sales(self, sku_defs: list[dict]):
        start = self.as_of - timedelta(days=self.days)
        rows: list[dict] = []
        stockout_rows: list[dict] = []
        clients = [f"CL-{i:04d}" for i in range(1, 120)]

        for s in sku_defs:
            wh = WAREHOUSES[self.rng.integers(0, len(WAREHOUSES))]
            s["warehouse"] = wh
            # запланируем 0-1 период stockout на позицию
            stockout_window = None
            if self.rng.random() < 0.45 and self.days > 100:
                so_start_off = int(self.rng.integers(60, self.days - 40))
                so_len = int(self.rng.integers(7, 25))
                stockout_window = (so_start_off, so_start_off + so_len)
                stockout_rows.append({
                    "sku": s["sku"], "warehouse": wh,
                    "start": start + timedelta(days=stockout_window[0]),
                    "end": start + timedelta(days=stockout_window[1]),
                })

            for d in range(self.days):
                cur = start + timedelta(days=d)
                if cur.weekday() >= 5:  # выходные — почти нет продаж (B2B)
                    if self.rng.random() > 0.15:
                        continue
                # спрос дня
                seas = _seasonal_factor(cur, s["amp"], s["peak"])
                growth = 1.0 + s["trend"] * d
                lam = max(0.0, s["base"] * seas * growth)
                in_stockout = (
                    stockout_window is not None
                    and stockout_window[0] <= d <= stockout_window[1]
                )
                if in_stockout:
                    continue  # нет товара — нет продаж (спрос будет "потерян")
                qty = self.rng.poisson(lam)
                if qty <= 0:
                    continue
                rows.append({
                    "date": cur, "sku": s["sku"], "name": s["name"],
                    "category": s["category"], "qty": float(qty),
                    "price": s["price"],
                    "client_id": clients[self.rng.integers(0, len(clients))],
                    "warehouse": wh,
                })

            # разовые крупные заказы (опт одному клиенту) — 0-2 на позицию
            n_bulk = int(self.rng.integers(0, 3))
            big_client = f"CL-B{self.rng.integers(1, 20):03d}"
            for _ in range(n_bulk):
                d = int(self.rng.integers(0, self.days))
                cur = start + timedelta(days=d)
                rows.append({
                    "date": cur, "sku": s["sku"], "name": s["name"],
                    "category": s["category"],
                    "qty": float(s["base"] * self.rng.integers(20, 60)),  # огромный объём
                    "price": s["price"], "client_id": big_client, "warehouse": wh,
                })

        sales = pd.DataFrame(rows, columns=["date", "sku", "name", "category", "qty", "price", "client_id", "warehouse"])
        sales = sales.sort_values("date").reset_index(drop=True)
        stockouts = pd.DataFrame(
            stockout_rows, columns=["sku", "warehouse", "start", "end"]
        )
        return sales, stockouts

    # ---------- текущие остатки ----------
    def _build_stock(self, sku_defs: list[dict]) -> pd.DataFrame:
        rows = []
        for s in sku_defs:
            wh = s["warehouse"]
            # часть позиций специально с низким остатком (срочные)
            days_cover = float(self.rng.choice([1, 3, 5, 10, 20, 45]))
            on_hand = round(s["base"] * days_cover, 0)
            rows.append({"sku": s["sku"], "warehouse": wh, "on_hand": on_hand, "as_of": self.as_of})
        return pd.DataFrame(rows)

    # ---------- товары в пути ----------
    def _build_in_transit(self, sku_defs: list[dict]) -> pd.DataFrame:
        rows = []
        for s in sku_defs:
            if self.rng.random() < 0.3:
                rows.append({
                    "sku": s["sku"],
                    "warehouse": s["warehouse"],
                    "qty": round(s["base"] * float(self.rng.integers(5, 20)), 0),
                    "eta": self.as_of + timedelta(days=int(self.rng.integers(3, 30))),
                    "source_as_of": self.as_of,
                })
        return pd.DataFrame(rows, columns=["sku", "warehouse", "qty", "eta", "source_as_of"])
