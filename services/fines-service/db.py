# -*- coding: utf-8 -*-
"""SQLite-хранилище: периоды (месяцы), штрафы, документы ФССП."""
import os
import sqlite3
from datetime import date, datetime

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "fines.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

MONTHS_RU = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    closed_at TEXT,
    UNIQUE(year, month)
);
CREATE TABLE IF NOT EXISTS fines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES periods(id),
    number TEXT UNIQUE,
    date TEXT,
    company TEXT,
    inn TEXT,
    vehicle TEXT,
    plate TEXT,
    article TEXT,
    violation TEXT,
    amount REAL,
    discount_amount REAL,
    violation_date TEXT,
    violation_time TEXT,
    violation_place TEXT,
    authority TEXT,
    employee TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Не оплачен',
    note TEXT DEFAULT '',
    pdf_name TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ip_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fine_id INTEGER REFERENCES fines(id),
    doc_type TEXT,
    title TEXT,
    ip_number TEXT,
    ip_date TEXT,
    doc_date TEXT,
    doc_number TEXT,
    fine_ref TEXT,
    debtor TEXT,
    debtor_inn TEXT,
    plate TEXT,
    vehicle TEXT,
    amount REAL,
    pdf_name TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def connect():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init():
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def period_name(p) -> str:
    return f"{MONTHS_RU[p['month']].capitalize()} {p['year']}"


def get_open_period(con, create=True):
    row = con.execute("SELECT * FROM periods WHERE status='open' ORDER BY year DESC, month DESC LIMIT 1").fetchone()
    if row or not create:
        return row
    today = date.today()
    con.execute("INSERT OR IGNORE INTO periods(year, month, status) VALUES(?,?,'open')", (today.year, today.month))
    con.execute("UPDATE periods SET status='open' WHERE year=? AND month=?", (today.year, today.month))
    con.commit()
    return get_open_period(con, create=False)


def close_period(con, period_id):
    con.execute("UPDATE periods SET status='closed', closed_at=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M"), period_id))
    con.commit()


def reopen_period(con, period_id):
    con.execute("UPDATE periods SET status='open', closed_at=NULL WHERE id=?", (period_id,))
    con.commit()


def open_next_period(con, year, month):
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    con.execute("INSERT OR IGNORE INTO periods(year, month, status) VALUES(?,?,'open')", (year, month))
    con.execute("UPDATE periods SET status='open' WHERE year=? AND month=?", (year, month))
    con.commit()


def adjacent_periods(con, period):
    """Соседние периоды для навигации: (прошлый, следующий) или None."""
    key = period["year"] * 100 + period["month"]
    prev = con.execute(
        "SELECT * FROM periods WHERE year*100+month < ? ORDER BY year DESC, month DESC LIMIT 1",
        (key,)).fetchone()
    nxt = con.execute(
        "SELECT * FROM periods WHERE year*100+month > ? ORDER BY year, month LIMIT 1",
        (key,)).fetchone()
    return prev, nxt


def period_stats(con, period_id) -> dict:
    row = con.execute("""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(amount),0) AS total,
               COALESCE(SUM(CASE WHEN status='Оплачен' THEN amount ELSE 0 END),0) AS paid_sum,
               SUM(CASE WHEN status='Оплачен' THEN 1 ELSE 0 END) AS paid_cnt
        FROM fines WHERE period_id=?""", (period_id,)).fetchone()
    ip = con.execute("""
        SELECT COUNT(DISTINCT f.id) AS cnt, COALESCE(SUM(f.amount),0) AS total FROM fines f
        WHERE f.period_id=? AND f.id IN (SELECT fine_id FROM ip_docs WHERE fine_id IS NOT NULL)""",
        (period_id,)).fetchone()
    cnt, total = row["cnt"], row["total"]
    paid_cnt = row["paid_cnt"] or 0
    return {
        "cnt": cnt, "total": total,
        "avg": (total / cnt) if cnt else 0,
        "paid_cnt": paid_cnt, "paid_sum": row["paid_sum"],
        "paid_pct": (row["paid_sum"] / total * 100) if total else 0,
        "unpaid_cnt": cnt - paid_cnt,
        "unpaid_sum": total - row["paid_sum"],
        "unpaid_pct": ((total - row["paid_sum"]) / total * 100) if total else 0,
        "ip_cnt": ip["cnt"], "ip_sum": ip["total"],
    }


def period_breakdowns(con, period_id) -> dict:
    """Статистика месяца: какие нарушения, автомобили, компании, водители дают больше всего штрафов."""
    total_cnt = con.execute("SELECT COUNT(*) FROM fines WHERE period_id=?", (period_id,)).fetchone()[0] or 1

    def query(label_sql, order="cnt DESC, total DESC", limit=8):
        rows = con.execute(f"""
            SELECT {label_sql} AS label, COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total
            FROM fines WHERE period_id=? GROUP BY label ORDER BY {order} LIMIT {limit}""",
            (period_id,)).fetchall()
        return [{"label": r["label"], "cnt": r["cnt"], "total": r["total"],
                 "share": r["cnt"] / total_cnt * 100} for r in rows]

    return {
        # превышения на разное число км/ч — один вид нарушения
        "violations": query("""CASE WHEN violation LIKE 'Превышение скорости%' THEN 'Превышение скорости'
                               ELSE COALESCE(NULLIF(violation,''),'Не указано') END"""),
        "vehicles": query("TRIM(COALESCE(vehicle,'') || ' ' || COALESCE(plate,''))"),
        "companies": query("COALESCE(NULLIF(company,''),'Не указана')"),
        "employees": query("COALESCE(NULLIF(employee,''),'Не указан')"),
        "articles": query("COALESCE(NULLIF(article,''),'Не указана')"),
    }


def fines_of_period(con, period_id):
    fines = con.execute(
        "SELECT * FROM fines WHERE period_id=? ORDER BY date, id", (period_id,)).fetchall()
    result = []
    for f in fines:
        docs = con.execute(
            "SELECT * FROM ip_docs WHERE fine_id=? ORDER BY doc_date, id", (f["id"],)).fetchall()
        result.append({"fine": f, "ip_docs": docs})
    return result


def find_fine_for_ip(con, data):
    """Поиск штрафа для привязки документа ФССП: по номеру постановления, затем по госномеру+сумме."""
    ref = data.get("fine_ref")
    if ref:
        row = con.execute("SELECT * FROM fines WHERE number=?", (ref,)).fetchone()
        if row:
            return row
    plate, amount = data.get("plate"), data.get("amount")
    if plate and amount:
        rows = con.execute(
            "SELECT * FROM fines WHERE plate=? AND amount=? ORDER BY date DESC", (plate, amount)).fetchall()
        if len(rows) == 1:
            return rows[0]
    return None
