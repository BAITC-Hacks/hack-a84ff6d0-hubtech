# Развёртывание

Сервис = FastAPI (Python) + статический фронт (React/Vite). Данные — выгрузки 1С в `IEK/` и `Systeme electric/`.

---

## Что нужно на сервере
- Python 3.9+, Node 18+ (для сборки фронта), опционально Docker и nginx.
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
npm install
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
        proxy_read_timeout 120s;   # полный расчёт на 250k строк ~26с
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
| `SERVICE_LEVEL` | 0.95 | Уровень сервиса для страхового запаса |
| `REVIEW_PERIOD_DAYS` | 14 | Период проверки |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | — / `gpt-4o-mini` | LLM-обоснования (опц.) |

---

## Безопасность (перед публичным доступом)
- Ограничить CORS реальным доменом фронта (сейчас `*` для дева).
- Не коммитить `.env` и выгрузки с чувствительными данными (см. `.gitignore`).
- Таймаут прокси ≥ времени полного расчёта; при росте объёма — кеш/фоновые задачи.
- Добавить аутентификацию перед мультипользовательским использованием (Roadmap).
