#!/usr/bin/env bash
# Создаёт/обновляет роль и базу PostgreSQL по DATABASE_URL из /etc/metallompro.env.
# Вызывается из install.sh и import-data.sh. Идемпотентно.
set -euo pipefail
ENVF=${1:-/etc/metallompro.env}
DBURL=$(grep -E '^DATABASE_URL=' "$ENVF" | cut -d= -f2- || true)
[ -n "$DBURL" ] || { echo "В $ENVF нет DATABASE_URL"; exit 1; }
read -r DBUSER DBPASS DBNAME < <(python3 - "$DBURL" <<'PY'
import sys, urllib.parse as u
p = u.urlparse(sys.argv[1]); print(p.username, u.unquote(p.password or ""), p.path.lstrip("/"))
PY
)
systemctl enable --now postgresql >/dev/null
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$DBUSER') THEN
    CREATE ROLE "$DBUSER" LOGIN PASSWORD '$DBPASS';
  ELSE
    ALTER ROLE "$DBUSER" PASSWORD '$DBPASS';
  END IF;
END \$\$;
SQL
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$DBUSER" "$DBNAME"
echo "$DBUSER $DBNAME"
