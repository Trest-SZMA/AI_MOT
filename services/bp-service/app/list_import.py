"""Импорт перечня продавца (xlsx): «Перечень МТР ... к реализации», КП и т.п.

Парсер сам находит строку заголовков (ищет колонки «наименование» и
«кол-во/количество/объём») и вычитывает позиции до конца таблицы.
Формат — как в перечнях Лукойла (лист «база данных» лота 1759):
№ п/п | Наименование | Номенклатурный номер | Ед.изм. | Кол-во | ...
| Место хранения | Комментарии | Примечание.
"""
from __future__ import annotations

from pathlib import Path
import re

from openpyxl import load_workbook

# Ключевые слова для распознавания колонок (по вхождению, без регистра).
# Поддерживаются два формата: перечень/КП продавца (колонки «Наименование» +
# «Кол-во») и внутренняя спецификация БП (колонки «Номенклатура», «Объем
# покупки», «Категория», «Тип покупки/продажи», «Цена продажи» и т.д.).
COLUMN_KEYS = {
    "name": ["наименование", "номенклатура"],
    # «Номер КССС» — так код позиции называется в перечнях РИТЭК (БП 1935),
    # «Номенклатурный номер ДХНО ТМЦ» — в перечнях Лукойла (БП 1785).
    "code": ["номенклатурный номер", "номенкл. номер", "кссс", "код"],
    "unit": ["ед.изм", "ед. изм", "ед.из", "изм", "единица"],
    "qty": ["кол-во", "количество", "объем покупки", "объём покупки",
            "объем", "объём", "тоннаж"],
    "supplier": ["поставщик"],
    "division": ["подразделение", "дивизион"],
    "category": ["категория"],
    "purchase_type": ["тип покупки"],
    "sale_type": ["тип продажи"],
    "purchase_price": ["цена за ед", "цена покупки", "цена закуп"],
    "sale_price": ["цена продажи", "цена реализации"],
    "shipment": ["вид отгрузки"],
    "price_owner": ["ответетственный по ценам", "ответственный по ценам"],
    "pos_buyer": ["покупатель"],
    "purchase_cost": ["стоимость покупки", "стоимость закуп"],
    "sale_volume": ["объем продажи", "объём продажи"],
    "base_point": ["базовый логистический пункт"],
    "distance_km": ["расстояние", "км"],
    "place": ["место хранения", "место хран", "место нахожд", "местонахожд",
              "склад", "адрес"],
    "condition": ["условия хранения", "состояни"],
    # Реквизиты перечня продавца (лист «база данных» книги 1785)
    "balance_price": ["балансовая цена"],
    "balance_cost": ["балансовая стоимость"],
    "tech_doc": ["наличие тех", "техническ", "паспорт"],
    "state_note": ["коментарии к состоянию", "комментарии к состоянию",
                   "к состоянию"],
    "works": ["необходимость дополнительных работ", "дополнительных работ",
              "примечание"],
    "licenses": ["наличие необходимых лицензий", "лицензи"],
    # Период реализации и причина возникновения лома — есть в перечнях
    # (кп1, перечень 68, отШумейко), влияют на план вывоза и обоснование.
    "sale_period": ["предполагаемый период реализации", "период реализации"],
    "origin_reason": ["причина возникновения", "комментарии к состоянию"],
    "dest": ["пункт назначения", "грузополучател", "станция назначения",
             "куда", "назначени"],
    "note": ["коментари", "комментари", "состояни", "примечание"],
}
# Поля формата спецификации БП — по ним выбираем «богатый» лист в книге с
# несколькими вкладками (позиции важнее сводных/черновых листов).
RICH_FIELDS = ("supplier", "division", "category", "purchase_type",
               "sale_type", "sale_price", "purchase_price")
STOP_WORDS = ("итого", "всего")
# Ошибки формул Excel (файл сохранён без пересчёта / со сломанной ссылкой) —
# трактуем как пустую ячейку, а не как данные.
ERROR_VALUES = {"#ref!", "#value!", "#div/0!", "#n/a", "#name?", "#null!",
                "#num!", "#####"}


def _clean(raw) -> str:
    text = str(raw).strip() if raw is not None else ""
    return "" if text.lower() in ERROR_VALUES else text


def _to_float(raw) -> float | None:
    text = str(raw or "").replace(",", ".").replace(" ", "").replace("\xa0", "")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _norm_text(value) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def category_from_text(text: str) -> str | None:
    low = (text or "").lower().replace("ё", "е")
    cat = re.search(r"(?<!\d)(\d{1,2})\s*[аa]\b", low)
    if cat and "металлолом" in low:
        return f"{cat.group(1)}А"
    if "черн" in low and "металлолом" in low:
        return "черный металл"
    if "мед" in low:
        return "медь"
    if "алюмин" in low:
        return "алюминий"
    if "кабел" in low:
        return "кабель"
    if "труб" in low or "нкт" in low:
        return "труба"
    return None


def xls_to_xlsx(src: str | Path, dst: str | Path) -> Path:
    """Старый .xls (Excel 97–2003, в нём приходят перечни Лукойла) → .xlsx с
    теми же листами и значениями: парсеру нужны только значения ячеек и
    порядок строк, стили и формулы не переносятся. Скрытые листы остаются
    скрытыми — detect_metadata их пропускает."""
    import xlrd
    from openpyxl import Workbook
    book = xlrd.open_workbook(str(src), formatting_info=False)
    wb = Workbook()
    wb.remove(wb.active)
    for sh in book.sheets():
        ws = wb.create_sheet(title=(sh.name or "Лист")[:31])
        if sh.visibility:
            ws.sheet_state = "hidden"
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                v = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        v = xlrd.xldate.xldate_as_datetime(v, book.datemode)
                    except (ValueError, OverflowError):
                        pass
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    v = None
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    v = bool(v)
                row.append(v)
            ws.append(row)
    dst = Path(dst)
    wb.save(dst)
    return dst


def detect_metadata(path: str | Path) -> dict[str, str | None]:
    """Извлекает реквизиты перечня из верхних строк книги: продавец, номер."""
    wb = load_workbook(path, data_only=True, read_only=True)
    meta = {"seller_name": None, "tender_ref": None}
    texts: list[str] = []
    # Только видимые листы: в скрытых остаются шаблоны других лотов
    # (БП 1785: скрытые листы ЛЗС давали чужого продавца).
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        for row in ws.iter_rows(min_row=1, max_row=min(20, ws.max_row), values_only=True):
            for value in row:
                text = _norm_text(value)
                if text:
                    texts.append(text)
    wb.close()

    joined = " ".join(texts)
    seller = re.search(
        r"(ООО\s+[\"«]?ЛУКОЙЛ\s*[-–—]\s*Западная\s+Сибирь[\"»]?)",
        joined,
        flags=re.IGNORECASE,
    )
    if seller:
        meta["seller_name"] = seller.group(1).replace("«", "\"").replace("»", "\"")
    else:
        # Кавычки бывают вложенными: ООО "НК "Югранефтепром" к реализации
        # (перечни ЮНП 16.09.2026) — берём до последней кавычки перед
        # «к реализации», не жадно.
        generic = re.search(r"((?:ООО|АО|ПАО|ЗАО|ОАО)\s+[\"«].{2,80}?[\"»])\s+к\s+реализации",
                            joined, flags=re.IGNORECASE)
        if generic:
            meta["seller_name"] = generic.group(1).replace("«", "\"").replace("»", "\"")

    ref = re.search(r"(?:№|N)\s*([А-ЯA-ZЁ0-9/-]{3,})", joined, flags=re.IGNORECASE)
    if ref:
        meta["tender_ref"] = ref.group(1)
    return meta


def detect_market_prices(path: str | Path) -> dict[str, dict]:
    """Ориентиры реализации из КП/сводных листов: категория → руб/тн.

    Ищет строки вида «... по 21500 ...» рядом с наименованием позиции.
    Для старта БП берём консервативный минимум по категории.
    """
    wb = load_workbook(path, data_only=True, read_only=True)
    by_cat: dict[str, list[float]] = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [_norm_text(v) for v in row if _norm_text(v)]
            if not cells:
                continue
            joined = " ".join(cells)
            prices = []
            for raw in re.findall(r"\bпо\s+(\d{4,6})(?:[,.]\d+)?\b", joined,
                                  flags=re.IGNORECASE):
                value = float(raw)
                if 1000 <= value <= 200000:
                    prices.append(value)
            if not prices:
                continue
            label = next((c for c in cells if category_from_text(c)), joined)
            category = category_from_text(label)
            if category:
                by_cat.setdefault(category, []).extend(prices)
    wb.close()
    return {
        cat: {"price": min(values), "samples": len(values),
              "min": min(values), "max": max(values)}
        for cat, values in by_cat.items()
    }


def _match_cols(cells: dict[int, str]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for field, keys in COLUMN_KEYS.items():
        for i, text in cells.items():
            if field not in cols and any(k in text for k in keys):
                cols[field] = i
    return cols


def _find_header(ws) -> tuple[int, dict[str, int]] | None:
    """Строка заголовков и карта колонок {поле: индекс}.

    Поддерживает двухстрочную шапку спецификации (например, группа «ПЛАН» над
    подзаголовками «Тип продажи»/«Цена продажи»): если под найденной строкой
    заголовков идёт не данные, её подписи домешиваются в карту колонок.
    """
    rows = list(ws.iter_rows(min_row=1, max_row=25, values_only=True))
    for idx, row in enumerate(rows):
        cells = {i: str(v).lower().replace("\n", " ")
                 for i, v in enumerate(row) if v is not None}
        cols = _match_cols(cells)
        if "name" in cols and "qty" in cols:
            nxt = rows[idx + 1] if idx + 1 < len(rows) else None
            if nxt is not None:
                qi = cols["qty"]
                below = nxt[qi] if qi < len(nxt) else None
                # Подзаголовок, а не данные, если под «Кол-во» нет числа.
                if _to_float(below) is None:
                    sub = {i: str(v).lower().replace("\n", " ")
                           for i, v in enumerate(nxt) if v is not None}
                    for field, i in _match_cols(sub).items():
                        cols.setdefault(field, i)
            # Книги 2026 г.: блок «ПЛАН» — объединённая шапка без подписей
            # над колонками «тип продажи | цена продажи | выручка».
            if "sale_price" not in cols:
                plan_i = next((i for i, t in cells.items()
                               if t.strip() == "план"), None)
                if plan_i is not None:
                    cols.setdefault("sale_type", plan_i)
                    cols["sale_price"] = plan_i + 1
            return idx + 1, cols
    return None


def _extract(ws, header_row: int, cols: dict[str, int]) -> tuple[list[dict], list[str]]:
    """Позиции из одного листа по известной шапке."""
    warnings: list[str] = []
    # Формат спецификации БП: есть поставщик+тип покупки или цена+категория+тип.
    # Только тогда доверяем богатым полям (у обычного перечня «Категория» может
    # ложно совпасть с «Наименованием» — такие поля не выгружаем).
    is_spec = ("supplier" in cols and "purchase_type" in cols) or (
        "sale_price" in cols and "category" in cols and "purchase_type" in cols)

    items: list[dict] = []
    empty_streak = 0
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        def cell(field):
            i = cols.get(field)
            v = row[i] if i is not None and i < len(row) else None
            return _clean(v)  # ошибки формул (#REF! и т.п.) → пустая строка

        name = cell("name")
        qty_cell = cell("qty")
        # Строка без наименования или без количества — не позиция (сюда попадают
        # блоки под таблицей: «Расчёт процентов», сводные подписи, строки со
        # сломанными ссылками #REF!). Пропускаем тихо; после серии пустых —
        # обрываем чтение. Строки-заготовки спецификации (заполнено
        # подразделение/поставщик, но нет номенклатуры и объёма — БП 1674)
        # серию не наращивают: таблица продолжается ниже.
        if not name or not qty_cell:
            templated = is_spec and (cell("supplier") or cell("division"))
            if not templated:
                empty_streak += 1
                if empty_streak > 5:
                    break
            continue
        empty_streak = 0
        # «ИТОГО/ВСЕГО» может стоять и над данными (как в перечнях Лукойла) —
        # пропускаем сводные строки, не обрывая чтение.
        if any(name.lower().startswith(w) for w in STOP_WORDS):
            continue
        # Числовое «наименование» — не позиция: под таблицей спецификации
        # идут расчётные блоки («Сумма %» и т.п.), где в колонках номенклатуры
        # и объёма оказываются числа (БП 1674: 2 550 000 / 26 машин).
        if re.fullmatch(r"[\d\s.,%-]+", name):
            continue
        qty = _to_float(qty_cell)
        if qty is None:
            warnings.append(f"«{name[:40]}»: количество «{qty_cell}» "
                            "не распознано — строка пропущена.")
            continue
        if qty <= 0:
            continue
        note_parts = [
            cell("note"),
            cell("condition") and f"условия хранения: {cell('condition')}",
            cell("works") and f"работы: {cell('works')}",
            cell("licenses") and f"лицензии: {cell('licenses')}",
        ]
        items.append({
            "name": name,
            "code": cell("code") or None,
            "unit": cell("unit") or "тн",
            "qty": qty,
            "base_point": " ".join(cell("base_point").split()) or None,
            "distance_km": _to_float(cell("distance_km")),
            "place": " ".join(cell("place").split()) or None,
            "dest": " ".join(cell("dest").split()) or None,
            "note": " / ".join(" ".join(p.split()) for p in note_parts if p) or None,
            # Богатые поля формата спецификации (None для обычного перечня).
            "supplier": (" ".join(cell("supplier").split()) or None) if is_spec else None,
            "division": (" ".join(cell("division").split()) or None) if is_spec else None,
            "category": (cell("category") or None) if is_spec else None,
            "purchase_type": (cell("purchase_type").lower() or None) if is_spec else None,
            "sale_type": (cell("sale_type").lower() or None) if is_spec else None,
            "sale_price": _to_float(cell("sale_price")) if is_spec else None,
            "purchase_price": _to_float(cell("purchase_price")) if is_spec else None,
            "purchase_cost": _to_float(cell("purchase_cost")) if is_spec else None,
            "sale_volume": _to_float(cell("sale_volume")) if is_spec else None,
            "balance_price": _to_float(cell("balance_price")),
            "balance_cost": _to_float(cell("balance_cost")),
            "tech_doc": " ".join(cell("tech_doc").split()) or None,
            "storage_conditions": " ".join(cell("condition").split()) or None,
            "condition_note": " ".join(cell("state_note").split()) or None,
            "extra_works": " ".join(cell("works").split()) or None,
            "sale_period": " ".join(cell("sale_period").split()) or None,
            "origin_reason": " ".join(cell("origin_reason").split()) or None,
            "shipment": (cell("shipment") or None) if is_spec else None,
            "price_owner": (cell("price_owner") or None) if is_spec else None,
            "pos_buyer": (cell("pos_buyer") or None) if is_spec else None,
        })
    return items, warnings


def parse_list(path: str | Path) -> tuple[list[dict], list[str]]:
    """→ (позиции, предупреждения).

    Каждая позиция: {name, code, unit, qty, place, note, …} и, для формата
    спецификации, дополнительно supplier/division/category/purchase_type/
    sale_type/sale_price/purchase_price/purchase_cost.
    """
    wb = load_workbook(path, data_only=True)

    # Кандидаты-листы: пропускаем черновики («не готов»), ранжируем по «богатству»
    # (позиции важнее сводных листов). Дубли-варианты одного БП (_лук/_дсп) имеют
    # равный ранг — перебираем по порядку, пока лист не даст позиции: так книга с
    # одним сломанным листом (#REF! в кэше формул) читается со второго, целого.
    # Сценарные листы «(-1000)» (цена минус прогноз) — не базовый вариант:
    # базовый лист без такого суффикса получает приоритет (БП 1674).
    candidates: list[tuple[int, object, int, dict]] = []
    for ws in wb.worksheets:
        if "не готов" in (ws.title or "").lower():
            continue
        found = _find_header(ws)
        if found:
            score = sum(1 for f in RICH_FIELDS if f in found[1]) * 2
            if not re.search(r"\(\s*-\s*\d+", ws.title or ""):
                score += 1                     # базовый сценарий важнее «(-N)»
            candidates.append((score, ws, found[0], found[1]))
    if not candidates:  # запасной проход — вместе с черновыми листами
        for ws in wb.worksheets:
            found = _find_header(ws)
            if found:
                candidates.append((0, ws, found[0], found[1]))
    if not candidates:
        wb.close()
        return [], ["Не найдена строка заголовков: нужны колонки "
                    "«Наименование»/«Номенклатура» и «Кол-во/Объём»."]

    candidates.sort(key=lambda c: -c[0])  # стабильно: при равенстве — первый лист
    last_warnings: list[str] = []
    for _, ws, header_row, cols in candidates:
        items, warnings = _extract(ws, header_row, cols)
        if items:
            wb.close()
            return items, warnings
        last_warnings = warnings or last_warnings
    wb.close()
    return [], last_warnings + ["Позиции не найдены под строкой заголовков "
                                "(проверьте, что файл сохранён без открытого Excel "
                                "и без ошибок #REF!)."]


# ── Импорт готового БП (внутренний Excel-формат МетОптТорг) ──────────

def variant_code(fam: str | None) -> str | None:
    """Код варианта расчёта по имени семейства листов: «..._лук» → 'luk'
    (вариант Лукойл), «..._дсп» → 'bsp' (внутренний базовый расчёт, в
    сервисе — «ДСП»). Комбинированные листы («лук+дсп») кода не имеют."""
    low = (fam or "").lower()
    has_luk = "лук" in low
    has_dsp = "дсп" in low
    if has_luk and not has_dsp:
        return "luk"
    if has_dsp and not has_luk:
        return "bsp"
    return None


def _fam_date(fam: str | None) -> tuple[int, int, int] | None:
    """Дата из имени семейства («23.07.2026_дсп» → (2026, 7, 23)) — по ней
    выбирается свежайшая пара листов: в книгах экономистов старые версии
    расчёта остаются рядом с актуальной («БП_25.09.2024_лук» в БП 1929)."""
    m = re.search(r"(\d{1,2})[._](\d{1,2})[._](\d{2,4})", fam or "")
    if not m:
        return None
    d, mo, y = (int(g) for g in m.groups())
    if y < 100:
        y += 2000
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return (y, mo, d)


def _is_scenario_family(fam: str | None) -> bool:
    """Семейство сценарного листа: «..._лук (-1000)» — сдвиг цены."""
    return bool(re.search(r"\(\s*-\s*\d+", fam or ""))


LISTING_FIELDS = ("balance_price", "balance_cost", "tech_doc",
                  "storage_conditions", "condition_note", "extra_works",
                  # Логистика места отгрузки: адрес хранения МТР, ближайший
                  # базовый логистический пункт и расстояние до него — это
                  # есть только в перечне продавца, а нужно для маршрутов.
                  "place", "base_point", "distance_km",
                  # Код позиции у продавца (КССС / номенклатурный номер):
                  # в расчётном листе его нет, он только в перечне.
                  "code", "sale_period", "origin_reason")


def _enrich_from_listing(wb, positions: list[dict], usable, family) -> None:
    """Дополнение позиций реквизитами листа-перечня продавца по наименованию.

    Позиции берутся с расчётной спецификации (там цены и типы), а условия
    хранения, состояние, документы и балансовая стоимость есть только в
    перечне (лист «перечень» / «база данных»). Совпадение — по
    нормализованному наименованию; при нескольких строках с одним именем
    берётся первая незанятая."""
    if not positions:
        return
    donors: dict[str, list[dict]] = {}
    for ws in wb.worksheets:
        if not usable(ws) or family(ws.title):        # только листы-перечни
            continue
        found = _find_header(ws)
        if not found:
            continue
        rows, _ = _extract(ws, found[0], found[1])
        for r in rows:
            if any(r.get(f) for f in LISTING_FIELDS):
                donors.setdefault(_norm_name(r["name"]), []).append(r)
    if not donors:
        return
    used: set[int] = set()
    for p in positions:
        pool = donors.get(_norm_name(p["name"]))
        if not pool:
            continue
        # Одно и то же наименование лежит в разных местах (БП 1935: труба НКТ
        # 73х5.5 и в Котово, и в Кошках). Донора выбираем по совпадению
        # места хранения, иначе к позиции приедет чужой базовый пункт и
        # чужое расстояние — а по нему считается транспорт.
        place = _norm_name(p.get("division") or p.get("place") or "")
        donor = None
        if place:
            donor = next(
                (d for d in pool if id(d) not in used
                 and _norm_name(d.get("place") or "") == place), None)
            if donor is None:            # частичное совпадение адреса
                donor = next(
                    (d for d in pool if id(d) not in used and d.get("place")
                     and (place in _norm_name(d["place"])
                          or _norm_name(d["place"]) in place)), None)
        if donor is None:
            donor = next((d for d in pool if id(d) not in used), pool[0])
        used.add(id(donor))
        for f in LISTING_FIELDS:
            if p.get(f) is None and donor.get(f) is not None:
                p[f] = donor[f]


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().replace("ё", "е").split())


def _after_colon(text: str) -> str:
    return text.split(":", 1)[1].strip() if ":" in text else ""


# Маркеры блоков P&L → секция статей приложения. «Налоги с ФОТ» закрывает
# подблок персонала: следующие строки снова постоянные.
_SECTION_MARKERS = [
    ("переменные затраты", "Переменные"),
    ("расходы на персонал", "Персонал"),
    ("налоги с фот", "Постоянные"),
    ("постоянные затраты", "Постоянные"),
    ("административные", "Административные"),
    ("прочие затраты", "Прочие"),
    ("операционная прибыль", None),
]


def _collect_cost_lines(sheets_rows: list[list[list]]) -> dict[str, dict]:
    """Метка (столбец 0) → {total (столбец 2), per_t (столбец 3), section}.
    Берутся строки, где хотя бы одна сумма — число. sheets_rows — строки,
    сгруппированные по листам: секция P&L сбрасывается на границе листа,
    чтобы справочные блоки расчётных листов (цены заводов на «v0») не
    считались статьями затрат."""
    out: dict[str, dict] = {}
    for sheet_rows in sheets_rows:
        section: str | None = None
        for row in sheet_rows:
            label = _clean(row[0]) if row else ""
            if not label:
                continue
            key = " ".join(label.lower().replace("ё", "е").split())
            for marker, sec in _SECTION_MARKERS:
                if marker in key:
                    section = sec
                    break
            total = _to_float(_clean(row[2])) if len(row) > 2 else None
            per_t = _to_float(_clean(row[3])) if len(row) > 3 else None
            if total is None and per_t is None:
                continue
            out.setdefault(key, {"total": total, "per_t": per_t,
                                 "section": section})
    return out


def _derive_removal_months(rows_: list[list], cost_lines_: dict,
                           purchase_gross: float | None,
                           rate_pct: float | None) -> float | None:
    """Срок капитала обратным расчётом из строки «Стоимость привлеченного
    капитала» (точнее меток шапки: в БП 1785 метка говорит «1 месяц», а
    капитал посчитан за неделю). Линейное убывание остатка:
    Σ = база × ставка/12 × (m+1)/2 при m ≥ 1; при сроке меньше месяца
    Σ = база × ставка/12 × m (один период). Фолбэк — «Срок вывоза: N»
    из шапки листа."""
    cap = (cost_lines_.get("стоимость привлеченного капитала") or {}).get("total")
    if cap and purchase_gross and rate_pct:
        monthly = purchase_gross * rate_pct / 100.0 / 12.0
        m = 2 * cap / monthly - 1
        if 0.5 <= m <= 24 and abs(m - round(m)) < 0.15:
            return float(round(m))
        m_frac = cap / monthly                     # доля месяца (неделя = 0,25)
        if 0.02 <= m_frac < 1.0 and abs(m_frac * 20 - round(m_frac * 20)) < 0.4:
            return round(m_frac, 2)
    for row in rows_:
        for raw in row:
            mm = re.search(r"срок\s+вывоза[:\s]*(\d+)", _clean(raw).lower())
            if mm:
                return float(mm.group(1))
    return None


def _find_labeled(rows: list[list], keys: list[str],
                  numeric: bool) -> object | None:
    """Значение рядом с меткой: «Метка: значение» в одной ячейке или значение
    в ближайших ячейках справа. Первое совпадение по порядку листов/строк."""
    for row in rows:
        for i, raw in enumerate(row):
            text = _clean(raw)
            low = text.lower()
            if not text or not any(k in low for k in keys):
                continue
            tail = _after_colon(text)
            if tail:
                return _to_float(tail) if numeric else tail
            for j in range(i + 1, min(i + 4, len(row))):
                val = _clean(row[j])
                if not val:
                    continue
                num = _to_float(val)
                if numeric and num is not None:
                    return num
                if not numeric:
                    return val
    return None


def parse_old_bp(path: str | Path) -> dict:
    """Разбор готового БП во внутреннем Excel-формате → позиции, параметры лота,
    статьи затрат (сырые метки→суммы) и контрольные суммы P&L для сверки.

    Статьи затрат отдаются как есть (метка → {total, per_t}); сопоставление с
    статьями приложения делается в вызывающем коде (по DEFAULT_COST_ITEMS).

    Книга экономистов содержит пары листов «БП X» + «v0 X» по семействам:
    актуальные варианты «..._дсп» (внутренний, вариант «ДСП») и «..._лук»
    (Лукойл), старые версии с меньшими датами и сценарные листы «(-N)».
    Базовым становится свежайшее семейство (при равной дате — «дсп»);
    парное семейство другого варианта той же даты и того же тоннажа
    возвращается ключом variant (цены и статьи затрат второго варианта)."""
    metadata = detect_metadata(path)

    wb = load_workbook(path, data_only=True)

    def family(title: str) -> str | None:
        """Семейство листов: 'БП 20.10.25_лук' → '20.10.25 лук'.
        Пары «БП X»+«v0 X»; параметры и статьи затрат нельзя смешивать
        между семействами. Подчёркивания и пробелы взаимозаменяемы:
        «БП_24.12.2025 лук+дсп» и «v0 24.12.2025_лук+дсп» — одна пара
        (БП 1785)."""
        m = re.match(r"^(бп|v\d+)[\s_]+(.+)$", (title or "").strip().lower())
        if not m:
            return None
        return " ".join(m.group(2).replace("_", " ").split())

    def usable(ws) -> bool:
        return (ws.sheet_state == "visible"
                and "не готов" not in (ws.title or "").lower())

    # Семейства → листы (в порядке книги).
    fam_sheets_map: dict[str, list] = {}
    for ws in wb.worksheets:
        if not usable(ws):
            continue
        fam = family(ws.title)
        if fam:
            fam_sheets_map.setdefault(fam, []).append(ws)

    def fam_positions(fam: str) -> list[dict]:
        for w in fam_sheets_map.get(fam, []):
            found = _find_header(w)
            if found:
                its, _ = _extract(w, found[0], found[1])
                if its:
                    return its
        return []

    def weighted_price(pos_list):
        priced = [p for p in pos_list if p.get("sale_price") and p.get("qty")]
        total = sum(p["qty"] for p in priced)
        return (sum(p["sale_price"] * p["qty"] for p in priced) / total
                if total else None)

    # Кандидаты в базовое семейство: несценарные, с позициями и ценами.
    fam_info: dict[str, dict] = {}
    for fam in fam_sheets_map:
        pos = fam_positions(fam)
        fam_info[fam] = {"positions": pos, "qty": sum(p["qty"] for p in pos),
                         "avg": weighted_price(pos), "date": _fam_date(fam),
                         "code": variant_code(fam),
                         "scenario": _is_scenario_family(fam)}
    candidates = [f for f, i in fam_info.items()
                  if not i["scenario"] and i["positions"] and i["avg"]]
    base_family = None
    if candidates:
        # Базовое семейство — первое по порядку книги (актуальный расчёт
        # экономисты держат первым; поздние листы могут быть черновиками).
        first = candidates[0]
        # Парное семейство противоположного варианта той же даты:
        # при паре «лук + дсп» базовым становится «дсп» (внутренний расчёт),
        # «лук» — вторым вариантом.
        pair = next(
            (f for f in candidates
             if f != first and fam_info[f]["code"]
             and fam_info[first]["code"]
             and fam_info[f]["code"] != fam_info[first]["code"]
             and fam_info[f]["date"] == fam_info[first]["date"]), None)
        if pair and fam_info[first]["code"] == "luk":
            base_family = pair
        else:
            base_family = first
    elif fam_sheets_map:
        base_family = next(iter(fam_sheets_map))

    positions = fam_info[base_family]["positions"] if base_family else []
    warnings: list[str] = []
    if not positions:
        # Книга без семейств «БП/v0» (обычный перечень) — прежний путь.
        positions, warnings = parse_list(path)

    # Реквизиты перечня продавца (условия и место хранения, состояние,
    # документы, балансовая стоимость) лежат на отдельном листе-перечне, а
    # позиции берутся со спецификации — связываем их по наименованию.
    _enrich_from_listing(wb, positions, usable, family)

    # Строки для поиска меток: сначала листы базового семейства, затем листы
    # без семейства (сводные «перечень», «Адреса» и т.п.) — метки (ставка,
    # срок вывоза, контрольные суммы) берутся по первому совпадению и должны
    # прийти из базового варианта, а не из посторонних сводных листов.
    # Сценарные и чужие семейства не подмешиваются.
    base_sheets_rows: list[list[list]] = []
    other_rows: list[list] = []
    for ws in wb.worksheets:
        if not usable(ws):
            continue
        fam = family(ws.title)
        if base_family and fam and fam != base_family:
            continue
        sheet_rows = [list(row) for row in ws.iter_rows(values_only=True)]
        if fam:
            base_sheets_rows.append(sheet_rows)
        else:
            other_rows.extend(sheet_rows)
    base_rows = [r for sheet in base_sheets_rows for r in sheet]
    rows = base_rows + other_rows

    def frac_to_pct(v):
        if v is None:
            return None
        return round(v * 100, 2) if abs(v) < 1 else round(v, 2)

    params = {
        "seller_name": metadata.get("seller_name")
                       or (positions[0]["supplier"] if positions and
                           positions[0].get("supplier") else None),
        "division": _find_labeled(rows, ["дивизион"], numeric=False),
        "tender_ref": metadata.get("tender_ref"),
        "capital_rate": frac_to_pct(_find_labeled(rows, ["ставка %"], numeric=True)),
        "removal_months": _find_labeled(rows, ["кол-во месяцев вывоза",
                                               "количество месяцев вывоза"],
                                        numeric=True),
        "contamination_pct": frac_to_pct(
            _find_labeled(rows, ["засор черный лом", "засор чёрный лом"],
                          numeric=True)),
        "shipment_type": _find_labeled(rows, ["вид отгрузки"], numeric=False),
        "relocation": _find_labeled(rows, ["перемещение"], numeric=False),
    }

    # Ставка НДС не записана в файле явно — она сидит в формулах (×1.22).
    # Выводим её из пары «Себестоимость без НДС» / «Себестоимость с НДС».
    cost_net = _find_labeled(rows, ["себестоимость без ндс"], numeric=True)
    cost_gross = _find_labeled(rows, ["себестоимость с ндс"], numeric=True)
    if cost_net and cost_gross and cost_gross > cost_net:
        params["vat_rate"] = round((cost_gross / cost_net - 1) * 100, 1)

    # Доля невозмещённого НДС — из названия статьи «НДС, не возмещенный NN%»
    # (параметр сценария: 1865 и 1674-базовый — 50%, 1674 «минус 1000» — 30%).
    for row in rows:
        for raw in row:
            text = _clean(raw)
            m = re.search(r"ндс[,\s]*не\s*возмещ[её]нный\s+(\d{1,3})\s*%",
                          text.lower())
            if m:
                params["vat_unrecovered_pct"] = float(m.group(1))
                break
        if "vat_unrecovered_pct" in params:
            break

    # Сценарий «минус N» в имени листа (БП 1674: «БП ..._лук (-1000)») —
    # это сдвиг цены реализации: заполняем Δ для сценариев Консервативный/
    # Базовый/Оптимистичный.
    for ws_name in wb.sheetnames:
        m = re.search(r"\(\s*-\s*(\d+)\s*\)", ws_name)
        if m:
            params["scen_price_delta"] = float(m.group(1))
            break

    # Статьи затрат: метка (столбец 0) → {суммарно (столбец 2), на 1 тонну
    # (столбец 3)}. Берём строки, где хотя бы одна сумма — число.
    # Статьи затрат — только из листов базового семейства (посторонние
    # сводные листы «инфо»/«свод» вносят ложные строки); если семейств в
    # книге нет — из всех собранных строк. Попутно отслеживается текущая
    # секция P&L (по маркерам блоков) — по ней нестандартные статьи
    # («Страхование груза» БП 1928) попадают в свою секцию.
    cost_lines = _collect_cost_lines(base_sheets_rows or [rows])

    # Ставка налогов с ФОТ — из пары статей «Налоги с ФОТ» / «Зарплата»
    # (файлы 2025 г. считают 40%, 2026 г. — 49,7%).
    payroll = (cost_lines.get("налоги с фот") or {}).get("total")
    salary = (cost_lines.get("зарплата") or {}).get("total")
    if payroll and salary:
        params["payroll_tax_rate"] = round(payroll / salary * 100, 2)

    # Отсрочка оплаты покупателем («месяц на дебиторку») — из меток книги.
    delay = _find_labeled(rows, ["отсрочка оплаты", "отсрочка платежа",
                                 "дебиторк"], numeric=True)
    if delay is not None and 0 <= delay <= 12:
        params["payment_delay_months"] = delay

    # База капитала может отличаться от закупки: метка «Стоимость закупа
    # с НДС» (БП 1785 — закуп без меди, медь оплачивает покупатель).
    # Записываем, только если метка отличается от базы, которую подставил
    # бы расчёт сам: закупка с НДС (= «Себестоимость с НДС» в нормальной
    # книге; в 1785 эта строка перебита закупом — тогда сумма покупок).
    cap_base = _find_labeled(rows, ["стоимость закупа"], numeric=True)
    lot_sum = sum(p["purchase_cost"] for p in positions
                  if p.get("purchase_cost")) or None
    est_base = (cost_gross if cost_gross and cost_net
                and cost_gross >= cost_net * 0.999 else lot_sum)
    if cap_base and est_base and abs(cap_base - est_base) > 0.5:
        params["capital_base"] = round(cap_base, 2)

    # Срок капитала: обратный расчёт из строки капитала точнее меток шапки
    # (см. _derive_removal_months); метка остаётся фолбэком.
    derived = _derive_removal_months(
        rows, cost_lines,
        params.get("capital_base") or cost_gross or cost_net,
        params.get("capital_rate"))
    if derived is not None:
        params["removal_months"] = derived

    control = {
        "operating_profit": _find_labeled(rows, ["операционная прибыль"],
                                          numeric=True),
        "net_profit": _find_labeled(rows, ["чистая прибыль"], numeric=True),
    }

    # ── Второй вариант расчёта: парное семейство «лук»/«дсп» той же даты ──
    # и того же тоннажа. Возвращается отдельно (variant): цены реализации,
    # статьи затрат и контрольные суммы второго варианта.
    variant: dict | None = None
    base_code = fam_info[base_family]["code"] if base_family else None
    if base_family:
        base_info = fam_info[base_family]
        for fam, info in fam_info.items():
            if (fam == base_family or info["scenario"] or not info["positions"]
                    or info["avg"] is None or info["code"] is None
                    or info["code"] == base_code
                    or info["date"] != base_info["date"]):
                continue
            if base_info["qty"] and \
                    abs(info["qty"] - base_info["qty"]) > 0.01 * base_info["qty"]:
                warnings.append(
                    f"Семейство «{fam}»: другой тоннаж ({info['qty']:,.1f} тн "
                    f"против {base_info['qty']:,.1f}) — вариантом не станет."
                    .replace(",", " "))
                continue
            v_sheets_rows = [[list(r) for r in w.iter_rows(values_only=True)]
                             for w in fam_sheets_map[fam]]
            v_rows = [r for sheet in v_sheets_rows for r in sheet]
            v_cost_lines = _collect_cost_lines(v_sheets_rows)
            # Параметры сделки в листах варианта: могут отличаться от базовых
            # (БП 1929: срок вывоза дсп=4 мес, лук=5 мес).
            v_params: dict[str, float] = {}
            for key, needles, pct in [
                    ("capital_rate", ["ставка %"], True),
                    ("removal_months", ["кол-во месяцев вывоза",
                                        "количество месяцев вывоза"], False),
                    ("contamination_pct", ["засор черный лом",
                                           "засор чёрный лом"], True)]:
                val = _find_labeled(v_rows, needles, numeric=True)
                if pct:
                    val = frac_to_pct(val)
                if val is not None:
                    v_params[key] = val
            for row in v_rows:
                hit = None
                for raw in row:
                    m = re.search(r"ндс[,\s]*не\s*возмещ[её]нный\s+(\d{1,3})\s*%",
                                  _clean(raw).lower())
                    if m:
                        hit = float(m.group(1))
                        break
                if hit is not None:
                    v_params["vat_unrecovered_pct"] = hit
                    break
            v_gross = ((v_cost_lines.get("себестоимость с ндс") or {})
                       .get("total")) or cost_gross or cost_net
            months = _derive_removal_months(
                v_rows, v_cost_lines, v_gross,
                v_params.get("capital_rate") or params.get("capital_rate"))
            if months is not None:
                v_params["removal_months"] = months
            # Стоимость лота в варианте может отличаться (цена закупки
            # по методике Лукойла против внутренней): сумма «Стоимость
            # покупки» позиций, иначе «Себестоимость без НДС».
            v_lot = sum(p["purchase_cost"] for p in info["positions"]
                        if p.get("purchase_cost")) or \
                ((v_cost_lines.get("себестоимость без ндс") or {})
                 .get("total"))
            if v_lot:
                v_params["lot_cost"] = round(v_lot, 2)
            variant = {
                "code": info["code"], "family": fam,
                "positions": info["positions"],
                "cost_lines": v_cost_lines,
                "params": v_params,
                "control": {
                    "operating_profit": _find_labeled(
                        v_rows, ["операционная прибыль"], numeric=True),
                    "net_profit": _find_labeled(
                        v_rows, ["чистая прибыль"], numeric=True),
                },
            }
            break

    # ── Сценарии-варианты: другие семейства листов («лук (-1000)») ─────
    # Для каждого видимого семейства, отличного от базового (и не ставшего
    # вторым вариантом), извлекаем сдвиг цены (разница средневзвешенных цен
    # позиций), порог, долю невозмещённого НДС и контрольную ЧП. Черновики
    # без цен сценариями не становятся.
    def scenario_name(fam: str, base: str) -> str:
        i = 0
        while i < min(len(fam), len(base)) and fam[i] == base[i]:
            i += 1
        # Откат к границе слова: общий префикс «25.04.26_лук» и «28.05.26_дсп»
        # — «2» — не должен съедать цифру даты («8.05.26_дсп»).
        while i > 0 and fam[i - 1] not in " _-":
            i -= 1
        return fam[i:].strip(" _-") or fam

    base_avg = weighted_price(positions)
    base_lot = sum(p["purchase_cost"] for p in positions
                   if p.get("purchase_cost")) or None
    scenarios: list[dict] = []
    seen_fams: set[str] = set()
    if variant:
        seen_fams.add(variant["family"])   # второй вариант — не сценарий
    for ws in wb.worksheets:
        fam = family(ws.title)
        if (not fam or not base_family or fam == base_family or fam in seen_fams
                or ws.sheet_state != "visible"
                or "не готов" in (ws.title or "").lower()):
            continue
        seen_fams.add(fam)
        # Родственные семейства объединяем: пара «БП 24.02.26_лук_жд» +
        # «v0 24.02.26_лук» (суффикс-уточнение только у одного листа) —
        # один сценарий, а не два.
        def related(other: str | None) -> bool:
            if not other:
                return False
            a, b = sorted((fam, other), key=len)
            return other == fam or (b.startswith(a)
                                    and b[len(a):len(a) + 1] in " _-(")
        fam_sheets = [w for w in wb.worksheets if related(family(w.title))]
        for w in fam_sheets:
            f2 = family(w.title)
            if f2:
                seen_fams.add(f2)
        fam_rows = [list(r) for w in fam_sheets
                    for r in w.iter_rows(values_only=True)]
        fam_items: list[dict] = []
        for w in fam_sheets:
            found = _find_header(w)
            if found:
                its, _ = _extract(w, found[0], found[1])
                if its:
                    fam_items = its
                    break
        fam_avg = weighted_price(fam_items)
        if fam_avg is None or base_avg is None:
            warnings.append(f"Лист «{ws.title}»: цены не заполнены — "
                            "сценарий не создан (черновик).")
            continue
        # Другой тоннаж (> ±1%) — это другая сделка или поздняя версия
        # расчёта, а не сценарий текущего лота: сдвигом цены её не
        # воспроизвести (БП 1855: сандибинские листы; БП 1692: версии
        # 16.02–02.03). Пропускаем с предупреждением.
        base_qty = sum(p["qty"] for p in positions)
        fam_qty = sum(p["qty"] for p in fam_items)
        if base_qty and abs(fam_qty - base_qty) > 0.01 * base_qty:
            warnings.append(
                f"Лист «{ws.title}»: другой тоннаж ({fam_qty:,.1f} тн против "
                f"{base_qty:,.1f}) — это отдельный расчёт, не сценарий лота; "
                "пропущен.".replace(",", " "))
            continue
        scen: dict = {
            "name": scenario_name(fam, base_family),
            "price_delta": round(fam_avg - base_avg, 2),
            "control_net_profit": _find_labeled(fam_rows, ["чистая прибыль"],
                                                numeric=True),
            "comment": f"из листа «{ws.title}»",
        }
        fam_lot = sum(p["purchase_cost"] for p in fam_items
                      if p.get("purchase_cost")) or None
        if fam_lot and base_lot and abs(fam_lot - base_lot) > 0.5:
            scen["lot_cost"] = round(fam_lot, 2)
        for raw_row in fam_rows:                       # доля невозм. НДС
            hit = None
            for raw in raw_row:
                m = re.search(r"ндс[,\s]*не\s*возмещ[её]нный\s+(\d{1,3})\s*%",
                              _clean(raw).lower())
                if m:
                    hit = float(m.group(1))
                    break
            if hit is not None:
                if params.get("vat_unrecovered_pct") != hit:
                    scen["vat_unrecovered_pct"] = hit
                break
        for key, needles in [("capital_rate", ["ставка %"]),
                             ("removal_months", ["кол-во месяцев вывоза",
                                                 "количество месяцев вывоза"]),
                             ("contamination_pct", ["засор черный лом",
                                                    "засор чёрный лом"])]:
            v = _find_labeled(fam_rows, needles, numeric=True)
            if key != "removal_months":
                v = frac_to_pct(v)
            if v is not None and params.get(key) is not None \
                    and abs(v - params[key]) > 1e-9:
                scen[key] = v
        scenarios.append(scen)

    wb.close()
    return {"positions": positions, "params": params, "cost_lines": cost_lines,
            "control": control, "warnings": warnings, "scenarios": scenarios,
            "base_code": base_code, "base_family": base_family,
            "variant": variant}


# ───────────────────── Указания продавца по реализации ─────────────────────

# Заголовки столбцов указаний. «БП ДСП»/«БП Лукойл» — перечень №7 книги 1935
# (указания Сергея по вариантам), «ШУМЕЙКО» — перечень 68 (общие указания).
_INSTR_COLS = {
    "bsp": ["бп дсп"],
    "luk": ["бп лукойл"],
    "any": ["шумейко", "указания по реализации", "указание по реализации"],
}


def collect_instructions(path: str | Path) -> list[dict]:
    """Указания по реализации из ВСЕХ листов книги.

    Позиции БП создаются из расчётного листа, а указания живут в перечне —
    отдельном листе с колонками «БП ДСП»/«БП Лукойл»/«ШУМЕЙКО». Раньше они
    терялись, и указания Сергея по 1935 переносили руками (раздел 7 п.2).
    Возвращает [{name, qty, bsp, luk}] — по строкам перечней, без привязки
    к позициям (её делает apply-код по имени номенклатуры)."""
    wb = load_workbook(path, data_only=True, read_only=True)
    out: list[dict] = []
    for ws in wb.worksheets:
        header, cols = None, {}
        for row in ws.iter_rows(min_row=1, max_row=8):
            names = {}
            for c in row:
                if not isinstance(c.value, str):
                    continue
                low = " ".join(c.value.lower().split())
                for key, needles in _INSTR_COLS.items():
                    if any(n in low for n in needles):
                        names[key] = c.column - 1
                if "наименование" in low and "name" not in names:
                    names["name"] = c.column - 1
                if low.startswith(("кол-во", "количество")):
                    names["qty"] = c.column - 1
            if "name" in names and any(k in names for k in ("bsp", "luk", "any")):
                header, cols = row[0].row, names
                break
        if header is None:
            continue
        for row in ws.iter_rows(min_row=header + 1, values_only=True):
            def cell(key):
                i = cols.get(key)
                v = row[i] if i is not None and i < len(row) else None
                return _clean(v)
            name = cell("name")
            if not name:
                continue
            bsp = cell("bsp") or cell("any")
            luk = cell("luk")
            if not bsp and not luk:
                continue
            try:
                qty = float(str(row[cols["qty"]]).replace(",", ".")) \
                    if cols.get("qty") is not None else None
            except (TypeError, ValueError):
                qty = None
            out.append({"name": name, "qty": qty,
                        "bsp": bsp or None, "luk": luk or None})
    wb.close()
    return out
