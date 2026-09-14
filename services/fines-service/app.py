# -*- coding: utf-8 -*-
"""Отчёт по штрафам: загрузка постановлений, автопривязка ИП, экспорт Excel/Word."""
import os
import re
import secrets
import shutil
import unicodedata
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature

import db
import exports
import parser as pdfparser

BASE = os.path.dirname(__file__)
# Пустой APP_PASSWORD = вход без пароля; задайте пароль в окружении, чтобы включить авторизацию
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    key_file = os.path.join(db.DATA_DIR, ".secret_key")
    os.makedirs(db.DATA_DIR, exist_ok=True)
    if os.path.exists(key_file):
        SECRET_KEY = open(key_file).read().strip()
    else:
        SECRET_KEY = secrets.token_hex(32)
        with open(key_file, "w") as fh:
            fh.write(SECRET_KEY)

signer = URLSafeSerializer(SECRET_KEY, salt="session")
app = FastAPI(title="Отчет по штрафам")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
templates.env.globals["period_name"] = db.period_name
templates.env.globals["auth_enabled"] = AUTH_ENABLED
# версия статики: меняется при каждом обновлении файлов, чтобы браузеры не держали старый CSS/JS
templates.env.globals["static_v"] = str(int(max(
    os.path.getmtime(os.path.join(BASE, "static", f)) for f in ("style.css", "table.js"))))
# ссылка с логотипа — в основной портал компании
templates.env.globals["portal_url"] = os.environ.get("PORTAL_URL", "http://192.168.6.157:8079")


def fmt_money(v):
    if v is None:
        return ""
    return f"{v:,.0f}".replace(",", " ")


def fmt_date(iso):
    if not iso:
        return ""
    p = str(iso).split("-")
    return f"{p[2]}.{p[1]}.{p[0]}" if len(p) == 3 else iso


templates.env.filters["money"] = fmt_money
templates.env.filters["dmy"] = fmt_date

db.init()


# ---------- auth ----------

def is_authed(request: Request) -> bool:
    token = request.cookies.get("session")
    if not token:
        return False
    try:
        return signer.loads(token).get("auth") is True
    except BadSignature:
        return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/favicon.ico") or is_authed(request):
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, APP_PASSWORD):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", signer.dumps({"auth": True}),
                        httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
        return resp
    return templates.TemplateResponse(request, "login.html", {"error": "Неверный пароль"})


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------- helpers ----------

def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", os.path.basename(name or "doc.pdf"))
    name = re.sub(r"[^\w\-. ()№]", "_", name, flags=re.UNICODE)
    return name[:180] or "doc.pdf"


def store_pdf(upload_name: str, content: bytes) -> str:
    name = safe_filename(upload_name)
    path = os.path.join(db.UPLOAD_DIR, name)
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(path):
        name = f"{stem}_{i}{ext}"
        path = os.path.join(db.UPLOAD_DIR, name)
        i += 1
    with open(path, "wb") as fh:
        fh.write(content)
    return name


def ingest_pdf(con, filename: str, content: bytes, period_id: int) -> dict:
    """Разбор одного PDF и запись в БД. Возвращает результат для отображения."""
    tmp = os.path.join(db.UPLOAD_DIR, ".tmp_ingest.pdf")
    with open(tmp, "wb") as fh:
        fh.write(content)
    try:
        parsed = pdfparser.parse_pdf(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    kind, data = parsed["kind"], parsed["data"]

    if kind == "fine":
        if not data.get("number"):
            return {"file": filename, "status": "error",
                    "msg": "Не удалось определить номер постановления — добавьте вручную"}
        existing = con.execute("SELECT id, period_id FROM fines WHERE number=?", (data["number"],)).fetchone()
        if existing:
            return {"file": filename, "status": "dup",
                    "msg": f"Штраф № {data['number']} уже есть в отчете", "fine_id": existing["id"]}
        pdf_name = store_pdf(filename, content)
        cur = con.execute("""
            INSERT INTO fines(period_id, number, date, company, inn, vehicle, plate, article,
                              violation, amount, discount_amount, violation_date, violation_time,
                              violation_place, authority, pdf_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            period_id, data["number"], data["date"], data["company"], data["inn"],
            data["vehicle"], data["plate"], data["article"], data["violation"],
            data["amount"], data["discount_amount"], data["violation_date"],
            data["violation_time"], data["violation_place"], data["authority"], pdf_name))
        con.commit()
        # возможно, ранее загруженные документы ФССП ждут этот штраф
        linked = 0
        for d in con.execute("SELECT * FROM ip_docs WHERE fine_id IS NULL AND fine_ref=?",
                             (data["number"],)).fetchall():
            con.execute("UPDATE ip_docs SET fine_id=? WHERE id=?", (cur.lastrowid, d["id"]))
            linked += 1
        if linked:
            con.execute("UPDATE fines SET status='ИП возбуждено' WHERE id=? AND status='Не оплачен'",
                        (cur.lastrowid,))
        con.commit()
        msg = f"Штраф № {data['number']} — {data.get('vehicle') or ''} {data.get('plate') or ''}, {fmt_money(data.get('amount'))} руб."
        if linked:
            msg += f" (привязано документов ФССП: {linked})"
        return {"file": filename, "status": "ok", "msg": msg, "fine_id": cur.lastrowid}

    if kind == "fssp":
        dup = None
        if data.get("doc_number"):
            dup = con.execute("SELECT id FROM ip_docs WHERE doc_number=?", (data["doc_number"],)).fetchone()
        if dup:
            return {"file": filename, "status": "dup", "msg": f"Документ ФССП № {data['doc_number']} уже загружен"}
        fine = db.find_fine_for_ip(con, data)
        pdf_name = store_pdf(filename, content)
        con.execute("""
            INSERT INTO ip_docs(fine_id, doc_type, title, ip_number, ip_date, doc_date, doc_number,
                                fine_ref, debtor, debtor_inn, plate, vehicle, amount, pdf_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            fine["id"] if fine else None, data["doc_type"], data["title"], data["ip_number"],
            data["ip_date"], data["doc_date"], data["doc_number"], data["fine_ref"],
            data["debtor"], data.get("debtor_inn"), data["plate"], data["vehicle"],
            data["amount"], pdf_name))
        if fine:
            con.execute("UPDATE fines SET status='ИП возбуждено' WHERE id=? AND status IN ('Не оплачен','')",
                        (fine["id"],))
        con.commit()
        if fine:
            return {"file": filename, "status": "ok",
                    "msg": f"{data['doc_type']} (ИП № {data.get('ip_number') or '—'}) привязан к штрафу № {fine['number']}",
                    "fine_id": fine["id"]}
        return {"file": filename, "status": "warn",
                "msg": f"{data['doc_type']} (ИП № {data.get('ip_number') or '—'}) загружен, но штраф № {data.get('fine_ref') or '?'} "
                       f"не найден в отчетах — привяжите вручную или загрузите постановление о штрафе"}

    return {"file": filename, "status": "error",
            "msg": "Не удалось распознать тип документа (нет текстового слоя?) — добавьте данные вручную"}


# ---------- pages ----------

def _period_context(con, period) -> dict:
    """Всё, что нужно странице месяца: строки, сводка, статистика, соседние месяцы."""
    prev_p, next_p = db.adjacent_periods(con, period)
    return {
        "period": period,
        "items": db.fines_of_period(con, period["id"]),
        "stats": db.period_stats(con, period["id"]),
        "breakdowns": db.period_breakdowns(con, period["id"]),
        "prev_p": prev_p, "next_p": next_p,
        "prev_stats": db.period_stats(con, prev_p["id"]) if prev_p else None,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, results: str = ""):
    con = db.connect()
    period = db.get_open_period(con)
    ctx = _period_context(con, period)
    unlinked = con.execute("SELECT COUNT(*) FROM ip_docs WHERE fine_id IS NULL").fetchone()[0]
    con.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        **ctx, "unlinked": unlinked, "active": "dashboard",
    })


@app.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    con = db.connect()
    period = db.get_open_period(con)
    results = []
    for uf in files:
        content = await uf.read()
        if not content:
            continue
        if not (uf.filename or "").lower().endswith(".pdf"):
            results.append({"file": uf.filename, "status": "error", "msg": "Не PDF-файл"})
            continue
        try:
            results.append(ingest_pdf(con, uf.filename, content, period["id"]))
        except Exception as e:
            results.append({"file": uf.filename, "status": "error", "msg": f"Ошибка обработки: {e}"})
    ctx = _period_context(con, period)
    unlinked = con.execute("SELECT COUNT(*) FROM ip_docs WHERE fine_id IS NULL").fetchone()[0]
    con.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        **ctx, "unlinked": unlinked, "results": results, "active": "dashboard",
    })


@app.get("/periods", response_class=HTMLResponse)
async def periods(request: Request):
    con = db.connect()
    rows = con.execute("SELECT * FROM periods ORDER BY year DESC, month DESC").fetchall()
    plist = [{"p": p, "stats": db.period_stats(con, p["id"])} for p in rows]
    con.close()
    return templates.TemplateResponse(request, "periods.html", {"plist": plist, "active": "periods"})


@app.get("/period/{pid}", response_class=HTMLResponse)
async def period_view(request: Request, pid: int):
    con = db.connect()
    period = con.execute("SELECT * FROM periods WHERE id=?", (pid,)).fetchone()
    if not period:
        con.close()
        return RedirectResponse("/periods", status_code=303)
    ctx = _period_context(con, period)
    con.close()
    return templates.TemplateResponse(request, "period.html", {**ctx, "active": "periods"})


@app.post("/period/{pid}/close")
async def period_close(pid: int, back: str = Form("")):
    con = db.connect()
    p = con.execute("SELECT * FROM periods WHERE id=?", (pid,)).fetchone()
    if p:
        db.close_period(con, pid)
        # следующий месяц открываем только если после закрытия не осталось ни одного открытого
        # (при закрытии переоткрытого старого месяца текущий уже открыт — его не трогаем)
        if not db.get_open_period(con, create=False):
            db.open_next_period(con, p["year"], p["month"])
    con.close()
    return RedirectResponse(f"/period/{pid}" if back == "period" else "/", status_code=303)


@app.post("/period/{pid}/reopen")
async def period_reopen(pid: int):
    con = db.connect()
    db.reopen_period(con, pid)
    con.close()
    return RedirectResponse(f"/period/{pid}", status_code=303)


@app.get("/fine/{fid}", response_class=HTMLResponse)
async def fine_view(request: Request, fid: int):
    con = db.connect()
    fine = con.execute("SELECT * FROM fines WHERE id=?", (fid,)).fetchone()
    if not fine:
        con.close()
        return RedirectResponse("/", status_code=303)
    docs = con.execute("SELECT * FROM ip_docs WHERE fine_id=? ORDER BY doc_date, id", (fid,)).fetchall()
    period = con.execute("SELECT * FROM periods WHERE id=?", (fine["period_id"],)).fetchone()
    con.close()
    return templates.TemplateResponse(request, "fine.html", {
        "fine": fine, "docs": docs, "period": period, "active": "dashboard"})


def _parse_date_input(value: str):
    value = value.strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Дата в формате ДД.ММ.ГГГГ")


def _parse_amount_input(value: str):
    value = value.strip().replace(" ", "").replace(" ", "").replace(",", ".").replace("руб.", "").rstrip("р.")
    if not value:
        return None
    return float(value)


@app.post("/fine/{fid}/update")
async def fine_update(fid: int, date: str = Form(""), violation_date: str = Form(""),
                      number: str = Form(""),
                      company: str = Form(""), inn: str = Form(""), vehicle: str = Form(""),
                      plate: str = Form(""), article: str = Form(""), violation: str = Form(""),
                      amount: str = Form(""), employee: str = Form(""),
                      status: str = Form("Не оплачен"), note: str = Form("")):
    con = db.connect()
    try:
        iso_date = _parse_date_input(date)
        iso_vdate = _parse_date_input(violation_date)
        amt = _parse_amount_input(amount)
    except ValueError:
        con.close()
        return RedirectResponse(f"/fine/{fid}", status_code=303)
    number = number.strip() or None
    if number:
        dup = con.execute("SELECT id FROM fines WHERE number=? AND id<>?", (number, fid)).fetchone()
        if dup:
            con.close()
            return RedirectResponse(f"/fine/{fid}", status_code=303)
    con.execute("""UPDATE fines SET date=?, violation_date=?, number=?, company=?, inn=?,
                   vehicle=?, plate=?, article=?, violation=?, amount=?, employee=?, status=?,
                   note=? WHERE id=?""",
                (iso_date, iso_vdate, number, company.strip(), inn.strip(), vehicle.strip(),
                 plate.strip().upper(), article.strip(), violation.strip(), amt,
                 employee.strip(), status, note.strip(), fid))
    con.commit()
    con.close()
    return RedirectResponse(f"/fine/{fid}", status_code=303)


EDITABLE_FIELDS = {"date", "violation_date", "number", "company", "inn", "vehicle", "plate",
                   "article", "violation", "amount", "employee", "status", "note"}


@app.post("/fine/{fid}/field")
async def fine_field(fid: int, field: str = Form(...), value: str = Form("")):
    """Правка одного поля из таблицы (двойной клик по ячейке)."""
    from fastapi.responses import JSONResponse
    if field not in EDITABLE_FIELDS:
        return JSONResponse({"ok": False, "error": "Недопустимое поле"}, status_code=400)
    con = db.connect()
    fine = con.execute("SELECT * FROM fines WHERE id=?", (fid,)).fetchone()
    if not fine:
        con.close()
        return JSONResponse({"ok": False, "error": "Штраф не найден"}, status_code=404)
    value = value.strip()
    try:
        if field in ("date", "violation_date"):
            stored = _parse_date_input(value)
            display, sort = fmt_date(stored), (stored or "")
        elif field == "amount":
            stored = _parse_amount_input(value)
            display, sort = fmt_money(stored), (stored if stored is not None else 0)
        elif field == "number":
            stored = value or None
            if stored:
                dup = con.execute("SELECT id FROM fines WHERE number=? AND id<>?", (stored, fid)).fetchone()
                if dup:
                    con.close()
                    return JSONResponse({"ok": False, "error": f"Штраф № {stored} уже есть в отчете"}, status_code=409)
            display, sort = stored or "—", (stored or "")
        else:
            if field == "plate":
                value = value.upper()
            stored = value
            display, sort = (stored or "—"), stored.lower()
    except ValueError as e:
        con.close()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
    con.execute(f"UPDATE fines SET {field}=? WHERE id=?", (stored, fid))
    con.commit()
    con.close()
    return JSONResponse({"ok": True, "display": display, "sort": sort})


@app.post("/fine/{fid}/status")
async def fine_status(fid: int, status: str = Form(...)):
    con = db.connect()
    con.execute("UPDATE fines SET status=? WHERE id=?", (status, fid))
    con.commit()
    con.close()
    return RedirectResponse("/", status_code=303)


@app.post("/fine/{fid}/delete")
async def fine_delete(fid: int):
    con = db.connect()
    con.execute("UPDATE ip_docs SET fine_id=NULL WHERE fine_id=?", (fid,))
    con.execute("DELETE FROM fines WHERE id=?", (fid,))
    con.commit()
    con.close()
    return RedirectResponse("/", status_code=303)


@app.get("/ip/unlinked", response_class=HTMLResponse)
async def ip_unlinked(request: Request):
    con = db.connect()
    docs = con.execute("SELECT * FROM ip_docs WHERE fine_id IS NULL ORDER BY id DESC").fetchall()
    fines = con.execute("""SELECT f.id, f.number, f.vehicle, f.plate, f.amount FROM fines f
                           ORDER BY f.date DESC LIMIT 300""").fetchall()
    con.close()
    return templates.TemplateResponse(request, "unlinked.html", {
        "docs": docs, "fines": fines, "active": "unlinked"})


@app.post("/ip/{did}/link")
async def ip_link(did: int, fine_id: int = Form(...)):
    con = db.connect()
    con.execute("UPDATE ip_docs SET fine_id=? WHERE id=?", (fine_id, did))
    con.execute("UPDATE fines SET status='ИП возбуждено' WHERE id=? AND status='Не оплачен'", (fine_id,))
    con.commit()
    con.close()
    return RedirectResponse("/ip/unlinked", status_code=303)


@app.post("/ip/{did}/create_fine")
async def ip_create_fine(did: int):
    """Создать запись штрафа из данных постановления ФССП и привязать к ней документы."""
    con = db.connect()
    doc = con.execute("SELECT * FROM ip_docs WHERE id=?", (did,)).fetchone()
    if not doc:
        con.close()
        return RedirectResponse("/ip/unlinked", status_code=303)

    existing = None
    if doc["fine_ref"]:
        existing = con.execute("SELECT id FROM fines WHERE number=?", (doc["fine_ref"],)).fetchone()
    if existing:
        fine_id = existing["id"]
    else:
        details = {"date": None, "article": None, "violation": None}
        if doc["pdf_name"]:
            path = os.path.join(db.UPLOAD_DIR, doc["pdf_name"])
            if os.path.exists(path):
                try:
                    details = pdfparser.fssp_fine_details(
                        pdfparser.extract_text(path), doc["fine_ref"])
                except Exception:
                    pass
        period = db.get_open_period(con)
        cur = con.execute("""
            INSERT INTO fines(period_id, number, date, company, vehicle, plate, article,
                              violation, amount, status, note)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
            period["id"], doc["fine_ref"], details["date"], doc["debtor"], doc["vehicle"],
            doc["plate"], details["article"], details["violation"], doc["amount"],
            "ИП возбуждено",
            f"Создан автоматически из постановления ФССП (ИП № {doc['ip_number'] or '—'})"))
        fine_id = cur.lastrowid

    if doc["fine_ref"]:
        con.execute("UPDATE ip_docs SET fine_id=? WHERE fine_id IS NULL AND fine_ref=?",
                    (fine_id, doc["fine_ref"]))
    else:
        con.execute("UPDATE ip_docs SET fine_id=? WHERE id=?", (fine_id, did))
    con.commit()
    con.close()
    return RedirectResponse(f"/fine/{fine_id}", status_code=303)


@app.post("/ip/{did}/delete")
async def ip_delete(did: int):
    con = db.connect()
    con.execute("DELETE FROM ip_docs WHERE id=?", (did,))
    con.commit()
    con.close()
    return RedirectResponse("/ip/unlinked", status_code=303)


@app.post("/ip/{did}/unlink")
async def ip_unlink(did: int):
    con = db.connect()
    row = con.execute("SELECT fine_id FROM ip_docs WHERE id=?", (did,)).fetchone()
    con.execute("UPDATE ip_docs SET fine_id=NULL WHERE id=?", (did,))
    con.commit()
    fid = row["fine_id"] if row else None
    con.close()
    return RedirectResponse(f"/fine/{fid}" if fid else "/", status_code=303)


# ---------- files & exports ----------

@app.get("/pdf/{kind}/{item_id}")
async def get_pdf(kind: str, item_id: int):
    con = db.connect()
    table = "fines" if kind == "fine" else "ip_docs"
    row = con.execute(f"SELECT pdf_name FROM {table} WHERE id=?", (item_id,)).fetchone()
    con.close()
    if not row or not row["pdf_name"]:
        return Response("Файл не найден", status_code=404)
    path = os.path.join(db.UPLOAD_DIR, row["pdf_name"])
    if not os.path.exists(path):
        return Response("Файл не найден", status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=row["pdf_name"])


def _export(pid: int, fmt: str):
    con = db.connect()
    period = con.execute("SELECT * FROM periods WHERE id=?", (pid,)).fetchone()
    if not period:
        con.close()
        return Response("Период не найден", status_code=404)
    items = db.fines_of_period(con, pid)
    stats = db.period_stats(con, pid)
    breakdowns = db.period_breakdowns(con, pid)
    prev_p, _ = db.adjacent_periods(con, period)
    prev = {"name": db.period_name(prev_p), "stats": db.period_stats(con, prev_p["id"])} if prev_p else None
    con.close()
    title = f"Штрафы {db.MONTHS_RU[period['month']]} {period['year']}"
    fname = f"Отчет по штрафам за {db.MONTHS_RU[period['month']]} {period['year']}"
    if fmt == "xlsx":
        data = exports.make_xlsx(title, items, stats, breakdowns, prev)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fname += ".xlsx"
    else:
        data = exports.make_docx(f"{db.MONTHS_RU[period['month']].capitalize()} {period['year']}",
                                 items, stats, breakdowns, prev)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        fname += ".docx"
    from urllib.parse import quote
    return Response(data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@app.get("/export/{pid}/xlsx")
async def export_xlsx(pid: int):
    return _export(pid, "xlsx")


@app.get("/export/{pid}/docx")
async def export_docx(pid: int):
    return _export(pid, "docx")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8077")))
