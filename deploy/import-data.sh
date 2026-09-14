#!/usr/bin/env bash
# Запускается на НОВОМ сервере от root ПОСЛЕ deploy/install.sh.
# Разворачивает архив, собранный deploy/export-data.sh на старом сервере:
# секреты, базы, файлы, дамп PostgreSQL; подменяет старый IP на новый в
# настройках; выставляет владельцев; перезапускает службы; проверяет.
#
#   sudo deploy/import-data.sh /root/ai_mot_data_20260915_0900.tar.gz [НОВЫЙ_IP]
#
# ⚠️ Бот ЛогистМОТ должен работать только в одном месте. Перед запуском
# остановите его на старом сервере: systemctl stop logistmot-bot
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запускать от root (sudo)"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
source "$REPO/deploy/services.sh"
ARCHIVE=${1:?укажите архив ai_mot_data_*.tar.gz}
NEW_IP=${2:-$(hostname -I | awk '{print $1}')}
STAGE=/root/ai_mot_import

log "Распаковка $ARCHIVE"
rm -rf "$STAGE"; mkdir -p "$STAGE"; tar xzf "$ARCHIVE" -C "$STAGE"
OLD_IP=$(sed -n 's/^source_ip: //p' "$STAGE/MANIFEST.txt")
cat "$STAGE/MANIFEST.txt"; echo "новый IP: $NEW_IP"

log "Останавливаю службы"
for name in $(svc_names); do for u in $(svc_field "$name" 4); do systemctl stop "$u" 2>/dev/null || true; done; done

log "Секреты (env-файлы)"
for f in "$STAGE"/etc/*.env; do
  b=$(basename "$f")
  if [ "$b" = logistmot.env ]; then
    install -m 600 -o logistmot -g logistmot "$f" /opt/logistmot/.env; echo "  /opt/logistmot/.env"
  else
    install -m 600 "$f" "/etc/$b"; echo "  /etc/$b"
  fi
done
if [ -n "$OLD_IP" ] && [ "$OLD_IP" != "$NEW_IP" ]; then
  sed -i "s/$OLD_IP/$NEW_IP/g" /etc/*.env /opt/logistmot/.env
  echo "  адреса $OLD_IP → $NEW_IP заменены в env-файлах"
fi
if [ -f "$STAGE/etc/authorized_keys" ]; then
  mkdir -p /root/.ssh; touch /root/.ssh/authorized_keys
  sort -u "$STAGE/etc/authorized_keys" /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.new
  mv /root/.ssh/authorized_keys.new /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
  echo "  ssh-ключи деплоя добавлены в /root/.ssh/authorized_keys"
fi

log "Файлы и базы сервисов"
for d in "$STAGE"/opt/*/; do
  name=$(basename "$d")
  rsync -a "$d" "/opt/$name/"
  user=$(svc_field "$name" 2 || echo root)
  [ "$user" != root ] && chown -R "$user:$user" "/opt/$name"
  echo "  /opt/$name ← $(du -sh "$d" | cut -f1)"
done

log "Состояние metoptorg-kp (StateDirectory с DynamicUser)"
# systemd создаёт /var/lib/private/metoptorg-kp и динамического пользователя при
# первом старте; узнаём его uid, останавливаем, копируем, отдаём права.
systemctl start metoptorg-kp.service; sleep 3; systemctl stop metoptorg-kp.service
KPDIR=/var/lib/private/metoptorg-kp
mkdir -p "$KPDIR"
KPOWN=$(stat -c %u:%g "$KPDIR")
rsync -a "$STAGE/var/metoptorg-kp/" "$KPDIR/"
chown -R "$KPOWN" "$KPDIR"; chmod 660 "$KPDIR/metopttorg.db"
echo "  $KPDIR ← $(du -sh "$STAGE/var/metoptorg-kp" | cut -f1), владелец $KPOWN"

log "PostgreSQL metallompro"
read -r DBUSER DBNAME < <(bash "$REPO/deploy/pg-ensure-role.sh" /etc/metallompro.env)
sudo -u postgres pg_restore --clean --if-exists --no-owner --role="$DBUSER" -d "$DBNAME" "$STAGE/pg/metallompro.dump" \
  || warn "pg_restore сообщил об ошибках (часто безобидные предупреждения при --clean на пустой базе)"
echo "  таблиц: $(sudo -u postgres psql -d "$DBNAME" -tAc "select count(*) from pg_tables where schemaname='public'")"

log "Запуск служб"
for name in $(svc_names); do
  for u in $(svc_field "$name" 4); do
    if [ "$u" = logistmot-bot.service ]; then
      echo
      warn "Бот ЛогистМОТ: убедитесь, что на СТАРОМ сервере он остановлен (systemctl stop logistmot-bot)."
      a=n; read -r -p "Запустить бота здесь? [y/N] " a </dev/tty 2>/dev/null || true
      [ "${a,,}" = y ] && systemctl enable --now "$u" || { echo "  бот не запущен: systemctl start logistmot-bot когда будете готовы"; continue; }
    else
      systemctl enable --now "$u" >/dev/null
    fi
    echo "  $u"
  done
done
systemctl reload nginx 2>/dev/null || true
rm -rf "$STAGE"

echo; sleep 5
bash "$REPO/deploy/check.sh" || true
echo
echo "Дальше: откройте http://$NEW_IP:8079 (портал) и проверьте каждый сервис."
echo "Реализация/Остатки: на странице «Обновление данных» нажать «проверить» связь с MSSQL."
