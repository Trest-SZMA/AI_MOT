#!/usr/bin/env bash
# Обновление кода после git pull. Данные и env-файлы не трогает.
#
#   cd /opt/AI_MOT && git pull
#   sudo deploy/update.sh                # все сервисы
#   sudo deploy/update.sh bp-service     # один
#
# Для каждого сервиса: rsync кода в /opt/<сервис>, pip install (если
# изменился requirements.txt), обновление юнитов, перезапуск, проверка порта.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запускать от root (sudo)"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
source "$REPO/deploy/services.sh"
ONLY=${1:-}

install -m 644 "$REPO"/deploy/systemd/*.service "$REPO"/deploy/systemd/*.timer /etc/systemd/system/
chmod 600 /etc/systemd/system/fines-service.service
install -m 755 "$REPO/deploy/metoptorg-healthcheck.py" /usr/local/bin/metoptorg-healthcheck
install -m 755 "$REPO/deploy/bp-backup.sh" /usr/local/bin/bp-backup.sh
systemctl daemon-reload

for name in $(svc_names); do
  [ -n "$ONLY" ] && [ "$ONLY" != "$name" ] && continue
  user=$(svc_field "$name" 2); venv=$(svc_field "$name" 3)
  log "Обновляю $name"
  before=$(md5sum "/opt/$name/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
  rsync -a --delete --exclude-from="$REPO/deploy/rsync-exclude.txt" \
        "$REPO/services/$name/" "/opt/$name/"
  after=$(md5sum "/opt/$name/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
  if [ "$venv" = yes ] && { [ "$before" != "$after" ] || [ ! -x "/opt/$name/.venv/bin/python" ]; }; then
    [ -x "/opt/$name/.venv/bin/python" ] || python3 -m venv "/opt/$name/.venv"
    "/opt/$name/.venv/bin/pip" install -q -r "/opt/$name/requirements.txt"
    echo "  зависимости обновлены"
  fi
  [ "$user" != root ] && chown -R "$user:$user" "/opt/$name"
  for unit in $(svc_field "$name" 4); do
    case $unit in
      *.timer) systemctl enable --now "$unit" >/dev/null;;
      *)       systemctl restart "$unit"; echo "  перезапущен $unit";;
    esac
  done
  port=${PORTS[$name]:-}
  if [ -n "$port" ]; then
    sleep 3
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "http://127.0.0.1:$port/" || echo 000)
    [ "$code" != 000 ] && echo "  порт $port отвечает ($code)" || { warn "порт $port не отвечает"; journalctl -u "${name}"* -n 20 --no-pager; }
  fi
done
echo; bash "$REPO/deploy/check.sh" || true
