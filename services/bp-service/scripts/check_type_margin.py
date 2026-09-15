"""Проверка фактической рентабельности по типам (шаг 4, 15.09.2026): карточка на
«Справочниках», метрика в P&L. Нужен поднятый сервис, собранная stat_type_margin
и БП «1700». Приём как в check_forms: временная учётка, вход через /login."""
import os, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "margincheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка маржи", auth.hash_password(password)))
c.commit()
bp_id = c.execute("SELECT id FROM business_plans WHERE source_name LIKE '%1700%' AND COALESCE(archived,0)=0 ORDER BY id LIMIT 1").fetchone()[0]
n = c.execute("SELECT COUNT(*) FROM stat_type_margin").fetchone()[0]
c.close()
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
def post(path): return subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE,"-X","POST",BASE+path],capture_output=True,text=True).stdout
check("stat_type_margin собрана", n > 0)
r=get("/references")
check("карточка на справочниках", 'id="ref-type-margin"' in r and "Все сделки" in r)
check("строки типов", "Чёрный лом" in r and "Кабель" in r)
e=get(f"/bp/{bp_id}/economics")
check("P&L: валовая маржа", "Валовая маржа" in e)
check("P&L: факт по типу", "Факт по типу: медиана" in e)
check("KPI: факт по типу", "факт по типу" in e)
check("нет ошибок шаблона", "Traceback" not in e and "Internal Server Error" not in e)
check("пересборка по кнопке", post("/references/type-margin/rebuild") in ("200","303"))
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c=sqlite3.connect(DB); c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (учётка {login} удалена)")
