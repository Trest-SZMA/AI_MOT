"""Проверка авторизации и рабочих процессов под ролями.

Требует поднятого сервиса на 8011. Ожидаемо: 34 ok, 0 fail.

Заводит служебные учётные записи `probe_*` с одноразовыми паролями, входит
под каждой ролью, импортирует книгу из attachments в СВОЙ временный БП,
прогоняет правки всех разделов и страницы, затем удаляет за собой и БП, и
учётные записи. Чужие БП не трогает.

В отличие от audit_processes.py здесь не сверяются суммы конкретной книги —
проверяется, что вход, роли, формы, версии и журнал работают. Полную сверку
расчёта даёт audit_processes.py (ему нужен файл scripts/1935.xlsx).
"""
import os, secrets, sqlite3, subprocess, sys

BASE = "http://127.0.0.1:8011"
ROOT = "/Users/macpavel/FASTBP/bp-service1"
DB = ROOT + "/bp.db"
BOOK = ROOT + "/attachments/БП-0014-2026/1738_25.11.25_v0_Лукойл_ЗС_7919тн.xlsx"
sys.path.insert(0, ROOT)
from app import auth

ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  OK   {name}")
    else: fail += 1; print(f"  FAIL {name} {detail}")

def db(q, args=()):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    r = c.execute(q, args).fetchall(); c.close(); return r

ROLES = ["manager", "economist", "logist", "director", "admin"]
COOKIE = {}
c = sqlite3.connect(DB)
creds = {}
for role in ROLES:
    login, password = f"probe_{role}", secrets.token_urlsafe(12)
    c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active,"
              " must_change_password) VALUES (?, ?, ?, ?, 1, 0) "
              "ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash,"
              " role=excluded.role, is_active=1, must_change_password=0",
              (login, f"Проба ({role})", role, auth.hash_password(password)))
    creds[role] = (login, password)
c.commit(); c.close()
for role, (login, password) in creds.items():
    out = subprocess.run(["curl","-s","-i","-X","POST","--data-urlencode",f"login={login}",
                          "--data-urlencode",f"password={password}", BASE+"/login"],
                         capture_output=True, text=True).stdout
    tok = [l.split("=",1)[1].split(";")[0] for l in out.splitlines()
           if l.lower().startswith("set-cookie: bp_session=")]
    check(f"вход под ролью {role}", bool(tok))
    COOKIE[role] = f"bp_session={tok[0]}" if tok else ""

def post(path, data, role="admin"):
    args = ["curl","-s","-o","/dev/null","-w","%{http_code}","-b",COOKIE[role],"-X","POST"]
    for k, v in data.items():
        args += ["--data-urlencode", f"{k}={v}"]
    return subprocess.run(args + [BASE+path], capture_output=True, text=True).stdout.strip()

def get(path, role="admin"):
    return subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","-b",
                           COOKIE[role], BASE+path], capture_output=True, text=True).stdout.strip()

before_max = db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]
subprocess.run(["curl","-s","-o","/dev/null","-b",COOKIE["manager"],
                "-F", f"file=@{BOOK};filename=probe.xlsx", BASE+"/bp/import"],
               capture_output=True)
BP = db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]
check("импорт книги создал БП", BP > before_max, f"(max {BP} было {before_max})")
if BP <= before_max:
    raise SystemExit(1)
items = db("SELECT * FROM bp_items WHERE bp_id=? ORDER BY id", (BP,))
check("импорт: позиции загружены", len(items) > 0, f"({len(items)})")
i0 = items[0]["id"]

for name, path, data, role in [
    ("шапка", f"/bp/{BP}/section/header", {"manager": "Проба"}, "manager"),
    ("стороны", f"/bp/{BP}/section/parties", {"buyer_name": "Чермет"}, "manager"),
    ("лот", f"/bp/{BP}/section/lot", {"auction_step": "100000"}, "economist"),
    ("цены позиций", f"/bp/{BP}/items/prices",
     {f"sale_price_{i0}": "19900", f"own_{i0}": "60"}, "economist"),
    ("реквизиты позиции", f"/bp/{BP}/items/requisites",
     {f"liquidity_{i0}": "неликвид"}, "economist"),
    ("автоподтягивание", f"/bp/{BP}/autofill", {}, "economist"),
    ("сопоставление 1С", f"/bp/{BP}/match/auto", {}, "manager"),
    ("граф маршрута", f"/bp/{BP}/route/build", {}, "logist"),
    ("статус: на проверку", f"/bp/{BP}/status", {"action": "На проверке"}, "manager"),
]:
    code = post(path, data, role)
    check(f"правка {name} ({role})", code == "303", f"(код {code})")

check("сохранилось: менеджер",
      db("SELECT manager FROM business_plans WHERE id=?", (BP,))[0]["manager"] == "Проба")
check("сохранилось: цена позиции",
      abs((db("SELECT sale_price FROM bp_items WHERE id=?", (i0,))[0]["sale_price"] or 0)
          - 19900) < 0.01)
check("автоверсия записана",
      db("SELECT COUNT(*) n FROM bp_versions WHERE bp_id=?", (BP,))[0]["n"] > 0)
check("автор версии — имя пользователя",
      any("Проба" in (r["author"] or "") for r in
          db("SELECT author FROM bp_versions WHERE bp_id=?", (BP,))))
check("журнал пишет логин",
      db("SELECT COUNT(*) n FROM audit_log WHERE bp_id=? AND user_name LIKE 'probe_%'",
         (BP,))[0]["n"] > 0)

for role in ROLES:
    check(f"карточка под ролью {role}", get(f"/bp/{BP}/deal", role) == "200")
for page in ["/", "/portfolio", "/references", f"/bp/{BP}/export",
             f"/bp/{BP}/schedule", f"/bp/{BP}/planfact", f"/bp/{BP}/route"]:
    check(f"страница {page}", get(page) == "200")
check("выгрузка формируется", get(f"/bp/{BP}/export") == "200")

# Уборка: временный БП и служебные учётные записи.
c = sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=ON")
c.execute("DELETE FROM business_plans WHERE id=?", (BP,))
c.execute("DELETE FROM user_sessions WHERE user_id IN "
          "(SELECT id FROM users WHERE login LIKE 'probe_%')")
c.execute("DELETE FROM users WHERE login LIKE 'probe_%'")
c.commit(); c.close()
print(f"\nИТОГО: {ok} ok, {fail} fail   (временный БП {BP} удалён)")
