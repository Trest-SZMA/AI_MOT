"""Проверка фактического слоя затрат (15.09.2026, вечер): площадка в шапке,
карточка «Затраты по факту 1С» на «Экономике», запись сумм по факту и
происхождение «факт 1С». Нужен поднятый сервис (BP_CHECK_BASE), загруженные
stat_fact_costs / stat_overhead_div / stat_division_tons и БП «1700».
Работает на КОПИИ сделки, чтобы не трогать расчёт; копия удаляется кодом
сервиса. Приём как в check_forms: временная учётка, вход через /login."""
import os, re, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "factlayercheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка факт-слоя", auth.hash_password(password)))
c.commit()
src = c.execute("SELECT id FROM business_plans WHERE source_name LIKE '%1700%' AND COALESCE(archived,0)=0 ORDER BY id LIMIT 1").fetchone()
c.close()
if not src: sys.exit("нет БП 1700")
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
def post(path, data, follow=False):
    args=["curl","-s","-o","/dev/null","-w","%{http_code} %{redirect_url}","-b",COOKIE,"-X","POST"]
    for k,v in data.items(): args+=["--data-urlencode",f"{k}={v}"]
    return subprocess.run(args+[BASE+path],capture_output=True,text=True).stdout
# копия сделки (штатный роут реестра) — берём самый новый БП после копии
c=sqlite3.connect(DB); before=c.execute("SELECT MAX(id) FROM business_plans").fetchone()[0]; c.close()
post("/bp/copy", {"source": str(src[0]), "copy_items": "1", "copy_costs": "1", "copy_route": "1"})
c=sqlite3.connect(DB); bp_id=c.execute("SELECT MAX(id) FROM business_plans").fetchone()[0]; c.close()
check("копия сделки создана", bp_id is not None and bp_id != before)
if bp_id == before: sys.exit(1)
h=get(f"/bp/{bp_id}/deal")
check("шапка: селектор площадки", 'id="site"' in h and "Площадка компании" in h)
check("шапка: площадка по факту определена", "По факту: " in h)
e=get(f"/bp/{bp_id}/economics")
check("экономика: карточка факт-слоя", 'id="fact-layer"' in e and "Затраты по факту 1С" in e)
check("экономика: строки со ставками и источником", "регистр затрат по сериям" in e and "распределяемые площадки" in e)
check("экономика: без ошибок", "Traceback" not in e and "Internal Server Error" not in e)
check("площадка вручную принята", post(f"/bp/{bp_id}/site", {"site":"Юг"}).startswith(("200","303")) and "задана вручную" in get(f"/bp/{bp_id}/deal"))
check("площадка: чужое имя отклонено", "нет в справочнике" in get(f"/bp/{bp_id}/deal") or post(f"/bp/{bp_id}/site", {"site":"Луна"}).startswith(("200","303")))
check("запись по факту принята", post(f"/bp/{bp_id}/costs/fact", {"variant":"bsp"}).startswith(("200","303")))
c=sqlite3.connect(DB)
n_fact = c.execute("SELECT COUNT(*) FROM bp_cost_origin o JOIN bp_costs b ON b.id=o.cost_id WHERE b.bp_id=? AND o.source='факт 1С'", (bp_id,)).fetchone()[0]
n_contam = c.execute("SELECT COUNT(*) FROM bp_costs WHERE bp_id=? AND item='Списание засора'", (bp_id,)).fetchone()[0]
total = c.execute("SELECT SUM(amount) FROM bp_costs WHERE bp_id=?", (bp_id,)).fetchone()[0]
c.close()
check("происхождение «факт 1С» у записанных статей", n_fact >= 5)
check("«Списание засора» в статьи не записано", n_contam == 0)
check("суммы записаны", (total or 0) > 0)
e=get(f"/bp/{bp_id}/economics")
check("экономика после записи: подсказки и P&L без ошибок", "Traceback" not in e and "по факту:" in e)
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
# удаление копии с каскадом (как в check_registry)
c=sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=ON"); c.execute("DELETE FROM business_plans WHERE id=?", (bp_id,))
c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (копия {bp_id} и учётка {login} удалены)")
