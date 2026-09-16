"""Независимая модель результата сделки — по факту закрытых сделок.

Цель директора: сервис как модель расчёта «исходя из того, что реально
происходит по факту, на основе обучаемости предыдущих вариантов». Книга
экономиста считает целевой результат (по архиву прибыль ровно 10 % у всех
типов, затраты подгоняются), нормативная модель считает структуру; здесь —
третий, независимый слой: что ПОКАЖЕТ сделка, если пойдёт как похожие
закрытые.

Обучающая выборка — закрытые сделки (продано ≥ type_margin_closed_pct
купленного) из снимка «Реализации» и регистра затрат: у каждой известны тип,
площадка, тоннаж, выручка, себестоимость продаж, затраты по серии. Для новой
сделки берутся соседи по типу и площадке (при нехватке — по типу, затем все),
и по ним считаются медианы и квартили удельных величин:

    цена продажи руб/т, валовая маржа продаж %, затраты по серии руб/т.

Ожидаемый P&L = удельные величины × тоннаж сделки на долю; распределяемые —
ставка площадки (fact_costs.site_overhead_rates). Никаких коэффициентов,
которые кто-то придумал: только медианы факта, обновляемые ночью.

Точность модели проверяется на самих закрытых сделках (leave-one-out): для
каждой закрытой сделки прогноз по соседям без неё против её факта — это и
есть «обучаемость», измеренная честно, по типам.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from statistics import median, quantiles

from . import fact_costs
from .calc import _f, _row_get, lot_share

MIN_NEIGHBORS = 5


def _q(values: list[float]) -> dict:
    v = sorted(values)
    if not v:
        return {"median": None, "p25": None, "p75": None, "n": 0}
    if len(v) >= 4:
        q = quantiles(v, n=4)
        return {"median": median(v), "p25": q[0], "p75": q[2], "n": len(v)}
    return {"median": median(v), "p25": None, "p75": None, "n": len(v)}


def training_set(conn: sqlite3.Connection) -> list[dict]:
    """Закрытые сделки с фактом: {deal_no, bp_type, site, sold_t, revenue,
    cost_of_sales, series_costs, price_per_t, gross_pct, costs_per_t}."""
    try:
        rows = conn.execute("SELECT * FROM stat_outcome_train ORDER BY deal_no").fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def build(conn: sqlite3.Connection, snapshot_path: str) -> dict:
    """Собрать обучающую выборку из снимка «Реализации» + регистра затрат."""
    from .db import get_setting
    from . import type_margin
    with open(snapshot_path, encoding="utf-8") as fh:
        d = json.load(fh)
    closed_pct = get_setting(conn, "type_margin_closed_pct", 80.0)
    dominance = get_setting(conn, "bp_type_threshold_pct", 80.0)
    gmap = type_margin.group_map(conn)
    smap = fact_costs.site_of_division(conn)
    bp_nm = d.get("bp_nm") or {}
    # затраты по серии и главное подразделение — из регистра
    costs = {r["deal_no"]: float(r["c"]) for r in conn.execute(
        "SELECT deal_no, SUM(amount) AS c FROM stat_fact_costs WHERE section IS NOT NULL "
        "AND item IS NOT NULL GROUP BY deal_no")}
    divs = {}
    for r in conn.execute("SELECT deal_no, division, amount FROM stat_fact_costs_div ORDER BY amount DESC"):
        divs.setdefault(r["deal_no"], r["division"])
    conn.execute("DELETE FROM stat_outcome_train")
    n = 0
    seen = set()
    for r in d.get("bpbuy") or []:
        no = str(r.get("bp") or "")
        bought, sold = float(r.get("t") or 0), float(r.get("sold") or 0)
        rev, cos = float(r.get("rub") or 0), float(r.get("cost") or 0)
        if not no or no in seen or bought <= 0 or sold <= 0 or rev <= 0:
            continue
        if sold / bought * 100.0 + 1e-9 < closed_pct:
            continue
        groups = ((bp_nm.get(r.get("s")) or {}).get("т") or {}).get("g") or {}
        total = sum(float(v) for v in groups.values())
        code = None
        if total > 0:
            tons: dict[str, float] = defaultdict(float)
            for g, v in groups.items():
                c = gmap.get(str(g).strip().lower())
                if c:
                    tons[c] += float(v)
            if tons:
                c, top = max(tons.items(), key=lambda kv: kv[1])
                code = c if top / total * 100.0 + 1e-9 >= dominance else "mixed"
        site = smap.get(divs.get(no, ""), None)
        sc = costs.get(no, 0.0)
        conn.execute(
            "INSERT INTO stat_outcome_train (deal_no, bp_type, site, bought_t, sold_t, revenue, "
            "cost_of_sales, series_costs, price_per_t, gross_pct, costs_per_t, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (no, code, site, bought, sold, rev, cos, sc, rev / sold, (rev - cos) / rev * 100.0,
             sc / sold))
        seen.add(no)
        n += 1
    from . import refsources
    refsources.mark(conn, "stat_outcome_train", n, "sales_data.json", "снимок «Реализации» + регистр затрат")
    return {"deals": n}


def neighbors(train: list[dict], bp_type: str | None, site: str | None,
              exclude: str | None = None) -> tuple[list[dict], str]:
    """Похожие сделки: тип+площадка → тип → все. Возвращает (список, уровень)."""
    pool = [t for t in train if t["deal_no"] != exclude and t["sold_t"] > 0]
    lvl = [t for t in pool if t["bp_type"] == bp_type and t["site"] == site and site]
    if len(lvl) >= MIN_NEIGHBORS:
        return lvl, f"тип «{bp_type}» на площадке {site}"
    lvl = [t for t in pool if t["bp_type"] == bp_type and bp_type]
    if len(lvl) >= MIN_NEIGHBORS:
        return lvl, f"тип «{bp_type}», все площадки"
    return pool, "все закрытые сделки"


def expect(conn: sqlite3.Connection, bp, items: list, site: str | None = None) -> dict:
    """Ожидаемый результат сделки по факту похожих закрытых сделок."""
    train = training_set(conn)
    bp_type = _row_get(bp, "bp_type")
    tons = sum(_f(it["volume_t"]) for it in items) * lot_share(bp)
    if not train or tons <= 0:
        return {"ok": False, "reason": "нет обучающей выборки или тоннажа"}
    nb, level = neighbors(train, bp_type, site)
    price = _q([t["price_per_t"] for t in nb])
    gross = _q([t["gross_pct"] for t in nb])
    # Затраты по серии: у части закрытых сделок в регистре нет ни одной строки
    # (ноль — это «не разнесли», а не «бесплатно»), берём только ненулевые.
    cpt = _q([t["costs_per_t"] for t in nb if t["costs_per_t"] and t["costs_per_t"] > 0])
    if not cpt["n"]:
        cpt = {"median": 0.0, "p25": None, "p75": None, "n": 0}
    # Распределяемые площадки — из фактических ставок
    site_rate = None
    if site:
        site_rate = next((d["rate_per_t"] for d in fact_costs.site_overhead_rates(conn) if d["site"] == site), None)
    revenue = price["median"] * tons
    purchase = revenue * (1 - gross["median"] / 100.0)          # себестоимость продаж (закупка + переработка)
    series_costs = cpt["median"] * tons
    overhead = (site_rate or 0.0) * tons
    profit = revenue - purchase - series_costs - overhead
    lo = (price["p25"] or price["median"]) * tons * (gross["p25"] or gross["median"]) / 100.0 - (cpt["p75"] or cpt["median"]) * tons - overhead
    hi = (price["p75"] or price["median"]) * tons * (gross["p75"] or gross["median"]) / 100.0 - (cpt["p25"] or cpt["median"]) * tons - overhead
    return {"ok": True, "level": level, "n": len(nb), "tons": round(tons, 3),
            "price": price, "gross": gross, "costs_per_t": cpt, "site": site, "site_rate": site_rate,
            "revenue": revenue, "cost_of_sales": purchase, "series_costs": series_costs,
            "overhead": overhead, "profit": profit, "profit_lo": lo, "profit_hi": hi,
            "margin_pct": profit / revenue * 100.0 if revenue else None,
            "neighbors": sorted(nb, key=lambda t: -t["revenue"])[:8]}


def accuracy(conn: sqlite3.Connection) -> list[dict]:
    """Точность модели на закрытых сделках (leave-one-out) по типам:
    медиана |прогноз − факт| / факт для цены, валовой маржи и затрат."""
    train = training_set(conn)
    errs: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in train:
        nb, _ = neighbors(train, t["bp_type"], t["site"], exclude=t["deal_no"])
        if len(nb) < MIN_NEIGHBORS:
            continue
        for key in ("price_per_t", "gross_pct", "costs_per_t"):
            vals = [x[key] for x in nb if key != "costs_per_t" or (x[key] and x[key] > 0)]
            if len(vals) < MIN_NEIGHBORS:
                continue
            pred = median(vals)
            fact = t[key]
            if fact and (key != "costs_per_t" or fact > 0):
                errs[t["bp_type"] or ""][key].append(abs(pred - fact) / abs(fact) * 100.0)
                errs[""][key].append(abs(pred - fact) / abs(fact) * 100.0)
    out = []
    for code, by_key in errs.items():
        out.append({"bp_type": code, "n": len(by_key["price_per_t"]),
                    "price_err": median(by_key["price_per_t"]) if by_key["price_per_t"] else None,
                    "gross_err": median(by_key["gross_pct"]) if by_key["gross_pct"] else None,
                    "costs_err": median(by_key["costs_per_t"]) if by_key["costs_per_t"] else None})
    return sorted(out, key=lambda r: (r["bp_type"] != "", r["bp_type"]))
