"""Фактические затраты по сделкам из регистра 1С «Прочие расход (все)».

Указание директора (26.08.2026): считать бизнес-план от реальных затрат.
До 15.09.2026 матрица собиралась из наших же расчётов и трёх узких выгрузок;
теперь в «Extractor» есть регистр затрат по статьям с колонкой «Серия»
(= сделка): 565 серий с 2025 года, 110 млн найм на отгрузку, 68 млн списание
засора, 61 млн найм на перемещение и т. д. — и это факт на нашу долю, потому
что 1С ведёт учёт только своей части лота.

Цепочка: `pull_1c.py` выгружает из регистра строки с серией (файл
`_Прочие_расход_серии__<штамп>.csv`, только нужные колонки) → `import_file`
складывает суммы по (серия, статья 1С), переводит статью 1С в статью сервиса
по `ref_cost_item_map`, берёт тоннаж и тип серии из снимка «Реализации» →
`stat_fact_costs`. Матрица затрат (`cost_matrix`) читает отсюда источник
«факт 1С: регистр затрат» — руб/т проданного по закрытым сделкам.

Тоннаж — ПРОДАННЫЙ по серии (снимок «Реализации», продажи регистра), а не
купленный: затраты на отгрузку и перемещение возникают на том, что уехало.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import deals
from .db import get_setting

# Колонки регистра, которые выгружаем (остальные 45 не нужны для матрицы).
COLUMNS = ["Период", "Подразделение", "СтатьяРасходов", "Серия", "Склад",
           "СуммаБезНДС", "ВидДоставки", "Километраж", "Операция",
           "ГруппаАналитическогоУчета"]
FILE_PREFIX = "_Прочие_расход_серии_"
SOURCE = "факт 1С: регистр затрат"

# Секции и статьи сервиса (как в bp_costs).
V = "Переменные"
P = "Персонал"
F = "Постоянные"
SHIP = "Транспортные расходы на отгрузку"
MOVE = "Транспортные расходы на перемещение"
PRR = "Погрузочно-разгрузочные расходы"
OWN = "Транспортные расходы — собственная техника"
GAS = "Заправка газом и кислородом"
OTHER = "Прочие производственные расходы"
CONTAM = "Списание засора"          # статьи только факта — плана по ним нет

# Начальное соответствие статей 1С статьям сервиса (раздел 3 записки
# «Логика расчёта с фактом», 15.09.2026). Правится на «Справочниках».
# (статья 1С или префикс, секция, статья, только-факт)
DEFAULT_MAP: list[tuple[str, str | None, str | None, int]] = [
    ("Найм а/м на отгрузку (23)", V, SHIP, 0),
    ("Ж/д тариф (провозная плата за вагоны) (ж/д найм) (23)", V, SHIP, 0),
    ("Расходы на взвешивание вагонов (ж/д найм) (23)", V, SHIP, 0),
    ("Расходы на взвешивание вагонов (ж/д самовывоз) (23)", V, SHIP, 0),
    ("Услуги подачи/уборки вагонов (ж/д найм) (23)", V, SHIP, 0),
    ("Услуги подачи/уборки вагонов (ж/д самовывоз) (23)", V, SHIP, 0),
    ("Использование ж/д путей, простой вагонов (ж/д найм) (23)", V, SHIP, 0),
    ("Использование ж/д путей, простой вагонов (ж/д самовывоз) (23)", V, SHIP, 0),
    ("Расходы на радиационный контроль (ж/д найм) (23)", V, SHIP, 0),
    ("Расходы на радиационный контроль (ж/д самовывоз) (23)", V, SHIP, 0),
    ("Расходы на радиационный контроль а/трансп при продаже (23)", V, SHIP, 0),
    ("Расходы на взвешивание а/трансп при продаже (23)", V, SHIP, 0),
    ("Расходы на укрытие, увязку, упаковку грузов при продаже (25)", V, SHIP, 0),
    ("Расходы на страхование груза при продаже (23)", V, SHIP, 0),
    ("Расходы прочие логистические при продаже (23)", V, SHIP, 0),
    ("Найм а/м на перемещение (23)", V, MOVE, 0),
    ("Найм а/м на приобретение (23)", V, MOVE, 0),
    ("Расходы на страхование груза при перемещении (23)", V, MOVE, 0),
    ("Расходы на взвешивание а/трансп при перемещении (23)", V, MOVE, 0),
    ("Расходы прочие логистические при перемещении (23)", V, MOVE, 0),
    ("Услуги подачи/уборки вагонов (ж/д перемещение найм) (23)", V, MOVE, 0),
    ("Использование ж/д путей, простой вагонов (ж/д перемещение найм) (23)", V, MOVE, 0),
    ("Найм крана на отгрузку (23)", V, PRR, 0),
    ("Найм крана на перемещение (23)", V, PRR, 0),
    ("(1) ", V, OWN, 0),                       # ГСМ/амортизация/ФОТ/налоги своих машин
    ("(2) ", V, OWN, 0),                       # то же на спец. работы
    ("(8) Амортизация ТС при производственных работах", V, OWN, 0),
    ("Резка (реализация)", V, GAS, 0),
    ("Производственные расходы на переработку ТМЦ сторонними организациями", F, OTHER, 0),
    ("Производственные расходы прошлых периодов", F, OTHER, 0),
    ("Рециклинг (26)", F, OTHER, 0),
    ("Списание засора при продаже", V, CONTAM, 1),
    ("Списание засора при перемещении", V, CONTAM, 1),
    ("Списание засора", V, CONTAM, 1),
    ("_Списание засора на проект (25)", V, CONTAM, 1),
    # не относится к экономике сделки — ниже операционной прибыли / учётные
    ("Инвентаризация (91)", None, None, 0),
    ("Выбытия товаров в прошлых периодах", None, None, 0),
    ("Разницы стоимости возврата и фактической стоимости товаров", None, None, 0),
    ("Прибыль/убыток прошлых лет (91)", None, None, 0),
    ("Отклонение в стоимости товаров", None, None, 0),
    ("Недосдача ТМЦ", None, None, 0),
]


def seed_map(conn: sqlite3.Connection) -> None:
    """Начальное соответствие — один раз; дальше правится в справочнике."""
    for item_1c, section, item, fact_only in DEFAULT_MAP:
        conn.execute(
            "INSERT OR IGNORE INTO ref_cost_item_map (item_1c, section, item, "
            "is_fact_only, note) VALUES (?, ?, ?, ?, ?)",
            (item_1c, section, item, fact_only,
             "типовое соответствие 15.09.2026" if section else
             "не относится к экономике сделки"))


def load_map(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ref_cost_item_map "
                        "ORDER BY LENGTH(item_1c) DESC").fetchall()


def map_item(mapping: list[sqlite3.Row], item_1c: str) -> sqlite3.Row | None:
    """Точное совпадение, иначе самый длинный префикс («(1) …»)."""
    t = (item_1c or "").strip()
    for m in mapping:
        if m["item_1c"] == t:
            return m
    for m in mapping:
        if t.startswith(m["item_1c"]):
            return m
    return None


_REQ = re.compile(r"^\s*(\d{3,4})\b")


def request_no_of_series(series: str) -> str | None:
    m = _REQ.match(series or "")
    return m.group(1) if m else None


def _series_index(snapshot_path: str | Path, conn: sqlite3.Connection) -> dict[str, dict]:
    """Имя серии → {bought_t, sold_t, bp_type, direction} из снимка «Реализации»."""
    import json
    from . import type_margin
    with open(snapshot_path, encoding="utf-8") as fh:
        d = json.load(fh)
    gmap = type_margin.group_map(conn)
    dominance = get_setting(conn, "bp_type_threshold_pct", 80.0)
    bp_nm = d.get("bp_nm") or {}
    out: dict[str, dict] = {}
    for r in d.get("bpbuy") or []:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        groups = ((bp_nm.get(r.get("s")) or {}).get("т") or {}).get("g") or {}
        total = sum(float(v) for v in groups.values())
        code = None
        if total > 0:
            tons: dict[str, float] = {}
            for g, v in groups.items():
                c = gmap.get(str(g).strip().lower())
                if c:
                    tons[c] = tons.get(c, 0.0) + float(v)
            if tons:
                c, top = max(tons.items(), key=lambda kv: kv[1])
                code = c if top / total * 100.0 + 1e-9 >= dominance else "mixed"
        out[name] = {"bought_t": float(r.get("t") or 0), "sold_t": float(r.get("sold") or 0),
                     "bp_type": code, "direction": r.get("dir")}
    return out


def newest_file(folder: str | Path) -> Path | None:
    files = sorted(Path(folder).glob(FILE_PREFIX + "_*.csv"))
    return files[-1] if files else None


def import_file(conn: sqlite3.Connection, csv_path: str | Path,
                snapshot_path: str | Path | None) -> dict:
    """CSV регистра (строки с серией) → stat_fact_costs."""
    csv.field_size_limit(10 ** 7)
    seed_map(conn)
    mapping = load_map(conn)
    idx = _series_index(snapshot_path, conn) if snapshot_path and Path(snapshot_path).is_file() else {}
    closed_pct = get_setting(conn, "type_margin_closed_pct", 80.0)
    agg: dict[tuple[str, str], list] = defaultdict(lambda: [0.0, 0, None, None])
    by_div: dict[tuple[str, str], list] = defaultdict(lambda: [0.0, 0])
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            series = (r.get("Серия") or "").strip()
            item_1c = (r.get("СтатьяРасходов") or "").strip()
            if not series or not item_1c:
                continue
            try:
                amount = float(r.get("СуммаБезНДС") or 0)
            except ValueError:
                continue
            period = (r.get("Период") or "")[:10]
            div = (r.get("Подразделение") or "").strip()
            if div:
                by_div[(series, div)][0] += amount
                by_div[(series, div)][1] += 1
            a = agg[(series, item_1c)]
            a[0] += amount
            a[1] += 1
            a[2] = period if a[2] is None or period < a[2] else a[2]
            a[3] = period if a[3] is None or period > a[3] else a[3]
    conn.execute("DELETE FROM stat_fact_costs")
    conn.execute("DELETE FROM stat_fact_costs_div")
    for (series, div), (amount, n) in by_div.items():
        if abs(amount) >= 0.005:
            conn.execute("INSERT INTO stat_fact_costs_div (series, deal_no, division, amount, rows_n) "
                         "VALUES (?, ?, ?, ?, ?)", (series, request_no_of_series(series), div, round(amount, 2), n))
    st = {"series": len({k[0] for k in agg}), "rows": 0, "unmapped": set(),
          "with_tons": 0, "amount": 0.0}
    for (series, item_1c), (amount, n, pmin, pmax) in agg.items():
        if abs(amount) < 0.005:
            continue
        m = map_item(mapping, item_1c)
        if m is None:
            st["unmapped"].add(item_1c)
        info = idx.get(series) or {}
        sold, bought = info.get("sold_t"), info.get("bought_t")
        closed = int(bool(bought) and sold is not None and sold / bought * 100.0 + 1e-9 >= closed_pct)
        conn.execute(
            "INSERT INTO stat_fact_costs (series, deal_no, bp_type, direction, item_1c, "
            "section, item, amount, rows_n, period_min, period_max, bought_t, sold_t, "
            "closed, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (series, request_no_of_series(series), info.get("bp_type"), info.get("direction"),
             item_1c, m["section"] if m else None, m["item"] if m else None,
             round(amount, 2), n, pmin, pmax, bought, sold, closed))
        st["rows"] += 1
        st["amount"] += amount
        if sold:
            st["with_tons"] += 1
    from . import refsources
    refsources.mark(conn, "stat_fact_costs", st["rows"], Path(csv_path).name,
                    "ночной импорт из Extractor")
    st["unmapped"] = sorted(st["unmapped"])
    return st


def observations(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Для матрицы: по закрытым сделкам — (тип, секция, статья) → руб/т проданного.

    Одно наблюдение = одна серия × статья сервиса (статьи 1С внутри сложены).
    Не сопоставленные статьи и статьи «только факт» в матрицу плана не идут —
    у них нет строки в bp_costs, сравнивать не с чем; они видны отдельно.
    """
    rows = conn.execute(
        "SELECT series, deal_no, bp_type, section, item, SUM(amount) AS amount, "
        "MAX(sold_t) AS sold_t, MAX(period_max) AS period_max, MIN(period_min) AS period_min "
        "FROM stat_fact_costs WHERE closed = 1 AND item IS NOT NULL AND section IS NOT NULL "
        "AND period_max >= ? GROUP BY series, deal_no, bp_type, section, item",
        (since,)).fetchall()
    out = []
    for r in rows:
        sold = float(r["sold_t"] or 0)
        if sold <= 0 or float(r["amount"]) <= 0:
            continue
        out.append({"bp_type": r["bp_type"], "section": r["section"], "item": r["item"],
                    "rub_per_t": float(r["amount"]) / sold, "tons": sold,
                    "period": r["period_max"] or "", "source": SOURCE,
                    "note": f"сделка {r['deal_no'] or r['series'][:12]}"})
    return out


def for_deal(conn: sqlite3.Connection, deal_no: str | None) -> list[sqlite3.Row]:
    """Факт по статьям одной сделки (все серии с этим номером запроса)."""
    if not deal_no:
        return []
    mapping = load_map(conn)
    rows = conn.execute(
        "SELECT section, item, item_1c, SUM(amount) AS amount, SUM(rows_n) AS rows_n, "
        "MIN(period_min) AS period_min, MAX(period_max) AS period_max "
        "FROM stat_fact_costs WHERE deal_no = ? GROUP BY section, item, item_1c",
        (deal_no,)).fetchall()
    agg: dict[tuple, dict] = {}
    for r in rows:
        m = map_item(mapping, r["item_1c"])
        key = (r["section"], r["item"], int(m["is_fact_only"]) if m else 0)
        a = agg.setdefault(key, {"section": r["section"], "item": r["item"],
                                 "is_fact_only": key[2], "amount": 0.0, "rows_n": 0,
                                 "period_min": None, "period_max": None, "items_1c": []})
        a["amount"] += float(r["amount"])
        a["rows_n"] += int(r["rows_n"])
        a["items_1c"].append(r["item_1c"])
        a["period_min"] = min(x for x in (a["period_min"], r["period_min"]) if x) if (a["period_min"] or r["period_min"]) else None
        a["period_max"] = max(x for x in (a["period_max"], r["period_max"]) if x) if (a["period_max"] or r["period_max"]) else None
    return sorted(agg.values(), key=lambda a: (a["section"] is None, a["section"] or "", -a["amount"]))


def summary(conn: sqlite3.Connection) -> dict:
    try:
        r = conn.execute(
            "SELECT COUNT(DISTINCT series) AS series, COUNT(*) AS rows_n, SUM(amount) AS amount, "
            "SUM(CASE WHEN item IS NULL AND section IS NULL THEN amount ELSE 0 END) AS unmapped_amount, "
            "SUM(CASE WHEN closed = 1 THEN 1 ELSE 0 END) AS closed_rows, "
            "MIN(period_min) AS pmin, MAX(period_max) AS pmax, MAX(updated_at) AS updated "
            "FROM stat_fact_costs").fetchone()
        unmapped = conn.execute(
            "SELECT item_1c, SUM(amount) AS amount FROM stat_fact_costs f "
            "WHERE NOT EXISTS (SELECT 1 FROM ref_cost_item_map m WHERE m.item_1c = f.item_1c "
            "OR f.item_1c LIKE m.item_1c || '%') GROUP BY item_1c ORDER BY amount DESC").fetchall()
        return {**dict(r), "unmapped": unmapped}
    except sqlite3.Error:
        return {"series": 0, "rows_n": 0, "amount": 0, "unmapped": []}


# ── Распределяемые: статьи без серии по подразделению ───────────────

OVERHEAD_PREFIX = "_Прочие_расход_распределяемые_"
# Статьи, которые не относятся к экономике площадки: ниже операционной
# прибыли, корпоративные, учётные. В ставку базы не входят.
OVERHEAD_EXCLUDE = ("Дивиденды", "Проценты по займам", "Инвентаризация", "Прибыль/убыток",
                    "Выбытия товаров", "Отклонение в стоимости", "Разницы стоимости",
                    "Погрешность расчета", "Списание засора", "Найм а/м", "Найм крана",
                    "Ж/д тариф", "Резка (реализация)")


def newest_overhead_file(folder: str | Path) -> Path | None:
    files = sorted(Path(folder).glob(OVERHEAD_PREFIX + "_*.csv"))
    return files[-1] if files else None


def import_overhead_file(conn: sqlite3.Connection, csv_path: str | Path) -> dict:
    """CSV агрегата (подразделение, месяц, статья, сумма, строк) → stat_overhead_div."""
    csv.field_size_limit(10 ** 7)
    conn.execute("DELETE FROM stat_overhead_div")
    n, total = 0, 0.0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            div = (r.get("Подразделение") or "").strip()
            item = (r.get("СтатьяРасходов") or "").strip()
            month = (r.get("Месяц") or "")[:7]
            if not div or not item or not month:
                continue
            try:
                amount = float(r.get("Сумма") or 0)
                rows_n = int(float(r.get("Строк") or 0))
            except ValueError:
                continue
            if abs(amount) < 0.005:
                continue
            conn.execute(
                "INSERT INTO stat_overhead_div (division, month, item_1c, amount, rows_n, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(division, month, item_1c) "
                "DO UPDATE SET amount = excluded.amount, rows_n = excluded.rows_n, "
                "updated_at = excluded.updated_at", (div, month, item, round(amount, 2), rows_n))
            n += 1
            total += amount
    from . import refsources
    refsources.mark(conn, "stat_overhead_div", n, Path(csv_path).name, "ночной импорт из Extractor")
    return {"rows": n, "amount": total}


def _base_tons(snapshot_path: str | Path) -> dict[str, float]:
    """Направление (bp_nm.d) → проданный тоннаж по снимку «Реализации».

    В bp_nm по каждой серии — тоннаж по направлениям (базам); складываем
    по всем сериям. Имена баз («База Усинск») совпадают с подразделениями 1С.
    """
    import json
    with open(snapshot_path, encoding="utf-8") as fh:
        d = json.load(fh)
    out: dict[str, float] = {}
    for v in (d.get("bp_nm") or {}).values():
        for k, t in (((v.get("т") or {}).get("d")) or {}).items():
            out[str(k).strip()] = out.get(str(k).strip(), 0.0) + float(t or 0)
    return out


def base_overhead_rates(conn: sqlite3.Connection, snapshot_path: str | Path | None,
                        months: int = 12) -> list[dict]:
    """Ставка распределяемых руб/т по подразделениям: сумма статей без
    серии за последние `months` закрытых месяцев / тоннаж через подразделение
    по отвесным за то же окно (stat_division_tons). snapshot_path оставлен
    для совместимости вызова, не используется."""
    try:
        # Последний ЗАКРЫТЫЙ месяц: в регистре есть расходы будущих периодов
        # (до 2027), поэтому берём последний месяц, где сумма не меньше
        # половины медианы предыдущих двенадцати.
        months_all = conn.execute(
            "SELECT month, SUM(amount) AS a FROM stat_overhead_div GROUP BY month ORDER BY month"
        ).fetchall()
    except sqlite3.Error:
        return []
    if not months_all:
        return []
    from statistics import median as _med
    last = None
    for i in range(len(months_all) - 1, -1, -1):
        prev = [float(r["a"]) for r in months_all[max(0, i - 12):i]]
        if prev and float(months_all[i]["a"]) >= 0.5 * _med(prev):
            last = months_all[i]["month"]
            break
    if last is None:
        last = months_all[-1]["month"]
    y, m = int(last[:4]), int(last[5:7])
    m0 = m - months + 1
    y0 = y + (m0 - 1) // 12
    m0 = (m0 - 1) % 12 + 1
    first = f"{y0:04d}-{m0:02d}"
    rows = conn.execute(
        "SELECT division, item_1c, SUM(amount) AS amount, COUNT(DISTINCT month) AS mn "
        "FROM stat_overhead_div WHERE month >= ? AND month <= ? GROUP BY division, item_1c",
        (first, last)).fetchall()
    # Знаменатель — тоннаж через подразделение по отвесным за то же окно
    # (имена подразделений совпадают с регистром затрат).
    tons = {r["division"]: (float(r["t"]), int(r["mn"])) for r in conn.execute(
        "SELECT division, SUM(tons) AS t, COUNT(DISTINCT month) AS mn FROM stat_division_tons "
        "WHERE month >= ? AND month <= ? GROUP BY division", (first, last))}
    by_div: dict[str, dict] = {}
    for r in rows:
        if any(x.lower() in r["item_1c"].lower() for x in OVERHEAD_EXCLUDE):
            continue
        d = by_div.setdefault(r["division"], {"division": r["division"], "amount": 0.0,
                                               "items": [], "months": 0})
        d["amount"] += float(r["amount"])
        d["months"] = max(d["months"], int(r["mn"]))
        d["items"].append((r["item_1c"], float(r["amount"])))
    out = []
    for d in by_div.values():
        t_all = tons.get(d["division"])
        # Подразделение без устойчивого тоннажа (отдел продаж, администрация)
        # — не площадка: ставка руб/т для него бессмысленна.
        if not t_all or t_all[0] <= 0 or t_all[1] < max(3, months // 3) or t_all[0] < 100:
            continue
        t = t_all[0]
        d["tons"] = round(t, 1)
        d["tons_months"] = t_all[1]
        d["rate_per_t"] = round(d["amount"] / t, 2) if t else None
        d["items"] = sorted(d["items"], key=lambda x: -x[1])[:8]
        d["period"] = (first, last)
        out.append(d)
    return sorted(out, key=lambda d: -d["amount"])


def _snapshot_months(snapshot_path) -> int:
    import json
    try:
        with open(snapshot_path, encoding="utf-8") as fh:
            meta = json.load(fh).get("meta") or {}
        a, b = meta.get("period_min", "")[:7], meta.get("period_max", "")[:7]
        if len(a) == 7 and len(b) == 7:
            return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:]) - int(a[5:]) + 1
    except (OSError, ValueError):
        pass
    return 0


# ── Площадки: аналитическая база из «Цеха и базы» 1С ────────────────

def site_of_division(conn: sqlite3.Connection) -> dict[str, str]:
    """подразделение 1С → площадка (analytic_base из ref_prod_units)."""
    try:
        return {r["name"]: r["analytic_base"] for r in conn.execute(
            "SELECT name, analytic_base FROM ref_prod_units "
            "WHERE analytic_base IS NOT NULL AND analytic_base <> ''")}
    except sqlite3.Error:
        return {}


def sites(conn: sqlite3.Connection) -> list[str]:
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT analytic_base FROM ref_prod_units "
            "WHERE analytic_base IS NOT NULL AND analytic_base <> '' ORDER BY 1")]
    except sqlite3.Error:
        return []


def site_overhead_rates(conn: sqlite3.Connection, months: int = 12) -> list[dict]:
    """Ставка распределяемых руб/т по ПЛОЩАДКАМ: подразделения одной
    аналитической базы (база, цех, транспортный отдел, службы) складываются;
    знаменатель — тоннаж по отвесным тех же подразделений за то же окно.
    Так «Усинск (База + Цех)» = База Усинск + Усинск + транспортные отделы
    + бухгалтерия + служба главного инженера площадки."""
    per_div = base_overhead_rates(conn, None, months)      # окно и исключения оттуда
    if not per_div:
        return []
    first, last = per_div[0]["period"]
    smap = site_of_division(conn)
    agg: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT division, item_1c, SUM(amount) AS amount FROM stat_overhead_div "
            "WHERE month >= ? AND month <= ? GROUP BY division, item_1c", (first, last)):
        if any(x.lower() in r["item_1c"].lower() for x in OVERHEAD_EXCLUDE):
            continue
        site = smap.get(r["division"])
        if not site:
            continue
        d = agg.setdefault(site, {"site": site, "amount": 0.0, "tons": 0.0, "divisions": set(),
                                  "items": defaultdict(float), "period": (first, last),
                                  "months": months})
        d["amount"] += float(r["amount"])
        d["divisions"].add(r["division"])
        d["items"][r["item_1c"]] += float(r["amount"])
    for r in conn.execute(
            "SELECT division, SUM(tons) AS t FROM stat_division_tons "
            "WHERE month >= ? AND month <= ? GROUP BY division", (first, last)):
        site = smap.get(r["division"])
        if site in agg:
            agg[site]["tons"] += float(r["t"])
    out = []
    for d in agg.values():
        if d["tons"] < 100:
            continue
        d["rate_per_t"] = round(d["amount"] / d["tons"], 2)
        d["tons"] = round(d["tons"], 1)
        d["divisions"] = sorted(d["divisions"])
        d["items"] = sorted(d["items"].items(), key=lambda kv: -kv[1])   # полный список
        out.append(d)
    return sorted(out, key=lambda d: -d["amount"])


def site_shares(conn: sqlite3.Connection, site: str, months: int = 12) -> dict[str, float]:
    """Доли статей 1С в распределяемых площадки — для разнесения по статьям сервиса."""
    for d in site_overhead_rates(conn, months):
        if d["site"] == site:
            total = sum(a for _, a in d["items"]) or 1.0
            return {name: a / total for name, a in d["items"]}
    return {}
