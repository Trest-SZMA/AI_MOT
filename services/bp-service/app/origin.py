"""Происхождение суммы затрат: откуда цифра и кто её поставил.

Указание директора: затраты не должны назначаться на глаз. Технически это
значит, что у каждой суммы есть источник — норматив модели, факт 1С или
книга экономистов, — а ручной ввод помечается как отклонение и требует
обоснования. Сумма при этом НЕ меняется сама: норматив подставляется
только явным действием человека, иначе один пересчёт задним числом
переписал бы согласованные расчёты.
"""
from __future__ import annotations

import sqlite3

MODEL = "модель"
MANUAL = "вручную"
BOOK = "книга"

# Насколько ручная сумма может отличаться от накопленного ориентира, прежде
# чем сервис попросит объяснение. 30% — тот же порог, по которому в карточке
# подсвечивается расхождение с матрицей затрат.
DEVIATION_PCT = 30.0


def set_origin(conn: sqlite3.Connection, cost_id: int, variant: str,
               source: str, source_ref: str | None = None,
               amount: float | None = None, note: str | None = None,
               author: str | None = None) -> None:
    """Записать источник суммы (одна строка на статью и вариант)."""
    conn.execute(
        "INSERT INTO bp_cost_origin (cost_id, variant, source, source_ref, "
        "amount, note, author, set_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(cost_id, variant) DO UPDATE SET source = excluded.source, "
        "source_ref = excluded.source_ref, amount = excluded.amount, "
        "note = COALESCE(excluded.note, bp_cost_origin.note), "
        "author = excluded.author, set_at = datetime('now')",
        (cost_id, variant, source, source_ref, amount, note, author))


def set_note(conn: sqlite3.Connection, cost_id: int, variant: str,
             note: str | None) -> None:
    """Обоснование отклонения, не трогая сам источник."""
    conn.execute(
        "UPDATE bp_cost_origin SET note = ? WHERE cost_id = ? AND variant = ?",
        (note or None, cost_id, variant))


def for_bp(conn: sqlite3.Connection, bp_id: int, variant: str) -> dict[int, dict]:
    """Источники сумм сделки: {cost_id: строка}."""
    try:
        rows = conn.execute(
            "SELECT o.* FROM bp_cost_origin o JOIN bp_costs c ON c.id = o.cost_id "
            "WHERE c.bp_id = ? AND o.variant = ?", (bp_id, variant)).fetchall()
    except sqlite3.Error:
        return {}
    return {r["cost_id"]: dict(r) for r in rows}


def deviations(conn: sqlite3.Connection, bp_id: int, variant: str,
               costs: list, sale_volume: float, hints: dict) -> list[dict]:
    """Ручные суммы, заметно расходящиеся с накопленным ориентиром.

    `hints` — ориентиры матрицы затрат по ключу (секция, статья). Отклонение
    без объяснения не мешает сохранить расчёт, но остаётся видимым: закрыть
    его можно только словами, а не молча.
    """
    origins = for_bp(conn, bp_id, variant)
    column = "amount_luk" if variant == "luk" else "amount"
    out: list[dict] = []
    for c in costs:
        amount = c[column] if column in c.keys() else None
        if amount is None:
            amount = c["amount"]
        amount = float(amount or 0)
        if amount <= 0 or not sale_volume:
            continue
        hint = hints.get((c["section"], c["item"]))
        if not hint or not hint["rub_per_t"]:
            continue
        per_t = amount / sale_volume
        diff = (per_t - hint["rub_per_t"]) / hint["rub_per_t"] * 100.0
        if abs(diff) < DEVIATION_PCT:
            continue
        o = origins.get(c["id"], {})
        out.append({
            "cost_id": c["id"], "section": c["section"], "item": c["item"],
            "amount": amount, "per_t": per_t, "hint": hint["rub_per_t"],
            "hint_source": hint["source"], "samples": hint["samples"],
            "diff_pct": diff, "source": o.get("source"), "note": o.get("note"),
            "author": o.get("author"), "set_at": o.get("set_at"),
            "explained": bool(o.get("note")),
        })
    out.sort(key=lambda d: -abs(d["diff_pct"]))
    return out
