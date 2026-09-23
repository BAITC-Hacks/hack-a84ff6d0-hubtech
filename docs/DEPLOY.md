# Развёртывание

Сервис = FastAPI (Python) + статический фронт (React/Vite). Учётные данные — выгрузки в `IEK/` и `Systeme electric/`. Дополнительный товарный справочник — локальный `data/ekt/catalog.json`. Установленная или подключённая 1С для прототипа не нужна.

---

## Что нужно на сервере
- Python 3.10+, Node 20.19+ в ветке 20.x либо 22.12+ (для сборки фронта), опционально Docker и nginx.
- Каталоги выгрузок 1С (`IEK/`, `Systeme electric/`) рядом с проектом.
- Опционально `OPENAI_API_KEY` (без него обоснования — по шаблону).

---

## Подготовка (один раз)

```bash
# Backend
cd backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env        # задать DATA_SOURCE, сроки, ключ (опц.)

# Frontend (сборка статики)
cd ../frontend
npm ci
npm run build               # → frontend/dist
```

---

## Путь 1. Bare-metal (dev / небольшой прод)

```bash
# Терминал 1 — backend
cd backend
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8017

# Терминал 2 — фронт (dev, hot-reload)
cd frontend
npm run dev                 # http://localhost:5173, прокси /api → :8017
```

Для продакшна фронт отдаётся как статика (`frontend/dist`) через nginx, API — через uvicorn.

---

## Путь 2. systemd (автозапуск backend)

```ini
# /etc/systemd/system/ekt-replenishment.service
[Unit]
Description=EKT Replenishment API
After=network.target

[Service]
WorkingDirectory=/opt/ekt/backend
ExecStart=/opt/ekt/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8017
Restart=always
Environment=DATA_SOURCE=excel

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now ekt-replenishment
```

---

## Домен и HTTPS (nginx reverse proxy)

```nginx
server {
    server_name ekt.example.kz;
    root /opt/ekt/frontend/dist;

    location /api/ {
        proxy_pass http://127.0.0.1:8017;
        proxy_read_timeout 120s;   # подобрать по измеренному времени расчёта
    }
    location / {
        try_files $uri /index.html;
    }
}
```
HTTPS — через certbot/Let's Encrypt.

---

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DATA_SOURCE` | `excel` | Источник: `excel` / `csv` / `synthetic` |
| `DATA_DIR` | `.` | Каталог с `IEK/`, `Systeme electric/` (или CSV) |
| `DATA_AS_OF` | — | Зафиксировать дату расчёта (`YYYY-MM-DD`) |
| `IEK_LEAD_TIME_DAYS` / `SYSTEME_LEAD_TIME_DAYS` | 21 / 35 | Сроки поставки (допущения) |
| `EKT_CATALOG_ENABLED` | `true` | Обогащение из локального JSON; `false/0/no/off` отключают |
| `EKT_CATALOG_PATH` | `data/ekt/catalog.json` | Относительный путь от корня проекта или абсолютный |
| `SERVICE_LEVEL` | 0.95 | Уровень сервиса для страхового запаса |
| `REVIEW_PERIOD_DAYS` | 14 | Период проверки |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | — / `gpt-4o-mini` | LLM-обоснования (опц.) |

---

## Безопасность (перед публичным доступом)
- Ограничить CORS реальным доменом фронта (сейчас `*` для дева).
- Не коммитить `.env` и выгрузки с чувствительными данными (см. `.gitignore`).
- Таймаут прокси ≥ времени полного расчёта; при росте объёма — кеш/фоновые задачи.
- Добавить аутентификацию перед мультипользовательским использованием (Roadmap).

## Обновление внешнего справочника

Каталог сайта синхронизируется отдельной CLI-командой; расчёт и экспорт не требуют доступа к ekt.kz. Подготовьте JSON на доступной машине и разместите по `EKT_CATALOG_PATH` либо выполните ограниченный проход на сервере. Параметры и ограничения — [КАТАЛОГ_EKT.md](КАТАЛОГ_EKT.md).

Не смешивайте этот процесс с обновлением остатков/продаж из 1С. Для регулярных учётных обновлений сначала согласуется файловая выгрузка по расписанию; HTTP/OData выбирается под конкретную конфигурацию. Живого подключения и записи заказов в 1С в текущем сервисе нет: [ИНТЕГРАЦИЯ_1С.md](ИНТЕГРАЦИЯ_1С.md).
