#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельная автовыгрузка «Проверка_БП» из Битрикса для сервиса реализации.

Заказано 21.08.2026: «данные, которые мы берём по ссылке на все БП, должны
обновляться автоматически раз в неделю; ссылка не меняется».

Скрипт НЕ парсит ничего сам — он вызывает соседний «Парсер БП»
(/opt/parser-bp: scan_bitrix_link обходит публичную папку Битрикса с
подпапками и пагинацией, build_workbook собирает тот самый
«Проверка_БП_*.xlsx» с листом «Проверка месяцев», который ест load_bp_months).

⚠️ ПРЕДОХРАНИТЕЛЬ, как у ночной сборки: если Битрикс отдал заметно меньше
строк, чем в прошлый раз (обрыв сети, смена ссылки, пустая папка), СТАРЫЙ файл
остаётся на месте, а скрипт выходит с ошибкой — журнал таймера её покажет.
Молчаливая подмена полного свода огрызком стоила бы планов вывоза сотням БП.

Запуск: systemd-таймер metoptorg-bp-weekly.timer (вс 03:10, до ночной сборки
04:03 — свежий файл подхватится ею в то же утро).
"""
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, "/opt/parser-bp")
from bitrix_link import scan_bitrix_link            # noqa: E402
from export import build_workbook                   # noqa: E402

URL = os.environ.get("BP_FOLDER_URL", "https://team.rosmetrade.ru/~PZxJj")
DATA = "/opt/metoptorg-realizaciya/data"
PATTERN = os.path.join(DATA, "Проверка_БП_*.xlsx")
KEEP = 3                                # сколько прошлых выгрузок оставлять
MIN_ROWS_ABS = 500                      # свод меньше этого — точно обрыв
MIN_RATIO = 0.7                         # и меньше 70 % прошлого — тоже


def main():
    prev = sorted(glob.glob(PATTERN))
    print("Обход папки Битрикса:", URL, flush=True)
    report = scan_bitrix_link(URL, recursive=True)
    rows = len(report.rows)
    print("строк свода: %d, пропущено файлов: %d" % (rows, len(report.skipped)))

    # предохранитель: сравниваем с прошлой выгрузкой по числу строк
    prev_rows = 0
    if prev:
        import openpyxl
        ws = openpyxl.load_workbook(prev[-1], read_only=True).worksheets[0]
        prev_rows = max(0, ws.max_row - 1)
    if rows < MIN_ROWS_ABS or (prev_rows and rows < prev_rows * MIN_RATIO):
        sys.exit("ОБРЫВ: свод %d строк против %d в прошлой выгрузке (%s) — "
                 "старый файл оставлен, новый НЕ записан"
                 % (rows, prev_rows, os.path.basename(prev[-1]) if prev else "—"))

    day = datetime.date.today().isoformat()
    dst = os.path.join(DATA, "Проверка_БП_Битрикс_%s.xlsx" % day)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(build_workbook(report, folder=URL))
    os.replace(tmp, dst)                # атомарно: полуфайл не подсунем

    # ---- ВЕРСИИ РАСЧЁТОВ ДЛЯ ВКЛАДКИ «ВЕРСИИ БП» (25.08.2026) ----
    # Экономисты отмечают на дашборде, какая версия расчёта верна; для этого
    # вкладке нужны все версии с их выручкой («Выручка без НДС», добавлена в
    # parser_core тем же правилом, что прибыль), прибылью и признаками лук/дсп.
    vdst = os.path.join(DATA, "БП_версии_%s.json" % day)
    rows = [{
        "name": r.bp_name, "sheet": r.sheet_name, "file": r.file_name,
        "months": r.months_by_rows, "status": r.status,
        "lukoil": r.has_lukoil, "dsp": r.has_dsp,
        "profit": r.net_profit,
        "revenue": getattr(r, "revenue", None),
        # ПЛАНОВЫЕ ПОЗИЦИИ версии: номенклатура текстом Лукойла, место хранения,
        # объём и цена — вторая половина мостика «план Excel <-> факт 1С».
        # Режем до 120 позиций на версию: файл и так 1,7 МБ, а хвост длинных
        # простыней в отчёте всё равно не читают.
        "items": (getattr(r, "plan_items", None) or [])[:120],
    } for r in report.rows]
    with open(vdst + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.datetime.now().isoformat(timespec="minutes"),
                   "source": URL, "rows": rows}, f, ensure_ascii=False)
    os.replace(vdst + ".tmp", vdst)
    print("записан:", vdst, "(%d версий)" % len(rows))

    try:
        import pwd
        u = pwd.getpwnam("metoptorg")
        for f_ in (dst, vdst):
            os.chown(f_, u.pw_uid, u.pw_gid)
    except Exception:
        pass
    print("записан:", dst)

    # прошлые выгрузки: оставляем KEEP последних, остальное убираем
    for pat in (PATTERN, os.path.join(DATA, "БП_версии_*.json")):
        for old in sorted(glob.glob(pat))[:-KEEP]:
            os.remove(old)
            print("удалён старый:", os.path.basename(old))


if __name__ == "__main__":
    main()
