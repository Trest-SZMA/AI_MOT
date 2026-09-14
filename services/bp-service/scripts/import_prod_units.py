"""Загрузка справочника производственных подразделений 1С в ref_prod_units.

Источник — выгрузка «_Производственные_подразделения__*.csv» (папка 1c):
Наименование, Родитель, Код, Функциональное и Аналитическое подразделение
(база цеха). Справочник показывает официальную структуру «цех → база» и
служит подсказкой при задании ролей пунктов отгрузки.

Запуск:  .venv/bin/python scripts/import_prod_units.py [путь_к_csv]
Без аргумента берётся самый свежий файл по маске из ../1c/.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import connect, init_db  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        folder = Path(__file__).resolve().parent.parent.parent / "1c"
        files = sorted(folder.glob("_Производственные_подразделения_*.csv"))
        if not files:
            sys.exit(f"Файл выгрузки не найден в {folder}")
        path = files[-1]
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    if not rows or "Наименование" not in rows[0]:
        sys.exit("Не похоже на выгрузку подразделений: нет колонки «Наименование».")

    init_db()
    conn = connect()
    conn.execute("DELETE FROM ref_prod_units")
    n = 0
    for r in rows:
        name = (r.get("Наименование") or "").strip()
        if not name:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO ref_prod_units (name, code, parent_name, "
            "functional_base, analytic_base, guid, parent_guid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, (r.get("Код") or "").strip(),
             (r.get("Родитель") or "").strip(),
             (r.get("ФункциональноеПодразделение") or "").strip(),
             (r.get("АналитическоеПодразделение") or "").strip(),
             (r.get("СсылкаГуид") or "").strip() or None,
             (r.get("РодительГуид") or "").strip() or None))
        n += 1
    conn.commit()
    bases = conn.execute(
        "SELECT COUNT(DISTINCT analytic_base) FROM ref_prod_units "
        "WHERE analytic_base != ''").fetchone()[0]
    conn.close()
    print(f"Загружено {n} подразделений из {path.name}; "
          f"аналитических баз: {bases}.")


if __name__ == "__main__":
    main()
