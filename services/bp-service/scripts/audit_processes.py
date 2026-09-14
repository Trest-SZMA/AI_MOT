"""Сквозная проверка процессов сервиса БП: импорт → правки → расчёт → выгрузки."""
import subprocess, sys, urllib.parse, sqlite3, os
BASE = "http://127.0.0.1:8011"
DB = "/Users/macpavel/FASTBP/bp-service1/bp.db"
ok = fail = 0
log = []

def db(q, args=()):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    r = c.execute(q, args).fetchall(); c.close(); return r

# ── Вход под каждой ролью ────────────────────────────────────────────
# Сервис закрыт авторизацией, поэтому проверка сначала заводит служебные
# учётные записи с одноразовыми паролями и входит под каждой ролью.
# Пароли случайные и нигде не сохраняются: после прогона войти под ними
# нельзя, а сами записи остаются отключёнными.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import secrets                                                  # noqa: E402
from app import auth                                            # noqa: E402

ROLES = ["manager", "economist", "logist", "director", "admin"]
COOKIE = {}

def _login_all():
    c = sqlite3.connect(DB)
    creds = {}
    for role in ROLES:
        login, password = f"audit_{role}", secrets.token_urlsafe(12)
        c.execute("INSERT INTO users (login, full_name, role, password_hash, "
                  "is_active, must_change_password) VALUES (?, ?, ?, ?, 1, 0) "
                  "ON CONFLICT(login) DO UPDATE SET password_hash = excluded."
                  "password_hash, role = excluded.role, is_active = 1, "
                  "must_change_password = 0",
                  (login, f"Проверка ({role})", role, auth.hash_password(password)))
        creds[role] = (login, password)
    c.commit(); c.close()
    for role, (login, password) in creds.items():
        out = subprocess.run(
            ["curl", "-s", "-i", "-X", "POST", "--data-urlencode", f"login={login}",
             "--data-urlencode", f"password={password}", BASE + "/login"],
            capture_output=True, text=True).stdout
        token = ""
        for line in out.splitlines():
            if line.lower().startswith("set-cookie: bp_session="):
                token = line.split("=", 1)[1].split(";")[0]
        if not token:
            print(f"Не удалось войти под ролью {role} — сервис поднят на {BASE}?")
            raise SystemExit(1)
        COOKIE[role] = f"bp_session={token}"

_login_all()

def post(path, data, role="admin"):
    args = ["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE[role],"-X","POST"]
    for k,v in data.items():
        args += ["--data-urlencode", f"{k}={v}"]
    args.append(BASE+path)
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()

def get(path, role="admin"):
    return subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}",
                           "-b",COOKIE[role],BASE+path], capture_output=True, text=True).stdout.strip()

def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; log.append(f"  OK   {name}")
    else: fail += 1; log.append(f"  FAIL {name} {detail}")

# ── 1. Импорт книги экономистов ──────────────────────────────────────
# Проверка работает ТОЛЬКО на своём БП, созданном импортом. Если книги нет
# или импорт не создал БП — выходим сразу: иначе правки ниже уедут в чужой,
# боевой БП (проверено на практике: пропавший файл 1935.xlsx привёл к тому,
# что аудит переписал реальный БП-0018-2026).
src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "1935.xlsx")
if not os.path.exists(src):
    print(f"Нет файла книги {src} — проверку запускать нельзя.\n"
          "Положите книгу 1935 (РИТЭК, труба) рядом со скриптом.")
    raise SystemExit(2)
before_max = db("SELECT COALESCE(MAX(id), 0) n FROM business_plans")[0]["n"]
before = db("SELECT COUNT(*) n FROM business_plans")[0]["n"]
r = subprocess.run(["curl","-s","-i","-b",COOKIE["manager"],"-F",f"file=@{src};filename=audit_1935.xlsx",
                    BASE+"/bp/import"], capture_output=True, text=True).stdout
loc = [l for l in r.splitlines() if l.lower().startswith("location")]
msg = urllib.parse.unquote(loc[0]) if loc else ""
bp = db("SELECT * FROM business_plans ORDER BY id DESC LIMIT 1")[0]
BP = bp["id"]
if BP <= before_max:
    print("Импорт не создал новый БП — дальше проверка правила бы чужой БП. "
          f"Ответ сервиса: {msg or r[:200]}")
    raise SystemExit(2)
check("импорт: создан БП", db("SELECT COUNT(*) n FROM business_plans")[0]["n"] == before+1)
items = db("SELECT * FROM bp_items WHERE bp_id=?", (BP,))
check("импорт: 8 позиций", len(items) == 8, f"({len(items)})")
check("импорт: тоннаж 535.985", abs(sum(i["volume_t"] for i in items) - 535.985) < 0.001)
check("импорт: коды продавца", all(i["seller_code"] for i in items))
check("импорт: группы учёта", sum(1 for i in items if i["cargo_group"]) >= 6)
check("импорт: единицы", all(i["unit"] for i in items))
check("импорт: вариант Лукойл", bp["has_luk"] == 1)
check("импорт: луковые цены", all(i["sale_price_luk"] for i in items))
check("импорт: луковые покупатели",
      db("SELECT COUNT(*) n FROM bp_item_sales WHERE bp_id=? AND variant='luk'", (BP,))[0]["n"] == 8)
check("импорт: позиционный засор Лукойл",
      any(i["contamination_pct_luk"] == 10.0 for i in items))
check("импорт: пункты отгрузки", db("SELECT COUNT(*) n FROM shipping_points")[0]["n"] >= 4)
check("импорт: расстояния из перечня",
      {p["distance_km"] for p in db("SELECT DISTINCT distance_km FROM shipping_points")} >= {11.0,120.0,130.0})
check("импорт: контрольные суммы", bp["control_net_profit_bsp"] and bp["control_net_profit_luk"])

# ── 2. Расчёт и сходимость ───────────────────────────────────────────
sys.path.insert(0, "/Users/macpavel/FASTBP/bp-service1")
from app.db import connect
from app import calc, norms, pricing
conn = connect()
bpr = conn.execute("SELECT * FROM business_plans WHERE id=?", (BP,)).fetchone()
its = conn.execute("SELECT * FROM bp_items WHERE bp_id=? ORDER BY id", (BP,)).fetchall()
cst = conn.execute("SELECT * FROM bp_costs WHERE bp_id=? ORDER BY id", (BP,)).fetchall()
for v, ctrl in (("bsp", bpr["control_net_profit_bsp"]), ("luk", bpr["control_net_profit_luk"])):
    bv, iv, cv = calc.apply_variant(bpr, its, cst, v)
    p = calc.pnl(bv, iv, cv, conn)
    check(f"расчёт {calc.VARIANTS[v]}: ЧП сходится с файлом", abs(p["net_profit"]-ctrl) <= 1,
          f"({p['net_profit']:.2f} vs {ctrl:.2f})")
    check(f"расчёт {calc.VARIANTS[v]}: тоннаж", abs(p["purchase_volume"]-535.985) < 0.01)
bv, iv, cv = calc.apply_variant(bpr, its, cst, "bsp")
p = calc.pnl(bv, iv, cv, conn)
check("расчёт: НДС на трубу начислен", p["vat_unrecovered"] > 0)
check("расчёт: капитал посчитан", p["capital_cost"] > 0)
model = norms.evaluate_model(iv, conn, {})
stages = {s["name"]: {b["base"]: b["amount"] for b in s["bases"]} for s in model["stages"]}
bpl = calc.base_pnl(bv, iv, cv, conn, stages)
check("базы: прибыль сходится с P&L",
      abs(sum(b["profit"] for b in bpl["bases"]) - p["profit_before_tax"]) < 1)
check("базы: направления построены", len(bpl["directions"]) >= 2)
check("логистика: рейсы посчитаны", all(t["trips"] > 0 for t in norms.trips_plan(iv, conn)))
# Квартальный срез: суммы кварталов обязаны совпасть с помесячными.
sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id=?", (BP,)).fetchall()
fact_rows = conn.execute("SELECT * FROM bp_fact WHERE bp_id=?", (BP,)).fetchall()
pf = calc.plan_fact(bv, iv, cv, sched_rows, fact_rows, conn)
qs = pf["quarters"]["rows"]
check("кварталы: срез построен", len(qs) >= 1, f"({len(qs)})")
check("кварталы: объём сходится с помесячным",
      abs(sum(q["plan_vol"] for q in qs) - pf["totals"]["plan_vol"]) < 0.001)
check("кварталы: выручка сходится с помесячной",
      abs(sum(q["plan_rev"] for q in qs) - pf["totals"]["plan_rev"]) < 1)
check("кварталы: месяцы не потеряны",
      sorted(m for q in qs for m in q["months"]) == [r["month"] for r in pf["rows"]])
check("кварталы: остаток закрывается в ноль",
      not qs or abs(qs[-1]["balance_end_plan"]) < 0.001,
      f"({qs[-1]['balance_end_plan'] if qs else 0:.3f})")
check("цены: матрица построена", bool(pricing.price_matrix(conn, ["Труба НКТ"]).get("rows")))
check("цены: прогноз есть", bool(pricing.sale_price_hints(conn, "Труба НКТ").get("forecast")))
conn.close()

# ── 3. Все формы редактирования ──────────────────────────────────────
i0 = items[0]["id"]
edits = [
 ("позиции: цены и поля", f"/bp/{BP}/items/prices", {
    "cargo_group_%d"%i0:"Труба НКТ", "sale_group_%d"%i0:"Лом черных металлов",
    "unit_%d"%i0:"т", "contamination_%d"%i0:"7", "sale_price_%d"%i0:"19900",
    "buyer_%d"%i0:"Чермет Волжский", "own_%d"%i0:"60", "cut_%d"%i0:"40", "batch_%d"%i0:"20"}),
 ("позиции: реквизиты и экспертиза", f"/bp/{BP}/items/requisites", {
    "balance_cost_%d"%i0:"125000", "liquidity_%d"%i0:"неликвид",
    "price_set_by_%d"%i0:"Шумейко", "sale_period_%d"%i0:"IV квартал 2026",
    "origin_reason_%d"%i0:"от списания АГЗУ"}),
 ("шапка сделки", f"/bp/{BP}/section/header", {"manager":"Иванов","bitrix_task_id":"12345"}),
 ("стороны", f"/bp/{BP}/section/parties", {"seller_name":'ООО "РИТЭК"',"buyer_name":"Чермет Волжский"}),
 ("параметры лота", f"/bp/{BP}/section/lot", {"lot_cost":"6233452","auction_step":"100000"}),
 ("план продажи: покупатель", f"/bp/{BP}/items/{i0}/sales", {"buyer":"Втор-ресурс","volume_t":"0.01","sale_price":"13500"}),
 ("свод: заполнение оптом", f"/bp/{BP}/items/aggregate", {
    "item_ids":",".join(str(i["id"]) for i in items[:2]), "price":"18000"}),
 ("автоподтягивание", f"/bp/{BP}/autofill", {}),
 ("сопоставление 1С", f"/bp/{BP}/match/auto", {}),
 ("риски: правка", None, None),
 ("сценарий цен", f"/bp/{BP}/scenarios", {"name":"минус 1000","price_delta":"-1000"}),
 ("статус: на проверку", f"/bp/{BP}/status", {"action":"На проверке"}),
]
for name, path, data in edits:
    if path is None:
        rid = db("SELECT id FROM bp_risks WHERE bp_id=? LIMIT 1", (BP,))
        if rid:
            code = post(f"/bp/{BP}/risks/{rid[0]['id']}/update",
                        {"risk_type":"Цена","description":"падение рынка","probability":"В","impact":"К","mitigation":"фиксация"})
            check(name, code == "303", f"(код {code})")
        continue
    code = post(path, data)
    check(name, code == "303", f"(код {code})")

# пункты отгрузки и корректировка баз
pt = db("SELECT id FROM shipping_points LIMIT 1")
if pt:
    check("пункт: плечо маршрута",
          post(f"/bp/{BP}/points/{pt[0]['id']}/route", {"to_name":"Чермет","distance_km":"245"}) == "303")
    # Без ключа 2ГИС роут обязан отвечать понятным сообщением, а не падать
    # (разбор ответов 2ГИС проверяется отдельно: scripts/check_dgis.py).
    check("2ГИС: без ключа понятный отказ",
          post(f"/bp/{BP}/points/{pt[0]['id']}/route/2gis",
               {"to_name": "Чермет"}) == "303")
base = db("SELECT DISTINCT division FROM bp_items WHERE bp_id=? LIMIT 1", (BP,))[0]["division"]
check("базы: корректировка разреза",
      post(f"/bp/{BP}/bases/split", {"base":base,"logistics":"100000","processing":"200000"}) == "303")

# ── 4. Результаты правок в базе ──────────────────────────────────────
it = db("SELECT * FROM bp_items WHERE id=?", (i0,))[0]
check("сохранено: группа продажи", it["sale_group"] == "Лом черных металлов")
check("сохранено: экспертиза", it["liquidity"] == "неликвид" and it["price_set_by"] == "Шумейко")
check("сохранено: дата цены проставлена сама", bool(it["price_set_at"]))
check("сохранено: период реализации", it["sale_period"] == "IV квартал 2026")
check("сохранено: задача Битрикс",
      db("SELECT bitrix_task_id FROM business_plans WHERE id=?", (BP,))[0]["bitrix_task_id"] == "12345")
check("сохранено: план продажи", db("SELECT COUNT(*) n FROM bp_item_sales WHERE item_id=? AND variant='bsp'", (i0,))[0]["n"] >= 1)
check("сохранено: корректировка базы", db("SELECT COUNT(*) n FROM bp_base_split WHERE bp_id=?", (BP,))[0]["n"] == 1)
check("сохранено: маршрут", db("SELECT COUNT(*) n FROM shipping_routes")[0]["n"] >= 1)
check("версии: автоснимки", db("SELECT COUNT(*) n FROM bp_versions WHERE bp_id=?", (BP,))[0]["n"] >= 1)
check("журнал: действия пишутся", db("SELECT COUNT(*) n FROM audit_log WHERE bp_id=?", (BP,))[0]["n"] >= 5)

# ── 5. Страницы и роли ───────────────────────────────────────────────
for path in ["/", f"/bp/{BP}/deal", f"/bp/{BP}/start", f"/bp/{BP}/lot", f"/bp/{BP}/logistics", f"/bp/{BP}/economics", f"/bp/{BP}/decision", f"/bp/{BP}/lot?variant=luk", "/portfolio", "/references",
             f"/bp/{BP}/export", f"/bp/{BP}/schedule", f"/bp/{BP}/planfact", f"/bp/{BP}/route",
             f"/bp/{BP}/versions/diff"]:
    check(f"страница {path}", get(path) == "200")
# Сравнение версий: явные номера, перевёрнутый порядок и мусор в параметрах
# не должны ронять страницу.
vers = [r["version"] for r in
        db("SELECT version FROM bp_versions WHERE bp_id=? ORDER BY version", (BP,))]
if len(vers) >= 2:
    check("сравнение версий: выбор номеров",
          get(f"/bp/{BP}/versions/diff?a={vers[0]}&b={vers[-1]}") == "200")
    check("сравнение версий: обратный порядок",
          get(f"/bp/{BP}/versions/diff?a={vers[-1]}&b={vers[0]}") == "200")
check("сравнение версий: мусор в параметрах",
      get(f"/bp/{BP}/versions/diff?a=abc&b=999") == "200")
for role in ["manager","economist","logist","director","admin"]:
    check(f"роль {role}: карточка", get(f"/bp/{BP}/deal", role) == "200")

# ── 6. Выгрузки документов ───────────────────────────────────────────
import glob, os as _os
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE["logist"],"-X","POST",
                BASE+f"/bp/{BP}/route/build"], capture_output=True)
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE["admin"],
                BASE+f"/bp/{BP}/export"], capture_output=True)
outdir = f"/Users/macpavel/FASTBP/bp-service1/output/{bp['bp_number']}"
made = [_os.path.basename(f) for f in glob.glob(outdir+"/*")]
check("выгрузка: сформированы файлы", len(made) >= 6, f"({len(made)})")
for kind in ["бизнес-план.xlsx", "бизнес-план.docx", "выручка", "затраты",
             "план_транспорт", "план_переработка"]:
    check(f"выгрузка: есть {kind}", any(kind in m for m in made))
import openpyxl as _x
for name, minrows in (("выручка",1), ("затраты",1), ("план_транспорт",1),
                      ("план_переработка",1)):
    f = [m for m in made if name in m]
    if not f:
        check(f"выгрузка {name}: строки", False, "(файла нет)"); continue
    ws = _x.load_workbook(outdir+"/"+f[0], data_only=True).active
    rows = sum(1 for r in ws.iter_rows(min_row=2, values_only=True)
               if any(c is not None for c in r))
    check(f"выгрузка {name}: не пустая", rows >= minrows, f"({rows} строк)")
for m in made:
    code = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}",
                           "-b",COOKIE["admin"], BASE+f"/bp/{BP}/download/"
                           + urllib.parse.quote(m)], capture_output=True, text=True).stdout.strip()
    check(f"скачивание {m[:34]}", code == "200", f"(код {code})")
# предупреждения о пустых выгрузках
html_no_route = subprocess.run(["curl","-s","-b",COOKIE["admin"], BASE+"/bp/17/export"],
                               capture_output=True, text=True).stdout
check("выгрузка: предупреждение без маршрута",
      "Граф маршрута не построен" in html_no_route)

# ── 6а. Импорт факта из 1С ───────────────────────────────────────────
# Файл собирается на месте: образец выгрузки от 1С-специалиста ещё не
# получен, а проверять распознавание колонок и свод по месяцам надо уже
# сейчас. Заодно проверяется, что предпросмотр НЕ пишет в базу.
import tempfile as _tf
from openpyxl import Workbook as _WB
from app import fact_import as _fi

_wb = _WB(); _ws = _wb.active
_ws.append(["Отчёт о реализации"])                       # шапка отчёта сверху
_ws.append([])
_ws.append(["№ БП", "Месяц", "Номенклатура", "Объём, тн", "Выручка без НДС",
            "Прямые затраты"])
for _r in ((bp["bp_number"], "01.2026", "Труба НКТ", 120.5, 2410000, 180000),
           (bp["bp_number"], "01.2026", "Лом 3А", 80.0, 1600000, 90000),
           (bp["bp_number"], "02.2026", "Труба НКТ", 200.0, 4000000, 260000),
           ("БП-9999-2026", "01.2026", "Чужой лот", 999.0, 9990000, 0)):
    _ws.append(list(_r))
_fact_file = _os.path.join(_tf.gettempdir(), "audit_fact.xlsx")
_wb.save(_fact_file)

_parsed = _fi.parse_file(_fact_file)
check("факт: колонки распознаны",
      {"bp_number", "period", "volume", "revenue", "costs"}
      <= set(_parsed["columns"]), f"({sorted(_parsed['columns'])})")
check("факт: замечаний нет", not _parsed["problems"], f"({_parsed['problems']})")
_sum = _fi.aggregate(_parsed["rows"], bp["bp_number"], 1, 2026, 6)
check("факт: чужой БП отброшен", _sum["skipped_other_bp"] == 1)
check("факт: свод по месяцам", [m["month"] for m in _sum["months"]] == [1, 2])
check("факт: суммы сложены",
      abs(_sum["months"][0]["volume"] - 200.5) < 1e-9
      and abs(_sum["months"][1]["revenue"] - 4000000) < 1e-9)
_before_fact = db("SELECT COUNT(*) n FROM bp_fact WHERE bp_id=?", (BP,))[0]["n"]
_code = subprocess.run(
    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-b", COOKIE["economist"],
     "-F", f"file=@{_fact_file}", BASE + f"/bp/{BP}/planfact/import"],
    capture_output=True, text=True).stdout.strip()
check("факт: предпросмотр открылся", _code == "200", f"(код {_code})")
check("факт: предпросмотр ничего не записал",
      db("SELECT COUNT(*) n FROM bp_fact WHERE bp_id=?", (BP,))[0]["n"] == _before_fact)
check("факт: запись после подтверждения",
      post(f"/bp/{BP}/planfact/apply",
           {"source": "проверка.xlsx", "mode": "replace", "vol_1": "200.5",
            "rev_1": "4010000", "costs_1": "270000"}, "economist") == "303")
_saved = db("SELECT * FROM bp_fact WHERE bp_id=? AND month=1", (BP,))
check("факт: сохранён верно",
      bool(_saved) and abs(_saved[0]["volume_t"] - 200.5) < 1e-9,
      f"({_saved[0]['volume_t'] if _saved else 'нет строки'})")
check("факт: режим «прибавить»",
      post(f"/bp/{BP}/planfact/apply",
           {"source": "проверка2.xlsx", "mode": "add", "vol_1": "10",
            "rev_1": "0", "costs_1": "0"}, "economist") == "303"
      and abs(db("SELECT volume_t FROM bp_fact WHERE bp_id=? AND month=1",
                 (BP,))[0]["volume_t"] - 210.5) < 1e-9)
check("факт: менеджеру загрузка закрыта",
      post(f"/bp/{BP}/planfact/apply", {"vol_1": "1"}, "manager") == "303"
      and abs(db("SELECT volume_t FROM bp_fact WHERE bp_id=? AND month=1",
                 (BP,))[0]["volume_t"] - 210.5) < 1e-9)
_os.unlink(_fact_file)

# ── 6б. Предварительный расчёт «на лету» ─────────────────────────────
# Живой пересчёт обязан давать те же числа, что и обычный расчёт ЭТОГО ЖЕ
# состояния БП, и при этом ничего не писать в базу. Состояние берём заново:
# выше скрипт уже правил цены, лот и план продажи.
import json as _json
def _preview(payload, role="economist"):
    out = subprocess.run(
        ["curl", "-s", "-b", COOKIE[role], "-H", "Content-Type: application/json",
         "-X", "POST", "-d", _json.dumps(payload), BASE + f"/bp/{BP}/preview"],
        capture_output=True, text=True).stdout
    try:
        return _json.loads(out)
    except ValueError:
        return {}

_conn = connect()
_bp = _conn.execute("SELECT * FROM business_plans WHERE id=?", (BP,)).fetchone()
_items = _conn.execute("SELECT * FROM bp_items WHERE bp_id=? ORDER BY id", (BP,)).fetchall()
_costs = _conn.execute("SELECT * FROM bp_costs WHERE bp_id=? ORDER BY id", (BP,)).fetchall()
_now = {}
for _v in ("bsp", "luk"):
    _b, _i, _c = calc.apply_variant(_bp, _items, _costs, _v)
    _now[_v] = calc.pnl(_b, _i, _c, _conn)
_conn.close()

for _v in ("bsp", "luk"):
    _got = _preview({"variant": _v, "items": {}, "bp": {}})
    check(f"предрасчёт {calc.VARIANTS[_v]}: совпадает с расчётом",
          _got and abs(_got["totals"]["net_profit"] - _now[_v]["net_profit"]) < 0.01,
          f"({_got.get('totals', {}).get('net_profit')} против {_now[_v]['net_profit']:.2f})")
_base = _preview({"variant": "bsp", "items": {}, "bp": {}})
check("предрасчёт: строк столько же, сколько в расчёте",
      len(_base["rows"]) == len(_now["bsp"]["rows"]),
      f"({len(_base['rows'])} против {len(_now['bsp']['rows'])})")

_i0 = _items[0]["id"]
_price_before = _items[0]["sale_price"]
_lot_before = _bp["lot_cost"]
_up = _preview({"variant": "bsp", "items": {str(_i0): {"sale_price": "99000"}}, "bp": {}})
check("предрасчёт: цена меняет выручку и прибыль",
      _up["totals"]["revenue"] > _base["totals"]["revenue"]
      and _up["totals"]["net_profit"] > _base["totals"]["net_profit"])
_lot = _preview({"variant": "bsp", "items": {}, "bp": {"lot_cost": "9000000"}})
check("предрасчёт: стоимость лота меняет закупку",
      abs(_lot["totals"]["lot_cost"] - 9000000) < 1
      and _lot["totals"]["net_profit"] < _base["totals"]["net_profit"])
check("предрасчёт: в базу ничего не записано",
      abs((db("SELECT sale_price FROM bp_items WHERE id=?", (_i0,))[0]["sale_price"] or 0)
          - (_price_before or 0)) < 0.01
      and abs((db("SELECT lot_cost FROM business_plans WHERE id=?", (BP,))[0]["lot_cost"] or 0)
              - (_lot_before or 0)) < 0.01)
check("предрасчёт: мусор в полях не роняет расчёт",
      _preview({"variant": "bsp", "items": {"нет": {"sale_price": "abc"}},
                "bp": {"lot_cost": "abc"}}).get("totals") is not None)

# ── 7. Авторизация ───────────────────────────────────────────────────
anon = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}",
                       BASE+f"/bp/{BP}/deal"], capture_output=True, text=True).stdout.strip()
check("доступ: без входа карточка закрыта", anon == "303", f"(код {anon})")
anon_post = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-X","POST",
                            "--data-urlencode","manager=Взломщик",
                            BASE+f"/bp/{BP}/section/header"],
                           capture_output=True, text=True).stdout.strip()
check("доступ: без входа правка закрыта", anon_post == "303", f"(код {anon_post})")
check("доступ: правка не прошла",
      db("SELECT manager FROM business_plans WHERE id=?", (BP,))[0]["manager"] != "Взломщик")
bad = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-X","POST",
                      "--data-urlencode","login=audit_admin",
                      "--data-urlencode","password=подбор", BASE+"/login"],
                     capture_output=True, text=True).stdout.strip()
check("доступ: неверный пароль отклонён", bad == "303", f"(код {bad})")
check("доступ: попытка записана в журнал",
      db("SELECT COUNT(*) n FROM audit_log WHERE action='login_failed' "
         "AND user_name='audit_admin'")[0]["n"] >= 1)
check("журнал: вход виден руководителю", get("/journal", "director") == "200")
check("журнал: экономисту закрыт", get("/journal", "economist") == "303")
check("пользователи: только администратору",
      get("/users", "admin") == "200" and get("/users", "manager") == "303")

# Служебные учётные записи проверки отключаются: пароли были одноразовыми,
# оставлять действующий вход после прогона нельзя.
_c = sqlite3.connect(DB)
_c.execute("PRAGMA foreign_keys=ON")
_c.execute("UPDATE users SET is_active = 0 WHERE login LIKE 'audit_%'")
_c.execute("DELETE FROM user_sessions WHERE user_id IN "
           "(SELECT id FROM users WHERE login LIKE 'audit_%')")
_c.commit(); _c.close()

print("\n".join(log))

# Свой БП убираем за собой — иначе реестр зарастает копиями 1935.
# При провале оставляем: по нему разбирают причину.
if fail == 0:
    _c = sqlite3.connect(DB)
    _c.execute("PRAGMA foreign_keys=ON")
    _c.execute("DELETE FROM business_plans WHERE id=?", (BP,))
    _c.commit(); _c.close()
    print(f"\nИТОГО: {ok} ok, 0 fail   (БП аудита {BP} удалён)")
else:
    print(f"\nИТОГО: {ok} ok, {fail} fail   (БП аудита оставлен: {BP})")
