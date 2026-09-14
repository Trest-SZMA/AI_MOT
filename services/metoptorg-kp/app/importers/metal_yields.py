"""Импорт файлов «Выходы металлов ЭПУ Сервис» (3 подформата) + профили семейств.

Подформаты:
1. Секционный (41–51): секции «Перечень № N», марки в заголовке секции,
   таблицы могут стоять бок о бок.
2. Бок-о-бок с количеством (39–40): «№ / Наименование МТР / Ед. изм. / Кол-во / марки»
   × два перечня на одном листе → прямой кг/шт.
3. Плоский (25–28): «МТР перечня № N» + колонки марок.

Детект xlsx-«скана»: лист без значимых ячеек + xl/media/*.emf → честная ошибка.
"""
from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field

import openpyxl
from sqlalchemy.orm import Session

from ..db.models import ComponentYield, ExpertMetalProfile
from ..normalize import classify_family, grade_header_material, normalize_name

log = logging.getLogger(__name__)

_LIST_RE = re.compile(r"переч[а-яё]*\s*№?\s*(\d+)", re.I)


class XlsxScanError(Exception):
    """Таблица вставлена картинкой — нужен OCR."""


@dataclass
class YieldImportStats:
    batches: int = 0
    yields: int = 0
    lists: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def detect_xlsx_scan(xlsx_path: str) -> bool:
    """Все листы пустые, но в архиве есть изображения → xlsx-«скан»."""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    has_data = False
    for ws in wb.worksheets:
        for row in ws.iter_rows(max_row=50, values_only=True):
            if any(v is not None and str(v).strip() for v in row):
                has_data = True
                break
        if has_data:
            break
    wb.close()
    if has_data:
        return False
    with zipfile.ZipFile(xlsx_path) as z:
        return any(n.startswith("xl/media/") for n in z.namelist())


def import_yields_file(session: Session, xlsx_path: str, source: str,
                       source_file_id: int | None = None,
                       block: str = "expert") -> YieldImportStats:
    """Универсальный импортёр: сам определяет подформат по строкам-заголовкам."""
    if detect_xlsx_scan(xlsx_path):
        raise XlsxScanError(
            "Похоже, таблицы вставлены в Excel картинками (xl/media). "
            "Нужен OCR — файл помечен «требует проверки».")

    stats = YieldImportStats()
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        _parse_sheet(session, rows, source, source_file_id, block, stats)
    wb.close()
    session.commit()
    return stats


def _parse_sheet(session, rows, source, source_file_id, block,
                 stats: YieldImportStats) -> None:
    """Разбор листа: находим сегменты (перечень, колонки марок) и строки данных."""
    ncols = max((len(r) for r in rows), default=0)

    # 1. Найти все «якоря»: ячейки «Перечень N» и строки заголовков с марками
    current_list_by_col: dict[int, str] = {}
    # активные сегменты: list of dict(name_col, qty_col, unit_col, grade_cols, list_no)
    segments: list[dict] = []

    for ri, row in enumerate(rows):
        texts = [(ci, str(v).strip()) for ci, v in enumerate(row)
                 if v is not None and str(v).strip()]
        if not texts:
            continue

        # «Перечень № N» — привязка по колонке
        for ci, t in texts:
            m = _LIST_RE.search(t)
            if m and len(t) < 40:
                current_list_by_col[ci] = m.group(1)
                if m.group(1) not in stats.lists:
                    stats.lists.append(m.group(1))

        # строка с марками лома → новый сегмент(ы)
        grade_cells = [(ci, grade_header_material(t)) for ci, t in texts
                       if grade_header_material(t)]
        if grade_cells and len(grade_cells) >= 1 and all(
                len(t) <= 20 for _, t in texts if grade_header_material(t)):
            # ищем в этой же строке колонки «Наименование», «Кол-во», «Ед.»
            name_cols = [ci for ci, t in texts if "наимен" in t.lower()
                         or "мтр перечня" in t.lower()]
            qty_cols = [ci for ci, t in texts if "кол-во" in t.lower()]
            unit_cols = [ci for ci, t in texts if "ед" in t.lower().split(".")[0][:2]
                         and "изм" in t.lower()]
            # сегментация «бок о бок»: режем по колонкам наименований
            if not name_cols:
                # секционный формат: имя в колонке A (или левее первой марки)
                first_grade = min(ci for ci, _ in grade_cells)
                name_cols = [0] if first_grade > 0 else []
            segments = _split_segments(name_cols, qty_cols, unit_cols,
                                       grade_cells, current_list_by_col, ncols)
            continue

        if not segments:
            continue

        # строка данных
        for seg in segments:
            name_v = row[seg["name_col"]] if seg["name_col"] < len(row) else None
            if name_v is None or not str(name_v).strip():
                continue
            sname = str(name_v).strip()
            if _LIST_RE.search(sname) and len(sname) < 40:
                continue
            if re.match(r"итого|всего|№|общий вес|сверка|источник|примечан",
                        sname, re.I):
                continue
            if sname.isdigit():
                continue
            grade_vals = []
            for ci, material in seg["grade_cols"]:
                v = row[ci] if ci < len(row) else None
                if isinstance(v, (int, float)) and v:
                    grade_vals.append((material, float(v)))
            if not grade_vals:
                continue
            qty = None
            if seg.get("qty_col") is not None and seg["qty_col"] < len(row):
                qv = row[seg["qty_col"]]
                if isinstance(qv, (int, float)):
                    qty = float(qv)
            unit = None
            if seg.get("unit_col") is not None and seg["unit_col"] < len(row):
                uv = row[seg["unit_col"]]
                unit = str(uv).strip() if uv else None
            list_no = seg.get("list_no")
            stats.batches += 1
            ref = f"{source}:{list_no or '?'}:{stats.batches}"
            for material, mass_t in grade_vals:
                session.add(ComponentYield(
                    item_name=sname,
                    item_name_normalized=normalize_name(sname),
                    component_name=material,
                    material=material,
                    quantity=qty,
                    unit=unit,
                    metal_mass_kg=mass_t * 1000.0,
                    block=block,
                    source_file_id=source_file_id,
                    document_ref=ref,
                    confidence="medium",
                    notes=f"перечень {list_no}" if list_no else source,
                ))
                stats.yields += 1


def _split_segments(name_cols, qty_cols, unit_cols, grade_cells,
                    current_list_by_col, ncols) -> list[dict]:
    """Режем строку марок на сегменты «бок о бок» по колонкам наименований."""
    if not name_cols:
        return [{
            "name_col": 0,
            "qty_col": qty_cols[0] if qty_cols else None,
            "unit_col": unit_cols[0] if unit_cols else None,
            "grade_cols": grade_cells,
            "list_no": _nearest_list(0, current_list_by_col),
        }]
    segments = []
    bounds = sorted(name_cols) + [ncols + 1]
    for k, nc in enumerate(sorted(name_cols)):
        hi = bounds[k + 1]
        segments.append({
            "name_col": nc,
            "qty_col": next((c for c in qty_cols if nc < c < hi), None),
            "unit_col": next((c for c in unit_cols if nc < c < hi), None),
            "grade_cols": [(c, m) for c, m in grade_cells if nc < c < hi],
            "list_no": _nearest_list(nc, current_list_by_col),
        })
    return [s for s in segments if s["grade_cols"]]


def _nearest_list(col: int, list_by_col: dict[int, str]) -> str | None:
    if not list_by_col:
        return None
    best = min(list_by_col.keys(), key=lambda c: abs(c - col))
    return list_by_col[best]


# ---------------------------------------------------------------- профили


def rebuild_profiles(session: Session) -> int:
    """Пересборка статистических профилей семейств из component_yields.

    кг/шт — только по штучным партиям (средневзвешенно, min–max);
    доли — от gross-массы, если известна, иначе от суммы выходов.
    """
    session.query(ExpertMetalProfile).delete()
    yields = session.query(ComponentYield).all()

    # группировка партий: document_ref → строки
    batches: dict[str, list[ComponentYield]] = {}
    for y in yields:
        batches.setdefault(y.document_ref or f"row-{y.id}", []).append(y)

    fam_mat: dict[tuple[str, str], dict] = {}
    for ref, rows_ in batches.items():
        name = rows_[0].item_name or ""
        family = classify_family(name)
        if not family:
            continue
        qty = rows_[0].quantity
        unit = (rows_[0].unit or "").lower()
        is_piece = qty and unit.startswith("шт")
        gross = rows_[0].gross_weight_kg
        total_metal = sum(r.metal_mass_kg for r in rows_)
        base_mass = gross or total_metal
        for r in rows_:
            key = (family, r.material)
            d = fam_mat.setdefault(key, {
                "kg": 0.0, "units": 0.0, "batches": 0,
                "min": None, "max": None, "mass_kg": 0.0, "base_kg": 0.0})
            d["batches"] += 1
            d["mass_kg"] += r.metal_mass_kg
            d["base_kg"] += base_mass / len(rows_) if base_mass else 0.0
            if is_piece:
                per_unit = r.metal_mass_kg / qty
                d["kg"] += r.metal_mass_kg
                d["units"] += qty
                d["min"] = per_unit if d["min"] is None else min(d["min"], per_unit)
                d["max"] = per_unit if d["max"] is None else max(d["max"], per_unit)

    # доля от общей базы семейства
    fam_base: dict[str, float] = {}
    for (family, _), d in fam_mat.items():
        fam_base[family] = fam_base.get(family, 0.0) + d["mass_kg"]

    count = 0
    for (family, material), d in fam_mat.items():
        kg_per_unit = d["kg"] / d["units"] if d["units"] else None
        percent = d["mass_kg"] / fam_base[family] if fam_base.get(family) else None
        session.add(ExpertMetalProfile(
            family=family,
            material=material,
            kg_per_unit=kg_per_unit,
            kg_per_unit_min=d["min"],
            kg_per_unit_max=d["max"],
            percent_of_mass=percent,
            batches=d["batches"],
            units=d["units"] or None,
            mass_t=d["mass_kg"] / 1000.0,
        ))
        count += 1
    session.commit()
    return count
