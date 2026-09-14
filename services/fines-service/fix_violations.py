# -*- coding: utf-8 -*-
"""Разовая перепроверка вида нарушения по PDF: python fix_violations.py

Обновляет только автоматически определённые значения (ручные правки не трогает),
дозаполняет пустые дату/время/место нарушения и орган.
"""
import os
import db
import parser as P

AUTO = set(P.ARTICLE_MAP.values()) | {l for _, l in P.VIOLATION_MAP if l} | {
    "Нарушение правил остановки/стоянки", "Размещение ТС на газоне (объекте озеленения)"}

con = db.connect()
fixed = dates = 0
for f in con.execute("SELECT * FROM fines WHERE pdf_name IS NOT NULL").fetchall():
    path = os.path.join(db.UPLOAD_DIR, f["pdf_name"])
    if not os.path.exists(path):
        continue
    r = P.parse_pdf(path)
    if r["kind"] != "fine":
        continue
    d = r["data"]
    cur = f["violation"] or ""
    is_auto = cur in AUTO or cur.startswith("Превышение скорости") or cur.startswith("Перегруз")
    if d["violation"] and d["violation"] != cur and is_auto:
        con.execute("UPDATE fines SET violation=? WHERE id=?", (d["violation"], f["id"]))
        print(f"#{f['id']} {f['number']}: {cur} -> {d['violation']}")
        fixed += 1
    if d["violation_date"] and not f["violation_date"]:
        con.execute("UPDATE fines SET violation_date=?, violation_time=?, violation_place=? WHERE id=?",
                    (d["violation_date"], d["violation_time"], d["violation_place"], f["id"]))
        dates += 1
    if d.get("authority") and not f["authority"]:
        con.execute("UPDATE fines SET authority=? WHERE id=?", (d["authority"], f["id"]))
con.commit()
con.close()
print(f"исправлено нарушений: {fixed} | дозаполнено дат нарушения: {dates}")
