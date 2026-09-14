"""Импортёр еженедельного отчёта MMI (xlsx «Еженедельный обзор рынка стального лома»).

Что берём:
  «Цены по потр»   — ежедневные средневзвешенные цены С ЖД-тарифом по 33 заводам
                     → market_quotes (для заводов из нашего справочника — прямо
                       в metric plant_<ID>, питает калькулятор) + слепок недели
  «Цены по обл»    — цены БЕЗ ЖД-тарифа по областям (наши регионы → market_quotes)
  «Потребители Всего/СР» — отгрузки по заводам (спрос) → слепок
  «Матрица отгрузок», «Межрегиональные поставки» → слепок (вкладка «Матрица связей»)

Слепок недели хранится в weekly_snapshots (kind=mmi_week) — история копится.
"""
from __future__ import annotations
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import ImportLog, MarketQuote, WeeklySnapshot
from app.services.market import save_quote

log = logging.getLogger("import_mmi")

# Завод в отчёте MMI → id в нашем справочнике (что маппится — питает калькулятор)
MMI_PLANT_MAP = {
    "Магнитогорский МК": "MMK",
    "Северский ТЗ": "SEVERSTAL_TZ",
    "Первоуральский НТЗ": "PNTZ",
    "Череповецкий МК": "SEVERSTAL",
    "Новолипецкий МК": "NLMK",
    "Белорусский МЗ": "BMZ",
    "Выксунский МЗ": "OMK_STAL",
    "ЕВРАЗ-ЗСМК": "EVRAZ_ZSMK",
    "Волжский ТЗ": "VOLZHSKY_TZ",
    "Таганрогский МЗ": "TAGANROG_MZ",
    "Абинский ЭМЗ": "ABINSK_EMZ",
}
FO_ROWS = {"УФО", "ЦФО", "СФО", "ЮФО", "Итого", "Прочие"}

# Область MMI → наш регион (для сводки)
MMI_OBL_MAP = {
    "Пермский кр.": "PERM",
    "респ. Коми": "KOMI_NORTH",
    "Ханты-Мансийский авт. окр.": "HMAO",
    "Волгоградская обл.": "SOUTH",
    "Свердловская обл.": "УРАЛ",
    "Челябинская обл.": "УРАЛ",
}


def looks_like_mmi(path: Path) -> bool:
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return False
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        ok = "Цены по потр" in wb.sheetnames and "Матрица отгрузок" in wb.sheetnames
        wb.close()
        return ok
    except Exception:
        return False


def _dates_row(ws, row=2):
    """Колонки-даты в шапке листа: {col: date} (пропускаем «Итого»)."""
    out = {}
    for c in range(2, ws.max_column + 1):
        v = ws.cell(row, c).value
        if isinstance(v, datetime):
            out[c] = v.date()
    return out


def _series_table(ws) -> dict:
    """Лист вида «строка × даты» → {label: {iso_date: value}} + порядок строк."""
    dates = _dates_row(ws)
    data, order = {}, []
    for r in range(3, ws.max_row + 1):
        label = ws.cell(r, 1).value
        if not label or str(label).startswith("Источник"):
            continue
        label = str(label).strip()
        row = {}
        for c, d in dates.items():
            v = ws.cell(r, c).value
            if isinstance(v, (int, float)) and v > 0:
                row[d.isoformat()] = round(float(v), 2)
        if row:
            data[label] = row
            order.append(label)
    return {"rows": data, "order": order}


def _matrix(ws, header_row: int, first_data_row: int) -> dict:
    """Матрица завод × область → {plant: {region: tons}}."""
    cols = {}
    for c in range(2, ws.max_column + 1):
        v = ws.cell(header_row, c).value
        if v:
            cols[c] = str(v).strip()
    out = {}
    for r in range(first_data_row, ws.max_row + 1):
        label = ws.cell(r, 1).value
        if not label:
            continue
        label = str(label).strip()
        if label.startswith(("Источник", "Итого", "Общий итог")):
            continue
        row = {}
        for c, reg in cols.items():
            if reg.startswith(("Итого", "Общий итог")):
                continue
            v = ws.cell(r, c).value
            if isinstance(v, (int, float)) and v > 0:
                row[reg] = round(float(v), 1)
        if row:
            out[label] = row
    return out


def import_mmi(db: Session, path: Path) -> ImportLog:
    import openpyxl
    import warnings
    warnings.filterwarnings("ignore")
    entry = ImportLog(filename=path.name, kind="mmi_week")
    try:
        wb = openpyxl.load_workbook(path, data_only=True)

        prices_plant = _series_table(wb["Цены по потр"])      # с ЖДТ
        prices_obl = _series_table(wb["Цены по обл"])         # без ЖДТ
        ship_total = _series_table(wb["Потребители Всего"])
        ship_free = _series_table(wb["Потребители СР"])
        matrix = _matrix(wb["Матрица отгрузок"], header_row=5, first_data_row=6)

        # Неделя = минимальная дата в ценах
        all_dates = sorted({d for row in prices_plant["rows"].values() for d in row})
        if not all_dates:
            raise ValueError("В отчёте не найдены даты/цены")
        week_start = date.fromisoformat(all_dates[0])
        last_date = all_dates[-1]

        # ── market_quotes: последняя цена недели по заводам нашего справочника
        saved_quotes = 0
        for mmi_name, series in prices_plant["rows"].items():
            if mmi_name in FO_ROWS:
                continue
            last_d = max(series)
            price = series[last_d]
            pid = MMI_PLANT_MAP.get(mmi_name)
            metric = f"plant_{pid}" if pid else f"mmi_plant_{mmi_name}"
            dup = (db.query(MarketQuote)
                   .filter(MarketQuote.metric == metric, MarketQuote.value == price,
                           MarketQuote.source.like("MMI%")).first())
            if dup:
                continue
            save_quote(db, metric=metric, value=price, source=f"MMI неделя {week_start:%d.%m}",
                       quality="live", basis="CPT_RD", unit="RUB/т",
                       extra={"plant": mmi_name, "date": last_d})
            saved_quotes += 1

        # ── цены без ЖДТ по нашим областям
        for obl, series in prices_obl["rows"].items():
            reg = MMI_OBL_MAP.get(obl)
            if not reg:
                continue
            last_d = max(series)
            price = series[last_d]
            dup = (db.query(MarketQuote)
                   .filter(MarketQuote.metric == "lom3a_obl", MarketQuote.region == reg,
                           MarketQuote.value == price, MarketQuote.source.like("MMI%")).first())
            if not dup:
                save_quote(db, metric="lom3a_obl", value=price,
                           source=f"MMI неделя {week_start:%d.%m}", quality="live",
                           region=reg, basis="FCA_NO_RAIL", unit="RUB/т",
                           extra={"obl": obl, "date": last_d})
                saved_quotes += 1

        # ── слепок недели
        payload = {
            "prices_plant": prices_plant, "prices_obl": prices_obl,
            "shipments_total": ship_total, "shipments_free": ship_free,
            "matrix": matrix, "last_date": last_date,
        }
        snap = (db.query(WeeklySnapshot)
                .filter(WeeklySnapshot.kind == "mmi_week",
                        WeeklySnapshot.week_start == week_start).first())
        if snap:
            snap.payload = payload
            snap.created_at = datetime.utcnow()
        else:
            db.add(WeeklySnapshot(kind="mmi_week", week_start=week_start,
                                  source=path.name, payload=payload))
        db.commit()
        entry.rows_total = len(prices_plant["rows"]) + len(matrix)
        entry.rows_loaded = saved_quotes
        entry.message = f"неделя с {week_start}, котировок +{saved_quotes}"
        db.add(entry)
        db.commit()
        log.info("MMI импортирован: %s (%s)", path.name, entry.message)
    except Exception as e:
        db.rollback()
        entry.status = "error"
        entry.message = str(e)[:500]
        db.add(entry)
        db.commit()
        log.exception("MMI импорт упал: %s", path.name)
    return entry
