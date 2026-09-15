"""Импорт CSV-выгрузок 1С (папка с файлами Закупки_*, Отвесная_* и т.д.)
в базу сервиса: справочники + агрегированная статистика для автоподсказок.

Запуск:  python import_1c_csv.py [путь_к_папке]   (по умолчанию /Users/macpavel/FASTBP/1c)

Справочники обновляются по GUID (идемпотентно), stat_*-таблицы
пересоздаются целиком. Тяжёлые файлы (СебТоваровДляСерий,
СебестоимостьТоваровОбороты, _Движение_ТМЦ, _Объемы, _Распределение)
в этой версии не импортируются. Все файлы читаются потоково.
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from app.db import connect, init_db
from app.matcher import normalize

DEFAULT_DIR = "/Users/macpavel/FASTBP/1c"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

# Внутри полей встречаются очень длинные перечисления (ТипАналитики).
csv.field_size_limit(10 ** 7)

# Мусорные ветки складов/мест погрузки.
_UNUSED_RE = re.compile(r"не\s+использ", re.IGNORECASE)
# Счёт учёта в имени статьи расходов: «... (26)».
_ACCOUNT_RE = re.compile(r"\((23|25|26|91)\)")
# Дата в имени регистратора: «... от 03.04.2023 23:59:59».
_REG_DATE_RE = re.compile(r"от (\d{2})\.(\d{2})\.(\d{4})")


# ── Утилиты разбора ─────────────────────────────────────────────────

def _s(v: str | None) -> str | None:
    """Строка: пустая → None."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def _f(v: str | None) -> float:
    """Число с точкой; мусор/пусто → 0.0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _guid(v: str | None) -> str | None:
    """GUID: нормализация регистра, пустой GUID → None."""
    v = _s(v)
    if not v:
        return None
    v = v.upper()
    return None if v == EMPTY_GUID else v


def _date(v: str | None) -> str | None:
    """Дата 1С '2026-04-30 00:00:00.000' → '2026-04-30'; пустая → None."""
    v = _s(v)
    if not v or v.startswith("0001-01-01"):
        return None
    return v[:10]


def _bool(v: str | None) -> int:
    v = (v or "").strip().lower()
    return 1 if v in ("1", "true", "да", "истина") else 0


def _unused(v: str | None) -> bool:
    return bool(v) and bool(_UNUSED_RE.search(v))


def find_file(folder: Path, prefix: str) -> Path | None:
    """Файл по префиксу имени: самый свежий датированный
    ('Закупки_*.csv' — сортировка по имени), иначе без даты."""
    dated = sorted(folder.glob(prefix + "_*.csv"))
    if dated:
        return dated[-1]
    plain = folder / (prefix + ".csv")
    return plain if plain.exists() else None


def iter_rows(path: Path):
    """Потоковое чтение CSV: словари по заголовку, без сторно-строк.

    Кодировка UTF-8, разделитель запятая, кавычки с удвоением,
    переводы строк внутри полей — поэтому только csv.reader.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        i_storno = header.index("Сторно") if "Сторно" in header else -1
        i_storn = header.index("Сторнирование") if "Сторнирование" in header else -1
        i_act = header.index("Активность") if "Активность" in header else -1
        for row in reader:
            if len(row) < len(header):
                continue
            if i_storno >= 0 and row[i_storno].strip() == "1":
                continue
            if i_storn >= 0 and row[i_storn].strip() == "Сторно":
                continue
            if i_act >= 0 and row[i_act].strip() == "0":
                continue
            yield dict(zip(header, row))


def _normalize_guid_case(conn, table: str) -> None:
    """Старые импорты (xlsx) хранили GUID в исходном регистре — приводим
    к верхнему, предварительно удаляя дубли (остаётся самая свежая запись)."""
    conn.execute(
        f"DELETE FROM {table} WHERE guid IS NOT NULL AND id NOT IN "
        f"(SELECT max(id) FROM {table} WHERE guid IS NOT NULL "
        f"GROUP BY upper(guid))")
    conn.execute(f"UPDATE {table} SET guid = upper(guid) "
                 f"WHERE guid IS NOT NULL AND guid <> upper(guid)")


# ── Справочники (upsert по GUID) ────────────────────────────────────

def import_series(conn, path: Path) -> int:
    n = 0
    for r in iter_rows(path):
        guid = _guid(r["СерияГуид"])
        name = _s(r["Серия"])
        if not guid or not name:
            continue
        conn.execute(
            "INSERT INTO ref_series (guid, name, project, project_guid, "
            "counterparty, counterparty_guid, removal_date, is_additional, "
            "is_paid, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(guid) DO UPDATE SET name = excluded.name, "
            "project = excluded.project, project_guid = excluded.project_guid, "
            "counterparty = excluded.counterparty, "
            "counterparty_guid = excluded.counterparty_guid, "
            "removal_date = excluded.removal_date, "
            "is_additional = excluded.is_additional, is_paid = excluded.is_paid, "
            "updated_at = datetime('now')",
            (guid, name, _s(r["Проект"]), _guid(r["ПроектГуид"]),
             _s(r["Контрагент"]), _guid(r["КонтрагентГуид"]),
             _date(r["ДатаВывоза"]), _bool(r["Дополнительная"]),
             _bool(r["Плтаная"])))
        n += 1
    return n


def import_nomenclature(conn, path: Path) -> int:
    n = 0
    _normalize_guid_case(conn, "ref_nomenclature_1c")
    for r in iter_rows(path):
        guid = _guid(r["НоменклатураГуид"])
        name = _s(r["Номенклатура"])
        if not guid or not name:
            continue
        conn.execute(
            "INSERT INTO ref_nomenclature_1c (name, guid, unit, gost, "
            "cargo_group, norm) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET name = excluded.name, "
            "unit = excluded.unit, gost = excluded.gost, "
            "cargo_group = excluded.cargo_group, norm = excluded.norm",
            (name, guid,
             _s(r["ЕдиницаДляОтчетов"]) or _s(r["ЕдиницаИзмерения"]),
             _s(r["ГОСТ"]), _s(r["НоменклатурнаяГруппаГрузов"]),
             normalize(name)))
        conn.execute(
            "INSERT INTO ref_nomen_1c_ext (guid, unit, unit_coef, cargo_group, "
            "cargo_group_guid, report_name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(guid) DO UPDATE SET unit = excluded.unit, "
            "unit_coef = excluded.unit_coef, cargo_group = excluded.cargo_group, "
            "cargo_group_guid = excluded.cargo_group_guid, "
            "report_name = excluded.report_name, updated_at = datetime('now')",
            (guid, _s(r["ЕдиницаИзмерения"]), _f(r["КоэффициентПересчета"]),
             _s(r["НоменклатурнаяГруппаГрузов"]),
             _guid(r["НоменклатурнаяГруппаГрузовГуид"]),
             _s(r["НоменклатураОтчета"])))
        n += 1
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_n1c_norm "
                 "ON ref_nomenclature_1c(norm)")
    return n


def import_warehouses(conn, path: Path) -> int:
    n = 0
    _normalize_guid_case(conn, "ref_warehouses")
    for r in iter_rows(path):
        guid = _guid(r["СкладГуид"])
        name = _s(r["Склад"])
        if not guid or not name:
            continue
        if _unused(r["ВысшийРодитель"]) or _unused(name):
            continue                      # мусорные ветки («Не используем»...)
        conn.execute(
            "INSERT INTO ref_warehouses (name, guid, parent_name, parent_guid, "
            "top_parent, is_group) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET name = excluded.name, "
            "parent_name = excluded.parent_name, "
            "parent_guid = excluded.parent_guid, "
            "top_parent = excluded.top_parent, is_group = excluded.is_group",
            (name, guid, _s(r["Родитель"]), _guid(r["РодительГуид"]),
             _s(r["ВысшийРодитель"]), _bool(r["ЭтоГруппа"])))
        n += 1
    return n


def import_expense_items(conn, path: Path) -> int:
    n = 0
    for r in iter_rows(path):
        guid = _guid(r["СсылкаГуид"])
        name = _s(r["Ссылка"])
        if not guid or not name:
            continue
        # Счёт (23/25/26/91) обычно указан в скобках в новой статье/родителе.
        m = (_ACCOUNT_RE.search(r["НоваяСтатьяРасходов"])
             or _ACCOUNT_RE.search(name)
             or _ACCOUNT_RE.search(r["Родитель"]))
        conn.execute(
            "INSERT INTO ref_expense_items_1c (guid, name, parent, parent_guid, "
            "distribution_var, analytics_type, production_process, "
            "cash_flow_item, account, account_1c, excluded, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(guid) DO UPDATE SET name = excluded.name, "
            "parent = excluded.parent, parent_guid = excluded.parent_guid, "
            "distribution_var = excluded.distribution_var, "
            "analytics_type = excluded.analytics_type, "
            "production_process = excluded.production_process, "
            "cash_flow_item = excluded.cash_flow_item, "
            "account = excluded.account, account_1c = excluded.account_1c, "
            "excluded = excluded.excluded, updated_at = datetime('now')",
            (guid, name, _s(r["Родитель"]), _guid(r["РодительГуид"]),
             _s(r["ВариантРаспределенияРасходовУпр"]), _s(r["ТипАналитики"]),
             _s(r["ПроизводственныйПроцесс"]), _s(r["СтатьяДДС"]),
             m.group(1) if m else None, _s(r["СчетУчета"]),
             _bool(r["ИсключатьИзОтчетов"])))
        n += 1
    return n


def import_prod_divisions(conn, path: Path) -> int:
    n = 0
    for r in iter_rows(path):
        guid = _guid(r["СсылкаГуид"])
        name = _s(r["Наименование"]) or _s(r["Ссылка"])
        if not guid or not name:
            continue
        conn.execute(
            "INSERT INTO ref_prod_divisions (guid, name, code, parent, "
            "functional_division, analytic_division, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(guid) DO UPDATE SET name = excluded.name, "
            "code = excluded.code, parent = excluded.parent, "
            "functional_division = excluded.functional_division, "
            "analytic_division = excluded.analytic_division, "
            "updated_at = datetime('now')",
            (guid, name, _s(r["Код"]), _s(r["Родитель"]),
             _s(r["ФункциональноеПодразделение"]),
             _s(r["АналитическоеПодразделение"])))
        n += 1
    return n


def import_vehicles(conn, path: Path) -> int:
    """ТС_*.csv → ref_vehicles (парк техники с грузоподъёмностью и объёмом)."""
    n = 0
    for r in iter_rows(path):
        name = _s(r.get("ТС"))
        if not name:
            continue
        conn.execute(
            "INSERT INTO ref_vehicles (guid, name, plate, brand, vehicle_type, "
            "ownership, division, capacity_t, volume_m3) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET name = excluded.name, "
            "plate = excluded.plate, brand = excluded.brand, "
            "vehicle_type = excluded.vehicle_type, ownership = excluded.ownership, "
            "division = excluded.division, capacity_t = excluded.capacity_t, "
            "volume_m3 = excluded.volume_m3, updated_at = datetime('now')",
            (_guid(r.get("ТСГуид")), name, _s(r.get("НомерТС")),
             _s(r.get("Марка")), _s(r.get("ТипТС")), _s(r.get("Вид")),
             _s(r.get("ТекущееПодразделение")) or _s(r.get("Подразделение")),
             _f(r.get("Грузоподъемность")),
             _f(r.get("ВместимостьВКубическихМетрах"))))
        n += 1
    return n


def import_vehicle_load(conn, path: Path, since: str = "2025-01-01") -> int:
    """Отвесная_*.csv → stat_vehicle_load: сколько реально грузят по типам.

    Тип техники берём из названия транспортного средства: в отвесной оно
    записано целиком («О936НА159 … КМУ шоссейный»), отдельного поля типа
    нет. Сопоставляем по вхождению типа из справочника парка.
    """
    from app import loading
    types = [t for (t,) in conn.execute(
        "SELECT DISTINCT vehicle_type FROM ref_vehicles "
        "WHERE vehicle_type IS NOT NULL AND capacity_t > 0")]
    types.sort(key=len, reverse=True)      # длинные раньше: «Тягач полноприводный»
    trips: dict[str, list] = defaultdict(list)
    for r in iter_rows(path):
        ts = _s(r.get("ТранспортноеСредство"))
        if not ts:
            continue
        date = _date(r.get("ДатаПогрузки")) or ""
        if date < since:
            continue
        weight = _f(r.get("ВесПоТТН"))
        if weight <= 0:
            continue
        low = ts.lower()
        for t in types:
            if t.lower() in low:
                trips[t].append(weight)
                break
    return loading.build_load_stats(conn, trips, since)


# ── Статистика (DELETE + INSERT агрегатов) ──────────────────────────

def import_weighing_stats(conn, path: Path) -> tuple[int, int]:
    """Отвесная_*.csv → stat_contamination + stat_transport.

    Засор: ПроцентЗасора, при нуле — ПроцентЗасораПСА; валидный диапазон
    (0; 30]% (в данных есть выбросы до 1300 — это проценты, посчитанные
    от нулевой отвесной). Недовоз: (ВесПоТТН − Отвесная)/ВесПоТТН для
    рейсов с реальной отвесной (> 0), |значение| ≤ 30%.
    Тарифы: рейсы с весом и стоимостью > 0; руб/т·км — по рейсам с км > 0
    и тарифом в разумных пределах 0.5–200 руб/т·км (отсечка выбросов:
    фиксированные суммы на 1–2 км дают тысячи руб/т·км).
    """
    cont = defaultdict(lambda: [0, 0.0, 0, 0.0])   # place → [n, sum%, n_short, sum_short%]
    trans = defaultdict(lambda: [0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0])
    # kind → [n_all, t_all, n_cost, t_cost, cost, km, tkm_cost, tkm]
    for r in iter_rows(path):
        place = _s(r["МестоПогрузки"])
        w = _f(r["ВесПоТТН"])
        netw = _f(r["Отвесная"])
        if place and not _unused(place) and not _unused(r["ВысшийРодитель"]):
            pct = _f(r["ПроцентЗасора"]) or _f(r["ПроцентЗасораПСА"])
            a = cont[place]
            if 0 < pct <= 30:
                a[0] += 1
                a[1] += pct
            if w > 0 and netw > 0:
                short = (w - netw) / w * 100.0
                if abs(short) <= 30:
                    a[2] += 1
                    a[3] += short
        kind = _s(r["ВидДоставки"])
        if not kind or _unused(kind):
            continue
        cost = _f(r["Стоимость"])
        km = _f(r["Километраж"])
        if w > 0:
            t = trans[kind]
            t[0] += 1
            t[1] += w
            if cost > 0:
                t[2] += 1
                t[3] += w
                t[4] += cost
                t[5] += km
                if km > 0 and 0.5 <= cost / (w * km) <= 200:
                    t[6] += cost
                    t[7] += w * km
    conn.execute("DELETE FROM stat_contamination")
    n_cont = 0
    for place, (n, s, ns, ss) in sorted(cont.items()):
        if n == 0 and ns == 0:
            continue
        conn.execute(
            "INSERT INTO stat_contamination (loading_place, samples, "
            "avg_contamination_pct, avg_shortage_pct) VALUES (?, ?, ?, ?)",
            (place, n, round(s / n, 2) if n else None,
             round(ss / ns, 2) if ns else None))
        n_cont += 1
    conn.execute("DELETE FROM stat_transport")
    n_tr = 0
    for kind, (n_all, t_all, n_c, t_c, cost, km, tc, tkm) in sorted(trans.items()):
        conn.execute(
            "INSERT INTO stat_transport (delivery_kind, trips_total, samples, "
            "total_t, total_cost, total_km, rub_per_t, rub_per_tkm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, n_all, n_c, round(t_all, 1), round(cost, 2), round(km, 1),
             round(cost / t_c, 2) if t_c else None,
             round(tc / tkm, 2) if tkm else None))
        n_tr += 1
    return n_cont, n_tr


def import_processing_stats(conn, path: Path) -> int:
    """_Производственная_себестоимость__*.csv → stat_processing.

    Ставки считаются как Сумма<статья>/Количество (руб на единицу выпуска),
    трудозатраты — часы на единицу. Единица выпуска зависит от группы
    (лом — тонны; кабель/комплектующие могут быть в кг/м/шт).
    """
    # Единица выпуска берётся из справочника 1С по GUID продукции: в самой
    # выгрузке её нет, а ставка руб/шт и ставка руб/т — разные величины.
    units = {str(g).lower(): u for g, u in conn.execute(
        "SELECT guid, unit FROM ref_nomenclature_1c WHERE guid IS NOT NULL "
        "AND unit IS NOT NULL")}
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    # (work, div, group, unit) → [n, qty, fot, prr, gsm, amort, labor]
    for r in iter_rows(path):
        work = _s(r["ВидРабот"])
        if not work:
            continue
        qty = _f(r["Количество"])
        if qty <= 0:
            continue
        unit = units.get((_guid(r.get("ПродукцияГуид")) or "").lower(), "")
        a = agg[(work, _s(r["Подразделение"]) or "", _s(r["Группа"]) or "", unit)]
        a[0] += 1
        a[1] += qty
        a[2] += _f(r["СуммаФОТ"])
        a[3] += _f(r["СуммаПРР"])
        a[4] += _f(r["СуммаГСМ"])
        a[5] += _f(r["СуммаАмортизации"])
        a[6] += _f(r["Трудозатраты"])
    conn.execute("DELETE FROM stat_processing")
    n = 0
    for (work, div, grp, unit), (cnt, qty, fot, prr, gsm, am, lab) in sorted(agg.items()):
        conn.execute(
            "INSERT INTO stat_processing (work_type, division, cargo_group, "
            "samples, total_qty, rate_fot, rate_prr, rate_gsm, rate_amort, "
            "labor_per_t, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (work, div, grp, cnt, round(qty, 2), round(fot / qty, 2),
             round(prr / qty, 2), round(gsm / qty, 2), round(am / qty, 2),
             round(lab / qty, 4), unit or None))
        n += 1
    return n


def import_purchase_stats(conn, path: Path) -> int:
    """Закупки_*.csv → stat_purchase_price (только позиции в тоннах).

    Возвраты и сторно в выгрузке идут с отрицательными оборотами —
    суммирование со знаком автоматически их вычитает.
    """
    agg = defaultdict(lambda: [0, 0.0, 0.0, None, None])
    # (counterparty, group) → [n, qty, sum, dmin, dmax]
    for r in iter_rows(path):
        if _s(r["ЕдИзм"]) != "т":
            continue
        qty = _f(r["КоличествоОборот"])
        amount = _f(r["СуммаБезНДСОборот"])
        if qty == 0 and amount == 0:
            continue
        cp = _s(r["Контрагент"])
        if not cp:
            continue
        grp = _s(r["АналитикаУчетаНоменклатурыНоменклатураГруппаАналитическогоУчета"]) or ""
        a = agg[(cp, grp)]
        a[0] += 1
        a[1] += qty
        a[2] += amount
        m = _REG_DATE_RE.search(r["Регистратор"] or "")
        if m:
            d = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            a[3] = d if a[3] is None or d < a[3] else a[3]
            a[4] = d if a[4] is None or d > a[4] else a[4]
    conn.execute("DELETE FROM stat_purchase_price")
    n = 0
    for (cp, grp), (cnt, qty, amount, dmin, dmax) in sorted(agg.items()):
        if qty <= 0 or amount <= 0:      # полностью возвращённые партии
            continue
        conn.execute(
            "INSERT INTO stat_purchase_price (counterparty, nomen_group, "
            "samples, total_qty_t, total_sum, rub_per_t, period_min, "
            "period_max) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cp, grp, cnt, round(qty, 3), round(amount, 2),
             round(amount / qty, 2), dmin, dmax))
        n += 1
    return n


def import_sale_stats(conn, path: Path) -> int:
    """_Выручка_на_загрузку__*.csv → stat_sale_price (фактические продажи)
    и stat_sale_price_hist (разрез по месяцам и покупателям).

    Средняя цена по типу груза не годится для расчёта: труба НКТ уходит
    Северскому трубному по 18 610, а трубы больших диаметров — по 32 897
    руб/тн. Поэтому дополнительно сохраняем «группа × дивизион × покупатель
    × месяц»: по этому разрезу строятся ориентир и тренд цены.
    """
    agg = defaultdict(lambda: [0, 0.0, 0.0])   # (group, div) → [n, qty, rev]
    hist = defaultdict(lambda: [0, 0.0, 0.0])  # (group, div, buyer, YYYY-MM)
    for r in iter_rows(path):
        if _s(r["ЕдИзм"]) == "шт":       # штучные позиции — не цена за тонну
            continue
        qty = _f(r["КоличествоПродажиАкт"])
        rev = _f(r["ВыручкаПродажиАкт"])
        if qty <= 0 or rev <= 0:
            continue
        grp = _s(r["ГруппаАналитическогоУчета"])
        if not grp:
            continue
        div = _s(r["Дивизион"]) or ""
        a = agg[(grp, div)]
        a[0] += 1
        a[1] += qty
        a[2] += rev
        # «Период» приходит датой (2026-06-01 …) — до месяца.
        period = (_s(r.get("Период")) or "")[:7]
        if len(period) == 7:
            h = hist[(grp, div, _s(r.get("Покупатель")) or "", period)]
            h[0] += 1
            h[1] += qty
            h[2] += rev
    conn.execute("DELETE FROM stat_sale_price")
    n = 0
    for (grp, div), (cnt, qty, rev) in sorted(agg.items()):
        conn.execute(
            "INSERT INTO stat_sale_price (cargo_group, division, samples, "
            "total_qty_t, total_revenue, rub_per_t) VALUES (?, ?, ?, ?, ?, ?)",
            (grp, div, cnt, round(qty, 3), round(rev, 2), round(rev / qty, 2)))
        n += 1
    conn.execute("DELETE FROM stat_sale_price_hist")
    for (grp, div, buyer, period), (cnt, qty, rev) in sorted(hist.items()):
        conn.execute(
            "INSERT INTO stat_sale_price_hist (cargo_group, division, buyer, "
            "period, samples, total_qty_t, total_revenue, rub_per_t) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (grp, div, buyer, period, cnt, round(qty, 3), round(rev, 2),
             round(rev / qty, 2)))
    return n


# ── Оркестровка ─────────────────────────────────────────────────────

def import_overhead_stats(conn, path: Path) -> int:
    """_Распределение_прочих_расходов__*.csv → stat_overheads.

    Документ переразноски: строка «Сторно» снимает сумму с котловой статьи,
    парная «Начисление» кладёт её на детализированную. Суммируем со знаком —
    получается фактическая сумма по подразделению, месяцу и статье.
    Название статьи и счёт учёта берутся из справочника ref_expense_items_1c
    по СсылкаГуид (проверено: совпадает 406 строк из 407).
    """
    items = {}
    try:
        for r in conn.execute("SELECT guid, name, account "
                              "FROM ref_expense_items_1c").fetchall():
            items[(r["guid"] or "").upper()] = (r["name"], r["account"])
    except sqlite3.Error:
        items = {}

    agg = defaultdict(lambda: [0, 0.0, None])   # (period, div, item) → [n, sum, acc]
    for r in iter_rows(path):
        period = _s(r.get("Период"))[:7]        # YYYY-MM
        if not period or period.startswith("0001"):
            continue
        div = _s(r.get("Подразделение"))
        guid = _s(r.get("СсылкаГуид")).upper()
        name, account = items.get(guid, ("Прочие расходы", None))
        # Конвенция файла: у строк «Сторно» (снятие с котловой статьи
        # «Прочие производственные расходы») сумма положительная, у парных
        # «Начисление» на детализированные статьи — отрицательная, итог по
        # файлу ноль. Инвертируем знак: котёл уходит в минус, конкретные
        # статьи получают положительный расход — так это и есть факт.
        amount = -_f(r.get("Сумма"))
        if amount == 0:
            continue
        a = agg[(period, div, name)]
        a[0] += 1
        a[1] += amount
        a[2] = account
    conn.execute("DELETE FROM stat_overheads")
    n = 0
    for (period, div, name), (cnt, amount, account) in sorted(agg.items()):
        if abs(amount) < 0.005:                 # сторно погасило начисление
            continue
        conn.execute(
            "INSERT INTO stat_overheads (period, division, item, account, "
            "amount, samples) VALUES (?, ?, ?, ?, ?, ?)",
            (period, div, name, account, round(amount, 2), cnt))
        n += 1
    return n


def import_all(folder: str, author: str = "импорт 1С") -> None:
    """author — кто/что обновило справочники (отметка в реестре источников)."""
    folder = Path(folder)
    init_db()
    conn = connect()
    summary: list[tuple[str, int]] = []

    from app import refsources

    def run(prefix: str, label_counts):
        """label_counts: [(таблица, функция-импортёр)] на одном файле."""
        path = find_file(folder, prefix)
        if path is None:
            print(f"ПРЕДУПРЕЖДЕНИЕ: файл {prefix}*.csv не найден — пропуск")
            return
        for label, fn in label_counts:
            res = fn(conn, path)
            if isinstance(res, tuple):
                continue                  # функция сама вернула пары ниже
            summary.append((label, res))
            # Отметка в реестре справочников: из какого файла и когда.
            refsources.mark(conn, label.split()[0], res, path.name, author)
        conn.commit()

    run("СерииНоменклатуры", [("ref_series", import_series)])
    run("Номенклатура", [("ref_nomenclature_1c (+ext)", import_nomenclature)])
    run("Склады", [("ref_warehouses", import_warehouses)])
    run("СтатьиРасходов", [("ref_expense_items_1c", import_expense_items)])
    run("_Производственные_подразделения_",
        [("ref_prod_divisions", import_prod_divisions)])

    path = find_file(folder, "Отвесная")
    if path is None:
        print("ПРЕДУПРЕЖДЕНИЕ: файл Отвесная_*.csv не найден — пропуск")
    else:
        n_cont, n_tr = import_weighing_stats(conn, path)
        summary += [("stat_contamination", n_cont), ("stat_transport", n_tr)]
        for key, cnt in (("stat_contamination", n_cont), ("stat_transport", n_tr)):
            refsources.mark(conn, key, cnt, path.name, author)
        conn.commit()

    run("_Производственная_себестоимость_",
        [("stat_processing", import_processing_stats)])
    run("Закупки", [("stat_purchase_price", import_purchase_stats)])
    run("_Выручка_на_загрузку_", [("stat_sale_price", import_sale_stats)])
    run("_Распределение_прочих_расходов_",
        [("stat_overheads", import_overhead_stats)])
    run("ТС", [("ref_vehicles", import_vehicles)])

    # Фактическая загрузка рейсов — после парка техники: тип рейса
    # определяется по справочнику машин.
    path = find_file(folder, "Отвесная")
    if path is not None:
        n_load = import_vehicle_load(conn, path)
        summary.append(("stat_vehicle_load", n_load))
        refsources.mark(conn, "stat_vehicle_load", n_load, path.name, author)
        conn.commit()

    conn.commit()
    conn.close()
    print("\nИтог импорта (таблица → строк):")
    for label, cnt in summary:
        print(f"  {label:32s} {cnt}")
    print("Импорт завершён.")


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    if not Path(p).is_dir():
        sys.exit(f"Папка не найдена: {p}")
    import_all(p)
