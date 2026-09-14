"""Импорт факта реализации из выгрузки 1С в план-факт БП.

Формат выгрузки на момент разработки не утверждён (образец «Реализация.xlsx»
ждём от 1С-специалиста), поэтому колонки распознаются по синонимам: имена в
выгрузках 1С пишут по-разному («Количество», «КоличествоПродажиАкт»,
«Объём, тн» — это одно и то же). Если колонка не найдена, сервис говорит,
какие заголовки в файле есть, вместо того чтобы молча загрузить нули.

Поддерживаются xlsx/xlsm и csv (разделитель определяется автоматически).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

# Синонимы заголовков. Сравнение — по строке в нижнем регистре без пробелов,
# знаков препинания и единиц измерения: «Объём, тн» = «объемтн».
COLUMN_SYNONYMS = {
    "bp_number": ["№бп", "номербп", "бп", "бизнесплан", "номербизнесплана",
                  "номерлота", "лот"],
    "period": ["месяц", "период", "датаотгрузки", "дата", "периодотгрузки",
               "месяцреализации"],
    "nomenclature": ["номенклатура", "наименование", "номенклатуранаименование",
                     "товар", "позиция"],
    "guid": ["номенклатурагуид", "гуид", "guid", "guidноменклатуры",
             "идноменклатуры"],
    "volume": ["объемтн", "объем", "количество", "количествопродажиакт",
               "количествотн", "тоннаж", "весстн", "количествофакт"],
    "revenue": ["выручкабезндс", "выручка", "выручкапродажиакт",
                "выручкафакт", "суммабезндс", "выручкаруббезндс"],
    "costs": ["прямыезатраты", "затраты", "себестоимость", "себестоимостьакт",
              "прямыерасходы", "затратыруб"],
    "buyer": ["покупатель", "контрагент", "клиент"],
}

# Символы, которые не влияют на смысл заголовка.
_CLEAN = re.compile(r"[\s_.,;:\-()/\\\"'«»]+")

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def _norm(text) -> str:
    """Заголовок в сравнимый вид. «ё» приводится к «е»: в выгрузках пишут и
    «Объём, тн», и «Объем» — без этого колонка не находилась."""
    return _CLEAN.sub("", str(text or "").strip().lower().replace("ё", "е"))


def _number(value) -> float | None:
    """Число из ячейки: «1 234,56», «1234.56», «-20 678,15» или None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    text = text.replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    return float(text)


def parse_period(value) -> tuple[int | None, int | None, int | None]:
    """(год, месяц, порядковый номер) из ячейки периода.

    Понимает дату, «2026-01-31», «01.2026», «январь 2026» и просто «3»
    (третий месяц срока вывоза). Год без месяца бессмыслен, поэтому
    возвращается тройка, и вызывающий решает, чем пользоваться."""
    if value is None or value == "":
        return None, None, None
    if isinstance(value, (datetime, date)):
        return value.year, value.month, None
    text = str(value).strip().lower()
    m = re.search(r"\b(20\d{2})[-./](\d{1,2})\b", text)          # 2026-01
    if m:
        return int(m.group(1)), int(m.group(2)), None
    m = re.search(r"\b(\d{1,2})[./](20\d{2})\b", text)           # 01.2026
    if m:
        return int(m.group(2)), int(m.group(1)), None
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", text)   # 31.01.2026
    if m:
        return int(m.group(3)), int(m.group(2)), None
    for name, num in _MONTHS.items():
        if name in text:
            year = re.search(r"\b(20\d{2})\b", text)
            return (int(year.group(1)) if year else None), num, None
    if re.fullmatch(r"\d{1,2}", text):        # просто номер месяца сделки
        return None, None, int(text)
    return None, None, None


def _read_table(path: Path) -> list[list]:
    """Строки файла как список списков (первая строка — заголовки)."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def detect_columns(header: list) -> dict:
    """Соответствие «поле → номер колонки» по синонимам заголовков."""
    found: dict[str, int] = {}
    normalized = [_norm(h) for h in header]
    for field, synonyms in COLUMN_SYNONYMS.items():
        for idx, name in enumerate(normalized):
            if not name:
                continue
            if name in synonyms or any(name.startswith(s) for s in synonyms):
                found.setdefault(field, idx)
                break
    return found


def parse_file(path: str | Path) -> dict:
    """Разбор выгрузки: строки факта, найденные колонки и замечания."""
    path = Path(path)
    table = _read_table(path)
    if not table:
        return {"rows": [], "columns": {}, "header": [],
                "problems": ["Файл пуст."]}

    # Заголовок ищется в первых 10 строках: в выгрузках 1С сверху бывает
    # шапка отчёта с названием и периодом.
    header, header_at, columns = [], 0, {}
    for i, row in enumerate(table[:10]):
        candidate = detect_columns(row)
        if len(candidate) > len(columns):
            header, header_at, columns = row, i, candidate
    problems = []
    for required in ("volume", "revenue"):
        if required not in columns:
            problems.append(
                f"Не найдена колонка «{'объём' if required == 'volume' else 'выручка'}». "
                "Заголовки файла: "
                + ", ".join(str(h) for h in header if h)[:300])

    rows = []
    for raw in table[header_at + 1:]:
        if not any(c not in (None, "") for c in raw):
            continue

        def cell(field):
            idx = columns.get(field)
            return raw[idx] if idx is not None and idx < len(raw) else None

        volume = _number(cell("volume"))
        revenue = _number(cell("revenue"))
        costs = _number(cell("costs"))
        if volume is None and revenue is None:
            continue
        year, month, ordinal = parse_period(cell("period"))
        rows.append({
            "bp_number": (str(cell("bp_number")).strip()
                          if cell("bp_number") not in (None, "") else None),
            "year": year, "month": month, "ordinal": ordinal,
            "nomenclature": (str(cell("nomenclature")).strip()
                             if cell("nomenclature") else None),
            "guid": (str(cell("guid")).strip() if cell("guid") else None),
            "buyer": (str(cell("buyer")).strip() if cell("buyer") else None),
            "volume": volume or 0.0,
            "revenue": revenue or 0.0,
            # Себестоимость в выгрузках 1С бывает со знаком минус — это
            # расход, а в план-факте затраты хранятся положительными.
            "costs": abs(costs) if costs is not None else 0.0,
        })
    if not rows and not problems:
        problems.append("В файле нет строк с объёмом или выручкой.")
    return {"rows": rows, "columns": columns, "header": header,
            "problems": problems}


def month_index(row: dict, start_month: int | None, start_year: int | None,
                months: int) -> int | None:
    """Номер месяца БП (1..срок вывоза) для строки выгрузки.

    Календарный период переводится в номер по месяцу начала вывоза. Если
    начало не задано, работает только явный порядковый номер из файла —
    угадывать соответствие сервис не должен."""
    if row["ordinal"]:
        return row["ordinal"] if 1 <= row["ordinal"] <= months else None
    if not row["month"] or not start_month:
        return None
    if row["year"] and start_year:
        offset = (row["year"] - start_year) * 12 + (row["month"] - start_month)
    else:
        offset = (row["month"] - start_month) % 12
    index = offset + 1
    return index if 1 <= index <= months else None


def aggregate(rows: list[dict], bp_number: str | None, start_month: int | None,
              start_year: int | None, months: int) -> dict:
    """Свод строк выгрузки по месяцам БП.

    Берутся строки своего БП: по номеру, если он в файле есть, иначе —
    все строки (файл выгружен по одной сделке). Возвращает суммы по месяцам
    и то, что не легло: без месяца или от другого БП."""
    mine, other, no_month = [], 0, 0
    for r in rows:
        if r["bp_number"] and bp_number and _norm(r["bp_number"]) != _norm(bp_number):
            other += 1
            continue
        mine.append(r)

    by_month: dict[int, dict] = {}
    for r in mine:
        idx = month_index(r, start_month, start_year, months)
        if idx is None:
            no_month += 1
            continue
        acc = by_month.setdefault(idx, {"month": idx, "volume": 0.0,
                                        "revenue": 0.0, "costs": 0.0,
                                        "lines": 0})
        acc["volume"] += r["volume"]
        acc["revenue"] += r["revenue"]
        acc["costs"] += r["costs"]
        acc["lines"] += 1
    return {
        "months": [by_month[k] for k in sorted(by_month)],
        "skipped_other_bp": other,
        "skipped_no_month": no_month,
        "total_volume": sum(m["volume"] for m in by_month.values()),
        "total_revenue": sum(m["revenue"] for m in by_month.values()),
        "total_costs": sum(m["costs"] for m in by_month.values()),
    }
