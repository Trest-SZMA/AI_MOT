"""Тип бизнес-плана: труба, чёрный лом, кабель, цветной лом, ДХНО, смешанный.

Тип определяется по составу лота (доля тоннажа по типам закупки позиций) и
задаёт логику расчёта: какие статьи затрат характерны, какие процессы
переработки ожидаются и чьё участие обязательно. Кабельный лот считается
принципиально иначе трубного — на сделке 1570 это стоило пересчёта цены
позиции в стоимость выхода металлов.

Справочник типов редактируется на странице «Справочники»; определение по
составу — здесь, чтобы правило было в одном месте.
"""
from __future__ import annotations

import sqlite3

from .db import get_setting

# Порог доминирования по умолчанию: если один тип занимает столько процентов
# тоннажа лота, сделка считается этого типа; иначе — смешанной.
DEFAULT_THRESHOLD_PCT = 80.0

MIXED = "mixed"

# (код, название, типы позиций, описание, статьи затрат, процессы, роли).
# Списки — через «; », редактируются в справочнике.
DEFAULT_TYPES = [
    ("pipe", "Труба и штанга", "труба",
     "Трубная продукция и штанги б/у: продаётся трубой либо ломом после резки.",
     "Погрузочно-разгрузочные расходы; Транспортные расходы на отгрузку; "
     "Транспортные расходы на перемещение; Заправка газом и кислородом",
     "Резка негабарита; Сортировка по размерам",
     "Экономист; Производство и вывоз; Планирование"),
    ("ferrous", "Чёрный лом", "лом",
     "Лом чёрных металлов по категориям (5А, 12А, 13А): к объёму продажи "
     "применяется засор.",
     "Погрузочно-разгрузочные расходы; Транспортные расходы на отгрузку; "
     "Заправка газом и кислородом",
     "Сортировка; Резка негабарита; Пакетирование прессом",
     "Экономист; Производство и вывоз"),
    ("cable", "Кабель", "кабель",
     "Кабельный лом: везётся на базу разделки, продаётся выход — медь, "
     "свинец, алюминий. Цена позиции без разделки смысла не имеет.",
     "Транспортные расходы на перемещение; Прочие производственные расходы; "
     "Транспортные расходы на отгрузку; Погрузочно-разгрузочные расходы",
     "Разделка кабеля; Выход меди; Выход свинца; Выход алюминия",
     "Экономист; Производство и вывоз; Планирование"),
    ("nonferrous", "Цветной лом", "цветмет",
     "Медь, латунь, алюминий, нержавеющая сталь: дорогой груз, обычно со "
     "страхованием и отдельной логистикой.",
     "Транспортные расходы на отгрузку; Погрузочно-разгрузочные расходы; "
     "Прочие производственные расходы",
     "Сортировка по маркам; Пакетирование",
     "Экономист; Планирование"),
    ("dhno", "ДХНО и оборудование", "ДХНО",
     "Длительно хранящееся неиспользуемое оборудование: разбирается на "
     "составляющие, часть уходит ломом, часть — товаром.",
     "Погрузочно-разгрузочные расходы; Транспортные расходы на отгрузку; "
     "Прочие производственные расходы",
     "Разборка; Дефектовка; Сортировка выхода",
     "Экономист; Производство и вывоз; Планирование"),
    (MIXED, "Смешанный", "",
     "Ни один тип не набирает порог доминирования: затраты и процессы "
     "считаются по каждой части лота отдельно.",
     "", "", "Экономист; Производство и вывоз; Планирование"),
]


def seed(conn: sqlite3.Connection) -> None:
    """Наполнение справочника типовыми значениями (идемпотентно)."""
    for sort, (code, name, item_types, descr, costs, procs, roles) in enumerate(
            DEFAULT_TYPES, start=1):
        conn.execute(
            "INSERT INTO ref_bp_types (code, name, sort, item_types, description, "
            "cost_items, processes, required_roles) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(code) DO NOTHING",
            (code, name, sort * 10, item_types, descr, costs, procs, roles))


def all_types(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ref_bp_types ORDER BY sort, name").fetchall()


def by_code(conn: sqlite3.Connection, code: str | None) -> sqlite3.Row | None:
    if not code:
        return None
    return conn.execute("SELECT * FROM ref_bp_types WHERE code = ?", (code,)).fetchone()


def label(conn: sqlite3.Connection, code: str | None) -> str:
    row = by_code(conn, code)
    return row["name"] if row else "не определён"


def _item_type_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Тип закупки позиции → код типа сделки (из справочника, не из кода:
    экономист может завести свой тип и указать, какие позиции к нему относятся)."""
    out: dict[str, str] = {}
    for row in all_types(conn):
        for part in (row["item_types"] or "").split(";"):
            key = part.strip().lower()
            if key:
                out.setdefault(key, row["code"])
    return out


def threshold(conn: sqlite3.Connection) -> float:
    return get_setting(conn, "bp_type_threshold_pct", DEFAULT_THRESHOLD_PCT)


def detect(conn: sqlite3.Connection, items: list) -> dict:
    """Тип сделки по составу лота.

    Считаем доли ТОННАЖА по типам закупки позиций: 1349 мелких трубных строк
    и одна крупная ломовая — всё-таки трубная сделка. Если тоннаж нулевой
    (позиции без объёма), падаем на число позиций, иначе тип не определить.
    """
    tons: dict[str, float] = {}
    count: dict[str, int] = {}
    for it in items:
        try:
            ptype = (it["purchase_type"] or "").strip().lower()
            vol = float(it["volume_t"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not ptype:
            continue
        tons[ptype] = tons.get(ptype, 0.0) + max(vol, 0.0)
        count[ptype] = count.get(ptype, 0) + 1
    if not count:
        return {"code": None, "shares": [], "dominant_pct": 0.0,
                "basis": "нет позиций",
                "reason": "Позиции не загружены — тип определить не по чему."}

    by_tons = sum(tons.values()) > 0
    weights = tons if by_tons else {k: float(v) for k, v in count.items()}
    total = sum(weights.values())
    basis = "тоннаж" if by_tons else "число позиций"

    type_map = _item_type_map(conn)
    shares = sorted(
        ({"item_type": t, "tons": tons.get(t, 0.0), "count": count.get(t, 0),
          "pct": weights[t] / total * 100.0, "code": type_map.get(t)}
         for t in weights),
        key=lambda s: -s["pct"])
    top = shares[0]
    limit = threshold(conn)

    if top["pct"] + 1e-9 >= limit and top["code"]:
        code = top["code"]
        reason = (f"{label(conn, code)}: {top['pct']:.0f}% лота по показателю "
                  f"«{basis}» — не меньше порога {limit:.0f}%.")
    elif top["code"] is None:
        code = MIXED
        reason = (f"Тип позиций «{top['item_type']}» не привязан ни к одному типу "
                  "сделки в справочнике — сделка помечена смешанной.")
    else:
        code = MIXED
        parts = ", ".join(f"{s['item_type']} {s['pct']:.0f}%" for s in shares[:3])
        reason = (f"Ни один тип не набрал порог {limit:.0f}% по показателю "
                  f"«{basis}»: {parts}.")
    return {"code": code, "shares": shares, "dominant_pct": top["pct"],
            "basis": basis, "reason": reason}


def apply_auto(conn: sqlite3.Connection, bp_id: int, items: list) -> dict:
    """Записать определённый тип, если человек не задал его руками.

    Ручной выбор не перетирается: экономист мог назвать сделку кабельной,
    хотя по тоннажу она смешанная.
    """
    result = detect(conn, items)
    row = conn.execute("SELECT bp_type, bp_type_source FROM business_plans "
                       "WHERE id = ?", (bp_id,)).fetchone()
    if row is None:
        return {**result, "written": False}
    if row["bp_type_source"] == "manual":
        return {**result, "written": False,
                "kept": row["bp_type"]}
    conn.execute("UPDATE business_plans SET bp_type = ?, bp_type_source = 'auto' "
                 "WHERE id = ?", (result["code"], bp_id))
    return {**result, "written": True}


def set_manual(conn: sqlite3.Connection, bp_id: int, code: str | None) -> None:
    """Ручной выбор типа; пустой код возвращает сделку к автоопределению."""
    if code:
        conn.execute("UPDATE business_plans SET bp_type = ?, "
                     "bp_type_source = 'manual' WHERE id = ?", (code, bp_id))
    else:
        conn.execute("UPDATE business_plans SET bp_type = NULL, "
                     "bp_type_source = NULL WHERE id = ?", (bp_id,))
