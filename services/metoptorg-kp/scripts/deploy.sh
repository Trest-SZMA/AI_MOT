#!/usr/bin/env bash
# Деплой сервиса оценки КП на сервер.
#
# Учитывает две ловушки, на которых уже обожглись:
#  1. rsync сохраняет владельца файлов с рабочей станции (UID 501) — база
#     становится недоступной для записи службе (DynamicUser). Поэтому код
#     копируем без владельца, а базу отдельно чиним chown под UID службы.
#  2. Служба пишет в StateDirectory, снаружи это /var/lib/private/<имя>.
#
# Проверка после деплоя обязательно включает ЗАПИСЬ: чтение работало и на
# сломанной базе, поэтому read-only проверка ничего не доказывает.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@192.168.6.157}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/fines_deploy}"
APP_DIR=/opt/metoptorg-kp
STATE_DIR=/var/lib/private/metoptorg-kp
UNIT=metoptorg-kp
PORT=8091
SSH=(ssh -i "$KEY" "$HOST")
cd "$(dirname "$0")/.."

echo "==> тесты"
.venv/bin/python -m pytest tests/ -q

echo "==> код → $HOST:$APP_DIR"
rsync -az --no-owner --no-group --exclude __pycache__ --exclude .venv \
      -e "ssh -i $KEY" app web tests scripts README.md "$HOST:$APP_DIR/"

if [[ "${1:-}" == "--with-db" ]]; then
  echo "==> база данных → $STATE_DIR (с сохранением учёток и бэкапом)"
  # Учётные записи живут ТОЛЬКО на сервере: локальная база их не содержит и
  # затирает. Поэтому выгружаем пользователей до замены и возвращаем после.
  "${SSH[@]}" "systemctl start ${UNIT}-backup.service || true
               python3 - <<'PYEOF'
import json, sqlite3
con = sqlite3.connect('$STATE_DIR/metopttorg.db')
rows = con.execute('SELECT login, full_name, password_hash, salt, role, '
                   'is_active FROM users').fetchall()
con.close()
open('/tmp/users_backup.json', 'w').write(json.dumps(rows))
print(f'  сохранено учёток: {len(rows)}')
PYEOF
               systemctl stop $UNIT"
  rsync -az --no-owner --no-group -e "ssh -i $KEY" \
        data/metopttorg.db "$HOST:$STATE_DIR/metopttorg.db"
  "${SSH[@]}" "python3 - <<'PYEOF'
import json, os, sqlite3
path = '/tmp/users_backup.json'
rows = json.loads(open(path).read()) if os.path.exists(path) else []
con = sqlite3.connect('$STATE_DIR/metopttorg.db')
have = {r[0] for r in con.execute('SELECT login FROM users')}
added = 0
for u in rows:
    if u[0] in have:
        continue
    con.execute('INSERT INTO users (login, full_name, password_hash, salt, '
                'role, is_active, created_at) VALUES (?,?,?,?,?,?, '
                'CURRENT_TIMESTAMP)', u)
    added += 1
con.commit()
con.close()
os.path.exists(path) and os.unlink(path)
print(f'  учёток возвращено: {added}')
PYEOF
               UID_SVC=\$(stat -c %u:%g $STATE_DIR); \
               chown -R \$UID_SVC $STATE_DIR; \
               chmod 660 $STATE_DIR/metopttorg.db"
fi

echo "==> перезапуск"
"${SSH[@]}" "systemctl restart $UNIT && sleep 4 && systemctl is-active $UNIT"

echo "==> проверка (чтение И запись)"
# пароль технической учётки лежит только на сервере (root, 600)
"${SSH[@]}" "python3 - <<'PYEOF'
import base64, json, pathlib, urllib.request, urllib.error
BASE = 'http://127.0.0.1:$PORT'
cred = pathlib.Path('/etc/metoptorg-kp-deploy.auth').read_text().strip()
AUTH = 'Basic ' + base64.b64encode(cred.encode()).decode()
def call(path, method='GET', body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={'Content-Type': 'application/json',
                                        'Authorization': AUTH})
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, (resp.read() if raw else json.load(resp))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]

st, docs = call('/api/kp'); assert st == 200, docs
print(f'  чтение КП: {st}, документов {len(docs)}')
if docs:
    doc_id = docs[0]['id']
    st, d = call(f'/api/kp/{doc_id}'); assert st == 200, d
    pos = d['positions'][0]
    print(f\"  расчёт: {len(d['positions'])} позиций, выкуп {d['totals']['base_buyout']:,.0f} ₽\")
    # ЗАПИСЬ: корректировка и откат
    st, r = call(f\"/api/kp/position/{pos['id']}\", 'PATCH', {'good_percent': 42})
    assert st == 200 and r['base']['good_percent'] == 42, f'запись не прошла: {st} {r}'
    st, _ = call(f\"/api/kp/position/{pos['id']}\", 'PATCH', {'good_percent': pos['manual']['good_percent']})
    print('  запись в базу: работает (корректировка сохранена и откачена)')
    st, x = call(f'/api/kp/{doc_id}/export', raw=True)
    assert st == 200 and len(x) > 4000, f'экспорт: {st}'
    print(f'  экспорт Excel: {len(x)} байт')
st, ai = call('/api/ai-status'); print(f\"  ИИ-поиск: включён={ai['enabled']}\")
# Режим входа сверяем с настройкой службы: включён — аноним обязан получить
# 401, выключен — обязан пройти. Молчаливое расхождение недопустимо.
env = pathlib.Path('/etc/metoptorg-kp.env').read_text() if pathlib.Path(
    '/etc/metoptorg-kp.env').exists() else ''
auth_off = 'METOPTTORG_AUTH=off' in env
try:
    with urllib.request.urlopen(urllib.request.Request(BASE + '/api/kp'),
                                timeout=30):
        anon_ok = True
except urllib.error.HTTPError as e:
    anon_ok = False
    assert e.code == 401, f'ожидался 401 без пароля, получен {e.code}'
if auth_off:
    assert anon_ok, 'вход выключен в настройке, но сервис требует пароль'
    print('  вход: ВЫКЛЮЧЕН (по настройке METOPTTORG_AUTH=off)')
else:
    assert not anon_ok, 'ОШИБКА: сервис отвечает без авторизации!'
    print('  вход: включён (аноним получает 401)')
print('  ВСЁ ОК')
PYEOF"
echo "==> готово: http://${HOST#*@}:$PORT"
