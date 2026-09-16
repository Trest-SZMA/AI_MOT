"""Определение цены реализации и прогноз.

Почему не «средняя по типу груза». В выгрузке 1С за 2024–2026 гг. одна и та
же труба уходит по совершенно разным ценам: НКТ Северскому трубному —
18 610 руб/тн, трубы больших диаметров — 32 897 руб/тн. Среднее по всем
трубным группам (22 740) не описывает ни одну реальную сделку, и строить на
нём план продажи нельзя.

Цена определяется тремя вещами:
  * ЧТО продаём — группа аналитического учёта (не «тип закупки»: труба,
    проданная ломом, стоит как лом, а не как труба);
  * КОМУ и КУДА — покупатель (канал сбыта) и дивизион;
  * КОГДА — рынок меняется, сделки прошлого года не равны сегодняшним.

Отсюда модель: ориентир берётся по самому узкому разрезу, где есть данные
(группа+дивизион+покупатель → группа+дивизион → группа), свежие месяцы
весят больше, а прогноз = свежая цена + тренд. План продажи в книгах
экономистов пишется как «текущая цена минус снижение» — эта же логика здесь:
`base` (текущая) и `delta` (снижение) дают плановую цену.
"""
from __future__ import annotations

import sqlite3
from datetime import date

# Свежесть: цены старше горизонта в ориентир не берём вовсе, внутри
# горизонта вес убывает вдвое каждые FRESH_HALFLIFE месяцев (сделка
# полугодовой давности весит вдвое меньше вчерашней).
HORIZON_MONTHS = 24
FRESH_HALFLIFE = 6.0
# Тренд считаем по последним TREND_WINDOW месяцам против предыдущих таких же.
TREND_WINDOW = 3
MIN_TREND_QTY = 20.0        # тонн в окне: меньше — выборка не показательна
# Потолок сдвига прогноза. На тонкой выборке тренд легко даёт +20% и более
# (цветной лом), а перенос такого скачка на период вперёд превращает прогноз
# в фантазию. Тренд показываем как есть, прогноз двигаем не более чем на
# столько процентов от свежей цены.
TREND_CAP_PCT = 15.0


def _month_index(period: str) -> int:
    """YYYY-MM → порядковый номер месяца (для расстояний между периодами)."""
    try:
        y, m = period.split("-")
        return int(y) * 12 + int(m)
    except (ValueError, AttributeError):
        return 0


def _now_index() -> int:
    today = date.today()
    return today.year * 12 + today.month


def _weighted(rows: list[sqlite3.Row], now_idx: int) -> dict | None:
    """Средневзвешенная цена: вес = объём × свежесть."""
    num = den = qty = 0.0
    samples = 0
    periods: list[str] = []
    for r in rows:
        q = float(r["total_qty_t"] or 0)
        price = float(r["rub_per_t"] or 0)
        if q <= 0 or price <= 0:
            continue
        age = max(0, now_idx - _month_index(r["period"]))
        if age > HORIZON_MONTHS:
            continue
        fresh = 0.5 ** (age / FRESH_HALFLIFE)
        num += price * q * fresh
        den += q * fresh
        qty += q
        samples += int(r["samples"] or 0)
        periods.append(r["period"])
    if den <= 0:
        return None
    return {"price": num / den, "qty": qty, "samples": samples,
            "period_from": min(periods), "period_to": max(periods)}


def _trend(rows: list[sqlite3.Row], now_idx: int) -> dict | None:
    """Тренд: последние TREND_WINDOW месяцев против предыдущих таких же.
    Возвращает изменение в руб/тн и процентах — основу прогноза."""
    recent_num = recent_den = prev_num = prev_den = 0.0
    for r in rows:
        q = float(r["total_qty_t"] or 0)
        price = float(r["rub_per_t"] or 0)
        if q <= 0 or price <= 0:
            continue
        age = now_idx - _month_index(r["period"])
        if 0 <= age < TREND_WINDOW:
            recent_num += price * q
            recent_den += q
        elif TREND_WINDOW <= age < TREND_WINDOW * 2:
            prev_num += price * q
            prev_den += q
    if recent_den < MIN_TREND_QTY or prev_den < MIN_TREND_QTY:
        return None
    recent = recent_num / recent_den
    prev = prev_num / prev_den
    return {"recent": recent, "prev": prev, "delta": recent - prev,
            "pct": (recent / prev - 1) * 100 if prev else 0.0}


def sale_price_hints(conn: sqlite3.Connection, cargo_group: str,
                     division: str | None = None, buyer: str | None = None,
                     limit_buyers: int = 6) -> dict:
    """Ориентиры цены реализации для группы аналитического учёта.

    Возвращает: `best` — рекомендуемый ориентир по самому узкому разрезу с
    данными; `forecast` — прогноз с учётом тренда; `buyers` — цены по
    каналам сбыта (видно, что Чермет даёт одну цену, а Втор-ресурс другую);
    `history` — помесячная динамика для графика/проверки.
    """
    if not cargo_group:
        return {}
    now_idx = _now_index()

    def fetch(where: str, params: tuple) -> list[sqlite3.Row]:
        try:
            return conn.execute(
                "SELECT period, buyer, division, samples, total_qty_t, "
                "total_revenue, rub_per_t FROM stat_sale_price_hist "
                f"WHERE cargo_group = ? {where} ORDER BY period",
                (cargo_group, *params)).fetchall()
        except sqlite3.Error:            # база без таблицы истории
            return []

    all_rows = fetch("", ())
    if not all_rows:
        return {}

    # Разрез от узкого к широкому: чем конкретнее совпадение, тем точнее цена.
    levels: list[tuple[str, list[sqlite3.Row]]] = []
    if division and buyer:
        sel = [r for r in all_rows
               if r["division"] == division and r["buyer"] == buyer]
        if sel:
            levels.append((f"{cargo_group} · {division} · {buyer}", sel))
    if buyer:
        sel = [r for r in all_rows if r["buyer"] == buyer]
        if sel:
            levels.append((f"{cargo_group} · покупатель {buyer}", sel))
    if division:
        sel = [r for r in all_rows if r["division"] == division]
        if sel:
            levels.append((f"{cargo_group} · {division}", sel))
    levels.append((cargo_group, all_rows))

    best = None
    for label, rows in levels:
        agg = _weighted(rows, now_idx)
        if agg and agg["qty"] > 0:
            best = {**agg, "scope": label}
            break
    if best is None:
        return {}

    # Прогноз строится от ПОСЛЕДНИХ фактических сделок, а не от средней за
    # два года: у труб малых диаметров последние месяцы идут по 32 000 при
    # средней 22 555 — прогноз от средней занизил бы цену в полтора раза.
    # К свежей цене добавляем наблюдаемый тренд на период вперёд.
    scope_rows = next(rows for label, rows in levels if label == best["scope"])
    tr = _trend(scope_rows, now_idx) or _trend(all_rows, now_idx)
    if tr:
        anchor = tr["recent"]
        capped = max(-TREND_CAP_PCT, min(TREND_CAP_PCT, tr["pct"]))
        forecast = anchor * (1 + capped / 100.0)
    else:
        anchor, forecast = best["price"], best["price"]

    # Цены по каналам сбыта: у кого сколько брали и почём.
    by_buyer: dict[str, list] = {}
    for r in all_rows:
        by_buyer.setdefault(r["buyer"] or "—", []).append(r)
    buyers = []
    for name, rows in by_buyer.items():
        agg = _weighted(rows, now_idx)
        if agg and agg["qty"] > 0:
            buyers.append({"buyer": name, **agg,
                           "trend": _trend(rows, now_idx)})
    buyers.sort(key=lambda b: -b["qty"])

    # Помесячная динамика выбранного разреза.
    months: dict[str, list[float]] = {}
    for r in scope_rows:
        q = float(r["total_qty_t"] or 0)
        p = float(r["rub_per_t"] or 0)
        if q <= 0 or p <= 0:
            continue
        m = months.setdefault(r["period"], [0.0, 0.0])
        m[0] += p * q
        m[1] += q
    history = [{"period": k, "price": v[0] / v[1], "qty": v[1]}
               for k, v in sorted(months.items()) if v[1] > 0][-18:]

    return {"best": best, "forecast": forecast, "anchor": anchor, "trend": tr,
            "buyers": buyers[:limit_buyers], "history": history}


def plan_price(base: float | None, discount: float | None) -> float | None:
    """Плановая цена = текущая минус снижение — так экономисты и пишут в
    книгах: «Вывозим на Чермет-Волжский по 19500 (текущая цена 21500 минус
    снижение)». Снижение задаётся в рублях на тонну."""
    if base is None:
        return None
    return round(float(base) - float(discount or 0), 2)


def price_matrix(conn: sqlite3.Connection, groups: list[str],
                 limit_buyers: int = 7) -> dict:
    """Матрица «группа × покупатель»: почём каждый канал берёт каждую группу.

    В книгах экономистов это лист «Сводная по направлениям», где под каждым
    покупателем стоят две цены — за изделие и за лом. Выбор канала и есть
    решение по сделке: труба на Чермет уходит одной ценой, а тот же объём
    местным покупателям — совсем другой. Одной цены «по группе» для этого
    решения недостаточно.
    """
    if not groups:
        return {}
    now_idx = _now_index()
    marks = ",".join("?" for _ in groups)
    try:
        rows = conn.execute(
            "SELECT cargo_group, buyer, period, samples, total_qty_t, "
            "total_revenue, rub_per_t FROM stat_sale_price_hist "
            f"WHERE cargo_group IN ({marks})", tuple(groups)).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}

    by_pair: dict[tuple, list] = {}
    by_buyer_qty: dict[str, float] = {}
    for r in rows:
        buyer = (r["buyer"] or "").strip() or "—"
        by_pair.setdefault((r["cargo_group"], buyer), []).append(r)
        by_buyer_qty[buyer] = by_buyer_qty.get(buyer, 0.0) + float(
            r["total_qty_t"] or 0)

    buyers = [b for b, _ in sorted(by_buyer_qty.items(),
                                   key=lambda kv: -kv[1])][:limit_buyers]
    matrix = []
    for grp in groups:
        cells = []
        best = None
        for buyer in buyers:
            agg = _weighted(by_pair.get((grp, buyer), []), now_idx)
            cells.append({"buyer": buyer, **(agg or {})} if agg
                         else {"buyer": buyer})
            if agg and (best is None or agg["price"] > best):
                best = agg["price"]
        for c in cells:
            c["is_best"] = bool(best and c.get("price")
                                and abs(c["price"] - best) < 0.01)
        matrix.append({"group": grp, "cells": cells, "best": best})
    return {"buyers": buyers, "rows": matrix}


def nomen_price_hint(conn: sqlite3.Connection, nomen_1c: str | None,
                     min_qty_t: float = 5.0, months: int = 24) -> dict | None:
    """Ориентир цены по конкретной номенклатуре 1С из регистра продаж.

    Свежие месяцы весят больше (полураспад FRESH_HALFLIFE), окно `months`.
    None — если номенклатуры нет или продано меньше `min_qty_t` тонн: тогда
    остаётся ориентир по группе.
    """
    if not nomen_1c:
        return None
    from .matcher import normalize
    try:
        rows = conn.execute(
            "SELECT period, samples, total_qty_t, total_revenue FROM stat_sale_price_nomen "
            "WHERE nomen_norm = ? ORDER BY period DESC", (normalize(nomen_1c),)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    now = _now_index()
    w_sum = q_sum = v_sum = 0.0
    n = 0
    p_from = p_to = None
    for r in rows:
        try:
            y, m = int(r["period"][:4]), int(r["period"][5:7])
        except ValueError:
            continue
        age = now - (y * 12 + m)
        if age < 0 or age > months:
            continue
        w = 0.5 ** (age / FRESH_HALFLIFE)
        q = float(r["total_qty_t"] or 0)
        v = float(r["total_revenue"] or 0)
        if q <= 0 or v <= 0:
            continue
        w_sum += w * q
        v_sum += w * v
        q_sum += q
        n += int(r["samples"] or 0)
        p_from = r["period"] if p_from is None or r["period"] < p_from else p_from
        p_to = r["period"] if p_to is None or r["period"] > p_to else p_to
    if q_sum < min_qty_t or w_sum <= 0:
        return None
    return {"price": round(v_sum / w_sum, 2), "qty": round(q_sum, 1), "samples": n,
            "period_from": p_from, "period_to": p_to, "nomen": nomen_1c}
