# AI_MOT — рабочие сервисы МетОптТорг

Монорепозиторий из девяти внутренних веб-сервисов компании и всего, что нужно,
чтобы развернуть их на новом сервере одной командой и потом обновлять через git.

**Два варианта развёртывания — на выбор ИТ-отдела, код и архив данных общие:**

| | Как | Документация |
|---|---|---|
| **Docker Compose** (рекомендуется) | `docker-compose.yml` в корне, `deploy/docker/setup.sh`, `deploy/docker/import-data.sh` | [docs/DOCKER.md](docs/DOCKER.md) |
| systemd + venv на Ubuntu 24.04 | `deploy/install.sh`, `deploy/import-data.sh`, `deploy/update.sh` | этот README ниже |

Как сервисы связаны между собой и с 1С — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
порядок переезда с чеклистом — [docs/MIGRATION.md](docs/MIGRATION.md).

## Состав

| Сервис | Порт | Что делает | Данные |
|---|---|---|---|
| metoptorg-portal | 8079 | Главная страница — витрина всех сервисов (basic-auth) | — |
| fines-service | 8077 | Отчёты по штрафам | SQLite `data/fines.db` |
| logistmot | 8080 + бот | Бот в мессенджере MAX для перевозчиков + веб-панель логиста | SQLite `bot.db` |
| metoptorg-ostatki | 8090 | Фактические остатки металлолома по складам (из 1С через MSSQL) | `data/`, `out/` |
| metoptorg-kp | 8091 | Оценка коммерческих предложений и выходов металлов | SQLite в `/var/lib/private/metoptorg-kp` |
| metoptorg-realizaciya | 8092 | Реализация по сериям и бизнес-планам (из 1С через MSSQL, ночная пересборка) | `data/`, `out/`, `builds/` |
| bp-service | 8093 / **8443 https** | Сервис бизнес-планов | SQLite `bp.db`, `attachments/`, `1c/` |
| parser-bp | 8094 | Парсер бизнес-планов из Битрикса (Streamlit) | — |
| metallompro | 8095 | MetalLomPro 2.0 — аналитика и прогноз рынка лома | **PostgreSQL** `metallompro` + `data/` |

Плюс: healthcheck (проверка всех служб каждые 5 минут, уведомления в MAX),
ежедневные бэкапы bp-service (cron 02:30) и metoptorg-kp (таймер 03:30),
ночная пересборка реализации (04:00), еженедельная выгрузка из Битрикса (вс 03:10).

## Требования к серверу

- Ubuntu 24.04 (проверено в LXC Proxmox), 4 ГБ RAM минимум (пересборка реализации ест до 2 ГБ), 40 ГБ диска.
- Сетевой доступ: **MSSQL «Extractor» 10.100.1.110:1433** (данные 1С — обязательно),
  интернет для `botapi.max.ru`, `api.perplexity.ai`, `cbr.ru`, сайтов ломозаготовителей (metallompro), Битрикс.
- Пользователи открывают сервисы по `http://<ip>:<порт>` из офисной сети; наружу ничего не публикуется.

## Установка на новый сервер (30 минут)

```bash
# 1. код
sudo apt-get install -y git
sudo git clone https://github.com/Trest-SZMA/AI_MOT.git /opt/AI_MOT
cd /opt/AI_MOT

# 2. пакеты, пользователи, venv, юниты, nginx, PostgreSQL, healthcheck
sudo deploy/install.sh

# 3. данные и секреты со старого сервера (архив делает deploy/export-data.sh, см. ниже)
sudo deploy/import-data.sh /root/ai_mot_data_XXXXXXXX_XXXX.tar.gz

# 4. проверка
sudo deploy/check.sh
```

`install.sh` можно запускать повторно — он не трогает данные и существующие env-файлы.

### Перенос данных со старого сервера

На **старом** сервере (192.168.6.157) от root:

```bash
sudo bash /opt/AI_MOT/deploy/export-data.sh      # → /root/ai_mot_data_<дата>.tar.gz (~4 ГБ)
scp /root/ai_mot_data_*.tar.gz root@НОВЫЙ_IP:/root/
```

Архив содержит все пароли и данные компании — только по защищённому каналу, в git не класть.
Подробный порядок переключения с чеклистом: [docs/MIGRATION.md](docs/MIGRATION.md).

## Обновление (когда в GitHub появилась новая версия)

```bash
cd /opt/AI_MOT
sudo git pull
sudo deploy/update.sh                    # все сервисы
sudo deploy/update.sh metoptorg-kp       # или один
```

`update.sh` копирует код в `/opt/<сервис>`, доставляет зависимости, если изменился
`requirements.txt`, обновляет юниты, перезапускает службу и проверяет порт.
Данные, базы и env-файлы не затрагиваются. Откат — `git checkout <прошлый тег>` и снова `update.sh`.

## Структура репозитория

```
services/<сервис>/       код каждого сервиса (то, что лежит в /opt/<сервис>)
deploy/install.sh        установка с нуля
deploy/import-data.sh    развёртывание данных из архива
deploy/export-data.sh    сборка архива данных на старом сервере
deploy/update.sh         обновление после git pull
deploy/check.sh          проверка служб и портов
deploy/services.sh       реестр сервисов: пользователи, порты, юниты
deploy/systemd/          unit-файлы и таймеры (копируются в /etc/systemd/system)
deploy/nginx/            https-прокси для bp-service
deploy/env/*.example     шаблоны env-файлов — имена переменных без значений
deploy/metoptorg-healthcheck.py  мониторинг с уведомлениями в MAX
docs/                    архитектура, порядок переезда
```

## Секреты и данные

В репозитории **нет** паролей, токенов, баз и выгрузок. Всё это живёт на сервере:

| Файл | Кому принадлежит | Что внутри |
|---|---|---|
| `/etc/<сервис>.env` | root, 600 | пароли MSSQL, API-ключи, учётки панелей |
| `/opt/logistmot/.env` | logistmot, 600 | токен MAX-бота, учётки панели |
| `/etc/metoptorg-healthcheck.env` | root, 600 | токен бота уведомлений |
| `/opt/<сервис>/data`, `out`, `builds`, `*.db`, `attachments` | сервис | данные |
| `/var/lib/private/metoptorg-kp` | динамический пользователь | база и загрузки КП |
| `/etc/ssl/bp-service/` | root | самоподписанный сертификат https |

Список переменных каждого сервиса — в `deploy/env/*.env.example`.

## Диагностика

```bash
sudo deploy/check.sh                          # сводная таблица
systemctl status <юнит>                       # состояние
journalctl -u <юнит> -n 100 --no-pager        # журнал
metoptorg-healthcheck --print                 # то, что видит мониторинг
systemctl list-timers | grep metoptorg        # таймеры
```

Юниты: `bp-service fines-service logistmot-bot logistmot-panel metallompro metoptorg-kp
metoptorg-ostatki metoptorg-portal metoptorg-realizaciya parser-bp` (+ таймеры
`metoptorg-kp-backup`, `metoptorg-realizaciya-nightly`, `metoptorg-bp-weekly`, `metoptorg-healthcheck*`).
