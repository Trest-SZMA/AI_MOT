"""Роли пунктов отгрузки РИТЭК-юга: что база, что цех и куда цех свозит.

Решение пользователя 18.08.2026: базами считаются трубные базы/площадки
ПРОДАВЦА (там бригада режет и грузит), одиночные мелкие места относятся к
ближайшей базе. Скрипт идемпотентен и работает по подстрокам нормализованного
имени, поэтому одинаково применяется к локальной и серверной базе — id точек
в них разные.

Заодно чинит два кривых автосопоставления складов 1С: «Котово, промзона,
трубная база» и Жирновские ЦДНГ автоматика привязала к складу «Урай,
промзона (ЭПУ)» по слову «промзона».

Запуск:  .venv/bin/python scripts/setup_south_bases.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import connect, init_db  # noqa: E402

BASE_KOTOVO = "Волгоградская обл., г. Котово, трубная база"
BASE_KOSHKI = "Самарская обл., с. Кошки, трубная площадка"
BASE_IBRAY = "Республика Татарстан, дер. Ст. Ибрайкино, трубная площадка"

# (подстроки нормализованного имени, роль, база). Порядок важен: первое
# совпадение выигрывает, поэтому сами базы стоят выше своих цехов.
RULES = [
    (["котово", "трубная база"], "база", BASE_KOTOVO),
    (["кошки", "трубная площадка"], "база", BASE_KOSHKI),
    (["ст ибрайкино"], "база", BASE_IBRAY),
    # Цеха Котовского куста (Волгоградская область).
    (["лапшинская"], "цех", BASE_KOTOVO),
    (["котовский", "спн"], "цех", BASE_KOTOVO),
    (["жирновский"], "цех", BASE_KOTOVO),
    (["овражный"], "цех", BASE_KOTOVO),
    (["арчеда"], "цех", BASE_KOTOVO),
    (["кудиновская"], "цех", BASE_KOTOVO),
    (["ключи"], "цех", BASE_KOTOVO),
    (["алексевская"], "цех", BASE_KOTOVO),
    (["николаевский"], "цех", BASE_KOTOVO),
    # Самарское направление.
    (["кошки", "бпо"], "цех", BASE_KOSHKI),
    (["константиновка"], "цех", BASE_KOSHKI),
    # Татарстан: ЦДНГ и одиночные Челны — к Ибрайкино (решение: мелкие
    # места к ближайшей базе).
    (["бпо ибрайкино"], "цех", BASE_IBRAY),
    (["набережные челны"], "цех", BASE_IBRAY),
]

# Починка сопоставления складов 1С: (подстроки места, правильный склад).
WAREHOUSE_FIX = [
    (["котово", "промзона", "трубная база"], "Котово Черникова Е.В"),
    (["жирновский", "цднг"], "Жирновский"),
]


def main() -> None:
    init_db()
    conn = connect()
    points = conn.execute("SELECT * FROM shipping_points").fetchall()
    changed = 0
    for p in points:
        norm = p["name_norm"]
        for subs, kind, base in RULES:
            if all(s in norm for s in subs):
                if p["kind"] == kind and p["base_name"] == base:
                    break
                conn.execute(
                    "UPDATE shipping_points SET kind = ?, base_name = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (kind, base, p["id"]))
                print(f"{kind:4s}  {p['name'][:60]}"
                      + (f"  → {base}" if kind == "цех" else ""))
                changed += 1
                break
    for subs, wh_name in WAREHOUSE_FIX:
        wh = conn.execute(
            "SELECT id, name FROM ref_warehouses WHERE is_group = 0 "
            "AND name LIKE ?", (f"%{wh_name}%",)).fetchone()
        if not wh:
            print(f"склад «{wh_name}» не найден — пропуск")
            continue
        for p in points:
            if all(s in p["name_norm"] for s in subs) \
                    and p["warehouse_name"] != wh["name"]:
                conn.execute(
                    "UPDATE shipping_points SET warehouse_id = ?, "
                    "warehouse_name = ?, match_source = 'вручную', "
                    "match_confirmed = 1, updated_at = datetime('now') "
                    "WHERE id = ?", (wh["id"], wh["name"], p["id"]))
                print(f"склад  {p['name'][:55]} → {wh['name']}")
                changed += 1
    conn.execute(
        "INSERT INTO audit_log (user_name, bp_id, action, details) "
        "VALUES ('скрипт setup_south_bases', NULL, 'point_roles', "
        "'роли цех/база РИТЭК-юг, правок: ' || ?)", (changed,))
    conn.commit()
    conn.close()
    print(f"Готово, правок: {changed}.")


if __name__ == "__main__":
    main()
