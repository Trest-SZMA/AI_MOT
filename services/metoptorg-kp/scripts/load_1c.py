"""Загрузка выгрузок 1С в базу: цены, разборка, логистика, сделки.

Порядок важен: справочник должен быть загружен раньше (scripts/load_all.py),
иначе движения не к чему привязывать. Файлы ищутся по подстроке имени в
data/1c — выгрузки приходят с меткой времени в названии.

    .venv/bin/python scripts/load_1c.py            # всё
    .venv/bin/python scripts/load_1c.py deals      # только сделки
"""
from __future__ import annotations

import glob
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal, init_db  # noqa: E402
from app.importers.deals import import_deals  # noqa: E402
from app.importers.one_c import (  # noqa: E402
    import_disassembly,
    import_fact_business_yields,
    import_logistics_rates,
    import_purchase_facts,
    import_sales_facts,
)

DIR = Path(__file__).resolve().parents[1] / "data" / "1c"


def find(fragment: str) -> str:
    matches = [p for p in glob.glob(str(DIR / "*.csv")) if fragment in Path(p).name]
    if not matches:
        raise SystemExit(f"не найден файл выгрузки со словом «{fragment}» в {DIR}")
    return max(matches)  # свежая выгрузка — с наибольшей меткой времени


STEPS = {
    "sales": ("цены продаж", lambda s: import_sales_facts(s, find("Движение"))),
    "purchases": ("цены закупок", lambda s: import_purchase_facts(s, find("Закупки"))),
    "disassembly": ("факт разборки", lambda s: import_disassembly(s, find("Движение"))),
    "yields": ("деловой выход по факту",
               lambda s: import_fact_business_yields(s, find("Движение"))),
    "logistics": ("ставки перевозки", lambda s: import_logistics_rates(s, find("Отвесная"))),
    # Сделки грузятся последними: они опираются на guid'ы карточек и алиасы.
    "deals": ("сделки по контрагентам", lambda s: import_deals(s, find("Движение"))),
}


def main(only: list[str]) -> None:
    init_db()
    s = SessionLocal()
    try:
        for key, (title, fn) in STEPS.items():
            if only and key not in only:
                continue
            t = time.time()
            try:
                res = fn(s)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 — один файл не должен рушить загрузку
                print(f"{title}: ОШИБКА {type(e).__name__}: {e}")
                continue
            print(f"{title}: {res} ({time.time() - t:.1f} с)")
    finally:
        s.close()


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if not a.startswith("-")])
