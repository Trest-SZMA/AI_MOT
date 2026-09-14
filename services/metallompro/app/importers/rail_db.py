"""Импортёр повагонной базы ЖД-отправок лома (выгрузка ИВМ, cp1251, «;»).

Заголовок: Дата отправления;Вид перевозки;Код груза;…;Объем (тонн);Тариф;Вагоны
Каждая строка — отправка: маршрут, грузополучатель (завод), род вагона,
собственник/оператор вагона, тонны и ФАКТИЧЕСКИЙ тариф РЖД за отправку.
"""
from __future__ import annotations
import csv
import hashlib
import logging
from datetime import datetime, date
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import ImportLog, RailShipment

log = logging.getLogger("import_rail")

# Грузополучатель (как в накладной) → id завода нашего справочника
CONSIGNEE_PLANT = [
    ("ММК", "MMK"), ("МАГНИТОГОРСК", "MMK"),
    ("ПРОМСОРТ-УРАЛ", "PROMSORT_URAL"),
    ("СЕВЕРСКИЙ", "SEVERSTAL_TZ"),
    ("ТМК", "SEVERSTAL_TZ"),          # ПАО ТМК — Северский/Волжский/Таганрог; уточняется станцией
    ("НЛМК", "NLMK"),
    ("АБИНСКИЙ", "ABINSK_EMZ"),
    ("СЕВЕРСТАЛЬ", "SEVERSTAL"),
    ("УГМК", "UGMK_TUMEN"),
    ("ЕВРАЗ", "EVRAZ_ZSMK"),
    ("ВМЗ", "OMK_STAL"), ("ВЫКСУНСКИЙ", "OMK_STAL"),
    ("БМЗ", "BMZ"), ("БЕЛОРУССКИЙ", "BMZ"),
    ("ПЕРВОУРАЛЬСКИЙ", "PNTZ"), ("ПНТЗ", "PNTZ"),
    ("ТАГМЕТ", "TAGANROG_MZ"), ("ТАГАНРОГ", "TAGANROG_MZ"),
    ("ВОЛЖСКИЙ", "VOLZHSKY_TZ"),
]
# Станция назначения уточняет завод внутри ТМК
TMK_STATIONS = {"ПОЛЕВСКОЙ": "SEVERSTAL_TZ", "ВОЛЖСКИЙ": "VOLZHSKY_TZ",
                "ТАГАНРОГ": "TAGANROG_MZ"}


def map_plant(consignee: str, to_station: str) -> str:
    c = (consignee or "").upper()
    st = (to_station or "").upper()
    for kw, pid in CONSIGNEE_PLANT:
        if kw in c:
            if pid == "SEVERSTAL_TZ" and "ТМК" in c:
                for skw, spid in TMK_STATIONS.items():
                    if skw in st:
                        return spid
            return pid
    return ""


def looks_like_rail(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    try:
        head = open(path, "rb").read(4000)
        for enc in ("cp1251", "utf-8-sig"):
            try:
                text = head.decode(enc)
                return "Дата отправления" in text and "Тариф" in text
            except UnicodeDecodeError:
                continue
    except OSError:
        pass
    return False


def _f(s) -> float:
    try:
        return float((s or "0").replace(",", ".").replace(" ", ""))
    except ValueError:
        return 0.0


def _d(s) -> date | None:
    try:
        return datetime.strptime((s or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def import_rail(db: Session, path: Path) -> ImportLog:
    entry = ImportLog(filename=path.name, kind="rail")
    try:
        enc = "cp1251"
        try:
            open(path, encoding="utf-8-sig").read(200)
            enc = "utf-8-sig"
        except UnicodeDecodeError:
            pass
        seen: dict[str, int] = {}
        total = loaded = 0
        batch = []

        def _flush(batch):
            nonlocal loaded
            if not batch:
                return 0
            hashes = [o.row_hash for o in batch]
            dup = {h for (h,) in db.query(RailShipment.row_hash)
                   .filter(RailShipment.row_hash.in_(hashes)).all()}
            fresh = [o for o in batch if o.row_hash not in dup]
            if fresh:
                db.bulk_save_objects(fresh)
                db.commit()
                loaded += len(fresh)
        with open(path, encoding=enc, newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                total += 1
                d = _d(r.get("Дата отправления"))
                tons = _f(r.get("Объем (тонн)"))
                if not d or tons <= 0:
                    continue
                consignee = (r.get("Грузополучатель") or "").strip()
                to_station = (r.get("Станция назначения РФ") or "").strip()
                base = "|".join([str(d), r.get("Станция отправления РФ", ""),
                                 to_station, consignee, r.get("Грузоотправитель", ""),
                                 str(tons), r.get("Тариф", ""), r.get("Вагоны", "")])
                h0 = hashlib.md5(base.encode()).hexdigest()
                n = seen[h0] = seen.get(h0, 0) + 1
                h = hashlib.md5(f"{h0}#{n}".encode()).hexdigest()
                batch.append(RailShipment(
                    ship_date=d,
                    transport_kind=(r.get("Вид перевозки") or "").strip(),
                    from_region=(r.get("Область отправления") or "").strip(),
                    from_station=(r.get("Станция отправления РФ") or "").strip(),
                    to_region=(r.get("Область назначения") or "").strip(),
                    to_station=to_station,
                    consignor=(r.get("Грузоотправитель") or "").strip(),
                    consignee=consignee,
                    plant_id=map_plant(consignee, to_station),
                    wagon_kind=(r.get("Род вагона") or "").strip(),
                    wagon_owner=(r.get("Собственник") or "").strip(),
                    wagon_operator=(r.get("Оператор") or "").strip(),
                    tons=tons, tariff_rub=_f(r.get("Тариф")),
                    wagons=int(_f(r.get("Вагоны"))),
                    row_hash=h,
                ))
                if len(batch) >= 2000:
                    _flush(batch)
                    batch = []
        _flush(batch)
        entry.rows_total, entry.rows_loaded = total, loaded
        entry.rows_skipped = total - loaded
        db.add(entry)
        db.commit()
        log.info("ЖД-база: %s → +%d отправок", path.name, loaded)
    except Exception as e:
        db.rollback()
        entry.status = "error"
        entry.message = str(e)[:500]
        db.add(entry)
        db.commit()
        log.exception("ЖД импорт упал: %s", path.name)
    return entry
