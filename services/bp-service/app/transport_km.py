"""Ставка транспорта по плечу — из фактических рейсов «Отвесной».

Прогон лота 1888 (16.09.2026): 410 т за 1 300 км, а фактический слой взял
транспорт медианой по типу сделки — 61 тыс. на весь лот против 2,4 млн по
нормативам. Медиана по типу не знает плеча; регистр затрат знает километраж
строки, но не тоннаж. Тоннаж, километраж и стоимость каждого рейса есть в
«Отвесной» (наёмный автотранспорт): отсюда ставка руб/т по поясам дальности,
отдельно для лома, трубы и кабеля (у них разная загрузка машины) и общая.

Правила отбора: вид доставки «Автотранспорт найм», стоимость и километраж
больше нуля, вес по ТТН 5–40 т (меньше — довоз и ошибки, больше — вагоны,
записанные автотранспортом), последние 24 месяца от самой свежей даты в
выгрузке. По поясу — медиана руб/т и руб/т·км. Затем ставка выравнивается
так, чтобы не убывать с расстоянием (объединение соседних поясов, где
дальний оказался дешевле ближнего): дешёвые дальние рейсы Когалым →
Северский завод — это обратная загрузка по договорной цене, для плана
с новой площадки на неё рассчитывать нельзя. Сырые медианы хранятся рядом.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

BANDS = [(0, 50), (50, 100), (100, 200), (200, 300), (300, 500), (500, 800), (800, 1200),
         (1200, 2000), (2000, 5000)]
GROUPS = ("лом", "труба", "кабель", "")
MIN_TRIPS = 8
SOURCE = "рейсы «Отвесной» (найм)"


def cargo_group(nomen: str) -> str:
    n = (nomen or "").lower()
    if "труб" in n or "нкт" in n or "штанг" in n:
        return "труба"
    if "кабел" in n:
        return "кабель"
    return "лом"


def group_of_type(bp_type: str | None) -> str:
    return {"pipe": "труба", "cable": "кабель"}.get(bp_type or "", "лом")


def band_of(km: float) -> tuple[int, int] | None:
    for a, b in BANDS:
        if a <= km < b:
            return a, b
    return None


def _isotonic(vals: list[tuple[float, float]]) -> list[float]:
    """Неубывающее сглаживание (pool adjacent violators), веса — рейсы."""
    blocks = [[v, w, 1] for v, w in vals]          # значение, вес, размер
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-9:
            a, b = blocks[i], blocks[i + 1]
            w = a[1] + b[1]
            merged = [(a[0] * a[1] + b[0] * b[1]) / w if w else (a[0] + b[0]) / 2, w, a[2] + b[2]]
            blocks[i:i + 2] = [merged]
            i = max(i - 1, 0)
        else:
            i += 1
    out: list[float] = []
    for v, _w, n in blocks:
        out.extend([v] * n)
    return out


def build(conn: sqlite3.Connection, path: str | Path, months: int = 24) -> dict:
    """Отвесная_*.csv → stat_transport_km."""
    from import_1c_csv import iter_rows, _f, _s
    trips: dict[tuple, list[tuple[float, float, float]]] = defaultdict(list)   # (группа, пояс) → [(руб/т, руб/т·км, т)]
    dates: list[str] = []
    raw = []
    for r in iter_rows(path):
        if _s(r.get("ВидДоставки")) != "Автотранспорт найм":
            continue
        w, cost, km = _f(r.get("ВесПоТТН")), _f(r.get("Стоимость")), _f(r.get("Километраж"))
        d = (_s(r.get("ДатаПогрузки")) or "")[:10]
        if cost <= 0 or km <= 0 or not (5.0 <= w <= 40.0) or not d:
            continue
        raw.append((d, cargo_group(_s(r.get("Номенклатура"))), km, w, cost))
        dates.append(d)
    if not raw:
        return {"rows": 0, "trips": 0}
    last = max(dates)
    y, m = int(last[:4]), int(last[5:7])
    since_idx = y * 12 + m - months
    n_trips = 0
    for d, g, km, w, cost in raw:
        if int(d[:4]) * 12 + int(d[5:7]) < since_idx:
            continue
        b = band_of(km)
        if not b:
            continue
        rec = (cost / w, cost / (w * km), w)
        trips[(g, b)].append(rec)
        trips[("", b)].append(rec)
        n_trips += 1
    conn.execute("DELETE FROM stat_transport_km")
    rows = 0
    for g in GROUPS:
        per_band = []
        for b in BANDS:
            v = trips.get((g, b), [])
            if len(v) >= MIN_TRIPS:
                per_band.append((b, v, median(x[0] for x in v), median(x[1] for x in v), median(x[2] for x in v)))
        if not per_band:
            continue
        smooth = _isotonic([(p[2], len(p[1])) for p in per_band])
        for (b, v, rpt, rptkm, tpt), sm in zip(per_band, smooth):
            conn.execute(
                "INSERT INTO stat_transport_km (cargo_group, km_from, km_to, trips, rub_per_t, rub_per_t_raw, "
                "rub_per_tkm, t_per_trip, period_from, period_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (g, b[0], b[1], len(v), round(sm, 2), round(rpt, 2), round(rptkm, 3), round(tpt, 1),
                 f"{(since_idx - 1) // 12}-{(since_idx - 1) % 12 + 1:02d}", last[:7]))
            rows += 1
    from . import refsources
    refsources.mark(conn, "stat_transport_km", rows, Path(path).name, SOURCE)
    return {"rows": rows, "trips": n_trips, "period_to": last[:7]}


def rate(conn: sqlite3.Connection, group: str, km: float) -> dict | None:
    """Ставка руб/т для плеча: точный пояс своей группы груза → точный пояс
    общей ставки → последний известный пояс своей группы (дальше не
    экстраполируем) → ближайший пояс снизу."""
    try:
        rows = conn.execute(
            "SELECT * FROM stat_transport_km WHERE cargo_group IN (?, '') ORDER BY km_from",
            (group,)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    def pack(hit, g, exact):
        return {"rub_per_t": float(hit["rub_per_t"]), "raw": float(hit["rub_per_t_raw"]),
                "trips": hit["trips"], "band": (hit["km_from"], hit["km_to"]),
                "group": g or "все грузы", "exact": exact}

    for g in (group, ""):
        hit = next((r for r in rows if r["cargo_group"] == g and r["km_from"] <= km < r["km_to"]), None)
        if hit is not None:
            return pack(hit, g, True)
    for g in (group, ""):
        own = [r for r in rows if r["cargo_group"] == g]
        if not own:
            continue
        if km >= own[-1]["km_to"]:
            return pack(own[-1], g, False)
        below = [r for r in own if r["km_to"] <= km]
        return pack(below[-1] if below else own[0], g, False)
    return None


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM stat_transport_km ORDER BY cargo_group = '' DESC, cargo_group, km_from").fetchall()
    except sqlite3.Error:
        return []
