#!/usr/bin/env bash
# Разворачивает архив deploy/export-data.sh (со старого сервера) в Docker-раскладку:
# секреты → env/*.env, данные → data/<сервис>/, дамп → PostgreSQL в контейнере.
# Запускать из корня репозитория после deploy/docker/setup.sh:
#   sudo deploy/docker/import-data.sh /root/ai_mot_data_20260915_0900.tar.gz
#
# ⚠️ Бот ЛогистМОТ должен работать только в одном месте — остановите его на старом
# сервере (systemctl stop logistmot-bot) до запуска здесь.
set -euo pipefail
cd "$(dirname "$0")/../.."
ARCHIVE=${1:?укажите архив ai_mot_data_*.tar.gz}
[ -f .env ] || { echo "Сначала deploy/docker/setup.sh"; exit 1; }
# shellcheck disable=SC1091
source .env
DATA_ROOT=${DATA_ROOT:-./data}
STAGE=$(mktemp -d /tmp/ai_mot_import.XXXX)

echo "==> распаковка"
tar xzf "$ARCHIVE" -C "$STAGE"
OLD_IP=$(sed -n 's/^source_ip: //p' "$STAGE/MANIFEST.txt" 2>/dev/null || true)
cat "$STAGE/MANIFEST.txt" 2>/dev/null || true
echo "новый IP: $HOST_IP"

echo "==> останавливаю сервисы (postgres остаётся)"
docker compose stop portal fines logistmot-bot logistmot-panel ostatki kp realizaciya bp-service parser-bp metallompro nginx scheduler monitor 2>/dev/null || true

echo "==> секреты → env/"
mkdir -p env
for f in "$STAGE"/etc/*.env; do
  b=$(basename "$f")
  [ "$b" = metoptorg-healthcheck.env ] && b=monitor.env
  install -m 600 "$f" "env/$b"; echo "  env/$b"
done
[ -n "$OLD_IP" ] && [ "$OLD_IP" != "$HOST_IP" ] && sed -i "s/$OLD_IP/$HOST_IP/g" env/*.env && echo "  $OLD_IP → $HOST_IP в env/*.env"
# DATABASE_URL и пути внутри контейнеров задаёт docker-compose.yml — старые значения из env-файлов он перекрывает.

echo "==> данные → $DATA_ROOT/"
cp_() { [ -e "$1" ] && { mkdir -p "$2"; cp -a "$1" "$2/"; echo "  $2/$(basename "$1")"; } || true; }
for x in bp.db attachments 1c output;   do cp_ "$STAGE/opt/bp-service/$x"            "$DATA_ROOT/bp-service"; done
cp_ "$STAGE/opt/logistmot/bot.db"                                                     "$DATA_ROOT/logistmot"
[ -d "$STAGE/opt/fines-service/data" ] && cp -a "$STAGE/opt/fines-service/data/." "$DATA_ROOT/fines-service/" && echo "  $DATA_ROOT/fines-service"
for x in data out;                      do cp_ "$STAGE/opt/metoptorg-ostatki/$x"     "$DATA_ROOT/metoptorg-ostatki"; done
for x in data out builds logs;          do cp_ "$STAGE/opt/metoptorg-realizaciya/$x" "$DATA_ROOT/metoptorg-realizaciya"; done
for x in metopttorg.db uploads exports; do cp_ "$STAGE/var/metoptorg-kp/$x"          "$DATA_ROOT/metoptorg-kp"; done
for x in inbox_1c archive;              do cp_ "$STAGE/opt/metallompro/data/$x"      "$DATA_ROOT/metallompro"; done
mkdir -p "$DATA_ROOT"/{parser-bp,monitor,postgres,metoptorg-realizaciya/data}
[ -f "$DATA_ROOT/metoptorg-realizaciya/data/sql_sources.json" ] || echo "  ВНИМАНИЕ: нет sql_sources.json у реализации — таблицы MSSQL придётся привязать заново"

echo "==> PostgreSQL metallompro"
docker compose up -d postgres
for i in $(seq 1 30); do docker compose exec -T postgres pg_isready -U metallompro -d metallompro >/dev/null 2>&1 && break; sleep 2; done
docker compose exec -T postgres pg_restore -U metallompro -d metallompro --clean --if-exists --no-owner < "$STAGE/pg/metallompro.dump" \
  || echo "  pg_restore сообщил об ошибках (на пустой базе с --clean это обычно предупреждения)"
echo "  таблиц: $(docker compose exec -T postgres psql -U metallompro -d metallompro -tAc "select count(*) from pg_tables where schemaname='public'")"

rm -rf "$STAGE"

echo "==> запуск"
docker compose up -d --build
echo
read -r -p "Бот ЛогистМОТ остановлен на старом сервере? Запустить его здесь? [y/N] " a
[ "${a,,}" = y ] || { docker compose stop logistmot-bot; echo "  бот остановлен: docker compose start logistmot-bot когда будете готовы"; }
sleep 10
docker compose ps
echo
echo "Портал: http://$HOST_IP:8079   Реализация/Остатки → «Обновление данных» → проверить MSSQL."
