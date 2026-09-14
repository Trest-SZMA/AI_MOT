"""Проверка выбора геосервиса и разбора ответов — без обращения к сети.

Сетевые вызовы подменяются, дальше работает настоящий код `app/geo.py`
и `app/dgis.py`. Живая проверка на реальных пунктах отгрузки делается на
сервере — там есть выход в интернет.

Запуск: .venv/bin/python scripts/check_geo.py     Ожидаемо: 22 ok, 0 fail
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dgis, geo                                        # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {detail}")


CALLS = {"osm_geocode": 0, "osm_route": 0}

NOMINATIM = {
    "Волгоградская обл., г. Котово, трубная база": [
        {"lat": "50.3222080", "lon": "44.8016430", "display_name": "Котово"}],
    "Чермет Волжский": [{"lat": "48.786", "lon": "44.775",
                         "display_name": "Волжский"}],
    "Несуществующее место": [],
}


def fake_fetch(url):
    if url.startswith(geo.NOMINATIM_URL):
        CALLS["osm_geocode"] += 1
        import urllib.parse
        query = urllib.parse.parse_qs(url.split("?", 1)[1])
        return NOMINATIM.get(query["q"][0], [])
    CALLS["osm_route"] += 1
    coords = url.rsplit("/", 1)[1].split("?")[0]
    if coords.startswith("0,0"):
        return {"code": "NoRoute", "message": "Impossible route"}
    return {"code": "Ok", "routes": [{"distance": 224059.6, "duration": 13253.5}]}


geo._fetch = fake_fetch
geo._throttle = lambda: None            # в проверке паузы не нужны

# ── Выбор провайдера ─────────────────────────────────────────────────
os.environ.pop("BP_GEO_PROVIDER", None)
check("по умолчанию OpenStreetMap", geo.provider() == "osm")
check("без ключа OSM всё равно работает", geo.enabled())
os.environ["BP_GEO_PROVIDER"] = "мусор"
check("неизвестный провайдер → OSM", geo.provider() == "osm")
os.environ["BP_GEO_PROVIDER"] = "2gis"
os.environ.pop("BP_2GIS_KEY", None)
check("2ГИС без ключа выключен", not geo.enabled())
os.environ["BP_GEO_PROVIDER"] = "osm"

# ── OpenStreetMap ────────────────────────────────────────────────────
lat, lon = geo.geocode("Волгоградская обл., г. Котово, трубная база")
check("Nominatim: координаты разобраны", (lat, lon) == (50.322208, 44.801643),
      f"({lat}, {lon})")
try:
    geo.geocode("Несуществующее место")
    check("Nominatim: пусто = отказ", False, "(исключения не было)")
except geo.GeoError as exc:
    check("Nominatim: пусто = отказ", "не нашёл место" in str(exc))

route = geo.road_distance((50.322208, 44.801643), (48.786, 44.775))
check("OSRM: метры переведены в км", route["distance_km"] == 224.1,
      f"({route['distance_km']})")
check("OSRM: секунды переведены в минуты", route["duration_min"] == 221,
      f"({route['duration_min']})")
try:
    geo.road_distance((0, 0), (48.786, 44.775))
    check("OSRM: нет маршрута = отказ", False, "(исключения не было)")
except geo.GeoError as exc:
    check("OSRM: нет маршрута = отказ", "не построен" in str(exc), f"({exc})")

# Порядок координат: OSRM ждёт «долгота,широта». Перепутанный порядок
# молча увёл бы маршрут в другую точку планеты.
CAPTURED = {}


def capture_fetch(url):
    if not url.startswith(geo.NOMINATIM_URL):
        CAPTURED["coords"] = url.rsplit("/", 1)[1].split("?")[0]
    return fake_fetch(url)


geo._fetch = capture_fetch
geo.road_distance((50.322208, 44.801643), (48.786, 44.775))
check("OSRM: порядок «долгота,широта»",
      CAPTURED["coords"] == "44.801643,50.322208;44.775,48.786",
      f"({CAPTURED.get('coords')})")
geo._fetch = fake_fetch

# ── Кэш координат ────────────────────────────────────────────────────
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE shipping_points (id INTEGER PRIMARY KEY, name TEXT, "
             "lat REAL, lon REAL, coords_note TEXT, updated_at TEXT)")
conn.execute("INSERT INTO shipping_points (id, name) VALUES "
             "(1, 'Волгоградская обл., г. Котово, трубная база')")
point = conn.execute("SELECT * FROM shipping_points WHERE id=1").fetchone()

CALLS["osm_geocode"] = 0
coords = geo.point_coords(conn, point)
check("кэш: первый раз идём в сеть", CALLS["osm_geocode"] == 1)
saved = conn.execute("SELECT lat, lon FROM shipping_points WHERE id=1").fetchone()
check("кэш: координаты сохранены",
      (saved["lat"], saved["lon"]) == (coords["lat"], coords["lon"]),
      f"({saved['lat']}, {saved['lon']} против {coords})")
check("кэш: первый раз помечен как не из кэша", not coords["cached"])
point = conn.execute("SELECT * FROM shipping_points WHERE id=1").fetchone()
CALLS["osm_geocode"] = 0
geo.point_coords(conn, point)
check("кэш: второй раз в сеть не идём", CALLS["osm_geocode"] == 0)

# Приблизительность должна пережить кэш: плечо переиспользуется годами,
# и предупреждение «считали до центра посёлка» нельзя терять.
conn.execute("UPDATE shipping_points SET lat=54.0, lon=50.0, "
             "coords_note='Самарская обл., с. Кошки' WHERE id=1")
rough_point = conn.execute("SELECT * FROM shipping_points WHERE id=1").fetchone()
cached = geo.point_coords(conn, rough_point)
check("кэш: приблизительность сохранена",
      cached["cached"] and not cached["exact"]
      and cached["matched"] == "Самарская обл., с. Кошки", f"({cached})")
conn.execute("UPDATE shipping_points SET coords_note=NULL WHERE id=1")

full = geo.distance_for_point(conn, point, "Чермет Волжский")
check("плечо целиком: километраж", full["distance_km"] == 224.1)
check("плечо целиком: источник назван", full["provider"] == "OpenStreetMap",
      f"({full['provider']})")

# Адрес с хвостом, которого нет в OSM: находим по населённому пункту и
# честно об этом сообщаем.
NOMINATIM["Самарская обл., с. Кошки, ул. Речная, 13, трубная база"] = []
NOMINATIM["Самарская обл., с. Кошки, ул. Речная"] = []
NOMINATIM["Самарская обл., с. Кошки"] = [{"lat": "54.0", "lon": "50.0"}]
detail = geo.osm_geocode_detail(
    "Самарская обл., с. Кошки, ул. Речная, 13, трубная база")
check("адрес с хвостом: нашли по населённому пункту",
      detail["matched"] == "Самарская обл., с. Кошки", f"({detail['matched']})")
check("адрес с хвостом: помечен как приблизительный", not detail["exact"])
check("точный адрес помечен как точный",
      geo.osm_geocode_detail("Чермет Волжский")["exact"])

# ── Переключение на 2ГИС ─────────────────────────────────────────────
os.environ["BP_GEO_PROVIDER"] = "2gis"
os.environ["BP_2GIS_KEY"] = "проверка"
dgis._get_json = lambda url, params: {
    "result": {"items": [{"point": {"lat": 50.318, "lon": 44.798}}]}}
dgis._post_json = lambda url, params, body: {
    "type": "error",
    "error_message": "excessive distance between points for demo-keys, max (km): 50"}
check("2ГИС выбирается ключом", geo.provider() == "2gis" and geo.enabled())
try:
    geo.road_distance((50.318, 44.798), (48.786, 44.775))
    check("2ГИС: лимит демо-ключа виден", False, "(исключения не было)")
except geo.GeoError as exc:
    check("2ГИС: лимит демо-ключа виден", "Демо-ключ" in str(exc), f"({exc})")
os.environ["BP_GEO_PROVIDER"] = "osm"

print(f"\nИТОГО: {ok} ok, {fail} fail")
raise SystemExit(1 if fail else 0)
