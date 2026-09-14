# Архитектура

## Общая схема

Один сервер Ubuntu 24.04. Каждый сервис — отдельный каталог `/opt/<сервис>` со
своим Python-venv и systemd-юнитом, слушает свой порт на всех интерфейсах.
Общего входа (reverse proxy) нет: пользователи ходят на `http://<ip>:<порт>`,
портал на 8079 — просто страница со ссылками. Исключение — bp-service, у которого
есть https-обёртка nginx на 8443 с самоподписанным сертификатом.

```
пользователи ──► :8079 портал ──ссылки──► :8077 :8080 :8090 :8091 :8092 :8093/:8443 :8094 :8095
                                                                              │
1С ──ночью──► MSSQL «Extractor» 10.100.1.110 ◄──читают── realizaciya, ostatki  │ nginx :8443 ► :8093
                                                   │
                                     realizaciya/data/*.csv ──копирует──► metallompro/inbox_1c ──► PostgreSQL
Битрикс ◄──parser-bp (таймер вс 03:10)──► realizaciya/data/Проверка_БП_*.xlsx
MAX messenger ◄──► logistmot-bot ──► bot.db ◄──► logistmot-panel :8080
```

## Поток данных из 1С

1. 1С около 01:00 выгружает регистры в базу MSSQL `Extractor` (10.100.1.110:1433).
2. **metoptorg-realizaciya** в 04:00 (таймер `metoptorg-realizaciya-nightly`) вызывает
   собственный `POST /api/rebuild`; `src/sqlsrc.py` читает 10 таблиц и сохраняет их в
   `data/*.csv` с теми же именами, что раньше клали руками. Расчёт работает по файлам.
   Какая таблица какому входу соответствует — `data/sql_sources.json` (настраивается
   в браузере, страница «Обновление данных»). Пароль MSSQL — только в
   `/etc/metoptorg-realizaciya.env`.
3. **metoptorg-ostatki** — та же схема, свой `data/sql_sources.json` и `/etc/metoptorg-ostatki.env`.
4. **metallompro** в MSSQL не ходит: раз в 10 минут копирует новые
   `СебестоимостьТоваровОбороты_*.csv` из `/opt/metoptorg-realizaciya/data/`
   (`NEIGHBOR_1C_DIR`) к себе в `data/inbox_1c`, импортирует в PostgreSQL и
   перекладывает в `data/archive`. Также берёт остатки у ostatki по `http://127.0.0.1:8090/data.json`.
   ⇒ realizaciya, ostatki и metallompro должны стоять на одной машине.
5. **parser-bp** по воскресеньям (таймер `metoptorg-bp-weekly`) выгружает из Битрикса
   `Проверка_БП_*.xlsx` в `data/` реализации.
6. **bp-service** получает выгрузки 1С файлами в `1c/` (импорт `import_1c_csv.py`) — ручной вход.

Важно: ночная пересборка не раньше 04:00 — в 01:00 «Extractor» обновляется из 1С, и
чтение в это время даст смесь старых и новых данных без единой ошибки.

## Сервисы

| Сервис | Запуск | Пользователь | Особенности |
|---|---|---|---|
| metoptorg-portal | `python3 portal.py` (системный python, без venv) | DynamicUser | Только чтение своего каталога, `web/services.json` — реестр плиток |
| fines-service | uvicorn `app:app` | fines | |
| logistmot-bot | `python bot.py` (библиотека maxapi) | logistmot | **Работать может только один экземпляр бота** |
| logistmot-panel | `python panel.py` | logistmot | Общий `bot.db` с ботом |
| metoptorg-ostatki | `python3 server.py` (http.server) | root | `ProtectSystem=strict`, пишет в `data/`, `out/` |
| metoptorg-kp | uvicorn `app.api.main:app` | DynamicUser + `StateDirectory` | Данные в `/var/lib/private/metoptorg-kp`, код read-only |
| metoptorg-realizaciya | uvicorn `server:app`, 1 воркер | metoptorg | `PYTHONHASHSEED=0` для воспроизводимости; MemoryMax 3G |
| bp-service | uvicorn `app.main:app` | bpservice | nginx https 8443; cron-бэкап 02:30 в `backups/` |
| parser-bp | streamlit `app.py` | DynamicUser | |
| metallompro | uvicorn `app.main:app` | root | PostgreSQL 16; APScheduler внутри (скрапер, брифинг, inbox) |

## Секреты

Только в env-файлах (`EnvironmentFile=` в юнитах), права 600. В коде и в git
паролей нет; значения по умолчанию в коде (например, старый IP `192.168.6.157` в
`PORTAL_URL`) перекрываются переменными из env, которые `import-data.sh`
переписывает на новый адрес.

## Мониторинг

`/usr/local/bin/metoptorg-healthcheck` (исходник `deploy/metoptorg-healthcheck.py`):
таймер каждые 5 минут проверяет юниты, порты, failed-юниты, вчерашние
ночные задачи, диск и память; пишет в MAX только при изменении состояния;
в 08:00 — сводка. Настройки в `/etc/metoptorg-healthcheck.env`.
Telegram с корпоративной сети недоступен — поэтому MAX.

## Известные долги (не блокируют переезд)

- metallompro и metoptorg-ostatki работают от root — стоит завести отдельных пользователей.
- Нет общего reverse-proxy с TLS; панели защищены только basic-auth. Порты должны быть закрыты файрволом от всего, кроме офисной сети.
- `metallompro/data/archive` растёт на 330 МБ при каждой пересборке реализации — нужна ротация.
- Тесты metoptorg-kp используют реальные файлы, которых нет в репозитории.
