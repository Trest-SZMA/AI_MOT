"""Проверка массовой правки позиций и постраничного вывода состава лота.

Сверка 17.08.2026: «в книге экономист протягивает формулу на 500 строк за
секунду, в сервисе этого нет», а вкладка «Лот» на 1950 позиций весила 9,8 МБ.
Здесь проверяется, что появившиеся взамен средства не портят данные:

* массовая правка меняет ТОЛЬКО выделенные строки и только заполненные поля;
* отмена возвращает прежние значения именно этих строк;
* страница отдаёт часть строк, но итоги считаются по всему лоту, а сохранение
  страницы не трогает позиции с других страниц.

Скрипт работает на СВОЁМ временном БП (импорт книги из attachments) и убирает
за собой. Чужие БП не трогаются.

    .venv/bin/python scripts/check_items_bulk.py

Адрес сервиса — BP_CHECK_BASE (по умолчанию 127.0.0.1:8011).
"""
import os
import re
import secrets
import sqlite3
import subprocess
import sys

BASE = os.environ.get("BP_CHECK_BASE", "http://127.0.0.1:8011")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "bp.db")
BOOK = os.path.join(ROOT, "attachments", "БП-0014-2026",
                    "1738_25.11.25_v0_Лукойл_ЗС_7919тн.xlsx")
sys.path.insert(0, ROOT)
from app import auth                                     # noqa: E402
from app.main import ITEMS_PER_PAGE                      # noqa: E402

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


login, password = "bulkcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active,"
          " must_change_password) VALUES (?, 'Проверка массовой правки', 'admin',"
          " ?, 1, 0) ON CONFLICT(login) DO UPDATE SET "
          "password_hash=excluded.password_hash, role='admin', is_active=1, "
          "must_change_password=0", (login, auth.hash_password(password)))
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


def get(path):
    return subprocess.run(["curl", "-s", "-b", COOKIE, BASE + path],
                          capture_output=True, text=True).stdout


before = db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]
subprocess.run(["curl", "-s", "-o", "/dev/null", "-b", COOKIE,
                "-F", f"file=@{BOOK};filename=bulkcheck.xlsx", BASE + "/bp/import"],
               capture_output=True)
BP = db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]
check("импорт создал свой БП", BP > before, f"(max {BP}, было {before})")
if BP <= before:
    raise SystemExit(1)

items = db("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id", (BP,))
check("позиции загружены", len(items) >= 4, f"({len(items)})")
picked = [it["id"] for it in items[:2]]
untouched = [it["id"] for it in items[2:]]


def prices(ids):
    marks = ",".join("?" for _ in ids)
    return {r["id"]: r["sale_price"] for r in db(
        f"SELECT id, sale_price FROM bp_items WHERE id IN ({marks})", tuple(ids))}


print("\n── Массовая правка ──")

before_picked = prices(picked)
before_rest = prices(untouched)
code = post(f"/bp/{BP}/items/bulk",
            {"item_ids": ",".join(str(i) for i in picked), "sale_price": "21500",
             "buyer": "Чермет-Волжский"})
check("массовая правка принята", code == "303", f"(код {code})")
check("цена задана выделенным",
      all(v == 21500.0 for v in prices(picked).values()), f"({prices(picked)})")
check("покупатель задан выделенным",
      all(r["buyer"] == "Чермет-Волжский" for r in db(
          "SELECT buyer FROM bp_items WHERE id IN (%s)"
          % ",".join(str(i) for i in picked))))
check("невыделенные строки не тронуты", prices(untouched) == before_rest,
      f"(было {before_rest}, стало {prices(untouched)})")

# Пустое поле не меняет ничего: «задать цену» не должно стирать покупателя.
post(f"/bp/{BP}/items/bulk",
     {"item_ids": ",".join(str(i) for i in picked), "sale_price": "22000",
      "buyer": ""})
check("пустое поле не стёрло покупателя",
      all(r["buyer"] == "Чермет-Волжский" for r in db(
          "SELECT buyer FROM bp_items WHERE id IN (%s)"
          % ",".join(str(i) for i in picked))))
check("цена обновилась", all(v == 22000.0 for v in prices(picked).values()))

# Опечатка в массовой правке не пишется никуда: сотня строк разом — это
# ровно тот случай, где тихая порча дороже всего.
post(f"/bp/{BP}/items/bulk",
     {"item_ids": ",".join(str(i) for i in picked), "sale_price": "-100"})
check("отрицательная цена не записана массово",
      all(v == 22000.0 for v in prices(picked).values()), f"({prices(picked)})")
post(f"/bp/{BP}/items/bulk",
     {"item_ids": ",".join(str(i) for i in picked), "sale_price": "abc"})
check("нечисловая цена не записана массово",
      all(v == 22000.0 for v in prices(picked).values()))

# Без выделения писать нечего.
post(f"/bp/{BP}/items/bulk", {"item_ids": "", "sale_price": "1"})
check("без выделенных строк ничего не записано",
      all(v == 22000.0 for v in prices(picked).values()))

# Чужие позиции по идентификатору не правятся.
other = db("SELECT id, sale_price FROM bp_items WHERE bp_id <> ? LIMIT 1", (BP,))
if other:
    alien, alien_price = other[0]["id"], other[0]["sale_price"]
    post(f"/bp/{BP}/items/bulk", {"item_ids": str(alien), "sale_price": "77777"})
    check("позиция чужого БП не тронута",
          db("SELECT sale_price FROM bp_items WHERE id = ?",
             (alien,))[0]["sale_price"] == alien_price)

print("\n── Отмена массового действия ──")

code = post(f"/bp/{BP}/items/bulk/undo", {})
check("отмена принята", code == "303", f"(код {code})")
check("вернулась цена до последней массовой правки",
      all(v == 21500.0 for v in prices(picked).values()), f"({prices(picked)})")
post(f"/bp/{BP}/items/bulk/undo", {})
check("вторая отмена вернула исходные цены", prices(picked) == before_picked,
      f"(было {before_picked}, стало {prices(picked)})")
check("отменять больше нечего",
      db("SELECT COUNT(*) n FROM bp_bulk_undo WHERE bp_id = ?", (BP,))[0]["n"] == 0)
post(f"/bp/{BP}/items/bulk/undo", {})
check("лишняя отмена ничего не сломала", prices(picked) == before_picked)

print("\n── Постраничный вывод ──")

html = get(f"/bp/{BP}/lot")
shown = html.count('class="row-pick"')
check("страница отдаёт не больше положенного", shown <= ITEMS_PER_PAGE,
      f"({shown} строк)")
check("итоги по всему лоту, а не по странице", 'data-total="purchase_volume"' in html)

# Лот, который заведомо длиннее страницы: проверяем сами страницы.
big = db("SELECT bp_id, COUNT(*) n FROM bp_items GROUP BY bp_id "
         "HAVING n > ? ORDER BY n DESC LIMIT 1", (ITEMS_PER_PAGE,))
if big:
    big_id, total = big[0]["bp_id"], big[0]["n"]
    first = get(f"/bp/{big_id}/lot")
    second = get(f"/bp/{big_id}/lot?page=2")
    whole = get(f"/bp/{big_id}/lot?per=all")
    ids = lambda page: re.findall(r'class="row-pick" value="(\d+)"', page)  # noqa: E731
    check(f"большой лот ({total} поз.): страница ограничена",
          len(ids(first)) == ITEMS_PER_PAGE, f"({len(ids(first))})")
    check("вторая страница — другие строки",
          set(ids(first)).isdisjoint(ids(second)))
    check("«показать все» отдаёт весь лот", len(ids(whole)) == total,
          f"({len(ids(whole))} из {total})")
    check("страница легче полного вывода в разы",
          len(first) * 3 < len(whole),
          f"({len(first)/1024/1024:.2f} МБ против {len(whole)/1024/1024:.2f} МБ)")
    check("на странице есть ссылки на другие",
          'class="pager-num' in first)
else:
    print("  (лота длиннее страницы в базе нет — проверка страниц пропущена)")

# Сохранение страницы не трогает позиции с других страниц: это та же защита,
# что и от неполной отправки формы, но здесь она работает постоянно.
item = items[0]["id"]
rest_before = prices(untouched)
post(f"/bp/{BP}/items/prices", {f"sale_price_{item}": "18000"})
check("сохранение видимых строк записало цену",
      prices([item])[item] == 18000.0)
check("позиции вне страницы не обнулены", prices(untouched) == rest_before)

c = sqlite3.connect(DB)
c.execute("PRAGMA foreign_keys=ON")
c.execute("DELETE FROM business_plans WHERE id = ?", (BP,))
c.execute("DELETE FROM user_sessions WHERE user_id IN "
          "(SELECT id FROM users WHERE login = ?)", (login,))
c.execute("DELETE FROM users WHERE login = ?", (login,))
c.commit()
c.close()

print(f"\nИТОГО: {ok} ok, {fail} fail   (временный БП {BP} удалён)")
raise SystemExit(1 if fail else 0)
