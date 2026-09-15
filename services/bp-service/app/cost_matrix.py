"""Матрица фактических затрат: тип сделки → статья → рублей на тонну.

Указание директора: считать не «плановые» затраты, назначенные на глаз, а
уровень, подтверждённый уже состоявшимися расчётами и фактом 1С. Матрица
собирается из двух источников и хранит их РАЗДЕЛЬНО — расхождение между
тем, что закладывали в расчёты, и тем, что показал факт, само по себе
информация, и смешивать их нельзя:

  «расчёты»  — статьи затрат бизнес-планов сервиса, делённые на тоннаж лота
               («что закладывали»);
  «факт 1С: регистр затрат» — с 15.09.2026 главный источник: регистр
               «Прочие расход (все)» по сериям (сделкам), руб/т проданного
               по закрытым сделкам (app/fact_costs.py);
  «факт 1С»  — прежние узкие выгрузки: переработка, транспорт (отвесная),
               распределяемые — оставлены для сравнения.

Медиана, а не среднее: одна сделка с наёмным транспортом через полстраны
сдвигает среднее так, что нормативом им пользоваться нельзя.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from statistics import median

from . import calc
from .db import get_setting

# С какой даты берём наблюдения (настройка cost_matrix_from).
DEFAULT_PERIOD_FROM = "2025-01-01"

SOURCE_BP = "расчёты"
SOURCE_1C_PROCESSING = "факт 1С: переработка"
SOURCE_1C_TRANSPORT = "факт 1С: транспорт"
SOURCE_1C_OVERHEAD = "факт 1С: распределяемые"

# Куда ложатся фактические ставки 1С в структуре статей P&L.
ITEM_TRANSPORT = "Транспортные расходы на отгрузку"
ITEM_PROCESSING = "Прочие производственные расходы"
ANY_TYPE = ""            # строка матрицы, не привязанная к типу сделки


def period_from(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = 'cost_matrix_from'"
                       ).fetchone()
    return (row["value"] if row and row["value"] else DEFAULT_PERIOD_FROM)


# Наблюдение с объёмом меньше этого не берём: ставка, посчитанная на
# сотне килограммов, — шум, а не норматив.
MIN_OBSERVATION_T = 1.0


def _stats(values: list[float]) -> tuple[float, float, float, int]:
    """Медиана, границы и сколько наблюдений отброшено как выбросы.

    Отсекаем по межквартильному размаху: в выгрузке попадаются строки, где
    сумма отнесена на почти нулевой выпуск, и одна такая уводила коридор на
    два миллиона рублей за тонну при медиане в тысячу.
    """
    values = sorted(values)
    if len(values) >= 5:
        q1 = median(values[:len(values) // 2])
        q3 = median(values[(len(values) + 1) // 2:])
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        kept = [v for v in values if lo <= v <= hi] or values
    else:
        kept = values
    return (round(median(kept), 2), round(min(kept), 2), round(max(kept), 2),
            len(values) - len(kept))


def _collect_bp(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Статьи затрат бизнес-планов сервиса → рублей на тонну лота.

    Берём базовый вариант расчёта (ДСП): вариант «Лукойл» — это тот же лот
    с другими ценами, и его суммы удвоили бы наблюдения по одной сделке.
    """
    rows = conn.execute(
        "SELECT b.id, b.bp_number, b.bp_type, b.created_at, "
        "       (SELECT SUM(volume_t) FROM bp_items WHERE bp_id = b.id) AS tons "
        "FROM business_plans b "
        "WHERE b.bp_type IS NOT NULL AND b.archived = 0 "
        "  AND COALESCE(b.created_at, '') >= ? "
        "ORDER BY b.id", (since,)).fetchall()
    out: list[dict] = []
    for bp in rows:
        tons = float(bp["tons"] or 0)
        if tons <= 0:
            continue
        for c in conn.execute(
                "SELECT section, item, amount FROM bp_costs WHERE bp_id = ?",
                (bp["id"],)).fetchall():
            amount = float(c["amount"] or 0)
            if amount <= 0:
                continue
            out.append({"bp_type": bp["bp_type"], "section": c["section"],
                        "item": c["item"], "rub_per_t": amount / tons,
                        "tons": tons, "period": (bp["created_at"] or "")[:10],
                        "source": SOURCE_BP})
    return out


def _type_of_group(conn: sqlite3.Connection, group: str | None) -> str:
    """Группа аналитического учёта 1С → код типа сделки.

    Через тот же разбор, что и у позиций (calc.type_of_group), а затем по
    справочнику типов: «Лом цветных металлов» → цветмет → nonferrous.
    """
    item_type = calc.type_of_group(group, "")
    if not item_type:
        return ANY_TYPE
    row = conn.execute(
        "SELECT code FROM ref_bp_types WHERE ';' || lower_ru(item_types) || ';' "
        "LIKE '%;' || lower_ru(?) || ';%'", (item_type,)).fetchone()
    if row:
        return row["code"]
    # Пробел после «;» в справочнике заполняется людьми по-разному.
    for r in conn.execute("SELECT code, item_types FROM ref_bp_types").fetchall():
        parts = [p.strip().lower() for p in (r["item_types"] or "").split(";")]
        if item_type.lower() in parts:
            return r["code"]
    return ANY_TYPE


def _collect_1c(conn: sqlite3.Connection) -> list[dict]:
    """Фактические ставки из выгрузок 1С, приведённые к рублям на тонну."""
    out: list[dict] = []

    # Переработка: сумма ставок ФОТ, ПРР, ГСМ и амортизации на единицу
    # выпуска. Только строки в тоннах: у кабельного сдира и комплектующих с
    # разбора единицей учёта идут штуки, литры и метры, и такая ставка в
    # рубли на тонну не переводится — со штуками медиана уезжала в сотни
    # тысяч рублей за тонну.
    try:
        proc = conn.execute(
            "SELECT work_type, cargo_group, samples, total_qty, rate_fot, "
            "rate_prr, rate_gsm, rate_amort FROM stat_processing "
            "WHERE total_qty > 0 AND lower_ru(COALESCE(unit, '')) IN ('т', 'тн', "
            "'тонн', 'тонна')").fetchall()
    except sqlite3.Error:
        proc = []
    for r in proc:
        rate = sum(float(r[k] or 0) for k in
                   ("rate_fot", "rate_prr", "rate_gsm", "rate_amort"))
        if rate <= 0 or float(r["total_qty"] or 0) < MIN_OBSERVATION_T:
            continue
        out.append({"bp_type": _type_of_group(conn, r["cargo_group"]),
                    "section": "Постоянные", "item": ITEM_PROCESSING,
                    "rub_per_t": rate, "tons": float(r["total_qty"] or 0),
                    "period": "", "source": SOURCE_1C_PROCESSING,
                    "note": f"{r['work_type']} · {r['cargo_group'] or 'без группы'}"})

    # Транспорт: фактическая стоимость рейсов, делённая на вывезенный вес.
    try:
        tr = conn.execute(
            "SELECT delivery_kind, samples, total_t, rub_per_t FROM stat_transport "
            "WHERE rub_per_t > 0").fetchall()
    except sqlite3.Error:
        tr = []
    for r in tr:
        out.append({"bp_type": ANY_TYPE, "section": "Переменные",
                    "item": ITEM_TRANSPORT, "rub_per_t": float(r["rub_per_t"]),
                    "tons": float(r["total_t"] or 0), "period": "",
                    "source": SOURCE_1C_TRANSPORT,
                    "note": r["delivery_kind"]})

    # Распределяемые расходы: косвенные затраты подразделений, делённые на
    # объём операций периода (та же ставка, что показывает блок обоснования).
    from . import norms
    rate = norms.overhead_rate(conn)
    if rate.get("rate_per_t"):
        out.append({"bp_type": ANY_TYPE, "section": "Постоянные",
                    "item": ITEM_PROCESSING, "rub_per_t": float(rate["rate_per_t"]),
                    "tons": float(rate.get("volume_t") or 0), "period": "",
                    "source": SOURCE_1C_OVERHEAD,
                    "note": f"{rate.get('months', 0)} мес, {rate.get('source', '')}"})
    return out


def build(conn: sqlite3.Connection) -> dict:
    """Пересборка матрицы. Возвращает сводку: сколько строк и наблюдений."""
    since = period_from(conn)
    rows = _collect_bp(conn, since) + _collect_1c(conn)
    # Главный источник с 15.09.2026: регистр затрат 1С по сериям (на нашу долю,
    # по закрытым сделкам). Старые источники остаются рядом для сравнения.
    from . import fact_costs
    try:
        rows += fact_costs.observations(conn, since)
    except sqlite3.Error:
        pass

    agg: dict[tuple, list] = defaultdict(list)
    notes: dict[tuple, set] = defaultdict(set)
    periods: dict[tuple, list] = defaultdict(list)
    tons: dict[tuple, float] = defaultdict(float)
    for r in rows:
        key = (r["bp_type"] or ANY_TYPE, r["section"], r["item"], r["source"])
        agg[key].append(r["rub_per_t"])
        tons[key] += r["tons"]
        if r.get("note"):
            notes[key].add(r["note"])
        if r.get("period"):
            periods[key].append(r["period"])

    conn.execute("DELETE FROM stat_cost_matrix")
    for key, values in sorted(agg.items()):
        bp_type, section, item, source = key
        mid, lo, hi, dropped = _stats(values)
        per = sorted(periods[key])
        note = "; ".join(sorted(notes[key])[:4]) or None
        if len(notes[key]) > 4:
            note += f" и ещё {len(notes[key]) - 4}"
        conn.execute(
            "INSERT INTO stat_cost_matrix (bp_type, section, item, source, "
            "samples, total_t, rub_per_t, p_min, p_max, period_min, period_max, "
            "note, outliers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bp_type, section, item, source, len(values), round(tons[key], 2),
             mid, lo, hi, per[0] if per else None, per[-1] if per else None,
             note, dropped))
    return {"rows": len(agg), "observations": len(rows), "since": since}


def for_type(conn: sqlite3.Connection, bp_type: str | None) -> list[sqlite3.Row]:
    """Строки матрицы, применимые к сделке: её тип плюс общие."""
    return conn.execute(
        "SELECT * FROM stat_cost_matrix WHERE bp_type = ? OR bp_type = '' "
        "ORDER BY section, item, source", (bp_type or "",)).fetchall()


def hint(conn: sqlite3.Connection, bp_type: str | None, section: str,
         item: str) -> sqlite3.Row | None:
    """Ориентир по статье: сначала свой тип, затем общий по всем сделкам."""
    return conn.execute(
        "SELECT * FROM stat_cost_matrix WHERE section = ? AND item = ? "
        "AND bp_type IN (?, '') ORDER BY bp_type = '', source <> ?, samples DESC LIMIT 1",
        (section, item, bp_type or "", "факт 1С: регистр затрат")).fetchone()


def hint_pair(conn: sqlite3.Connection, bp_type: str | None, section: str,
              item: str) -> dict:
    """Факт регистра и «что закладывали» (расчёты) рядом — для карточки:
    разница между ними и есть обучение (указание директора)."""
    fact = conn.execute(
        "SELECT * FROM stat_cost_matrix WHERE section = ? AND item = ? AND source = ? "
        "AND bp_type IN (?, '') ORDER BY bp_type = '', samples DESC LIMIT 1",
        (section, item, "факт 1С: регистр затрат", bp_type or "")).fetchone()
    plan = conn.execute(
        "SELECT * FROM stat_cost_matrix WHERE section = ? AND item = ? AND source = ? "
        "AND bp_type IN (?, '') ORDER BY bp_type = '', samples DESC LIMIT 1",
        (section, item, SOURCE_BP, bp_type or "")).fetchone()
    return {"fact": fact, "plan": plan}


def all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stat_cost_matrix ORDER BY bp_type = '', bp_type, "
        "section, item, source").fetchall()
