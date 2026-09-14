"""Импорт еженедельного обзора рынка стального лома.

Файл даёт внешний ориентир цен, которого у нас нет в 1С:
  * «Цены по обл»  — средневзвешенная цена продаж ИЗ субъекта, базис FCA
    (без ж/д тарифа) — прямо сопоставима с нашей отгрузкой с базы;
  * «Цены по потр» — цена входящего лома НА предприятия, базис CPT
    (с ж/д тарифом) — показывает, сколько платит завод-потребитель.

Индекс НЕ подменяет наш прайс: по факту 1С компания продаёт чермет заметно
дешевле индекса (марка, засор, условия приёмки). Поэтому рабочая цена
считается как индекс × исторический коэффициент компании (см. calibration).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import statistics
from dataclasses import dataclass, field

import openpyxl
from sqlalchemy.orm import Session

from ..db.models import MarketPrice

log = logging.getLogger(__name__)

SHEET_REGIONS = "Цены по обл"
SHEET_CONSUMERS = "Цены по потр"
SHEET_CONTENTS = "Содержание"
MIN_PRICE, MAX_PRICE = 1_000, 200_000


@dataclass
class MarketImportStats:
    regions: int = 0
    consumers: int = 0
    issue: str | None = None
    period: tuple | None = None
    skipped: list = field(default_factory=list)


def _issue_and_week(wb) -> tuple[str | None, dt.datetime | None, dt.datetime | None]:
    """Номер недели и период из листа «Содержание»."""
    if SHEET_CONTENTS not in wb.sheetnames:
        return None, None, None
    text = " ".join(
        str(c) for row in wb[SHEET_CONTENTS].iter_rows(max_row=10, values_only=True)
        for c in row if c)
    week = re.search(r"Неделя\s*№?\s*(\d+)", text)
    # «выпуск 24.08.2026» — дата публикации, а не начало недели, поэтому
    # период берём из строки «Неделя № 34: 17.08.2026 - 23.08.2026»
    span = re.search(r"Неделя[^\d]*\d+[^\d]*(\d{2}\.\d{2}\.\d{4})\D+"
                     r"(\d{2}\.\d{2}\.\d{4})", text)
    start = end = None
    if span:
        start = dt.datetime.strptime(span.group(1), "%d.%m.%Y")
        end = dt.datetime.strptime(span.group(2), "%d.%m.%Y")
    else:
        dates = sorted(dt.datetime.strptime(d, "%d.%m.%Y")
                       for d in re.findall(r"(\d{2}\.\d{2}\.\d{4})", text))
        if len(dates) >= 2:
            start, end = dates[0], dates[-1]
    year = end.year if end else dt.date.today().year
    issue = f"{week.group(1)}-{year}" if week else None
    return issue, start, end


def _parse_sheet(wb, sheet: str) -> list[tuple[str, float, float, float, int]]:
    """→ [(название, средняя, мин, макс, наблюдений)] по строкам листа цен."""
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    # строка дат — первая, где больше трёх дат подряд
    date_row = next((r for r in rows[:5]
                     if sum(isinstance(c, dt.datetime) for c in r) >= 3), None)
    if date_row is None:
        return []
    ncols = sum(1 for c in date_row if isinstance(c, dt.datetime))
    out = []
    for row in rows:
        name = str(row[0] or "").strip()
        if not name or name == "" or isinstance(row[0], dt.datetime):
            continue
        vals = [float(v) for v in row[1:ncols + 2]
                if isinstance(v, (int, float)) and MIN_PRICE < v < MAX_PRICE]
        if not vals:
            continue
        out.append((name, statistics.mean(vals), min(vals), max(vals), len(vals)))
    return out


def import_market_review(session: Session, xlsx_path: str,
                         source_file_id: int | None = None) -> MarketImportStats:
    stats = MarketImportStats()
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    issue, start, end = _issue_and_week(wb)
    stats.issue = issue
    stats.period = (start, end)

    for sheet, kind, basis in ((SHEET_REGIONS, "region", "FCA"),
                               (SHEET_CONSUMERS, "consumer", "CPT")):
        if sheet not in wb.sheetnames:
            stats.skipped.append(f"нет листа «{sheet}»")
            continue
        rows = _parse_sheet(wb, sheet)
        # повторная загрузка того же выпуска не плодит дубликаты
        session.query(MarketPrice).filter(
            MarketPrice.issue == issue,
            MarketPrice.scope_kind == kind).delete(synchronize_session=False)
        for name, avg, lo, hi, n in rows:
            session.add(MarketPrice(
                scope_kind=kind, scope=name, material="чермет", grade="3А",
                basis=basis, price_per_tonne=round(avg, 2),
                price_min=lo, price_max=hi, observations=n,
                period_start=start, period_end=end, issue=issue,
                source_file_id=source_file_id))
        if kind == "region":
            stats.regions = len(rows)
        else:
            stats.consumers = len(rows)
    wb.close()
    session.commit()
    return stats
