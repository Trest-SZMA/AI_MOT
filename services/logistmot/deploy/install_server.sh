#!/usr/bin/env bash
# Установка ЛогистМОТ на Linux-сервер (Ubuntu/Debian).
# Порядок:
#   1) распакуйте архив в /opt/logistmot
#   2) положите СВЕЖУЮ bot.db с рабочего компьютера в /opt/logistmot
#   3) проверьте /opt/logistmot/.env (токен, MAX_CHAT_ID, ADMIN_IDS,
#      PANEL_HOST=0.0.0.0, PANEL_USERS=... для руководителей)
#   4) sudo bash /opt/logistmot/deploy/install_server.sh
set -euo pipefail

DIR=/opt/logistmot
cd "$DIR"

echo "[1/5] Python и venv..."
if ! command -v python3 >/dev/null; then
    apt-get update -y && apt-get install -y python3 python3-venv
fi
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "[2/5] Служебный пользователь..."
id -u logistmot >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin logistmot
chown -R logistmot:logistmot "$DIR"
chmod 600 "$DIR/.env"

echo "[3/5] Проверка настроек..."
grep -q "MAX_BOT_TOKEN=" .env || { echo "ОШИБКА: нет MAX_BOT_TOKEN в .env"; exit 1; }
if grep -q "PANEL_HOST=0.0.0.0" .env && ! grep -q "PANEL_USERS=." .env; then
    echo "ВНИМАНИЕ: панель открыта в сеть, учётки руководителей (PANEL_USERS) не заданы"
fi

echo "[4/5] Службы systemd..."
cp deploy/logistmot-bot.service deploy/logistmot-panel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now logistmot-bot logistmot-panel

echo "[5/5] Статус:"
systemctl --no-pager -l status logistmot-bot | head -5
systemctl --no-pager -l status logistmot-panel | head -5

echo
echo "=== Готово ==="
echo "Журнал бота:   journalctl -u logistmot-bot -f"
echo "Журнал панели: journalctl -u logistmot-panel -f"
echo "Панель:        http://<ip-сервера>:8080 (логин/пароль из .env)"
