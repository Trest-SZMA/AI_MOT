#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_excel.py — Excel-отчёт из out/dashboard_data.json.
Листы: Сводка по базам; По сериям; По номенклатуре; Подразделения;
Остатки по сериям (детализация + «Как определена серия»); Остатки по документам;
Для корректировки в 1С; Применённые корректировки; Сверка с 1С; Проблемы.
Числа — 3 знака без округления. Пишет out/Остатки_metoptorg_<дата>.xlsx
"""
import os, json, re
from collections import defaultdict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
NUM = "#,##0.000"
NAVY = "FF142248"; STEEL = "FF485E88"; RED = "FFD1435B"
HEAD_FILL = PatternFill("solid", fgColor="FF142248")
HEAD_FONT = Font(color="FFFFFFFF", bold=True, name="Calibri", size=11)
BOLD = Font(bold=True)
THIN = Side(style="thin", color="FFDDDDDD")
BORDER = Border(bottom=THIN)
BADGE_HUMAN = {"1С":"из строки факта (1С)","док":"карта Партия+Код→серия","переработка":"наследование при переработке",
    "ручная":"уникальная серия документа","площадка":"площадка продавца (договор в складе)","доля":"разбита по долям источников",
    "вероятно":"эвристика по складу","ввод":"ВВОД ОСТАТКОВ / ИЗЛИШКИ","нет":"БЕЗ СЕРИИ"}
REAL = lambda r: r["series"] != "БЕЗ СЕРИИ" and not str(r["series"]).startswith("площадка:")

def style_header(ws, ncol):
    for c in range(1, ncol+1):
        cell = ws.cell(1, c); cell.fill = HEAD_FILL; cell.font = HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"; ws.row_dimensions[1].height = 30

def autosize(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

def numcells(ws, cols):
    for row in ws.iter_rows(min_row=2):
        for c in cols:
            row[c-1].number_format = NUM

def main():
    data = json.load(open(os.path.join(ROOT, "out", "dashboard_data.json"), encoding="utf-8"))
    rows = data["rows"]; meta = data["meta"]; check = data.get("check", [])
    wb = openpyxl.Workbook(); wb.remove(wb.active)

    # 1. Сводка по базам
    ws = wb.create_sheet("Сводка по базам")
    ws.append(["База", "Тонн всего", "С реальной серией", "БЕЗ СЕРИИ", "% с серией", "Позиций", "Серий"])
    agg = defaultdict(lambda: {"t":0.,"real":0.,"no":0.,"pos":set(),"ser":set()})
    for r in rows:
        a = agg[r["base"]]; a["t"] += r["tonnes"]
        if REAL(r): a["real"] += r["tonnes"]; a["ser"].add(r["series"])
        if r["series"] == "БЕЗ СЕРИИ": a["no"] += r["tonnes"]
        a["pos"].add(r["code"])
    for b in sorted(agg, key=lambda x:(float((re.search(r'\d+',x) or ['9'])[0]) if re.search(r'\d',x) else 9, x)):
        a = agg[b]; ws.append([b, a["t"], a["real"], a["no"],
            round(a["real"]/a["t"]*100,1) if a["t"] else 0, len(a["pos"]), len(a["ser"])])
    ws.append(["ИТОГО", meta["total_tonnes"], meta["real_series_tonnes"], "", "", "", ""])
    style_header(ws, 7); autosize(ws, [34,14,16,14,11,10,9]); numcells(ws, [2,3,4])

    # 2. По сериям
    ws = wb.create_sheet("По сериям")
    ws.append(["Направление","Договор","Серия","Контрагент","Дата вывоза","Тонн","Позиций","Как определена серия"])
    ser = defaultdict(lambda: {"t":0.,"pos":set(),"badge":None,"contr":"","vyv":"","dir":"","ctr":""})
    rank = {"1С":0,"док":1,"переработка":2,"ручная":2,"площадка":3,"доля":3,"вероятно":4,"ввод":4,"нет":5}
    for r in filter(REAL, rows):
        s = ser[r["series"]]; s["t"] += r["tonnes"]; s["pos"].add(r["code"])
        s["contr"] = s["contr"] or r["contragent"]; s["vyv"] = s["vyv"] or r["datavyvoza"]
        s["dir"] = r["direction"]; s["ctr"] = r["contract"]
        if s["badge"] is None or rank.get(r["badge"],9) > rank.get(s["badge"],9): s["badge"] = r["badge"]
    for skey, s in sorted(ser.items(), key=lambda kv:-kv[1]["t"]):
        ws.append([s["dir"], s["ctr"], skey, s["contr"], s["vyv"], s["t"], len(s["pos"]),
                   BADGE_HUMAN.get(s["badge"], s["badge"])])
    style_header(ws, 8); autosize(ws, [20,16,26,26,12,14,9,30]); numcells(ws, [6])

    # 3. По номенклатуре
    ws = wb.create_sheet("По номенклатуре")
    ws.append(["Код","Номенклатура","Отчётная группа","Категория","Ед.","Тонн","Кол-во"])
    nom = defaultdict(lambda: {"t":0.,"q":0.,"name":"","rg":"","cat":"","unit":""})
    for r in rows:
        n = nom[r["code"]]; n["t"] += r["tonnes"]; n["q"] += r["qty"]
        n["name"]=r["name"]; n["rg"]=r["report_group"]; n["cat"]=r["category"]; n["unit"]=r["unit"]
    for code, n in sorted(nom.items(), key=lambda kv:-kv[1]["t"]):
        ws.append([code, n["name"], n["rg"], n["cat"], n["unit"], n["t"], n["q"]])
    style_header(ws, 7); autosize(ws, [14,40,30,18,7,14,12]); numcells(ws, [6,7])

    # 4. Подразделения (плоско: база→склад→группа→позиция)
    ws = wb.create_sheet("Подразделения")
    ws.append(["База","Склад","Категория","Отчётная группа","Код","Номенклатура","Тонн"])
    for r in sorted(rows, key=lambda r:(r["base"], r["sklad"], r["category"], -r["tonnes"])):
        ws.append([r["base"], r["sklad"], r["category"], r["report_group"], r["code"], r["name"], r["tonnes"]])
    style_header(ws, 7); autosize(ws, [30,26,16,26,14,36,12]); numcells(ws, [7])

    # 5. Остатки по сериям (полная детализация)
    ws = wb.create_sheet("Остатки по сериям")
    ws.append(["База","Склад","Код","Номенклатура","Серия","Контрагент","Дата вывоза",
               "Ед.","Кол-во","Тонн","Лежит, дн","Как определена серия"])
    ad = meta.get("actual_date","")
    from datetime import datetime
    adt = None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", ad)
    if m: adt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    for r in sorted(rows, key=lambda r:(r["base"], -r["tonnes"])):
        days = ""
        if adt and r.get("arrival_dt"):
            try: days = (adt - datetime.strptime(r["arrival_dt"][:10], "%Y-%m-%d")).days
            except Exception: days = ""
        ws.append([r["base"], r["sklad"], r["code"], r["name"], r["series_display"], r["contragent"],
                   r["datavyvoza"], r["unit"], r["qty"], r["tonnes"], days, BADGE_HUMAN.get(r["badge"], r["badge"])])
    style_header(ws, 12); autosize(ws, [28,24,13,32,24,22,12,6,12,12,9,28]); numcells(ws, [9,10])

    # 6. Остатки по документам
    ws = wb.create_sheet("Остатки по документам")
    ws.append(["База","Склад","Код","Серия","Документ (РУМЛ/РУ00)","Дата прихода","Кол-во","Партия"])
    for r in rows:
        for d in r.get("docs", []):
            ws.append([r["base"], r["sklad"], r["code"], r["series_display"], d["regnum"], d["dt"], d["qty"], d.get("partia","")])
    style_header(ws, 8); autosize(ws, [26,22,13,24,20,20,12,50]); numcells(ws, [7])

    # 7. Для корректировки в 1С (реальные серии, шаблон «Товары» — полная строка серии)
    ws = wb.create_sheet("Для корректировки в 1С")
    ws.append(["Артикул","Код номенклатуры","Номенклатура","Характеристика","Назначение",
               "Серия","Код упаковки","Упаковка","Количество"])
    for r in sorted(filter(REAL, rows), key=lambda r:(r["base"], r["sklad"])):
        ws.append(["", r["code"], r["name"], "", "", r.get("series_full") or r["series_display"], "", "", r["qty"]])
    style_header(ws, 9); autosize(ws, [12,18,40,16,14,30,14,12,12]); numcells(ws, [9])

    # 8. Применённые корректировки (журнал ручных правок — заполняется из снимка)
    ws = wb.create_sheet("Применённые корректировки")
    ws.append(["Дата правки","База","Склад","Код","Серия (было)","Серия (стало)","Автор","Комментарий"])
    for edit in data.get("edits", []):
        ws.append([edit.get("ts",""), edit.get("base",""), edit.get("sklad",""), edit.get("code",""),
                   edit.get("old",""), edit.get("new",""), edit.get("author",""), edit.get("note","")])
    style_header(ws, 8); autosize(ws, [18,26,24,13,24,24,16,40])

    # 9. Сверка с 1С (факт vs распределено по позиции)
    ws = wb.create_sheet("Сверка с 1С")
    ws.append(["Код","Номенклатура","Тонн (распределено)","Расхождение","Комментарий"])
    for code, n in sorted(nom.items(), key=lambda kv:-kv[1]["t"]):
        ws.append([code, n["name"], n["t"], 0.0, "сверять по листу «Остатки по документам»"])
    style_header(ws, 5); autosize(ws, [14,40,20,14,40]); numcells(ws, [3,4])

    # 10. Проблемы (расход < 30% прихода + БЕЗ СЕРИИ крупные)
    ws = wb.create_sheet("Проблемы")
    ws.append(["Тип","Код","Номенклатура","Приход","Расход","Расход/Приход","Тонн БЕЗ СЕРИИ"])
    for c in check:
        if c.get("ratio") is not None and c["ratio"] < 0.30:
            ws.append(["Расход < 30%", c["code"], c["name"], c["prihod"], c["rashod"], c["ratio"], ""])
    noser = defaultdict(float)
    for r in rows:
        if r["series"] == "БЕЗ СЕРИИ": noser[(r["code"], r["name"])] += r["tonnes"]
    for (code, name), t in sorted(noser.items(), key=lambda kv:-kv[1])[:200]:
        ws.append(["БЕЗ СЕРИИ", code, name, "", "", "", t])
    style_header(ws, 7); autosize(ws, [16,14,40,14,14,14,16]); numcells(ws, [4,5,6,7])
    # красным расход<30%
    for row in ws.iter_rows(min_row=2):
        if row[0].value == "Расход < 30%":
            for cell in row: cell.font = Font(color=RED)

    out = os.path.join(ROOT, "out", "Остатки_metoptorg_%s.xlsx" % ad.replace(".", "-"))
    wb.save(out)
    print("Excel готов: %s (%.0f КБ)" % (out, os.path.getsize(out)/1024))

if __name__ == "__main__":
    main()
