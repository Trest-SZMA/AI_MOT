"""Проверка загрузки перечня продавца в старом .xls (16.09.2026): файл
переводится в xlsx на лету, продавец читается из шапки с вложенными
кавычками, позиции и тоннаж сходятся. Нужен поднятый сервис и файл
1c/kp_unp/ЮНП_4_2026.xls (перечень Югранефтепром № 4/2026, 457,375 т).
Приём как в check_forms: временная учётка, вход через /login, временный БП
удаляется по каскаду."""
import os, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
XLS = os.environ.get("BP_CHECK_XLS", os.path.join(os.path.dirname(ROOT), "1c", "kp_unp", "ЮНП_4_2026.xls"))
login, password = "xlscheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка xls", auth.hash_password(password)))
c.commit()
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout

check("файл .xls на месте", os.path.isfile(XLS))
before = c.execute("SELECT COALESCE(MAX(id),0) FROM business_plans").fetchone()[0]
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/bp/new"],capture_output=True)
BP = c.execute("SELECT COALESCE(MAX(id),0) FROM business_plans").fetchone()[0]
check("временный БП создан", BP > before)
code = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE,"-F",f"file=@{XLS};filename=ЮНП_4_2026.xls",BASE+f"/bp/{BP}/items/upload"],capture_output=True,text=True).stdout
check("загрузка .xls принята", code in ("200","303"))
n = c.execute("SELECT COUNT(*), COALESCE(SUM(volume_t),0) FROM bp_items WHERE bp_id=?", (BP,)).fetchone()
check(f"позиции: {n[0]}, тоннаж {n[1]:.3f}", n[0] == 32 and abs(n[1] - 457.375) < 0.01)
bp = c.execute("SELECT seller_name, tender_ref, bp_type FROM business_plans WHERE id=?", (BP,)).fetchone()
check("продавец из шапки с вложенными кавычками", bp["seller_name"] == 'ООО "НК "Югранефтепром"')
check("номер перечня", bp["tender_ref"] == "4/2026")
check("тип сделки — чёрный лом", bp["bp_type"] == "ferrous")
km = c.execute("SELECT COUNT(*) FROM bp_items WHERE bp_id=? AND distance_km > 0", (BP,)).fetchone()[0]
check("расстояния позиций перенесены", km == 32)
lot = get(f"/bp/{BP}/lot")
check("страница лота без ошибок", "Traceback" not in lot and "Металлолом" in lot)
code2 = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE,"-F",f"file=@{os.path.join(ROOT,'requirements.txt')};filename=мусор.xls",BASE+f"/bp/{BP}/items/upload"],capture_output=True,text=True).stdout
check("битый .xls — понятный отказ, не 500", code2 in ("200","303"))
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c.execute("PRAGMA foreign_keys=ON"); c.execute("DELETE FROM business_plans WHERE id=?", (BP,))
c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (временный БП {BP} и учётка {login} удалены)")
