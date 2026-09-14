"""Сжать выгрузку «Движение ТМЦ» до строк и колонок, нужных импортёру сделок.

Полный файл — 527 МБ (75 колонок, 256 тыс. строк), из них сделками являются
101 тыс. строк, а импортёру нужны 12 колонок. Возить на сервер полбайта
гигабайта незачем: срез весит около 20 МБ и читается тем же
`app/importers/deals.py`, потому что имена колонок сохранены.

    .venv/bin/python scripts/slim_movements.py [исходный.csv] [срез.csv]
"""
from __future__ import annotations

import csv
import glob
import sys
from pathlib import Path

KEEP = ["Период", "ХозяйственнаяОперация", "Количество", "Цена", "Выручка",
        "ЕдиницаИзмерения", "Номенклатура", "НоменклатураГуид",
        "Контрагент", "КонтрагентГуид", "Менеджер", "Склад"]

OPS = ("Реализация", "Закупка у поставщика", "Закупка через подотчетное лицо")

DIR = Path(__file__).resolve().parents[1] / "data" / "1c"


def main(src: str | None = None, dst: str | None = None) -> None:
    if src is None:
        found = [p for p in glob.glob(str(DIR / "*.csv")) if "Движение" in Path(p).name]
        if not found:
            raise SystemExit(f"не найдена выгрузка «Движение ТМЦ» в {DIR}")
        src = max(found)
    dst = dst or str(DIR / "Движение_ТМЦ_сделки.csv")

    rows = kept = 0
    with open(src, encoding="utf-8-sig", newline="") as fi, \
            open(dst, "w", encoding="utf-8-sig", newline="") as fo:
        reader = csv.DictReader(fi)
        missing = [c for c in KEEP if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"в выгрузке нет колонок: {', '.join(missing)}")
        writer = csv.DictWriter(fo, fieldnames=KEEP, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            rows += 1
            op = (row.get("ХозяйственнаяОперация") or "").strip()
            if not op.startswith(OPS):
                continue
            writer.writerow({k: row.get(k) for k in KEEP})
            kept += 1
    size_mb = Path(dst).stat().st_size / 1024 / 1024
    print(f"{src}\n  → {dst}\n  строк {rows} → {kept}, размер {size_mb:,.1f} МБ")


if __name__ == "__main__":
    main(*(sys.argv[1:3] or [None, None])[:2])
