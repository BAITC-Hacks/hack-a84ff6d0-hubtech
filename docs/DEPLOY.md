# Развёртывание Umytpa на Linux

Поставка: статическая сборка React, nginx с HTTPS, FastAPI, отдельный worker,
SQLite на локальном диске и ежедневный systemd timer резервного копирования.
API слушает только `127.0.0.1:8017`; браузер использует один origin и `/api`.
Для небольшого отдела запускается ровно один worker и один процесс API.

## 1. Требования и каталоги

Поддерживаемая среда — Linux с systemd, Python **3.12**, Node.js **22.19**
(минимум 22.12), npm, nginx и доверенный TLS-сертификат. Доступ сотрудников
ограничивается корпоративной сетью/VPN и сетевыми правилами организации.
Потребление памяти зависит от выгрузок; до эксплуатации проверьте полный
расчёт на целевом сервере. SQLite должна находиться на локальном диске,
а не на SMB/NFS/синхронизируемой папке.

| Каталог | Владелец / права | Содержимое |
|---|---|---|
| `/opt/umytpa` | root, чтение сервису | Репозиторий, среда Python, сборка UI |
| `/etc/umytpa/umytpa.env` | root:umytpa, 0640 | Настройки и ключ OpenAI, если нужен |
| `/etc/umytpa/tls` | root, закрытый ключ 0600 | `fullchain.pem`, `privkey.pem` |
| `/srv/umytpa/data` | root:umytpa, чтение сервису | `IEK/`, `Systeme electric/` |
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
исходных имён; дайте группе `umytpa` чтение файлов и проход по каталогам.
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
  не старше 30 секунд. При остановке worker endpoint возвращает 503.
- Откройте HTTPS-домен, войдите, создайте менеджера, выполните расчёт,
  исправьте количество, обновите страницу, утвердите и скачайте оба вида Excel.
- Проверьте второй аккаунт и конфликт изменения одного заказа в двух вкладках.
- Перезапустите API/worker: заказы и решения должны сохраниться.
- Проверьте восстановление копии в отдельную тестовую БД, команды ниже.
- Выполните остальные сценарии [матрицы приёмки](ПРИЕМКА.md), включая
  реальные выгрузки, мобильную ширину и клавиатуру.

## 4. Настройки и эксплуатация

| Настройка | Значение в поставке |
|---|---|
| `APP_ENV` | `production`; `development` допустим только для локального HTTP |
| `APP_ORIGIN` | Настоящий внешний HTTPS-origin; пример домена нужно заменить |
| `APP_DB_PATH` | `/var/lib/umytpa/umytpa.sqlite3`; старый `ORDER_DB_PATH` — fallback |
| `DATA_DIR`, `DATA_SOURCE` | `/srv/umytpa/data`, `excel` (`csv`/`synthetic` для своих источников/демо) |
| `DATA_AS_OF` | Пусто: дата по последней продаже; YYYY-MM-DD для фиксации |
| `IEK_LEAD_TIME_DAYS`, `SYSTEME_LEAD_TIME_DAYS` | 21, 35; допущения до подтверждения |
| `SERVICE_LEVEL`, `REVIEW_PERIOD_DAYS` | 0.95, 14 |
| `JOB_TIMEOUT_SECONDS`, `JOB_QUEUE_LIMIT` | 600 секунд, 20 заданий |
| `WORKER_STALE_SECONDS` | 30 секунд |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Ключ пустой, модель задаёт администратор |

Сессии живут восемь часов. Пароли хешируются Argon2id, cookie — HttpOnly,
Secure, SameSite. Изменяющие запросы защищены CSRF, попытки входа ограничены.
Изменение количества снимает утверждение атомарно; версия защищает от
потери правок. Один новый расчёт не удаляет предыдущий заказ.

`journalctl -u umytpa-api -u umytpa-worker -u umytpa-backup` показывает
структурированные события и request ID. HTTP access log отключён; содержимое
продаж, тела запросов и секреты не должны выводиться в журнал. Ограничьте
хранение journald по политике сервера. Настройте внешний мониторинг `/api/ready`,
ошибок служб, свободного диска и давности копий; интеграции уведомлений с
конкретной системой организации в поставке нет.

Администратор сервера обновляет источники во время остановленного worker,
заменяя весь согласованный набор файлов. Веб-загрузка выгрузок отсутствует.
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
При откате восстанавливайте одновременно предыдущую версию кода и её
проверенную копию БД. Не удаляйте `/var/lib/umytpa` при обновлении.

CI проверяет Python/JS, сборку и синтаксис Linux-конфигурации. Локальная
Windows-среда не подтверждает запуск systemd/nginx на вашем сервере: ввод в
эксплуатацию завершается проверками выше на настоящем Linux-хосте.
