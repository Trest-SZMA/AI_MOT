# Развёртывание в Docker Compose

Альтернатива systemd-варианту из README: те же 9 сервисов, те же порты наружу,
но каждый сервис — контейнер, база — контейнер PostgreSQL, вместо systemd-таймеров —
планировщик ofelia, вместо healthcheck-скрипта — контейнер monitor.

```
docker-compose.yml           стек (в корне репозитория)
deploy/docker/Dockerfile     общий образ python:3.12-slim для всех сервисов
deploy/docker/setup.sh       первичная подготовка: .env, env/, data/, сертификат
deploy/docker/import-data.sh данные и секреты из архива старого сервера
deploy/docker/ofelia.ini     расписание: пересборка 04:00, бэкапы, выгрузка Битрикс, сводка
deploy/docker/monitor.py     мониторинг контейнеров → MAX
deploy/docker/nginx-bp-service.conf  https :8443 → bp-service
deploy/docker/tools/         entrypoint, ночная пересборка, бэкап bp-service
```

На хосте после установки появляются каталоги, которых нет в git:
`.env` (пароль PostgreSQL, IP), `env/*.env` (секреты сервисов), `data/<сервис>/` (все данные), `certs/`.

## Требования

- Ubuntu 22.04/24.04 (или любой Linux) с Docker Engine ≥ 24 и плагином Compose v2:
  ```bash
  curl -fsSL https://get.docker.com | sh
  docker compose version
  ```
- 4 ГБ RAM минимум (realizaciya и ostatki ограничены 3 ГБ каждый), 40 ГБ диска.
- Сеть с хоста: MSSQL `10.100.1.110:1433`, `botapi.max.ru`, `api.perplexity.ai`, `cbr.ru`, Битрикс, Docker Hub (для сборки образов).

## Сеть: проверка MTU (важно в Proxmox/LXC)

Если у хоста путь в интернет идёт через VLAN или туннель с MTU меньше 1500, часть HTTPS-сайтов
(botapi.max.ru, cbr.ru, api.perplexity.ai) будет **молча зависать** — бот ЛогистМОТ не получает
события, скрапер metallompro не работает. Проверка на хосте:

```bash
ping -c1 -M do -s 1472 8.8.8.8     # FAIL → MTU меньше 1500
ping -c1 -M do -s 1400 8.8.8.8     # OK  → ставим 1400
```

Тогда: в Proxmox у контейнера Network → net0 → MTU = 1450 (постоянно), и в `.env`
`DOCKER_MTU=1400`, затем `docker compose down && docker compose up -d` (сеть пересоздаётся).
На сервере AI-MOT это уже сделано (путь = 1488).

## Установка

```bash
git clone https://github.com/Trest-SZMA/AI_MOT.git /opt/AI_MOT
cd /opt/AI_MOT
deploy/docker/setup.sh                                  # .env, env/, data/, сертификат
sudo deploy/docker/import-data.sh /root/ai_mot_data_XXXXXXXX_XXXX.tar.gz
docker compose ps                                       # все должны быть Up (healthy)
```

`import-data.sh` сам поднимает postgres, восстанавливает дамп, раскладывает данные по
`data/`, кладёт секреты в `env/`, заменяет старый IP на новый и делает `docker compose up -d --build`.
Перед запуском бота спросит подтверждение — на старом сервере бот должен быть остановлен.

Без архива (чистая установка): в `env/*.env` раскомментировать и заполнить нужные переменные
(пустое `KEY=` перебивает значение по умолчанию в коде — лишние строки оставить закомментированными),
затем `docker compose up -d --build`.

Стек проверен 2026-09-14: сборка всех образов, запуск 14 контейнеров, бэкапы и ночная
пересборка через планировщик, импорт архива с восстановлением PostgreSQL. Проверка шла на
arm64 (macOS/colima); на x86-64 те же образы, но первый `docker compose up --build` на новом
сервере стоит посмотреть глазами.

## Обновление

```bash
cd /opt/AI_MOT
git pull
docker compose up -d --build          # пересобирает только изменившиеся образы и перезапускает их
docker compose ps
```

Один сервис: `docker compose up -d --build kp`. Откат: `git checkout <тег>` и та же команда.
Данные в `data/` и секреты в `env/` при обновлении не затрагиваются.

## Эксплуатация

```bash
docker compose ps                              # состояние и healthcheck
docker compose logs -f --tail=100 realizaciya  # журнал сервиса
docker compose logs scheduler                  # что и когда запускал планировщик
docker compose exec monitor python /app/monitor.py --print   # что видит мониторинг
docker compose restart bp-service
```

Имена сервисов в compose: `portal fines logistmot-bot logistmot-panel ostatki kp realizaciya
bp-service parser-bp metallompro nginx postgres scheduler monitor`.

Бэкап всего стека = каталог `data/` (там же `data/postgres` — файлы PostgreSQL; для
переносимого дампа: `docker compose exec -T postgres pg_dump -U metallompro -Fc metallompro > metallompro.dump`).
Ежедневные бэкапы bp-service и kp планировщик кладёт в `data/bp-service/backups` и `data/metoptorg-kp/backups`.

Уведомления мониторинга: в `env/monitor.env` вписать `MAX_BOT_TOKEN`, написать боту в MAX,
`docker compose exec monitor python /app/monitor.py --setup` покажет `MAX_CHAT_ID`, затем `--test`.

## Как устроено внутри

- Код каждого сервиса лежит в образе в `/opt/<сервис>` — по тем же путям, что на старом
  сервере, поэтому код не менялся. Каталоги с данными внутри образа заменены симлинками
  на том `/data` (аргумент `LINKS` в Dockerfile), а том смонтирован из `data/<сервис>`.
- metallompro читает CSV у реализации: том `data/metoptorg-realizaciya/data` смонтирован
  ему в `/neighbor` (только чтение), а parser-bp — в `/opt/metoptorg-realizaciya/data` (запись, еженедельная выгрузка).
- Планировщик ofelia выполняет команды внутри контейнеров (`job-exec`) через docker.sock;
  ночная пересборка идёт через API реализации, как и раньше (замок, снимок, лог).
- Контейнеры работают от root внутри своего namespace; наружу опубликованы только порты сервисов.
- nginx в контейнере — источник запроса для bp-service будет адрес контейнера nginx
  (172.x), а не 127.0.0.1; в журнале bp-service клиентский IP берётся из X-Forwarded-For.

## Отличия от systemd-варианта

| | systemd | Docker |
|---|---|---|
| Установка | `deploy/install.sh` | `deploy/docker/setup.sh` + `docker compose up` |
| Данные | `/opt/<сервис>/…`, `/var/lib/private/metoptorg-kp` | `data/<сервис>/` в одном месте |
| Секреты | `/etc/<сервис>.env` | `env/<сервис>.env` |
| Расписание | systemd-таймеры + cron | контейнер `scheduler` (ofelia.ini) |
| Мониторинг | `metoptorg-healthcheck` (таймер) | контейнер `monitor` |
| PostgreSQL | пакет на хосте | контейнер `postgres:16` |
| Обновление | `git pull && deploy/update.sh` | `git pull && docker compose up -d --build` |

Оба варианта используют один и тот же архив данных `deploy/export-data.sh` и один код.

### bp-service: ночная выгрузка из 1С (Extractor)

С 15.09.2026 `bp-service` сам забирает таблицы 1С из MSSQL «Extractor»
(`pull_1c.py`, планировщик `bp-import-1c` в 03:00) — ручные CSV в `data/bp-service/1c`
больше не нужны. В `env/bp-service.env` должны быть:

```
BP_SQL_USER=<пользователь Extractor, тот же, что у реализации>
BP_SQL_PASSWORD=<пароль, тот же, что METOPTORG_SQL_PASSWORD у реализации>
```

Хост/база по умолчанию 10.100.1.110 / Extractor (`BP_SQL_HOST`, `BP_SQL_DATABASE`).
Проверить руками: `docker exec ai_mot-bp-service python pull_1c.py --only ТС --no-import`.
Итог последней выгрузки — на странице «Справочники» сервиса и в `data/bp-service/1c/_last_pull.json`.

### Сборка образов и MTU

`DOCKER_MTU` в `.env` действует только на внутреннюю сеть `ai_mot`. Сборка
образов (`pip install`) идёт через сборочную сеть `docker0` с MTU 1500, и при
MTU хоста 1450 запросы к pypi.org молча обрываются по таймауту (15.09.2026,
`ReadTimeoutError ... pypi.org`). Поэтому у всех `build:` в compose стоит
`network: host` — сборка ходит в интернет как сам хост. Если собираете вручную:
`docker build --network host -f deploy/docker/Dockerfile --build-arg SERVICE=bp-service --build-arg LINKS="bp.db attachments 1c output backups" -t ai_mot/bp-service .`
