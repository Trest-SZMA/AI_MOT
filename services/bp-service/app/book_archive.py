"""Архив книг экономистов из Битрикса: «что закладывали» по всем сделкам.

Вопрос директора 15.09.2026: почему нормативы взяты из одной книги, если
есть база бизнес-планов и парсер. Парсер (`metoptorg-bp-weekly`, сосед
«Реализация», файл `data/БП_версии_<дата>.json`) вытаскивает из каждой
версии книги: выручку, прибыль и позиции с объёмом, ценой и стоимостью
закупки. Статей затрат в нём нет — их парсер не разбирает, поэтому
нормативы (руб/л, ед/т …) из архива не вывести. Но итог затрат версии
считается как остаток:

    затраты плана = выручка − Σ закупка позиций − прибыль

По 1 600 сделкам и 4 900 версиям это даёт плановые затраты руб/т и долю
затрат в выручке — по типам сделок. Это «что закладывали» по всей истории
книг (2021–2026), против которого ставится факт регистра 1С.

Версии с неполным разбором позиций (Σ объём позиций сильно меньше тоннажа
в имени файла) отбрасываются: у них «закупка» — часть лота, и остаток
бессмыслен. Тип сделки — по снимку «Реализации» (группы 1С), иначе по
названию номенклатуры позиций.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

SOURCE = "книги экономистов (архив)"
_NO = re.compile(r"БП\s*(\d{3,4})")
_TONS_IN_NAME = re.compile(r"(\d[\d\s]*[.,]?\d*)\s*тн", re.IGNORECASE)


def newest(folder: str | Path) -> Path | None:
    files = sorted(Path(folder).glob("БП_версии_*.json"))
    return files[-1] if files else None


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _type_by_items(items: list, gmap: dict[str, str]) -> str | None:
    """Тип по названиям позиций, если снимок сделку не знает."""
    tons: dict[str, float] = defaultdict(float)
    for it in items:
        name = (str(it.get("nomenclature") or "") + " " + str(it.get("category") or "")).lower()
        vol = _f(it.get("volume"))
        if vol <= 0:
            continue
        if "кабел" in name:
            tons["cable"] += vol
        elif any(w in name for w in ("труб", "нкт", "штанг")):
            tons["pipe"] += vol
        elif any(w in name for w in ("медь", "латун", "алюмин", "цвет", "бронз", "свин")):
            tons["nonferrous"] += vol
        elif any(w in name for w in ("лом", "5а", "12а", "3а", "чм")):
            tons["ferrous"] += vol
    if not tons:
        return None
    code, top = max(tons.items(), key=lambda kv: kv[1])
    return code if top / sum(tons.values()) >= 0.8 else "mixed"


def load(path: str | Path, conn: sqlite3.Connection, snapshot_path: str | Path | None) -> list[dict]:
    """Версии книг → записи {no, bp_type, rev, purchase, profit, costs, vol, file, ver}."""
    from . import type_margin
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    # тип сделки по снимку «Реализации»
    type_by_no: dict[str, str] = {}
    if snapshot_path and Path(snapshot_path).is_file():
        with open(snapshot_path, encoding="utf-8") as fh:
            snap = json.load(fh)
        gmap = type_margin.group_map(conn)
        bp_nm = snap.get("bp_nm") or {}
        for r in snap.get("bpbuy") or []:
            no = str(r.get("bp") or "")
            groups = ((bp_nm.get(r.get("s")) or {}).get("т") or {}).get("g") or {}
            total = sum(float(v) for v in groups.values())
            if not no or total <= 0 or no in type_by_no:
                continue
            tons: dict[str, float] = defaultdict(float)
            for g, v in groups.items():
                c = gmap.get(str(g).strip().lower())
                if c:
                    tons[c] += float(v)
            if tons:
                c, top = max(tons.items(), key=lambda kv: kv[1])
                type_by_no[no] = c if top / total >= 0.8 else "mixed"
    out = []
    for r in d.get("rows") or []:
        if r.get("status") != "ОК":
            continue
        rev, profit = _f(r.get("revenue")), r.get("profit")
        if rev <= 0 or profit is None:
            continue
        m = _NO.match(r.get("name") or "")
        if not m:
            continue
        no = m.group(1)
        items = [it for it in (r.get("items") or []) if _f(it.get("volume")) > 0 and _f(it.get("cost")) > 0]
        vol = sum(_f(it["volume"]) for it in items)
        purchase = sum(_f(it["cost"]) for it in items)
        if vol <= 0 or purchase <= 0 or purchase >= rev:
            continue
        # тоннаж в имени файла — контроль полноты разбора позиций
        mt = _TONS_IN_NAME.search(r.get("file") or "")
        if mt:
            named = _f(mt.group(1).replace(" ", "").replace(",", "."))
            if named > 0 and vol < 0.7 * named:
                continue
        costs = rev - purchase - _f(profit)
        if costs < 0 or costs > rev:
            continue
        out.append({"no": no, "bp_type": type_by_no.get(no) or _type_by_items(items, {}),
                    "rev": rev, "purchase": purchase, "profit": _f(profit), "costs": costs,
                    "vol": vol, "file": r.get("file"), "ver": r.get("sheet"),
                    "luk": r.get("lukoil") == "да", "dsp": r.get("dsp") == "да",
                    "year": (re.search(r"(20\d\d)", r.get("file") or "") or [None, None])[1]})
    return out


def build(conn: sqlite3.Connection, path: str | Path, snapshot_path: str | Path | None) -> dict:
    """Архив → stat_book_plan (по версиям) и сводка по типам в stat_type_plan."""
    rows = load(path, conn, snapshot_path)
    conn.execute("DELETE FROM stat_book_plan")
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO stat_book_plan (deal_no, bp_type, version, file, year, revenue, "
            "purchase, profit, costs, volume_t, costs_per_t, costs_pct, gross_pct, luk, dsp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["no"], r["bp_type"], r["ver"], r["file"], r["year"], r["rev"], r["purchase"],
             r["profit"], r["costs"], r["vol"], r["costs"] / r["vol"], r["costs"] / r["rev"] * 100,
             (r["rev"] - r["purchase"]) / r["rev"] * 100, int(r["luk"]), int(r["dsp"])))
    # сводка по типам: медианы, одна запись на сделку (последняя версия)
    last: dict[str, dict] = {}
    for r in rows:
        last[r["no"]] = r
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in last.values():
        by_type[r["bp_type"] or ""].append(r)
        by_type[""].append(r) if r["bp_type"] else None
    conn.execute("DELETE FROM stat_type_plan")
    for code, rs in by_type.items():
        cpt = [x["costs"] / x["vol"] for x in rs]
        cpc = [x["costs"] / x["rev"] * 100 for x in rs]
        gm = [(x["rev"] - x["purchase"]) / x["rev"] * 100 for x in rs]
        pm = [x["profit"] / x["rev"] * 100 for x in rs]
        conn.execute(
            "INSERT INTO stat_type_plan (bp_type, n, costs_per_t, costs_pct, gross_pct, profit_pct, "
            "generated_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (code, len(rs), round(median(cpt), 2), round(median(cpc), 2), round(median(gm), 2),
             round(median(pm), 2)))
    from . import refsources
    refsources.mark(conn, "stat_book_plan", len(rows), Path(path).name, "архив книг (парсер Битрикса)")
    return {"versions": len(rows), "deals": len(last), "types": len(by_type)}


def type_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT p.*, COALESCE(t.name, CASE p.bp_type WHEN '' THEN 'Все сделки' "
            "WHEN 'mixed' THEN 'Смешанные' ELSE p.bp_type END) AS name, COALESCE(t.sort, 900) AS sort "
            "FROM stat_type_plan p LEFT JOIN ref_bp_types t ON t.code = p.bp_type "
            "ORDER BY p.bp_type = '' DESC, sort").fetchall()
    except sqlite3.Error:
        return []


def for_type(conn: sqlite3.Connection, bp_type: str | None) -> sqlite3.Row | None:
    try:
        r = conn.execute("SELECT * FROM stat_type_plan WHERE bp_type = ?", (bp_type or "",)).fetchone()
        return r or conn.execute("SELECT * FROM stat_type_plan WHERE bp_type = ''").fetchone()
    except sqlite3.Error:
        return None


def for_deal(conn: sqlite3.Connection, deal_no: str | None) -> list[sqlite3.Row]:
    if not deal_no:
        return []
    try:
        return conn.execute("SELECT * FROM stat_book_plan WHERE deal_no = ? ORDER BY file, version",
                            (deal_no,)).fetchall()
    except sqlite3.Error:
        return []
