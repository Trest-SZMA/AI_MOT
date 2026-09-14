"""Техника, категории груза и нормы погрузки.

Указание директора: рейсы и машины должны считаться от того, чем реально
возят, а не от единого «16 тонн на машину», и с оглядкой на нормы погрузки,
чтобы не ставить в план заведомый перевес.

Сколько тонн войдёт в рейс, решает не только грузоподъёмность: лёгкий
объёмный лом (12А, стружка, кабель в бухтах) упирается в кубатуру кузова
задолго до тоннажа. Поэтому вместимость считается как минимум из двух
ограничений — по массе и по объёму.
"""
from __future__ import annotations

import sqlite3
from statistics import median

# Категории груза и насыпная плотность. Значения ТИПОВЫЕ, взяты как
# отраслевые ориентиры и помечены источником: до подтверждения нормативным
# документом ими нельзя обосновывать погрузку перед ГИБДД, но считать рейсы
# они позволяют уже сейчас. Правятся в справочнике.
DEFAULT_CATEGORIES = [
    ("Лом стальной габаритный (3А, 5А)", "5а;3а;габарит;лом черных", 1.20),
    ("Лом стальной негабаритный (12А, 13А)", "12а;13а;негабарит;легковес", 0.60),
    ("Труба и штанга б/у", "труба;нкт;штанга;обсадн", 0.80),
    ("Стружка стальная", "стружк", 0.60),
    ("Кабель в пучках и барабанах", "кабель;провод;бухт", 0.70),
    ("Лом меди", "медь;медн", 1.50),
    ("Лом свинца", "свинец;свинц", 2.50),
    ("Лом алюминия", "алюмин;дюраль", 0.40),
    ("Аккумуляторы", "аккумулятор;акб", 1.80),
    ("Оборудование и ДХНО", "оборудован;дхно;станц;эцн;пэд", 0.50),
]
DEFAULT_SOURCE = "типовое значение, требует подтверждения"

# Ниже этого веса рейс не берём в статистику: порожние и частичные ездки
# сдвигают медиану загрузки вниз и делают вывод «недогруз» там, где его нет.
MIN_TRIP_T = 1.0
# Выше этого — не автомобильный рейс: в отвесной часть весов записана в
# килограммах (18 431 «тонн» у КМУ с паспортом 13 тн), и такие строки уводили
# 95-й процентиль на сотни тонн. Не пересчитываем, а отбрасываем и считаем
# отдельно: догадка «это, наверное, килограммы» в норматив погрузки не годится.
MAX_TRIP_T = 60.0


def seed_categories(conn: sqlite3.Connection) -> None:
    for name, words, density in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT INTO ref_cargo_categories (name, match_words, density_t_m3, "
            "source) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
            (name, words, density, DEFAULT_SOURCE))


def categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ref_cargo_categories ORDER BY name").fetchall()


def category_for(conn: sqlite3.Connection, text: str | None) -> sqlite3.Row | None:
    """Категория груза по названию позиции: первое совпадение по словам."""
    low = " ".join((text or "").lower().replace("ё", "е").split())
    if not low:
        return None
    for row in categories(conn):
        for word in (row["match_words"] or "").split(";"):
            word = word.strip().lower()
            if word and word in low:
                return row
    return None


def vehicles(conn: sqlite3.Connection, with_capacity: bool = True) -> list[sqlite3.Row]:
    """Парк техники. По умолчанию только машины с грузоподъёмностью —
    легковые и спецтехника в расчёте рейсов не участвуют."""
    where = "WHERE capacity_t > 0" if with_capacity else ""
    return conn.execute(
        f"SELECT * FROM ref_vehicles {where} ORDER BY vehicle_type, name").fetchall()


def vehicle_types(conn: sqlite3.Connection) -> list[dict]:
    """Типы техники со сводкой: сколько машин, паспортные грузоподъёмность
    и объём, и что показывает фактическая загрузка рейсов."""
    rows = conn.execute(
        "SELECT vehicle_type, COUNT(*) AS units, "
        "       MIN(capacity_t) AS cap_min, MAX(capacity_t) AS cap_max, "
        "       MIN(NULLIF(volume_m3, 0)) AS vol_min, "
        "       MAX(NULLIF(volume_m3, 0)) AS vol_max "
        "FROM ref_vehicles WHERE capacity_t > 0 "
        "GROUP BY vehicle_type ORDER BY units DESC").fetchall()
    try:
        load = {r["vehicle_type"]: r for r in conn.execute(
            "SELECT * FROM stat_vehicle_load")}
    except sqlite3.Error:
        load = {}
    out = []
    for r in rows:
        d = dict(r)
        d["load"] = load.get(r["vehicle_type"])
        out.append(d)
    return out


def payload_t(capacity_t: float | None, volume_m3: float | None,
              density_t_m3: float | None) -> dict:
    """Сколько тонн войдёт в рейс и что именно ограничивает.

    По массе — паспортная грузоподъёмность; по объёму — кубатура кузова,
    умноженная на насыпную плотность груза. Берём меньшее из двух: это и
    есть защита от плана, который на бумаге сходится, а в рейсе даёт
    перевес или полупустой кузов.
    """
    cap = float(capacity_t or 0)
    vol = float(volume_m3 or 0)
    dens = float(density_t_m3 or 0)
    by_volume = vol * dens if vol > 0 and dens > 0 else 0.0
    if cap > 0 and by_volume > 0:
        limit = "масса" if cap <= by_volume else "объём"
        return {"payload_t": round(min(cap, by_volume), 2), "limit": limit,
                "by_mass_t": round(cap, 2), "by_volume_t": round(by_volume, 2)}
    if cap > 0:
        return {"payload_t": round(cap, 2), "limit": "масса (объём неизвестен)",
                "by_mass_t": round(cap, 2), "by_volume_t": None}
    if by_volume > 0:
        return {"payload_t": round(by_volume, 2),
                "limit": "объём (грузоподъёмность неизвестна)",
                "by_mass_t": None, "by_volume_t": round(by_volume, 2)}
    return {"payload_t": 0.0, "limit": "нет данных", "by_mass_t": None,
            "by_volume_t": None}


def plan_trips(conn: sqlite3.Connection, volume_t: float, cargo_text: str | None,
               vehicle_type: str | None = None) -> dict:
    """План рейсов на объём: какая техника, сколько тонн в рейсе, сколько рейсов."""
    cat = category_for(conn, cargo_text)
    density = cat["density_t_m3"] if cat else None
    types = vehicle_types(conn)
    if vehicle_type:
        types = [t for t in types if t["vehicle_type"] == vehicle_type] or types
    if not types:
        return {"category": cat, "options": []}
    options = []
    for t in types:
        cap = t["cap_max"] or t["cap_min"]
        vol = t["vol_max"] or t["vol_min"]
        pay = payload_t(cap, vol, density)
        fact = t["load"]["median_t"] if t["load"] else None
        trips = int(-(-volume_t // pay["payload_t"])) if pay["payload_t"] else 0
        options.append({**t, **pay, "trips": trips, "fact_median_t": fact})
    options.sort(key=lambda o: -(o["payload_t"] or 0))
    return {"category": cat, "density": density, "options": options}


def load_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM stat_vehicle_load ORDER BY trips DESC").fetchall()
    except sqlite3.Error:
        return []


def _percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    k = max(0, min(len(values) - 1, int(round((len(values) - 1) * p))))
    return values[k]


def build_load_stats(conn: sqlite3.Connection, trips_by_type: dict[str, list[float]],
                     since: str) -> int:
    """Запись фактической загрузки по типам техники."""
    caps = {r["vehicle_type"]: r["cap"] for r in conn.execute(
        "SELECT vehicle_type, MEDIAN_CAP AS cap FROM ("
        "  SELECT vehicle_type, AVG(capacity_t) AS MEDIAN_CAP FROM ref_vehicles "
        "  WHERE capacity_t > 0 GROUP BY vehicle_type)")}
    conn.execute("DELETE FROM stat_vehicle_load")
    n = 0
    for vtype, raw in sorted(trips_by_type.items()):
        weights = [w for w in raw if MIN_TRIP_T <= w <= MAX_TRIP_T]
        dropped = len(raw) - len(weights)
        if not weights:
            continue
        cap = caps.get(vtype)
        over = (sum(1 for w in weights if cap and w > cap) / len(weights) * 100
                if cap else None)
        conn.execute(
            "INSERT INTO stat_vehicle_load (vehicle_type, period_from, trips, "
            "median_t, p95_t, max_t, capacity_t, over_pct, dropped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vtype, since, len(weights), round(median(weights), 2),
             round(_percentile(weights, 0.95), 2), round(max(weights), 2),
             round(cap, 2) if cap else None,
             round(over, 1) if over is not None else None, dropped))
        n += 1
    return n
