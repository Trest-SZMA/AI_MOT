"""Сверка книг экономистов с фактом 1С по каждой сделке.

Директор 16.09.2026: «по каждому БП — разница, что было по факту и как надо
было считать, а не так, как писали экономисты». Три столбца на сделку:

  книга    — версия книги, отправленная заказчику (`dflt` в bpver снимка
             «Реализации»: лук → лук+дсп → дсп → прочая), из архива
             stat_book_plan: тоннаж, выручка, закупка, затраты (остаток),
             прибыль;
  факт 1С  — снимок «Реализации» (куплено, продано, выручка, себестоимость
             продаж) + регистр затрат по серии (только с 2025 г.) + распре-
             деляемые площадки по фактической ставке руб/т;
  модель   — как посчитал бы сервис от факта: тот же лот и та же закупка,
             что у экономиста, но цена продажи — по номенклатуре: медиана
             факта той же номенклатуры 1С у других сделок (год ±1, без самой
             сделки), взвешенная по составу сделки; где номенклатура редкая —
             медиана цены похожих закрытых сделок (тип, год ±1); затраты по
             серии — медиана руб/т закрытых сделок того же типа по регистру,
             распределяемые — ставка площадки.

Ошибка каждого столбца против факта считается по цене продажи (руб/т),
затратам (руб/т) и рентабельности (план − факт, процентные пункты, чтобы
малая прибыль не давала бесконечных процентов). Затраты у экономиста — все
(транспорт, переработка, накладные по нормативам) против факта «серия +
распределяемые»; у модели распределяемые — та же ставка площадки, что и в
факте, поэтому её ошибка затрат считается только по серии — честно. Сводка по типам — медианы ошибок: где
модель ближе к факту, чем экономист, и где нет. Это честная проверка,
а не демонстрация.

Что не сравнивается и почему: затраты по серии до 2025 г. в регистре
отсутствуют (выгрузка за 24 месяца); у половины сделок 2025–2026 гг. к серии
привязаны только 1–2 статьи (одна «собственная техника» на десятки рублей
за тонну) — это «не разнесли», а не «дёшево», поэтому факт затрат считается
известным только при трёх и более статьях по серии (`series_full`), и лишь у
таких сделок есть прибыль факта и ошибка затрат; ставка распределяемых —
сегодняшняя (12 закрытых месяцев), для сделок 2022–2024 гг. она условна.
Затраты по серии — в основном транспорт, они зависят от плеча, и медиана
по типу даёт ±25–50 %: это слабое место модели, честно показанное в сводке;
правильный путь — ставка руб/т·км от факта, для чего нужен километраж
сделок.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

from . import fact_costs, type_margin
from .db import get_setting

MIN_NEIGHBORS = 5
FULL_ITEMS = 3          # статей по серии в регистре, чтобы считать факт затрат полным
DFLT_ORDER = {"luk": 0, "luk+dsp": 1, "dsp": 2, "other": 3, "": 9}
_YEAR = re.compile(r"(20\d\d)")


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _med(values: list) -> float | None:
    v = [x for x in values if x is not None]
    return median(v) if v else None


def _pct(a: float | None, b: float | None) -> float | None:
    """(a − b) / |b|, %. None — нечего делить."""
    if a is None or b is None or not b:
        return None
    return (a - b) / abs(b) * 100.0


def _chosen_version(bpver: list[dict]) -> dict[str, str]:
    """Номер запроса → лист книги, отправленной заказчику."""
    best: dict[str, tuple[int, str]] = {}
    for v in bpver:
        no = str(v.get("bp") or "")
        if not no or v.get("status") != "ОК":
            continue
        rank = DFLT_ORDER.get(str(v.get("dflt") or ""), 9)
        cur = best.get(no)
        if cur is None or rank < cur[0]:
            best[no] = (rank, v.get("sheet") or "")
    return {no: sheet for no, (rank, sheet) in best.items()}


def _start_year(gantt: list[dict]) -> dict[str, str]:
    """Имя серии → год первой закупки по графику «Реализации»."""
    out: dict[str, str] = {}
    for g in gantt:
        start = None
        for ver in (g.get("v") or {}).values():
            s = (ver.get("т") or {}).get("start")
            if s:
                start = min(start or s, s)
        if start:
            out[g.get("series") or ""] = start[:4]
    return out


def _deal_type(groups: dict, gmap: dict, dominance: float) -> str | None:
    total = sum(_f(v) for v in groups.values())
    if total <= 0:
        return None
    tons: dict[str, float] = defaultdict(float)
    for g, v in groups.items():
        c = gmap.get(str(g).strip().lower())
        if c:
            tons[c] += _f(v)
    if not tons:
        return None
    c, top = max(tons.items(), key=lambda kv: kv[1])
    return c if top / total * 100.0 + 1e-9 >= dominance else "mixed"


def _neighbors(rows: list[dict], me: dict) -> tuple[list[dict], str]:
    """Похожие закрытые сделки: тип и год ±1 → тип → все. Без самой сделки."""
    pool = [r for r in rows if r["closed"] and r["deal_no"] != me["deal_no"] and r["fact_price"]]
    yr = int(me["year"]) if me.get("year") else None
    if me.get("bp_type") and yr:
        lvl = [r for r in pool if r["bp_type"] == me["bp_type"] and r.get("year")
               and abs(int(r["year"]) - yr) <= 1]
        if len(lvl) >= MIN_NEIGHBORS:
            return lvl, f"тип, {yr - 1}–{yr + 1}"
    if me.get("bp_type"):
        lvl = [r for r in pool if r["bp_type"] == me["bp_type"]]
        if len(lvl) >= MIN_NEIGHBORS:
            return lvl, "тип, все годы"
    return pool, "все закрытые"


def build(conn: sqlite3.Connection, snapshot_path: str | Path) -> dict:
    """Снимок «Реализации» + архив книг + регистр затрат → stat_deal_audit."""
    with open(snapshot_path, encoding="utf-8") as fh:
        d = json.load(fh)
    closed_pct = get_setting(conn, "type_margin_closed_pct", 80.0)
    dominance = get_setting(conn, "bp_type_threshold_pct", 80.0)
    gmap = type_margin.group_map(conn)
    smap = fact_costs.site_of_division(conn)
    site_rate = {r["site"]: r["rate_per_t"] for r in fact_costs.site_overhead_rates(conn)}
    chosen = _chosen_version(d.get("bpver") or [])
    years = _start_year(d.get("gantt") or [])
    bp_nm = d.get("bp_nm") or {}
    # затраты по серии из регистра (с 2025 г.) и подразделение с наибольшей суммой
    series_costs, series_items = {}, {}
    for r in conn.execute(
            "SELECT deal_no, SUM(amount) AS c, COUNT(DISTINCT item) AS k FROM stat_fact_costs "
            "WHERE section IS NOT NULL AND item IS NOT NULL GROUP BY deal_no"):
        series_costs[r["deal_no"]] = _f(r["c"])
        series_items[r["deal_no"]] = int(r["k"])
    reg_div: dict[str, str] = {}
    for r in conn.execute("SELECT deal_no, division, amount FROM stat_fact_costs_div ORDER BY amount DESC"):
        reg_div.setdefault(r["deal_no"], r["division"])
    # книги: выбранная версия, иначе последняя в архиве
    books: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM stat_book_plan ORDER BY id"):
        rec = dict(r)
        no = rec["deal_no"]
        want = chosen.get(no)
        if no not in books or (want and rec["version"] == want and books[no]["version"] != want):
            books[no] = rec
    deal_names = {r["deal_no"]: (r["name"], r["kind"]) for r in conn.execute(
        "SELECT deal_no, name, kind FROM ref_deals")}
    # факт по номеру запроса — суммируем серии одной сделки
    fact: dict[str, dict] = {}
    for r in d.get("bpbuy") or []:
        no = str(r.get("bp") or "")
        if not no:
            continue
        f = fact.setdefault(no, {"bought": 0.0, "sold": 0.0, "rev": 0.0, "cos": 0.0,
                                 "groups": defaultdict(float), "dirs": defaultdict(float),
                                 "year": None, "ca": r.get("ca") or ""})
        f["bought"] += _f(r.get("t"))
        f["sold"] += _f(r.get("sold"))
        f["rev"] += _f(r.get("rub"))
        f["cos"] += _f(r.get("cost"))
        t = (bp_nm.get(r.get("s")) or {}).get("т") or {}
        for g, v in (t.get("g") or {}).items():
            f["groups"][g] += _f(v)
        for dv, v in (t.get("d") or {}).items():
            f["dirs"][dv] += _f(v)
        y = years.get(r.get("s") or "")
        if y and (f["year"] is None or y < f["year"]):
            f["year"] = y
    # цена по номенклатуре 1С: сделка → [(номенклатура, продано, руб/т)]
    nomen_idx: dict[str, list[tuple[str, str | None, float, float]]] = defaultdict(list)
    mix: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in d.get("bpbuy") or []:
        no = str(r.get("bp") or "")
        if not no:
            continue
        y = years.get(r.get("s") or "")
        for n in r.get("nm") or []:
            sold, rev = _f(n.get("s")), _f(n.get("r"))
            if sold <= 0 or rev <= 0:
                continue
            key = str(n.get("n") or "").strip().lower()
            nomen_idx[key].append((no, y, rev / sold, sold))
            mix[no].append((key, sold))
    rows: list[dict] = []
    for no, f in fact.items():
        b = books.get(no)
        if f["sold"] <= 0 or f["rev"] <= 0:
            continue
        site = smap.get(reg_div.get(no, ""))
        if not site and f["dirs"]:
            for dv, _v in sorted(f["dirs"].items(), key=lambda kv: -kv[1]):
                if smap.get(dv):
                    site = smap[dv]
                    break
        sc = series_costs.get(no)
        full = series_items.get(no, 0) >= FULL_ITEMS
        name, kind = deal_names.get(no, (f["ca"], None))
        row = {
            "deal_no": no, "name": name or f["ca"] or None, "kind": kind,
            "bp_type": _deal_type(f["groups"], gmap, dominance),
            "site": site, "year": f["year"] or (b or {}).get("year"),
            "closed": int(f["bought"] > 0 and f["sold"] / f["bought"] * 100.0 + 1e-9 >= closed_pct),
            "bought_t": f["bought"], "sold_t": f["sold"], "fact_rev": f["rev"], "fact_cos": f["cos"],
            "fact_price": f["rev"] / f["sold"],
            "fact_gross_pct": (f["rev"] - f["cos"]) / f["rev"] * 100.0,
            "fact_series": sc, "fact_series_pt": (sc / f["sold"]) if sc else None,
            "series_items": series_items.get(no, 0), "series_full": int(full),
            "fact_overhead": (site_rate[site] * f["sold"]) if site in site_rate else None,
        }
        row["fact_profit"] = (f["rev"] - f["cos"] - sc - row["fact_overhead"]
                              if full and row["fact_overhead"] is not None else None)
        if b:
            row.update({
                "plan_version": b["version"], "plan_file": b["file"], "plan_t": b["volume_t"],
                "plan_rev": b["revenue"], "plan_purchase": b["purchase"], "plan_costs": b["costs"],
                "plan_profit": b["profit"], "plan_price": b["revenue"] / b["volume_t"],
                "plan_costs_pt": b["costs"] / b["volume_t"],
                "plan_margin_pct": b["profit"] / b["revenue"] * 100.0,
            })
        rows.append(row)
    # модель: цена по номенклатуре (год ±1, без самой сделки), иначе соседи по типу
    for row in rows:
        nb, level = _neighbors(rows, row)
        type_price = _med([r["fact_price"] for r in nb])
        price, covered, total = None, 0.0, 0.0
        acc = 0.0
        yr = int(row["year"]) if row.get("year") else None
        for key, sold in mix.get(row["deal_no"], []):
            cands = [p for (dn, y, p, _t) in nomen_idx[key]
                     if dn != row["deal_no"] and (not yr or not y or abs(int(y) - yr) <= 1)]
            if len(cands) >= 3:
                acc += median(cands) * sold
                covered += sold
            elif type_price is not None:
                acc += type_price * sold
            else:
                continue
            total += sold
        if total > 0:
            price = acc / total
            level = f"по номенклатуре {covered / total * 100:.0f} % тоннажа, остальное — {level}"
        else:
            price = type_price
        row["model_level"], row["model_n"] = level, len(nb)
        cpt = _med([r["fact_series_pt"] for r in nb if r["series_full"] and r["fact_series_pt"]])
        oh = site_rate.get(row["site"])
        row["model_price"], row["model_costs_pt"], row["model_overhead_pt"] = price, cpt, oh
        if row.get("plan_t") and price is not None:
            t = row["plan_t"]
            row["model_rev"] = price * t
            row["model_costs"] = ((cpt or 0.0) + (oh or 0.0)) * t
            row["model_profit"] = row["model_rev"] - row["plan_purchase"] - row["model_costs"]
        # ошибки против факта: цена и затраты руб/т, прибыль в % выручки факта
        fact_costs_pt = ((row["fact_series"] or 0.0) + (row["fact_overhead"] or 0.0)) / row["sold_t"] \
            if row["series_full"] and row["fact_overhead"] is not None else None
        row["fact_costs_pt"] = fact_costs_pt
        row["plan_price_err"] = _pct(row.get("plan_price"), row["fact_price"])
        row["model_price_err"] = _pct(price, row["fact_price"])
        row["plan_costs_err"] = _pct(row.get("plan_costs_pt"), fact_costs_pt)
        row["model_costs_err"] = _pct(cpt, row["fact_series_pt"]) if row["series_full"] else None
        row["plan_profit_err"] = ((row["plan_profit"] / row["plan_rev"] * 100.0)
                                  - row["fact_profit"] / row["fact_rev"] * 100.0
                                  if row.get("plan_rev") and row["fact_profit"] is not None else None)
        row["model_profit_err"] = ((row["model_profit"] / row["model_rev"] * 100.0)
                                   - row["fact_profit"] / row["fact_rev"] * 100.0
                                   if row.get("model_rev") and row["fact_profit"] is not None else None)
    conn.execute("DELETE FROM stat_deal_audit")
    cols = ["deal_no", "name", "kind", "bp_type", "site", "year", "closed", "bought_t", "sold_t",
            "fact_rev", "fact_cos", "fact_price", "fact_gross_pct", "fact_series", "fact_series_pt",
            "series_items", "series_full", "fact_overhead", "fact_costs_pt", "fact_profit", "plan_version", "plan_file", "plan_t",
            "plan_rev", "plan_purchase", "plan_costs", "plan_profit", "plan_price", "plan_costs_pt",
            "plan_margin_pct", "model_level", "model_n", "model_price", "model_costs_pt",
            "model_overhead_pt", "model_rev", "model_costs", "model_profit", "plan_price_err",
            "model_price_err", "plan_costs_err", "model_costs_err", "plan_profit_err",
            "model_profit_err"]
    conn.executemany(
        f"INSERT INTO stat_deal_audit ({', '.join(cols)}, updated_at) "
        f"VALUES ({', '.join('?' * len(cols))}, datetime('now'))",
        [tuple(r.get(c) for c in cols) for r in rows])
    from . import refsources
    refsources.mark(conn, "stat_deal_audit", len(rows), Path(snapshot_path).name,
                    "снимок «Реализации» + архив книг + регистр затрат")
    return {"deals": len(rows), "with_book": sum(1 for r in rows if r.get("plan_rev")),
            "closed": sum(r["closed"] for r in rows),
            "with_costs": sum(1 for r in rows if r["fact_profit"] is not None)}


def rows(conn: sqlite3.Connection, bp_type: str | None = None, year: str | None = None,
         closed_only: bool = False, with_book: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM stat_deal_audit WHERE 1 = 1"
    args: list = []
    if bp_type:
        sql += " AND bp_type = ?"
        args.append(bp_type)
    if year:
        sql += " AND year = ?"
        args.append(year)
    if closed_only:
        sql += " AND closed = 1"
    if with_book:
        sql += " AND plan_rev IS NOT NULL"
    sql += " ORDER BY CAST(deal_no AS INTEGER) DESC"
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def for_deal(conn: sqlite3.Connection, deal_no: str | None) -> sqlite3.Row | None:
    if not deal_no:
        return None
    try:
        return conn.execute("SELECT * FROM stat_deal_audit WHERE deal_no = ?", (deal_no,)).fetchone()
    except sqlite3.Error:
        return None


def summary(conn: sqlite3.Connection) -> list[dict]:
    """По типам: сколько сделок, медианы ошибок экономиста и модели против
    факта (цена, затраты, прибыль) и медианы самих величин."""
    try:
        rs = [dict(r) for r in conn.execute(
            "SELECT * FROM stat_deal_audit WHERE plan_rev IS NOT NULL AND closed = 1")]
    except sqlite3.Error:
        return []
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        by[r["bp_type"] or ""].append(r)
        if r["bp_type"]:
            by[""].append(r)
    names = {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM ref_bp_types")}
    out = []
    for code, lst in by.items():
        def med_abs(key):
            return _med([abs(r[key]) for r in lst if r.get(key) is not None])
        def med_signed(key):
            return _med([r[key] for r in lst if r.get(key) is not None])
        out.append({
            "bp_type": code, "name": names.get(code) or {"": "Все сделки", "mixed": "Смешанные"}.get(code, code),
            "n": len(lst), "n_costs": sum(1 for r in lst if r["fact_profit"] is not None),
            "plan_price_err": med_abs("plan_price_err"), "model_price_err": med_abs("model_price_err"),
            "plan_price_bias": med_signed("plan_price_err"), "model_price_bias": med_signed("model_price_err"),
            "plan_costs_err": med_abs("plan_costs_err"), "model_costs_err": med_abs("model_costs_err"),
            "plan_costs_bias": med_signed("plan_costs_err"), "model_costs_bias": med_signed("model_costs_err"),
            "plan_profit_err": med_abs("plan_profit_err"), "model_profit_err": med_abs("model_profit_err"),
            "plan_profit_bias": med_signed("plan_profit_err"), "model_profit_bias": med_signed("model_profit_err"),
            "plan_margin": _med([r["plan_margin_pct"] for r in lst]),
            "fact_margin": _med([r["fact_profit"] / r["fact_rev"] * 100.0 for r in lst if r["fact_profit"] is not None and r["fact_rev"]]),
            "fact_gross": _med([r["fact_gross_pct"] for r in lst]),
            "plan_costs_pt": _med([r["plan_costs_pt"] for r in lst]),
            "fact_costs_pt": _med([r["fact_costs_pt"] for r in lst if r["fact_costs_pt"] is not None]),
        })
    return sorted(out, key=lambda r: (r["bp_type"] != "", -r["n"]))


def years(conn: sqlite3.Connection) -> list[str]:
    try:
        return [r["year"] for r in conn.execute(
            "SELECT DISTINCT year FROM stat_deal_audit WHERE year IS NOT NULL ORDER BY year")]
    except sqlite3.Error:
        return []


XLSX_COLS = [
    ("deal_no", "№ запроса", None), ("name", "Контрагент", None), ("kind", "Вид", None),
    ("bp_type", "Тип", None), ("site", "Площадка", None), ("year", "Год", None),
    ("closed", "Закрыта", None), ("series_full", "Факт затрат полный", None),
    ("plan_version", "Версия книги", None),
    ("plan_t", "Книга: тоннаж, т", "0.0"), ("plan_rev", "Книга: выручка", "#,##0"),
    ("plan_purchase", "Книга: закупка", "#,##0"), ("plan_costs", "Книга: затраты", "#,##0"),
    ("plan_profit", "Книга: прибыль", "#,##0"), ("plan_price", "Книга: цена, руб/т", "#,##0"),
    ("plan_costs_pt", "Книга: затраты, руб/т", "#,##0"), ("plan_margin_pct", "Книга: рентабельность, %", "0.0"),
    ("bought_t", "Факт: куплено, т", "0.0"), ("sold_t", "Факт: продано, т", "0.0"),
    ("fact_rev", "Факт: выручка", "#,##0"), ("fact_cos", "Факт: себестоимость продаж", "#,##0"),
    ("fact_series", "Факт: затраты по серии", "#,##0"), ("fact_overhead", "Факт: распределяемые", "#,##0"),
    ("fact_profit", "Факт: прибыль", "#,##0"), ("fact_price", "Факт: цена, руб/т", "#,##0"),
    ("fact_costs_pt", "Факт: затраты, руб/т", "#,##0"), ("fact_gross_pct", "Факт: валовая, %", "0.0"),
    ("model_price", "Модель: цена, руб/т", "#,##0"), ("model_costs_pt", "Модель: серия, руб/т", "#,##0"),
    ("model_overhead_pt", "Модель: распределяемые, руб/т", "#,##0"), ("model_rev", "Модель: выручка", "#,##0"),
    ("model_costs", "Модель: затраты", "#,##0"), ("model_profit", "Модель: прибыль", "#,##0"),
    ("model_level", "Модель: по чему", None),
    ("plan_price_err", "Книга − факт: цена, %", "0.0"), ("model_price_err", "Модель − факт: цена, %", "0.0"),
    ("plan_costs_err", "Книга − факт: затраты, %", "0.0"), ("model_costs_err", "Модель − факт: серия, %", "0.0"),
    ("plan_profit_err", "Книга − факт: рентабельность, п.п.", "0.0"),
    ("model_profit_err", "Модель − факт: рентабельность, п.п.", "0.0"),
]


def to_xlsx(conn: sqlite3.Connection, path: str | Path) -> Path:
    """Вся сверка в xlsx: лист по сделкам и лист сводки по типам."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    wb = Workbook()
    ws = wb.active
    ws.title = "По сделкам"
    ws.append([title for _k, title, _fmt in XLSX_COLS])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows(conn, with_book=False):
        ws.append([r[k] for k, _t, _f in XLSX_COLS])
    for i, (_k, _t, fmt) in enumerate(XLSX_COLS, start=1):
        col = ws.cell(row=1, column=i).column_letter
        ws.column_dimensions[col].width = 14
        if fmt:
            for cell in ws[col][1:]:
                cell.number_format = fmt
    ws.freeze_panes = "B2"
    ws2 = wb.create_sheet("Сводка по типам")
    head = ["Тип", "Сделок", "С полным фактом затрат", "Книга: рентабельность, %", "Факт: рентабельность, %",
            "Факт: валовая, %", "Книга: затраты, руб/т", "Факт: затраты, руб/т",
            "Книга − факт: цена, % (модуль)", "Модель − факт: цена, % (модуль)",
            "Книга: смещение цены, %", "Модель: смещение цены, %",
            "Книга − факт: затраты, % (модуль)", "Модель − факт: серия, % (модуль)",
            "Книга: смещение затрат, %", "Модель: смещение серии, %",
            "Книга − факт: рентабельность, п.п. (модуль)", "Модель − факт: рентабельность, п.п. (модуль)",
            "Книга: смещение рентабельности, п.п.", "Модель: смещение рентабельности, п.п."]
    ws2.append(head)
    for c in ws2[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for s in summary(conn):
        ws2.append([s["name"], s["n"], s["n_costs"], s["plan_margin"], s["fact_margin"], s["fact_gross"],
                    s["plan_costs_pt"], s["fact_costs_pt"], s["plan_price_err"], s["model_price_err"],
                    s["plan_price_bias"], s["model_price_bias"], s["plan_costs_err"], s["model_costs_err"],
                    s["plan_costs_bias"], s["model_costs_bias"], s["plan_profit_err"], s["model_profit_err"],
                    s["plan_profit_bias"], s["model_profit_bias"]])
    for i in range(1, len(head) + 1):
        ws2.column_dimensions[ws2.cell(row=1, column=i).column_letter].width = 16
        for cell in ws2[ws2.cell(row=1, column=i).column_letter][1:]:
            if i > 2:
                cell.number_format = "0.0"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
