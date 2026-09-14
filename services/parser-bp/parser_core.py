# -*- coding: utf-8 -*-
"""
Ядро парсера бизнес-планов (БП).

Ищет на листах книг Excel блок "Расчет процентов" и сравнивает два способа
определения количества месяцев вывоза:
  1) по строкам  — число строк "Привлеченный капитал за N месяц", у которых
                   проставлен порядковый номер месяца (столбец "Порядковый номер...");
  2) по ячейке   — значение строки "Кол-во месяцев вывоза".

Если значения совпадают — строка отчёта помечается зелёным, если расходятся — красным
и заполняется столбец с описанием расхождения.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Iterable, Iterator

import openpyxl

# ---------------------------------------------------------------------------
# Настройки поиска
# ---------------------------------------------------------------------------

SUPPORTED_EXT = (".xlsx", ".xlsm")
LEGACY_EXT = (".xls",)

ANCHOR_BLOCK = "расчет процентов"
LABEL_MONTHS_CELL = "кол-во месяцев вывоза"
LABEL_MONTHS_YEAR = "кол-во месяцев в году"
LABEL_CAPITAL = "привлеченный капитал"
LABEL_ORDER_NO = "порядковый номер"
LABEL_SUM_PCT = "сумма %"
LABEL_PURCHASE = "стоимость закупа"
LABEL_RATE = "ставка %"
LABEL_PROFIT = "чистая прибыл"      # «Чистая прибыль» — итоговая строка расчёта
LABEL_REVENUE = "выручка без ндс"    # «Выручка без НДС» — план выручки по версии
# ⚠️ LABEL_REVENUE добавлен 25.08.2026 для сервиса реализации (вкладка «Версии
# БП»): выручка берётся тем же правилом, что и прибыль, — из столбца «Суммарно»,
# последняя подходящая строка листа. Копия патча — в репозитории сервиса
# (deploy/parser-bp-revenue.patch.md).
LABEL_TOTAL_COL = "суммарно"        # шапка столбца с итоговой суммой

# сколько строк/столбцов от якоря просматриваем
MAX_BLOCK_ROWS = 80
MAX_SCAN_COLS = 30
MAX_SCAN_ROWS = 400


def _norm(value) -> str:
    """Нормализует текст ячейки: нижний регистр, ё→е, схлопнутые пробелы."""
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def _as_number(value):
    """Возвращает число из ячейки или None (пустая строка, текст, пробел — None)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Результаты
# ---------------------------------------------------------------------------


@dataclass
class BlockResult:
    """Один найденный блок 'Расчет процентов' на одном листе."""

    bp_name: str = ""            # "БП 1545_23.05.2025 | v0 21.05.2025_лук"
    file_name: str = ""
    sheet_name: str = ""
    months_by_rows: int | None = None
    months_by_cell: float | None = None
    status: str = ""             # "ОК" / "Расхождение" / "Не определено"
    mismatch: str = ""           # описание расхождения
    block_cell: str = ""         # координата якоря, напр. "A11"
    rows_detail: str = ""        # какие строки посчитаны, напр. "16, 17"
    net_profit: float | None = None   # итоговая «Чистая прибыль» столбца «Суммарно»
    profit_sheet: str = ""            # с какого листа она взята
    revenue: float | None = None      # «Выручка без НДС» столбца «Суммарно»
    revenue_sheet: str = ""           # с какого листа взята выручка
    plan_items: list = field(default_factory=list)   # плановые позиции листа
    has_lukoil: str = ""              # «да» / «—»
    has_dsp: str = ""
    hidden_sheets: str = ""           # «нет» или «да: имена»
    file_path: str = ""

    def as_row(self) -> dict:
        return asdict(self)


@dataclass
class ScanReport:
    rows: list[BlockResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (файл, причина)
    files_scanned: int = 0
    sheets_scanned: int = 0


# ---------------------------------------------------------------------------
# Разбор имени файла
# ---------------------------------------------------------------------------

# "1545_23.05.2025_v0_Лукойл..."  или  "П-Ф 1581_01.07.2025_v0_..."
_RE_NUM_DATE = re.compile(r"(\d{2,6})\s*_\s*(\d{2}\.\d{2}\.\d{2,4})")


def make_bp_title(file_name: str) -> str:
    """'1545_23.05.2025_v0_Лукойл...' -> 'БП 1545_23.05.2025'."""
    stem = os.path.splitext(file_name)[0]
    m = _RE_NUM_DATE.search(stem)
    if m:
        prefix = stem[: m.start()].strip(" _-")
        title = f"БП {m.group(1)}_{m.group(2)}"
        if prefix:
            title = f"{prefix} {title}"
        return title
    return f"БП {stem}"


# ---------------------------------------------------------------------------
# Поиск блока на листе
# ---------------------------------------------------------------------------


def _find_label_row(grid, start_row, end_row, label, max_col):
    """Ищет строку, чья ячейка начинается с label. Возвращает (row, col) или None."""
    for r in range(start_row, end_row + 1):
        for c in range(1, max_col + 1):
            if _norm(grid[r][c]).startswith(label):
                return r, c
    return None


def _value_right_of(grid, row, col, max_col):
    """Первое числовое значение справа от подписи."""
    for c in range(col + 1, max_col + 1):
        num = _as_number(grid[row][c])
        if num is not None:
            return num
    return None


def _read_grid(ws):
    """
    Читает начало листа в матрицу [row][col] с индексами от 1.

    Не полагается на ws.max_row/ws.max_column: в режиме read_only они берутся
    из служебного тега размерности и у части файлов завышены или отсутствуют.
    Возвращает (grid, max_row, max_col) — фактически прочитанные границы.
    """
    grid = [[None] * (MAX_SCAN_COLS + 2)]
    max_row = 0
    max_col = 0
    for r, row in enumerate(
        ws.iter_rows(min_row=1, max_row=MAX_SCAN_ROWS, max_col=MAX_SCAN_COLS, values_only=True),
        start=1,
    ):
        target = [None] * (MAX_SCAN_COLS + 2)
        for c, value in enumerate(row[:MAX_SCAN_COLS], start=1):
            if value is not None:
                target[c] = value
                if c > max_col:
                    max_col = c
        grid.append(target)
        max_row = r
    return grid, max_row, max_col


def _parse_block(grid, anchor_row, anchor_col, max_row, max_col):
    """
    Разбирает блок 'Расчет процентов', начинающийся со строки anchor_row.
    Возвращает (months_by_rows, months_by_cell, rows_detail) или None.
    """
    end_row = min(anchor_row + MAX_BLOCK_ROWS, max_row)

    # --- столбец "Порядковый номер месяца по убыванию" (заголовок в шапке блока)
    order_col = None
    for r in range(anchor_row, min(anchor_row + 6, max_row) + 1):
        for c in range(1, max_col + 1):
            if LABEL_ORDER_NO in _norm(grid[r][c]):
                order_col = c
                break
        if order_col:
            break

    # --- строка "Кол-во месяцев вывоза"
    months_by_cell = None
    found = _find_label_row(grid, anchor_row, end_row, LABEL_MONTHS_CELL, max_col)
    if found:
        months_by_cell = _value_right_of(grid, found[0], found[1], max_col)

    # --- строки "Привлеченный капитал за N месяц"
    capital_rows = []
    for r in range(anchor_row, end_row + 1):
        for c in range(1, max_col + 1):
            if _norm(grid[r][c]).startswith(LABEL_CAPITAL):
                capital_rows.append((r, c))
                break

    if not capital_rows and months_by_cell is None:
        return None

    # Если столбец с порядковым номером не нашли по заголовку — берём третий
    # столбец справа от подписи (B=капитал, C=сумма %, D=порядковый номер).
    if order_col is None and capital_rows:
        candidate = capital_rows[0][1] + 3
        order_col = candidate if candidate <= max_col else None

    # Строки блока могут продолжаться без подписи (подпись есть не у всех строк),
    # поэтому досчитываем непрерывный «хвост» до строки "Сумма %".
    scan_rows = [r for r, _ in capital_rows]
    if scan_rows:
        r = max(scan_rows) + 1
        label_col = capital_rows[0][1]
        while r <= end_row:
            label = _norm(grid[r][label_col])
            if label.startswith(LABEL_SUM_PCT):
                break
            if label and not label.startswith(LABEL_CAPITAL):
                break
            scan_rows.append(r)
            r += 1

    counted = []
    if order_col is not None:
        for r in scan_rows:
            num = _as_number(grid[r][order_col])
            if num is not None and num > 0:
                counted.append(r)

    # Сколько строк блока вообще заполнено деньгами. Нужно, чтобы отличить
    # реальное расхождение от незаполненного шаблона, где капитал везде 0.
    capital_col = capital_rows[0][1] + 1 if capital_rows else None
    filled = 0
    if capital_col is not None and capital_col <= max_col:
        for r in scan_rows:
            num = _as_number(grid[r][capital_col])
            if num:
                filled += 1

    months_by_rows = len(counted) if order_col is not None else None
    rows_detail = ", ".join(str(r) for r in counted)
    return months_by_rows, months_by_cell, rows_detail, filled


def find_net_profit(grid, max_row, max_col):
    """
    Итоговая «Чистая прибыль» листа или None.

    Берём значение из столбца под шапкой «Суммарно» — именно она содержит сумму
    по всему БП. Если такой шапки на листе нет, значение не берём вовсе: строка
    «Чистая прибыль» встречается ещё и на аналитических листах вроде
    «анализ фин ЗС НКТ», где числа означают совсем другое (млн руб., руб/тн).

    Если строк «Чистая прибыль» несколько, берём последнюю — она итоговая.
    """
    total_col = None
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            if _norm(grid[r][c]).startswith(LABEL_TOTAL_COL):
                total_col = c
                break
        if total_col:
            break
    if total_col is None:
        return None

    value = None
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            if _norm(grid[r][c]).startswith(LABEL_PROFIT):
                num = _as_number(grid[r][total_col])
                if num is not None:
                    value = num          # перезаписываем: нужна последняя строка
                break
    return value


def find_revenue(grid, max_row, max_col):
    """Итоговая «Выручка без НДС» листа или None — тем же правилом, что
    find_net_profit: столбец «Суммарно», последняя подходящая строка."""
    total_col = None
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            if _norm(grid[r][c]).startswith(LABEL_TOTAL_COL):
                total_col = c
                break
        if total_col:
            break
    if total_col is None:
        return None
    value = None
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            if _norm(grid[r][c]).startswith(LABEL_REVENUE):
                num = _as_number(grid[r][total_col])
                if num is not None:
                    value = num
                break
    return value


PLAN_HDR_NOM = "номенклатура"
PLAN_HDR_VOL = ("объем покупки", "объём покупки")


def find_plan_items(grid, max_row, max_col):
    """Плановые позиции листа расчёта -> список словарей.

    Та самая таблица, о которой говорил финдир: «в Excel написано, как у
    Лукойла — номенклатура текстом и место хранения, а рядом объём». Шапка
    одинакова во всех проверенных файлах 2021-2026:
      Поставщик | Подразделение | Номенклатура (состав лота по спецификации) |
      Категория лома | Тип покупки | Ед. изм | Объем покупки | Цена за ед.изм |
      Стоимость покупки без НДС | Объем продажи | ...
    Ищем строку шапки по паре «номенклатура» + «объем покупки», дальше читаем
    строки до конца таблицы.

    ВАЖНО:
    * строку «ИТОГО» пропускаем — это сумма, и в списке позиций она удвоила бы
      плановый объём;
    * подразделение пишут только в первой строке группы (объединённые ячейки),
      поэтому тянем последнее непустое значение вниз, иначе у позиций 2..N
      место хранения потерялось бы;
    * не больше 300 позиций с листа — защита от простыни на тысячи строк.
    """
    hdr_row = None
    hdr = []
    limit = min(max_row, 40)
    for r in range(1, limit + 1):
        cells = [_norm(grid[r][c]) for c in range(1, max_col + 1)]
        has_nom = any(PLAN_HDR_NOM in c for c in cells)
        has_vol = any(any(v in c for v in PLAN_HDR_VOL) for c in cells)
        if has_nom and has_vol:
            hdr_row = r
            hdr = cells
            break
    if hdr_row is None:
        return []

    def col(*keys):
        for i, name in enumerate(hdr):
            if not name:
                continue
            for k in keys:
                if k in name:
                    return i + 1
        return None

    c_sup = col("поставщик")
    c_div = col("подразделение")
    c_nom = col(PLAN_HDR_NOM)
    c_cat = col("категория")
    c_unit = col("ед. изм", "ед.изм", "ед изм")
    c_vol = col("объем покупки", "объём покупки")
    c_price = col("цена за ед")
    c_cost = col("стоимость покупки")
    if not c_nom or not c_vol:
        return []

    out = []
    sup = ""
    div = ""
    empty = 0
    for r in range(hdr_row + 1, max_row + 1):
        nom = str(grid[r][c_nom] or "").strip()
        vol = _as_number(grid[r][c_vol])
        if c_sup:
            v = str(grid[r][c_sup] or "").strip()
            if v:
                sup = v
        if c_div:
            v = str(grid[r][c_div] or "").strip()
            if v:
                div = v
        if not nom and vol is None:
            empty += 1
            if empty >= 6 and out:
                break
            continue
        empty = 0
        if _norm(nom).startswith("итого"):
            continue
        if not nom or vol is None:
            continue
        price = _as_number(grid[r][c_price]) if c_price else None
        cost = _as_number(grid[r][c_cost]) if c_cost else None
        out.append({
            "supplier": sup[:120],
            "division": div[:120],
            "nomenclature": nom[:160],
            "category": (str(grid[r][c_cat] or "").strip()[:60] if c_cat else ""),
            "unit": (str(grid[r][c_unit] or "").strip()[:20] if c_unit else ""),
            "volume": round(vol, 3),
            "price": (round(price, 2) if price is not None else None),
            "cost": (round(cost, 2) if cost is not None else None),
        })
        if len(out) >= 300:
            break
    return out


def find_list_items(grid, max_row, max_col):
    """Позиции из ПЕРЕЧНЯ Лукойла («Перечень № 1 НВО МТР»): шапка содержит
    «наименование» и «вес». Одинаковые ТМЦ в одном регионе складываются —
    перечень идёт по местам вывоза (АЗС), а план сравнивают по номенклатуре.
    Запасной источник для версий со сломанной таблицей (#REF!)."""
    hdr_row, hdr = None, []
    for r in range(1, min(max_row, 15) + 1):
        cells = [_norm(grid[r][c]) for c in range(1, max_col + 1)]
        if any("наименование" in c for c in cells) and any(c.startswith("вес") for c in cells):
            hdr_row, hdr = r, cells
            break
    if hdr_row is None:
        return []

    def col(*keys):
        for i, name in enumerate(hdr):
            for k in keys:
                if name and k in name:
                    return i + 1
        return None

    c_nom, c_w = col("наименование"), col("вес")
    c_reg, c_pl = col("регион"), col("мес")
    if not c_nom or not c_w:
        return []
    agg = {}
    for r in range(hdr_row + 1, max_row + 1):
        nom = str(grid[r][c_nom] or "").strip()
        w = _as_number(grid[r][c_w])
        if not nom or w is None or _norm(nom).startswith("итого"):
            continue
        reg = str(grid[r][c_reg] or "").strip() if c_reg else ""
        key = (nom[:160], reg[:120])
        a = agg.setdefault(key, {"supplier": "", "division": reg[:120],
                                 "nomenclature": nom[:160], "category": "",
                                 "unit": "т", "volume": 0.0, "price": None,
                                 "cost": None, "src": "перечень"})
        a["volume"] = round(a["volume"] + w, 3)
    return list(agg.values())[:300]


def sheet_key(title: str) -> str:
    """
    Ключ для сопоставления парных листов: «БП_23.08.2022_лук» и «v0 23.08.2022_лук».

    Блок «Расчет процентов» лежит на листе v0, а «Чистая прибыль» — на парном
    листе БП, поэтому их нужно связывать по общей части названия.
    """
    text = _norm(title)
    text = re.sub(r"^(бп|v\s*\d+)[\s_.:-]*", "", text)
    return re.sub(r"[^a-zа-я0-9]", "", text)


def _grid_anchors(grid, max_row, max_col) -> Iterator[tuple[int, int]]:
    """Координаты якорей блоков 'Расчет процентов' в уже прочитанной сетке."""
    if not max_row or not max_col:
        return

    anchors = []
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            if ANCHOR_BLOCK in _norm(grid[r][c]):
                anchors.append((r, c))
                break

    # запасной вариант: блок без заголовка, но со строкой "Кол-во месяцев вывоза"
    if not anchors:
        for r in range(1, max_row + 1):
            for c in range(1, max_col + 1):
                if _norm(grid[r][c]).startswith(LABEL_MONTHS_CELL):
                    anchors.append((max(1, r - 4), c))
                    break

    yield from anchors


def scan_sheet_grid(grid, max_row, max_col, sheet_title, file_name, file_path) -> list[BlockResult]:
    """Блоки одного листа. Сетка уже прочитана — второй раз лист не читаем."""
    results = []
    for anchor_row, anchor_col in _grid_anchors(grid, max_row, max_col):
        parsed = _parse_block(grid, anchor_row, anchor_col, max_row, max_col)
        if parsed is None:
            continue
        months_by_rows, months_by_cell, rows_detail, filled = parsed

        res = BlockResult(
            bp_name=f"{make_bp_title(file_name)} | {sheet_title}",
            file_name=file_name,
            sheet_name=sheet_title,
            months_by_rows=months_by_rows,
            months_by_cell=months_by_cell,
            block_cell=f"{openpyxl.utils.get_column_letter(anchor_col)}{anchor_row}",
            rows_detail=rows_detail,
            file_path=file_path,
        )

        if months_by_rows is None or months_by_cell is None:
            res.status = "Не определено"
            missing = []
            if months_by_rows is None:
                missing.append("не найден столбец с порядковым номером месяца")
            if months_by_cell is None:
                missing.append("не найдено значение «Кол-во месяцев вывоза»")
            res.mismatch = "; ".join(missing)
        elif abs(months_by_rows - months_by_cell) < 1e-9:
            res.status = "ОК"
        elif months_by_rows == 0 and filled == 0:
            # Шаблон скопировали, но расчёт не вели: ни одного порядкового номера
            # и ни одной суммы капитала. Это не ошибка экономиста, а пустая заготовка,
            # поэтому выносим в отдельную категорию, чтобы не топить реальные расхождения.
            res.status = "Не заполнен"
            res.mismatch = (
                f"блок пустой: строк с расчётом нет, "
                f"а в ячейке «Кол-во месяцев вывоза» стоит {_fmt(months_by_cell)}"
            )
        else:
            res.status = "Расхождение"
            diff = months_by_rows - months_by_cell
            res.mismatch = (
                f"по строкам {months_by_rows}, по ячейке {_fmt(months_by_cell)} "
                f"(разница {diff:+g}); строки блока: {rows_detail or '—'}"
            )
        results.append(res)
    return results


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------------------
# Обход папки
# ---------------------------------------------------------------------------


def iter_excel_files(root: str, recursive: bool = True) -> Iterator[str]:
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "~$"))]
            for name in sorted(filenames):
                yield os.path.join(dirpath, name)
    else:
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if os.path.isfile(path):
                yield path


def _is_target(path: str) -> bool:
    name = os.path.basename(path)
    if name.startswith("~$") or name.startswith("."):
        return False
    return name.lower().endswith(SUPPORTED_EXT + LEGACY_EXT)


def _open_workbook(path: str):
    """
    Открывает книгу только на чтение. Для сетевых/удалённых папок сначала копирует
    файл во временный каталог: так файл не блокируется у коллег и меньше обрывов связи.
    """
    tmp_dir = tempfile.mkdtemp(prefix="bp_parser_")
    tmp_path = os.path.join(tmp_dir, os.path.basename(path))
    try:
        shutil.copy2(path, tmp_path)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # не удалось скопировать — пробуем читать напрямую
        return openpyxl.load_workbook(path, data_only=True, read_only=True), None
    wb = openpyxl.load_workbook(tmp_path, data_only=True, read_only=True)
    return wb, tmp_dir


def scan_folder(
    root: str,
    recursive: bool = True,
    sheet_filter: str = "",
    progress=None,
) -> ScanReport:
    """
    Обходит папку и собирает отчёт.

    sheet_filter — если задан, обрабатываются только листы, чьё имя содержит эту строку
                   (без учёта регистра). Пусто = все листы.
    progress     — callable(done, total, current_file) для индикатора.
    """
    report = ScanReport()
    files = [p for p in iter_excel_files(root, recursive) if _is_target(p)]
    total = len(files)
    needle = sheet_filter.strip().lower()

    for idx, path in enumerate(files, start=1):
        name = os.path.basename(path)
        if progress:
            progress(idx, total, name)
        scan_one_file(path, name, needle, report)

    return report


def scan_one_file(
    path: str,
    display_name: str,
    needle: str,
    report: ScanReport,
    report_path: str | None = None,
) -> None:
    """
    Разбирает одну книгу и дописывает результат в отчёт. Ошибки не пробрасывает.

    report_path — что показать в столбце «Полный путь». Для загруженных через браузер
    файлов это имя файла, а не временный путь на сервере.
    """
    shown_path = report_path if report_path is not None else path
    if display_name.lower().endswith(LEGACY_EXT):
        report.skipped.append((display_name, "старый формат .xls — пересохраните как .xlsx"))
        return

    tmp_dir = None
    try:
        wb, tmp_dir = _open_workbook(path)
    except Exception as exc:  # noqa: BLE001
        report.skipped.append((display_name, f"не удалось открыть: {exc}"))
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    try:
        report.files_scanned += 1

        titles = [ws.title for ws in wb.worksheets]
        hidden = [ws.title for ws in wb.worksheets if ws.sheet_state != "visible"]

        # Признаки расчёта: и по названиям листов, и по имени файла.
        haystack = " ".join(titles).lower() + " " + display_name.lower()
        has_lukoil = "да" if ("лук" in haystack) else "—"
        has_dsp = "да" if ("дсп" in haystack) else "—"
        hidden_text = ("да: " + ", ".join(hidden)) if hidden else "нет"

        # (порядковый номер листа, имя, прибыль) — порядок важен для сопоставления
        found_profits: list[tuple[int, str, float]] = []
        found_revs: list[tuple[int, str, float]] = []
        plan_by_sheet = {}
        list_items = []          # позиции с листа-перечня (запасной источник)
        pending: list[tuple[int, str, list[BlockResult]]] = []

        # Один проход по листам: сетка читается ровно один раз и используется
        # и для блока «Расчет процентов», и для строки «Чистая прибыль».
        for index, ws in enumerate(wb.worksheets):
            grid, max_row, max_col = _read_grid(ws)

            profit = find_net_profit(grid, max_row, max_col)
            if profit is not None:
                found_profits.append((index, ws.title, profit))
            rev = find_revenue(grid, max_row, max_col)
            if rev is not None:
                found_revs.append((index, ws.title, rev))
            try:
                plan_by_sheet[ws.title] = find_plan_items(grid, max_row, max_col)
            except Exception:
                plan_by_sheet[ws.title] = []
            try:
                if "перечень" in ws.title.lower() or "перечен" in ws.title.lower():
                    li = find_list_items(grid, max_row, max_col)
                    if li and not list_items:
                        list_items = li
            except Exception:
                pass

            if needle and needle not in ws.title.lower():
                continue
            report.sheets_scanned += 1
            try:
                rows = scan_sheet_grid(grid, max_row, max_col, ws.title, display_name, shown_path)
            except Exception as exc:  # noqa: BLE001
                report.skipped.append((f"{display_name} [{ws.title}]", f"ошибка разбора: {exc}"))
                continue
            if rows:
                pending.append((index, ws.title, rows))

        for index, sheet_title, rows in pending:
            match = _match_profit(index, sheet_title, found_profits)
            rmatch = _match_profit(index, sheet_title, found_revs)
            for res in rows:
                if match:
                    res.net_profit, res.profit_sheet = match
                if rmatch:
                    res.revenue, res.revenue_sheet = rmatch
                res.plan_items = plan_by_sheet.get(sheet_title) or list_items or []
                res.has_lukoil = has_lukoil
                res.has_dsp = has_dsp
                res.hidden_sheets = hidden_text
                report.rows.append(res)
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _match_profit(index: int, sheet_title: str, found: list[tuple[int, str, float]]):
    """
    Подбирает «Чистую прибыль» для листа с блоком «Расчет процентов».

    Порядок поиска:
      1) тот же лист;
      2) лист с таким же названием без префикса (БП_X ↔ v0 X);
      3) ближайший лист с прибылью выше по книге — листы идут парами
         «БП_дата» и следом «v0 дата», причём даты в этих названиях
         совпадают не всегда: при копировании их часто не меняют;
      4) ближайший ниже, если книга собрана в обратном порядке.
    """
    if not found:
        return None

    for idx, title, value in found:
        if idx == index:
            return value, title

    key = sheet_key(sheet_title)
    for idx, title, value in found:
        if sheet_key(title) == key:
            return value, title

    before = [f for f in found if f[0] < index]
    if before:
        idx, title, value = max(before, key=lambda f: f[0])
        return value, title

    idx, title, value = min(found, key=lambda f: f[0])
    return value, title


def scan_uploads(uploads, sheet_filter: str = "", progress=None) -> ScanReport:
    """
    Разбирает файлы, загруженные через браузер.

    uploads — объекты Streamlit UploadedFile (у них есть .name и .getbuffer()).
    Файлы кладутся во временный каталог и удаляются сразу после разбора:
    на сервере ничего не остаётся.
    """
    report = ScanReport()
    needle = sheet_filter.strip().lower()
    total = len(uploads)

    tmp_dir = tempfile.mkdtemp(prefix="bp_upload_")
    try:
        for idx, upload in enumerate(uploads, start=1):
            name = os.path.basename(upload.name)
            if progress:
                progress(idx, total, name)

            path = os.path.join(tmp_dir, name)
            try:
                with open(path, "wb") as fh:
                    fh.write(upload.getbuffer())
            except Exception as exc:  # noqa: BLE001
                report.skipped.append((name, f"не удалось сохранить загруженный файл: {exc}"))
                continue

            scan_one_file(path, name, needle, report, report_path=name)
            try:
                os.remove(path)
            except OSError:
                pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return report
