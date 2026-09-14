#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_excel.py — Excel-отчёт реализации из out/sales_data.json.

Листы: Сводка по БП; По сериям продано; По номенклатуре; По периодам;
Продажи по документам; Сверка остатков; Для выгрузки в 1С;
Применённые корректировки; Проблемы.
Числа — 3 знака без округления. -> out/Реализация_metoptorg_<дата>.xlsx
"""
import os, json, re
from collections import defaultdict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
NUM = "#,##0.000"
HEAD_FILL = PatternFill("solid", fgColor="FF142248")
HEAD_FONT = Font(color="FFFFFFFF", bold=True, name="Calibri", size=11)
RED_FONT = Font(color="FFD1435B", bold=True)
BOLD = Font(bold=True)

REAL = lambda r: r["series"] != "БЕЗ СЕРИИ" and not str(r["series"]).startswith("площадка:")
PLAT = lambda r: str(r["series"]).startswith("площадка:")

def bucket(r):
    """Куда отнести тоннаж: реальная серия 1С / псевдо-серия площадки / без серии."""
    if PLAT(r): return "plat"
    return "real" if REAL(r) else "no"


def style_header(ws):
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(1, c)
        cell.fill = HEAD_FILL; cell.font = HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"; ws.row_dimensions[1].height = 30


def autosize(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def numcells(ws, cols):
    for row in ws.iter_rows(min_row=2):
        for c in cols:
            if c <= len(row): row[c - 1].number_format = NUM


def main():
    data = json.load(open(os.path.join(ROOT, "out", "sales_data.json"), encoding="utf-8"))
    items = data["items"]; meta = data["meta"]; checks = data.get("checks", [])
    S = data.get("S", [])
    sstr = lambda i: S[i] if isinstance(i, int) and 0 <= i < len(S) else (i or "")
    wb = openpyxl.Workbook(); wb.remove(wb.active)

    # ---------------- 1. Сводка по БП ----------------
    ws = wb.create_sheet("Сводка по БП")
    # Бизнес-план = ПРОЕКТ из справочника серий (их столько же, сколько проектов).
    ws.append(["Направление", "Бизнес-план (проект)", "Договор", "Контрагент", "Вывоз до",
               "Продано, т", "Реальная серия 1С, т",
               "Псевдо-серия площадки, т", "БЕЗ СЕРИИ, т", "% реальной серии",
               "Позиций", "Документов"])
    agg = defaultdict(lambda: {"t": 0., "real": 0., "plat": 0., "no": 0.,
                               "pos": set(), "docs": 0, "r": None})
    for r in items:
        a = agg[(r["direction"], r["series_full"] or "— проект не определён —")]
        a["t"] += r["tonnes"]; a["pos"].add(r["code"]); a["docs"] += len(r["docs"])
        a[bucket(r)] += r["tonnes"]
        a["r"] = a["r"] or r
    for (d, bp), a in sorted(agg.items(), key=lambda kv: -kv[1]["t"]):
        r0 = a["r"] or {}
        ws.append([d, bp, r0.get("contract", ""), r0.get("contragent", ""),
                   r0.get("datavyvoza", ""), a["t"], a["real"], a["plat"], a["no"],
                   round(a["real"] / a["t"] * 100, 1) if a["t"] else 0,
                   len(a["pos"]), a["docs"]])
    ws.append(["ИТОГО", "", "", "", "", meta["total_tonnes"], meta["real_series_tonnes"],
               meta["platform_tonnes"], meta["no_series_tonnes"],
               meta["real_series_pct"], "", ""])
    ws.cell(ws.max_row, 1).font = BOLD
    style_header(ws); autosize(ws, [24, 50, 16, 26, 12, 15, 18, 20, 15, 14, 10, 12])
    numcells(ws, [6, 7, 8, 9])

    # ---------------- 2. По сериям продано ----------------
    ws = wb.create_sheet("По сериям продано")
    ws.append(["Серия / проект (полная строка)", "Ключ проекта", "Договор", "Направление", "Контрагент",
               "Вывоз до", "Происхождение", "Продано, т", "Позиций", "Документов",
               "Подразделения"])
    agg = defaultdict(lambda: {"t": 0., "pos": set(), "docs": 0, "bases": set(), "b": None, "full": "", "r": None})
    for r in items:
        a = agg[r["series"]]
        a["t"] += r["tonnes"]; a["pos"].add(r["code"]); a["docs"] += len(r["docs"])
        a["bases"].add(r.get("division") or r["base"]); a["full"] = a["full"] or r["series_full"]; a["r"] = a["r"] or r
        a["b"] = r["badge"] if a["b"] is None else a["b"]
    for skey, a in sorted(agg.items(), key=lambda kv: -kv[1]["t"]):
        r = a["r"]
        ws.append([a["full"], skey, r["contract"], r["direction"], r["contragent"],
                   r["datavyvoza"], a["b"], a["t"], len(a["pos"]), a["docs"],
                   ", ".join(sorted(a["bases"]))])
    style_header(ws); autosize(ws, [52, 14, 16, 20, 26, 12, 14, 14, 10, 12, 34]); numcells(ws, [8])

    # ---------------- 3. По номенклатуре ----------------
    ws = wb.create_sheet("По номенклатуре")
    ws.append(["Категория", "Отчётная группа", "Код", "Номенклатура", "Ед.",
               "Продано, т", "Реальная серия 1С, т", "Псевдо-серия площадки, т",
               "БЕЗ СЕРИИ, т", "Серий", "Документов"])
    agg = defaultdict(lambda: {"t": 0., "real": 0., "plat": 0., "no": 0., "ser": set(), "docs": 0, "u": ""})
    for r in items:
        a = agg[(r["category"], r["report_group"], r["code"], r["name"])]
        a["t"] += r["tonnes"]; a["docs"] += len(r["docs"]); a["u"] = a["u"] or r["unit"]
        a[bucket(r)] += r["tonnes"]
        if REAL(r): a["ser"].add(r["series"])
    for (cat, g, code, name), a in sorted(agg.items(), key=lambda kv: (kv[0][0], -kv[1]["t"])):
        ws.append([cat, g, code, name, a["u"], a["t"], a["real"], a["plat"], a["no"],
                   len(a["ser"]), a["docs"]])
    style_header(ws); autosize(ws, [20, 30, 14, 40, 7, 14, 18, 20, 14, 9, 12]); numcells(ws, [6, 7, 8, 9])

    # ---------------- 4. По периодам ----------------
    ws = wb.create_sheet("По периодам")
    ws.append(["Месяц", "Продано, т", "Реальная серия 1С, т", "Псевдо-серия площадки, т",
               "БЕЗ СЕРИИ, т", "% реальной серии", "Документов", "Бизнес-планов", "Серий"])
    agg = defaultdict(lambda: {"t": 0., "real": 0., "plat": 0., "no": 0., "docs": 0, "bp": set(), "ser": set()})
    for r in items:
        b = bucket(r)
        for d in r["docs"]:
            a = agg[d["dt"][:7]]
            a["t"] += d["tonnes"]; a["docs"] += 1; a[b] += d["tonnes"]
            if r["contract"]: a["bp"].add(r["contract"])
            if REAL(r): a["ser"].add(r["series"])
    for m, a in sorted(agg.items()):
        ws.append([m, a["t"], a["real"], a["plat"], a["no"],
                   round(a["real"] / a["t"] * 100, 1) if a["t"] else 0,
                   a["docs"], len(a["bp"]), len(a["ser"])])
    style_header(ws); autosize(ws, [12, 14, 20, 20, 14, 16, 12, 14, 10]); numcells(ws, [2, 3, 4, 5])

    # ---------------- 4b. Закупки и продажи (жизненный цикл партии) ----------------
    ws = wb.create_sheet("Закупки и продажи")
    order = meta.get("flow_order", [])
    title = meta.get("flow_title", {})
    tot = meta.get("flow_totals", {})
    label = meta.get("flow_label", {})
    cols = [k for k in order if abs(tot.get(k, 0)) > 0.001]
    # ФАКТ ВЫРУЧКИ (14.09.2026): колонки только когда регистр загружен
    has_rub = bool((meta.get("revenue") or {}).get("linked"))
    rub_hdr = ["ВЫРУЧКА без НДС, ₽", "СЕБЕСТОИМОСТЬ продаж, ₽", "МАРЖА, ₽"] if has_rub else []
    def rub_cells(r):
        if not has_rub:
            return []
        rv, c = r.get("выручка", 0) or 0, r.get("себестоимость", 0) or 0
        return [rv, c, rv - c]
    ws.append(["Серия (полная строка)", "Бизнес-план", "Направление"]
              + [label.get(k, k.replace("_", " ")) for k in cols]
              + ["внутренний оборот", "ВНЕШНИЙ ПРИХОД", "ВНЕШНЕЕ ВЫБЫТИЕ",
                 "потери переработки", "недоезд", "ОСТАТОК (факт)", "НЕВЯЗКА"] + rub_hdr)
    flows_rows = sorted(data.get("flows", []), key=lambda r: -r.get("куплено", 0))
    for r in flows_rows:
        ws.append([r["series_full"], r["contract"], r["direction"]]
                  + [r.get(k, 0) for k in cols]
                  + [r.get("внутренний_оборот", 0), r["поступило"], r["выбыло"],
                     r.get("потери_переработки", 0), r.get("недоезд", 0),
                     r["остаток"], r["невязка"]] + rub_cells(r))
        if abs(r["невязка"]) > 0.001:
            ws.cell(ws.max_row, 4 + len(cols) + 6).font = RED_FONT
    ws.append(["ИТОГО", "", ""] + [tot.get(k, 0) for k in cols]
              + [tot.get("внутренний_оборот", 0), tot.get("поступило", 0),
                 tot.get("выбыло", 0), tot.get("потери_переработки", 0),
                 tot.get("недоезд", 0), tot.get("остаток", 0), tot.get("невязка", 0)]
              + ([sum(r.get("выручка", 0) or 0 for r in flows_rows),
                  sum(r.get("себестоимость", 0) or 0 for r in flows_rows),
                  sum((r.get("выручка", 0) or 0) - (r.get("себестоимость", 0) or 0)
                      for r in flows_rows)] if has_rub else []))
    ws.cell(ws.max_row, 1).font = BOLD
    style_header(ws)
    autosize(ws, [50, 18, 20] + [17] * (len(cols) + 7 + len(rub_hdr)))
    numcells(ws, list(range(4, 4 + len(cols) + 7 + len(rub_hdr))))
    # расшифровка потоков под таблицей
    ws.append([]); ws.append(["Что означают колонки:"])
    ws.cell(ws.max_row, 1).font = BOLD
    for k in cols:
        ws.append([label.get(k, k.replace("_", " ")), title.get(k, "")])
    ws.append(["внутренний оборот", "перепродажа своей организации «проект Базы» — не продажа"])
    ws.append(["ВНЕШНИЙ ПРИХОД", "купили + излишки + ввод остатков + пересортица"])
    ws.append(["ВНЕШНЕЕ ВЫБЫТИЕ", "продали + возврат + недостачи + списание на затраты + внутр. оборот"])
    ws.append(["потери переработки", "в переработку − из переработки (внутри компании)"])
    ws.append(["недоезд", "уехало − приехало (внутри компании)"])
    ws.append(["НЕВЯЗКА", "приход − выбытие − потери − недоезд − остаток; 0 = цикл сходится"])
    if has_rub:
        ws.append(["ВЫРУЧКА / СЕБЕСТОИМОСТЬ / МАРЖА",
                   "регистр 1С «Выручка и себестоимость продаж» без НДС, разнесён по "
                   "сериям так же, как тонны продаж; маржа = выручка − себестоимость"])
    ws.append(["", "«уехало/приехало» и «в переработку/из переработки» — ВНУТРЕННИЕ движения,"])
    ws.append(["", "они перекладывают тот же металл и в приход/выбытие НЕ входят."])

    # ---------------- 5. Продажи по документам ----------------
    ws = wb.create_sheet("Продажи по документам")
    ws.append(["Документ", "Дата", "Лот-партия", "Покупатель", "База", "Склад", "Тип площадки",
               "Код", "Номенклатура", "Серия (полная строка)", "Бизнес-план",
               "Направление", "Происхождение", "Кол-во", "Ед.", "Продано, т"])
    recs = []
    for r in items:
        for d in r["docs"]:
            recs.append([d["regnum"], d["dt"], d.get("lot", ""), sstr(d["buyer"]),
                         r["base"], r["sklad"], r["site"], r["code"], r["name"],
                         r["series_full"], r["contract"], r["direction"], d["badge"],
                         d["qty"], r["unit"], d["tonnes"]])
    recs.sort(key=lambda x: (x[1], str(x[0])), reverse=True)
    for rec in recs: ws.append(rec)
    style_header(ws); autosize(ws, [16, 12, 16, 26, 20, 26, 12, 14, 36, 46, 16, 20, 14, 12, 7, 14])
    numcells(ws, [14, 16])

    # ---------------- 5a. Движения по иерархии ----------------
    # Тот же разворот, что в дереве дашборда: направление → бизнес-план → серия →
    # тип подразделения → склад → номенклатура, и в каждой строке ВСЕ потоки
    # (купили · уехало · приехало · в производство · из производства · ПРОДАЛИ · возврат ·
    # недостачи · на затраты). Считается из построчных движений, поэтому суммы
    # складываются снизу вверх и сходятся с 1С на каждом уровне.
    ws = wb.create_sheet("Движения по иерархии")
    MC0 = {c: i for i, c in enumerate(meta.get("move_cols", []))}
    MFL0 = meta.get("move_flows", [])
    SK_SITE = data.get("sk_site", {})
    NM0 = data.get("nm", {})
    FLOW_COLS = ["куплено", "уехало", "приехало", "переработка_забрали",
                 "переработка_вернули", "продано", "возврат", "списано",
                 "списано_на_затраты"]
    lbl = meta.get("flow_label", {})
    # справочные поля проекта — из позиций продаж
    proj_info = {}
    for r in items:
        proj_info.setdefault(r["series"], (r["direction"], r["series_full"], r["contract"]))
    cells = defaultdict(lambda: defaultdict(float))
    for r in data.get("moves", []):
        key = (sstr(r[MC0["series"]]), sstr(r[MC0["variant"]]),
               sstr(r[MC0["sklad"]]), sstr(r[MC0["code"]]))
        cells[key][MFL0[r[MC0["flow"]]]] += r[MC0["tonnes"]]
    # ОСТАТОК — сколько ещё лежит на этом складе и может быть продано:
    # приход минус расход по тому же ключу (серия · вариант · склад · код).
    BAL_IN = ["куплено", "приехало", "переработка_вернули", "излишки",
              "ввод_остатков", "пересортица"]
    BAL_OUT = ["уехало", "переработка_забрали", "продано", "возврат", "списано",
               "списано_на_затраты", "внутренний_оборот"]
    balance = lambda fl: (sum(fl.get(k, 0.0) for k in BAL_IN)
                          - sum(fl.get(k, 0.0) for k in BAL_OUT))
    ws.append(["Направление", "Бизнес-план (проект)", "Договор",
               "Серия (полная строка)", "Тип подразделения", "Склад", "Код",
               "Номенклатура"]
              + [lbl.get(k, k.replace("_", " ")) for k in FLOW_COLS]
              + ["ОСТАТОК (можно продать)"])
    rows_h = []
    for (skey, svar, sklad, code), fl in cells.items():
        d, full, contract = proj_info.get(skey, ("—", skey, ""))
        rows_h.append([d, full, contract, svar, SK_SITE.get(sklad, "—"), sklad, code,
                       NM0.get(code, "")]
                      + [round(fl.get(k, 0.0), 3) for k in FLOW_COLS]
                      + [round(balance(fl), 3)])
    # сортировка: направление, проект, серия, склад — как в дереве
    rows_h.sort(key=lambda x: (str(x[0]), str(x[1]), str(x[3]), str(x[5]), str(x[6])))
    for rec in rows_h:
        ws.append(rec)
    style_header(ws)
    autosize(ws, [22, 44, 16, 44, 16, 28, 14, 32] + [13] * len(FLOW_COLS) + [22])
    numcells(ws, list(range(9, 10 + len(FLOW_COLS))))
    print("   строк в «Движения по иерархии»: %d" % len(rows_h))

    # ---------------- 5b. Движения по документам (§5 ТЗ) ----------------
    # Построчно, без агрегатов: документ → номенклатура → количество → откуда →
    # куда. Именно этот лист сверяется с 1С один в один.
    ws = wb.create_sheet("Движения по документам")
    MC = {c: i for i, c in enumerate(meta.get("move_cols", []))}
    MFL = meta.get("move_flows", [])
    MPH = meta.get("move_phrase", {})
    MSH = meta.get("move_short", {})
    NM = data.get("nm", {})
    ws.append(["Дата", "Документ 1С", "Поток", "Что произошло", "Код",
               "Номенклатура", "Количество", "Тонн", "Откуда", "Куда",
               "Лот-партия", "Серия (полная строка)", "Происхождение"])
    mrows = data.get("moves", [])
    def mval(r, c): return r[MC[c]]
    def mstr(r, c): return sstr(mval(r, c))
    recs = []
    for r in mrows:
        fl = MFL[mval(r, "flow")]
        frm, to = mstr(r, "from"), mstr(r, "to")
        phrase = (MPH.get(fl, fl).replace("{from}", frm or "—").replace("{to}", to or "—"))
        code = mstr(r, "code")
        recs.append([mstr(r, "dt"), mstr(r, "doc"), MSH.get(fl, fl), phrase, code,
                     NM.get(code, ""), mval(r, "qty"), mval(r, "tonnes"), frm, to,
                     mstr(r, "lot"), mstr(r, "variant"), mstr(r, "badge")])
    recs.sort(key=lambda x: (x[0], str(x[1])), reverse=True)   # свежие сверху
    for rec in recs:
        ws.append(rec)
    style_header(ws)
    autosize(ws, [12, 16, 14, 54, 14, 34, 13, 12, 30, 30, 16, 46, 14])
    numcells(ws, [7, 8])

    # ---------------- 5c. План-факт по БП (диаграмма Ганта) ----------------
    # Та же таблица, что за диаграммой: плановая дата вывоза из справочника серий
    # против фактического закрытия по движениям, дискретность — месяц.
    ws = wb.create_sheet("План-факт по БП")
    # Диаграмма строится в двух вариантах, и оба лежат в одной строке: сначала
    # общая часть (срок вывоза и его источник), потом блок каждого варианта.
    VARS = meta.get("gantt_variants") or []
    head = ["Где заготовлено", "Направление", "Бизнес-план (проект)", "Договор",
            "Контрагент", "Срок вывоза", "Источник срока", "Комментарий к сроку",
            "Срок ранний", "Сроков у проекта"]
    for v in VARS:
        t = v["title"]
        head += [t + ": статус", t + ": первый приход", t + ": последний вывоз",
                 t + ": опоздание, мес", t + ": приход, т", t + ": вывоз, т",
                 t + ": % вывезено", t + ": остаток, т"]
    ws.append(head)
    def _key(x):
        v = (x.get("v") or {}).get(VARS[0]["key"] if VARS else "") or {}
        return (-(v.get("delay") if v.get("delay") is not None else -999),
                -(v.get("rest") or 0))
    for g in sorted(data.get("gantt", []), key=_key):
        row = [g.get("site", ""), g["direction"], g["name"], g["contract"],
               g["contragent"], g["plan_end"], g.get("plan_src", ""),
               g.get("plan_note", ""), g["plan_end_min"], g["plans"]]
        for v in VARS:
            r = (g.get("v") or {}).get(v["key"]) or {}
            row += [r.get("status", ""), r.get("start", ""), r.get("fact_end", ""),
                    r.get("delay"), r.get("bought"), r.get("sold"),
                    r.get("pct"), r.get("rest")]
        ws.append(row)
        for i, v in enumerate(VARS):
            r = (g.get("v") or {}).get(v["key"]) or {}
            if r.get("status") == "просрочен":
                ws.cell(ws.max_row, 11 + i * 8).font = RED_FONT
    style_header(ws)
    autosize(ws, [16, 20, 46, 16, 26, 14, 15, 30, 14, 15]
                 + [18, 18, 18, 15, 14, 14, 13, 14] * len(VARS))
    numcells(ws, [15 + i * 8 for i in range(len(VARS))]
                 + [16 + i * 8 for i in range(len(VARS))]
                 + [18 + i * 8 for i in range(len(VARS))])

    # ---------------- 6. Сверка остатков ----------------
    ws = wb.create_sheet("Сверка остатков")
    # Обратный ход: якорь — факт на сегодня, движения отматываются назад.
    ws.append(["База", "Склад", "Код", "Номенклатура", "Ед.",
               "① Факт " + meta.get("stock_date", "") + " (якорь)", "② Расход", "в т.ч. продано",
               "③ Приход", "④ Остаток на " + meta.get("balance_anchor", "") + " (①+②−③)",
               "Полнота регистра (③−②)", "Δ", "Есть якорь"])
    for c in sorted(checks, key=lambda c: (not c["covered"], -abs(c["diff"]))):
        ws.append([c["base"], c["sklad"], c["code"], c["name"], c["unit"],
                   c["fact"] if c["covered"] else "нет якоря",
                   c["rashod"], c["sale"], c["prihod"], c["opening"], c["calc"],
                   c["diff"] if c["covered"] else "",
                   "да" if c["covered"] else "нет (склада нет в остатках на сегодня)"])
        if c["covered"] and abs(c["diff"]) > 0.001:
            ws.cell(ws.max_row, 12).font = RED_FONT
    style_header(ws); autosize(ws, [20, 28, 14, 36, 7, 20, 14, 14, 14, 24, 20, 14, 34])
    numcells(ws, [6, 7, 8, 9, 10, 11, 12])

    # ---------------- 7. Для выгрузки в 1С ----------------
    ws = wb.create_sheet("Для выгрузки в 1С")
    ws.append(["Склад", "Ед. изм.", "Номенклатура.Код", "Подразделение",
               "Номенклатура", "Серия", "Количество"])
    rows1c = [[r["sklad"], r["unit"], r["code"], r.get("division") or r["base"], r["name"],
               r["series_full"] if REAL(r) else "", r["tonnes"]] for r in items]
    rows1c.sort(key=lambda x: (str(x[4]), str(x[5])))
    for rec in rows1c: ws.append(rec)
    style_header(ws); autosize(ws, [28, 10, 18, 22, 40, 50, 14]); numcells(ws, [7])

    # ---------------- 8. Применённые корректировки ----------------
    ws = wb.create_sheet("Применённые корректировки")
    ws.append(["Документ", "Код", "Было", "Стало", "Кол-во", "Автор", "Комментарий", "Дата документа"])
    for e in data.get("edits", []):
        ws.append([e["regnum"], e["code"], e["old"], e["new"], e["qty"],
                   e["author"], e["note"], e["ts"]])
    if not data.get("edits"):
        ws.append(["— ручных привязок не применялось —"])
    style_header(ws); autosize(ws, [18, 16, 20, 20, 12, 16, 40, 20]); numcells(ws, [5])

    # ---------------- 9. Проблемы ----------------
    ws = wb.create_sheet("Проблемы")
    ws.append(["Бейдж", "Что это значит", "База", "Склад", "Тип площадки", "Код",
               "Номенклатура", "Серия", "Продано, т", "Документов"])
    bt = meta.get("badge_title", {})
    # «площадка» не проблема: по алгоритму такая позиция всегда несёт номер договора
    # и/или номер БП из имени склада — партия опознана. Оставляем только если нет ни того, ни другого.
    def unresolved(r):
        if r["badge"] == "площадка":
            return not r["contract"] and "·БП" not in str(r["series"])
        return r["badge"] in ("нет", "вероятно", "ввод", "доля")
    probl = [r for r in items if unresolved(r)]
    for r in sorted(probl, key=lambda r: -r["tonnes"]):
        ws.append([r["badge"], bt.get(r["badge"], ""), r["base"], r["sklad"], r["site"],
                   r["code"], r["name"], r["series_display"], r["tonnes"], len(r["docs"])])
    style_header(ws); autosize(ws, [14, 44, 20, 28, 12, 14, 36, 22, 14, 12]); numcells(ws, [9])
    # почему часть тоннажа осталась без серии — говорим прямо, а не прячем
    ws.append([])
    ws.append(["Почему серия не восстановлена"]); ws.cell(ws.max_row, 1).font = BOLD
    ws.append(["Строк реализации без описания партии в 1С", meta.get("sale_rows_no_lot", 0)])
    ws.append(["", "У такой строки нет лота, привязывать не к чему: карта (Партия+Код) "
                   "неприменима. Ключ («пусто» + код) склеивал бы все безпартийные "
                   "приходы этого кода по всей базе — это фикция, поэтому такие строки "
                   "остаются БЕЗ СЕРИИ."])
    ws.append(["Смешанных лотов (в приходе несколько серий)", meta.get("mix_lots", 0)])
    ws.append(["Строк расхода из смешанных лотов", meta.get("mix_alloc_rows", 0)])
    ws.append(["Из них пришлось делить (бейдж «доля»)", meta.get("mix_split_rows", 0)])
    for nt in data.get("mix_notes", [])[:60]:
        ws.append(["", "лот %s / код %s, документ %s, %s т — %s"
                   % (nt.get("lot", ""), nt.get("code", ""), nt.get("regnum", ""),
                      nt.get("qty", ""), nt.get("reason", ""))])

    # ---------------- 10. Автопроверки (§11) ----------------
    ws = wb.create_sheet("Автопроверки")
    ws.append(["Статус", "Проверка", "Результат", "Что это значит"])
    for a in meta.get("autochecks", []):
        ws.append([{"ok": "✓ сходится", "warn": "! вопрос к данным 1С",
                    "bad": "✗ ошибка расчёта"}.get(a["status"], a["status"]),
                   a["name"], a["value"], a.get("note", "")])
        if a["status"] == "bad":
            ws.cell(ws.max_row, 1).font = RED_FONT
        for c in range(1, 5):
            ws.cell(ws.max_row, c).alignment = Alignment(vertical="top", wrap_text=True)
    style_header(ws); autosize(ws, [22, 56, 44, 80])

    # ---------------- титул ----------------
    ws = wb.create_sheet("О расчёте", 0)
    info = [
        ["МЕТОПТОРГ · Реализация металлолома по сериям и бизнес-планам", ""],
        ["", ""],
        ["Сформировано", meta.get("generated", "")],
        ["Период выгрузки", meta.get("period_min", "") + " … " + meta.get("period_max", "")],
        ["Остатки на дату", meta.get("stock_date", "")],
        ["", ""],
        ["ПРОДАНО ВСЕГО, т", meta.get("total_tonnes", 0)],
        ["  с реальной серией 1С, т", meta.get("real_series_tonnes", 0)],
        ["  псевдо-серия площадки, т", meta.get("platform_tonnes", 0)],
        ["  БЕЗ СЕРИИ, т", meta.get("no_series_tonnes", 0)],
        ["  % с реальной серией", meta.get("real_series_pct", 0)],
        ["", ""],
        ["Строк реализации", meta.get("sale_rows", 0)],
        ["Позиций (склад·код·серия)", meta.get("positions", 0)],
        ["Строк регистра всего", meta.get("rows_total", 0)],
        ["", ""],
        ["Внутренний оборот (перепродажа своей орг. «проект Базы») — ИСКЛЮЧЁН", ""],
        ["  строк", meta.get("internal_rows", 0)],
        ["  количество, ед.", meta.get("internal_qty", 0)],
        ["", ""],
        ["Продажей считается только «Реализация товаров и услуг».", ""],
        ["Перемещение / внутреннее потребление / производство / сборка / списание — не продажа.", ""],
        ["", ""],
        ["Расчёт идёт ОБРАТНЫМ ХОДОМ: якорь — фактический остаток на сегодня (факт из 1С),", ""],
        ["движения отматываются назад. Остаток на начало периода — результат, а не вход,", ""],
        ["поэтому файл «остатки на начало периода» не требуется.", ""],
        ["Якорь покрывает %s складов из %s в движениях — там, где якоря нет, остаток принят нулевым."
         % (meta.get("stock_sklady", 0), meta.get("move_sklady", 0)), ""],
    ]
    for r in info: ws.append(r)
    ws.cell(1, 1).font = Font(bold=True, size=14, color="FF142248")
    autosize(ws, [64, 28])

    date = (meta.get("stock_date") or meta.get("generated", ""))[:10].replace("-", "")
    out = os.path.join(ROOT, "out", "Реализация_metoptorg_%s.xlsx" % date)
    wb.save(out)
    print("Готово: %s (%.1f МБ)" % (out, os.path.getsize(out) / 1e6))
    for s in wb.sheetnames:
        print("   лист: %s" % s)


if __name__ == "__main__":
    main()
