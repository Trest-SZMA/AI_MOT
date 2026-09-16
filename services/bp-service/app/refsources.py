"""Единый слой справочников: что откуда взялось и когда обновлялось.

Указание директора: в основе расчёта лежат справочники, и они должны
обновляться автоматически или систематической загрузкой. Чтобы это можно
было спросить, а не выяснять, у каждого справочника есть одна карточка:
происхождение, дата последнего обновления, число строк, файл выгрузки,
владелец и ожидаемая периодичность. Просроченный справочник виден сразу —
иначе расчёт молча опирается на прошлогодние цифры.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

MANUAL = "заполняется вручную"
EXPORT = "выгрузка 1С"
DERIVED = "считается сервисом"

# (ключ, название, происхождение, таблица, колонка с датой, ожидаемая
# периодичность в днях). Периодичность — ориентир «когда пора обновлять»,
# правится в справочнике: у номенклатуры и ставок свой ритм, у сезонности —
# раз в полгода.
REGISTRY = [
    ("ref_nomenclature_1c", "Номенклатура 1С", EXPORT, "ref_nomenclature_1c", None, 30),
    ("ref_deals", "Сделки Битрикса: доля лота", EXPORT, "ref_deals", None, 7),
    ("bp_fact_snapshot", "Факт реализации из 1С (снимок «Реализации»)", EXPORT, "bp_fact_snapshot", "generated_at", 3),
    ("stat_type_margin", "Фактическая рентабельность по типам сделок", DERIVED, "stat_type_margin", "generated_at", 3),
    ("stat_fact_costs", "Факт затрат по сделкам (регистр 1С)", EXPORT, "stat_fact_costs", "period_max", 3),
    ("ref_cost_item_map", "Соответствие статей 1С статьям сервиса", MANUAL, "ref_cost_item_map", None, 180),
    ("norm_fact", "Калибровка нормативов фактом", DERIVED, "norm_fact", None, 3),
    ("stat_overhead_div", "Распределяемые расходы по подразделениям (регистр 1С)", EXPORT, "stat_overhead_div", "month", 3),
    ("stat_division_tons", "Тоннаж через подразделения (отвесная)", EXPORT, "stat_division_tons", "month", 3),
    ("stat_book_plan", "Архив книг экономистов: что закладывали", EXPORT, "stat_book_plan", None, 10),
    ("stat_outcome_train", "Обучающая выборка: закрытые сделки с фактом", DERIVED, "stat_outcome_train", None, 3),
    ("stat_deal_audit", "Сверка книг с фактом по сделкам", DERIVED, "stat_deal_audit", "updated_at", 3),
    ("ref_nomen_groups", "Группы аналитического учёта", EXPORT, "ref_nomen_groups", None, 90),
    ("ref_counterparties", "Контрагенты", EXPORT, "ref_counterparties", None, 30),
    ("ref_warehouses", "Склады", EXPORT, "ref_warehouses", None, 90),
    ("ref_divisions", "Подразделения", EXPORT, "ref_divisions", None, 90),
    ("ref_prod_units", "Цеха и базы (площадки)", EXPORT, "ref_prod_units", None, 30),
    ("ref_work_types", "Виды работ и процессы", EXPORT, "ref_work_types", None, 90),
    ("ref_expense_items_1c", "Статьи расходов 1С", EXPORT, "ref_expense_items_1c", None, 90),
    ("ref_series", "Серии номенклатуры", EXPORT, "ref_series", None, 30),
    ("ref_vehicles", "Техника (парк ТС)", EXPORT, "ref_vehicles", "updated_at", 30),
    ("transport_rates", "Транспортные ставки (приказ)", EXPORT, "transport_rates",
     "loaded_at", 30),
    ("stat_purchase_price", "Цены закупки, факт", EXPORT, "stat_purchase_price",
     "updated_at", 30),
    ("stat_sale_price", "Цены продажи, факт", EXPORT, "stat_sale_price", "updated_at", 30),
    ("stat_contamination", "Засор по местам погрузки, факт", EXPORT,
     "stat_contamination", "updated_at", 30),
    ("stat_transport", "Тарифы перевозки, факт", EXPORT, "stat_transport",
     "updated_at", 30),
    ("stat_processing", "Ставки переработки, факт", EXPORT, "stat_processing",
     "updated_at", 30),
    ("stat_overheads", "Распределяемые расходы, факт", EXPORT, "stat_overheads",
     "updated_at", 30),
    ("stat_vehicle_load", "Загрузка рейсов, факт", DERIVED, "stat_vehicle_load",
     "updated_at", 30),
    ("stat_cost_matrix", "Матрица затрат по типам сделок", DERIVED,
     "stat_cost_matrix", "updated_at", 30),
    ("cost_norms", "Нормативы модели затрат", MANUAL, "cost_norms", None, 90),
    ("ref_bp_types", "Типы бизнес-планов", MANUAL, "ref_bp_types", None, 180),
    ("ref_cargo_categories", "Категории груза и плотности", MANUAL,
     "ref_cargo_categories", None, 180),
    ("loading_tariffs", "Тарифы погрузки по местам", MANUAL, "loading_tariffs", None, 180),
    ("norm_nomen_factors", "Коэффициенты по номенклатуре", MANUAL,
     "norm_nomen_factors", None, 180),
    ("seasonal_access", "Сезонность мест вывоза", MANUAL, "seasonal_access", None, 180),
]

BY_KEY = {r[0]: r for r in REGISTRY}


def mark(conn: sqlite3.Connection, key: str, rows: int | None = None,
         source_file: str | None = None, author: str | None = None) -> None:
    """Отметить факт обновления справочника."""
    entry = BY_KEY.get(key)
    title = entry[1] if entry else key
    origin = entry[2] if entry else None
    conn.execute(
        "INSERT INTO ref_sources (key, title, origin, source_file, rows, "
        "updated_at, author) VALUES (?, ?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(key) DO UPDATE SET title = excluded.title, "
        "origin = COALESCE(excluded.origin, ref_sources.origin), "
        "source_file = COALESCE(excluded.source_file, ref_sources.source_file), "
        "rows = COALESCE(excluded.rows, ref_sources.rows), "
        "updated_at = excluded.updated_at, author = excluded.author",
        (key, title, origin, source_file, rows, author))


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    except sqlite3.Error:
        return None


def _own_date(conn: sqlite3.Connection, table: str, column: str | None) -> str | None:
    """Дата из самой таблицы: у части справочников она есть построчно."""
    if not column:
        return None
    try:
        return conn.execute(f"SELECT MAX({column}) AS d FROM {table}").fetchone()["d"]
    except sqlite3.Error:
        return None


def _days_since(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        return (date.today() - datetime.fromisoformat(stamp[:19]).date()).days
    except ValueError:
        return None


def state(conn: sqlite3.Connection) -> list[dict]:
    """Состояние всех справочников: строк, когда обновлён, не просрочен ли."""
    try:
        saved = {r["key"]: dict(r) for r in
                 conn.execute("SELECT * FROM ref_sources")}
    except sqlite3.Error:
        saved = {}
    # Нормативы модели помечают дату обновления настройкой, а не таблицей.
    norms_at = (conn.execute(
        "SELECT value FROM settings WHERE key = 'norms_updated_at'").fetchone()
        or {"value": None})["value"]

    out: list[dict] = []
    for key, title, origin, table, column, period in REGISTRY:
        row = saved.get(key, {})
        updated = row.get("updated_at") or _own_date(conn, table, column)
        if key == "cost_norms" and not updated:
            updated = norms_at
        period_days = row.get("period_days") or period
        days = _days_since(updated)
        out.append({
            "key": key, "title": title,
            "origin": row.get("origin") or origin,
            "rows": _count(conn, table),
            "updated_at": updated,
            "days": days,
            "period_days": period_days,
            "stale": bool(days is not None and period_days and days > period_days),
            "unknown": updated is None,
            "source_file": row.get("source_file"),
            "author": row.get("author"),
            "owner": row.get("owner"),
            "comment": row.get("comment"),
        })
    return out


def summary(conn: sqlite3.Connection) -> dict:
    rows = state(conn)
    return {"total": len(rows),
            "stale": sum(1 for r in rows if r["stale"]),
            "unknown": sum(1 for r in rows if r["unknown"]),
            "no_owner": sum(1 for r in rows if not r["owner"])}


def set_meta(conn: sqlite3.Connection, key: str, owner: str | None,
             period_days: float | None, comment: str | None) -> None:
    """Владелец справочника и ожидаемая периодичность обновления."""
    entry = BY_KEY.get(key)
    if entry is None:
        return
    conn.execute(
        "INSERT INTO ref_sources (key, title, origin, owner, period_days, comment) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "owner = excluded.owner, period_days = excluded.period_days, "
        "comment = excluded.comment",
        (key, entry[1], entry[2], owner or None,
         int(period_days) if period_days else None, comment or None))
