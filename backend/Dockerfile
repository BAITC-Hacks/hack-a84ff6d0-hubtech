# Backend (FastAPI + движок расчёта). Контекст сборки — корень репозитория,
# чтобы образ включал данные партнёра (IEK/, Systeme electric/, data/ekt/).
FROM python:3.12-slim

# Структура повторяет репозиторий: PROJECT_ROOT = /app, код в /app/backend.
WORKDIR /app/backend

# Зависимости отдельным слоем — кешируются между сборками.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код бэкенда.
COPY backend/ ./

# Данные, нужные источнику excel (без тяжёлых data/ml, data/clean — они в .dockerignore).
COPY IEK/ /app/IEK/
COPY ["Systeme electric/", "/app/Systeme electric/"]
COPY data/ekt/ /app/data/ekt/

ENV DATA_SOURCE=excel \
    PYTHONUNBUFFERED=1

EXPOSE 8017
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8017"]
