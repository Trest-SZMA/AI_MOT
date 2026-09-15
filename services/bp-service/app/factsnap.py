"""Факт реализации по сделке — снимок из сервиса «Реализация».

«Реализация» (тот же сервер, порт 8092) ночью пересобирает из регистров 1С
факт по каждому бизнес-плану: куплено и продано по номенклатуре, выручка и
себестоимость продаж, плановая выручка книги экономиста по трекам «лук»/«дсп»
и доля лота. Итог лежит в `out/sales_data.json` (ключ `bpbuy`, ~1 000 БП).
Считать то же самое второй раз из 300-мегабайтных регистров незачем: снимок
берётся готовым, привязка — по номеру запроса (`deals.request_no`).

Снимки копятся по дате сборки (UNIQUE bp_id + generated_at): по ним видно,
как факт догоняет план. Страница «План-факт» показывает последний.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import deals
from .calc import _f


def load(path: str | Path) -> tuple[dict, dict[str, dict]]:
    """sales_data.json → (meta, {номер запроса: запись bpbuy})."""
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    by_no: dict[str, dict] = {}
    for r in d.get("bpbuy") or []:
        no = str(r.get("bp") or "").strip()
        if no and no not in by_no:          # первая запись — головная серия
            by_no[no] = r
    return d.get("meta") or {}, by_no


def _rec_to_row(bp_id: int, no: str, r: dict, meta: dict, source: str) -> dict:
    rev = r.get("rev") or {}
    luk, dsp = rev.get("luk") or {}, rev.get("dsp") or {}
    deal = r.get("deal") or {}
    share = deal.get("share")
    items = [{"code": n.get("c"), "name": n.get("n"), "bought_t": _f(n.get("t")),
              "sold_t": _f(n.get("s")), "revenue": _f(n.get("r")),
              "warehouses": n.get("w") or []} for n in (r.get("nm") or [])]
    return {"bp_id": bp_id, "deal_no": no, "generated_at": meta.get("generated") or "",
            "dump_dt": meta.get("dump_dt"), "series_name": r.get("name"),
            "bought_t": _f(r.get("t")), "sold_t": _f(r.get("sold")),
            "revenue": _f(r.get("rub")), "cost_of_sales": _f(r.get("cost")),
            "share_pct": (float(share) * 100.0 if share is not None else None),
            "plan_rev_luk": luk.get("v"), "plan_rev_dsp": dsp.get("v"),
            "plan_t_luk": luk.get("pt"), "plan_t_dsp": dsp.get("pt"),
            "plan_ver_luk": luk.get("ver"), "plan_ver_dsp": dsp.get("ver"),
            "items_json": json.dumps(items, ensure_ascii=False), "source_file": source}


def import_file(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Снимок для всех неархивных БП с номером запроса. -> статистика."""
    meta, by_no = load(path)
    st = {"generated": meta.get("generated"), "bps_in_file": len(by_no),
          "checked": 0, "matched": 0, "written": 0}
    for bp in conn.execute("SELECT * FROM business_plans WHERE COALESCE(archived, 0) = 0"):
        st["checked"] += 1
        no = deals.request_no(bp)
        r = by_no.get(no or "")
        if r is None:
            continue
        st["matched"] += 1
        row = _rec_to_row(bp["id"], no, r, meta, Path(path).name)
        cur = conn.execute(
            "INSERT INTO bp_fact_snapshot (bp_id, deal_no, generated_at, dump_dt, "
            "series_name, bought_t, sold_t, revenue, cost_of_sales, share_pct, "
            "plan_rev_luk, plan_rev_dsp, plan_t_luk, plan_t_dsp, plan_ver_luk, "
            "plan_ver_dsp, items_json, source_file, taken_at) VALUES (:bp_id, :deal_no, "
            ":generated_at, :dump_dt, :series_name, :bought_t, :sold_t, :revenue, "
            ":cost_of_sales, :share_pct, :plan_rev_luk, :plan_rev_dsp, :plan_t_luk, "
            ":plan_t_dsp, :plan_ver_luk, :plan_ver_dsp, :items_json, :source_file, "
            "datetime('now')) ON CONFLICT(bp_id, generated_at) DO UPDATE SET "
            "bought_t = excluded.bought_t, sold_t = excluded.sold_t, "
            "revenue = excluded.revenue, cost_of_sales = excluded.cost_of_sales, "
            "share_pct = excluded.share_pct, items_json = excluded.items_json, "
            "plan_rev_luk = excluded.plan_rev_luk, plan_rev_dsp = excluded.plan_rev_dsp, "
            "plan_t_luk = excluded.plan_t_luk, plan_t_dsp = excluded.plan_t_dsp, "
            "plan_ver_luk = excluded.plan_ver_luk, plan_ver_dsp = excluded.plan_ver_dsp, "
            "taken_at = datetime('now')", row)
        st["written"] += cur.rowcount
    from . import refsources
    refsources.mark(conn, "bp_fact_snapshot", st["matched"], Path(path).name,
                    "снимок «Реализации»")
    return st


def latest(conn: sqlite3.Connection, bp_id: int) -> sqlite3.Row | None:
    try:
        return conn.execute(
            "SELECT * FROM bp_fact_snapshot WHERE bp_id = ? "
            "ORDER BY generated_at DESC, id DESC LIMIT 1", (bp_id,)).fetchone()
    except sqlite3.Error:
        return None


def history(conn: sqlite3.Connection, bp_id: int, limit: int = 12) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT generated_at, bought_t, sold_t, revenue, cost_of_sales "
            "FROM bp_fact_snapshot WHERE bp_id = ? ORDER BY generated_at DESC LIMIT ?",
            (bp_id, limit)).fetchall()
    except sqlite3.Error:
        return []


def _pct(a: float, b: float) -> float | None:
    return a / b * 100.0 if b else None


def compare(conn: sqlite3.Connection, bp, items: list, pnl: dict) -> dict:
    """План (наш P&L на долю) против факта снимка — для страницы «План-факт».

    План книги экономиста (весь лот) приводится к доле так же, как это делает
    «Реализация»: план × доля. Позиции факта сопоставляются с планом по имени
    номенклатуры 1С (`bp_items.nomen_1c`), кода 1С в перечне нет.
    """
    snap = latest(conn, bp["id"])
    out = {"snapshot": snap, "deal_no": deals.request_no(bp), "history": [],
           "items": [], "plan": None, "fact": None, "ratios": None}
    if snap is None:
        return out
    share = _f(pnl.get("lot_share_pct")) / 100.0 if pnl.get("lot_share_pct") else 1.0
    plan = {"purchase_t": _f(pnl.get("purchase_volume")), "sale_t": _f(pnl.get("sale_volume")),
            "revenue": _f(pnl.get("revenue")), "revenue_per_t": _f(pnl.get("revenue_per_t")),
            "book_luk": (_f(snap["plan_rev_luk"]) * share if snap["plan_rev_luk"] else None),
            "book_dsp": (_f(snap["plan_rev_dsp"]) * share if snap["plan_rev_dsp"] else None),
            "book_ver_luk": snap["plan_ver_luk"], "book_ver_dsp": snap["plan_ver_dsp"],
            "share_pct": share * 100.0}
    fact = {"bought_t": _f(snap["bought_t"]), "sold_t": _f(snap["sold_t"]),
            "revenue": _f(snap["revenue"]), "cost_of_sales": _f(snap["cost_of_sales"])}
    fact["revenue_per_t"] = fact["revenue"] / fact["sold_t"] if fact["sold_t"] else 0.0
    fact["gross"] = fact["revenue"] - fact["cost_of_sales"]
    fact["gross_pct"] = _pct(fact["gross"], fact["revenue"])
    ratios = {"revenue_pct": _pct(fact["revenue"], plan["revenue"]),
              "bought_pct": _pct(fact["bought_t"], plan["purchase_t"]),
              "sold_pct": _pct(fact["sold_t"], plan["sale_t"]),
              "price_pct": _pct(fact["revenue_per_t"], plan["revenue_per_t"]),
              "book_luk_pct": _pct(fact["revenue"], plan["book_luk"] or 0.0),
              "book_dsp_pct": _pct(fact["revenue"], plan["book_dsp"] or 0.0)}
    # Позиции: факт по номенклатуре + план той же номенклатуры (по имени 1С).
    plan_by_name: dict[str, dict] = {}
    for it in items:
        name = (it["nomen_1c"] or it["nomenclature"] or "").strip().lower()
        if not name:
            continue
        p = plan_by_name.setdefault(name, {"volume_t": 0.0, "revenue": 0.0,
                                           "name": (it["nomen_1c"] or it["nomenclature"]).strip()})
        vol = _f(it["volume_t"]) * share
        p["volume_t"] += vol
        p["revenue"] += vol * _f(it["sale_price"])
    rows = []
    try:
        fact_items = json.loads(snap["items_json"] or "[]")
    except ValueError:
        fact_items = []
    for fi in sorted(fact_items, key=lambda x: -_f(x.get("revenue"))):
        name = (fi.get("name") or "").strip()
        p = plan_by_name.pop(name.lower(), None)
        sold = _f(fi.get("sold_t"))
        rows.append({"code": fi.get("code"), "name": name, "bought_t": _f(fi.get("bought_t")),
                     "sold_t": sold, "revenue": _f(fi.get("revenue")),
                     "price_per_t": _f(fi.get("revenue")) / sold if sold else None,
                     "plan_t": p["volume_t"] if p else None,
                     "plan_price": (p["revenue"] / p["volume_t"] if p and p["volume_t"] else None),
                     "warehouses": fi.get("warehouses") or []})
    # Плановые позиции, которых в факте ещё нет.
    for name, p in plan_by_name.items():
        if p["volume_t"] > 0:
            rows.append({"code": None, "name": p["name"], "bought_t": 0.0, "sold_t": 0.0,
                         "revenue": 0.0, "price_per_t": None, "plan_t": p["volume_t"],
                         "plan_price": p["revenue"] / p["volume_t"], "warehouses": [],
                         "plan_only": True})
    out.update({"plan": plan, "fact": fact, "ratios": ratios, "items": rows,
                "history": history(conn, bp["id"])})
    return out
