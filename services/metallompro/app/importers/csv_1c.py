"""Импортёр CSV-выгрузок 1С.

Тип файла определяется по заголовкам (имя файла не важно):
  • «Выручка на загрузку»            → co_sales   (колонка ВыручкаПродажиАкт)
  • «Отвесная»                       → co_trips   (колонка ВесПоТТН)
  • «СебестоимостьТоваровОбороты»    → co_stock_moves (КоличествоПриход/Расход)
Остальные файлы регистрируются в import_log со статусом skipped.

Дедупликация: md5-хэш значимых полей строки (+порядковый номер повтора в файле),
поэтому один и тот же файл можно заливать повторно — дублей не будет.
"""
from __future__ import annotations
import csv
import hashlib
import logging
from datetime import datetime, date
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import CompanySale, CompanyTrip, CompanyStock, ImportLog

log = logging.getLogger("import_1c")
csv.field_size_limit(10_000_000)

BATCH = 5000


# ── Классификация номенклатуры ───────────────────────────────────
def item_group(item: str) -> str:
    t = (item or "").lower()
    if "кабель" in t:
        return "cable"
    if any(k in t for k in ("медь", "меди", "медн", "алюмин", "латун", "свинец", "свинц",
                            "цинк", "бронз", "нерж", "нейзильбер", "цам")):
        return "cvetmet"
    if any(k in t for k in ("труба", "нкт", "штанга", "пш", "муфта")):
        return "pipes"
    if any(k in t for k in ("лом", "чермет", "обрезь", "стружка", "чугун", "н/л")):
        return "chermet"
    return "other"


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()[:10]
    if not s or s.startswith("0001"):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _f(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _hash(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def detect_kind(headers: list[str]) -> str:
    hs = set(headers)
    if "ВыручкаПродажиАкт" in hs:
        return "sales"
    if "ВесПоТТН" in hs:
        return "trips"
    if "КоличествоПриход" in hs and "КоличествоРасход" in hs:
        return "stock"
    return "unknown"


# ── Обработчики строк ────────────────────────────────────────────
def _row_sale(r: dict, seen: dict) -> CompanySale | None:
    period = _parse_date(r.get("Период", ""))
    qty, rev = _f(r.get("КоличествоПродажиАкт")), _f(r.get("ВыручкаПродажиАкт"))
    if not period or qty <= 0 or rev <= 0:
        return None
    item = (r.get("Номенклатура") or "").strip()
    h = _hash("sale", period, r.get("Покупатель"), item, r.get("Склад"),
              qty, rev, r.get("Операция"))
    n = seen[h] = seen.get(h, 0) + 1
    return CompanySale(
        period=period, buyer=(r.get("Покупатель") or "").strip(), item=item,
        item_group=item_group(item), warehouse=(r.get("Склад") or "").strip(),
        division=(r.get("Дивизион") or "").strip(),
        operation=(r.get("Операция") or "").strip(),
        qty_t=qty, revenue_rub=rev, cost_rub=_f(r.get("СебестоимостьАкт")),
        row_hash=_hash(h, n),
    )


def _row_trip(r: dict, seen: dict) -> CompanyTrip | None:
    d = _parse_date(r.get("ДатаПогрузки", ""))
    w = _f(r.get("ВесПоТТН"))
    if not d or w <= 0:
        return None
    item = (r.get("Номенклатура") or "").strip()
    guid = (r.get("СсылкаГуид") or "").strip()
    h = _hash("trip", guid or (d, r.get("МестоПогрузки"), r.get("Грузополучатель"),
                               item, w, r.get("Стоимость")))
    n = seen[h] = seen.get(h, 0) + 1
    return CompanyTrip(
        load_date=d, from_place=(r.get("МестоПогрузки") or "").strip(),
        recipient=(r.get("Грузополучатель") or "").strip(),
        km=_f(r.get("Километраж")), item=item, item_group=item_group(item),
        weight_ttn=w, weight_net=_f(r.get("Отвесная")),
        cost_rub=_f(r.get("Стоимость")),
        delivery_kind=(r.get("ВидДоставки") or "").strip(),
        carrier=(r.get("Перевозчик") or "").strip(),
        vehicle=(r.get("ГосНомер") or r.get("ТранспортноеСредство") or "").strip(),
        warehouse=(r.get("Склад") or "").strip(),
        division=(r.get("Подразделение") or "").strip(),
        guid=guid, row_hash=_hash(h, n),
    )


def _row_stock(r: dict, seen: dict) -> CompanyStock | None:
    period = _parse_date(r.get("Период", ""))
    inc, dec = _f(r.get("КоличествоПриход")), _f(r.get("КоличествоРасход"))
    if inc == 0 and dec == 0:
        return None
    item = (r.get("АналитикаУчетаНоменклатурыНоменклатура") or "").strip()
    wh = (r.get("АналитикаУчетаНоменклатурыСкладскаяТерритория") or "").strip()
    h = _hash("stock", period, r.get("Регистратор"), item, wh, inc, dec)
    n = seen[h] = seen.get(h, 0) + 1
    return CompanyStock(
        period=period, warehouse=wh, item=item, item_group=item_group(item),
        operation=(r.get("РазделУчета") or "").strip(), qty=inc - dec, value_rub=0.0,
        division=(r.get("АналитикаУчетаНоменклатурыСкладскаяТерриторияПодразделение") or "").strip(),
        row_hash=_hash(h, n),
    )


HANDLERS = {"sales": (_row_sale, CompanySale),
            "trips": (_row_trip, CompanyTrip),
            "stock": (_row_stock, CompanyStock)}


# ── Импорт файла ─────────────────────────────────────────────────
def import_file(db: Session, path: Path) -> ImportLog:
    started = datetime.utcnow()
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            kind = detect_kind(reader.fieldnames or [])
            entry = ImportLog(filename=path.name, kind=kind)
            if kind == "unknown":
                entry.status = "skipped"
                entry.message = "Тип не распознан — этот файл сейчас не используется"
                db.add(entry)
                db.commit()
                return entry

            handler, model = HANDLERS[kind]
            # Дедуп ПО-БАТЧЕВО (SELECT ... WHERE row_hash IN (...)) — раньше грузили
            # ВСЕ хэши таблицы в память разом, и на регистре в 300+ тыс. строк это
            # съедало сотни МБ RAM при каждом импорте.
            seen: dict[str, int] = {}
            total = loaded = skipped = 0
            batch = []

            def _flush(batch):
                nonlocal loaded, skipped
                if not batch:
                    return
                hashes = [o.row_hash for o in batch]
                dup = {h for (h,) in db.query(model.row_hash)
                       .filter(model.row_hash.in_(hashes)).all()}
                fresh = [o for o in batch if o.row_hash not in dup]
                skipped += len(batch) - len(fresh)
                if fresh:
                    db.bulk_save_objects(fresh)
                    db.commit()
                    loaded += len(fresh)

            for r in reader:
                total += 1
                obj = handler(r, seen)
                if obj is None:
                    skipped += 1
                    continue
                batch.append(obj)
                if len(batch) >= BATCH:
                    _flush(batch)
                    batch = []
            _flush(batch)
            entry.rows_total, entry.rows_loaded, entry.rows_skipped = total, loaded, skipped
            entry.message = f"за {(datetime.utcnow() - started).seconds} сек"
            db.add(entry)
            db.commit()
            log.info("Импорт %s (%s): %d загружено, %d пропущено", path.name, kind, loaded, skipped)
            return entry
    except Exception as e:
        db.rollback()
        entry = ImportLog(filename=path.name, kind="error", status="error", message=str(e)[:500])
        db.add(entry)
        db.commit()
        log.exception("Импорт %s упал", path.name)
        return entry


def import_any(db: Session, path: Path) -> ImportLog:
    """Диспетчер: MMI-xlsx / повагонная ЖД-база / CSV 1С — по содержимому файла."""
    from app.importers.mmi_weekly import looks_like_mmi, import_mmi
    from app.importers.rail_db import looks_like_rail, import_rail
    if looks_like_mmi(path):
        return import_mmi(db, path)
    if looks_like_rail(path):
        return import_rail(db, path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        entry = ImportLog(filename=path.name, kind="unknown", status="skipped",
                          message="xlsx не распознан (ожидаю еженедельный отчёт MMI)")
        db.add(entry)
        db.commit()
        return entry
    return import_file(db, path)


ARCHIVE_MAX_MB = 50          # файлы крупнее — не архивируем (данные уже в БД,
ARCHIVE_KEEP_DAYS = 30       # оригинал есть у источника), архив чистим по сроку


def scan_inbox(db: Session, inbox: Path, archive: Path) -> list[dict]:
    """Watch-папка: импортировать все CSV/XLSX; мелкое — в архив, гиганты — удалить."""
    results = []
    for path in sorted(list(inbox.glob("*.csv")) + list(inbox.glob("*.xlsx"))):
        entry = import_any(db, path)
        results.append({"file": path.name, "kind": entry.kind, "status": entry.status,
                        "loaded": entry.rows_loaded, "skipped": entry.rows_skipped})
        try:
            if path.stat().st_size > ARCHIVE_MAX_MB * 1024 * 1024:
                path.unlink()
            else:
                path.rename(archive / f"{datetime.now():%Y%m%d_%H%M%S}_{path.name}")
        except OSError:
            pass
    return results


def cleanup_archive(archive: Path) -> int:
    """Удалить из архива файлы старше ARCHIVE_KEEP_DAYS. Запускается раз в сутки."""
    import time
    cutoff = time.time() - ARCHIVE_KEEP_DAYS * 86400
    removed = 0
    for p in archive.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("Архив: удалено старых файлов: %d", removed)
    return removed
