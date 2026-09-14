# -*- coding: utf-8 -*-
"""Патч /opt/parser-bp/parser_core.py (14.09.2026): плановые позиции из листа
«перечень», когда таблица версии сломана.

БП 1875: на листе версии «Объем покупки» и «Подразделение» — #REF! (формулы
ссылались на удалённый лист), позиций ноль, хотя перечень Лукойла с весами по
каждому месту вывоза лежит на соседнем листе «перечень 1» (354 строки,
523,758 т). Таких версий ~90 из 5 778. Если у версии позиций нет — берём их с
листа-перечня: колонки «Наименование ТМЦ» и «Вес, т», регион/место — в
«подразделение». Цены в перечне нет, стоимость остаётся пустой.

Применять:  python3 parser-bp-list-fallback.patch.py /opt/parser-bp/parser_core.py
"""
import io, sys

p = sys.argv[1] if len(sys.argv) > 1 else "/opt/parser-bp/parser_core.py"
s = io.open(p, encoding="utf-8").read()
if "def find_list_items(" in s:
    print("уже применён"); sys.exit(0)

FN = '''def find_list_items(grid, max_row, max_col):
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


'''
anchor = "def sheet_key(title: str) -> str:"
assert anchor in s; s = s.replace(anchor, FN + anchor, 1)

old = '''            try:
                plan_by_sheet[ws.title] = find_plan_items(grid, max_row, max_col)
            except Exception:
                plan_by_sheet[ws.title] = []'''
new = '''            try:
                plan_by_sheet[ws.title] = find_plan_items(grid, max_row, max_col)
            except Exception:
                plan_by_sheet[ws.title] = []
            try:
                if "перечень" in ws.title.lower() or "перечен" in ws.title.lower():
                    li = find_list_items(grid, max_row, max_col)
                    if li and not list_items:
                        list_items = li
            except Exception:
                pass'''
assert old in s; s = s.replace(old, new, 1)

old2 = '''        plan_by_sheet = {}
        pending: list[tuple[int, str, list[BlockResult]]] = []'''
new2 = '''        plan_by_sheet = {}
        list_items = []          # позиции с листа-перечня (запасной источник)
        pending: list[tuple[int, str, list[BlockResult]]] = []'''
assert old2 in s; s = s.replace(old2, new2, 1)

old3 = '''                res.plan_items = plan_by_sheet.get(sheet_title) or []'''
new3 = '''                res.plan_items = plan_by_sheet.get(sheet_title) or list_items or []'''
assert old3 in s; s = s.replace(old3, new3, 1)
io.open(p, "w", encoding="utf-8").write(s)
print("патч применён")
