"""Проверка помощника ИИ (16.09.2026) без ключа: кнопки отключены, честные
сообщения, роуты не падают; с ключом (ANTHROPIC_API_KEY в окружении
проверки) — разбор перечня и предпросмотр на копии сделки. Приём как в
check_forms: временная учётка, вход через /login, удаление."""
import os, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "assistcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка ИИ", auth.hash_password(password)))
c.commit(); src = c.execute("SELECT id FROM business_plans WHERE source_name LIKE '%1700%' AND COALESCE(archived,0)=0 ORDER BY id LIMIT 1").fetchone()[0]; before=c.execute("SELECT MAX(id) FROM business_plans").fetchone()[0]; c.close()
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]; COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
def post(path, data=None, files=None):
    args=["curl","-s","-o","/dev/null","-w","%{http_code} %{redirect_url}","-b",COOKIE,"-X","POST"]
    for k,v in (data or {}).items(): args+=(["-F",f"{k}={v}"] if files else ["--data-urlencode",f"{k}={v}"])
    for k,v in (files or {}).items(): args+=["-F",f"{k}=@{v}"]
    return subprocess.run(args+[BASE+path],capture_output=True,text=True).stdout
post("/bp/copy", {"source": str(src), "copy_items": "1", "copy_costs": "1"})
c=sqlite3.connect(DB); bp_id=c.execute("SELECT MAX(id) FROM business_plans").fetchone()[0]; c.close()
check("копия сделки создана", bp_id != before)
on = bool(os.environ.get("ANTHROPIC_API_KEY"))
h=get(f"/bp/{bp_id}/lot"); e=get(f"/bp/{bp_id}/economics")
check("лот: форма разбора ИИ", 'id="assist-parse"' in h)
check("лот: блок спорных позиций", 'id="assist-matches"' in h)
check("экономика: блок объяснения", 'id="assist-explain"' in e)
if not on:
    check("без ключа: подпись «не подключён»", "не подключён" in h and "не подключён" in e)
    r = post(f"/bp/{bp_id}/assist/explain", {}); check("без ключа: объяснение возвращает понятное сообщение", "303" in r and "%D0%BD%D0%B5%20%D0%BF%D0%BE%D0%B4%D0%BA%D0%BB%D1%8E%D1%87" in r)
    r = post(f"/bp/{bp_id}/assist/match", {}); check("без ключа: сопоставление возвращает сообщение", "303" in r)
else:
    xlsx = "/Users/macpavel/FASTBP/1c/КП_1956_Лукойл-ПНОС_перечень.xlsx"
    r = post(f"/bp/{bp_id}/assist/parse", {}, {"file": xlsx}); check("с ключом: разбор принят", "303" in r and "assist-preview" in r)
    h=get(f"/bp/{bp_id}/lot"); check("с ключом: предпросмотр с позициями", 'id="assist-preview"' in h and "Лом алюминия" in h)
    r = post(f"/bp/{bp_id}/assist/apply", {"replace":"1"}); check("с ключом: загрузка позиций", "303" in r)
    c=sqlite3.connect(DB); n=c.execute("SELECT COUNT(*) FROM bp_items WHERE bp_id=?", (bp_id,)).fetchone()[0]; c.close(); check("с ключом: позиции в базе (7)", n == 7)
    r = post(f"/bp/{bp_id}/assist/explain", {}); check("с ключом: объяснение получено", "303" in r and "assist-explain" in r)
    e=get(f"/bp/{bp_id}/economics"); check("с ключом: текст объяснения на странице", 'class="assist-text"' in e)
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c=sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=ON"); c.execute("DELETE FROM business_plans WHERE id=?", (bp_id,)); c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (ключ {'есть' if on else 'нет'}; копия {bp_id} и учётка удалены)")
