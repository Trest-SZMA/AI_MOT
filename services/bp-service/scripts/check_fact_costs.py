"""Проверка факта затрат из регистра 1С (15.09.2026, вечер): подсказки
«факт / закладывали» в карточке затрат и таблица план/факт по статьям на
«План-факте». Нужен поднятый сервис (BP_CHECK_BASE), загруженные
stat_fact_costs и матрица, БП с «1865» и «1700» в названии источника.
Приём как в check_forms: временная учётка, вход через /login, удаление."""
import os, secrets, sqlite3, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "factcostcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='admin', is_active=1, must_change_password=0", (login, "Проверка факта затрат", auth.hash_password(password)))
c.commit()
bp_1865 = c.execute("SELECT id FROM business_plans WHERE source_name LIKE '%1865%' AND COALESCE(archived,0)=0 ORDER BY id LIMIT 1").fetchone()
n_fact = c.execute("SELECT COUNT(*) FROM stat_fact_costs").fetchone()[0]
n_matrix = c.execute("SELECT COUNT(*) FROM stat_cost_matrix WHERE source = 'факт 1С: регистр затрат'").fetchone()[0]
n_map = c.execute("SELECT COUNT(*) FROM ref_cost_item_map").fetchone()[0]
c.close()
if not bp_1865: sys.exit("нет БП 1865")
bp_id = bp_1865[0]
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
check("stat_fact_costs загружена", n_fact > 0)
check("матрица содержит строки регистра", n_matrix > 0)
check("справочник соответствия заполнен", n_map >= 40)
e=get(f"/bp/{bp_id}/economics")
check("экономика: подсказка «факт 1С»", "факт 1С <b>" in e)
check("экономика: рядом «закладывали»", "закладывали" in e)
check("экономика: без ошибок", "Traceback" not in e and "Internal Server Error" not in e)
p=get(f"/bp/{bp_id}/planfact")
check("план-факт: карточка затрат", 'id="cost-plan-fact"' in p)
check("план-факт: строка транспорта на отгрузку с фактом", "Транспортные расходы на отгрузку" in p and "Ж/д тариф" in p)
check("план-факт: списание засора как «только в факте»", "только в факте" in p)
check("план-факт: без ошибок", "Traceback" not in p and "Internal Server Error" not in p)
r=get("/references")
def post(path, data):
    args=["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE,"-X","POST"]
    for k,v in data.items(): args+=["--data-urlencode",f"{k}={v}"]
    return subprocess.run(args+[BASE+path],capture_output=True,text=True).stdout
check("справочники: строка факта затрат", "Факт затрат по сделкам" in r)
check("справочники: нормативы с колонкой «Факт 1С»", "Сверить с фактом 1С" in r and "расходится с фактом" in r)
check("справочники: калибровка по кнопке", post("/references/norms/calibrate", {}) in ("200","303"))
check("справочники: карточка соответствия статей", 'id="ref-fact-costs"' in r and "Найм а/м на отгрузку (23)" in r)
check("соответствие: правка принята", post("/references/cost-item-map", {"item_1c":"Рециклинг (26)","section":"Постоянные","item":"Прочие производственные расходы","is_fact_only":"0"}) in ("200","303"))
check("соответствие: секция без статьи отклонена", post("/references/cost-item-map", {"item_1c":"Рециклинг (26)","section":"Постоянные","item":"","is_fact_only":"0"}) in ("200","303") and "Рециклинг (26)" in get("/references"))
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c=sqlite3.connect(DB); c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (учётка {login} удалена)")
