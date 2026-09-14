"""Проверка разбора ответов 2ГИС без обращения к настоящему API.

Ключа 2ГИС пока нет, но логика (разбор JSON, кэш координат, отказы) должна
быть проверена заранее: сетевые вызовы подменяются, дальше работает
настоящий код `app/dgis.py`.

Запуск: .venv/bin/python scripts/check_dgis.py     Ожидаемо: 12 ok, 0 fail
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["BP_2GIS_KEY"] = "проверка"          # включаем модуль
from app import dgis                                            # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {detail}")


# ── Подмена сетевых вызовов ──────────────────────────────────────────
CALLS = {"geocode": 0, "matrix": 0}
GEO = {
    "Волгоградская обл., г. Котово": {"lat": 50.318, "lon": 44.798},
    "Чермет Волжский": {"lat": 48.786, "lon": 44.775},
    "Несуществующее место": None,
}


def fake_get(url, params):
    CALLS["geocode"] += 1
    if params["key"] == "плохой":
        # Так отвечает настоящий геокодер на неверный ключ: HTTP 200,
        # а ошибка внутри тела (проверено на сервере 14.08.2026).
        return {"meta": {"code": 403, "error": {"type": "forbidden",
                                                "message": "Authorization error, incorrect key."}}}
    point = GEO.get(params["q"])
    if point is None:
        return {"result": {"items": []}}
    return {"result": {"items": [{"point": point}]}}


def fake_post(url, params, body):
    CALLS["matrix"] += 1
    if body["points"][1]["lat"] == 0:            # маршрут не строится
        return {"routes": [{"status": "FAIL", "source_id": 0, "target_id": 1}]}
    if body["points"][1]["lat"] == 1:            # ответ демо-ключа на длинное плечо
        return {"type": "error", "error_message":
                "excessive distance between points for demo-keys, max (km): 50"}
    return {"routes": [{"status": "OK", "source_id": 0, "target_id": 1,
                        "distance": 245_300, "duration": 12_600}]}


dgis._get_json = fake_get
dgis._post_json = fake_post

check("модуль включается ключом", dgis.enabled())

lat, lon = dgis.geocode("Волгоградская обл., г. Котово")
check("геокодер: координаты разобраны", (lat, lon) == (50.318, 44.798),
      f"({lat}, {lon})")
try:
    dgis.geocode("Несуществующее место")
    check("геокодер: пустой ответ = отказ", False, "(исключения не было)")
except dgis.DgisError as exc:
    check("геокодер: пустой ответ = отказ", "не нашёл место" in str(exc))
try:
    dgis.geocode("   ")
    check("геокодер: пустой адрес = отказ", False, "(исключения не было)")
except dgis.DgisError:
    check("геокодер: пустой адрес = отказ", True)

# Ошибка ключа приходит с HTTP 200 и кодом внутри тела — сообщение должно
# говорить про ключ, а не про «место не найдено».
os.environ["BP_2GIS_KEY"] = "плохой"
try:
    dgis.geocode("Волгоградская обл., г. Котово")
    check("геокодер: неверный ключ распознан", False, "(исключения не было)")
except dgis.DgisError as exc:
    check("геокодер: неверный ключ распознан", "отклонил ключ" in str(exc),
          f"({exc})")
os.environ["BP_2GIS_KEY"] = "проверка"

result = dgis.road_distance((50.318, 44.798), (48.786, 44.775))
check("матрица: метры переведены в км", result["distance_km"] == 245.3,
      f"({result['distance_km']})")
check("матрица: секунды переведены в минуты", result["duration_min"] == 210,
      f"({result['duration_min']})")
try:
    dgis.road_distance((50.318, 44.798), (0, 0))
    check("матрица: статус FAIL = отказ", False, "(исключения не было)")
except dgis.DgisError as exc:
    check("матрица: статус FAIL = отказ", "не построил маршрут" in str(exc))
# Лимит демо-ключа (50 км) приходит с HTTP 200 и телом-ошибкой — сообщение
# должно называть причину, а не «маршрут не построен».
try:
    dgis.road_distance((50.318, 44.798), (1, 44.8))
    check("матрица: лимит демо-ключа распознан", False, "(исключения не было)")
except dgis.DgisError as exc:
    check("матрица: лимит демо-ключа распознан", "Демо-ключ" in str(exc),
          f"({exc})")

# ── Кэш координат в базе ─────────────────────────────────────────────
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE shipping_points (id INTEGER PRIMARY KEY, name TEXT, "
             "lat REAL, lon REAL, updated_at TEXT)")
conn.execute("INSERT INTO shipping_points (id, name) VALUES "
             "(1, 'Волгоградская обл., г. Котово')")
point = conn.execute("SELECT * FROM shipping_points WHERE id=1").fetchone()

CALLS["geocode"] = 0
coords = dgis.point_coords(conn, point)
check("кэш: первый раз идём в 2ГИС", CALLS["geocode"] == 1)
saved = conn.execute("SELECT lat, lon FROM shipping_points WHERE id=1").fetchone()
check("кэш: координаты сохранены", (saved["lat"], saved["lon"]) == coords)

point = conn.execute("SELECT * FROM shipping_points WHERE id=1").fetchone()
CALLS["geocode"] = 0
dgis.point_coords(conn, point)
check("кэш: второй раз в 2ГИС не идём", CALLS["geocode"] == 0)

full = dgis.distance_for_point(conn, point, "Чермет Волжский")
check("плечо целиком: километраж", full["distance_km"] == 245.3)
check("плечо целиком: адреса показаны",
      full["from_name"] == "Волгоградская обл., г. Котово"
      and full["to_name"] == "Чермет Волжский")

# ── Без ключа модуль молчит ──────────────────────────────────────────
os.environ["BP_2GIS_KEY"] = ""
check("без ключа модуль выключен", not dgis.enabled())
try:
    dgis.distance_for_point(conn, point, "Чермет Волжский")
    check("без ключа — понятный отказ", False, "(исключения не было)")
except dgis.DgisError as exc:
    check("без ключа — понятный отказ", "BP_2GIS_KEY" in str(exc))

print(f"\nИТОГО: {ok} ok, {fail} fail")
raise SystemExit(1 if fail else 0)
