#!/usr/bin/env bash
# Запускается на СТАРОМ сервере от root. Собирает в один архив всё, чего нет
# в git: секреты (env-файлы), базы, загруженные файлы, выгрузки 1С, дамп
# PostgreSQL. Сервисы не останавливает: SQLite копируется «горячо» через
# .backup, PostgreSQL — pg_dump.
#
#   sudo bash export-data.sh            # → /root/ai_mot_data_<дата>.tar.gz
#
# Архив содержит пароли и данные компании: передавать ИТ-отделу только по
# защищённому каналу, в git не класть.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запускать от root"; exit 1; }
STAMP=$(date +%Y%m%d_%H%M)
STAGE=/root/ai_mot_export
OUT=/root/ai_mot_data_$STAMP.tar.gz
rm -rf "$STAGE"; mkdir -p "$STAGE"/{etc,opt,pg,var}

echo "==> секреты и системные файлы"
for f in metallompro metoptorg-kp metoptorg-ostatki metoptorg-portal metoptorg-realizaciya parser-bp bp-service metoptorg-healthcheck; do
  [ -f "/etc/$f.env" ] && cp -p "/etc/$f.env" "$STAGE/etc/"
done
cp -p /opt/logistmot/.env "$STAGE/etc/logistmot.env"
cp -p /root/.ssh/authorized_keys "$STAGE/etc/authorized_keys" 2>/dev/null || true
for svc in metoptorg-realizaciya metoptorg-ostatki; do
  [ -f "/opt/$svc/data/sql_sources.json" ] || echo "  ВНИМАНИЕ: нет /opt/$svc/data/sql_sources.json (привязка таблиц MSSQL)"
done

sqlite_backup() {  # горячая копия SQLite: sqlite_backup <src> <dst>
  mkdir -p "$(dirname "$2")"
  python3 - "$1" "$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  echo "  sqlite: $1 → $(du -h "$2" | cut -f1)"
}

echo "==> базы SQLite"
sqlite_backup /opt/bp-service/bp.db                       "$STAGE/opt/bp-service/bp.db"
sqlite_backup /opt/logistmot/bot.db                       "$STAGE/opt/logistmot/bot.db"
sqlite_backup /opt/fines-service/data/fines.db            "$STAGE/opt/fines-service/data/fines.db"
sqlite_backup /var/lib/private/metoptorg-kp/metopttorg.db "$STAGE/var/metoptorg-kp/metopttorg.db"

echo "==> PostgreSQL metallompro"
sudo -u postgres pg_dump -Fc metallompro > "$STAGE/pg/metallompro.dump"
du -h "$STAGE/pg/metallompro.dump" | cut -f1

echo "==> файлы данных"
R="rsync -a --exclude __pycache__ --exclude '*.part'"
$R /opt/bp-service/attachments /opt/bp-service/1c /opt/bp-service/output "$STAGE/opt/bp-service/"
$R --exclude fines.db /opt/fines-service/data/ "$STAGE/opt/fines-service/data/"
$R /opt/metoptorg-ostatki/data /opt/metoptorg-ostatki/out "$STAGE/opt/metoptorg-ostatki/"
$R /opt/metoptorg-realizaciya/data /opt/metoptorg-realizaciya/out /opt/metoptorg-realizaciya/logs "$STAGE/opt/metoptorg-realizaciya/"
# история сборок реализации — только 3 последние
mkdir -p "$STAGE/opt/metoptorg-realizaciya/builds"
for b in $(ls -1t /opt/metoptorg-realizaciya/builds | head -3); do
  $R "/opt/metoptorg-realizaciya/builds/$b" "$STAGE/opt/metoptorg-realizaciya/builds/"
done
$R /var/lib/private/metoptorg-kp/uploads /var/lib/private/metoptorg-kp/exports "$STAGE/var/metoptorg-kp/"
# metallompro: inbox + из архива только самый свежий файл каждого вида
mkdir -p "$STAGE/opt/metallompro/data/archive"
$R /opt/metallompro/data/inbox_1c "$STAGE/opt/metallompro/data/"
ls -1 /opt/metallompro/data/archive | sed -E 's/^[0-9]{8}_[0-9]{6}_//' | sort -u | while read -r kind; do
  last=$(ls -1 /opt/metallompro/data/archive/*"$kind" 2>/dev/null | sort | tail -1)
  [ -n "$last" ] && cp -p "$last" "$STAGE/opt/metallompro/data/archive/"
done

cat > "$STAGE/MANIFEST.txt" <<EOF
ai_mot data export
created: $(date -Is)
source_host: $(hostname)
source_ip: $(hostname -I | awk '{print $1}')
EOF

echo "==> архив"
tar czf "$OUT" -C "$STAGE" .
rm -rf "$STAGE"
ls -lh "$OUT"
echo
echo "Готово: $OUT"
echo "Перенести на новый сервер, например: scp $OUT root@НОВЫЙ_IP:/root/"
