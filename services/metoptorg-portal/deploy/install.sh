#!/usr/bin/env bash
# Установка портала МетОптТорг на 192.168.6.157.
#
# Скрипт НИЧЕГО не трогает у работающих сервисов: создаёт свой каталог,
# свой env-файл, свою службу на свободном порту 8079. Existing-службы
# (fines, logistmot, ostatki, kp, realizaciya) не перезапускаются и не
# переконфигурируются.
#
# Запуск:  sudo bash /opt/metoptorg-portal/deploy/install.sh
set -euo pipefail

DIR=/opt/metoptorg-portal
ENVF=/etc/metoptorg-portal.env
UNIT=metoptorg-portal.service
PORT=8079

echo "[1/6] Проверяю, что порт $PORT свободен…"
if ss -tln 2>/dev/null | grep -q ":$PORT "; then
  # Занят нами же при повторной установке — это нормально.
  if systemctl is-active --quiet "$UNIT"; then
    echo "      порт занят самим порталом, продолжаю (обновление)"
  else
    echo "ОШИБКА: порт $PORT занят чужим процессом. Установка прервана,"
    echo "        чтобы ничего не сломать. Кто занял:"
    ss -tlnp | grep ":$PORT " || true
    exit 1
  fi
else
  echo "      свободен"
fi

echo "[2/6] Проверяю python3…"
command -v python3 >/dev/null || { echo "ОШИБКА: нет python3"; exit 1; }
python3 --version

echo "[3/6] Готовлю $ENVF…"
if [ -f "$ENVF" ] && grep -q "PORTAL_PASSWORD=." "$ENVF"; then
  echo "      уже существует, пароль сохраняю прежним"
else
  PASS=$(python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print(''.join(secrets.choice(a) for _ in range(20)))")
  cat > "$ENVF" <<EOF
# Портал МетОптТорг. Логин и пароль ТОЛЬКО от главной страницы —
# у каждого сервиса свой вход, портал их паролей не знает.
PORTAL_USER=metoptorg
PORTAL_PASSWORD=$PASS
PORTAL_PORT=$PORT
PORTAL_HOST=0.0.0.0
EOF
  echo "      создан новый пароль"
fi
chmod 600 "$ENVF"
chown root:root "$ENVF"

echo "[4/6] Проверяю файлы портала…"
for f in portal.py web/index.html web/services.json; do
  [ -f "$DIR/$f" ] || { echo "ОШИБКА: нет $DIR/$f"; exit 1; }
done
python3 -c "import ast;ast.parse(open('$DIR/portal.py').read())"
python3 -c "import json;d=json.load(open('$DIR/web/services.json'));print('      сервисов в реестре:',len(d['services']))"

echo "[5/6] Ставлю службу…"
cp "$DIR/deploy/$UNIT" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$UNIT"
sleep 2

echo "[6/6] Проверяю…"
systemctl is-active --quiet "$UNIT" || { echo "ОШИБКА: служба не поднялась"; journalctl -u "$UNIT" -n 20 --no-pager; exit 1; }
code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 "http://127.0.0.1:$PORT/" || true)
[ "$code" = "401" ] || { echo "ОШИБКА: без пароля портал отдал $code, ожидался 401"; exit 1; }
echo "      без пароля: 401 — как надо"

U=$(grep '^PORTAL_USER=' "$ENVF" | cut -d= -f2)
P=$(grep '^PORTAL_PASSWORD=' "$ENVF" | cut -d= -f2-)
code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 -u "$U:$P" "http://127.0.0.1:$PORT/" || true)
[ "$code" = "200" ] || { echo "ОШИБКА: с паролем портал отдал $code"; exit 1; }
echo "      с паролем: 200 — как надо"

echo
echo "=== Портал установлен ==="
echo "Адрес:  http://192.168.6.157:$PORT"
echo "Логин:  $U"
echo "Пароль: $P"
echo
echo "Журнал: journalctl -u $UNIT -f"
echo "Сменить пароль: отредактировать $ENVF и systemctl restart $UNIT"
