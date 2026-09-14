#!/usr/bin/env bash
# Установка сервисов МетОптТорг на чистый Ubuntu 24.04.
#
#   sudo deploy/install.sh              # все сервисы
#   sudo deploy/install.sh bp-service   # только один
#
# Что делает: ставит пакеты, создаёт служебных пользователей, копирует код
# в /opt/<сервис>, собирает venv, кладёт env-файлы (если их ещё нет — из
# шаблонов deploy/env/*.example, их потом заполняет deploy/import-data.sh),
# настраивает PostgreSQL для metallompro, nginx+сертификат для bp-service,
# юниты systemd, таймеры, cron-бэкап и healthcheck.
#
# Скрипт идемпотентен: повторный запуск ничего не ломает и не трогает данные.
# Данные (базы, выгрузки, секреты) переносятся ОТДЕЛЬНО: deploy/import-data.sh.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запускать от root (sudo)"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=services.sh
source "$REPO/deploy/services.sh"

ONLY=${1:-}
SERVER_IP=$(hostname -I | awk '{print $1}')
MISSING_ENV=()

# ---------- 1. пакеты ----------
log "Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx postgresql \
    curl rsync git poppler-utils ca-certificates >/dev/null
timedatectl set-timezone Asia/Yekaterinburg || true
python3 --version

# ---------- 2. пользователи ----------
log "Служебные пользователи"
for u in bpservice fines logistmot metoptorg; do
  if ! id -u "$u" >/dev/null 2>&1; then
    useradd -r -s /usr/sbin/nologin -M "$u"; echo "  создан $u"
  fi
done

# ---------- 3. код, venv, env ----------
install_service() {
  local name=$1 user venv
  user=$(svc_field "$name" 2); venv=$(svc_field "$name" 3)
  log "Сервис $name (пользователь $user)"
  mkdir -p "/opt/$name"
  rsync -a --delete --exclude-from="$REPO/deploy/rsync-exclude.txt" \
        "$REPO/services/$name/" "/opt/$name/"
  if [ "$venv" = yes ]; then
    [ -x "/opt/$name/.venv/bin/python" ] || python3 -m venv "/opt/$name/.venv"
    "/opt/$name/.venv/bin/pip" install -q --upgrade pip
    "/opt/$name/.venv/bin/pip" install -q -r "/opt/$name/requirements.txt"
    echo "  venv готов"
  fi
  # env-файл: не перезаписываем существующий
  local envf=${ENV_FILE[$name]:-}
  if [ -n "$envf" ] && [ ! -f "$envf" ]; then
    local example="$REPO/deploy/env/$name.env.example"
    [ "$name" = logistmot ] && example="$REPO/deploy/env/logistmot.env.example"
    install -m 600 "$example" "$envf"
    MISSING_ENV+=("$envf")
    echo "  создан пустой $envf — ЗАПОЛНИТЬ (или import-data.sh)"
  fi
  if [ "$user" != root ]; then
    chown -R "$user:$user" "/opt/$name"
    [ -f "$envf" ] && [ "$name" = logistmot ] && chown "$user:$user" "$envf"
  fi
}

for name in $(svc_names); do
  [ -n "$ONLY" ] && [ "$ONLY" != "$name" ] && continue
  install_service "$name"
done

# ---------- 4. PostgreSQL для metallompro ----------
if [ -z "$ONLY" ] || [ "$ONLY" = metallompro ]; then
  log "PostgreSQL: роль и база metallompro"
  systemctl enable --now postgresql >/dev/null
  ENVF=/etc/metallompro.env
  DBURL=$(grep -E '^DATABASE_URL=' "$ENVF" | cut -d= -f2- || true)
  if [ -z "$DBURL" ]; then
    PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
    DBURL="postgresql+psycopg2://metallompro:$PW@127.0.0.1:5432/metallompro"
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=$DBURL|" "$ENVF"
    echo "  DATABASE_URL сгенерирован и записан в $ENVF"
  fi
  read -r DBUSER DBNAME < <(bash "$REPO/deploy/pg-ensure-role.sh" "$ENVF")
  echo "  роль $DBUSER / база $DBNAME готовы (схему создаст сервис при старте, данные — import-data.sh)"
fi

# ---------- 5. nginx + сертификат для bp-service ----------
if [ -z "$ONLY" ] || [ "$ONLY" = bp-service ]; then
  log "nginx (https :8443 → bp-service :8093)"
  if [ ! -f /etc/ssl/bp-service/bp-service.crt ]; then
    bash "$REPO/deploy/make-cert.sh" "$(hostname)" "$SERVER_IP" >/dev/null
    echo "  выпущен самоподписанный сертификат для $(hostname) / $SERVER_IP"
  fi
  sed "s/server_name .*/server_name $(hostname) $SERVER_IP;/" \
      "$REPO/deploy/nginx/bp-service.conf" > /etc/nginx/sites-available/bp-service
  ln -sf /etc/nginx/sites-available/bp-service /etc/nginx/sites-enabled/bp-service
  rm -f /etc/nginx/sites-enabled/default
  nginx -t -q && systemctl enable --now nginx >/dev/null && systemctl reload nginx
fi

# ---------- 6. systemd, cron, healthcheck ----------
log "Юниты systemd и таймеры"
install -m 644 "$REPO"/deploy/systemd/*.service "$REPO"/deploy/systemd/*.timer /etc/systemd/system/
chmod 600 /etc/systemd/system/fines-service.service
systemctl daemon-reload

log "Cron-бэкап bp-service и healthcheck"
install -m 755 "$REPO/deploy/bp-backup.sh" /usr/local/bin/bp-backup.sh
( crontab -l 2>/dev/null | grep -v bp-backup.sh; cat "$REPO/deploy/root-crontab.txt" ) | crontab -
install -m 755 "$REPO/deploy/metoptorg-healthcheck.py" /usr/local/bin/metoptorg-healthcheck
mkdir -p /var/lib/metoptorg-healthcheck
[ -f /etc/metoptorg-healthcheck.env ] || { install -m 600 "$REPO/deploy/env/metoptorg-healthcheck.env.example" /etc/metoptorg-healthcheck.env; MISSING_ENV+=(/etc/metoptorg-healthcheck.env); }
systemctl enable --now metoptorg-healthcheck.timer metoptorg-healthcheck-summary.timer >/dev/null

# ---------- 7. запуск ----------
log "Включение служб"
for name in $(svc_names); do
  [ -n "$ONLY" ] && [ "$ONLY" != "$name" ] && continue
  for unit in $(svc_field "$name" 4); do
    if [ "$unit" = logistmot-bot.service ] && [ ${#MISSING_ENV[@]} -gt 0 ]; then
      # бот без токена бессмысленно перезапускать в цикле; включит import-data.sh
      systemctl enable "$unit" >/dev/null; echo "  $unit — включён, но не запущен (нет .env)"; continue
    fi
    systemctl enable --now "$unit" >/dev/null && echo "  $unit"
  done
done

echo
if [ ${#MISSING_ENV[@]} -gt 0 ]; then
  warn "Созданы пустые env-файлы — сервисы без секретов работать не будут:"
  printf '    %s\n' "${MISSING_ENV[@]}"
  warn "Следующий шаг: sudo deploy/import-data.sh <архив с данными старого сервера>"
else
  log "Проверка"; bash "$REPO/deploy/check.sh"
fi
