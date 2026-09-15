"""Проверка доли лота (шаг 2, 15.09.2026) через боевые страницы.

Нужен поднятый сервис (адрес в BP_CHECK_BASE) и в базе БП с «1865» в названии
источника плюс загруженный реестр сделок (ref_deals). Приём тот же, что в
check_forms: временная учётная запись, честный вход через /login, удаление
после. 13 проверок: доля из реестра в шапке и KPI, пометка в P&L, ручная
доля, отказ при 150 %, возврат к реестру, строка реестра на «Справочниках».
"""
import os, re, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "sharecheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка доли", auth.hash_password(password)))
c.commit()
bp_id = c.execute("SELECT id FROM business_plans WHERE source_name LIKE '%1865%' AND COALESCE(archived,0)=0 ORDER BY id LIMIT 1").fetchone()[0]
c.close()
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond, extra=""):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name, extra if not cond else "")
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
def post(path, data):
    args=["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE,"-X","POST"]
    for k,v in data.items(): args+=["--data-urlencode",f"{k}={v}"]
    return subprocess.run(args+[BASE+path],capture_output=True,text=True).stdout
h=get(f"/bp/{bp_id}/deal")
check("шапка: блок доли лота", 'id="lot-share"' in h)
check("шапка: доля из реестра", "из реестра сделок" in h)
check("шапка: реквизиты сделки 1865", "Сделка № 1865" in h and "УВМ 50" in h)
check("шапка: подпись расчёта на долю", "Расчёт ведётся на 50 % лота" in h)
check("KPI: доля в плитке лота", "доля 50 %" in h)
e=get(f"/bp/{bp_id}/economics")
check("экономика: P&L с пометкой доли", "доля 50 %" in e and "Себестоимость закупки (лот)" in e)
check("ручная доля 30 принята", post(f"/bp/{bp_id}/share", {"lot_share_pct":"30"}) in ("200","303"))
h=get(f"/bp/{bp_id}/deal")
check("шапка: задана вручную", "задана вручную" in h)
check("шапка: предупреждение о расхождении с реестром", "в шапке — 30" in h)
check("ошибка: доля 150 отклонена", post(f"/bp/{bp_id}/share", {"lot_share_pct":"150"}) in ("200","303") and "в шапке — 30" in get(f"/bp/{bp_id}/deal"))
check("возврат к реестру", post(f"/bp/{bp_id}/share", {"lot_share_pct":""}) in ("200","303") and "из реестра сделок" in get(f"/bp/{bp_id}/deal"))
r=get("/references")
check("справочники: строка реестра сделок", "Сделки Битрикса" in r and "DEAL_2026-09-14.xlsx" in r)
check("справочники: подпись ночной выгрузки", "Ночная выгрузка из 1С" in r)
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c=sqlite3.connect(DB); c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (учётка {login} удалена)")
