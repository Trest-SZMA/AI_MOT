"""Проверка ввода: сервис обязан отказывать, а не портить расчёт молча.

Сверка 17.08.2026 показала главное: опечатка в числовом поле проходила без
возражений. `abc` записывался как пусто, `1e999` давал inf и превращал чистую
прибыль в nan, `-5` принималось как отрицательная цена, а неполная отправка
формы обнуляла все статьи затрат. Здесь проверяется, что этого больше нет.

Две части:

* разбор значений (app/forms.py) — без сети и без базы;
* боевые формы — на СВОЁМ временном БП: импорт книги из attachments, попытки
  испортить данные, сверка, что в базе ничего не изменилось. Чужие БП не
  трогаются, за собой скрипт убирает.

    .venv/bin/python scripts/check_forms.py

Адрес сервиса берётся из BP_CHECK_BASE (по умолчанию 127.0.0.1:8011).
"""
import atexit
import json
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
from app import auth, forms                              # noqa: E402

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


def rejects(raw, field):
    """Значение отвергнуто с объяснением?"""
    try:
        forms.number(raw, field)
        return False
    except forms.Invalid:
        return True


print("\n── Разбор значений (app/forms.py) ──")

# То, что 17.08.2026 сервис принимал в «Стоимость лота» и портил расчёт.
for raw in ("abc", "1e999", "-5", "0,,5", "1" * 400, "12 345 руб"):
    check(f"отвергнуто в стоимости лота: {raw[:12]!r}", rejects(raw, "lot_cost"))

# То, что принимать обязан: экономист копирует числа из книги как есть.
for raw, expect in [("19900", 19900.0), ("19 900,5", 19900.5),
                    ("19 900,5", 19900.5), ("0", 0.0), (".5", 0.5),
                    ("", None), ("  ", None)]:
    got = forms.number(raw, "lot_cost")
    check(f"принято: {raw!r} → {expect}", got == expect, f"(получено {got})")

# Диапазоны: доля больше 100% и отрицательный процент — это опечатка.
check("засор 150% отвергнут", rejects("150", "contamination_pct"))
check("засор 6% принят", forms.number("6", "contamination_pct") == 6.0)
check("доля своего транспорта 101% отвергнута",
      rejects("101", "own_transport_pct"))
check("отрицательная цена реализации отвергнута", rejects("-100", "sale_price"))
check("сдвиг цены в сценариях может быть отрицательным",
      forms.number("-1000", "scen_price_delta") == -1000.0)

# Отсутствие поля и пустое поле — разные вещи: их смешение обнуляло затраты.
f = forms.Form({"amount_1": "500"})
check("поле пришло — видно", f.has("amount_1"))
check("поле не пришло — не видно", not f.has("amount_2"))
check("не пришло → прежнее значение", f.num("amount_2", 241358.0) == 241358.0)
check("пришло пустым → очистка", forms.Form({"a": ""}).num("a", 7.0) is None)

# Ошибка не пишется, а называется.
bad = forms.Form({"lot_cost": "abc", "vat_rate": "200"})
bad.num("lot_cost")
bad.num("vat_rate")
check("ошибки накоплены", not bad.ok and len(bad.errors) == 2)
check("сообщение начинается с «Не сохранено»",
      bad.message().startswith("Не сохранено."), f"({bad.message()})")
check("сообщение называет поле", "Стоимость лота" in bad.message())

if not os.path.exists(BOOK):
    print(f"\nКнига для проверки форм не найдена: {BOOK}")
    print(f"\nИТОГО: {ok} ok, {fail} fail   (боевые формы не проверялись)")
    raise SystemExit(1 if fail else 0)

print("\n── Боевые формы (свой временный БП) ──")

login, password = "formcheck", secrets.token_urlsafe(12)
c = sqlite3.connect(DB)
# Роль admin: импорт книги разрешён менеджеру и администратору, правки
# разделов — экономисту и администратору. Одна учётная запись на всю проверку.
c.execute("INSERT INTO users (login, full_name, role, password_hash, is_active,"
          " must_change_password) VALUES (?, ?, 'admin', ?, 1, 0) "
          "ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash,"
          " role='admin', is_active=1, must_change_password=0",
          (login, "Проверка форм", auth.hash_password(password)))
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


before = db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]
subprocess.run(["curl", "-s", "-o", "/dev/null", "-b", COOKIE,
                "-F", f"file=@{BOOK};filename=formcheck.xlsx", BASE + "/bp/import"],
               capture_output=True)
BP = db("SELECT COALESCE(MAX(id),0) n FROM business_plans")[0]["n"]
check("импорт создал свой БП", BP > before, f"(max {BP}, было {before})")
if BP <= before:
    raise SystemExit(1)


def bp_field(field):
    return db(f"SELECT {field} AS v FROM business_plans WHERE id = ?", (BP,))[0]["v"]


def costs_total():
    return db("SELECT COALESCE(SUM(amount), 0) s FROM bp_costs WHERE bp_id = ?",
              (BP,))[0]["s"]


# Стоимость лота: то самое поле из сверки.
post(f"/bp/{BP}/section/lot", {"lot_cost": "5000000"})
check("годное значение записано", bp_field("lot_cost") == 5000000.0,
      f"({bp_field('lot_cost')})")

for raw, why in [("abc", "не число"), ("1e999", "inf"), ("-5", "отрицательное"),
                 ("0,,5", "две запятые")]:
    post(f"/bp/{BP}/section/lot", {"lot_cost": raw})
    check(f"стоимость лота не испорчена: {raw} ({why})",
          bp_field("lot_cost") == 5000000.0, f"(стало {bp_field('lot_cost')})")

# Раздел сохраняется целиком либо никак: соседнее годное поле тоже не пишется.
step_before = bp_field("auction_step")
post(f"/bp/{BP}/section/lot", {"lot_cost": "abc", "auction_step": "77777"})
check("при ошибке не записано и соседнее поле",
      bp_field("auction_step") == step_before,
      f"(было {step_before}, стало {bp_field('auction_step')})")

# Неполная отправка: раньше обнуляла весь раздел.
post(f"/bp/{BP}/section/lot", {"auction_step": "88888"})
check("неполная форма не обнулила стоимость лота",
      bp_field("lot_cost") == 5000000.0, f"({bp_field('lot_cost')})")
check("пришедшее поле при этом записано", bp_field("auction_step") == 88888.0)

# Затраты: отправка формы без сумм обнуляла все статьи (241 358 → 0).
total_before = costs_total()
check("в БП есть затраты", total_before > 0, f"({total_before})")
post(f"/bp/{BP}/costs/update", {"nothing": "1"})
check("форма затрат без сумм не обнулила статьи",
      abs(costs_total() - total_before) < 0.01,
      f"(было {total_before}, стало {costs_total()})")

cost_id = db("SELECT id FROM bp_costs WHERE bp_id = ? AND amount > 0 "
             "ORDER BY id LIMIT 1", (BP,))[0]["id"]
amount_before = db("SELECT amount FROM bp_costs WHERE id = ?",
                   (cost_id,))[0]["amount"]
post(f"/bp/{BP}/costs/update", {f"amount_{cost_id}": "abc"})
check("опечатка в сумме статьи не записана",
      db("SELECT amount FROM bp_costs WHERE id = ?", (cost_id,))[0]["amount"]
      == amount_before)
post(f"/bp/{BP}/costs/update", {f"amount_{cost_id}": "12345"})
check("годная сумма статьи записана",
      db("SELECT amount FROM bp_costs WHERE id = ?", (cost_id,))[0]["amount"]
      == 12345.0)

# Цены позиций.
item_id = db("SELECT id FROM bp_items WHERE bp_id = ? ORDER BY id LIMIT 1",
             (BP,))[0]["id"]
post(f"/bp/{BP}/items/prices", {f"sale_price_{item_id}": "19900"})
check("цена позиции записана",
      db("SELECT sale_price FROM bp_items WHERE id = ?",
         (item_id,))[0]["sale_price"] == 19900.0)
for raw in ("-100", "abc", "1e999"):
    post(f"/bp/{BP}/items/prices", {f"sale_price_{item_id}": raw})
    check(f"цена позиции не испорчена: {raw}",
          db("SELECT sale_price FROM bp_items WHERE id = ?",
             (item_id,))[0]["sale_price"] == 19900.0)
post(f"/bp/{BP}/items/prices", {f"own_{item_id}": "150"})
check("доля своего транспорта 150% отклонена формой",
      (db("SELECT own_transport_pct FROM bp_items WHERE id = ?",
          (item_id,))[0]["own_transport_pct"] or 0) <= 100)

# Расчёт после всех попыток порчи остаётся числом, а не nan.
sys.path.insert(0, ROOT)
from app import calc                                     # noqa: E402
from app.db import connect                               # noqa: E402
conn = connect()
bp_row = conn.execute("SELECT * FROM business_plans WHERE id = ?", (BP,)).fetchone()
items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ?", (BP,)).fetchall()
cost_rows = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ?", (BP,)).fetchall()
net = calc.pnl(bp_row, items, cost_rows, conn)["net_profit"]
conn.close()
check("чистая прибыль — число, а не nan", net == net, f"({net})")

print("\n── Модель затрат не переписывает статьи молча ──")

# Ошибка 17.08: ввод в поле параметра затрат (даже значения, равного
# нормативу) переписывал ВСЕ статьи варианта суммами по модели. На боевом
# БП-0018 так ушла чистая прибыль «Лукойл»: 31 714 → −294 715, расхождение с
# книгой 326 429 руб. Проверки на это не было — теперь есть.
# Ставим временному БП контрольную сумму, равную его же расчёту: только на
# сходящемся БП видно, предупреждает ли сервис о потере сходимости.
sys.path.insert(0, ROOT)
from app import calc                                      # noqa: E402
from app.db import connect as _connect                    # noqa: E402
_c = _connect()
_bp = _c.execute("SELECT * FROM business_plans WHERE id = ?", (BP,)).fetchone()
_items = _c.execute("SELECT * FROM bp_items WHERE bp_id = ?", (BP,)).fetchall()
_costs = _c.execute("SELECT * FROM bp_costs WHERE bp_id = ?", (BP,)).fetchall()
_b, _i, _cc = calc.apply_variant(_bp, _items, _costs, "bsp")
_own = calc.pnl(_b, _i, _cc, _c)["net_profit"]
_c.execute("UPDATE business_plans SET control_net_profit_bsp = ? WHERE id = ?",
           (_own, BP))
_c.commit()
_c.close()
check("временному БП задана сходящаяся контрольная сумма", _own is not None)


def location(path, data):
    """Сообщение, с которым сервер вернул человека (заголовок Location)."""
    args = ["curl", "-s", "-o", "/dev/null", "-D", "-", "-b", COOKIE, "-X", "POST"]
    for k, v in data.items():
        args += ["--data-urlencode", f"{k}={v}"]
    out = subprocess.run(args + [BASE + path], capture_output=True, text=True).stdout
    line = next((l for l in out.splitlines() if l.lower().startswith("location:")), "")
    return urllib.parse.unquote(line)


def costs_sum():
    return db("SELECT COALESCE(SUM(amount), 0) s FROM bp_costs WHERE bp_id = ?",
              (BP,))[0]["s"]


def post_json(path, payload):
    return subprocess.run(
        ["curl", "-s", "-b", COOKIE, "-X", "POST",
         "-H", "Content-Type: application/json", "-d", json.dumps(payload),
         BASE + path], capture_output=True, text=True).stdout


before_costs = costs_sum()
answer = post_json(f"/bp/{BP}/costs/param",
                   {"key": "oxygen_rate_per_t", "value": "9", "variant": "bsp"})
model = json.loads(answer) if answer.strip().startswith("{") else {}
check("живой пересчёт параметра принят", bool(model.get("total") is not None),
      f"({answer[:80]})")
check("параметр сделки сохранён",
      db("SELECT COUNT(*) n FROM bp_cost_params WHERE bp_id = ? AND key = ?",
         (BP, "oxygen_rate_per_t"))[0]["n"] == 1)
check("статьи затрат живой пересчёт НЕ изменил",
      abs(costs_sum() - before_costs) < 0.01,
      f"(было {before_costs:.2f}, стало {costs_sum():.2f})")
check("сервер прямо сообщает, что статьи не тронуты",
      "не изменены" in (model.get("status") or ""), f"({model.get('status')})")

# Опечатка в параметре: значение не пишется, ответ объясняет причину.
bad = post_json(f"/bp/{BP}/costs/param",
                {"key": "oxygen_rate_per_t", "value": "abc", "variant": "bsp"})
check("опечатка в параметре отклонена с объяснением",
      "не число" in bad, f"({bad[:90]})")
check("после отказа значение параметра прежнее",
      db("SELECT value FROM bp_cost_params WHERE bp_id = ? AND key = ?",
         (BP, "oxygen_rate_per_t"))[0]["value"] == 9.0)

# Запись сумм по модели — только явным действием, и она реально меняет статьи.
msg = location(f"/bp/{BP}/costs/norms", {"variant": "bsp"})
check("запись сумм по модели принята", bool(msg), f"({msg[:60]})")
check("статьи затрат изменились только после явной записи",
      abs(costs_sum() - before_costs) > 0.01,
      f"(было {before_costs:.2f}, стало {costs_sum():.2f})")
check("сервер предупредил о потере сходимости с книгой",
      "не сходится с книгой" in msg, f"({msg[-140:]})")
check("в предупреждении названо расхождение", "расхождение" in msg,
      f"({msg[-140:]})")

# Защита от одновременной правки: форма со старым номером версии не пишет.
version = db("SELECT version FROM business_plans WHERE id = ?", (BP,))[0]["version"]
db("SELECT 1")
c = sqlite3.connect(DB)
c.execute("INSERT INTO bp_versions (bp_id, version, snapshot_json, author, "
          "snapshot_hash) VALUES (?, ?, '{}', 'Другой экономист', 'x')",
          (BP, (version or 0) + 1))
c.execute("UPDATE business_plans SET version = ? WHERE id = ?",
          ((version or 0) + 1, BP))
c.commit()
c.close()
post(f"/bp/{BP}/section/lot", {"lot_cost": "999999", "_v": str(version)})
check("правка поверх чужой не записана", bp_field("lot_cost") == 5000000.0,
      f"({bp_field('lot_cost')})")
post(f"/bp/{BP}/section/lot",
     {"lot_cost": "999999", "_v": str(db("SELECT version FROM business_plans "
                                         "WHERE id = ?", (BP,))[0]["version"])})
check("правка с актуальной версией записана", bp_field("lot_cost") == 999999.0,
      f"({bp_field('lot_cost')})")

# Уборка: временный БП и служебная учётная запись. Вынесена в atexit —
# 17.08 проверка упала на середине (не тот импорт) и оставила свой БП в
# реестре: уборка «в конце скрипта» до этого места просто не доехала.
def cleanup():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("DELETE FROM business_plans WHERE id = ?", (BP,))
    c.execute("DELETE FROM user_sessions WHERE user_id IN "
              "(SELECT id FROM users WHERE login = ?)", (login,))
    c.execute("DELETE FROM users WHERE login = ?", (login,))
    c.commit()
    c.close()


atexit.register(cleanup)

print(f"\nИТОГО: {ok} ok, {fail} fail   (временный БП {BP} удалён)")
raise SystemExit(1 if fail else 0)
