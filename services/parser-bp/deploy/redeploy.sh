#!/bin/bash
# Передеплой парсера БП на сервер 192.168.6.157 с полной очисткой старой установки.
#
#   ./deploy/redeploy.sh            # новый порт 8094
#   ./deploy/redeploy.sh 8095       # другой порт
#
# Что делает:
#   1) сносит старую службу, каталог, конфиг и плитку портала;
#   2) заливает код заново, собирает venv, ставит библиотеки;
#   3) поднимает службу на новом порту и регистрирует плитку;
#   4) проверяет, что порт реально отвечает 200.

set -euo pipefail

PORT="${1:-8094}"
HOST=192.168.6.157
KEY=~/.ssh/fines_deploy
SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=10 root@$HOST"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== Передеплой парсера БП на порт $PORT =="

# --- 0. связь
if ! $SSH 'true' 2>/dev/null; then
    echo "ОШИБКА: сервер $HOST недоступен по SSH. Передеплой не начат."
    exit 1
fi

# --- 1. снос старой установки
echo "-- убираю старую установку"
$SSH '
systemctl disable --now parser-bp.service 2>/dev/null || true
rm -f /etc/systemd/system/parser-bp.service /etc/parser-bp.env
systemctl daemon-reload
rm -rf /opt/parser-bp /var/lib/parser-bp
python3 - <<PY
import json, pathlib
p = pathlib.Path("/opt/metoptorg-portal/web/services.json")
d = json.loads(p.read_text(encoding="utf-8"))
before = len(d["services"])
d["services"] = [s for s in d["services"] if s.get("id") != "parserbp"]
if len(d["services"]) != before:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("   плитка портала удалена")
PY
'

# --- 2. заливка кода
echo "-- заливаю код"
rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude 'packages' --exclude 'packages_py3.14.zip' \
  --exclude 'bat_src' --exclude '*.bat' --exclude '.last_folder.txt' --exclude '.claude' \
  --exclude '.DS_Store' \
  -e "ssh -i $KEY -o BatchMode=yes" \
  "$DIR/" "root@$HOST:/opt/parser-bp/"

# --- 3. окружение и служба
echo "-- ставлю библиотеки (2-3 минуты)"
PASS=$(python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(14)))")

$SSH "
set -e
cd /opt/parser-bp
chown -R root:root /opt/parser-bp
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet
.venv/bin/python -c 'import streamlit, starlette; print(\"   streamlit\", streamlit.__version__, \"| starlette\", starlette.__version__)'

printf 'PARSER_BP_SERVER=1\nPARSER_BP_PASSWORD=%s\n' '$PASS' > /etc/parser-bp.env
chmod 600 /etc/parser-bp.env

sed 's/--server.port 8093/--server.port $PORT/' /opt/parser-bp/deploy/parser-bp.service \
    > /etc/systemd/system/parser-bp.service
systemctl daemon-reload
systemctl enable --now parser-bp.service
"

# --- 4. плитка портала
echo "-- регистрирую плитку"
$SSH "python3 - <<PY
import json, pathlib
p = pathlib.Path('/opt/metoptorg-portal/web/services.json')
d = json.loads(p.read_text(encoding='utf-8'))
d['services'] = [s for s in d['services'] if s.get('id') != 'parserbp']
d['services'].append({
    'id': 'parserbp',
    'name': 'Парсер БП',
    'group': 'МетОптТорг',
    'summary': 'Проверять количество месяцев вывоза в бизнес-планах',
    'port': $PORT,
    'auth': 'form',
    'owner': 'экономист',
    'hidden': False,
})
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('   плитка добавлена, порт $PORT')
PY"

# --- 5. проверка
echo "-- проверяю"
sleep 8
for U in "/" "/_stcore/health"; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 15 "http://$HOST:$PORT$U" || echo 000)
    printf "   %-18s http %s\n" "$U" "$CODE"
    [ "$CODE" = "200" ] || { echo "ОШИБКА: $U вернул $CODE"; $SSH 'journalctl -u parser-bp -n 20 --no-pager'; exit 1; }
done

echo
echo "Готово. Адрес: http://$HOST:$PORT"
echo "Пароль входа: $PASS"
