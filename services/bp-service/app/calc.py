"""Расчёт P&L сделки по методике отчёта о прибылях и убытках.

Единственное место в системе, где определены формулы экономики.
Изменения — только по согласованию с финансовым директором.

Базовая методика — БП 1865 (Лукойл-Западная Сибирь, труба/штанга 27 208 тн,
v0 29.05.26), сверено с BP_1759 и расчётом экономистов «Данные_Тест»:
- Закупка лотом: цена закупки руб/тн = стоимость лота / суммарный тоннаж.
- Объём продажи: если позиция ПРОДАЁТСЯ как чёрный лом — объём × (1 − засор);
  прочее без засора (в БП 1865 труба продаётся ломом, засор 5% применяется).
- Закупка лома НДС не облагается; труба/ДХНО/прочее закупается с НДС.
- Если тип покупки ≠ типу продажи (труба → лом) — НДС не возмещается.
  На затраты относится доля невозмещённого НДС (vat_unrecovered_pct,
  по умолчанию 50% — строка «НДС, не возмещенный 50%» БП 1865); при расчёте
  налога на прибыль эта сумма ДОБАВЛЯЕТСЯ обратно к базе (формула
  C53 = (прибыль до налога + НДС 50%) × ставка — НДС не уменьшает базу).
- Привлечённый капитал: база = закупка с НДС, остаток убывает линейно
  (равными долями за срок вывоза), проценты = остаток × ставка / 12,
  суммарно за весь срок (в БП 1865 это Σ = база × ставка/12 × 3,5 при 6 мес).
- Затраты считаются ПО КАЖДОЙ ПОЗИЦИИ: статья затрат распределяется на
  позиции своей базы (поле base статьи или привязка к стрелке графа),
  без привязки — на все позиции по тоннажу; итог P&L — сумма позиций.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_setting

FERROUS = "лом"          # чёрный лом — тип, к которому применяется засор
VAT_FREE_TYPES = {"лом"}  # закупка/реализация лома не облагается НДС

# Группа аналитического учёта (справочник 1С) → тип для расчёта. Экономисту
# и менеджеру видна одна классификация — группа; НДС и засор зависят от того,
# лом это или изделие, поэтому тип выводится из группы, а не задаётся отдельно.
GROUP_TYPE_RULES = (
    ("лом черных", "лом"),
    ("лом легированной", "лом"),
    ("лом цветных", "цветмет"),
    ("кабель", "кабель"),
    ("дхно", "ДХНО"),
    ("труба", "труба"),
    ("штанга", "труба"),
    # Простые слова — ПОСЛЕДНИМИ, после специфичных («лом цветных» должен
    # успеть сработать раньше). Без них план продажи «лом», набранный
    # человеком (или массовой правкой), молча откатывался к типу закупки —
    # на КП 1578 из-за этого весь лот посчитался «труба → труба» и
    # невозмещённый НДС стал нулём.
    ("лом", "лом"),
    ("цветмет", "цветмет"),
)


def type_of_group(cargo_group: str | None, default: str = "лом") -> str:
    """Тип расчёта по группе аналитического учёта."""
    low = (cargo_group or "").lower().replace("ё", "е")
    if not low:
        return default
    for needle, kind in GROUP_TYPE_RULES:
        if needle in low:
            return kind
    return default

# Типовые статьи затрат P&L: (секция, статья) — предзаполняются при создании БП.
DEFAULT_COST_ITEMS = [
    ("Переменные", "Погрузочно-разгрузочные расходы"),
    ("Переменные", "Заправка газом и кислородом"),
    ("Переменные", "Транспортные расходы — собственная техника"),
    ("Переменные", "Транспортные расходы на отгрузку"),
    ("Переменные", "Транспортные расходы на перемещение"),
    ("Персонал", "Зарплата"),
    ("Персонал", "Аренда квартир (проживание)"),
    ("Персонал", "Командировочные расходы"),
    ("Персонал", "Прочие расходы на персонал"),
    ("Постоянные", "Оборудование и инструменты"),
    ("Постоянные", "Аренда баз, коммунальные расходы, охрана"),
    ("Постоянные", "Прочие производственные расходы"),
    ("Постоянные", "Амортизационные отчисления (транспорт, оборудование)"),
]
PAYROLL_ITEM = "Зарплата"           # база для налогов с ФОТ
SALE_TYPES = ["лом", "труба", "цветмет", "кабель", "ДХНО"]

# Варианты расчёта внутри одного БП. Экономисты ведут в книге две пары листов:
# «БП/v0 ..._дсп» — внутренний базовый расчёт (вариант «ДСП») и
# «БП/v0 ..._лук» — расчёт по варианту Лукойл. У варианта «Лукойл» свои цены
# реализации позиций (bp_items.sale_price_luk) и суммы статей затрат
# (bp_costs.amount_luk); всё остальное (состав лота, объёмы, ставки) — общее.
VARIANTS = {"bsp": "ДСП", "luk": "Лукойл"}
VARIANT_SHEET_HINTS = {"bsp": "листы «_дсп»", "luk": "листы «_лук»"}


def _to_dict(row) -> dict:
    return dict(row) if isinstance(row, dict) else dict(zip(row.keys(), tuple(row)))


def luk_overrides(bp) -> dict:
    """Переопределения параметров сделки в варианте «Лукойл» (JSON-поле
    business_plans.luk_overrides: {removal_months: 5, ...})."""
    import json
    try:
        raw = bp["luk_overrides"]
    except (KeyError, IndexError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def apply_variant(bp, items: list, costs: list, variant: str):
    """Данные выбранного варианта: для «Лукойл» цены позиций и суммы статей
    подменяются значениями _luk (NULL = значение не задано, берётся базовое),
    а параметры сделки — переопределениями luk_overrides (срок вывоза,
    ставки). Для «ДСП» подменять нечего, но вариант всё равно помечается в
    bp['_variant'] — по нему item_rows берёт план продажи (разбивку позиции
    по покупателям), он у вариантов свой."""
    if variant != "luk":
        bp_v = _to_dict(bp)
        bp_v["_variant"] = "bsp"
        return bp_v, items, costs
    bp_v = _to_dict(bp)
    bp_v["_variant"] = "luk"
    for k, v in luk_overrides(bp).items():
        if v is not None:
            bp_v[k] = v
    items_v = []
    for it in items:
        row = _to_dict(it)
        if row.get("sale_price_luk") is not None:
            row["sale_price"] = row["sale_price_luk"]
        if row.get("contamination_pct_luk") is not None:
            row["contamination_pct"] = row["contamination_pct_luk"]
        items_v.append(row)
    costs_v = []
    for c in costs:
        row = _to_dict(c)
        if row.get("amount_luk") is not None:
            row["amount"] = row["amount_luk"]
        costs_v.append(row)
    return bp_v, items_v, costs_v


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def rate(bp: sqlite3.Row, field: str, conn: sqlite3.Connection, default: float) -> float:
    """Ставка из БП, иначе из настроек, иначе значение по умолчанию."""
    v = _f(bp[field])
    return v if v else get_setting(conn, field, default)


def base_of(it) -> str:
    """База позиции: «Поставщик» (Усинск, Ухта), а если он не заполнен или
    совпадает с продавцом — подразделение/место хранения (в БП 1865 базы
    Лукойла лежат в «Подразделении», колонка «Поставщик» — сам продавец)."""
    for key in ("supplier", "division", "warehouse"):
        try:
            v = it[key]
        except (KeyError, IndexError):
            v = None
        if v and str(v).strip():
            return str(v).strip()
    return "—"


def item_rows(bp: sqlite3.Row, items: list[sqlite3.Row],
              conn: sqlite3.Connection) -> list[dict]:
    """Построчный расчёт позиций (аналог листа «Данные»)."""
    contamination = rate(bp, "contamination_pct", conn, 6.0) / 100.0
    vat = rate(bp, "vat_rate", conn, 20.0) / 100.0
    total_volume = sum(_f(it["volume_t"]) for it in items)
    lot_cost = _f(bp["lot_cost"])
    price = lot_cost / total_volume if total_volume else 0.0

    # План продажи: позиция может быть разделена между покупателями (своя
    # цена, отгрузка, засор). Строки свои у каждого варианта расчёта.
    sales = sales_by_item(bp, items, conn)

    # Доля лота: сделка может делиться с партнёром (УВМ) — тогда мы покупаем
    # и продаём только свою часть каждой позиции. Цена закупки руб/тн от доли
    # не зависит (лот / полный тоннаж), объёмы и суммы — на долю.
    share = lot_share(bp)
    rows = []
    for it in items:
        for line in split_item(it, sales.get(it["id"])):
            if share != 1.0:
                line = _to_dict(line)
                line["volume_t"] = _f(line.get("volume_t")) * share
            rows.append(_item_row(line, bp, price, contamination, vat))
    return rows


def lot_share(bp) -> float:
    """Наша доля лота как множитель 0..1 (NULL / 0 / пусто = весь лот)."""
    pct = _f(_row_get(bp, "lot_share_pct"))
    if pct <= 0 or pct >= 100:
        return 1.0
    return pct / 100.0


def sales_by_item(bp, items: list, conn: sqlite3.Connection) -> dict[int, list]:
    """Строки плана продажи активного варианта, сгруппированные по позиции."""
    bp_id = _row_get(bp, "id")
    if conn is None or bp_id is None or not items:
        return {}
    variant = _row_get(bp, "_variant") or "bsp"
    out: dict[int, list] = {}
    try:
        cur = conn.execute(
            "SELECT * FROM bp_item_sales WHERE bp_id = ? AND variant = ? "
            "ORDER BY item_id, sort, id", (bp_id, variant))
    except sqlite3.Error:                # старая база без таблицы
        return {}
    for s in cur.fetchall():
        out.setdefault(s["item_id"], []).append(s)
    return out


def split_item(it, lines) -> list[dict]:
    """Позиция → строки продажи. Без разбивки — одна строка (как раньше).
    С разбивкой объём делится между покупателями; необлагаемый остаток
    (объём сверх суммы строк) остаётся за покупателем самой позиции."""
    row = _to_dict(it)
    if not lines:
        return [row]
    out: list[dict] = []
    assigned = 0.0
    for s in lines:
        part = _to_dict(row)
        vol = _f(s["volume_t"])
        assigned += vol
        part.update({
            "sale_id": s["id"], "volume_t": vol,
            "buyer": s["buyer"] or row.get("buyer"),
            "shipment": s["shipment"] or row.get("shipment"),
            "note": s["note"] or row.get("note"),
        })
        if s["sale_price"] is not None:
            part["sale_price"] = s["sale_price"]
        if s["sale_type"]:
            part["sale_type"] = s["sale_type"]
        if s["contamination_pct"] is not None:
            part["contamination_pct"] = s["contamination_pct"]
        out.append(part)
    rest = round(_f(row.get("volume_t")) - assigned, 6)
    if rest > 0.0005:                    # нераспределённый объём позиции
        tail = _to_dict(row)
        tail["volume_t"] = rest
        tail["sale_id"] = None
        out.append(tail)
    return out


def _item_row(it, bp, price: float, contamination: float, vat: float) -> dict:
    """Расчёт одной строки продажи (позиция целиком или её часть)."""
    vol = _f(it["volume_t"])
    # Тип ЗАКУПКИ берётся из спецификации и группой не переопределяется:
    # группа аналитического учёта описывает, чем позиция продаётся («Лом
    # черных металлов»), а покупаем мы трубу — с НДС. Подмена типа закупки
    # обнуляла бы НДС на закупку и завышала прибыль (БП 1935: +184 тыс).
    # Группа используется как запасной вариант, если тип не задан вовсе.
    group = _row_get(it, "cargo_group")
    ptype = (it["purchase_type"] or "").strip() or type_of_group(group)
    # План продажи задаётся группой аналитического учёта («продаём как Лом
    # черных металлов»); тип для НДС и засора выводится из неё. Если группа
    # продажи не задана — остаётся тип из спецификации.
    sgroup = _row_get(it, "sale_group")
    if sgroup:
        stype = type_of_group(sgroup, ptype)
    else:
        stype = (it["sale_type"] or "").strip() or type_of_group(group, ptype)
    cost = vol * price
    # Засор снимает объём у чёрного лома по типу ПРОДАЖИ: труба, проданная
    # ломом (БП 1865), теряет 5% так же, как купленный лом (BP_1759).
    # Позиция может переопределять засор (БП 1738: 5,8–5,9% по строкам);
    # явный засор позиции применяется к ЛЮБОМУ типу — в БП 1785 цветной
    # лом (латунь/медь) теряет 5% при засоре чёрного 6%.
    item_cont = _row_get(it, "contamination_pct")
    if item_cont is not None:
        sale_vol = vol * (1 - item_cont / 100.0)
    else:
        sale_vol = vol * (1 - contamination) if stype == FERROUS else vol
    revenue = sale_vol * _f(it["sale_price"])
    cost_vat = cost if ptype in VAT_FREE_TYPES else cost * (1 + vat)
    revenue_vat = revenue if stype in VAT_FREE_TYPES else revenue * (1 + vat)
    vat_unrecovered = 0.0 if ptype == stype else max(cost_vat - cost, 0.0)
    # Кратность партии покупателя (машина 20 тн): объём продажи делится
    # на целые партии, остаток — «хвост» (аппендикс), который вывозится
    # на переработку или отдаётся другому покупателю.
    batch = _f(_row_get(it, "batch_size_t"))
    batches = int(sale_vol // batch) if batch > 0 else 0
    tail = round(sale_vol - batches * batch, 6) if batch > 0 else 0.0
    return {
        "sale_id": _row_get(it, "sale_id"),
        "batch_size_t": batch, "batches": batches, "tail_t": tail,
        "id": it["id"], "supplier": it["supplier"], "division": it["division"],
        "warehouse": it["warehouse"], "nomenclature": it["nomenclature"],
        "base": base_of(it),
        "category": it["category"], "purchase_type": ptype, "sale_type": stype,
        "volume_t": vol, "purchase_price": price, "purchase_cost": cost,
        "sale_volume_t": sale_vol, "sale_price": _f(it["sale_price"]),
        "revenue": revenue, "purchase_cost_vat": cost_vat,
        "revenue_vat": revenue_vat, "vat_unrecovered": vat_unrecovered,
        "buyer": it["buyer"], "shipment": it["shipment"],
        "price_owner": it["price_owner"], "note": it["note"],
    }


def capital_schedule(base: float, annual_rate: float, months: float,
                     payment_delay: float = 0.0) -> list[dict]:
    """График привлечённого капитала: остаток гасится по мере ОПЛАТЫ.

    Отгрузка идёт равномерно за срок вывоза, деньги приходят через
    payment_delay месяцев после отгрузки (требование экономиста: «месяц на
    вывоз, месяц на дебиторку»). Без отсрочки остаток убывает линейно за
    срок вывоза — прежняя методика (в БП 1865 Σ = база × ставка/12 × 3,5
    при 6 мес). Срок меньше месяца (БП 1785: «1 неделя» = 0,25 мес) —
    период с долей месяца.
    """
    m = float(months or 0)
    delay = max(float(payment_delay or 0), 0.0)
    if m <= 0:
        return []
    monthly = annual_rate / 12.0
    schedule: list[dict] = []
    elapsed, total = 0.0, m + delay
    while elapsed < total - 1e-9:
        span = min(1.0, total - elapsed)          # период — месяц или его доля
        # Доля лота, уже оплаченной к началу периода: отгружено к моменту
        # (elapsed − отсрочка), деньги за неё уже поступили.
        paid = min(max(elapsed - delay, 0.0), m) / m
        balance = base * (1 - paid)
        schedule.append({"month": len(schedule) + 1, "balance": balance,
                         "span": span, "interest": balance * monthly * span})
        elapsed += span
    return schedule


def share_costs(bp, costs: list) -> list:
    """Статьи затрат на нашу долю лота: суммы в bp_costs (и модельные, и
    введённые вручную) задаются на весь лот, как и книга экономиста, поэтому
    при доле < 100 % каждая сумма делится так же, как выручка и закупка.
    Повторного деления нет: у уже поделённых строк стоит признак _shared."""
    share = lot_share(bp)
    if share == 1.0 or not costs:
        return costs
    out = []
    for c in costs:
        if _row_get(c, "_shared"):
            return costs
        d = _to_dict(c)
        d["amount"] = _f(d.get("amount")) * share
        d["_shared"] = 1
        out.append(d)
    return out


def _cost_sum(costs: list[sqlite3.Row], section: str) -> float:
    return sum(_f(c["amount"]) for c in costs if c["section"] == section)


def _row_get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _edge_scopes(conn: sqlite3.Connection, bp_id) -> dict[int, dict]:
    """Стрелки графа маршрута: id → поток, подписи узлов и базы выше по
    течению. Затраты стрелки относятся к позициям всех баз, чьи потоки
    проходят через неё: для стрелки из узла консолидации это и базы,
    из которых в узел ведут пути (обход графа назад по рёбрам)."""
    try:
        rows = conn.execute(
            "SELECT e.id, e.from_node, e.to_node, e.flow_group, "
            "nf.label AS from_label, nt.label AS to_label "
            "FROM bp_edges e "
            "JOIN bp_nodes nf ON nf.id = e.from_node "
            "JOIN bp_nodes nt ON nt.id = e.to_node WHERE e.bp_id = ?",
            (bp_id,)).fetchall()
        labels = {n["id"]: n["label"] for n in conn.execute(
            "SELECT id, label FROM bp_nodes WHERE bp_id = ?", (bp_id,)).fetchall()}
    except sqlite3.Error:
        return {}
    incoming: dict[int, list[int]] = {}
    for r in rows:
        incoming.setdefault(r["to_node"], []).append(r["from_node"])
    out = {}
    for r in rows:
        upstream, queue = [], [r["from_node"]]
        seen = set()
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            upstream.append(labels.get(node, ""))
            queue.extend(incoming.get(node, []))
        e = dict(r)
        e["upstream_labels"] = upstream
        out[r["id"]] = e
    return out


_BASE_STOPWORDS = {"ооо", "оао", "зао", "ао", "база", "базе", "г", "вп", "в/п",
                   "склад", "цех", "консолидация"}


def _base_tokens(name: str) -> set[str]:
    """Значимые слова имени базы, обрезанные до основы (7 букв): падежные
    окончания не мешают сопоставлению («Пякяхинское месторождение» =
    «промысел Пякяхинского месторождения»)."""
    import re as _re
    words = _re.sub(r"[^а-яёa-z0-9 ]", " ", (name or "").lower()).split()
    return {w[:7] for w in words if w not in _BASE_STOPWORDS and len(w) > 1}


def match_score(a: set[str], b: set[str]) -> int:
    """Ранг совпадения имён по значимым словам: 4 — множества равны,
    3 — одно вложено в другое, 2/1 — сильное/слабое пересечение
    («Пякяхинское месторождение (НГК промысел)» ↔ «Нефтегазоконденсатный
    промысел Пякяхинского месторождения»), 0 — не совпадают. Однотипные
    имена («ЛНПО-Сервис Покачи» и «ЛНПО-Сервис Лангепас») различаются
    рангом: точное/вложенное совпадение всегда выигрывает у пересечения."""
    if not a or not b:
        return 0
    if a == b:
        return 4
    if a <= b or b <= a:
        return 3
    common = len(a & b)
    if common >= 2 and common >= 0.8 * min(len(a), len(b)):
        return 2
    if common >= 2 and common >= 0.6 * min(len(a), len(b)):
        return 1
    return 0


def tokens_match(a: set[str], b: set[str]) -> bool:
    return match_score(a, b) > 0


def _match_base(rows: list[dict], name: str) -> list[int]:
    """Позиции, чья база совпадает с именем — берутся только ЛУЧШИЕ
    совпадения: статья «Покачи» не размазывается на Лангепас из-за общего
    «ЛНПО-Сервис» (у Покачей ранг выше)."""
    low = (name or "").strip().lower()
    if not low:
        return []
    tokens = _base_tokens(name)
    scored: list[tuple[int, int]] = []
    for i, r in enumerate(rows):
        rlow = r["base"].lower()
        if rlow == low or low in rlow or rlow in low:
            scored.append((5, i))
            continue
        s = match_score(tokens, _base_tokens(r["base"]))
        if s:
            scored.append((s, i))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [i for s, i in scored if s == best]


def _cost_scope(rows: list[dict], cost, edges: dict[int, dict]) -> list[int]:
    """Позиции, на которые распределяется статья затрат:
    1) база статьи (bp_costs.base); 2) стрелка графа (edge_id): поток стрелки
    или база из подписи узлов; 3) иначе — все позиции."""
    sel = _match_base(rows, _row_get(cost, "base") or "")
    if sel:
        return sel
    edge = edges.get(_row_get(cost, "edge_id"))
    if edge:
        fg = (edge["flow_group"] or "").strip().lower()
        if fg:
            sel = [i for i, r in enumerate(rows)
                   if fg in ((r["category"] or "").lower(),
                             (r["nomenclature"] or "").lower())]
            if sel:
                return sel
        # Все базы выше по течению: стрелка из узла консолидации несёт
        # потоки баз, из которых в него ведут пути.
        merged: list[int] = []
        for label in edge.get("upstream_labels", []):
            for i in _match_base(rows, label):
                if i not in merged:
                    merged.append(i)
        if merged:
            return sorted(merged)
        sel = _match_base(rows, edge["to_label"])
        if sel:
            return sel
    return list(range(len(rows)))


def pnl(bp: sqlite3.Row, items: list[sqlite3.Row], costs: list[sqlite3.Row],
        conn: sqlite3.Connection) -> dict:
    """Отчёт о прибылях и убытках по утверждённой структуре."""
    costs = share_costs(bp, costs)          # доля лота — и на статьи затрат
    rows = item_rows(bp, items, conn)
    vat = rate(bp, "vat_rate", conn, 20.0) / 100.0
    cap_rate = rate(bp, "capital_rate", conn, 25.0) / 100.0
    tax_rate = rate(bp, "tax_rate", conn, 25.0) / 100.0
    payroll_rate = rate(bp, "payroll_tax_rate", conn, 49.7) / 100.0
    months = _f(bp["removal_months"]) or get_setting(conn, "removal_months", 6.0)

    purchase_volume = sum(r["volume_t"] for r in rows)
    sale_volume = sum(r["sale_volume_t"] for r in rows)
    revenue = sum(r["revenue"] for r in rows)

    # Выручка и объёмы по типам продажи (ПЛАН)
    by_type = {}
    for t in SALE_TYPES:
        sel = [r for r in rows if r["sale_type"] == t]
        by_type[t] = {"volume": sum(r["sale_volume_t"] for r in sel),
                      "revenue": sum(r["revenue"] for r in sel)}

    share = lot_share(bp)
    lot_cost = _f(bp["lot_cost"]) * share            # закупка — на нашу долю
    purchase_cost_vat = sum(r["purchase_cost_vat"] for r in rows)
    # Невозмещённый НДС: на затраты относится доля — параметр сценария БП
    # (1865 и 1674-базовый — 50%, 1674 «минус 1000» — 30%), по умолчанию
    # из настроек; при налоге на прибыль сумма добавляется обратно к базе.
    try:
        vat_unrec_pct = rate(bp, "vat_unrecovered_pct", conn, 50.0) / 100.0
    except (KeyError, IndexError):
        vat_unrec_pct = get_setting(conn, "vat_unrecovered_pct", 50.0) / 100.0
    vat_unrecovered_full = sum(r["vat_unrecovered"] for r in rows)
    vat_unrecovered = vat_unrecovered_full * vat_unrec_pct
    for r in rows:
        r["vat_unrec_cost"] = r["vat_unrecovered"] * vat_unrec_pct

    gross_purchase = revenue - lot_cost                      # валовый доход от закупки
    markup_pct = gross_purchase / lot_cost * 100.0 if lot_cost else 0.0

    variable_costs = _cost_sum(costs, "Переменные")
    gross_production = revenue - lot_cost - variable_costs

    personnel = _cost_sum(costs, "Персонал")
    salary = sum(_f(c["amount"]) for c in costs
                 if c["section"] == "Персонал" and c["item"] == PAYROLL_ITEM)
    payroll_tax = salary * payroll_rate
    personnel_total = personnel + payroll_tax
    fixed_other = _cost_sum(costs, "Постоянные")
    # «Постоянные затраты» — без невозмещённого НДС: в книгах экономистов он
    # идёт ОТДЕЛЬНОЙ строкой после постоянных, и при построчной сверке
    # смешение давало расхождение в сотни тысяч (1935 ДСП: 623 345) при
    # полностью верном итоге. В прямые затраты НДС по-прежнему входит.
    fixed_costs_wo_vat = personnel_total + fixed_other
    fixed_costs = fixed_costs_wo_vat + vat_unrecovered

    direct_costs = variable_costs + fixed_costs
    operating_profit = revenue - lot_cost - direct_costs

    admin_costs = _cost_sum(costs, "Административные")
    other_costs = _cost_sum(costs, "Прочие")

    # База капитала: по умолчанию закупка с НДС; экономист может задать свою
    # (БП 1785: «стоимость закупа» без меди — медь оплачивает покупатель).
    # База капитала задаётся на весь лот — как и его стоимость — и делится так же.
    capital_base_v = _f(_row_get(bp, "capital_base")) * share or purchase_cost_vat
    # Отсрочка оплаты покупателем: деньги приходят через N месяцев после
    # отгрузки — капитал держится дольше срока вывоза.
    payment_delay = _f(_row_get(bp, "payment_delay_months"))
    schedule = capital_schedule(capital_base_v, cap_rate, months, payment_delay)
    capital_cost = sum(s["interest"] for s in schedule)

    profit_before_tax = operating_profit - admin_costs - capital_cost - other_costs
    # Налоговая база: прибыль до налога + отнесённый на затраты невозмещённый
    # НДС (он не уменьшает базу) — формула C53 БП 1865.
    income_tax = max((profit_before_tax + vat_unrecovered) * tax_rate, 0.0)
    net_profit = profit_before_tax - income_tax

    margin_threshold = get_setting(conn, "margin_threshold", 10.0)
    ros = net_profit / revenue * 100.0 if revenue else 0.0

    # Построчная экономика: затраты считаются ПО КАЖДОЙ ПОЗИЦИИ и суммируются
    # в общий P&L. Статья затрат распределяется на позиции своей базы
    # (поле base статьи или стрелка графа, к которой она привязана),
    # без привязки — на все позиции пропорционально тоннажу продажи.
    # Капитал — пропорционально закупке с НДС; невозмещённый НДС — свой у
    # каждой позиции. Σ прибыли позиций = прибыль до налогообложения.
    edges = _edge_scopes(conn, bp["id"]) if _row_get(bp, "id") else {}
    alloc = [0.0] * len(rows)
    weights = [r["sale_volume_t"] or r["volume_t"] for r in rows]

    def distribute(amount: float, sel: list[int]) -> None:
        if not amount or not sel:
            return
        w = sum(weights[i] for i in sel)
        for i in sel:
            alloc[i] += amount * (weights[i] / w if w else 1.0 / len(sel))

    for c in costs:
        sel = _cost_scope(rows, c, edges)
        distribute(_f(c["amount"]), sel)
        if c["section"] == "Персонал" and c["item"] == PAYROLL_ITEM:
            distribute(_f(c["amount"]) * payroll_rate, sel)   # налоги с ФОТ

    for i, r in enumerate(rows):
        cap_alloc = (capital_cost * r["purchase_cost_vat"] / purchase_cost_vat
                     if purchase_cost_vat else 0.0)
        direct_alloc = alloc[i] + r["vat_unrec_cost"]
        r["alloc_direct"] = direct_alloc
        r["alloc_capital"] = cap_alloc
        r["alloc_costs"] = direct_alloc + cap_alloc
        r["cost_per_t"] = (r["alloc_costs"] / r["sale_volume_t"]
                           if r["sale_volume_t"] else 0.0)
        r["profit"] = r["revenue"] - r["purchase_cost"] - r["alloc_costs"]
        r["profit_per_t"] = (r["profit"] / r["sale_volume_t"]
                             if r["sale_volume_t"] else 0.0)

    return {
        "rows": rows,
        "purchase_volume": purchase_volume,
        "sale_volume": sale_volume,
        "by_type": by_type,
        "revenue": revenue,
        "revenue_per_t": revenue / sale_volume if sale_volume else 0.0,
        "lot_cost": lot_cost,
        "purchase_price_per_t": lot_cost / purchase_volume if purchase_volume else 0.0,
        "purchase_cost_vat": purchase_cost_vat,
        "vat_rate": vat * 100.0,
        "vat_unrecovered": vat_unrecovered,
        "vat_unrecovered_full": vat_unrecovered_full,
        "vat_unrecovered_pct": vat_unrec_pct * 100.0,
        "gross_purchase": gross_purchase,
        "markup_pct": markup_pct,
        "variable_costs": variable_costs,
        "gross_production": gross_production,
        "salary": salary,
        "payroll_tax": payroll_tax,
        "personnel_total": personnel_total,
        "fixed_costs": fixed_costs,
        # Постоянные без НДС — строка «Постоянные затраты» книги.
        "fixed_costs_wo_vat": fixed_costs_wo_vat,
        "direct_costs": direct_costs,
        "operating_profit": operating_profit,
        "operating_profit_per_t": operating_profit / sale_volume if sale_volume else 0.0,
        "admin_costs": admin_costs,
        "other_costs": other_costs,
        "capital_rate": cap_rate * 100.0,
        "removal_months": months,
        "payment_delay_months": payment_delay,
        "capital_base": capital_base_v,
        "lot_share_pct": round(share * 100.0, 2),
        "capital_schedule": schedule,
        "capital_cost": capital_cost,
        "profit_before_tax": profit_before_tax,
        "tax_rate": tax_rate * 100.0,
        "income_tax": income_tax,
        "net_profit": net_profit,
        "net_profit_per_t": net_profit / sale_volume if sale_volume else 0.0,
        "ros_pct": ros,                                    # рентабельность продаж
        "operating_margin_pct": operating_profit / revenue * 100.0 if revenue else 0.0,
        "margin_threshold": margin_threshold,
        "above_threshold": ros >= margin_threshold,
    }


def base_pnl(bp, items: list, costs: list, conn: sqlite3.Connection,
             stages: dict[str, dict[str, float]] | None = None) -> dict:
    """P&L по базам — структура листа «ЗС НКТ+ЛОМ» книг экономистов.

    По каждой базе: объёмы (закупка, продажа, остаток), цена реализации,
    себестоимость закупки, логистика (этапы вывоза и отгрузки), валовый
    доход, переработка, прочие затраты, операционная прибыль — в руб/тн и
    в рублях. Дополнительно — разрез по группам номенклатуры (лом, труба,
    цветмет...), как в книге, где НКТ и ЛОМ считаются отдельно и сводно.

    stages — суммы этапов по базам из нормативной модели (norms.evaluate_model):
    {этап: {база: сумма}}. Без них логистика и переработка берутся из общей
    аллокации затрат на позиции.
    """
    costs = share_costs(bp, costs)
    p = pnl(bp, items, costs, conn)
    stages = stages or {}

    def stage_sum(stage: str, base: str) -> float:
        return _f((stages.get(stage) or {}).get(base))

    # Ручные корректировки разреза по базам (экономист правит деление
    # «логистика / переработка» по факту работы на площадке).
    bp_id = _row_get(bp, "id")
    variant = _row_get(bp, "_variant") or "bsp"
    splits: dict[str, dict] = {}
    if conn is not None and bp_id is not None:
        try:
            for row in conn.execute(
                    "SELECT base, logistics, processing, comment "
                    "FROM bp_base_split WHERE bp_id = ? AND variant = ?",
                    (bp_id, variant)).fetchall():
                splits[row["base"]] = {"logistics": row["logistics"],
                                       "processing": row["processing"],
                                       "comment": row["comment"]}
        except sqlite3.Error:
            splits = {}

    # Направление базы — базовый логистический пункт из реестра пунктов
    # отгрузки (Котово, Самара). Затраты экономист смотрит по направлению:
    # в одно стягивают лом с нескольких мест, и логистика с переработкой
    # считаются на направление целиком.
    directions: dict[str, str] = {}
    try:
        for row in conn.execute(
                "SELECT name, base_point, region FROM shipping_points").fetchall():
            key = (row["name"] or "").strip()
            if key:
                directions[key] = ((row["base_point"] or "").strip()
                                   or (row["region"] or "").strip() or "")
    except sqlite3.Error:
        directions = {}

    groups: dict[str, dict] = {}
    for r in p["rows"]:
        base = r["base"]
        g = groups.setdefault(base, {
            "base": base,
            "direction": directions.get(base) or "Без направления",
            "volume": 0.0, "sale_volume": 0.0, "revenue": 0.0,
            "purchase_cost": 0.0, "alloc_costs": 0.0, "alloc_capital": 0.0,
            "vat_unrec": 0.0, "profit": 0.0, "types": {}})
        g["volume"] += r["volume_t"]
        g["sale_volume"] += r["sale_volume_t"]
        g["revenue"] += r["revenue"]
        g["purchase_cost"] += r["purchase_cost"]
        g["alloc_costs"] += r["alloc_costs"]
        g["alloc_capital"] += r["alloc_capital"]
        g["vat_unrec"] += r["vat_unrec_cost"]
        g["profit"] += r["profit"]
        t = g["types"].setdefault(r["sale_type"], {
            "type": r["sale_type"], "volume": 0.0, "sale_volume": 0.0,
            "revenue": 0.0, "purchase_cost": 0.0, "alloc_costs": 0.0,
            "profit": 0.0})
        for key in ("volume", "sale_volume", "revenue", "purchase_cost",
                    "alloc_costs", "profit"):
            t[key] += r[{"volume": "volume_t", "sale_volume": "sale_volume_t",
                         "revenue": "revenue", "purchase_cost": "purchase_cost",
                         "alloc_costs": "alloc_costs", "profit": "profit"}[key]]

    def per_t(value: float, volume: float) -> float:
        return value / volume if volume else 0.0

    rows = []
    for g in sorted(groups.values(), key=lambda x: -x["volume"]):
        sv = g["sale_volume"]
        # Затраты базы — ФАКТИЧЕСКИЕ (статьи БП, распределённые на позиции),
        # а их структура (логистика / переработка) — в пропорциях
        # нормативной модели по этой базе. Так суммы сходятся с P&L, а
        # разрез показывает, где деньги: на путях или на площадке.
        move = (stage_sum("Вывоз с цеха на базу", g["base"])
                + stage_sum("Отгрузка с базы", g["base"]))
        work = stage_sum("Переработка на базе", g["base"])
        model_base = move + work
        fact = g["alloc_costs"]
        if model_base > 0:
            logistics = fact * move / model_base
            processing = fact * work / model_base
            other = 0.0
        else:                       # модель по базе ничего не дала
            logistics = processing = 0.0
            other = fact
        # Корректировка экономиста заменяет расчётный разрез. Итог базы
        # остаётся фактическим: разница уходит в «прочие», иначе сумма
        # прибыли баз перестала бы сходиться с P&L.
        override = splits.get(g["base"])
        corrected = False
        if override and (override["logistics"] is not None
                         or override["processing"] is not None):
            if override["logistics"] is not None:
                logistics = _f(override["logistics"])
            if override["processing"] is not None:
                processing = _f(override["processing"])
            other = fact - logistics - processing
            corrected = True
        gross = g["revenue"] - g["purchase_cost"]
        rows.append({
            **g,
            # Ключи *_per_t называются как сами суммы — шаблон строит строки
            # таблицы по одному имени показателя.
            "price_per_t": per_t(g["revenue"], sv),
            "revenue_per_t": per_t(g["revenue"], sv),
            "purchase_cost_per_t": per_t(g["purchase_cost"], sv),
            "cost_per_t": per_t(g["purchase_cost"], sv),
            "logistics": logistics, "logistics_per_t": per_t(logistics, sv),
            "processing": processing, "processing_per_t": per_t(processing, sv),
            "other": other, "other_per_t": per_t(other, sv),
            "corrected": corrected,
            "split_comment": (override or {}).get("comment") if override else None,
            "fact_costs": fact,
            "capital": g["alloc_capital"],
            "capital_per_t": per_t(g["alloc_capital"], sv),
            "gross": gross, "gross_per_t": per_t(gross, sv),
            "profit_per_t": per_t(g["profit"], sv),
            "types": sorted(g["types"].values(), key=lambda t: -t["volume"]),
        })

    totals = {
        "volume": sum(r["volume"] for r in rows),
        "sale_volume": sum(r["sale_volume"] for r in rows),
        "revenue": sum(r["revenue"] for r in rows),
        "purchase_cost": sum(r["purchase_cost"] for r in rows),
        "logistics": sum(r["logistics"] for r in rows),
        "processing": sum(r["processing"] for r in rows),
        "other": sum(r["other"] for r in rows),
        "capital": sum(r["capital"] for r in rows),
        "gross": sum(r["gross"] for r in rows),
        "profit": sum(r["profit"] for r in rows),
    }
    sv = totals["sale_volume"]
    for key in ("revenue", "purchase_cost", "logistics", "processing", "other",
                "capital", "gross", "profit"):
        totals[key + "_per_t"] = per_t(totals[key], sv)
    # Свод по направлениям: логистика и переработка на каждое направление
    # и на тонну — экономист планирует ресурсы направлением, а не отдельной
    # площадкой (в 1935 Котово и Самара, под каждым по нескольку баз).
    dirs: dict[str, dict] = {}
    for b in rows:
        d = dirs.setdefault(b["direction"], {
            "direction": b["direction"], "bases": [], "volume": 0.0,
            "sale_volume": 0.0, "revenue": 0.0, "purchase_cost": 0.0,
            "logistics": 0.0, "processing": 0.0, "other": 0.0,
            "capital": 0.0, "profit": 0.0})
        d["bases"].append(b)
        for key in ("volume", "sale_volume", "revenue", "purchase_cost",
                    "logistics", "processing", "other", "capital", "profit"):
            d[key] += b[key]
    directions_out = []
    for d in dirs.values():
        sv = d["sale_volume"]
        for key in ("revenue", "purchase_cost", "logistics", "processing",
                    "other", "capital", "profit"):
            d[key + "_per_t"] = per_t(d[key], sv)
        d["bases"].sort(key=lambda x: -x["volume"])
        directions_out.append(d)
    directions_out.sort(key=lambda x: -x["volume"])

    return {"bases": rows, "totals": totals, "pnl": p,
            "directions": directions_out}


def division_summary(bp, items: list, costs: list, conn: sqlite3.Connection,
                     stages: dict | None = None) -> list[dict]:
    """Свод «бизнес-план для МСК»: по типам металла — объём, выручка,
    себестоимость, затраты (прямые и распределяемые) и прибыль на 1 тонну.
    Так руководство сравнивает сделки между дивизионами."""
    bases = base_pnl(bp, items, costs, conn, stages)
    by_type: dict[str, dict] = {}
    for b in bases["bases"]:
        for t in b["types"]:
            row = by_type.setdefault(t["type"], {
                "type": t["type"], "volume": 0.0, "sale_volume": 0.0,
                "revenue": 0.0, "purchase_cost": 0.0, "costs": 0.0,
                "profit": 0.0})
            row["volume"] += t["volume"]
            row["sale_volume"] += t["sale_volume"]
            row["revenue"] += t["revenue"]
            row["purchase_cost"] += t["purchase_cost"]
            row["costs"] += t["alloc_costs"]
            row["profit"] += t["profit"]
    out = []
    for row in sorted(by_type.values(), key=lambda r: -r["volume"]):
        sv = row["sale_volume"] or row["volume"]
        out.append({**row,
                    "price_per_t": row["revenue"] / sv if sv else 0.0,
                    "cost_per_t": row["purchase_cost"] / sv if sv else 0.0,
                    "costs_per_t": row["costs"] / sv if sv else 0.0,
                    "profit_per_t": row["profit"] / sv if sv else 0.0})
    return out


def price_scenarios(bp: sqlite3.Row, items: list[sqlite3.Row],
                    costs: list[sqlite3.Row], conn: sqlite3.Connection) -> list[dict]:
    """Сценарии цен: консервативный / базовый / оптимистичный.

    Цены реализации всех позиций сдвигаются на ±Δ руб/тн (scen_price_delta),
    P&L пересчитывается целиком. Структура — «бизнес-план для МСК».
    """
    delta = _f(bp["scen_price_delta"])
    if not delta:
        return []
    out = []
    for name, d in [("Консервативный", -delta), ("Базовый", 0.0),
                    ("Оптимистичный", +delta)]:
        shifted = []
        for it in items:
            row = _to_dict(it)
            if row.get("sale_price"):
                row["sale_price"] = _f(row["sale_price"]) + d
            shifted.append(row)
        p = pnl(bp, shifted, costs, conn)
        out.append({"name": name, "delta": d,
                    "revenue": p["revenue"], "revenue_per_t": p["revenue_per_t"],
                    "operating_profit": p["operating_profit"],
                    "net_profit": p["net_profit"],
                    "net_profit_per_t": p["net_profit_per_t"],
                    "ros_pct": p["ros_pct"], "ok": p["above_threshold"]})
    return out


# Поля БП, которые сценарий-вариант может переопределить (NULL = наследуется).
SCENARIO_OVERRIDE_FIELDS = ("lot_cost", "vat_unrecovered_pct", "vat_rate",
                            "capital_rate", "removal_months",
                            "contamination_pct", "tax_rate")
SCENARIO_FIELD_LABELS = {
    "lot_cost": "порог закупки, руб",
    "vat_unrecovered_pct": "невозм. НДС на затраты, %",
    "vat_rate": "ставка НДС, %",
    "capital_rate": "ставка капитала, %",
    "removal_months": "срок вывоза, мес",
    "contamination_pct": "засор, %",
    "tax_rate": "налог на прибыль, %",
}
# Параметры, которые может переопределить вариант «Лукойл» (JSON-поле
# luk_overrides — в отличие от сценариев, колонок в таблице не требует).
VARIANT_OVERRIDE_FIELDS = SCENARIO_OVERRIDE_FIELDS + ("capital_base",
                                                      "payment_delay_months")
VARIANT_FIELD_LABELS = {**SCENARIO_FIELD_LABELS,
                        "capital_base": "база капитала, руб",
                        "payment_delay_months": "отсрочка оплаты, мес"}


def scenario_variants(bp: sqlite3.Row, items: list[sqlite3.Row],
                      costs: list[sqlite3.Row], conn: sqlite3.Connection,
                      scen_rows: list[sqlite3.Row]) -> list[dict]:
    """Сценарии-варианты: полный пересчёт P&L с переопределением параметров
    сделки (сдвиг цены реализации ± порог закупки, доля невозмещённого НДС,
    ставки). В отличие от price_scenarios (только ±Δ цены), воспроизводит
    сценарные листы файлов экономистов (БП 1674 «лук (-1000)»: цена −1000,
    НДС 30%, порог 67,8 млн). Первый элемент — базовый вариант; пустой
    список, если сценарии у БП не заведены."""
    if not scen_rows:
        return []

    def summary(name, p, scen_id=None, delta=0.0, overrides=None,
                control=None, comment=None):
        control_ok = (abs(p["net_profit"] - control) <= max(1.0, abs(control) * 0.001)
                      if control is not None else None)
        return {"id": scen_id, "name": name, "delta": delta,
                "overrides": overrides or {},
                "override_labels": [
                    SCENARIO_FIELD_LABELS.get(k, k) + ": "
                    + f"{v:,.0f}".replace(",", " ")
                    for k, v in (overrides or {}).items()],
                "revenue": p["revenue"], "revenue_per_t": p["revenue_per_t"],
                "lot_cost": p["lot_cost"],
                "vat_unrecovered": p["vat_unrecovered"],
                "vat_unrecovered_pct": p["vat_unrecovered_pct"],
                "operating_profit": p["operating_profit"],
                "capital_cost": p["capital_cost"],
                "income_tax": p["income_tax"],
                "net_profit": p["net_profit"],
                "net_profit_per_t": p["net_profit_per_t"],
                "ros_pct": p["ros_pct"], "ok": p["above_threshold"],
                "control": control, "control_ok": control_ok,
                "comment": comment}

    out = [summary("Базовый", pnl(bp, items, costs, conn))]
    for s in scen_rows:
        bpd = _to_dict(bp)
        overrides = {}
        for f in SCENARIO_OVERRIDE_FIELDS:
            if s[f] is not None:
                bpd[f] = s[f]
                overrides[f] = s[f]
        delta = _f(s["price_delta"])
        shifted = []
        for it in items:
            row = _to_dict(it)
            if row.get("sale_price") and delta:
                row["sale_price"] = _f(row["sale_price"]) + delta
            shifted.append(row)
        p = pnl(bpd, shifted, costs, conn)
        out.append(summary(s["name"], p, scen_id=s["id"], delta=delta,
                           overrides=overrides,
                           control=s["control_net_profit"],
                           comment=s["comment"]))
    return out


_MONTH_NAMES = ["январ", "феврал", "март", "апрел", "мая", "май", "июн", "июл",
                "август", "сентябр", "октябр", "ноябр", "декабр"]
_MONTH_INDEX = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5,
                "май": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9,
                "октябр": 10, "ноябр": 11, "декабр": 12}


def start_month_number(value) -> int | None:
    """Календарный номер месяца начала вывоза: «январь 2026», «01.2026»,
    «2026-01». None, если месяц не указан или не распознан."""
    import re as _re
    text = str(value or "").strip().lower()
    if not text:
        return None
    for name, num in _MONTH_INDEX.items():
        if name in text:
            return num
    m = _re.search(r"\b(\d{1,2})[./-](\d{4})\b", text)       # 01.2026
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 12 else None
    m = _re.search(r"\b(\d{4})[./-](\d{1,2})\b", text)       # 2026-01
    if m:
        n = int(m.group(2))
        return n if 1 <= n <= 12 else None
    m = _re.fullmatch(r"(\d{1,2})", text)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    return None


def load_seasonal_rules(conn: sqlite3.Connection) -> list[dict]:
    """Правила сезонного доступа (зимники): маска → список месяцев."""
    try:
        rows = conn.execute("SELECT pattern, months, comment "
                            "FROM seasonal_access").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        months = {int(x) for x in str(r["months"] or "").replace(" ", "").split(",")
                  if x.isdigit() and 1 <= int(x) <= 12}
        if months:
            out.append({"pattern": r["pattern"], "months": months,
                        "comment": r["comment"]})
    return out


def seasonal_for(text: str, rules: list[dict]) -> dict | None:
    low = (text or "").lower().replace("ё", "е")
    for r in rules:
        if r["pattern"] and r["pattern"].lower() in low:
            return r
    return None


def removal_schedule(bp: sqlite3.Row, items: list[sqlite3.Row],
                     sched_rows: list[sqlite3.Row],
                     conn: sqlite3.Connection) -> dict:
    """График вывоза по базам: остатки → отгрузка → реализация (с потерями).

    Структура листа «ЗС НКТ+ЛОМ»: остаток на начало месяца, план отгрузки,
    реализация = отгрузка × (1 − потери), остаток на конец. Перемещение —
    справочно. База позиции — поле «Поставщик».
    """
    loss = rate(bp, "shipment_loss_pct", conn, 3.0) / 100.0
    months = int(_f(bp["removal_months"]) or get_setting(conn, "removal_months", 6.0))
    volumes: dict[str, float] = {}
    texts: dict[str, list[str]] = {}
    for it in items:
        base = base_of(it)
        volumes[base] = volumes.get(base, 0.0) + _f(it["volume_t"])
        texts.setdefault(base, []).append(
            " ".join(str(_row_get(it, k) or "") for k in
                     ("warehouse", "division", "supplier", "note")))
    plan = {(r["base"], r["month"]): r for r in sched_rows}

    # Сезонность: на зимники технику пускают только зимой — месяцы вне окна
    # помечаются недоступными (в них не планируем отгрузку).
    rules = load_seasonal_rules(conn)
    start_month = start_month_number(_row_get(bp, "start_month"))
    calendar = [((start_month - 1 + i) % 12) + 1 if start_month else None
                for i in range(months)]

    bases = []
    month_totals = [{"shipment": 0.0, "sale": 0.0} for _ in range(months)]
    for base, volume in sorted(volumes.items(), key=lambda kv: -kv[1]):
        rule = seasonal_for(base + " " + " ".join(texts.get(base, [])), rules)
        balance = volume
        rows = []
        for m in range(1, months + 1):
            p = plan.get((base, m))
            ship = _f(p["shipment_t"]) if p else 0.0
            reloc = _f(p["relocation_t"]) if p else 0.0
            sale = ship * (1 - loss)
            cal = calendar[m - 1]
            available = not rule or cal is None or cal in rule["months"]
            rows.append({"month": m, "calendar_month": cal,
                         "available": available,
                         "balance_start": balance, "shipment": ship,
                         "sale": sale, "relocation": reloc,
                         "balance_end": balance - ship})
            balance -= ship
            month_totals[m - 1]["shipment"] += ship
            month_totals[m - 1]["sale"] += sale
        shipped = sum(r["shipment"] for r in rows)
        bases.append({"base": base, "volume": volume, "rows": rows,
                      "shipped": shipped, "sold": shipped * (1 - loss),
                      "left": volume - shipped,
                      "seasonal": rule["pattern"] if rule else None,
                      "seasonal_comment": rule["comment"] if rule else None,
                      "blocked": sum(1 for r in rows if not r["available"])})
    return {"months": months, "loss_pct": loss * 100.0, "bases": bases,
            "month_totals": month_totals,
            "start_month_number": start_month,
            "calendar": calendar,
            "total_volume": sum(volumes.values()),
            "total_shipped": sum(b["shipped"] for b in bases),
            "total_left": sum(b["left"] for b in bases)}


def plan_fact(bp: sqlite3.Row, items: list[sqlite3.Row], costs: list[sqlite3.Row],
              sched_rows: list[sqlite3.Row], fact_rows: list[sqlite3.Row],
              conn: sqlite3.Connection) -> dict:
    """План-факт по месяцам (заготовка, структура «анализ фин ЗС НКТ»).

    План: реализация из графика вывоза × средние показатели P&L на 1 тн.
    Факт: объём/выручка/затраты вводятся вручную (позже — загрузка из 1С);
    прибыль факта = выручка − закупка по средней цене лота − затраты.
    """
    p = pnl(bp, items, costs, conn)
    sched = removal_schedule(bp, items, sched_rows, conn)
    months = sched["months"]
    rev_per_t = p["revenue_per_t"]
    profit_per_t = (p["profit_before_tax"] / p["sale_volume"]
                    if p["sale_volume"] else 0.0)
    purchase_per_t = p["purchase_price_per_t"]
    fact = {r["month"]: r for r in fact_rows}

    rows, tot = [], {"plan_vol": 0.0, "plan_rev": 0.0, "plan_profit": 0.0,
                     "fact_vol": 0.0, "fact_rev": 0.0, "fact_profit": 0.0,
                     "has_fact": False}
    for m in range(1, months + 1):
        plan_vol = sched["month_totals"][m - 1]["sale"]
        plan_rev = plan_vol * rev_per_t
        plan_profit = plan_vol * profit_per_t
        f = fact.get(m)
        f_vol = _f(f["volume_t"]) if f else 0.0
        f_rev = _f(f["revenue"]) if f else 0.0
        f_costs = _f(f["costs"]) if f else 0.0
        has_fact = bool(f and (f_vol or f_rev or f_costs))
        f_profit = f_rev - f_vol * purchase_per_t - f_costs if has_fact else 0.0
        rows.append({
            "month": m, "plan_vol": plan_vol, "plan_rev": plan_rev,
            "plan_price": rev_per_t if plan_vol else 0.0,
            "plan_profit": plan_profit,
            "fact_vol": f_vol, "fact_rev": f_rev, "fact_costs": f_costs,
            "fact_price": f_rev / f_vol if f_vol else 0.0,
            "fact_profit": f_profit, "has_fact": has_fact,
            "dev_vol": f_vol - plan_vol if has_fact else None,
            "dev_rev": f_rev - plan_rev if has_fact else None,
            "comment": f["comment"] if f else None,
        })
        tot["plan_vol"] += plan_vol
        tot["plan_rev"] += plan_rev
        tot["plan_profit"] += plan_profit
        if has_fact:
            tot["has_fact"] = True
            tot["fact_vol"] += f_vol
            tot["fact_rev"] += f_rev
            tot["fact_profit"] += f_profit
    # Полный P&L по месяцам — структура листа «анализ фин»: каждая статья
    # раскладывается по месяцам пропорционально плановой реализации, в
    # рублях и руб/тн; колонка ИТОГО сверяется с P&L сделки.
    sale_total = p["sale_volume"] or 0.0
    costs = share_costs(bp, costs)
    section_sum = {s: sum(_f(c["amount"]) for c in costs if c["section"] == s)
                   for s in ("Переменные", "Персонал", "Постоянные",
                             "Административные", "Прочие")}
    metrics = [
        ("Выручка без НДС", p["revenue"], "Объём реализации месяца × средняя цена."),
        ("Себестоимость закупки", p["lot_cost"],
         "Доля стоимости лота, приходящаяся на реализацию месяца."),
        ("Маржинальная прибыль", p["gross_purchase"],
         "Выручка минус себестоимость закупки."),
        ("Переменные расходы", section_sum["Переменные"],
         "Транспорт, ПРР, газ и кислород."),
        ("Расходы на персонал", p["personnel_total"],
         "Зарплата, суточные, проживание и налоги с ФОТ."),
        ("Постоянные расходы", section_sum["Постоянные"],
         "Аренда, амортизация, распределяемые расходы баз."),
        ("Невозмещённый НДС", p["vat_unrecovered"],
         "Доля НДС, отнесённая на затраты при смене типа (труба → лом)."),
        ("Административные", section_sum["Административные"], "Ручные статьи."),
        ("Прочие расходы", section_sum["Прочие"], "Ручные статьи."),
        ("Стоимость капитала", p["capital_cost"],
         "Проценты за привлечённый капитал."),
        ("Прибыль до налога", p["profit_before_tax"], "Итог до налога."),
        ("Налог на прибыль", p["income_tax"], "Ставка × база с учётом НДС."),
        ("Чистая прибыль", p["net_profit"], "Итог сделки."),
    ]
    lines = []
    for label, total, hint in metrics:
        per_t_value = total / sale_total if sale_total else 0.0
        by_month = [{"month": r["month"],
                     "amount": per_t_value * r["plan_vol"],
                     "per_t": per_t_value} for r in rows]
        lines.append({"label": label, "hint": hint, "total": total,
                      "per_t": per_t_value, "months": by_month})
    return {"months": months, "rows": rows, "totals": tot, "lines": lines,
            "sale_volume": sale_total,
            "quarters": quarter_fact(bp, rows, sale_total),
            "purchase_per_t": purchase_per_t}


_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def start_year_number(value) -> int | None:
    """Год начала вывоза из строки «январь 2026» / «01.2026» / «2026-01»."""
    import re as _re
    m = _re.search(r"\b(20\d{2})\b", str(value or ""))
    return int(m.group(1)) if m else None


def quarter_fact(bp, rows: list[dict], total_volume: float) -> dict:
    """Квартальный срез плана и факта (лист «анализ фин ЗС НКТ»).

    По каждому кварталу: остаток лота на 01 число (сколько ещё не
    реализовано — отдельно по плану и по факту), план и факт квартала,
    накопленный итог и отклонение. Экономист смотрит не «месяц 7», а
    «III квартал», и сравнивает нарастающим итогом.

    Если месяц начала вывоза не задан, кварталы считаются от начала сделки
    («1–3 мес»): выдумывать календарь сервис не должен.

    База остатка — плановая реализация ИЗ ГРАФИКА, а не объём реализации из
    P&L: в P&L объём считается от закупки за вычетом засора, в графике — от
    отгрузки за вычетом потерь. Смешение баз давало «выполнено 102%» и
    отрицательный остаток на конец.
    """
    total_volume = sum(r["plan_vol"] for r in rows) or total_volume
    start_month = start_month_number(_row_get(bp, "start_month"))
    start_year = start_year_number(_row_get(bp, "start_month"))

    groups: list[dict] = []
    index: dict[tuple, int] = {}
    for r in rows:
        offset = r["month"] - 1
        if start_month:
            cal = (start_month - 1 + offset) % 12 + 1
            year = (start_year + (start_month - 1 + offset) // 12
                    if start_year else None)
            quarter = (cal - 1) // 3 + 1
            key = (year, quarter)
            label = f"{_ROMAN[quarter]} кв" + (f" {year}" if year else "")
        else:
            key = (None, offset // 3)
            label = f"{offset // 3 * 3 + 1}–{offset // 3 * 3 + 3} мес"
        if key not in index:
            index[key] = len(groups)
            groups.append({"label": label, "months": [], "plan_vol": 0.0,
                           "plan_rev": 0.0, "plan_profit": 0.0,
                           "fact_vol": 0.0, "fact_rev": 0.0,
                           "fact_profit": 0.0, "has_fact": False})
        g = groups[index[key]]
        g["months"].append(r["month"])
        g["plan_vol"] += r["plan_vol"]
        g["plan_rev"] += r["plan_rev"]
        g["plan_profit"] += r["plan_profit"]
        if r["has_fact"]:
            g["has_fact"] = True
            g["fact_vol"] += r["fact_vol"]
            g["fact_rev"] += r["fact_rev"]
            g["fact_profit"] += r["fact_profit"]

    cum = {"plan_vol": 0.0, "plan_rev": 0.0, "plan_profit": 0.0,
           "fact_vol": 0.0, "fact_rev": 0.0, "fact_profit": 0.0}
    for g in groups:
        # Остаток на 01 число: лот минус всё, реализованное до этого квартала.
        g["balance_plan"] = total_volume - cum["plan_vol"]
        g["balance_fact"] = total_volume - cum["fact_vol"]
        for key in cum:
            cum[key] += g[key]
            g["cum_" + key] = cum[key]
        g["dev_vol"] = g["fact_vol"] - g["plan_vol"] if g["has_fact"] else None
        g["dev_rev"] = g["fact_rev"] - g["plan_rev"] if g["has_fact"] else None
        g["dev_profit"] = (g["fact_profit"] - g["plan_profit"]
                           if g["has_fact"] else None)
        # Готовность лота: сколько тонн уже реализовано нарастающим итогом.
        g["done_plan_pct"] = (g["cum_plan_vol"] / total_volume * 100
                              if total_volume else 0.0)
        g["done_fact_pct"] = (g["cum_fact_vol"] / total_volume * 100
                              if total_volume else 0.0)
        g["balance_end_plan"] = total_volume - g["cum_plan_vol"]
        g["balance_end_fact"] = total_volume - g["cum_fact_vol"]
    return {"rows": groups, "total_volume": total_volume,
            "calendar": bool(start_month), "has_fact": any(g["has_fact"]
                                                           for g in groups)}


def auction_ladder(bp: sqlite3.Row, items: list[sqlite3.Row],
                   costs: list[sqlite3.Row], conn: sqlite3.Connection,
                   max_steps: int = 30) -> dict:
    """Аукционная лестница: экономика сделки на каждом шаге торгов.

    Стоимость лота на шаге N = порог + шаг × N; P&L пересчитывается целиком
    (капитал и налоги зависят от стоимости лота). Возвращает шаги и предельный
    шаг, при котором рентабельность продаж ещё не ниже порога.
    """
    start = _f(bp["lot_cost"])
    step = _f(bp["auction_step"])
    if not start or not step:
        return {"steps": [], "max_ok_step": None}
    steps, max_ok = [], None
    for s in range(0, max_steps + 1):
        bpd = _to_dict(bp)
        bpd["lot_cost"] = start + step * s
        p = pnl(bpd, items, costs, conn)
        ok = p["above_threshold"]
        if ok:
            max_ok = s
        # Аукцион идёт в ценах С НДС (лист «расчеты по лоту» книг экономистов):
        # показываем обе величины, чтобы на торгах не пересчитывать в уме.
        vat_k = 1 + rate(bp, "vat_rate", conn, 20.0) / 100.0
        steps.append({
            "step": s, "lot_cost": bpd["lot_cost"],
            "lot_cost_vat": bpd["lot_cost"] * vat_k,
            "price_per_t_vat": (p["purchase_price_per_t"] * vat_k),
            "step_cost": step * s,
            "step_cost_vat": step * s * vat_k,
            "price_per_t": p["purchase_price_per_t"],
            "net_profit": p["net_profit"],
            "net_profit_per_t": p["net_profit_per_t"],
            "ros_pct": p["ros_pct"], "ok": ok,
        })
        if p["net_profit"] < 0 and s >= 3:       # дальше лестницу не тянем
            break
    return {"steps": steps, "max_ok_step": max_ok,
            "vat_rate": rate(bp, "vat_rate", conn, 20.0),
            "threshold": get_setting(conn, "margin_threshold", 10.0)}


_RISK_SCORE = {"В": 3, "С": 2, "Н": 1}
_IMPACT_SCORE = {"К": 3, "З": 2, "Н": 1}

# В базе и в оценке риск хранится буквой (так его писали в книгах
# экономистов), а человеку показывается словом: «С» и «З» в таблице читались
# только теми, кто помнит шкалу. Значения не меняем — на буквах завязаны
# оценка integral_risk и уже заведённые БП.
PROBABILITY_LABELS = {"В": "Высокая", "С": "Средняя", "Н": "Низкая"}
IMPACT_LABELS = {"К": "Критическое", "З": "Значительное", "Н": "Незначительное"}


def risk_label(code: str, labels: dict) -> str:
    """Слово вместо буквы; неизвестный код показываем как есть."""
    return labels.get((code or "").strip(), code or "")


def integral_risk(risks: list[sqlite3.Row]) -> str:
    scores = [_RISK_SCORE.get(r["probability"] or "", 0) * _IMPACT_SCORE.get(r["impact"] or "", 0)
              for r in risks]
    top = max(scores, default=0)
    if top >= 6:
        return "Высокий"
    if top >= 3:
        return "Средний"
    return "Низкий" if top else "—"
