"""Пункты отгрузки, сопоставление со складами 1С и маршруты.

Места отгрузки приходят из перечня продавца адресом («Волгоградская обл.,
г. Котово, трубная база»), а склады в 1С названы внутренними кодами
(«252_ВНПЗ Волгоград ЛОМ»). Прямое сравнение строк тут не работает, поэтому
сопоставление идёт по значимым словам адреса (город, посёлок, район) с
именем и дивизионом склада, а результат подтверждает человек — как в
сопоставлении номенклатуры.

Маршруты: строим ссылки в 2ГИС по адресам. Расстояние по дорогам нельзя
взять из воздуха — его либо подставляет перечень (колонка «расстояние до
базового логистического пункта»), либо человек проверяет по ссылке и
вносит; проверенное значение сохраняется в shipping_routes и дальше
переиспользуется.
"""
from __future__ import annotations

import math
import re
import sqlite3
from urllib.parse import quote

# Слова адреса, не несущие смысла при сопоставлении.
_STOP = {"обл", "область", "г", "город", "с", "село", "п", "пос", "поселок",
         "посёлок", "р-н", "район", "ул", "улица", "д", "дом", "база",
         "площадка", "трубная", "территория", "участок", "км", "рф", "нао",
         "хмао", "янао", "ст", "станция", "мкр", "пгт"}
# Слово, встречающееся более чем у стольких складов, считаем общим (регион,
# «лом», «прочее») — оно не указывает на конкретное место отгрузки.
GENERIC_DF = 60
_REGION_RE = re.compile(
    r"([А-ЯЁ][а-яё-]+(?:ская|цкая|кая)\s+обл\.?|[А-ЯЁ][а-яё-]+\s+край|"
    r"Республика\s+[А-ЯЁ][а-яё-]+|ХМАО|ЯНАО|НАО)", re.IGNORECASE)


def norm_name(value: str) -> str:
    """Нормализация адреса для поиска дублей: регистр, пунктуация, пробелы."""
    low = (value or "").lower().replace("ё", "е")
    low = re.sub(r"[^\w\s]", " ", low, flags=re.UNICODE)
    return " ".join(low.split())


def tokens(value: str) -> set[str]:
    """Значимые слова адреса: города и посёлки, без «обл.», «ул.» и цифр."""
    out = set()
    for t in norm_name(value).split():
        if t in _STOP or len(t) < 3 or t.isdigit():
            continue
        out.add(t)
    return out


def region_of(address: str) -> str | None:
    m = _REGION_RE.search(address or "")
    return m.group(1).strip() if m else None


def register_point(conn: sqlite3.Connection, name: str, *,
                   base_point: str | None = None,
                   distance_km: float | None = None,
                   seller_name: str | None = None) -> int | None:
    """Регистрация места отгрузки в реестре (идемпотентно по адресу).

    Возвращает id пункта. Уже известный пункт дополняется недостающими
    реквизитами, но не перетирает подтверждённые человеком данные.
    """
    name = " ".join((name or "").split())
    key = norm_name(name)
    if not key or len(key) < 4:
        return None
    row = conn.execute("SELECT * FROM shipping_points WHERE name_norm = ?",
                       (key,)).fetchone()
    if row:
        sets, params = [], []
        if base_point and not row["base_point"]:
            sets.append("base_point = ?")
            params.append(base_point)
        if distance_km is not None and row["distance_km"] is None:
            sets.append("distance_km = ?")
            params.append(distance_km)
        if sets:
            params.append(row["id"])
            conn.execute("UPDATE shipping_points SET %s, "
                         "updated_at = datetime('now') WHERE id = ?"
                         % ", ".join(sets), params)
        return row["id"]
    cur = conn.execute(
        "INSERT INTO shipping_points (name, name_norm, region, base_point, "
        "distance_km, seller_name) VALUES (?, ?, ?, ?, ?, ?)",
        (name, key, region_of(name), base_point, distance_km, seller_name))
    return cur.lastrowid


def suggest_warehouses(conn: sqlite3.Connection, point_name: str,
                       limit: int = 5) -> list[dict]:
    """Кандидаты складов 1С для места отгрузки.

    Совпадение — по значимым словам адреса (город/посёлок) в имени склада
    или его дивизионе. Рабочие склады приоритетнее: группы и явно
    выведенные из оборота («не используем») уходят вниз.
    """
    # Название региона в сопоставлении не участвует: оно хранится отдельным
    # полем, а как слово только мешает — «волгоградская» есть у складов всей
    # области и перебивала «котово», хотя именно посёлок и определяет место.
    toks = tokens(point_name) - tokens(region_of(point_name) or "")
    if not toks:
        return []
    rows = conn.execute(
        "SELECT id, name, top_parent, parent_name FROM ref_warehouses "
        "WHERE is_group = 0").fetchall()
    # Первый проход: у скольких складов встречается слово — этим и меряется
    # его информативность (см. вес ниже).
    hits_by_row: list[tuple] = []
    df: dict[str, int] = {}
    for r in rows:
        hay = norm_name(" ".join(filter(None, [r["name"], r["top_parent"],
                                               r["parent_name"]])))
        hits = [t for t in toks if t in hay]
        if not hits:
            continue
        for t in hits:
            df[t] = df.get(t, 0) + 1
        hits_by_row.append((r, hits))
    generic = {t for t, n in df.items() if n > GENERIC_DF}
    scored: list[dict] = []
    for r, hits in hits_by_row:
        useful = [t for t in hits if t not in generic]
        if not useful:
            continue
        # Вес слова — по редкости: чем у меньшего числа складов оно есть,
        # тем точнее указывает на место (сравнение по длине слова давало
        # ложные попадания).
        score = sum(round(math.log(len(rows) / max(df[t], 1)), 2)
                    for t in useful)
        parent = (r["top_parent"] or "").lower()
        if "не использ" in parent:
            score -= 15                  # выведенные из оборота — в конец
        scored.append({"id": r["id"], "name": r["name"],
                       "division": r["top_parent"], "score": score,
                       "matched": sorted(useful)})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def match_points(conn: sqlite3.Connection, only_unmatched: bool = True) -> int:
    """Автосопоставление пунктов со складами: проставляет лучший кандидат,
    оставляя признак «авто» — подтверждает человек."""
    where = "WHERE warehouse_id IS NULL" if only_unmatched else ""
    n = 0
    for p in conn.execute(f"SELECT * FROM shipping_points {where}").fetchall():
        if p["match_confirmed"]:
            continue
        best = suggest_warehouses(conn, p["name"], limit=1)
        if not best or best[0]["score"] <= 0:
            continue
        conn.execute(
            "UPDATE shipping_points SET warehouse_id = ?, warehouse_name = ?, "
            "match_source = 'авто', updated_at = datetime('now') WHERE id = ?",
            (best[0]["id"], best[0]["name"], p["id"]))
        n += 1
    return n


def gis_search_url(address: str) -> str:
    """Ссылка на поиск места в 2ГИС — проверить адрес и снять координаты."""
    return "https://2gis.ru/search/" + quote(" ".join((address or "").split()))


def gis_route_url(origin: str, destination: str) -> str:
    """Маршрут по дорогам в 2ГИС между адресами.

    Расстояние сервис не выдумывает: ссылка открывает построение маршрута,
    человек видит километраж по доступным дорогам и вносит его в карточку —
    дальше значение хранится и переиспользуется.
    """
    a = quote(" ".join((origin or "").split()))
    b = quote(" ".join((destination or "").split()))
    return f"https://2gis.ru/directions/tab/car/search/{a}%20to%20{b}"


def route_for(conn: sqlite3.Connection, point_id: int,
              to_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM shipping_routes WHERE from_point = ? AND to_name = ?",
        (point_id, to_name)).fetchone()


def save_route(conn: sqlite3.Connection, point_id: int, to_name: str,
               distance_km: float | None, duration_min: float | None = None,
               source: str = "вручную", note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO shipping_routes (from_point, to_name, distance_km, "
        "duration_min, source, checked_at, note) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT (from_point, to_name) DO UPDATE SET "
        "distance_km = excluded.distance_km, duration_min = excluded.duration_min, "
        "source = excluded.source, checked_at = excluded.checked_at, "
        "note = excluded.note",
        (point_id, to_name, distance_km, duration_min, source, note))


def points_for_bp(conn: sqlite3.Connection, bp_id: int) -> list[dict]:
    """Пункты отгрузки лота: адреса позиций с объёмом, сопоставленным
    складом и известными маршрутами."""
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(i.division), ''), i.warehouse) AS place, "
        "SUM(i.volume_t) AS volume, COUNT(*) AS positions, "
        "MAX(i.distance_km) AS distance_km "
        "FROM bp_items i WHERE i.bp_id = ? "
        "GROUP BY place HAVING place IS NOT NULL ORDER BY volume DESC",
        (bp_id,)).fetchall()
    out = []
    for r in rows:
        p = conn.execute("SELECT * FROM shipping_points WHERE name_norm = ?",
                         (norm_name(r["place"]),)).fetchone()
        routes = conn.execute(
            "SELECT * FROM shipping_routes WHERE from_point = ? ORDER BY to_name",
            (p["id"],)).fetchall() if p else []
        out.append({
            "place": r["place"], "volume": r["volume"],
            "positions": r["positions"],
            "distance_km": (p["distance_km"] if p else None) or r["distance_km"],
            "point": p, "routes": routes,
            "gis_search": gis_search_url(r["place"]),
            "suggestions": (suggest_warehouses(conn, r["place"], 3)
                            if p is None or not p["warehouse_id"] else []),
        })
    return out
