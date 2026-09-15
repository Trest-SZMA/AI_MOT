"""Калибровка нормативов фактом из регистров 1С.

44 норматива модели затрат (цена ДТ, наём руб/т, кислород на резку, пресс
руб/т, ж/д за вагон …) взяты в июле 2026 из книги 1935 и листов Усинск/Ухта.
Теперь по тем же величинам есть факт. Норматив НЕ перезаписывается: рядом
появляется фактическое значение с датой, числом наблюдений и способом
расчёта, а при расхождении больше порога — предупреждение в справочнике
и в модели затрат. Решение остаётся за экономистом.

Источники:
  stat_fact_costs      — регистр «Прочие расход (все)» по сериям (наём,
                         кран, ж/д — руб/т проданного по закрытым сделкам);
  stat_processing      — «Производственная себестоимость» (резка, пресс:
                         ФОТ, ПРР, ГСМ, амортизация, материалы руб/т);
  stat_transport_rates — ставки перевозчика из приказа (руб/км).
"""
from __future__ import annotations

import sqlite3
from statistics import median


DEVIATION_PCT = 25.0            # больше — предупреждение


def _median_rub_per_t(conn, item: str, since: str) -> tuple[float | None, int]:
    rows = conn.execute(
        "SELECT SUM(amount) AS a, MAX(sold_t) AS t FROM stat_fact_costs "
        "WHERE closed = 1 AND item = ? AND period_max >= ? GROUP BY series",
        (item, since)).fetchall()
    vals = [float(r["a"]) / float(r["t"]) for r in rows if r["t"] and float(r["t"]) > 0 and float(r["a"]) > 0]
    return (round(median(vals), 2), len(vals)) if vals else (None, 0)


def _processing(conn, work_like: str, col: str) -> tuple[float | None, int]:
    """Средневзвешенная ставка руб/т по виду работ (только тонны)."""
    r = conn.execute(
        f"SELECT SUM({col} * total_qty) AS s, SUM(total_qty) AS q, SUM(samples) AS n "
        "FROM stat_processing WHERE work_type LIKE ? AND unit IN ('т', 'тн', 'тонн', 'тонна') "
        f"AND {col} IS NOT NULL", (work_like,)).fetchone()
    if not r or not r["q"]:
        return None, 0
    return round(float(r["s"]) / float(r["q"]), 2), int(r["n"] or 0)


def build(conn: sqlite3.Connection) -> dict:
    from . import cost_matrix
    since = cost_matrix.period_from(conn)
    facts: list[tuple[str, float | None, str, int, str, str]] = []

    v, n = _median_rub_per_t(conn, "Транспортные расходы на перемещение", since)
    facts.append(("hired_transport_rate_t", v, "руб/тн", n,
                  "медиана по закрытым сделкам: найм а/м на перемещение / ВСЕ проданные тн "
                  "(норматив — на тонну, которую везёт наём; ниже факта на весь лот быть не может, "
                  "разница показывает долю наёмного перемещения)",
                  "регистр затрат 1С"))
    v, n = _median_rub_per_t(conn, "Погрузочно-разгрузочные расходы", since)
    facts.append(("crane_rate_t", v, "руб/тн", n,
                  "медиана по закрытым сделкам: найм крана / продано тн", "регистр затрат 1С"))
    # Резка ручная: материалы (кислород, пропан, бензин) руб/т против
    # нашего oxygen_rate_per_t × oxygen_price + бензин.
    v, n = _processing(conn, "Резка ручная%", "rate_mat")
    facts.append(("oxygen_cost_per_t", v, "руб/тн", n,
                  "материалы на ручную газокислородную резку / тн выпуска",
                  "производственная себестоимость 1С"))
    v, n = _processing(conn, "Резка ручная%", "rate_fot")
    facts.append(("cutter_fot_per_t", v, "руб/тн", n,
                  "ФОТ на ручную резку / тн выпуска", "производственная себестоимость 1С"))
    # Пресс: ГСМ + амортизация руб/т против press_cost_per_t (+ амортизация отдельно).
    v_g, n = _processing(conn, "Резка механизированная%", "rate_gsm")
    facts.append(("press_cost_per_t", v_g, "руб/тн", n,
                  "ГСМ/энергия пресс-ножниц / тн выпуска", "производственная себестоимость 1С"))
    v_a, n = _processing(conn, "Резка механизированная%", "rate_amort")
    facts.append(("press_amort_per_t", v_a, "руб/тн", n,
                  "амортизация пресс-ножниц / тн выпуска", "производственная себестоимость 1С"))
    v, n = _processing(conn, "Резка механизированная%", "rate_fot")
    facts.append(("press_fot_per_t", v, "руб/тн", n,
                  "ФОТ оператора пресса / тн выпуска", "производственная себестоимость 1С"))
    # Ставка перевозчика руб/км — из приказа (ref: stat_transport_rates), если есть.
    try:
        r = conn.execute(
            "SELECT AVG(price) AS p, COUNT(*) AS n FROM transport_rates "
            "WHERE unit LIKE '%км%' AND kind = 'current'").fetchone()
        if r and r["p"]:
            facts.append(("hired_rate_per_km", round(float(r["p"]), 2), "руб/км", int(r["n"]),
                          "средняя рабочая ставка перевозчика за км", "приказ о транспортных ставках"))
    except sqlite3.Error:
        pass

    # Распределяемые по базам: ставка руб/т из статей без серии по
    # подразделению-базе. Общая — медиана по базам; по каждой базе — своя
    # строка (ключ base_overhead_per_t@<база>) для нормативов с базой.
    try:
        import os
        from . import fact_costs
        snap = os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "out", "sales_data.json")
        rates = [r for r in fact_costs.base_overhead_rates(conn, snap) if r.get("rate_per_t")]
        if rates:
            facts.append(("base_overhead_per_t", round(median(r["rate_per_t"] for r in rates), 2),
                          "руб/тн", len(rates),
                          "медиана по базам: статьи без серии за 12 мес / тоннаж базы",
                          "регистр затрат 1С"))
            for r in rates:
                facts.append((f"base_overhead_per_t@{r['division']}", r["rate_per_t"], "руб/тн",
                              r["months"], f"{r['division']}: {round(r['amount'] / 1e6, 1)} млн за "
                              f"{r['months']} мес / {r['tons']} тн", "регистр затрат 1С"))
    except Exception:
        pass

    conn.execute("DELETE FROM norm_fact")
    written = 0
    for key, value, unit, n, basis, source in facts:
        if value is None:
            continue
        conn.execute(
            "INSERT INTO norm_fact (key, fact_value, unit, samples, basis, source, period_from, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (key, value, unit, n, basis, source, since))
        written += 1
    from . import refsources
    refsources.mark(conn, "norm_fact", written, None, "калибровка фактом")
    return {"written": written, "since": since}


def for_norms(conn: sqlite3.Connection) -> dict[str, dict]:
    """key → факт с отклонением от норматива (для карточки нормативов).

    Некоторые факты сравниваются не с одним нормативом, а с произведением
    (кислород: ед/т × цена): такие ключи-агрегаты считаются здесь.
    """
    try:
        facts = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM norm_fact")}
    except sqlite3.Error:
        return {}
    norms = {(r["base"], r["key"]): float(r["value"]) for r in conn.execute("SELECT base, key, value FROM cost_norms")}
    g = lambda k: norms.get((None, k))
    out: dict[str, dict] = {}

    def put(norm_key: str, fact_key: str, norm_value: float | None):
        f = facts.get(fact_key)
        if f is None or norm_value is None:
            return
        dev = (f["fact_value"] - norm_value) / norm_value * 100.0 if norm_value else None
        out[norm_key] = {**f, "norm_value": norm_value, "deviation_pct": dev,
                         "warn": dev is not None and abs(dev) >= DEVIATION_PCT}

    put("hired_transport_rate_t", "hired_transport_rate_t", g("hired_transport_rate_t"))
    put("base_overhead_per_t", "base_overhead_per_t", g("base_overhead_per_t"))
    # Нормативы с базой: факт своей базы по имени подразделения 1С.
    for (base, key), val in norms.items():
        if base and key == "base_overhead_per_t":
            # Норматив базы «Усинск» — это подразделение «База Усинск» 1С;
            # одноимённый цех («Усинск») — другая площадка.
            wanted = f"base_overhead_per_t@база {base.lower()}"
            for fk, f in facts.items():
                if fk.lower() == wanted or (fk.lower() == f"base_overhead_per_t@{base.lower()}"
                                            and wanted not in {k.lower() for k in facts}):
                    dev = (f["fact_value"] - val) / val * 100.0 if val else None
                    out[f"{key}@{base}"] = {**f, "norm_value": val, "deviation_pct": dev,
                                            "warn": dev is not None and abs(dev) >= DEVIATION_PCT}
    put("press_cost_per_t", "press_cost_per_t", g("press_cost_per_t"))
    if g("oxygen_rate_per_t") and g("oxygen_price"):
        put("oxygen_rate_per_t", "oxygen_cost_per_t", g("oxygen_rate_per_t") * g("oxygen_price"))
    # ФОТ резчика: норматив руб/мес и норма тн/смену × смен → руб/т
    if g("cutter_salary_month") and g("cut_rate_t_shift") and g("shifts_per_month"):
        put("cutter_salary_month", "cutter_fot_per_t",
            g("cutter_salary_month") / (g("cut_rate_t_shift") * g("shifts_per_month")))
    if g("press_salary_month") and g("press_rate_t_shift") and g("shifts_per_month"):
        put("press_salary_month", "press_fot_per_t",
            g("press_salary_month") / (g("press_rate_t_shift") * g("shifts_per_month")))
    if g("press_depreciation_month") and g("press_rate_t_shift") and g("shifts_per_month"):
        put("press_depreciation_month", "press_amort_per_t",
            g("press_depreciation_month") / (g("press_rate_t_shift") * g("shifts_per_month")))
    return out


def all_facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute("SELECT * FROM norm_fact ORDER BY key").fetchall()
    except sqlite3.Error:
        return []
