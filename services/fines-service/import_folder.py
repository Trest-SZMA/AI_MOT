# -*- coding: utf-8 -*-
"""Разовый импорт всех PDF из папки в отчет: python import_folder.py /path/to/folder"""
import glob
import os
import sys

import db
from app import ingest_pdf

folder = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db.init()
con = db.connect()
period = db.get_open_period(con)
print(f"Импорт в период: {db.period_name(period)}\n")

pdfs = sorted(glob.glob(os.path.join(folder, "*.pdf")))
if not pdfs:
    print("PDF-файлы не найдены в", folder)
    sys.exit(1)

icons = {"ok": "✓", "dup": "≡", "warn": "⚠", "error": "✗"}
for path in pdfs:
    with open(path, "rb") as fh:
        content = fh.read()
    try:
        r = ingest_pdf(con, os.path.basename(path), content, period["id"])
    except Exception as e:
        r = {"status": "error", "msg": str(e)}
    print(f"{icons.get(r['status'], '?')} {os.path.basename(path)}\n   {r['msg']}")

stats = db.period_stats(con, period["id"])
print(f"\nИтого в отчете: {stats['cnt']} штрафов на {stats['total']:,.0f} руб., с ИП: {stats['ip_cnt']}".replace(",", " "))
con.close()
