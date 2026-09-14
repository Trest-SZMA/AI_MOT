"""Импорт кабельных справочников содержания металлов.

Форматы (все — реальные файлы компании):
1. «Оценка_металла» / «ННОС» / «ПНОС» — построчные оценки позиций 1С
   (код, наименование, запас, ЕИ, вес кабеля, % выхода, металл) со сверкой итогов.
2. «Таблица по маркам» / «расчет по маркам1» — марка → кг/км металлов.
3. «ВНПЗ медь» / «ВНПЗ алюминий» — марка → «мой вес кабеля кг/км»,
   «вес металла в 1 км» + URL-источники.
Гигиена: #DIV/0!, #VALUE!, «нет такого кабеля», «недостаточно информации» — в лог.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import openpyxl
from sqlalchemy.orm import Session

from ..calc.cable import parse_cable_marking
from ..db.models import CableBrand, ComponentYield
from ..normalize import normalize_name

log = logging.getLogger(__name__)

_BAD_VALUES = {"#DIV/0!", "#VALUE!", "#REF!", "#N/A"}
_BAD_NOTES_RE = re.compile(r"недостаточно информации|нет такого кабеля", re.I)

MATERIALS = {"медь", "медь луженая", "алюминий", "свинец", "оптоволокно",
             "чермет", "латунь", "нержавейка", "16АЦ", "стальная жила"}


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".").replace("\xa0", "")
    if s in _BAD_VALUES or not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bad(v) -> bool:
    return isinstance(v, str) and (v.strip() in _BAD_VALUES or bool(_BAD_NOTES_RE.search(v)))


@dataclass
class CableImportStats:
    rows: int = 0
    imported: int = 0
    skipped: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)


# ------------------------------------------------------- формат «Оценка_металла»


def import_valuation_sheet(session: Session, xlsx_path: str, sheet: str,
                           source: str, source_file_id: int | None = None,
                           block: str = "mot") -> CableImportStats:
    """Построчные оценки позиций (Оценка_металла, ННОС, ПНОС).

    Колонки ищутся по заголовкам: наименование, запас, ЕИ, вес кабеля,
    вес металла (в кабеле), металл, вес металла в 1 км / % выхода.
    """
    stats = CableImportStats()
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    header_i, cols = None, {}
    for i, row in enumerate(rows[:5]):
        c = {}
        for j, cell in enumerate(row):
            h = str(cell or "").strip().lower()
            if not h:
                continue
            if "наимен" in h:
                c["name"] = j
            elif h in ("запас",):
                c["qty"] = j
            elif h in ("еи", "ед. изм.", "единица измерения"):
                c["unit"] = j
            elif "материал" == h:
                c["code"] = j
            elif "вес кабеля" in h and "1" not in h:
                c["gross"] = j
            elif "металл" == h.strip():
                c["material"] = j
            elif "масса металла, кг" in h or ("вес металла в кабеле" in h):
                c["metal_kg"] = j
            elif "вес металла в 1 км" in h or "вес меди в 1 км" in h:
                c["metal_per_km"] = j
            elif "мой вес кабеля" in h:
                c["cable_per_km"] = j
            elif "% выхода" in h or "% металла в кабеле" in h:
                c["percent"] = j
        if "name" in c and ("metal_kg" in c or "percent" in c):
            header_i, cols = i, c
            break
    if header_i is None:
        raise ValueError(f"{sheet}: не найден заголовок таблицы оценки")

    def cell(row, key):
        j = cols.get(key)
        return row[j] if j is not None and j < len(row) else None

    total_metal = 0.0
    for row in rows[header_i + 1:]:
        name = cell(row, "name")
        if not name or not str(name).strip():
            continue
        sname = str(name).strip()
        if re.match(r"итого|всего|суммарная|средневзвеш", sname, re.I):
            break
        stats.rows += 1
        if _bad(name) or any(_bad(v) for v in row if v is not None):
            stats.skipped.append(sname)
            continue
        metal_kg = _num(cell(row, "metal_kg"))
        material = str(cell(row, "material") or "").strip().lower() or None
        if metal_kg is None or not material:
            stats.skipped.append(sname)
            continue
        qty = _num(cell(row, "qty"))
        unit = str(cell(row, "unit") or "").strip() or None
        gross = _num(cell(row, "gross"))
        session.add(ComponentYield(
            item_name=sname,
            item_name_normalized=normalize_name(sname),
            component_name=material,
            material=material,
            quantity=qty,
            unit=unit,
            gross_weight_kg=gross,
            metal_mass_kg=metal_kg,
            block=block,
            source_file_id=source_file_id,
            document_ref=f"{source}:{sheet}:{stats.rows}",
            confidence="medium",
            notes=source,
        ))
        total_metal += metal_kg
        # попутно пополняем помарочный справочник, если есть кг/км
        per_km = _num(cell(row, "metal_per_km"))
        cable_per_km = _num(cell(row, "cable_per_km"))
        if per_km:
            _upsert_brand(session, sname, material, per_km, cable_per_km,
                          source, None, source_file_id)
        stats.imported += 1

    stats.checks["metal_kg_sum"] = round(total_metal, 3)
    session.commit()
    return stats


# ------------------------------------------------------- формат «по маркам»


def import_brand_table(session: Session, xlsx_path: str, sheet: str,
                       source: str, source_file_id: int | None = None) -> CableImportStats:
    """«Таблица по маркам»: Марка | Масса кабеля кг/км | … | Масса меди кг/км."""
    stats = CableImportStats()
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    current_material = "медь"
    cols = None
    for row in rows:
        cells = [str(v or "").strip() for v in row]
        low = [c.lower() for c in cells]
        # переключатель секции («алюм» между блоками)
        if any(c.startswith("алюм") for c in low if c):
            current_material = "алюминий"
        if "марка кабеля" in low:
            cols = {}
            for j, h in enumerate(low):
                if "марка" in h:
                    cols["brand"] = j
                elif "масса кабеля" in h:
                    cols["cable"] = j
                elif "масса меди" in h:
                    cols["metal"] = j
                    current_material = "медь"
                elif "масса алюминия" in h:
                    cols["metal"] = j
                    current_material = "алюминий"
                elif "оболочки" in h:
                    cols["lead"] = j
            continue
        if not cols:
            continue
        brand = row[cols["brand"]] if cols.get("brand") is not None else None
        if not brand or not str(brand).strip():
            continue
        sbrand = str(brand).strip()
        stats.rows += 1
        if _bad(brand) or any(_bad(v) for v in row if isinstance(v, str)):
            stats.skipped.append(sbrand)
            continue
        metal = _num(row[cols["metal"]]) if cols.get("metal") is not None else None
        cable = _num(row[cols["cable"]]) if cols.get("cable") is not None else None
        lead = _num(row[cols["lead"]]) if cols.get("lead") is not None else None
        if metal is None:
            stats.skipped.append(sbrand)
            continue
        mk = parse_cable_marking(sbrand)
        material = current_material
        if mk and mk.conductor == "алюминий":
            material = "алюминий"
        _upsert_brand(session, sbrand, material, metal, cable, source,
                      lead, source_file_id)
        stats.imported += 1
    session.commit()
    return stats


# ------------------------------------------------------- формат «ВНПЗ»


def import_vnpz_sheet(session: Session, xlsx_path: str, sheet: str,
                      material: str, source: str,
                      source_file_id: int | None = None) -> CableImportStats:
    """«ВНПЗ медь/алюминий»: марка, кол-во т, …, мой вес кг/км, металл в 1 км."""
    stats = CableImportStats()
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    cols = None
    for row in rows:
        low = [str(v or "").strip().lower() for v in row]
        if any("марка" in c for c in low):
            cols = {}
            for j, h in enumerate(low):
                if "марка" in h:
                    cols["brand"] = j
                elif "мой вес кабеля" in h:
                    cols["cable"] = j
                elif "в 1 км" in h:
                    cols["metal"] = j
            continue
        if not cols or cols.get("brand") is None:
            continue
        brand = row[cols["brand"]]
        if not brand or not str(brand).strip():
            continue
        sbrand = str(brand).strip()
        stats.rows += 1
        if any(_bad(v) for v in row if isinstance(v, str)):
            stats.skipped.append(sbrand)
            continue
        metal = _num(row[cols["metal"]]) if cols.get("metal") is not None else None
        cable = _num(row[cols["cable"]]) if cols.get("cable") is not None else None
        url = next((str(v) for v in row if isinstance(v, str) and v.startswith("http")), None)
        if metal is None:
            stats.skipped.append(sbrand)
            continue
        _upsert_brand(session, sbrand, material, metal, cable, source, None,
                      source_file_id, url)
        stats.imported += 1
    session.commit()
    return stats


# ------------------------------------------------------- общее


def _upsert_brand(session: Session, brand: str, material: str,
                  metal_kg_per_km: float, cable_kg_per_km: float | None,
                  source: str, lead_kg_per_km: float | None,
                  source_file_id: int | None, url: str | None = None) -> None:
    norm = normalize_name(brand)
    existing = session.query(CableBrand).filter(
        CableBrand.brand_normalized == norm,
        CableBrand.source == source).first()
    rec = existing or CableBrand(brand=brand, brand_normalized=norm, source=source,
                                 source_file_id=source_file_id)
    mk = parse_cable_marking(brand)
    if mk:
        rec.cores_spec = "+".join(f"{n}х{s:g}" for n, s in mk.core_groups)
    if cable_kg_per_km:
        rec.cable_kg_per_km = cable_kg_per_km
    if material == "медь":
        rec.copper_kg_per_km = metal_kg_per_km
    elif material == "медь луженая":
        rec.tinned_copper_kg_per_km = metal_kg_per_km
    elif material == "алюминий":
        rec.aluminum_kg_per_km = metal_kg_per_km
    if lead_kg_per_km:
        rec.lead_kg_per_km = lead_kg_per_km
    if url:
        rec.source_url = url
    rec.confidence = "medium"
    session.add(rec)


def find_brand(session: Session, name: str) -> list[CableBrand]:
    """Поиск марки для позиции КП: точное нормализованное совпадение → вхождение."""
    norm = normalize_name(re.sub(r"^\s*(отходы\s+)?кабел[ья]\s+", "", str(name),
                                 flags=re.I))
    exact = session.query(CableBrand).filter(
        CableBrand.brand_normalized == norm).all()
    if exact:
        return exact
    like = session.query(CableBrand).filter(
        CableBrand.brand_normalized.contains(norm)).all()
    return like
