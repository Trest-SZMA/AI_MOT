"""Расстояния по дорогам: выбор геосервиса и общий кэш.

Два источника:

* **osm** (по умолчанию, бесплатный) — геокодер Nominatim и маршрутизатор
  OSRM на данных OpenStreetMap. Ключ не нужен, ограничения по длине
  маршрута нет.
* **2gis** — платный, через `app/dgis.py`. Демо-ключ 2ГИС считает маршруты
  не длиннее 50 км, а плечи в сделках бывают 130–245 км, поэтому по
  умолчанию он не выбран.

Провайдер задаётся переменной `BP_GEO_PROVIDER` (`osm` или `2gis`).

Общий кэш для обоих: координаты пункта — в `shipping_points.lat/lon`,
готовое плечо — в `shipping_routes`. Публичные серверы OSM живут на
пожертвованиях и просят не создавать нагрузку, а у 2ГИС оплачивается
каждый расчёт — повторный запрос по тому же плечу уходить не должен.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import dgis

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
TIMEOUT = 20

# Nominatim просит представляться и не чаще одного запроса в секунду.
# Только латиница: заголовки HTTP кодируются latin-1, и кириллица в
# User-Agent роняет запрос ещё до отправки (поймано на живой проверке).
USER_AGENT = "MetOptTorg-BP/1.0 (internal business-plan service)"
MIN_INTERVAL = 1.1

_last_call = 0.0
_lock = threading.Lock()


class GeoError(Exception):
    """Понятная человеку причина, почему расстояние не получено."""


def provider() -> str:
    """Выбранный геосервис. По умолчанию бесплатный OSM."""
    name = (os.environ.get("BP_GEO_PROVIDER") or "osm").strip().lower()
    return name if name in ("osm", "2gis") else "osm"


def provider_label() -> str:
    return "2ГИС" if provider() == "2gis" else "OpenStreetMap"


def enabled() -> bool:
    """Можно ли считать расстояния. Для OSM ключ не нужен — всегда да."""
    return dgis.enabled() if provider() == "2gis" else True


def _throttle() -> None:
    """Не чаще одного запроса в секунду — условие публичного Nominatim."""
    global _last_call
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _fetch(url: str) -> object:
    _throttle()
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GeoError(f"Сервис карт ответил {exc.code}. "
                       "Попробуйте позже или внесите километраж вручную.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GeoError("Нет связи с сервисом карт: проверьте выход "
                       "в интернет с сервера.") from exc
    except json.JSONDecodeError as exc:
        raise GeoError("Сервис карт вернул не JSON.") from exc


# ── OpenStreetMap ───────────────────────────────────────────────────

def _try_geocode(address: str) -> tuple[float, float] | None:
    query = urllib.parse.urlencode({"q": address, "format": "jsonv2",
                                    "limit": 1, "countrycodes": "ru"})
    data = _fetch(f"{NOMINATIM_URL}?{query}")
    if isinstance(data, list) and data:
        first = data[0]
        if first.get("lat") and first.get("lon"):
            return float(first["lat"]), float(first["lon"])
    return None


def osm_geocode_detail(address: str) -> dict:
    """Координаты адреса и строка, по которой они нашлись.

    Пункты отгрузки записаны как «Самарская обл., Кошкинский р-н, с. Кошки,
    ул. Речная, 13, трубная база» — целиком такую строку Nominatim не
    находит: «трубная база» и номер дома в OSM отсутствуют. Отбрасываем
    хвост по запятой, пока место не найдётся. Точность при этом падает до
    населённого пункта, поэтому строку совпадения возвращаем наружу: без
    неё экономист не поймёт, откуда взялись 17 км там, где в перечне 11 —
    это расстояние до центра посёлка, а не до конкретной базы."""
    address = " ".join((address or "").split())
    if not address:
        raise GeoError("Пустой адрес.")
    parts = [p.strip() for p in address.split(",") if p.strip()]
    tried = []
    for cut in range(0, min(len(parts) - 1, 3) + 1):
        candidate = ", ".join(parts[:len(parts) - cut]) if cut else address
        if candidate in tried:
            continue
        tried.append(candidate)
        found = _try_geocode(candidate)
        if found:
            return {"lat": found[0], "lon": found[1], "matched": candidate,
                    "exact": candidate == address}
    raise GeoError(f"OpenStreetMap не нашёл место «{address[:60]}» "
                   f"(пробовали {len(tried)} варианта). "
                   "Уточните адрес или внесите плечо вручную.")


def osm_geocode(address: str) -> tuple[float, float]:
    found = osm_geocode_detail(address)
    return found["lat"], found["lon"]


def osm_route(origin: tuple[float, float],
              destination: tuple[float, float]) -> dict:
    # OSRM принимает координаты в порядке «долгота,широта» — обратном
    # привычному: перепутанный порядок молча даёт маршрут не туда.
    coords = (f"{origin[1]},{origin[0]};"
              f"{destination[1]},{destination[0]}")
    data = _fetch(f"{OSRM_URL}/{coords}?overview=false")
    if not isinstance(data, dict) or data.get("code") != "Ok":
        message = (data or {}).get("message") if isinstance(data, dict) else ""
        raise GeoError("Маршрут не построен"
                       + (f": {message}" if message else
                          " (нет проезда либо координаты неточные)."))
    routes = data.get("routes") or []
    if not routes:
        raise GeoError("Маршрут не построен: сервис не вернул ни одного пути.")
    route = routes[0]
    return {"distance_km": round(float(route["distance"]) / 1000.0, 1),
            "duration_min": round(float(route["duration"]) / 60.0)}


# ── Общий интерфейс ─────────────────────────────────────────────────

def geocode_detail(address: str) -> dict:
    """Координаты и то, по какой строке они найдены (для честного отчёта)."""
    if provider() == "2gis":
        try:
            lat, lon = dgis.geocode(address)
        except dgis.DgisError as exc:
            raise GeoError(str(exc)) from exc
        return {"lat": lat, "lon": lon, "matched": address, "exact": True}
    return osm_geocode_detail(address)


def geocode(address: str) -> tuple[float, float]:
    found = geocode_detail(address)
    return found["lat"], found["lon"]


def road_distance(origin: tuple[float, float],
                  destination: tuple[float, float]) -> dict:
    if provider() == "2gis":
        try:
            return dgis.road_distance(origin, destination)
        except dgis.DgisError as exc:
            raise GeoError(str(exc)) from exc
    return osm_route(origin, destination)


def point_coords(conn, point) -> dict:
    """Координаты пункта отгрузки: из базы, иначе спрашиваем и запоминаем.

    Вместе с координатами хранится строка, по которой они нашлись
    (`coords_note`): без неё предупреждение «считали до центра посёлка»
    показывалось бы только в первый раз, а плечо переиспользуется годами."""
    if point["lat"] is not None and point["lon"] is not None:
        note = _column(point, "coords_note") or point["name"]
        return {"lat": float(point["lat"]), "lon": float(point["lon"]),
                "matched": note, "exact": note == point["name"],
                "cached": True}
    found = geocode_detail(point["name"])
    conn.execute("UPDATE shipping_points SET lat = ?, lon = ?, "
                 "coords_note = ?, updated_at = datetime('now') WHERE id = ?",
                 (found["lat"], found["lon"], found["matched"], point["id"]))
    found["cached"] = False
    return found


def _column(row, name: str):
    """Значение колонки, если она есть в выборке (старые базы без неё)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def distance_for_point(conn, point, to_name: str) -> dict:
    """Плечо от пункта отгрузки до названного места.

    `to_name` — база, цех или покупатель: обычный адрес, который тоже
    геокодируется. В ответе адреса и координаты, по которым считали, —
    экономисту нужно видеть, что сервис понял под названием."""
    if not enabled():
        raise GeoError("Ключ 2ГИС не задан (переменная BP_2GIS_KEY). "
                       "Уберите BP_GEO_PROVIDER, чтобы считать по "
                       "OpenStreetMap бесплатно.")
    origin = point_coords(conn, point)
    destination = geocode_detail(to_name)
    result = road_distance((origin["lat"], origin["lon"]),
                           (destination["lat"], destination["lon"]))
    # Что именно нашлось: если хвост адреса отброшен, расстояние считалось
    # до центра населённого пункта — экономист должен это видеть.
    rough = [m["matched"] for m in (origin, destination) if not m.get("exact")]
    result.update({"from_name": point["name"], "to_name": to_name,
                   "from_matched": origin["matched"],
                   "to_matched": destination["matched"],
                   "rough": rough, "provider": provider_label()})
    return result
