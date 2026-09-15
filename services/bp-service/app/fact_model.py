"""Фактический слой модели затрат: суммы статей от фактических ставок.

Указание директора: понять реальные затраты и включить их в алгоритм так,
чтобы они корректировались при изменениях. Сверка по трём закрытым сделкам
15.09.2026 показала, почему план и факт не совпадают по структуре:

  * транспорт, ПРР, своя техника, засор — в 1С привязаны к серии (сделке);
    план завышал их у трубы и лома втрое, а у кабеля вовсе не заполнял;
  * персонал, амортизация, аренда, охрана — в 1С к сделке не привязаны, это
    распределяемые расходы подразделения; по сделке они всегда «0»;
  * «списание засора» в 1С — деньги, в плане — только объём.

Здесь каждая статья считается как СТАВКА ФАКТА × ТОННАЖ СДЕЛКИ на долю:

  статьи по серии      — медиана руб/т по закрытым сделкам того же типа
                         (stat_cost_matrix, источник «факт 1С: регистр затрат»);
  распределяемые       — ставка руб/т подразделения-базы сделки за 12 закрытых
                         месяцев (stat_overhead_div / stat_division_tons),
                         разнесённая на статьи персонала и постоянных в
                         пропорции факта подразделения;
  засор деньгами       — отдельная статья «Списание засора», только факт.

Ставки обновляются ночью; суммы зависят от тоннажа, доли, типа и базы
сделки, поэтому меняются вместе с ними. Нормативная модель (norms.py)
остаётся рядом: она объясняет структуру, факт даёт уровень.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from . import bp_types, cost_matrix, fact_costs
from .calc import _f, _row_get, lot_share

SOURCE = "факт 1С: регистр затрат"
# Статьи персонала и постоянных, на которые раскладывается ставка
# распределяемых подразделения — по группам статей 1С.
OVERHEAD_SPLIT = [
    ("Персонал", "Зарплата", ("фот", "оплата труда", "зарплат", "премии")),
    ("Персонал", "Прочие расходы на персонал", ("страховые взносы", "налоги с зп", "спецодежда",
                                               "обучение", "медосмотр")),
    ("Персонал", "Командировочные расходы", ("суточные", "проезд", "командиров")),
    ("Персонал", "Аренда квартир (проживание)", ("проживание", "аренда квартир", "гостиниц")),
    ("Постоянные", "Амортизационные отчисления (транспорт, оборудование)", ("амортизац",)),
    ("Постоянные", "Аренда баз, коммунальные расходы, охрана", ("аренда", "охран", "коммунал",
                                                                "электроэнерг", "отоплен")),
    ("Постоянные", "Оборудование и инструменты", ("оборудован", "инструмент", "инвентарь")),
]
OVERHEAD_OTHER = ("Постоянные", "Прочие производственные расходы")


def deal_tons(bp, items: list) -> float:
    """Тоннаж сделки на нашу долю — база для всех ставок руб/т."""
    return sum(_f(it["volume_t"]) for it in items) * lot_share(bp)


def deal_bases(items: list) -> list[str]:
    """Базы сделки по позициям (division/warehouse) — для распределяемых."""
    from .calc import base_of
    seen: list[str] = []
    for it in items:
        b = (base_of(it) or "").strip()
        if b and b not in seen:
            seen.append(b)
    return seen


def _series_rates(conn: sqlite3.Connection, bp_type: str | None) -> dict[tuple, sqlite3.Row]:
    """(секция, статья) → строка матрицы из регистра: свой тип, иначе общая."""
    rows = conn.execute(
        "SELECT * FROM stat_cost_matrix WHERE source = ? AND bp_type IN (?, '') "
        "ORDER BY bp_type = '' DESC", (SOURCE, bp_type or "")).fetchall()
    out: dict[tuple, sqlite3.Row] = {}
    for r in rows:                              # общие первыми, свой тип перекрывает
        out[(r["section"], r["item"])] = r
    return out


def _split_items(items_1c: list[tuple[str, float]]) -> dict[tuple, float]:
    """Статьи 1С площадки → доли статей сервиса (персонал / постоянные)."""
    shares: dict[tuple, float] = defaultdict(float)
    total = 0.0
    for name_raw, a in items_1c:
        name = (name_raw or "").lower()
        if a <= 0:
            continue
        key = OVERHEAD_OTHER
        for section, item, words in OVERHEAD_SPLIT:
            if any(w in name for w in words):
                key = (section, item)
                break
        shares[key] += a
        total += a
    return {k: v / total for k, v in shares.items()} if total else {}


def detect_site(conn: sqlite3.Connection, bp) -> dict:
    """Площадка сделки по факту: подразделение регистра затрат с наибольшей
    суммой по серии сделки → площадка из «Цеха и базы». Нет серии — None."""
    from . import deals
    no = deals.request_no(bp)
    if not no:
        return {"site": None, "reason": "номер запроса не найден"}
    smap = fact_costs.site_of_division(conn)
    rows = conn.execute(
        "SELECT division, SUM(amount) AS a FROM stat_fact_costs_div WHERE deal_no = ? "
        "GROUP BY division ORDER BY a DESC", (no,)).fetchall() if _has_div_table(conn) else []
    for r in rows:
        site = smap.get(r["division"])
        if site:
            return {"site": site, "reason": f"по регистру затрат: {r['division']} ({r['a'] / 1e3:,.0f} тыс.)".replace(",", " ")}
    return {"site": None, "reason": "в регистре затрат нет строк по серии" if not rows
            else "подразделения серии не привязаны к площадке"}


def _has_div_table(conn) -> bool:
    try:
        conn.execute("SELECT 1 FROM stat_fact_costs_div LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def resolve_site(conn: sqlite3.Connection, bp) -> tuple[str | None, str]:
    """Площадка сделки: заданная вручную, иначе по факту."""
    site = _row_get(bp, "site")
    if site and site != "auto":
        return site, "задана вручную"
    d = detect_site(conn, bp)
    return d["site"], d["reason"]


def evaluate(bp, items: list, conn: sqlite3.Connection) -> dict:
    """Суммы статей по факту для сделки.

    -> {articles: {(section, item): amount}, lines: [...], tons, share_pct,
        bp_type, overhead: {...}, missing: [...]}
    """
    tons = deal_tons(bp, items)
    bp_type = _row_get(bp, "bp_type") or bp_types.detect(conn, items).get("code")
    lines: list[dict] = []
    articles: dict[tuple, float] = defaultdict(float)
    missing: list[str] = []
    if tons <= 0:
        return {"articles": {}, "lines": [], "tons": 0.0, "share_pct": lot_share(bp) * 100,
                "bp_type": bp_type, "overhead": None, "missing": ["нет тоннажа"]}

    # 1. Статьи по серии — ставка матрицы × тоннаж.
    rates = _series_rates(conn, bp_type)
    for (section, item), r in rates.items():
        amount = float(r["rub_per_t"] or 0) * tons
        if amount <= 0.5:
            continue
        articles[(section, item)] += amount
        lines.append({"section": section, "item": item, "amount": round(amount, 2),
                      "rate": float(r["rub_per_t"]), "basis": "тоннаж сделки на долю",
                      "samples": r["samples"], "scope": ("тип " + r["bp_type"]) if r["bp_type"] else "все типы",
                      "source": "регистр затрат по сериям"})

    # 2. Распределяемые — ставка ПЛОЩАДКИ сделки × тоннаж, разложенная по статьям.
    overhead = None
    site, site_reason = resolve_site(conn, bp)
    matched = next((d for d in fact_costs.site_overhead_rates(conn) if d["site"] == site), None) if site else None
    if matched and matched.get("rate_per_t"):
        split = _split_items(matched["items"])
        total = float(matched["rate_per_t"]) * tons
        overhead = {"site": site, "site_reason": site_reason, "rate_per_t": matched["rate_per_t"],
                    "amount": round(total, 2), "months": matched["months"],
                    "tons_site": matched["tons"], "divisions": matched["divisions"], "split": []}
        for key, share in sorted(split.items(), key=lambda kv: -kv[1]):
            amount = total * share
            if amount <= 0.5:
                continue
            articles[key] += amount
            overhead["split"].append({"section": key[0], "item": key[1],
                                      "share_pct": round(share * 100, 1), "amount": round(amount, 2)})
            lines.append({"section": key[0], "item": key[1], "amount": round(amount, 2),
                          "rate": round(float(matched["rate_per_t"]) * share, 2),
                          "basis": "тоннаж сделки на долю", "samples": matched["months"],
                          "scope": site, "source": "распределяемые площадки (12 мес)"})
    else:
        missing.append("распределяемые: площадка сделки не определена — " + site_reason
                       + ". Задайте площадку в шапке сделки.")

    if not rates:
        missing.append("матрица факта по сериям пуста — ночная выгрузка ещё не прошла")

    return {"articles": {k: round(v, 2) for k, v in articles.items()},
            "lines": sorted(lines, key=lambda x: (x["section"], -x["amount"])),
            "tons": round(tons, 3), "share_pct": round(lot_share(bp) * 100, 2),
            "bp_type": bp_type, "site": site, "site_reason": site_reason,
            "overhead": overhead, "missing": missing,
            "total": round(sum(articles.values()), 2)}


def write(conn: sqlite3.Connection, bp, items: list, variant: str = "bsp",
          author: str | None = None) -> dict:
    """Записать суммы факта в bp_costs выбранного варианта (явная кнопка).

    Статья «Списание засора» в bp_costs не пишется: в плане засор режет
    объём, деньги по нему — только в сверке. Остальные статьи модели, по
    которым факта нет, обнуляются, чтобы не смешивать источники.
    """
    from . import origin
    res = evaluate(bp, items, conn)
    if not res["articles"]:
        return res
    col = "amount_luk" if variant == "luk" else "amount"
    existing = {(c["section"], c["item"]): c["id"] for c in conn.execute(
        "SELECT id, section, item FROM bp_costs WHERE bp_id = ?", (bp["id"],)).fetchall()}
    stamp = conn.execute("SELECT MAX(updated_at) AS u FROM stat_cost_matrix").fetchone()["u"]
    ref = f"факт 1С, ставки на {stamp[:16]}" if stamp else "факт 1С"
    written = 0
    for (section, item), amount in res["articles"].items():
        if item == fact_costs.CONTAM:
            continue
        detail = "; ".join(f"{l['source']}: {l['rate']:,.0f} руб/т".replace(",", " ")
                           for l in res["lines"] if (l["section"], l["item"]) == (section, item))
        key = (section, item)
        if key in existing:
            cost_id = existing[key]
            conn.execute(f"UPDATE bp_costs SET {col} = ?, comment = ? WHERE id = ?",
                         (amount, f"по факту: {detail}"[:300], cost_id))
        else:
            cur = conn.execute(
                f"INSERT INTO bp_costs (bp_id, section, item, {col}, comment) VALUES (?, ?, ?, ?, ?)",
                (bp["id"], section, item, amount, f"по факту: {detail}"[:300]))
            cost_id = cur.lastrowid
        origin.set_origin(conn, cost_id, variant, origin.FACT, ref, amount, author=author)
        written += 1
    res["written"] = written
    return res
