# Развёртывание и эксплуатация Umytpa

Поставка: статическая сборка React, nginx с HTTPS, FastAPI, отдельный worker,
SQLite на локальном диске и ежедневный systemd timer резервного копирования.
API слушает только `127.0.0.1:8017`; браузер использует один origin и `/api`.
Реализованная конфигурация запускает ровно один worker и один процесс API.

Комплект появился при интеграции `baitc/frontend` (`5a0ace2`). Он содержит
рабочие механизмы входа, постоянных заказов, очереди и копирования, но не
закрывает полное [ТЗ платформы](ТЗ_платформы.md): пока две роли и самостоятельное
подтверждение строк менеджером, отсутствуют независимое согласование, области
склада/поставщика и управляемые партии импорта. Перед ежедневной работой
требуется приёмка на целевом сервере с владельцем данных и процесса закупки.

## 0. Воспроизводимый локальный запуск

Нужны Python **3.12**, Node.js **22.19+ в ветке 22.x или 24.x** и npm. Зависимости устанавливаются из
lock-файлов. Текущая интеграция проверялась также на Python 3.12.14 / Node 24.19;
старый Python 3.9 не является поддерживаемой средой сервиса.

Подготовка из корня проекта:

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

В локальном `backend/.env` задайте `APP_ENV=development`,
`APP_ORIGIN=http://127.0.0.1:5173` и пустой `OPENAI_API_KEY`. Сохраните одинаковые
настройки/`APP_DB_PATH` для CLI, API и worker. Для реальных данных оставьте
`DATA_SOURCE=excel`, `DATA_DIR=.`; в корне находятся `IEK/`, `Systeme electric/`
и необязательный `data/ekt/catalog.json`.

Оставаясь в `backend/` после подготовки, один раз создайте именную учётную запись (пароль вводится скрыто):

```bash
.venv/bin/python -m app.manage migrate
.venv/bin/python -m app.manage create-admin --username admin
```

Три отдельных терминала из корня проекта:

```bash
# 1: API
cd backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8017 --no-access-log
```

```bash
# 2: worker — без него новый расчёт вернёт 503
cd backend
.venv/bin/python -m app.worker
```

```bash
# 3: UI
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Открывайте `http://127.0.0.1:5173` (один hostname с `APP_ORIGIN`, не смесь localhost
и 127.0.0.1). Vite проксирует `/api` к 8017; для отдельного тестового API можно
задать `API_PROXY_TARGET`. Swagger доступен на API `/docs` только в development.
Cookie Secure остаётся обязательной для production HTTPS.

Проверки из корня проекта:

```bash
cd backend
RUN_REAL_EXCEL_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m tests.validate_must_have
```

```bash
backend/.venv/bin/python -m unittest discover -s scripts -p 'test_*.py' -v
backend/.venv/bin/python qa/smoke_service.py
backend/.venv/bin/python qa/smoke_service.py --real-data
```

```bash
cd frontend
npm test
npm run lint
npm run build
```

HTTP smoke создаёт временную БД и собственные API/worker-процессы, проверяет
вход, CSRF, владельцев, revision, XLSX, перезапуск, отмену и отзыв сессии.
Реальные исходники используются только на чтение. Браузерная проверка имеет
отдельный сценарий `qa/browser_smoke.mjs`; актуальные команды и результаты —
[отчёт по критериям](ОТЧЁТ_ПО_КРИТЕРИЯМ.md). В репозитории есть CI для тестов,
реальных данных, обоих HTTP smoke и синтаксиса Linux-конфигурации; наличие
workflow не означает, что удалённый CI уже запускался на текущем diff.

## 1. Требования и каталоги

Поддерживаемая среда — Linux с systemd, Python **3.12**, Node.js **22.19+ в ветке 22.x или 24.x**, npm, nginx и доверенный TLS-сертификат. Доступ сотрудников
ограничивается корпоративной сетью/VPN и сетевыми правилами организации.
Потребление памяти зависит от выгрузок; до эксплуатации проверьте полный
расчёт на целевом сервере. SQLite должна находиться на локальном диске,
а не на SMB/NFS/синхронизируемой папке.

| Каталог | Владелец / права | Содержимое |
|---|---|---|
| `/opt/umytpa` | root, чтение сервису | Репозиторий, среда Python, сборка UI |
| `/etc/umytpa/umytpa.env` | root:umytpa, 0640 | Настройки и ключ OpenAI, если нужен |
| `/etc/umytpa/tls` | root, закрытый ключ 0600 | `fullchain.pem`, `privkey.pem` |
| `/srv/umytpa/data` | root:umytpa, чтение сервису | `IEK/`, `Systeme electric/`, `ekt/catalog.json` |
| `/var/lib/umytpa` | umytpa:umytpa, 0700 | SQLite и её WAL-файлы |
| `/var/backups/umytpa` | umytpa:umytpa, 0700 | 14 последних проверенных копий |

## 2. Установка

Разместите репозиторий в `/opt/umytpa`. Создайте системного пользователя и
каталоги (разово, с правами администратора сервера):

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin umytpa
sudo install -d -o root -g umytpa -m 0750 /etc/umytpa /srv/umytpa/data
sudo install -d -o root -g root -m 0700 /etc/umytpa/tls
sudo install -d -o umytpa -g umytpa -m 0700 /var/lib/umytpa /var/backups/umytpa
cd /opt/umytpa/backend
sudo python3.12 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.lock
cd ../frontend
npm ci
npm run build
```

Сборку выполняет пользователь развёртывания; готовые файлы должны быть
доступны nginx на чтение. Выгрузки скопируйте в `/srv/umytpa/data` с сохранением
исходных имён; локальный справочник EKT разместите в `ekt/catalog.json`
относительно DATA_DIR либо задайте его отдельный путь. Дайте группе `umytpa` чтение файлов и проход по каталогам.
`ProtectSystem=strict` и `ReadOnlyPaths` запрещают сервису изменение источников.

```bash
cd /opt/umytpa
sudo install -o root -g umytpa -m 0640 deploy/umytpa.env.example /etc/umytpa/umytpa.env
sudo install -m 0644 deploy/umytpa-api.service deploy/umytpa-worker.service deploy/umytpa-backup.service deploy/umytpa-backup.timer /etc/systemd/system/
```

Отредактируйте `/etc/umytpa/umytpa.env`, задайте `APP_ORIGIN` равным настоящему
HTTPS-origin сайта (схема и домен, без завершающего `/`) и установите сертификат в
`/etc/umytpa/tls`. Ключ OpenAI необязателен; LLM включается отдельно в UI.
Секреты не добавляйте в репозиторий. Установите `deploy/nginx.conf` как сайт
nginx; замените `server_name _` в обоих блоках на корпоративный домен и
отключите конфликтующий default site штатным способом.

```bash
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/umytpa-api.service /etc/systemd/system/umytpa-worker.service /etc/systemd/system/umytpa-backup.service /etc/systemd/system/umytpa-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now umytpa-api umytpa-worker
sudo systemctl reload nginx
```

API выполняет атомарные миграции при старте. Старые снимки в существующей БД
импортируются в архив администратора после его создания. Для переноса старой
БД остановите старый сервис и перенесите согласованную копию через
`app.ops backup`, а не отдельно живой `.sqlite3` без WAL.

Создание первого администратора — интерактивная команда; пароль вводится
скрыто, стандартного пароля и открытой регистрации нет:

```bash
cd /opt/umytpa/backend
sudo -u umytpa env APP_DB_PATH=/var/lib/umytpa/umytpa.sqlite3 .venv/bin/python -m app.manage create-admin --username admin
sudo systemctl start umytpa-backup.service
sudo systemctl enable --now umytpa-backup.timer
```

Для автоматизированного bootstrap поддержан `--password-env ИМЯ_ПЕРЕМЕННОЙ`;
значение пароля не передавайте аргументом команды и не печатайте в журнал.

## 3. Проверки после установки

- `curl --fail http://127.0.0.1:8017/api/health` — API жив.
- `curl --fail http://127.0.0.1:8017/api/ready` — БД доступна, heartbeat worker
  не старше 30 секунд, обязательные исходные файлы доступны и не пусты.
  При остановке worker или отсутствии входов endpoint возвращает 503.
  Это не проверка свежести и содержательной полноты исходных строк.
- Откройте HTTPS-домен, войдите, создайте менеджера, выполните расчёт,
  исправьте количество, обновите страницу, подтвердите позиции и скачайте оба вида Excel. Это проверка личного
  подтверждения менеджером, а не независимого согласования.
- Проверьте второй аккаунт и конфликт изменения одного заказа в двух вкладках.
- Перезапустите API/worker: заказы и решения должны сохраниться.
- Проверьте восстановление копии в отдельную тестовую БД, команды ниже.
- Выполните остальные сценарии [ТЗ платформы](ТЗ_платформы.md) и [отчёта по критериям](ОТЧЁТ_ПО_КРИТЕРИЯМ.md), включая
  реальные выгрузки, мобильную ширину и клавиатуру.

## 4. Настройки и эксплуатация

| Настройка | Значение в поставке |
|---|---|
| `APP_ENV` | `production`; `development` допустим только для локального HTTP |
| `APP_ORIGIN` | Настоящий внешний HTTPS-origin; пример домена нужно заменить |
| `APP_DB_PATH` | `/var/lib/umytpa/umytpa.sqlite3`; старый `ORDER_DB_PATH` — fallback |
| `DATA_DIR`, `DATA_SOURCE` | `/srv/umytpa/data`, `excel` (`csv`/`synthetic` для своих источников/демо) |
| `EKT_CATALOG_ENABLED`, `EKT_CATALOG_PATH` | `true`, `/srv/umytpa/data/ekt/catalog.json`; описательный JSON необязателен |
| `DATA_AS_OF` | Пусто: дата по последней продаже; YYYY-MM-DD для фиксации |
| `IEK_LEAD_TIME_DAYS`, `SYSTEME_LEAD_TIME_DAYS` | 21, 35; допущения до подтверждения |
| `SERVICE_LEVEL`, `REVIEW_PERIOD_DAYS` | 0.95, 14 |
| `JOB_TIMEOUT_SECONDS`, `JOB_QUEUE_LIMIT` | 600 секунд, 20 заданий |
| `WORKER_STALE_SECONDS` | 30 секунд |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Ключ пустой, модель задаёт администратор |

Сессии живут восемь часов. Пароли хешируются Argon2id, cookie — HttpOnly,
Secure, SameSite. Изменяющие запросы защищены CSRF, попытки входа ограничены.
Изменение количества снимает подтверждение атомарно; версия защищает от
потери правок. Один новый расчёт не удаляет предыдущий заказ.

`journalctl -u umytpa-api -u umytpa-worker -u umytpa-backup` показывает
структурированные события и request ID. HTTP access log отключён; содержимое
продаж, тела запросов и секреты не должны выводиться в журнал. Ограничьте
хранение journald по политике сервера. Настройте внешний мониторинг `/api/ready`,
ошибок служб, свободного диска и давности копий; интеграции уведомлений с
конкретной системой организации в поставке нет.

Администратор сервера обновляет источники при остановленных **API и worker**,
заменяя весь согласованный набор файлов. API тоже читает файлы для `/meta`,
поэтому остановки одного worker недостаточно. Сохраните предыдущий комплект
для восстановления, проверьте импорт до возобновления работы. Веб-загрузка,
manifest и атомарная активация проверенной партии пока отсутствуют.
Новый расчёт использует новые данные; экспорт старого заказа всегда
использует исходный сохранённый расчёт.

## 5. Резервное копирование и восстановление

Timer запускает проверенную online-копию SQLite каждый день около 02:00 UTC,
пропущенный запуск выполняется после включения сервера. Копия включает
зафиксированные WAL-страницы. Проверяется `integrity_check`; только успешная
копия заменяет временный файл. Хранятся 14 последних копий (включая ручные).
Необычные файлы в каталоге не удаляются. Перед обновлением:

```bash
sudo systemctl start umytpa-backup.service
sudo systemctl status umytpa-backup.service
sudo systemctl list-timers umytpa-backup.timer
cd /opt/umytpa/backend
sudo -u umytpa .venv/bin/python -m app.ops backup-health --directory /var/backups/umytpa
```

`backup-health` возвращает код 1, если успешной копии нет или она старше
26 часов. Копии на том же сервере не защищают от утраты всего диска;
организация отдельно переносит их в защищённое внешнее хранилище.
Исходные Excel, локальный JSON EKT и конфигурацию нужно копировать отдельно.
Ежедневный timer не обеспечивает целевой RPO=1 час из полного ТЗ; расписание,
внешняя доставка копий и измеренное восстановление должны быть согласованы
до соответствующей production-приёмки.

Для восстановления остановите все клиенты БД, API, worker и timer. Выберите
конкретную копию вместо `ИМЯ-КОПИИ.sqlite3`:

```bash
sudo systemctl stop umytpa-backup.timer umytpa-backup.service umytpa-worker umytpa-api
cd /opt/umytpa/backend
sudo -u umytpa env APP_DB_PATH=/var/lib/umytpa/umytpa.sqlite3 .venv/bin/python -m app.ops restore --source /var/backups/umytpa/ИМЯ-КОПИИ.sqlite3 --services-stopped
sudo systemctl start umytpa-api umytpa-worker umytpa-backup.timer
```

Восстановление сначала проверяет источник, делает защитную копию нынешней БД
в `/var/lib/umytpa/before-restore`, затем атомарно заменяет файл. При наличии
`-wal`/`-shm` операция отклоняется: не удаляйте эти файлы вручную. Убедитесь,
что нет открытых клиентов; при остатках после аварии выполните SQLite
`PRAGMA wal_checkpoint(TRUNCATE)` доверенным инструментом при полностью
остановленных службах и закройте соединение перед повтором.

Для учебного восстановления используйте другой `APP_DB_PATH`, например
`/var/lib/umytpa/restore-check.sqlite3`, не изменяя рабочую базу.

## 6. Обновление и откат

Сделайте копию, остановите worker/API, установите согласованную версию кода,
выполните `pip install -r requirements.lock`, `npm ci`, тесты и сборку.
Запустите службы: миграция проверяет версию схемы и отказывается открывать
более новую схему старым приложением. Проверьте readiness и рабочий сценарий.
При несовместимой схеме откат требует согласованной предыдущей версии кода
и проверенной копии БД. Сначала оцените и сохраните новые решения пользователей
после этой копии: молчаливо терять их при откате нельзя. Не удаляйте `/var/lib/umytpa` при обновлении.

CI проверяет Python/JS, сборку и синтаксис Linux-конфигурации. Локальная
macOS-проверка не подтверждает запуск systemd/nginx на вашем сервере: ввод в
эксплуатацию завершается проверками выше на настоящем Linux-хосте.


## 7. Наблюдаемость и остающиеся условия выпуска

Не путайте живость, техническую готовность и пригодность данных для закупки.
Внешний мониторинг должен наблюдать `/ready`, ошибки/время API и worker,
очередь, свободное место, сертификат и возраст резервной копии. Отдельно
ответственный за данные подтверждает даты остатков/пути, lead time, MOQ,
единицы и полноту выгрузок. Названия городов на ekt.kz этого не заменяют.

Поставка не содержит SSO, пяти бизнес-ролей, независимого согласования,
доступов по складам, PostgreSQL, версионированных партий, отдельного кабинета
наблюдателя и измеренной нагрузки 20/10/2. Worker выполняет одну задачу за раз.
Ни CI, ни README не заменяют испытание с сотрудниками на согласованных данных.

Обновление EKT: [КАТАЛОГ_EKT.md](КАТАЛОГ_EKT.md). Формат учётного обмена и
границы 1С: [ИНТЕГРАЦИЯ_1С.md](ИНТЕГРАЦИЯ_1С.md). Очистка/ML/backtest:
[АУДИТ_ВЕТКИ_ALISH.md](АУДИТ_ВЕТКИ_ALISH.md), [BACKTEST.md](../scripts/BACKTEST.md).
Эти исследовательские команды не запускаются автоматически в рабочем API.
