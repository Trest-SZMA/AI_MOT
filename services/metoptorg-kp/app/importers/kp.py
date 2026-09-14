"""Импорт КП продавца (Excel/CSV/PDF) со всеми коварностями реальных файлов.

- автодетект строки заголовка среди первых ~20 строк (ячейки ≤ 60 симв.);
- данные не обязательно на первом листе — выбираем лист с лучшим набором колонок;
- агрегация сотен строк-лотов по (имя, ед.);
- «Кол-во» в тоннах само является весом;
- фильтр служебных строк (ИТОГО/ВСЕГО/… — «ВСЕГО» бывает ПЕРЕД данными);
- сопоставление со справочником каскадом.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field

import openpyxl
from sqlalchemy.orm import Session

from ..db.models import Item, ItemAlias, KpDocument, KpPosition
from ..normalize import (is_scrap, model_key, normalize_name, tokens,
                         unit_info)

log = logging.getLogger(__name__)

NAME_HINTS = ("наимен", "номенкл", "товар", "позици", "мтр")
NAME_FALLBACK_HINTS = ("тип б/у", "тип продукции")
QTY_HINTS = ("кол-во", "колво", "количество", "кол.")
UNIT_HINTS = ("ед.", "ед ", "единиц")
WEIGHT_HINTS = ("вес", "масса")
PRICE_HINTS = ("цена", "стоимост")

SERVICE_RE = re.compile(
    r"^\s*(итого|всего|общий вес|сверка|источник|примечан|№)\b", re.I)
# строка с итогом файла — её вес нужен для сверки полноты разбора
TOTAL_RE = re.compile(r"^\s*(итого|всего|общий вес)", re.I)


@dataclass
class ParsedRow:
    name: str
    quantity: float | None
    unit: str | None
    weight_kg: float | None
    price: float | None


@dataclass
class KpImportResult:
    positions: int = 0
    lots: int = 0
    total_weight_t: float = 0.0
    sheet: str | None = None
    # итог, заявленный в самом файле (строки ИТОГО/ВСЕГО/Общий вес) — нужен
    # для сверки: расхождение означает, что часть строк не разобрана
    file_total_t: float | None = None
    warnings: list[str] = field(default_factory=list)


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _detect_header(rows: list[list]) -> tuple[int, dict] | None:
    """Строка заголовка = максимум распознанных колонок среди первых ~20 строк."""
    best = None
    for ri, row in enumerate(rows[:20]):
        cols: dict[str, int] = {}
        for ci, cell in enumerate(row):
            if cell is None:
                continue
            h = str(cell).strip().lower()
            if not h or len(h) > 80:
                continue
            if any(k in h for k in NAME_HINTS) and "name" not in cols:
                cols["name"] = ci
            elif any(k in h for k in QTY_HINTS) and "qty" not in cols:
                cols["qty"] = ci
            elif any(h.startswith(k) for k in UNIT_HINTS) and "unit" not in cols:
                cols["unit"] = ci
            elif any(k in h for k in WEIGHT_HINTS) and "weight" not in cols:
                cols["weight"] = ci
                cols["weight_unit"] = 1000.0 if re.search(r"\bт\b|тонн", h) else (
                    1.0 if "кг" in h else 1000.0)
            elif any(k in h for k in PRICE_HINTS) and "price" not in cols:
                cols["price"] = ci
        if "name" not in cols:
            # запасные подсказки — только если основных нет
            for ci, cell in enumerate(row):
                h = str(cell or "").strip().lower()
                if any(k in h for k in NAME_FALLBACK_HINTS):
                    cols["name"] = ci
                    break
        if "name" in cols:
            score = len(cols)
            if best is None or score > best[2]:
                best = (ri, cols, score)
    return (best[0], best[1]) if best else None


def _parse_sheet_rows(rows: list[list]) -> tuple[list[ParsedRow], dict, float | None] | None:
    """→ (позиции, колонки, заявленный в файле итог по весу в кг)."""
    det = _detect_header(rows)
    if det is None:
        return None
    hri, cols = det
    parsed: list[ParsedRow] = []
    file_totals: list[float] = []
    for row in rows[hri + 1:]:
        get = lambda k: (row[cols[k]] if k in cols and cols[k] < len(row) else None)
        # «ИТОГО» может стоять в любой колонке строки (часто в колонке «№»),
        # поэтому ищем маркер по всей строке, а не только в имени
        if any(isinstance(c, str) and TOTAL_RE.match(c) for c in row):
            total = _total_from_row(row, cols)
            if total:
                file_totals.append(total)
            continue
        name = get("name")
        if name is None or not str(name).strip():
            continue
        sname = str(name).strip()
        if SERVICE_RE.match(sname) or sname.isdigit():
            continue
        qty = _num(get("qty"))
        unit = str(get("unit") or "").strip() or None
        weight = _num(get("weight"))
        if weight is not None:
            weight = weight * cols.get("weight_unit", 1000.0)
        price = _num(get("price"))
        # «Кол-во» в тоннах само является весом
        if unit and unit.strip().lower() in ("т", "тн", "тонна") and weight is None:
            weight = (qty or 0.0) * 1000.0 or None
        if qty is None and weight is None:
            continue
        parsed.append(ParsedRow(sname, qty, unit, weight, price))
    # «ВСЕГО» может стоять и перед данными, и после — берём наибольший
    file_total = max(file_totals) if file_totals else None
    return (parsed, cols, file_total) if parsed else None


def _total_from_row(row: list, cols: dict) -> float | None:
    """Вес из строки ИТОГО: сначала колонка веса, иначе колонка количества."""
    def cell(key):
        return row[cols[key]] if key in cols and cols[key] < len(row) else None
    weight = _num(cell("weight"))
    if weight is not None:
        return weight * cols.get("weight_unit", 1000.0)
    qty = _num(cell("qty"))  # у ЛУКОЙЛа «Кол-во» в тоннах и есть вес
    return qty * 1000.0 if qty else None


def parse_kp_file(path: str, filename: str) -> tuple[list[ParsedRow], KpImportResult]:
    """Разбор файла: все листы, выбор лучшего."""
    result = KpImportResult()
    candidates: list[tuple[str, list[ParsedRow], int]] = []
    fn = filename.lower()
    if fn.endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            res = _parse_sheet_rows(rows)
            if res:
                parsed, cols, file_total = res
                candidates.append((ws.title, parsed, len(cols), file_total))
        wb.close()
    elif fn.endswith(".csv"):
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]
        res = _parse_sheet_rows(rows)
        if res:
            candidates.append(("csv", res[0], len(res[1]), res[2]))
    elif fn.endswith(".pdf"):
        candidates = _parse_pdf(path)
    else:
        raise ValueError(f"Неподдерживаемый формат: {filename}")

    if not candidates:
        raise ValueError(
            "Не удалось найти таблицу с колонкой наименования. "
            "Если это скан — нужен OCR.")
    # лучший лист: больше колонок, при равенстве — больше позиций
    candidates.sort(key=lambda c: (c[2], len(c[1])), reverse=True)
    sheet, parsed, _, file_total = candidates[0]
    result.sheet = sheet
    result.file_total_t = file_total / 1000.0 if file_total else None
    return parsed, result


def _parse_pdf(path: str) -> list[tuple[str, list[ParsedRow], int]]:
    import pdfplumber

    all_rows: list[list] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                for t in tables:
                    all_rows.extend(t)
            else:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    parts = re.split(r"\s{2,}", line.strip())
                    if len(parts) >= 2:
                        all_rows.append(parts)
    if not all_rows:
        raise ValueError("Похоже, это скан (нет текстового слоя) — нужен OCR.")
    res = _parse_sheet_rows(all_rows)
    return [("pdf", res[0], len(res[1]), res[2])] if res else []


# ---------------------------------------------------------------- сопоставление


# Индекс модельных ключей строится один раз на пачку позиций КП: перебирать
# 9 тыс. карточек на каждую строку перечня слишком дорого.
_model_index_cache: dict = {"key": None, "index": None}


def _model_index(session: Session) -> dict[str, list[int]]:
    count = session.query(Item).filter(Item.is_trade.is_(True)).count()
    if _model_index_cache["key"] != count:
        index: dict[str, list[int]] = {}
        rows = (session.query(Item.id, Item.name)
                .filter(Item.is_trade.is_(True)).all())
        for iid, name in rows:
            key = model_key(name)
            if len(key) >= 5:  # слишком короткие ключи неспецифичны
                index.setdefault(key, []).append(iid)
        _model_index_cache.update(key=count, index=index)
    return _model_index_cache["index"]


def reset_match_cache() -> None:
    _model_index_cache.update(key=None, index=None)


def _unit_family(unit: str | None) -> str | None:
    info = unit_info(unit)
    return info[0] if info else None


def match_item(session: Session, raw_name: str,
               unit: str | None = None) -> tuple[int | None, str, float]:
    """Каскад: точное → alias → модельный ключ → вхождение → токены → обратное.

    `unit` — единица из перечня: при равных кандидатах выбираем карточку с
    совместимой единицей, иначе штучная позиция садится на весовую карточку
    и расчёт идёт по чужой цене.
    """
    norm = normalize_name(raw_name)
    want = _unit_family(unit)
    want_scrap = is_scrap(raw_name)

    def compatible(rows):
        """Ломовую карточку нельзя подставлять деловой позиции и наоборот.

        «аккумуляторных батарей ТНЖШ-350ВМ-У5» садилось на «Лом аккумуляторных
        батарей (кг)» через вхождение — и изделие оценивалось по цене лома.
        """
        same = [r for r in rows if is_scrap(r.name) == want_scrap]
        return same or []

    def pick(rows):
        """Самое короткое имя (точная модификация), но сперва — по единице."""
        if want:
            same = [r for r in rows if _unit_family(r.unit) == want]
            if same:
                rows = same
        return min(rows, key=lambda i: len(i.name_normalized))

    exact = session.query(Item).filter(Item.name_normalized == norm,
                                       Item.is_trade.is_(True)).all()
    if exact:
        return pick(exact).id, "exact", 1.0
    alias = (session.query(ItemAlias)
             .filter(ItemAlias.name_normalized == norm).first())
    if alias:
        return alias.item_id, "alias", 1.0

    # Модельный ключ: снимает разный порядок слов и пунктуацию внутри марки
    # («Секция насосная 1ЭЦНД5-30, 3м б/у» = «1ЭЦНД5-30 б/у (3 м.)»)
    key = model_key(raw_name)
    if len(key) >= 5:
        matches = _model_index(session).get(key)
        if matches:
            rows = compatible(session.query(Item)
                              .filter(Item.id.in_(matches)).all())
            if rows:
                return pick(rows).id, "model", 0.95

    # вхождение: имя карточки содержит запрос → самое короткое имя
    like = compatible(session.query(Item)
                      .filter(Item.is_trade.is_(True),
                              Item.name_normalized.contains(norm))
                      .order_by(Item.name_normalized).all())
    if like:
        return pick(like).id, "contains", 0.9

    # По токенам: отбрасываем только СЛОВЕСНЫЕ хвостовые токены. Размерные
    # (с цифрами) обязательны — иначе «Задвижка 30с41нж Ду100» находит
    # «Задвижку 30с41нж 50х1.6», то есть другой диаметр и другую цену.
    from sqlalchemy import or_

    all_toks = tokens(raw_name)
    size_toks = [t for t in all_toks if any(ch.isdigit() for ch in t)]
    word_toks = [t for t in all_toks if not any(ch.isdigit() for ch in t)]

    def token_filter(query, toks_):
        for t in toks_:
            # «нкт-73» должен матчить и «нкт 73», и «нкт73»
            variants = {t, t.replace("-", " "), t.replace("-", "")}
            query = query.filter(or_(*[Item.name_normalized.contains(v)
                                       for v in variants]))
        return query

    # Размерные токены НЕ отбрасываем: без них «Отвод 90 Ду200» находит
    # «Трубу 900». Неверное сопоставление хуже отсутствия — оценщик увидит
    # «не сопоставлено» и выберет карточку сам.
    while size_toks or word_toks:
        q = token_filter(
            session.query(Item).filter(Item.is_trade.is_(True)),
            size_toks + word_toks)
        rows = compatible(q.all())
        if rows:
            score = 0.7 if size_toks else 0.5
            return pick(rows).id, "tokens", score
        if not word_toks:
            break
        word_toks = word_toks[:-1]  # снимаем только уточняющие слова

    # Обратное вхождение (имя карточки содержится в имени КП) — источник
    # генериков: «Электродвигатель погружной ПЭД-117» садился на карточку
    # «Электродвигатель» в тоннах. Поэтому карточка обязана содержать все
    # размерные токены запроса.
    all_size = [t for t in tokens(raw_name) if any(ch.isdigit() for ch in t)]
    if len(norm) >= 8:
        for item in session.query(Item).filter(Item.is_trade.is_(True)).all():
            cand = item.name_normalized
            if len(cand) < 8 or cand not in norm:
                continue
            if is_scrap(item.name) != want_scrap:
                continue
            if all(any(v in cand for v in (t, t.replace("-", " "),
                                           t.replace("-", "")))
                   for t in all_size):
                return item.id, "reverse", 0.6
    return None, "none", 0.0


def import_kp(session: Session, path: str, filename: str,
              source_file_id: int | None = None) -> tuple[KpDocument, KpImportResult]:
    parsed, result = parse_kp_file(path, filename)

    # агрегация лотов по (нормализованное имя, ед.)
    groups: dict[tuple[str, str | None], list[ParsedRow]] = {}
    for r in parsed:
        groups.setdefault((normalize_name(r.name), r.unit), []).append(r)

    doc = KpDocument(source_file_id=source_file_id, title=filename,
                     file_total_weight_kg=(result.file_total_t * 1000.0
                                           if result.file_total_t else None))
    session.add(doc)
    session.flush()

    for (_, unit), rows_ in groups.items():
        qty = sum(r.quantity for r in rows_ if r.quantity is not None) or None
        weight = sum(r.weight_kg for r in rows_ if r.weight_kg is not None) or None
        price = next((r.price for r in rows_ if r.price is not None), None)
        item_id, kind, score = match_item(session, rows_[0].name, unit)
        comment = f"объединено лотов: {len(rows_)}" if len(rows_) > 1 else None
        session.add(KpPosition(
            document_id=doc.id,
            raw_name=rows_[0].name,
            quantity=qty,
            unit=unit,
            weight_kg=weight,
            seller_price=price,
            item_id=item_id,
            match_kind=kind,
            match_score=score,
            lots_merged=len(rows_),
            comment=comment,
        ))
        result.positions += 1
        result.lots += len(rows_)
        result.total_weight_t += (weight or 0.0) / 1000.0
    if result.file_total_t:
        diff = abs(result.total_weight_t - result.file_total_t)
        if diff / result.file_total_t > 0.005:  # допуск 0,5%
            result.warnings.append(
                f"расхождение с ИТОГО файла: разобрано "
                f"{result.total_weight_t:,.3f} т против {result.file_total_t:,.3f} т "
                f"— проверьте, все ли строки распознаны")
    session.commit()
    return doc, result
