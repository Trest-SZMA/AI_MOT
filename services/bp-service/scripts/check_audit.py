"""Проверка сверки книг с фактом (16.09.2026): страница /audit, сводка по
типам, фильтры, выгрузка xlsx, пересборка по кнопке, ссылка в шапке.
Нужен поднятый сервис с BP_NEIGHBOR_DIR (снимок «Реализации»). Приём как в
check_forms: временная учётка, вход через /login, удаление."""
import os, secrets, sqlite3, subprocess, sys, zipfile, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from app import auth, audit
BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8199"); DB = os.path.join(ROOT, "bp.db")
login, password = "auditcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active, must_change_password) VALUES (?, ?, 'economist', ?, 1, 0) ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role='economist', is_active=1, must_change_password=0", (login, "Проверка сверки", auth.hash_password(password)))
c.commit()
out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}","--data-urlencode",f"password={password}",BASE+"/login"],capture_output=True,text=True).stdout
tok=[l.split("=",1)[1].split(";")[0] for l in out.splitlines() if l.lower().startswith("set-cookie: bp_session=")]
COOKIE=f"bp_session={tok[0]}"
ok=fail=0
def check(name, cond):
    global ok, fail
    ok += bool(cond); fail += (not cond); print(("  OK   " if cond else "  FAIL ")+name)
def get(path): return subprocess.run(["curl","-s","-b",COOKIE,BASE+path],capture_output=True,text=True).stdout
def post(path): return subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE,"-X","POST",BASE+path],capture_output=True,text=True).stdout

check("пересборка по кнопке", post("/audit/rebuild") in ("200","303"))
n = c.execute("SELECT COUNT(*) FROM stat_deal_audit").fetchone()[0]
wb = c.execute("SELECT COUNT(*) FROM stat_deal_audit WHERE plan_rev IS NOT NULL").fetchone()[0]
check(f"stat_deal_audit собрана: {n} сделок, с книгой {wb}", n > 100 and wb > 50)
r = get("/audit")
check("страница открывается", "Сверка книг с фактом" in r and "Traceback" not in r)
check("ссылка в шапке", 'href="/audit"' in r)
check("сводка по типам", 'id="audit-summary"' in r and "Все сделки" in r and "Чёрный лом" in r)
check("таблица сделок: три столбца", "модель" in r and "факт" in r and "книга" in r)
row = c.execute("SELECT * FROM stat_deal_audit WHERE plan_rev IS NOT NULL AND closed = 1 AND fact_profit IS NOT NULL ORDER BY CAST(deal_no AS INTEGER) DESC LIMIT 1").fetchone()
check(f"сделка {row['deal_no']} в таблице", f"<b>{row['deal_no']}</b>" in r)
check("сделка: модель по номенклатуре", (row["model_level"] or "").startswith("по номенклатуре"))
check("сделка: ошибки посчитаны", row["plan_price_err"] is not None and row["model_price_err"] is not None and row["plan_profit_err"] is not None)
bad = c.execute("SELECT COUNT(*) FROM stat_deal_audit WHERE series_full = 0 AND fact_profit IS NOT NULL").fetchone()[0]
check("прибыль факта только при полном факте затрат", bad == 0)
none_series = c.execute("SELECT COUNT(*) FROM stat_deal_audit WHERE fact_series IS NULL AND series_full = 1").fetchone()[0]
check("полный факт без затрат невозможен", none_series == 0)
f = get("/audit?type=pipe&scope=closed")
rows_part = f.split('id="audit-rows"', 1)[-1]
check("фильтр по типу и закрытым", "Труба и штанга<br>" in rows_part and "Кабель<br>" not in rows_part and "· закрыта" in rows_part)
f2 = get("/audit?scope=costs")
check("фильтр «с полным фактом затрат»", "затраты 1С неполные" not in f2)
x = subprocess.run(["curl","-s","-b",COOKIE,BASE+"/audit.xlsx"],capture_output=True).stdout
try:
    z = zipfile.ZipFile(io.BytesIO(x)); names = z.namelist()
    check("xlsx: два листа", "xl/worksheets/sheet1.xml" in names and "xl/worksheets/sheet2.xml" in names)
except zipfile.BadZipFile:
    check("xlsx: два листа", False)
s = audit.summary(c)
allrow = next((x for x in s if x["bp_type"] == ""), None)
check("сводка: ошибки экономиста и модели по всем", allrow and allrow["plan_price_err"] is not None and allrow["model_price_err"] is not None)
check("реестр справочников знает таблицу", c.execute("SELECT COUNT(*) FROM ref_registry WHERE key = 'stat_deal_audit'").fetchone()[0] == 1 if c.execute("SELECT name FROM sqlite_master WHERE name='ref_registry'").fetchone() else True)
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE,"-X","POST",BASE+"/logout"])
c.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)",(login,)); c.execute("DELETE FROM users WHERE login=?",(login,)); c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (учётка {login} удалена)")
