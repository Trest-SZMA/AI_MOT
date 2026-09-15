"""Фактическая рентабельность по типам сделок — из сборки «Реализации».

Порог рентабельности в сервисе один на всё (настройка `margin_threshold`,
ROS по чистой прибыли). Факт по ~1 000 сделкам компании показывает, что
типы живут в разных диапазонах: у чёрного лома медианная валовая маржа
продаж около 32 %, у кабеля — 24 %, у трубы НКТ — 46 % (сентябрь 2026).
Здесь по каждому типу считаются квартили фактической валовой маржи
(выручка − себестоимость продаж) / выручка по ЗАКРЫТЫМ сделкам — продано не
меньше `type_margin_closed_pct` купленного, иначе маржа незакрытого лота
случайна. Тип чужой сделки определяется по доминирующей группе аналитического
учёта 1С (`ref_bp_types.fact_groups`, порог доминирования тот же, что для
наших БП — `bp_type_threshold_pct`).

С планом сравнивается валовая маржа производства нашего P&L
(`gross_production / revenue`: выручка минус закупка и переменные — то же,
что регистр 1С кладёт в себестоимость продаж). Квартили, не среднее.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path

from .db import get_setting

# Типовые группы аналитического учёта 1С по типам сделок. Правятся в
# справочнике (колонка fact_groups); здесь — начальное наполнение.
GROUP_DEFAULTS = {
    "pipe": "Труба НКТ; Труба и штанга; Труба больших диаметров (более 159 мм); "
            "Труба малых диаметров (до 159 мм); Штанга",
    "ferrous": "Лом черных металлов; Лом легированной стали; "
               "Готовая продукция Лом черных металлов",
    "cable": "Кабель",
    "nonferrous": "Лом цветных металлов",
    "dhno": "ДХНО; Комплектующие с разбора; Трансформаторы",
}
ALL = ""                       # ключ строки «по всем сделкам»
MIN_SAMPLE = 4                 # меньше — квартили не считаем


def seed_groups(conn: sqlite3.Connection) -> None:
    """Заполнить fact_groups у типов, где они пусты (один раз)."""
    for code, groups in GROUP_DEFAULTS.items():
        conn.execute("UPDATE ref_bp_types SET fact_groups = ? "
                     "WHERE code = ? AND COALESCE(fact_groups, '') = ''", (groups, code))


def group_map(conn: sqlite3.Connection) -> dict[str, str]:
    """группа аналитического учёта (нижний регистр) → код типа сделки."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT code, fact_groups FROM ref_bp_types WHERE is_active = 1"):
        for part in (r["fact_groups"] or "").split(";"):
            key = part.strip().lower()
            if key:
                out.setdefault(key, r["code"])
    return out


def closed_pct(conn: sqlite3.Connection) -> float:
    return get_setting(conn, "type_margin_closed_pct", 80.0)


def _quartiles(values: list[float]) -> tuple[float | None, float, float | None]:
    if len(values) >= MIN_SAMPLE:
        q = statistics.quantiles(values, n=4)
        return q[0], statistics.median(values), q[2]
    return None, statistics.median(values), None


def build(conn: sqlite3.Connection, json_path: str | Path) -> dict:
    """sales_data.json «Реализации» → stat_type_margin. -> статистика."""
    with open(json_path, encoding="utf-8") as fh:
        d = json.load(fh)
    meta = d.get("meta") or {}
    bp_nm = d.get("bp_nm") or {}
    gmap = group_map(conn)
    dominance = get_setting(conn, "bp_type_threshold_pct", 80.0)
    closed = closed_pct(conn)
    by_type: dict[str, list[tuple[float, float]]] = {}
    st = {"bps": 0, "closed": 0, "typed": 0, "generated": meta.get("generated")}
    for r in d.get("bpbuy") or []:
        st["bps"] += 1
        bought, sold = float(r.get("t") or 0), float(r.get("sold") or 0)
        rub, cost = float(r.get("rub") or 0), float(r.get("cost") or 0)
        if bought <= 0 or sold <= 0 or rub <= 0:
            continue
        if sold / bought * 100.0 + 1e-9 < closed:
            continue
        st["closed"] += 1
        margin = (rub - cost) / rub * 100.0
        by_type.setdefault(ALL, []).append((margin, rub))
        groups = ((bp_nm.get(r.get("s")) or {}).get("т") or {}).get("g") or {}
        total = sum(float(v) for v in groups.values())
        if total <= 0:
            continue
        tons: dict[str, float] = {}
        for g, v in groups.items():
            code = gmap.get(str(g).strip().lower())
            if code:
                tons[code] = tons.get(code, 0.0) + float(v)
        if not tons:
            continue
        code, top = max(tons.items(), key=lambda kv: kv[1])
        code = code if top / total * 100.0 + 1e-9 >= dominance else "mixed"
        by_type.setdefault(code, []).append((margin, rub))
        st["typed"] += 1
    conn.execute("DELETE FROM stat_type_margin")
    for code, obs in by_type.items():
        vals = [m for m, _ in obs]
        p25, med, p75 = _quartiles(vals)
        conn.execute(
            "INSERT INTO stat_type_margin (bp_type, n, p25, median, p75, revenue, "
            "closed_pct, generated_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "datetime('now'))",
            (code, len(vals), p25, med, p75, sum(r for _, r in obs), closed,
             meta.get("generated")))
    from . import refsources
    refsources.mark(conn, "stat_type_margin", sum(len(v) for v in by_type.values()),
                    Path(json_path).name, "снимок «Реализации»")
    st["types"] = len(by_type)
    return st


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT m.*, COALESCE(t.name, CASE m.bp_type WHEN '' THEN 'Все сделки' "
            "WHEN 'mixed' THEN 'Смешанные' ELSE m.bp_type END) AS name, "
            "COALESCE(t.sort, 900) AS sort FROM stat_type_margin m "
            "LEFT JOIN ref_bp_types t ON t.code = m.bp_type "
            "ORDER BY m.bp_type = '' DESC, sort").fetchall()
    except sqlite3.Error:
        return []


def assess(conn: sqlite3.Connection, bp_type: str | None, plan_margin_pct: float) -> dict | None:
    """Где план стоит относительно факта по типу. None — факта нет.

    -> {code, n, p25, median, p75, position: 'below_p25'|'below_median'|
        'above_median'|'above_p75', label}.
    """
    code = bp_type or ALL
    try:
        row = conn.execute("SELECT * FROM stat_type_margin WHERE bp_type = ?",
                           (code,)).fetchone()
        if row is None and code != ALL:
            row = conn.execute("SELECT * FROM stat_type_margin WHERE bp_type = ''").fetchone()
            code = ALL
    except sqlite3.Error:
        return None
    if row is None:
        return None
    p25, med, p75 = row["p25"], row["median"], row["p75"]
    if p25 is not None and plan_margin_pct < p25:
        pos, label = "below_p25", "ниже четверти худших сделок по факту"
    elif plan_margin_pct < med:
        pos, label = "below_median", "ниже медианы факта"
    elif p75 is not None and plan_margin_pct >= p75:
        pos, label = "above_p75", "выше трёх четвертей сделок по факту"
    else:
        pos, label = "above_median", "не ниже медианы факта"
    return {"code": code, "n": row["n"], "p25": p25, "median": med, "p75": p75,
            "position": pos, "label": label, "generated_at": row["generated_at"],
            "by_all": code == ALL}
