"""Проверка снимка факта «Реализации» на странице «План-факт» (шаг 3, 15.09.2026).

Нужен поднятый сервис (BP_CHECK_BASE), БП с «1700» в названии источника и
загруженный снимок (bp_fact_snapshot). Приём как в check_forms: временная
учётная запись, вход через /login, удаление после.
"""
import os, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "factcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка факта", auth.hash_password(password)))
c.commit()
row = c.execute("SELECT b.id FROM business_plans b JOIN bp_fact_snapshot s ON s.bp_id=b.id WHERE b.source_name LIKE '%1700%' AND COALESCE(b.archived,0)=0 ORDER BY b.id LIMIT 1").fetchone()
nosnap = c.execute("SELECT id FROM business_plans WHERE id NOT IN (SELECT bp_id FROM bp_fact_snapshot) AND COALESCE(archived,0)=0 ORDER BY id LIMIT 1").fetchone()
c.close()
if not row: sys.exit("нет БП 1700 со снимком — сначала импорт снимка")
bp_id = row[0]
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
h=get(f"/bp/{bp_id}/planfact")
check("карточка факта есть", 'id="fact-snapshot"' in h)
check("дата снимка", "снимок «Реализации» 2026-" in h)
check("серия 1С и регистры", "Серия 1С" in h and "регистры на" in h)
check("плитки план/факт", "Выручка без НДС: факт / план" in h and "% плана" in h)
check("таблица номенклатуры", "Номенклатура 1С" in h and "Труба НКТ" in h)
check("книга экономиста × доля", "Книга экономиста" in h)
check("нет ошибок шаблона (Traceback)", "Traceback" not in h and "Internal Server Error" not in h)
if nosnap:
    h2=get(f"/bp/{nosnap[0]}/planfact")
    check("БП без снимка: понятная подпись", "снимка нет" in h2 and "Traceback" not in h2)
r=get("/references")
check("справочники: строка снимка факта", "Факт реализации из 1С" in r)
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c=sqlite3.connect(DB); c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (учётка {login} удалена)")
