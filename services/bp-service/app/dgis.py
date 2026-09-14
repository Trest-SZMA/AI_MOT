"""Расстояния по дорогам из 2ГИС: адрес → координаты → километраж.

Работает только когда задан ключ (`BP_2GIS_KEY` в окружении). Без ключа
модуль молчит: в карточке остаются ссылка на 2ГИС и ручной ввод плеча —
сервис по-прежнему не выдумывает расстояния.

Два запроса на пару адресов:
  1. геокодер `catalog.api.2gis.com/3.0/items/geocode` — координаты места;
  2. `routing.api.2gis.com/get_dist_matrix` — расстояние и время по дорогам.

Оплачивается количество расчётов, поэтому и координаты, и километраж
кэшируются: координаты в `shipping_points.lat/lon`, расстояние — в
`shipping_routes`. Повторный запрос по тому же плечу в 2ГИС не уходит.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

GEOCODE_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
MATRIX_URL = "https://routing.api.2gis.com/get_dist_matrix"
TIMEOUT = 12                      # сервис не должен зависать из-за внешнего API


class DgisError(Exception):
    """Понятная человеку причина, почему расстояние не получено."""


def api_key() -> str:
    return (os.environ.get("BP_2GIS_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def _get_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DgisError(f"2ГИС ответил {exc.code}: проверьте ключ и подписку "
                        "на геокодер") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DgisError("Нет связи с 2ГИС: проверьте выход в интернет "
                        "с сервера") from exc
    except json.JSONDecodeError as exc:
        raise DgisError("2ГИС вернул не JSON") from exc


def _post_json(url: str, params: dict, body: dict) -> dict:
    query = urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{url}?{query}", data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DgisError(f"2ГИС ответил {exc.code}: проверьте ключ и подписку "
                        "на расчёт расстояний") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DgisError("Нет связи с 2ГИС: проверьте выход в интернет "
                        "с сервера") from exc
    except json.JSONDecodeError as exc:
        raise DgisError("2ГИС вернул не JSON") from exc


def _check_meta(data: dict) -> None:
    """Ошибка геокодера приходит ВНУТРИ ответа с кодом HTTP 200.

    Проверено на сервере: неверный ключ даёт 200 и
    `meta.code = 403, meta.error.message = "Authorization error"`. Без этой
    проверки пустой список сошёл бы за «место не найдено», и человек искал
    бы опечатку в адресе вместо проблемы с ключом."""
    meta = data.get("meta") or {}
    code = meta.get("code")
    if code and int(code) >= 400:
        message = ((meta.get("error") or {}).get("message")
                   or "ошибка без описания")
        if int(code) in (401, 403):
            raise DgisError(f"2ГИС отклонил ключ ({code}: {message}). "
                            "Проверьте BP_2GIS_KEY и подписку на геокодер.")
        raise DgisError(f"2ГИС вернул ошибку {code}: {message}")


def geocode(address: str) -> tuple[float, float]:
    """Координаты адреса. Бросает DgisError, если место не найдено."""
    address = " ".join((address or "").split())
    if not address:
        raise DgisError("Пустой адрес.")
    data = _get_json(GEOCODE_URL, {"q": address, "fields": "items.point",
                                   "key": api_key()})
    _check_meta(data)
    items = (data.get("result") or {}).get("items") or []
    for item in items:
        point = item.get("point") or {}
        if point.get("lat") is not None and point.get("lon") is not None:
            return float(point["lat"]), float(point["lon"])
    raise DgisError(f"2ГИС не нашёл место «{address[:60]}». "
                    "Уточните адрес или введите плечо вручную.")


def road_distance(origin: tuple[float, float],
                  destination: tuple[float, float]) -> dict:
    """Расстояние (км) и время (мин) по дорогам между двумя точками."""
    body = {
        "points": [{"lat": origin[0], "lon": origin[1]},
                   {"lat": destination[0], "lon": destination[1]}],
        "sources": [0], "targets": [1], "transport": "driving",
    }
    data = _post_json(MATRIX_URL, {"key": api_key(), "version": "2.0"}, body)
    # Матрица тоже умеет отвечать HTTP 200 с телом-ошибкой. Отдельно ловим
    # лимит демо-ключа: он ограничен 50 км, а наши плечи бывают 130–245 км,
    # и без разбора это выглядело бы как «маршрут не построен».
    if data.get("type") == "error":
        message = data.get("error_message") or "ошибка без описания"
        if "demo-keys" in message or "демо" in message.lower():
            raise DgisError(
                "Демо-ключ 2ГИС считает маршруты не длиннее 50 км, а это "
                "плечо длиннее. Для реальных расстояний нужна платная "
                "подписка на Distance Matrix; пока внесите километраж вручную.")
        raise DgisError(f"2ГИС: {message}")
    routes = data.get("routes") or []
    for route in routes:
        # status = OK только когда маршрут действительно построен; на
        # островах и закрытых территориях приходит FAIL, и расстояние в
        # ответе нулевое — записать такое в плечо нельзя.
        if route.get("status") == "OK" and route.get("distance") is not None:
            return {"distance_km": round(float(route["distance"]) / 1000.0, 1),
                    "duration_min": (round(float(route["duration"]) / 60.0)
                                     if route.get("duration") is not None else None)}
    raise DgisError("2ГИС не построил маршрут между этими точками "
                    "(нет проезда либо координаты неточные).")


def point_coords(conn, point) -> tuple[float, float]:
    """Координаты пункта отгрузки: из базы, иначе геокодируем и запоминаем.

    Кэш обязателен: у 2ГИС оплачивается каждый расчёт, а пункты отгрузки
    живут между сделками и используются многократно."""
    if point["lat"] is not None and point["lon"] is not None:
        return float(point["lat"]), float(point["lon"])
    lat, lon = geocode(point["name"])
    conn.execute("UPDATE shipping_points SET lat = ?, lon = ?, "
                 "updated_at = datetime('now') WHERE id = ?",
                 (lat, lon, point["id"]))
    return lat, lon


def distance_for_point(conn, point, to_name: str) -> dict:
    """Плечо от пункта отгрузки до названного места через 2ГИС.

    `to_name` — база, цех или покупатель: обычный адрес, который тоже
    геокодируется. Возвращает километраж, время и адреса, по которым
    считали, — экономисту нужно видеть, что 2ГИС понял под названием."""
    if not enabled():
        raise DgisError("Ключ 2ГИС не задан (переменная BP_2GIS_KEY).")
    origin = point_coords(conn, point)
    destination = geocode(to_name)
    result = road_distance(origin, destination)
    result.update({"from_name": point["name"], "to_name": to_name,
                   "from_coords": origin, "to_coords": destination})
    return result
