"""Проверка реестра: копирование сделки по образцу, поиск, фильтры, архив.

Сверка 17.08.2026, раздел 5: «новый БП начинается с нуля, хотя 80% параметров
повторяются от лота к лоту одного продавца» и «в реестре нет ни поиска, ни
фильтра, ни сортировки». Здесь проверяется, что появившееся взамен работает и
ничего не теряет:

* копия наследует параметры сделки и выбранные части, но НЕ забирает
  стоимость лота, шаг аукциона, контрольные суммы, статус и даты образца;
* образец после копирования не изменился;
* поиск находит по номеру и продавцу, фильтры и порядок работают;
* архив убирает БП из рабочего реестра и из портфеля лотов, возврат — обратно,
  и ничего при этом не удаляется.

Скрипт работает на своих временных БП и убирает за собой.

    .venv/bin/python scripts/check_registry.py

Адрес сервиса — BP_CHECK_BASE (по умолчанию 127.0.0.1:8011).
"""
import os
import secrets
import sqlite3
import subprocess
import sys
import urllib.parse

BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8011")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "bp.db")
BOOK = os.path.join(ROOT, "attachments", "БП-0014-2026",
                    "1738_25.11.25_v0_Лукойл_ЗС_7919тн.xlsx")
sys.path.insert(0, ROOT)
from app import auth                                     # noqa: E402
from app.main import COPY_BP_FIELDS                      # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {detail}")


def db(q, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(q, args).fetchall()
    c.close()
    return rows


login, password = "regcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active,"
          " must_change_password) VALUES (?, 'Проверка реестра', 'admin', ?, 1, 0)"
          " ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash,"
          " role='admin', is_active=1, must_change_password=0",
          (login, auth.hash_password(password)))
c.commit()
c.close()
out = subprocess.run(["curl", "-s", "-i", "-X", "POST",
                      "--data-urlencode", f"login={login}",
                      "--data-urlencode", f"password={password}", BASE + "/login"],
                     capture_output=True, text=True).stdout
token = [l.split("=", 1)[1].split(";")[0] for l in out.splitlines()
         if l.lower().startswith("set-cookie: bp_session=")]
check("вход выполнен", bool(token))
if not token:
    raise SystemExit(1)
COOKIE = f"bp_session={token[0]}"


def post(path, data):
    args = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "-b", COOKIE, "-X", "POST"]
    for k, v in data.items():
        args += ["--data-urlencode", f"{k}={v}"]
    return subprocess.run(args + [BASE + path],
                          capture_output=True, text=True).stdout.strip()


def get(path, **params):
    """Страница реестра. Значения фильтров кодируются: кириллица в адресе
    без кодирования уходит сырыми байтами и до сервера доезжает мусором —
    браузер так не делает, а curl делает."""
    if params:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    return subprocess.run(["curl", "-s", "-b", COOKIE, BASE + path],
                          capture_output=True, text=True).stdout


created = []


def newest():
    return db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]


before = newest()
subprocess.run(["curl", "-s", "-o", "/dev/null", "-b", COOKIE,
                "-F", f"file=@{BOOK};filename=regcheck.xlsx", BASE + "/bp/import"],
               capture_output=True)
SRC = newest()
check("импорт создал БП-образец", SRC > before, f"(max {SRC}, было {before})")
if SRC <= before:
    raise SystemExit(1)
created.append(SRC)

# Образцу задаём то, что должно копироваться, и то, что копироваться НЕ должно.
post(f"/bp/{SRC}/section/lot", {"lot_cost": "7000000", "auction_step": "50000",
                                "capital_rate": "23", "payment_terms": "30 дней"})
post(f"/bp/{SRC}/section/header", {"manager": "Иванов", "division": "Коми"})
post(f"/bp/{SRC}/section/parties", {"seller_name": "ООО «Образец»"})
post(f"/bp/{SRC}/status", {"action": "to_review"})
c = sqlite3.connect(DB)
c.execute("UPDATE business_plans SET control_net_profit_bsp = 12345 WHERE id = ?",
          (SRC,))
c.commit()
c.close()
src = db("SELECT * FROM business_plans WHERE id = ?", (SRC,))[0]

print("\n── Копирование по образцу ──")

code = post("/bp/copy", {"source": str(SRC), "copy_costs": "1", "copy_risks": "1"})
NEW = newest()
check("копия создана", code == "303" and NEW > SRC, f"(код {code}, id {NEW})")
created.append(NEW)
new = db("SELECT * FROM business_plans WHERE id = ?", (NEW,))[0]

for field in ("manager", "division", "seller_name", "payment_terms",
              "capital_rate", "vat_rate", "tax_rate", "contamination_pct"):
    check(f"перенесено: {field}", new[field] == src[field],
          f"(образец {src[field]!r}, копия {new[field]!r})")
check("параметры сделки берутся из одного списка",
      "capital_rate" in COPY_BP_FIELDS and "lot_cost" not in COPY_BP_FIELDS)

for field, why in [("lot_cost", "цена именно того лота"),
                   ("auction_step", "шаг того аукциона"),
                   ("control_net_profit_bsp", "контрольная сумма книги образца"),
                   ("approved_date", "дата согласования образца")]:
    check(f"НЕ перенесено: {field} ({why})", not new[field],
          f"(копия {new[field]!r})")
check("копия начинается черновиком", new["status"] == "Черновик",
      f"({new['status']})")
check("номер у копии свой", new["bp_number"] != src["bp_number"])
check("копия не в архиве", not new["archived"])


def count(table, bp_id):
    return db(f"SELECT COUNT(*) n FROM {table} WHERE bp_id = ?", (bp_id,))[0]["n"]


check("статьи затрат перенесены",
      count("bp_costs", NEW) == count("bp_costs", SRC),
      f"({count('bp_costs', NEW)} против {count('bp_costs', SRC)})")
check("суммы статей совпадают",
      abs((db("SELECT COALESCE(SUM(amount),0) s FROM bp_costs WHERE bp_id = ?",
              (NEW,))[0]["s"])
          - (db("SELECT COALESCE(SUM(amount),0) s FROM bp_costs WHERE bp_id = ?",
                (SRC,))[0]["s"])) < 0.01)
check("статьи не удвоились (пустой БП завёл свои)",
      count("bp_costs", NEW) == count("bp_costs", SRC))
check("риски перенесены", count("bp_risks", NEW) == count("bp_risks", SRC))
check("позиции НЕ перенесены без запроса", count("bp_items", NEW) == 0,
      f"({count('bp_items', NEW)})")
check("факт образца не перенесён", count("bp_fact", NEW) == 0)
check("версии образца не перенесены",
      count("bp_versions", NEW) <= 2, f"({count('bp_versions', NEW)})")

# Образец не должен измениться от того, что с него сняли копию.
after = db("SELECT * FROM business_plans WHERE id = ?", (SRC,))[0]
check("образец не изменился",
      all(after[k] == src[k] for k in src.keys() if k not in ("updated_at", "version")))

# Копия с позициями: план продажи должен уехать на НОВЫЕ позиции.
post("/bp/copy", {"source": str(SRC), "copy_items": "1", "copy_route": "1"})
NEW2 = newest()
created.append(NEW2)
check("копия с позициями: позиции перенесены",
      count("bp_items", NEW2) == count("bp_items", SRC),
      f"({count('bp_items', NEW2)} против {count('bp_items', SRC)})")
check("копия с позициями: узлы маршрута перенесены",
      count("bp_nodes", NEW2) == count("bp_nodes", SRC),
      f"({count('bp_nodes', NEW2)} против {count('bp_nodes', SRC)})")
alien = db("SELECT COUNT(*) n FROM bp_edges e WHERE e.bp_id = ? AND "
           "(e.from_node NOT IN (SELECT id FROM bp_nodes WHERE bp_id = ?) OR "
           " e.to_node NOT IN (SELECT id FROM bp_nodes WHERE bp_id = ?))",
           (NEW2, NEW2, NEW2))[0]["n"]
check("плечи маршрута ссылаются на свои узлы", alien == 0, f"({alien} чужих)")
alien_sales = db("SELECT COUNT(*) n FROM bp_item_sales s WHERE s.bp_id = ? AND "
                 "s.item_id NOT IN (SELECT id FROM bp_items WHERE bp_id = ?)",
                 (NEW2, NEW2))[0]["n"]
check("план продажи ссылается на свои позиции", alien_sales == 0,
      f"({alien_sales} чужих)")
check("копия считается без ошибок", get(f"/bp/{NEW2}/economics") != "")

check("несуществующий образец отклонён",
      post("/bp/copy", {"source": "999999"}) == "303" and newest() == NEW2)
check("образец не выбран — БП не создан",
      post("/bp/copy", {"source": ""}) == "303" and newest() == NEW2)

print("\n── Поиск, фильтры, порядок ──")

number = src["bp_number"]
page = get("/", q=number)
check("поиск по номеру находит образец", number in page)
check("поиск по номеру не тащит остальные", page.count('/deal">') <= 3,
      f"({page.count(chr(39) + '/deal')} ссылок)")
check("поиск по продавцу работает (кириллица)",
      number in get("/", q="Образец"))
check("поиск по менеджеру работает", number in get("/", q="Иванов"))
check("бессмысленный запрос даёт пустой список",
      "ничего не найдено" in get("/", q="zzzzнетакого"))
own_status = db("SELECT status FROM business_plans WHERE id = ?",
                (SRC,))[0]["status"]
by_status = get("/", status=own_status)
check(f"фильтр по статусу («{own_status}») находит образец", number in by_status)
others = {r["status"] for r in db("SELECT DISTINCT status FROM business_plans")
          if r["status"] != own_status}
check("фильтр по статусу не показывает БП других статусов",
      all(f'status {st}">{st}' not in by_status for st in others),
      f"(другие статусы в базе: {sorted(others)})")
check("фильтр по дивизиону отбирает", number in get("/", division="Коми"))
check("фильтры складываются",
      number in get("/", division="Коми", status=own_status))
check("несовпадающая пара фильтров даёт пустой список",
      "ничего не найдено" in get("/", division="Коми", q="zzzzнетакого"))
for sort in ("new", "old", "number", "updated", "lot_cost", "items"):
    check(f"порядок «{sort}» отдаёт страницу",
          "Реестр бизнес-планов" in get("/", sort=sort))
check("неизвестный порядок не роняет реестр",
      "Реестр бизнес-планов" in get("/", sort="нетакого"))

# Пометка «не считается» — по самим данным: снимки версий с чистой прибылью
# есть далеко не у всех БП, и судить по ним значило бы пометить пробой почти
# весь реестр.
page = get("/", archived="all")
zero = db("SELECT bp.bp_number FROM business_plans bp WHERE "
          "(SELECT COUNT(*) FROM bp_items i WHERE i.bp_id = bp.id) = 0 "
          "OR (SELECT COUNT(*) FROM bp_items i WHERE i.bp_id = bp.id "
          "    AND i.sale_price > 0) = 0 "
          "OR bp.lot_cost IS NULL OR bp.lot_cost = 0")
full = db("SELECT bp.bp_number FROM business_plans bp WHERE "
          "bp.lot_cost > 0 AND (SELECT COUNT(*) FROM bp_items i "
          "WHERE i.bp_id = bp.id AND i.sale_price > 0) > 0")
marks = page.count(">не считается<")
check("пометка «не считается» стоит ровно у БП без цен или стоимости лота",
      marks == len(zero), f"({marks} пометок против {len(zero)} таких БП)")
check("у посчитанных БП пометки нет", len(full) > 0 and marks < len(zero) + 1,
      f"(считаемых БП {len(full)})")

print("\n── Архив ──")

code = post(f"/bp/{SRC}/archive", {"action": "archive"})
check("в архив принято", code == "303", f"(код {code})")
check("признак архива поставлен",
      db("SELECT archived FROM business_plans WHERE id = ?", (SRC,))[0]["archived"] == 1)
# Номер ищем как ссылку на карточку: простое вхождение ловило бы «по
# образцу БП-XXXX» у копии — это происхождение, а не строка реестра.
check("архивный БП не показывается в реестре",
      f">{number}</a>" not in get("/"))
check("архивный БП виден фильтром «только архив»",
      number in get("/", archived="only"))
check("архивный БП виден фильтром «вместе с рабочими»",
      number in get("/", archived="all"))
check("данные архивного БП на месте",
      count("bp_costs", SRC) > 0 and db(
          "SELECT lot_cost FROM business_plans WHERE id = ?",
          (SRC,))[0]["lot_cost"] == 7000000.0)
check("карточка архивного БП открывается",
      "Состав лота" in get(f"/bp/{SRC}/lot"))
portfolio = get("/portfolio")
check("архивный БП не идёт в портфель лотов",
      f">{number}</a>" not in portfolio)
post(f"/bp/{SRC}/archive", {"action": "restore"})
check("возврат из архива",
      db("SELECT archived FROM business_plans WHERE id = ?",
         (SRC,))[0]["archived"] == 0)
check("после возврата БП снова в реестре", number in get("/"))
check("ничего не удалено", db("SELECT COUNT(*) n FROM business_plans "
                             "WHERE id = ?", (SRC,))[0]["n"] == 1)

c = sqlite3.connect(DB)
c.execute("PRAGMA foreign_keys=ON")
for bp_id in created:
    c.execute("DELETE FROM business_plans WHERE id = ?", (bp_id,))
c.execute("DELETE FROM user_sessions WHERE user_id IN "
          "(SELECT id FROM users WHERE login = ?)", (login,))
c.execute("DELETE FROM users WHERE login = ?", (login,))
c.commit()
c.close()

print(f"\nИТОГО: {ok} ok, {fail} fail   "
      f"(временные БП {', '.join(str(i) for i in created)} удалены)")
raise SystemExit(1 if fail else 0)
