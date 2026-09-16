"""Сервис подготовки бизнес-планов МетОптТорг (v2 — модель по реальному БП).

Запуск:
    uvicorn app.main:app --reload   →  http://localhost:8000

Вход по логину и паролю (app/auth.py): роль задана учётной записью, сессия
живёт в таблице `user_sessions`, каждое действие пишется в журнал `audit_log`
под логином автора.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.templating import Jinja2Templates

from . import (assist, audit, auth, book_archive, transport_km, bp_types, deals, calc, cost_matrix, fact_costs, fact_import, fact_model, factsnap, forms, geo, norm_calib, outcome, type_margin,
               list_import, loading, logistics, origin, refsources,
               matcher, norms, pricing, rates, readiness, versions, workflow)
from .db import ATTACH_DIR, BASE_DIR, OUTPUT_DIR, connect, get_setting, init_db, log
from .docgen.bp_docx import build_bp_docx
from .docgen.bp_pdf import build_bp_pdf
from .docgen.bp_xlsx import build_bp_xlsx
from .docgen.upload_tables import build_upload_tables

app = FastAPI(title="МетОптТорг · Бизнес-планы")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

init_db()


def _bootstrap_admin() -> None:
    """Первый запуск: если ни у одной учётной записи нет пароля — завести
    вход администратора и показать пароль в логе (в базе только хеш)."""
    conn = connect()
    try:
        password = auth.ensure_admin(conn)
        conn.commit()
    finally:
        conn.close()
    if password:
        print("\n" + "=" * 62 +
              "\n  Создана учётная запись администратора: login = admin"
              f"\n  Пароль: {password}"
              "\n  Пароль потребуется сменить при первом входе."
              "\n" + "=" * 62 + "\n", flush=True)


_bootstrap_admin()

# Пути, которые сами ведут историю или не меняют расчёт: снимок не нужен.
# «/status» пишет свою версию с комментарием решения, «/role» вне БП,
# фото и загрузки вложений расчёт не трогают.
NO_AUTOVERSION = ("/status", "/photos", "/attachments")


@app.middleware("http")
async def autoversion(request: Request, call_next):
    """Автоверсия расчёта после каждой успешной правки БП.

    Единая точка вместо снимка в каждом из ~40 POST-роутов: после успешного
    POST /bp/{id}/… состояние БП сравнивается с последней версией и, если
    данные изменились, пишется новая (save_version дедуплицирует по хешу)."""
    response = await call_next(request)
    if request.method != "POST" or response.status_code >= 400:
        return response
    m = re.match(r"^/bp/(\d+)(/.*)?$", request.url.path)
    if not m or any(request.url.path.endswith(p) for p in NO_AUTOVERSION):
        return response
    bp_id, tail = int(m.group(1)), (m.group(2) or "/")
    conn = connect()
    try:
        save_version(conn, bp_id, actor_name(request),
                     change_note=tail.strip("/") or "карточка")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()                  # версия не должна ломать саму правку
    finally:
        conn.close()
    return response


# Страницы, доступные без входа: сама форма входа и проверка живости сервиса.
PUBLIC_PATHS = {"/login", "/healthz"}
# Пути, разрешённые пользователю, которому предписана смена пароля.
PASSWORD_PATHS = {"/password", "/logout"}


def _parse_networks(raw: str) -> list:
    """Список сетей из переменной окружения BP_ALLOWED_NETWORKS.

    «192.168.6.0/24, 10.0.0.0/8» — кому сервис отвечает вообще. Пустое
    значение = не ограничивать (сервис закрыт только паролем)."""
    nets = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            print(f"BP_ALLOWED_NETWORKS: «{part}» — не сеть и не адрес, пропущено",
                  flush=True)
    return nets


ALLOWED_NETWORKS = _parse_networks(os.environ.get("BP_ALLOWED_NETWORKS", ""))
if ALLOWED_NETWORKS:
    print("Доступ разрешён только из сетей: "
          + ", ".join(str(n) for n in ALLOWED_NETWORKS), flush=True)


def _network_allowed(ip: str) -> bool:
    if not ALLOWED_NETWORKS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback:                 # сам сервер: обслуживание и проверки
        return True
    return any(addr in net for net in ALLOWED_NETWORKS)


@app.middleware("http")
async def restrict_network(request: Request, call_next):
    """Ответ только своей сети, если она задана в BP_ALLOWED_NETWORKS.

    Второй рубеж после пароля: сервис слушает 0.0.0.0, и в сети предприятия
    его порт виден всем. Объявлен последним, поэтому проверяется первым —
    чужой адрес не доходит ни до входа, ни до данных.

    Адрес берётся тем же способом, что и для журнала: за nginx (HTTPS на
    8443) соединение приходит с loopback, и проверка адреса соединения
    пропускала бы кого угодно. X-Forwarded-For принимается только от
    локального прокси, поэтому подделать адрес снаружи нельзя."""
    if not _network_allowed(_client_ip(request)):
        return PlainTextResponse("Доступ из вашей сети закрыт.", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Проверка сессии до всего остального.

    Объявлен после autoversion, поэтому оборачивает его снаружи: запрос без
    входа до правки БП и до записи версии не доходит. Пользователь сессии
    кладётся в request.state.user — из него берутся роль и автор журнала."""
    request.state.user = None
    path = request.url.path
    conn = connect()
    try:
        user = auth.session_user(conn, request.cookies.get(auth.SESSION_COOKIE))
    finally:
        conn.close()
    request.state.user = user

    if user is None and path not in PUBLIC_PATHS:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Требуется вход"}, status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    if user is not None and user["must_change_password"] \
            and path not in PASSWORD_PATHS:
        return RedirectResponse("/password", status_code=303)
    return await call_next(request)


SECTION_FIELDS = {
    "header": ["manager", "source_type", "tender_ref", "bitrix_task_id",
               "division", "scenario",
               "approved_date", "payment_date", "work_start_date"],
    "parties": ["seller_id", "seller_name", "buyer_id", "buyer_name"],
    "lot": ["lot_cost", "auction_step", "contamination_pct", "removal_months",
            "start_month", "shipment_type", "relocation", "payment_terms",
            "purchase_special", "vat_rate", "vat_unrecovered_pct", "capital_rate",
            "capital_base", "payment_delay_months", "tax_rate",
            "payroll_tax_rate", "shipment_loss_pct", "scen_price_delta"],
    "summary": ["risk_expert", "recommendation", "economist_comment", "clarify_notes"],
}
FLOAT_FIELDS = {"seller_id", "buyer_id", "lot_cost", "auction_step",
                "contamination_pct", "removal_months", "vat_rate",
                "vat_unrecovered_pct", "capital_rate", "capital_base",
                "payment_delay_months", "tax_rate", "payroll_tax_rate",
                "shipment_loss_pct", "scen_price_delta"}

# Короткие названия месяцев для графика вывоза (ключ — номер месяца).
MONTH_NAMES = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
               7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}

DEFAULT_RISKS = [
    ("Рыночный", "Снижение цен реализации до момента вывоза", "С", "З",
     "Зафиксировать цену реализации до подписания"),
    ("Контрагент", "Недопоставка объёма или несоответствие качества", "С", "З",
     "Включить штрафные санкции в договор"),
    ("Логистический", "Срыв сроков вывоза (зимники, удалённость)", "С", "З",
     "Резерв времени в плане; альтернативный перевозчик"),
    ("Качества", "Реальный засор выше заявленного", "С", "К",
     "Входной контроль; корректировка цены по факту"),
    ("Финансовый", "Рост стоимости привлечённого капитала", "Н", "З",
     "Зафиксировать ставку; сократить срок вывоза"),
]


# Цвета потоков по типу металла/покупки (граф маршрута).
FLOW_COLORS = {"лом": "#37474f", "цветмет": "#e67e22", "труба": "#6a5acd",
               "кабель": "#16a085", "ДХНО": "#8d6e63"}
NODE_COLORS = {"seller": "#1f4e5f", "base": "#4da3c7", "workshop": "#c78f4d",
               "production": "#7a4dc7", "custody": "#78909c", "buyer": "#3c9d5d"}


def flow_groups(items: list) -> list[dict]:
    """Потоки: позиции БП, сгруппированные по категории номенклатуры.

    200 строк «станция управления» = один поток; чёрный лом и цветмет —
    разные потоки с разными маршрутами.
    """
    groups: dict[str, dict] = {}
    for it in items:
        key = (it["category"] or it["nomenclature"] or "без категории").strip()
        g = groups.setdefault(key, {
            "name": key, "ptype": it["purchase_type"] or "лом",
            "count": 0, "volume": 0.0, "own_w": 0.0, "cut_w": 0.0, "w": 0.0,
        })
        vol = float(it["volume_t"] or 0)
        g["count"] += 1
        g["volume"] += vol
        if it["own_transport_pct"] is not None:
            g["own_w"] += vol * float(it["own_transport_pct"])
            g["w"] += vol
        if it["workshop_cut_pct"] is not None:
            g["cut_w"] += vol * float(it["workshop_cut_pct"])
    out = []
    for g in groups.values():
        out.append({
            "name": g["name"], "ptype": g["ptype"], "count": g["count"],
            "volume": g["volume"],
            "own_pct": g["own_w"] / g["w"] if g["w"] else None,      # ср.взвеш. AD
            "workshop_pct": g["cut_w"] / g["w"] if g["w"] else None, # ср.взвеш. AF
            "color": FLOW_COLORS.get(g["ptype"], "#4da3c7"),
        })
    out.sort(key=lambda g: -g["volume"])
    return out


def bitrix_task_url(conn, task_id) -> str | None:
    """Ссылка на задачу Битрикс24. Базовый адрес портала — в настройках
    (settings.bitrix_url), номер задачи хранится в карточке. Если вставили
    целиком ссылку — берём из неё номер, чтобы работал и такой ввод."""
    raw = str(task_id or "").strip()
    if not raw:
        return None
    m = re.search(r"(\d{3,})", raw)
    if not m:
        return None
    base = str(get_setting(conn, "bitrix_url", "https://team.rosmetrade.ru")).rstrip("/")
    return f"{base}/company/personal/user/0/tasks/task/view/{m.group(1)}/"


def current_user(request: Request):
    """Пользователь текущей сессии (положен middleware) либо None."""
    return getattr(request.state, "user", None)


def current_role(request: Request) -> str:
    """Роль вошедшего пользователя. Без входа — пустая строка: ни один
    раздел не редактируется и ни одно действие не разрешено."""
    user = current_user(request)
    if user is None:
        return ""
    role = user["role"]
    return role if role in workflow.ROLES else ""


def actor(request: Request) -> str:
    """Автор действия для журнала: логин вошедшего пользователя."""
    user = current_user(request)
    return user["login"] if user is not None else "—"


def actor_name(request: Request) -> str:
    """Имя автора для истории версий: «Иванов И. (Экономист)»."""
    user = current_user(request)
    if user is None:
        return ""
    role = workflow.ROLES.get(user["role"], user["role"])
    return f"{user['full_name'] or user['login']} ({role})"


def load_bp(conn, bp_id: int):
    bp = conn.execute("SELECT * FROM business_plans WHERE id = ?", (bp_id,)).fetchone()
    items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    return bp, items, costs


def resolve_variant(request: Request, bp) -> str:
    """Активный вариант расчёта: параметр ?variant=… (переключатель в
    карточке), иначе последний выбранный (business_plans.active_variant).
    «Лукойл» доступен только когда вариант заведён (has_luk)."""
    v = request.query_params.get("variant") or bp["active_variant"] or "bsp"
    if v not in calc.VARIANTS or (v == "luk" and not bp["has_luk"]):
        v = "bsp"
    return v


def back(request: Request, bp_id: int, msg: str = "",
         default_tab: str = "deal") -> RedirectResponse:
    """Вернуть на ту вкладку БП, с которой отправили форму.

    Карточка разрезана на вкладки-страницы, и общий редирект «на /bp/{id}»
    выкидывал бы человека из раздела, который он правит: поправил затраты —
    оказался на «Сделке». Адрес берётся из Referer и проверяется на
    принадлежность этому же БП, чтобы не превратиться в открытый редирект."""
    referer = request.headers.get("referer", "")
    parsed = urlparse(referer) if referer else None
    # Хост тоже проверяем: путь из чужого Referer подставлять нельзя, даже
    # если он ведёт на наш адрес — это чужие данные о том, «где был человек».
    same_host = bool(parsed) and (not parsed.netloc
                                  or parsed.netloc == request.url.netloc)
    path = parsed.path if parsed and same_host else ""
    if re.match(rf"^/bp/{bp_id}(/[a-z_]+)?/?$", path):
        # Прежнее сообщение из адреса убираем, иначе они копятся.
        query = "&".join(p for p in urlparse(referer).query.split("&")
                         if p and not p.startswith("msg="))
        return redirect(path + (f"?{query}" if query else ""), msg)
    return redirect(f"/bp/{bp_id}/{default_tab}", msg)


def redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        # msg — до якоря: «/references#ref-x?msg=…» клал сообщение во
        # фрагмент, браузер его на сервер не отправляет, и плашка не
        # показывалась ни у одной карточки справочников.
        base, _, frag = url.partition("#")
        base += ("&" if "?" in base else "?") + "msg=" + quote(msg)
        url = base + (("#" + frag) if frag else "")
    return RedirectResponse(url, status_code=303)


async def read_form(request: Request) -> forms.Form:
    """Отправка формы, готовая к проверке значений (app/forms.py)."""
    return forms.Form(await request.form())


def single(field: str, raw, **limits) -> tuple[float | None, str]:
    """Одно число из простой формы: значение и сообщение об ошибке.

    Для роутов с параметрами `Form(...)`, где заводить `forms.Form` ради
    одного поля незачем. Пустое сообщение — значение годное.
    """
    try:
        return forms.number(raw, field, **limits), ""
    except forms.Invalid as exc:
        return None, f"Не сохранено. {exc}."


def edit_conflict(conn, request: Request, bp, form: forms.Form) -> str:
    """Сообщение, если карточку изменили, пока эту форму заполняли.

    Двое экономистов, открывших одну карточку, затирали правки друг друга
    молча — побеждал тот, кто сохранил последним, а второй узнавал об этом
    только из истории версий. Страница несёт номер версии, с которым её
    отрисовали (скрытое поле `_v`); если в базе он уже другой, правка не
    пишется.

    Свои же изменения не считаются конфликтом: живой пересчёт затрат пишет
    параметр и поднимает версию на той же открытой странице, и блокировать
    человека его собственной правкой было бы не защитой, а помехой.

    Форма без `_v` (служебная проверка, старая вкладка, браузер без
    JavaScript) не блокируется: молчаливая потеря правок хуже, чем её
    отсутствие, но и отказ сохранять привычные формы недопустим.
    """
    raw = (form.raw("_v") or "").strip()
    if not raw.isdigit():
        return ""
    seen, now = int(raw), int(bp["version"] or 0)
    if seen >= now:
        return ""
    others = [r["author"] for r in conn.execute(
        "SELECT DISTINCT author FROM bp_versions WHERE bp_id = ? AND version > ?",
        (bp["id"], seen)).fetchall()]
    mine = actor_name(request)
    if all((a or "") == mine for a in others):
        return ""                        # это мои же правки в соседней вкладке
    who = next((a for a in others if a and a != mine), "другой пользователь")
    return (f"Карточку изменил {who}, пока вы правили её у себя. "
            f"Ничего не сохранено — обновите страницу и повторите правку.")


def save_version(conn, bp_id: int, author: str, change_note: str = "",
                 variant: str | None = None) -> int | None:
    """Снимок расчёта БП целиком (оба варианта) в bp_versions.

    Вызывается автоматически после каждой правки: если состав данных не
    изменился (тот же snapshot_hash, что у последней версии) — новая запись
    не создаётся, поэтому повторное сохранение формы без правок не плодит
    версии. Возвращает номер версии либо None, если изменений не было."""
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        return None
    risks = conn.execute("SELECT * FROM bp_risks WHERE bp_id = ?",
                         (bp_id,)).fetchall()
    snapshot = {
        "bp": {k: bp[k] for k in bp.keys() if k != "updated_at"},
        "items": [dict(it) for it in items],
        "costs": [dict(c) for c in costs],
        "risks": [dict(r) for r in risks],
    }
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    last = conn.execute(
        "SELECT version, snapshot_hash FROM bp_versions WHERE bp_id = ? "
        "ORDER BY id DESC LIMIT 1", (bp_id,)).fetchone()
    if last and last["snapshot_hash"] == digest:
        return None                      # правка ничего не изменила

    # ЧП обоих вариантов — чтобы список версий читался без разбора снимка.
    profits: dict[str, float | None] = {"bsp": None, "luk": None}
    for code in ("bsp", "luk"):
        if code == "luk" and not bp["has_luk"]:
            continue
        bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, code)
        profits[code] = calc.pnl(bp_v, items_v, costs_v, conn).get("net_profit")
    snapshot["pnl"] = {"net_profit_bsp": profits["bsp"],
                       "net_profit_luk": profits["luk"]}

    version = (last["version"] + 1) if last else 1
    conn.execute(
        "INSERT INTO bp_versions (bp_id, version, snapshot_json, author, "
        "variant, scenario, net_profit_bsp, net_profit_luk, change_note, "
        "snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (bp_id, version,
         json.dumps(snapshot, ensure_ascii=False, default=str), author,
         variant or bp["active_variant"] or "bsp", bp["scenario"],
         profits["bsp"], profits["luk"], change_note or None, digest))
    conn.execute("UPDATE business_plans SET version = ? WHERE id = ?",
                 (version, bp_id))
    return version


def base_ctx(request: Request, conn) -> dict:
    return {"role": current_role(request), "roles": workflow.ROLES,
            "user": current_user(request),
            "msg": request.query_params.get("msg", "")}


# ─────────────────────────────────────── Список БП ──────────────────
# Порядок реестра: подпись для выбора и чем сортировать. Прибыли здесь нет
# намеренно: чтобы её узнать, нужно посчитать P&L каждого БП (у одного из них
# 1950 позиций), а реестр должен открываться мгновенно. Прибыль и
# рентабельность по всем сделкам показывает «Портфель лотов».
REGISTRY_SORTS = {
    "new": "сначала новые",
    "old": "сначала старые",
    "number": "по номеру",
    "updated": "по дате правки",
    "lot_cost": "по стоимости лота",
    "items": "по числу позиций",
}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = connect()
    # Поиск и фильтры: при сотне сделок в год реестр без них непригоден —
    # найти БП по номеру, продавцу или менеджеру было нечем.
    query = (request.query_params.get("q") or "").strip()
    status = (request.query_params.get("status") or "").strip()
    division = (request.query_params.get("division") or "").strip()
    archived = (request.query_params.get("archived") or "").strip()
    sort = (request.query_params.get("sort") or "new").strip()
    if sort not in REGISTRY_SORTS:
        sort = "new"

    where, args = [], []
    if query:
        # lower() в SQLite не знает кириллицы — только lower_ru (см. db.py).
        like = f"%{query}%"
        fields = ["bp.bp_number", "COALESCE(bp.source_name, '')",
                  "COALESCE(bp.seller_name, '')", "COALESCE(bp.buyer_name, '')",
                  "COALESCE(bp.manager, '')", "COALESCE(bp.tender_ref, '')",
                  "COALESCE(bp.bitrix_task_id, '')"]
        where.append("(" + " OR ".join(
            f"lower_ru({f}) LIKE lower_ru(?)" for f in fields) + ")")
        args += [like] * len(fields)
    if status:
        where.append("bp.status = ?")
        args.append(status)
    if division:
        where.append("bp.division = ?")
        args.append(division)
    # Архив по умолчанию скрыт: пробы не должны мешать искать рабочие БП.
    if archived == "only":
        where.append("bp.archived = 1")
    elif archived != "all":
        where.append("bp.archived = 0")

    sql = ("SELECT bp.*, (SELECT COUNT(*) FROM bp_items i WHERE i.bp_id = bp.id) "
           "AS items_count FROM business_plans bp")
    if where:
        sql += " WHERE " + " AND ".join(where)
    plans = conn.execute(sql + " ORDER BY bp.id DESC", tuple(args)).fetchall()
    # Версии расчёта под строкой БП: декомпозиция реестра — какой вариант
    # правили, когда и с каким результатом (последние 20 на БП).
    versions: dict[int, list] = {}
    for row in conn.execute(
            "SELECT bp_id, version, created_at, variant, scenario, "
            "net_profit_bsp, net_profit_luk, change_note, author "
            "FROM bp_versions ORDER BY bp_id, version DESC").fetchall():
        versions.setdefault(row["bp_id"], []).append(row)
    # «Похоже на пробу» — по самим данным, а не по снимку версии: снимки с
    # чистой прибылью появились только 14.08 и есть у одного БП, так что
    # судить по ним значило бы пометить пробами почти весь реестр. Признак
    # тот же, что назван в сверке: нет позиций, нет цен, нет стоимости лота —
    # расчёт такого БП заведомо нулевой.
    empty = {}
    for row in conn.execute(
            "SELECT bp.id, bp.lot_cost, "
            "(SELECT COUNT(*) FROM bp_items i WHERE i.bp_id = bp.id) AS items, "
            "(SELECT COUNT(*) FROM bp_items i WHERE i.bp_id = bp.id "
            " AND i.sale_price > 0) AS priced FROM business_plans bp").fetchall():
        if not row["items"]:
            empty[row["id"]] = "нет позиций лота"
        elif not row["priced"]:
            empty[row["id"]] = "ни у одной позиции нет цены реализации"
        elif not row["lot_cost"]:
            empty[row["id"]] = "не задана стоимость лота"

    order = {
        "new": lambda p: -p["id"],
        "old": lambda p: p["id"],
        "number": lambda p: p["bp_number"],
        "updated": lambda p: p["updated_at"] or "",
        "lot_cost": lambda p: -(p["lot_cost"] or 0.0),
        "items": lambda p: -p["items_count"],
    }[sort]
    plans = sorted(plans, key=order)
    if sort == "updated":
        plans.reverse()                       # свежая правка сверху

    ctx = {**base_ctx(request, conn), "plans": plans,
           "versions": {k: v[:20] for k, v in versions.items()},
           # Ссылки на задачи Битрикс24 — первая колонка реестра.
           "bitrix_links": {p["id"]: bitrix_task_url(conn, p["bitrix_task_id"])
                            for p in plans},
           "variant_labels": calc.VARIANTS,
           # Названия типов сделки для колонки реестра (в строке лежит код).
           "bp_type_names": {t["code"]: t["name"] for t in bp_types.all_types(conn)},
           "filters": {"q": query, "status": status, "division": division,
                       "archived": archived, "sort": sort},
           "sorts": REGISTRY_SORTS,
           "empty_plans": empty,
           "total_plans": conn.execute(
               "SELECT COUNT(*) n FROM business_plans").fetchone()["n"],
           "archived_count": conn.execute(
               "SELECT COUNT(*) n FROM business_plans WHERE archived = 1"
           ).fetchone()["n"],
           "statuses": [r["status"] for r in conn.execute(
               "SELECT DISTINCT status FROM business_plans ORDER BY status")],
           "divisions": [r["division"] for r in conn.execute(
               "SELECT DISTINCT division FROM business_plans "
               "WHERE division IS NOT NULL AND division <> '' ORDER BY division")],
           }
    conn.close()
    return templates.TemplateResponse(request, "index.html", ctx)


@app.post("/bp/{bp_id}/archive")
def archive_bp(request: Request, bp_id: int, action: str = Form("archive")):
    """Убрать БП из рабочего реестра или вернуть обратно.

    Удаления здесь нет намеренно: даже у явной пробы есть расчёт, версии и
    журнал, а решение «это точно мусор» человек принимает не глядя. Признак
    снимается тем же нажатием, портфель лотов архивные БП не считает.
    """
    role = current_role(request)
    if role not in ("manager", "director", "admin"):
        return redirect("/", "Разбирать реестр может менеджер или руководитель.")
    conn = connect()
    bp = conn.execute("SELECT bp_number FROM business_plans WHERE id = ?",
                      (bp_id,)).fetchone()
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    to_archive = action != "restore"
    conn.execute("UPDATE business_plans SET archived = ? WHERE id = ?",
                 (1 if to_archive else 0, bp_id))
    log(conn, actor(request), bp_id,
        "archive" if to_archive else "unarchive", bp["bp_number"])
    conn.commit()
    conn.close()
    return redirect(_safe_next(request.query_params.get("next", "")) or "/",
                    f"{bp['bp_number']}: "
                    + ("убран в архив реестра." if to_archive
                       else "возвращён в рабочий реестр."))


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio(request: Request):
    """Портфель лотов: сводка по всем БП — тоннаж, стоимость лота, сколько
    вывезено и осталось, прибыль и рентабельность, оборачиваемость запаса.
    Структура листа «анализ фин» книг экономистов (реестр лотов года)."""
    conn = connect()
    # Архивные БП в портфель не идут: пробы искажали итог сильнее всего
    # (сверка 17.08: БП-0010 давал −33 млн, БП-0012 +19 млн на тестовых данных).
    plans = conn.execute(
        "SELECT * FROM business_plans WHERE archived = 0 ORDER BY id DESC").fetchall()
    rows, totals = [], {"volume": 0.0, "lot_cost": 0.0, "revenue": 0.0,
                        "profit": 0.0, "shipped": 0.0, "left": 0.0,
                        "fact_revenue": 0.0}
    for bp in plans:
        items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ?",
                             (bp["id"],)).fetchall()
        if not items:
            continue
        costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ?",
                             (bp["id"],)).fetchall()
        bp_v, items_v, costs_v = calc.apply_variant(
            bp, items, costs, bp["active_variant"] or "bsp")
        p = calc.pnl(bp_v, items_v, costs_v, conn)
        sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id = ?",
                                  (bp["id"],)).fetchall()
        sched = calc.removal_schedule(bp_v, items_v, sched_rows, conn)
        fact = conn.execute(
            "SELECT COALESCE(SUM(volume_t), 0) AS v, COALESCE(SUM(revenue), 0) AS r "
            "FROM bp_fact WHERE bp_id = ?", (bp["id"],)).fetchone()
        row = {
            "bp": bp, "volume": p["purchase_volume"], "lot_cost": p["lot_cost"],
            "revenue": p["revenue"], "profit": p["net_profit"],
            "ros": p["ros_pct"], "ok": p["above_threshold"],
            "shipped": sched["total_shipped"], "left": sched["total_left"],
            "fact_volume": _f(fact["v"]), "fact_revenue": _f(fact["r"]),
            "months": p["removal_months"],
            # Оборачиваемость: сколько раз за год обернётся вложенный капитал
            # при таком сроке вывоза (12 / срок) — как в книге экономистов.
            "turns_year": (12.0 / p["removal_months"]
                           if p["removal_months"] else 0.0),
        }
        for key in ("volume", "lot_cost", "revenue", "profit", "shipped",
                    "left", "fact_revenue"):
            totals[key] += row.get(key, 0.0)
        rows.append(row)
    totals["ros"] = (totals["profit"] / totals["revenue"] * 100.0
                     if totals["revenue"] else 0.0)
    totals["turns_year"] = (sum(r["turns_year"] * r["lot_cost"] for r in rows)
                            / totals["lot_cost"] if totals["lot_cost"] else 0.0)
    totals["months_turn"] = (12.0 / totals["turns_year"]
                             if totals["turns_year"] else 0.0)
    ctx = {**base_ctx(request, conn), "rows": rows, "totals": totals}
    conn.close()
    return templates.TemplateResponse(request, "portfolio.html", ctx)


# ───────────────────────────────── Сверка книг с фактом ────────────

def _xls_to_xlsx_tmp(tmp_path: Path) -> Path:
    """Временный .xls → временный .xlsx (исходный удаляется)."""
    dst = tmp_path.with_suffix(".xlsx")
    list_import.xls_to_xlsx(tmp_path, dst)
    tmp_path.unlink(missing_ok=True)
    return dst


def _neighbor_snapshot() -> str:
    return os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "out", "sales_data.json")


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    """По каждой сделке: книга экономиста, факт 1С и модель сервиса от факта;
    сводка по типам — кто ближе к факту."""
    conn = connect()
    f = {"type": (request.query_params.get("type") or "").strip(),
         "year": (request.query_params.get("year") or "").strip(),
         "scope": (request.query_params.get("scope") or "").strip()}
    rows = audit.rows(conn, f["type"] or None, f["year"] or None,
                      closed_only=f["scope"] == "closed", with_book=f["scope"] != "all")
    if f["scope"] == "costs":
        rows = [r for r in rows if r["fact_profit"] is not None]
    types = conn.execute("SELECT code, name FROM ref_bp_types WHERE is_active = 1 ORDER BY sort").fetchall()
    stats = conn.execute(
        "SELECT COUNT(*) AS deals, SUM(plan_rev IS NOT NULL) AS with_book, SUM(closed) AS closed, "
        "SUM(fact_profit IS NOT NULL) AS with_costs FROM stat_deal_audit").fetchone()
    ctx = {**base_ctx(request, conn), "rows": rows, "summary": audit.summary(conn),
           "filters": f, "types": types, "years": audit.years(conn),
           "type_names": {t["code"]: t["name"] for t in types} | {"mixed": "Смешанный"},
           "stats": {k: (stats[k] or 0) for k in stats.keys()}}
    conn.close()
    return templates.TemplateResponse(request, "audit.html", ctx)


@app.get("/audit.xlsx")
def audit_xlsx(request: Request):
    conn = connect()
    path = audit.to_xlsx(conn, Path(ATTACH_DIR) / "_audit" / "Сверка_книг_с_фактом.xlsx")
    conn.close()
    return FileResponse(path, filename="Сверка_книг_с_фактом.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/audit/rebuild")
def audit_rebuild(request: Request):
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/audit", "Пересобирать сверку может экономист.")
    snap = _neighbor_snapshot()
    if not os.path.isfile(snap):
        return redirect("/audit", "Снимок «Реализации» недоступен: нет файла out/sales_data.json.")
    conn = connect()
    res = audit.build(conn, snap)
    log(conn, actor(request), None, "audit", f"{res['deals']} сделок, с книгой {res['with_book']}")
    conn.commit()
    conn.close()
    return redirect("/audit", f"Сверка пересобрана: {res['deals']} сделок, с книгой {res['with_book']}, "
                              f"закрытых {res['closed']}, с полным фактом затрат {res['with_costs']}.")


def _f(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ───────────────────────────────── Вход и учётные записи ────────────

def _client_ip(request: Request) -> str:
    """IP клиента для журнала и проверки сети.

    X-Forwarded-For принимается ТОЛЬКО от локального прокси (nginx на том же
    сервере): заголовок ставит кто угодно, и без этого условия им обходили бы
    ограничение по сетям и подделывали записи в журнале."""
    peer = _peer_ip(request)
    if _is_loopback(peer):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


def _peer_ip(request: Request) -> str:
    """Адрес, с которого пришло соединение (подделать нельзя)."""
    return request.client.host if request.client else ""


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def upload_name(raw: str | None, default: str) -> str:
    """Имя загруженного файла в читаемом виде.

    Заголовки multipart разбираются как latin-1, поэтому русское имя файла
    приезжает «кракозябрами» («Ð¤Ð°ÐºÑ‚.xlsx») и в таком виде попадает в
    журнал и на диск. Обратная перекодировка возвращает кириллицу; если имя
    и так корректное, оно в latin-1 не кодируется и остаётся как есть."""
    name = Path(raw or default).name
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def trim_source_name(stem: str) -> str:
    """Содержательная часть имени книги для реестра.

    Имя файла экономиста несёт номер запроса и контрагента, а дальше —
    дату, версию и тоннаж («1700_05.11.25_v0_РИТЭК_ЧЛом, труба,штанга_1227тн»):
    служебный хвост растягивал колонку реестра (раздел 7 п.4). Убираются
    дата, «vN» и токены с тоннажем; что осталось — и есть название
    («1700_РИТЭК_ЧЛом, труба,штанга»)."""
    s = stem
    s = re.sub(r"[_\s]*v\d+[_\s]*", "_", s, flags=re.IGNORECASE)
    s = re.sub(r"[_\s]*\d{1,2}\.\d{1,2}\.\d{2,4}[_\s]*", "_", s)
    s = re.sub(r"[-—–\s_]*[\d\s.,]+\s*тн\.?\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"__+", "_", s).strip("_ -—–")
    return s or stem


def _safe_next(raw: str) -> str:
    """Куда вернуть после входа. Только внутренний путь — внешний адрес в
    параметре next превратил бы форму входа в открытый редирект."""
    target = (raw or "/").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# Порядок профилей на экране входа: руководитель первым, служебные учётные
# записи последними — человек ищет себя, а не администратора.
LOGIN_ROLE_ORDER = {"director": 0, "economist": 1, "logist": 2, "manager": 3,
                    "admin": 9}


def login_profiles(conn) -> list[dict]:
    """Кого показать на экране входа. Пароли сюда не попадают.

    Список видно до входа — как в панели логиста: логин никто не помнит, а
    имя коллеги узнаётся с одного взгляда. Учётка без пароля показывается
    отдельно: иначе человек будет тыкать в свою плитку и не понимать, почему
    не пускает.
    """
    rows = conn.execute(
        "SELECT login, full_name, role, password_hash IS NOT NULL AS has_pw "
        "FROM users WHERE is_active = 1").fetchall()
    out = [{"login": r["login"], "name": r["full_name"] or r["login"],
            "role": r["role"], "role_label": workflow.ROLES.get(r["role"], r["role"]),
            "has_pw": bool(r["has_pw"])} for r in rows]
    out.sort(key=lambda u: (LOGIN_ROLE_ORDER.get(u["role"], 5), u["name"]))
    return out


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request) is not None:
        return redirect(_safe_next(request.query_params.get("next", "/")))
    conn = connect()
    try:
        profiles = login_profiles(conn)
    finally:
        conn.close()
    # Выбранный профиль: экран сначала спрашивает «кто вы», потом пароль.
    picked_login = (request.query_params.get("user") or "").strip().lower()
    picked = next((u for u in profiles if u["login"] == picked_login), None)
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(request.query_params.get("next", "/")),
        "error": request.query_params.get("error", ""),
        "msg": request.query_params.get("msg", ""),
        "profiles": profiles,
        "picked": picked,
        # Ручной ввод логина остаётся: учётки может не быть в списке
        # (отключена, заводится прямо сейчас), и админу так привычнее.
        "manual": request.query_params.get("manual") == "1",
    })


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...),
          next: str = Form("/")):
    conn = connect()
    target = _safe_next(next)
    try:
        user, error = auth.authenticate(conn, login, password)
        if user is None:
            log(conn, (login or "").strip().lower(), None, "login_failed",
                _client_ip(request))
            conn.commit()
            url = "/login?next=" + quote(target) + "&error=" + quote(error)
            return RedirectResponse(url, status_code=303)
        token = auth.create_session(conn, user["id"], _client_ip(request),
                                    request.headers.get("user-agent", ""))
        log(conn, user["login"], None, "login", _client_ip(request))
        conn.commit()
    finally:
        conn.close()
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_DAYS * 24 * 3600)
    # Селектор роли из прежней версии: cookie больше не значит ничего.
    resp.delete_cookie("role")
    return resp


@app.post("/logout")
def logout(request: Request):
    conn = connect()
    try:
        user = current_user(request)
        auth.drop_session(conn, request.cookies.get(auth.SESSION_COOKIE))
        if user is not None:
            log(conn, user["login"], None, "logout", "")
        conn.commit()
    finally:
        conn.close()
    resp = RedirectResponse("/login?msg=" + quote("Вы вышли из сервиса."),
                            status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/password", response_class=HTMLResponse)
def password_form(request: Request):
    user = current_user(request)
    conn = connect()
    ctx = base_ctx(request, conn)
    conn.close()
    ctx.update({"forced": bool(user["must_change_password"])})
    return templates.TemplateResponse(request, "password.html", ctx)


@app.post("/password")
def password_change(request: Request, current: str = Form(""),
                    new1: str = Form(...), new2: str = Form(...)):
    user = current_user(request)
    conn = connect()
    try:
        if not auth.verify_password(current, user["password_hash"]):
            return redirect("/password", "Текущий пароль указан неверно.")
        if new1 != new2:
            return redirect("/password", "Новые пароли не совпадают.")
        problem = auth.password_problem(new1)
        if problem:
            return redirect("/password", problem)
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 "
            "WHERE id = ?", (auth.hash_password(new1), user["id"]))
        # Смена пароля закрывает все прежние сессии, кроме текущей.
        conn.execute("DELETE FROM user_sessions WHERE user_id = ? AND token <> ?",
                     (user["id"], request.cookies.get(auth.SESSION_COOKIE, "")))
        log(conn, user["login"], None, "password_change", "")
        conn.commit()
    finally:
        conn.close()
    return redirect("/", "Пароль изменён.")


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    if current_role(request) != "admin":
        return redirect("/", "Управление пользователями доступно администратору.")
    conn = connect()
    ctx = base_ctx(request, conn)
    users = conn.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM user_sessions s "
        "WHERE s.user_id = u.id AND s.expires_at > datetime('now')) AS sessions "
        "FROM users u ORDER BY u.is_active DESC, u.login").fetchall()
    conn.close()
    ctx.update({"users": users, "role_labels": workflow.ROLES})
    return templates.TemplateResponse(request, "users.html", ctx)


@app.post("/users/add")
def users_add(request: Request, login: str = Form(...),
              full_name: str = Form(""), role: str = Form("manager"),
              password: str = Form(...)):
    if current_role(request) != "admin":
        return redirect("/", "Управление пользователями доступно администратору.")
    login = (login or "").strip().lower()
    for problem in (auth.login_problem(login), auth.password_problem(password)):
        if problem:
            return redirect("/users", problem)
    if role not in workflow.ROLES:
        return redirect("/users", "Неизвестная роль.")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users (login, full_name, role, password_hash, "
            "is_active, must_change_password) VALUES (?, ?, ?, ?, 1, 1)",
            (login, (full_name or login).strip(), role,
             auth.hash_password(password)))
        log(conn, actor(request), None, "user_add", f"{login} ({role})")
        conn.commit()
    except sqlite3.IntegrityError:
        return redirect("/users", f"Логин «{login}» уже занят.")
    finally:
        conn.close()
    return redirect("/users", f"Пользователь {login} создан.")


@app.post("/users/{user_id}/update")
def users_update(request: Request, user_id: int, full_name: str = Form(""),
                 role: str = Form("manager"), is_active: str = Form("")):
    if current_role(request) != "admin":
        return redirect("/", "Управление пользователями доступно администратору.")
    if role not in workflow.ROLES:
        return redirect("/users", "Неизвестная роль.")
    active = 1 if is_active else 0
    conn = connect()
    try:
        me = current_user(request)
        if user_id == me["id"] and (role != "admin" or not active):
            return redirect("/users", "Нельзя снять права с самого себя — "
                                      "иначе сервис останется без администратора.")
        conn.execute(
            "UPDATE users SET full_name = ?, role = ?, is_active = ? WHERE id = ?",
            ((full_name or "").strip(), role, active, user_id))
        if not active:
            auth.drop_user_sessions(conn, user_id)     # отключён — сразу выкинуть
        log(conn, actor(request), None, "user_update",
            f"id={user_id} {role} active={active}")
        conn.commit()
    finally:
        conn.close()
    return redirect("/users", "Учётная запись обновлена.")


@app.post("/users/{user_id}/password")
def users_password(request: Request, user_id: int, password: str = Form(...)):
    if current_role(request) != "admin":
        return redirect("/", "Управление пользователями доступно администратору.")
    problem = auth.password_problem(password)
    if problem:
        return redirect("/users", problem)
    conn = connect()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 "
            "WHERE id = ?", (auth.hash_password(password), user_id))
        auth.drop_user_sessions(conn, user_id)   # прежние сессии недействительны
        log(conn, actor(request), None, "user_password", f"id={user_id}")
        conn.commit()
    finally:
        conn.close()
    return redirect("/users", "Пароль задан. Пользователь сменит его при входе.")


@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request):
    """Журнал действий: кто, когда и что менял. Виден руководителю и
    администратору — остальным роль не даёт смысла в чужих правках."""
    if current_role(request) not in ("admin", "director"):
        return redirect("/", "Журнал доступен руководителю и администратору.")
    conn = connect()
    ctx = base_ctx(request, conn)
    q = (request.query_params.get("q") or "").strip()
    action = (request.query_params.get("action") or "").strip()
    sql = ("SELECT a.*, u.full_name, bp.bp_number FROM audit_log a "
           "LEFT JOIN users u ON u.login = a.user_name "
           "LEFT JOIN business_plans bp ON bp.id = a.bp_id WHERE 1 = 1")
    args: list = []
    if q:
        sql += (" AND (lower_ru(a.user_name) LIKE ? OR lower_ru(a.details) LIKE ? "
                "OR lower_ru(COALESCE(u.full_name, '')) LIKE ? "
                "OR COALESCE(bp.bp_number, '') LIKE ?)")
        args += [f"%{q.lower()}%"] * 3 + [f"%{q}%"]
    if action:
        sql += " AND a.action = ?"
        args.append(action)
    rows = conn.execute(sql + " ORDER BY a.id DESC LIMIT 500", args).fetchall()
    actions = conn.execute(
        "SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action "
        "ORDER BY action").fetchall()
    conn.close()
    ctx.update({"rows": rows, "actions": actions, "q": q, "action": action})
    return templates.TemplateResponse(request, "journal.html", ctx)


def create_blank_bp(conn) -> tuple[int, str]:
    """Пустой БП со значениями по умолчанию: номер, риски, статьи затрат,
    стартовые узлы маршрута. Возвращает (id, номер)."""
    year = datetime.now().year
    next_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM business_plans").fetchone()["n"]
    number = f"БП-{next_id:04d}-{year}"
    cur = conn.execute(
        "INSERT INTO business_plans (bp_number, vat_rate, capital_rate, tax_rate, "
        "payroll_tax_rate, contamination_pct, removal_months) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (number, get_setting(conn, "vat_rate", 20.0),
         get_setting(conn, "capital_rate", 25.0),
         get_setting(conn, "tax_rate", 25.0),
         get_setting(conn, "payroll_tax_rate", 49.7),
         get_setting(conn, "contamination_pct", 6.0),
         get_setting(conn, "removal_months", 6.0)))
    bp_id = cur.lastrowid
    for r in DEFAULT_RISKS:
        conn.execute(
            "INSERT INTO bp_risks (bp_id, risk_type, description, probability, impact, "
            "mitigation) VALUES (?, ?, ?, ?, ?, ?)", (bp_id, *r))
    for section, item in calc.DEFAULT_COST_ITEMS:
        conn.execute(
            "INSERT INTO bp_costs (bp_id, section, item, amount) VALUES (?, ?, ?, 0)",
            (bp_id, section, item))
    # Стартовые узлы маршрута: продавец и покупатель.
    conn.execute("INSERT INTO bp_nodes (bp_id, kind, label, x, y) "
                 "VALUES (?, 'seller', 'Продавец', -300, 0)", (bp_id,))
    conn.execute("INSERT INTO bp_nodes (bp_id, kind, label, x, y) "
                 "VALUES (?, 'buyer', 'Покупатель', 300, 0)", (bp_id,))
    return bp_id, number


@app.post("/bp/new")
def bp_new(request: Request):
    role = current_role(request)
    if role not in ("manager", "admin"):
        return redirect("/", "Создавать БП может менеджер.")
    conn = connect()
    bp_id, number = create_blank_bp(conn)
    log(conn, actor(request), bp_id, "create", number)
    conn.commit()
    conn.close()
    return back(request, bp_id)


# Поля сделки, которые повторяются от лота к лоту одного продавца и потому
# переносятся «по образцу». Остальное НЕ копируется намеренно:
# lot_cost и auction_step — цена именно того лота; control_* — контрольные
# суммы книги, по которой сверяли образец; даты, статус, версия, номер задачи
# Битрикс и выводы (риск-эксперт, рекомендация, комментарии) относятся к той
# сделке, а не к новой.
COPY_BP_FIELDS = [
    "manager", "source_type", "division", "scenario",
    "seller_id", "seller_name", "buyer_id", "buyer_name",
    "contamination_pct", "removal_months", "start_month", "shipment_type",
    "relocation", "payment_terms", "purchase_special",
    "vat_rate", "vat_unrecovered_pct", "capital_rate", "capital_base",
    "payment_delay_months", "tax_rate", "payroll_tax_rate",
    "shipment_loss_pct", "scen_price_delta",
    "has_luk", "luk_overrides", "active_variant",
]

# Что можно перенести вместе с параметрами. Ключ — имя флажка в форме.
COPY_PARTS = {
    "costs": "статьи затрат с суммами и параметрами расчёта",
    "risks": "риски",
    "scenarios": "сценарии цен",
    "route": "граф маршрута (узлы, плечи, операции)",
    "items": "позиции лота с ценами и планом продажи",
}


def copy_bp(conn, source, parts: set[str]) -> tuple[int, str, list[str]]:
    """Новый БП по образцу. Возвращает (id, номер, что перенесено).

    Новый БП начинается с нуля, хотя 80% параметров повторяются от лота к
    лоту одного продавца: продавец, дивизион, ставки, условия оплаты, состав
    статей затрат, риски. «Создать по образцу» экономит больше времени, чем
    любой расчёт (сверка 17.08, раздел 5).
    """
    bp_id, number = create_blank_bp(conn)
    values = [source[f] for f in COPY_BP_FIELDS]
    conn.execute("UPDATE business_plans SET "
                 + ", ".join(f"{f} = ?" for f in COPY_BP_FIELDS)
                 + " WHERE id = ?", (*values, bp_id))
    # Происхождение копии видно в реестре: без него через неделю никто не
    # вспомнит, откуда взялись параметры (раздел 7 п.4).
    origin = f"по образцу {source['bp_number']}"
    if source["source_name"]:
        origin += f" ({source['source_name']})"
    conn.execute("UPDATE business_plans SET source_name = ? WHERE id = ?",
                 (origin, bp_id))
    done: list[str] = ["параметры сделки"]

    # Узлы и плечи маршрута: ссылки внутри графа переносятся по карте
    # «старый id → новый», иначе стрелки уедут к чужим узлам.
    nodes: dict[int, int] = {}
    edges: dict[int, int] = {}
    if "route" in parts:
        conn.execute("DELETE FROM bp_nodes WHERE bp_id = ?", (bp_id,))
        for n in conn.execute("SELECT * FROM bp_nodes WHERE bp_id = ? ORDER BY id",
                              (source["id"],)).fetchall():
            cur = conn.execute(
                "INSERT INTO bp_nodes (bp_id, kind, label, ref_kind, ref_id, x, y) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bp_id, n["kind"], n["label"], n["ref_kind"], n["ref_id"],
                 n["x"], n["y"]))
            nodes[n["id"]] = cur.lastrowid
        for e in conn.execute("SELECT * FROM bp_edges WHERE bp_id = ? ORDER BY id",
                              (source["id"],)).fetchall():
            if e["from_node"] not in nodes or e["to_node"] not in nodes:
                continue
            cur = conn.execute(
                "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, "
                "volume_t, comment, flow_group) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bp_id, nodes[e["from_node"]], nodes[e["to_node"]],
                 e["transport"], e["volume_t"], e["comment"], e["flow_group"]))
            edges[e["id"]] = cur.lastrowid
        for pr in conn.execute("SELECT * FROM bp_processes WHERE bp_id = ? ORDER BY id",
                               (source["id"],)).fetchall():
            if pr["node_id"] not in nodes:
                continue
            conn.execute(
                "INSERT INTO bp_processes (bp_id, node_id, work_type, input_nomen, "
                "output_nomen, volume_t, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bp_id, nodes[pr["node_id"]], pr["work_type"], pr["input_nomen"],
                 pr["output_nomen"], pr["volume_t"], pr["comment"]))
        done.append("граф маршрута")

    if "costs" in parts:
        # Пустой БП уже завёл статьи по умолчанию — заменяем их статьями
        # образца, иначе список удвоится.
        conn.execute("DELETE FROM bp_costs WHERE bp_id = ?", (bp_id,))
        for c in conn.execute("SELECT * FROM bp_costs WHERE bp_id = ? ORDER BY id",
                              (source["id"],)).fetchall():
            conn.execute(
                "INSERT INTO bp_costs (bp_id, section, item, amount, base, "
                "comment, amount_luk, edge_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bp_id, c["section"], c["item"], c["amount"], c["base"],
                 c["comment"], c["amount_luk"], edges.get(c["edge_id"])))
        for p in conn.execute("SELECT * FROM bp_cost_params WHERE bp_id = ?",
                              (source["id"],)).fetchall():
            conn.execute("INSERT INTO bp_cost_params (bp_id, key, value) "
                         "VALUES (?, ?, ?)", (bp_id, p["key"], p["value"]))
        done.append("статьи затрат")

    if "risks" in parts:
        conn.execute("DELETE FROM bp_risks WHERE bp_id = ?", (bp_id,))
        for r in conn.execute("SELECT * FROM bp_risks WHERE bp_id = ? ORDER BY id",
                              (source["id"],)).fetchall():
            conn.execute(
                "INSERT INTO bp_risks (bp_id, risk_type, description, probability, "
                "impact, mitigation) VALUES (?, ?, ?, ?, ?, ?)",
                (bp_id, r["risk_type"], r["description"], r["probability"],
                 r["impact"], r["mitigation"]))
        done.append("риски")

    if "scenarios" in parts:
        for s in conn.execute("SELECT * FROM bp_scenarios WHERE bp_id = ? ORDER BY id",
                              (source["id"],)).fetchall():
            conn.execute(
                "INSERT INTO bp_scenarios (bp_id, name, price_delta, lot_cost, "
                "vat_unrecovered_pct, vat_rate, capital_rate, removal_months, "
                "contamination_pct, tax_rate, control_net_profit, comment, sort) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bp_id, s["name"], s["price_delta"], s["lot_cost"],
                 s["vat_unrecovered_pct"], s["vat_rate"], s["capital_rate"],
                 s["removal_months"], s["contamination_pct"], s["tax_rate"],
                 None, s["comment"], s["sort"]))
        done.append("сценарии")

    if "items" in parts:
        item_cols = [r["name"] for r in conn.execute("PRAGMA table_info(bp_items)")
                     if r["name"] not in ("id", "bp_id")]
        marks = ", ".join("?" for _ in item_cols)
        items_map: dict[int, int] = {}
        for it in conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id",
                               (source["id"],)).fetchall():
            cur = conn.execute(
                f"INSERT INTO bp_items (bp_id, {', '.join(item_cols)}) "
                f"VALUES (?, {marks})",
                (bp_id, *[it[c] for c in item_cols]))
            items_map[it["id"]] = cur.lastrowid
        for s in conn.execute("SELECT * FROM bp_item_sales WHERE bp_id = ? ORDER BY id",
                              (source["id"],)).fetchall():
            if s["item_id"] not in items_map:
                continue
            conn.execute(
                "INSERT INTO bp_item_sales (bp_id, item_id, variant, buyer, "
                "volume_t, sale_price, sale_type, shipment, contamination_pct, "
                "note, sort) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bp_id, items_map[s["item_id"]], s["variant"], s["buyer"],
                 s["volume_t"], s["sale_price"], s["sale_type"], s["shipment"],
                 s["contamination_pct"], s["note"], s["sort"]))
        done.append(f"позиции ({len(items_map)})")

    return bp_id, number, done


@app.post("/bp/copy")
async def bp_copy(request: Request):
    """Создать сделку по образцу другого БП."""
    role = current_role(request)
    if role not in ("manager", "admin"):
        return redirect("/", "Создавать БП может менеджер.")
    form = await read_form(request)
    raw = form.raw("source").strip()
    conn = connect()
    source = (conn.execute("SELECT * FROM business_plans WHERE id = ?",
                           (int(raw),)).fetchone() if raw.isdigit() else None)
    if source is None:
        conn.close()
        return redirect("/", "Не сохранено. Выберите БП-образец из списка.")
    parts = {name for name in COPY_PARTS if form.has(f"copy_{name}")}
    bp_id, number, done = copy_bp(conn, source, parts)
    log(conn, actor(request), bp_id, "copy_bp",
        f"по образцу {source['bp_number']}: {', '.join(done)}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/deal",
                    f"{number} создан по образцу {source['bp_number']}. "
                    f"Перенесено: {', '.join(done)}. Стоимость лота, шаг "
                    f"аукциона и контрольные суммы не переносятся — они свои "
                    f"у каждой сделки.")


_CP_STOPWORDS = {"ооо", "оао", "зао", "ао", "пао", "ип", "гк", "тк", "нал"}


def match_counterparty(conn, name: str) -> int | None:
    """Контрагент 1С по имени: точное совпадение, иначе — по значимым словам
    («ООО "Лукойл-западная Сибирь"» = «ЛУКОЙЛ-ЗАПАДНАЯ СИБИРЬ ООО»)."""
    ref = conn.execute(
        "SELECT id FROM ref_counterparties WHERE lower_ru(name) = lower_ru(?) "
        "LIMIT 1", (name,)).fetchone()
    if ref:
        return ref["id"]

    def tokens(s: str) -> set[str]:
        words = re.sub(r"[^а-яёa-z0-9 ]", " ", (s or "").lower()).split()
        return {w for w in words if w not in _CP_STOPWORDS and len(w) > 1}

    want = tokens(name)
    if not want:
        return None
    best = None
    for row in conn.execute("SELECT id, name FROM ref_counterparties").fetchall():
        have = tokens(row["name"])
        if have and (want <= have or have <= want):
            if best is not None:            # неоднозначно — не угадываем
                return None
            best = row["id"]
    return best


# Явное сопоставление статей старого БП со статьями приложения по различающему
# признаку (надёжнее нечёткого поиска; неузнанное остаётся 0).
OLD_BP_COST_ALIASES = {
    "Погрузочно-разгрузочные расходы": ["погрузочно"],
    "Заправка газом и кислородом": ["заправка газ"],
    "Транспортные расходы — собственная техника": ["собственн"],
    "Транспортные расходы на отгрузку": ["на отгрузку", "отгрузк"],
    "Транспортные расходы на перемещение": ["на перемещение", "перемещени"],
    "Зарплата": ["зарплата"],
    "Аренда квартир (проживание)": ["квартир"],
    "Командировочные расходы": ["командиров"],
    "Прочие расходы на персонал": ["прочие расходы на перонал",
                                   "прочие расходы на персонал"],
    "Оборудование и инструменты": ["инструмент"],
    "Аренда баз, коммунальные расходы, охрана": ["коммун", "аренда баз"],
    "Прочие производственные расходы": ["прочие производствен"],
    "Амортизационные отчисления (транспорт, оборудование)": ["амортизац"],
}
# Строки-агрегаты P&L (группы и итоги) — не статьи, в затраты не переносятся.
# «Прямые (производственные) затраты, в т.ч.» из БП 1865 без этого фильтра
# ложно попадала в «Прочие производственные расходы» всей суммой.
OLD_BP_AGGREGATE_LINES = ("итого", "в т.ч", "переменные затраты",
                          "постоянные затраты", "прямые")
# Служебные строки P&L (выручка, прибыли, налоги, капитал и т.п.) — считаются
# приложением, при импорте статьями затрат не становятся.
OLD_BP_SERVICE_LINES = (
    "выручка", "себестоимость", "маржинальн", "% наценки", "наценк",
    "операционная", "прибыль", "налоги", "чистая", "рентабельность",
    "привлеченн", "сумма", "дивизион", "объем", "объём", "показатели",
    "комментар", "срок", "месяц", "вид отгрузки", "перемещение", "засор",
    "ндс", "расходы на персонал", "средняя", "цена", "ставка",
    "стоимость закуп", "тоннаж", "перечень", "запрос")
# Строки типов продажи в P&L (расшифровка выручки) — только точное совпадение:
# статья «Разделка кабеля» отсекаться не должна.
OLD_BP_TYPE_LINES = {"лом", "черный лом", "чёрный лом", "цветной лом", "труба",
                     "дхно", "кабель", "цветмет"}


def map_old_bp_costs(cost_lines: dict, total_volume: float,
                     extras: list[dict] | None = None) -> dict:
    """Статья приложения → сумма затрат, руб. Значение: «Суммарно» из файла,
    иначе «на 1 тонну» × объём. Несколько строк файла под одну статью
    суммируются (БП 1692: «Аренда баз, коммун.расходы» + «Аренда базы
    ответ.хранения»). Нулевые пропускаются (остаётся значение 0).

    Если передан extras — в него складываются нестандартные статьи файла
    («Страхование груза» БП 1928): строки затрат, не подошедшие ни под одну
    типовую статью и не являющиеся служебными строками P&L; секция берётся
    из позиции строки в блоках P&L (cost_lines[...]["section"])."""
    out: dict[str, float] = {}
    used: set[str] = set()
    for item, needles in OLD_BP_COST_ALIASES.items():
        total = None
        for key, vals in cost_lines.items():
            if key in used or any(a in key for a in OLD_BP_AGGREGATE_LINES):
                continue
            if key.strip() == "расходы на персонал":   # заголовок группы БП 1865
                continue
            if not any(n in key for n in needles):
                continue
            amount = vals.get("total")
            if amount is None and vals.get("per_t") is not None:
                amount = vals["per_t"] * total_volume
            if amount:
                total = (total or 0.0) + amount
                used.add(key)
        if total:
            out[item] = round(total, 2)
    if extras is not None:
        for key, vals in cost_lines.items():
            if key in used or any(a in key for a in OLD_BP_AGGREGATE_LINES) \
                    or any(s in key for s in OLD_BP_SERVICE_LINES) \
                    or key.strip() in OLD_BP_TYPE_LINES:
                continue
            if not vals.get("section"):   # строка вне блоков затрат P&L
                continue
            amount = vals.get("total")
            if amount is None and vals.get("per_t") is not None:
                amount = vals["per_t"] * total_volume
            if not amount:
                continue
            extras.append({"key": key,
                           "item": key[:1].upper() + key[1:],
                           "amount": round(amount, 2),
                           "section": vals["section"]})
    return out


def apply_import_variant(conn, bp_id: int, data: dict, total_volume: float) -> str:
    """Перенос второго варианта расчёта из книги экономистов: цены реализации
    позиций → bp_items.sale_price_luk, статьи затрат → bp_costs.amount_luk,
    контрольные суммы обоих вариантов → business_plans.control_*. Возвращает
    фрагмент сообщения для пользователя (пустой, если варианта нет)."""
    control = data.get("control") or {}
    conn.execute(
        "UPDATE business_plans SET control_op_profit_bsp = ?, "
        "control_net_profit_bsp = ? WHERE id = ?",
        (control.get("operating_profit"), control.get("net_profit"), bp_id))

    variant = data.get("variant")
    if not variant:
        if data.get("base_code") == "luk":
            return (" В книге только листы «_лук» — они стали основным "
                    "расчётом (вариант ДСП).")
        return ""
    if variant.get("code") != "luk":
        return (f" Семейство «{variant.get('family')}» не перенесено: "
                "ожидались листы «_лук».")

    def norm(s):
        return " ".join((s or "").lower().replace("ё", "е")
                        .replace("«", '"').replace("»", '"').split())

    def pos_key(supplier, division, name):
        return (norm(supplier), norm(division), norm(name))

    # Цены варианта по позициям: сопоставление по (поставщик, подразделение,
    # номенклатура); одинаковые строки разбираются по порядку следования.
    # «Поставщик» = сам продавец сделки обнуляется так же, как при вставке
    # базовых позиций (insert_parsed_items) — иначе ключи не совпадут.
    seller_row = conn.execute(
        "SELECT seller_name FROM business_plans WHERE id = ?",
        (bp_id,)).fetchone()
    seller_norm = norm(seller_row["seller_name"] if seller_row else "")
    from collections import defaultdict
    by_key: dict[tuple, list] = defaultdict(list)
    for p in variant["positions"]:
        sup = p.get("supplier")
        if seller_norm and norm(sup) == seller_norm:
            sup = None
        by_key[pos_key(sup, p.get("division") or p.get("place"),
                       p.get("name"))].append(p)
    bp_cont_row = conn.execute(
        "SELECT contamination_pct FROM business_plans WHERE id = ?",
        (bp_id,)).fetchone()
    bp_cont = float(bp_cont_row["contamination_pct"] or 0) if bp_cont_row else 0.0
    items = conn.execute(
        "SELECT id, supplier, division, nomenclature, sale_type, volume_t, "
        "buyer, shipment, contamination_pct FROM bp_items WHERE bp_id = ? "
        "ORDER BY id", (bp_id,)).fetchall()
    priced_luk = 0
    buyers_luk = 0
    for it in items:
        queue = by_key.get(pos_key(it["supplier"], it["division"],
                                   it["nomenclature"]))
        p = queue.pop(0) if queue else None
        if p and p.get("sale_price") is not None:
            conn.execute("UPDATE bp_items SET sale_price_luk = ? WHERE id = ?",
                         (p["sale_price"], it["id"]))
            priced_luk += 1
        # Засор позиции в варианте «Лукойл»: J/G лукового листа. Записываем,
        # только если отличается от засора базового варианта (БП 1935: труба
        # ТТ 114х73 — 10% в «Лукойл» против 5% в «ДСП»); иначе NULL → как ДСП.
        if p and p.get("sale_volume") is not None and p.get("qty") \
                and 0 < p["sale_volume"] <= p["qty"]:
            cont_luk = round((1 - p["sale_volume"] / p["qty"]) * 100, 4)
            base_eff = (it["contamination_pct"] if it["contamination_pct"] is not None
                        else (bp_cont if it["sale_type"] == "лом" else 0.0))
            if abs(cont_luk - base_eff) > 0.005:
                conn.execute(
                    "UPDATE bp_items SET contamination_pct_luk = ? WHERE id = ?",
                    (cont_luk, it["id"]))
        # Покупатель варианта отличается от базового (в 1935 «ДСП» везёт на
        # Чермет-Волжский, «Лукойл» — на ВТЗ/Балаково): заводим строку плана
        # продажи варианта на весь объём позиции, иначе покупатель терялся бы.
        v_buyer = (p or {}).get("pos_buyer")
        if v_buyer and norm(v_buyer) != norm(it["buyer"]):
            conn.execute(
                "INSERT INTO bp_item_sales (bp_id, item_id, variant, buyer, "
                "volume_t, sale_price, shipment) VALUES (?, ?, 'luk', ?, ?, ?, ?)",
                (bp_id, it["id"], v_buyer, it["volume_t"],
                 p.get("sale_price"), p.get("shipment")))
            buyers_luk += 1

    v_extras: list[dict] = []
    cost_map_luk = map_old_bp_costs(variant["cost_lines"], total_volume, v_extras)
    costs_luk = 0
    for item, amount in cost_map_luk.items():
        cur = conn.execute(
            "UPDATE bp_costs SET amount_luk = ? WHERE bp_id = ? AND item = ?",
            (amount, bp_id, item))
        costs_luk += cur.rowcount
    for ex in v_extras:
        cur = conn.execute(
            "UPDATE bp_costs SET amount_luk = ? WHERE bp_id = ? AND item = ?",
            (ex["amount"], bp_id, ex["item"]))
        if cur.rowcount == 0:   # статья есть только в варианте «Лукойл»
            conn.execute(
                "INSERT INTO bp_costs (bp_id, section, item, amount, amount_luk) "
                "VALUES (?, ?, ?, 0, ?)",
                (bp_id, ex["section"], ex["item"], ex["amount"]))
        costs_luk += 1

    # Параметры, отличающиеся от базового варианта (срок вывоза, ставки),
    # сохраняются переопределениями JSON — pnl применяет их при variant='luk'.
    bp_row = conn.execute("SELECT * FROM business_plans WHERE id = ?",
                          (bp_id,)).fetchone()
    overrides = {}
    for key, val in (variant.get("params") or {}).items():
        base_val = bp_row[key] if key in bp_row.keys() else None
        if val is not None and (base_val is None
                                or abs(float(base_val) - val) > 1e-9):
            overrides[key] = val

    # Луковый лист P&L полон: статья, пустая в «Лукойл», означает 0, а не
    # «как в ДСП». Иначе строки вроде «Аренда квартир» (5000 в ДСП, пусто в
    # луке) ошибочно наследовались бы (БП 1935: +5000 в персонале).
    conn.execute("UPDATE bp_costs SET amount_luk = 0 "
                 "WHERE bp_id = ? AND amount_luk IS NULL", (bp_id,))

    v_control = variant.get("control") or {}
    conn.execute(
        "UPDATE business_plans SET has_luk = 1, luk_overrides = ?, "
        "control_op_profit_luk = ?, control_net_profit_luk = ? WHERE id = ?",
        (json.dumps(overrides, ensure_ascii=False) if overrides else None,
         v_control.get("operating_profit"), v_control.get("net_profit"), bp_id))
    return (f" Вариант «Лукойл» (листы «_лук»): цены у {priced_luk} позиций, "
            f"статей затрат {costs_luk}"
            + (f", свои покупатели у {buyers_luk} позиций" if buyers_luk else "")
            + ".")


@app.post("/bp/import")
async def bp_import(request: Request, file: UploadFile):
    """Импорт готового БП из внутреннего Excel-формата: создаётся новая карточка,
    заполненная позициями, ценами, сторонами, параметрами лота и статьями
    затрат. P&L пересчитывается приложением и сверяется с числами из файла."""
    role = current_role(request)
    if role not in ("manager", "admin"):
        return redirect("/", "Импортировать БП может менеджер.")
    fname = upload_name(file.filename, "БП.xlsx")
    if not fname.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return redirect("/", "Поддерживается только xlsx/xlsm/xls.")

    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=Path(fname).suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        if tmp_path.suffix.lower() == ".xls":
            try:
                tmp_path = _xls_to_xlsx_tmp(tmp_path)
            except Exception as e:
                return redirect("/", f"Файл .xls не прочитан: {type(e).__name__}: {e}. "
                                     "Сохраните его в Excel как xlsx и загрузите снова.")
        data = list_import.parse_old_bp(tmp_path)
        # Указания продавца («БП ДСП»/«БП Лукойл»/«ШУМЕЙКО») живут на листах
        # перечней той же книги — собрать, пока файл не удалён.
        try:
            instructions = list_import.collect_instructions(tmp_path)
        except Exception:
            instructions = []
    finally:
        tmp_path.unlink(missing_ok=True)

    parsed = data["positions"]
    if not parsed:
        return redirect("/", "Не удалось импортировать БП — позиции не распознаны: "
                        + (" ".join(data["warnings"]) or "проверьте формат файла."))

    params = data["params"]
    total_volume = sum(float(p["qty"] or 0) for p in parsed)
    lot_cost = sum(p["purchase_cost"] for p in parsed
                   if p.get("purchase_cost")) or None

    conn = connect()
    bp_id, number = create_blank_bp(conn)

    # Продавец: сопоставление с контрагентом 1С по имени; при неточном
    # написании («ООО "Лукойл-западная Сибирь"» ↔ «ЛУКОЙЛ-ЗАПАДНАЯ СИБИРЬ
    # ООО») — по совпадению значимых слов.
    seller_id = None
    if params.get("seller_name"):
        seller_id = match_counterparty(conn, params["seller_name"])

    sets = {
        "seller_name": params.get("seller_name"),
        "seller_id": seller_id,
        "division": params.get("division"),
        "tender_ref": params.get("tender_ref"),
        "capital_rate": params.get("capital_rate"),
        "removal_months": params.get("removal_months"),
        "contamination_pct": params.get("contamination_pct"),
        "shipment_type": params.get("shipment_type"),
        "relocation": params.get("relocation"),
        "vat_rate": params.get("vat_rate"),   # выведена из себестоимости с/без НДС
        "vat_unrecovered_pct": params.get("vat_unrecovered_pct"),
        "capital_base": params.get("capital_base"),  # база капитала ≠ закупке (1785)
        "payment_delay_months": params.get("payment_delay_months"),
        "payroll_tax_rate": params.get("payroll_tax_rate"),  # из «Налоги с ФОТ»/«Зарплата»
        "scen_price_delta": params.get("scen_price_delta"),  # лист «(-N)» в книге
        "lot_cost": round(lot_cost, 2) if lot_cost else None,
        "source_type": "Импорт готового БП",
        # Старое название БП — имя исходной книги экономистов (в скобках
        # рядом с номером в карточке и реестре).
        "source_name": trim_source_name(Path(fname).stem),
    }
    sets = {k: v for k, v in sets.items() if v is not None}
    if sets:
        assignment = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(f"UPDATE business_plans SET {assignment}, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (*sets.values(), bp_id))

    bp = conn.execute("SELECT * FROM business_plans WHERE id = ?", (bp_id,)).fetchone()
    matched, priced = insert_parsed_items(conn, bp_id, parsed, bp["buyer_name"], None,
                                          seller_name=bp["seller_name"])
    instructed = apply_sale_instructions(conn, bp_id, instructions)

    extras: list[dict] = []
    cost_map = map_old_bp_costs(data["cost_lines"], total_volume, extras)
    costs_set = 0
    for item, amount in cost_map.items():
        cur = conn.execute(
            "UPDATE bp_costs SET amount = ? WHERE bp_id = ? AND item = ?",
            (amount, bp_id, item))
        costs_set += cur.rowcount
    # Нестандартные статьи файла («Страхование груза») — новыми строками.
    for ex in extras:
        conn.execute(
            "INSERT INTO bp_costs (bp_id, section, item, amount) "
            "VALUES (?, ?, ?, ?)",
            (bp_id, ex["section"], ex["item"], ex["amount"]))
        costs_set += 1

    # Места отгрузки из спецификации — в реестр пунктов: адрес, базовый
    # логистический пункт и расстояние до него. Реестр живёт между сделками,
    # поэтому сопоставление со складом 1С делается один раз.
    points = 0
    for p in parsed:
        place = p.get("division") or p.get("place")
        if not place:
            continue
        if logistics.register_point(
                conn, place, base_point=p.get("base_point"),
                distance_km=p.get("distance_km"),
                seller_name=bp["seller_name"]):
            points += 1
    matched_points = logistics.match_points(conn)
    # Сразу подтягиваем всё, что уже известно из связанных таблиц,
    # чтобы экономист не вводил одно и то же повторно.
    autofill_items(conn, bp_id)
    # График вывоза и сценарии цен — обязательные части БП. Заполняем сразу,
    # иначе экономист открывает карточку с пустыми разделами и не знает, что
    # для них есть отдельные кнопки.
    bp_row = conn.execute("SELECT * FROM business_plans WHERE id = ?",
                          (bp_id,)).fetchone()
    items_row = conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id",
                             (bp_id,)).fetchall()
    try:
        fill_schedule(conn, bp_id, bp_row, items_row)
    except Exception:                 # график не должен ронять импорт
        pass
    if not bp_row["scen_price_delta"]:
        conn.execute("UPDATE business_plans SET scen_price_delta = ? WHERE id = ?",
                     (get_setting(conn, "scen_price_delta", 1000.0), bp_id))

    # Второй вариант расчёта из парного семейства листов («_лук»): свои цены
    # реализации позиций и суммы статей затрат, переключается в карточке.
    variant_msg = apply_import_variant(conn, bp_id, data, total_volume)

    # Сценарии-варианты из сценарных листов книги («лук (-1000)» и т.п.).
    for i, s in enumerate(data.get("scenarios") or []):
        conn.execute(
            "INSERT INTO bp_scenarios (bp_id, name, price_delta, lot_cost, "
            "vat_unrecovered_pct, vat_rate, capital_rate, removal_months, "
            "contamination_pct, tax_rate, control_net_profit, comment, sort) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bp_id, s["name"], s.get("price_delta"), s.get("lot_cost"),
             s.get("vat_unrecovered_pct"), s.get("vat_rate"),
             s.get("capital_rate"), s.get("removal_months"),
             s.get("contamination_pct"), s.get("tax_rate"),
             s.get("control_net_profit"), s.get("comment"), i))

    log(conn, actor(request), bp_id, "import_bp", f"{fname}: {len(parsed)} позиций")

    # Сверка: пересчитанный P&L против чисел из файла.
    bp = conn.execute("SELECT * FROM business_plans WHERE id = ?", (bp_id,)).fetchone()
    items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    pnl = calc.pnl(bp, items, costs, conn)
    conn.commit()
    conn.close()

    msg = (f"Импортирован готовый БП: позиций {len(parsed)}, сопоставлено с 1С "
           f"{matched}, цены у {priced}, статей затрат {costs_set}.")
    if instructed:
        msg += f" Указания перечня у {instructed} позиций."
    if seller_id is None and params.get("seller_name"):
        msg += " Продавец не найден в 1С — уточните вручную."
    file_op = data["control"].get("operating_profit")
    if file_op is not None:
        calc_op = pnl.get("operating_profit", 0.0)
        diff_ok = abs(calc_op - file_op) <= max(1.0, abs(file_op) * 0.01)
        mark = "совпала" if diff_ok else "отличается — проверьте"
        msg += (f" Опер. прибыль: пересчёт {round(calc_op):,}".replace(",", " ")
                + f" против файла {round(file_op):,}".replace(",", " ")
                + f" ({mark}).")
    if points:
        msg += (f" Мест отгрузки в реестре: {points}"
                + (f", сопоставлено со складами 1С {matched_points}" if matched_points else "")
                + ".")
    msg += variant_msg
    # После импорта — на страницу заведения: книга не содержит месяца начала
    # вывоза, шага аукциона и части реквизитов, и человек должен увидеть
    # список недостающего сразу, а не наткнуться на него в расчёте.
    return redirect(f"/bp/{bp_id}/start", msg)


# ─────────────────────────────────────── Карточка БП ────────────────
@app.get("/bp/{bp_id}", response_class=HTMLResponse)
def bp_card(request: Request):
    """Прежний адрес карточки — ведёт на первую вкладку.

    Ссылки на /bp/{id} остались в реестре, журнале и переписке, поэтому
    адрес не убираем, а переводим на вкладку «Сделка»."""
    target = request.url.path.rstrip("/") + "/deal"
    query = request.url.query
    return RedirectResponse(target + (f"?{query}" if query else ""),
                            status_code=303)


def bp_base(request: Request, conn, bp_id: int, tab: str):
    """Общая часть любой вкладки БП: сделка, вариант, KPI и готовность.

    Возвращает (контекст, bp, items, costs, вариант_расчёта) либо
    (None, …) если БП не найден. Тяжёлые расчёты (лестница, нормативная
    модель, матрица цен) вкладка добавляет себе сама — страница должна
    считать только то, что показывает."""
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        return None, None, None, None, None
    variant = resolve_variant(request, bp)
    if request.query_params.get("variant") and variant != bp["active_variant"]:
        conn.execute("UPDATE business_plans SET active_variant = ? WHERE id = ?",
                     (variant, bp_id))
        conn.commit()
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)
    pnl = calc.pnl(bp_v, items_v, costs_v, conn)
    role = current_role(request)

    # Сводка обоих вариантов для переключателя (сравнение и сверка с файлом).
    variant_summary = []
    for code, label in calc.VARIANTS.items():
        if code == "luk" and not bp["has_luk"]:
            continue
        bv, iv, cv = calc.apply_variant(bp, items, costs, code)
        p = pnl if code == variant else calc.pnl(bv, iv, cv, conn)
        control = bp[f"control_net_profit_{code}"]
        control_ok = (abs(p["net_profit"] - control)
                      <= max(1.0, abs(control) * 0.001)) if control is not None else None
        variant_summary.append({
            "code": code, "label": label,
            "sheets": calc.VARIANT_SHEET_HINTS.get(code, ""),
            "net_profit": p["net_profit"], "ros_pct": p["ros_pct"],
            "operating_profit": p["operating_profit"],
            "control": control, "control_ok": control_ok,
            "active": code == variant,
        })

    ctx = {
        **base_ctx(request, conn),
        "bp": bp, "items": items, "costs": costs,
        "variant": variant,
        "variant_label": calc.VARIANTS.get(variant, variant),
        "variants": calc.VARIANTS,
        "variant_labels": calc.VARIANTS,
        "variant_summary": variant_summary,
        "luk_overrides": calc.luk_overrides(bp),
        "luk_override_labels": calc.VARIANT_FIELD_LABELS,
        "pnl": pnl,
        "can_edit": {s: workflow.can_edit_section(s, role, bp["status"])
                     for s in workflow.SECTION_ROLES},
        # Правки в итоговом статусе разрешены (экономист доводит «Лукойл»
        # после закрытия «ДСП»), но карточка предупреждает об этом.
        "final_status": workflow.is_final_status(bp["status"]),
        "actions": workflow.available_actions(bp["status"], role),
        "bitrix_url": bitrix_task_url(conn, bp["bitrix_task_id"]),
        "items_by_id": {it["id"]: it for it in items},
        # Тип сделки: справочник для выбора, текущее значение и что дал бы
        # автоматический разбор состава лота (показываем рядом с ручным
        # выбором, чтобы расхождение было видно).
        "bp_type_list": bp_types.all_types(conn),
        "bp_type_row": bp_types.by_code(conn, bp["bp_type"]),
        "bp_type_detected": bp_types.detect(conn, items),
        # Доля лота: наша часть сделки по реестру Битрикса или заданная вручную.
        "lot_share": deals.describe(conn, bp),
        # Площадка компании (для распределяемых по факту): вручную или по регистру.
        "site_info": {"sites": fact_costs.sites(conn), "current": bp["site"],
                      "detected": fact_model.detect_site(conn, bp, items)},
        **readiness.build(bp_gaps(conn, bp, items, costs, pnl, variant), tab),
    }
    return ctx, bp, items, costs, variant


def bp_gaps(conn, bp, items, costs, pnl, variant: str) -> dict:
    """Чего не хватает на каждой вкладке — для меток на полосе вкладок.

    Запросы дешёвые (пункты отгрузки, наличие графа маршрута), поэтому
    метки считаются на любой вкладке: человек должен видеть пробелы
    соседних разделов, не заходя в них."""
    points = logistics.points_for_bp(conn, bp["id"])
    point_rows = [p["point"] for p in points if p.get("point")]
    routes = conn.execute(
        "SELECT COUNT(*) n FROM shipping_routes WHERE from_point IN "
        "(SELECT id FROM shipping_points)").fetchone()["n"]
    edges = conn.execute("SELECT COUNT(*) n FROM bp_edges WHERE bp_id = ?",
                         (bp["id"],)).fetchone()["n"]
    risks = conn.execute("SELECT COUNT(*) n FROM bp_risks WHERE bp_id = ?",
                         (bp["id"],)).fetchone()["n"]
    return {
        "deal": readiness.deal_gaps(bp),
        "lot": readiness.lot_gaps(bp, items),
        "logistics": readiness.logistics_gaps(point_rows, routes),
        "economics": readiness.economics_gaps(bp, costs, pnl, variant),
        "decision": readiness.decision_gaps(
            bp, risks, bool(edges), any(it["nomen_1c"] for it in items)),
    }


@app.get("/bp/{bp_id}/start", response_class=HTMLResponse)
def bp_start(request: Request, bp_id: int):
    """С чего начать после импорта: что распозналось и чего не хватает.

    Открывается сразу после загрузки книги. Смысл — не отчёт об импорте, а
    список того, что придётся заполнить руками, с последствиями: книга
    экономистов не содержит ни месяца начала вывоза, ни шага аукциона, и
    раньше об этом узнавали, когда расчёт уже расходился."""
    conn = connect()
    ctx, bp, items, costs, variant = bp_base(request, conn, bp_id, "start")
    if ctx is None:
        conn.close()
        return redirect("/", "БП не найден.")
    control = bp[f"control_net_profit_{variant}"]
    diff = (abs(ctx["pnl"]["net_profit"] - control)
            if control is not None else None)
    ctx["facts"] = {
        "count": len(items),
        "volume": sum(float(it["volume_t"] or 0) for it in items),
        "sales": conn.execute(
            "SELECT COUNT(*) n FROM bp_item_sales WHERE bp_id = ?",
            (bp_id,)).fetchone()["n"],
        "control_ok": None if diff is None else diff <= max(1.0, abs(control) * 0.001),
        "control_diff": diff,
    }
    conn.close()
    return templates.TemplateResponse(request, "bp_start.html", ctx)


@app.get("/bp/{bp_id}/deal", response_class=HTMLResponse)
def bp_deal(request: Request, bp_id: int):
    """Вкладка «Сделка»: шапка, стороны, параметры лота."""
    conn = connect()
    ctx, bp, items, _, _ = bp_base(request, conn, bp_id, "deal")
    if ctx is None:
        conn.close()
        return redirect("/", "БП не найден.")
    ctx["lot_hints"] = lot_hints_for_bp(conn, bp, items)
    # Тоннаж лота для пересчёта «стоимость лота ↔ цена закупки, руб/тн».
    ctx["lot_volume"] = sum(_f(it["volume_t"]) for it in items)
    # Покупателей в сделке обычно несколько: поле в «Сторонах» — основной,
    # остальные приходят из плана продажи и из строк позиций.
    ctx["buyers"] = buyers_summary(conn, bp_id, items, ctx["variant"])
    conn.close()
    return templates.TemplateResponse(request, "bp_deal.html", ctx)


# Строк состава лота на одной странице. Лот на 1950 позиций давал 9,8 МБ
# разметки: сервер отдаёт её за полсекунды, а браузер на обычном ноутбуке
# думает секунды и ест память. 200 строк — это около 0,5 МБ и полный экран
# работы; отправка формы при этом безопасна: поля, которых нет на странице,
# не переписываются (app/forms.py, Form.has).
ITEMS_PER_PAGE = 200


def paginate(rows: list, request: Request, per_default: int = ITEMS_PER_PAGE) -> dict:
    """Срез строк для показа и всё, что нужно ссылкам постраничного вывода.

    `?per=all` показывает всё целиком — иногда нужно (печать, поиск глазами
    по всему лоту), поэтому запрет здесь не уместен, но и умолчанием быть не
    может.
    """
    total = len(rows)
    raw_per = (request.query_params.get("per") or "").strip().lower()
    per = 0 if raw_per == "all" else per_default
    if raw_per.isdigit() and int(raw_per) > 0:
        per = min(int(raw_per), 2000)
    if not per or total <= per:
        return {"rows": rows, "total": total, "page": 1, "pages": 1,
                "per": per, "first": 1, "last": total, "all": True}
    pages = (total + per - 1) // per
    raw_page = (request.query_params.get("page") or "1").strip()
    page = int(raw_page) if raw_page.isdigit() else 1
    page = max(1, min(page, pages))
    start = (page - 1) * per
    return {"rows": rows[start:start + per], "total": total, "page": page,
            "pages": pages, "per": per, "first": start + 1,
            "last": min(start + per, total), "all": False}


@app.get("/bp/{bp_id}/lot", response_class=HTMLResponse)
def bp_lot(request: Request, bp_id: int):
    """Вкладка «Лот»: состав позиций, сопоставление с 1С, реквизиты, фото."""
    conn = connect()
    ctx, bp, items, costs, variant = bp_base(request, conn, bp_id, "lot")
    if ctx is None:
        conn.close()
        return redirect("/", "БП не найден.")
    _, items_v, _ = calc.apply_variant(bp, items, costs, variant)
    groups = analytic_groups_for(conn, items)
    # Разрез групп таблицы: место (как в перечне) / номенклатура / оба.
    items_grp = request.query_params.get("grp") or "place"
    if items_grp not in ITEM_GROUP_MODES:
        items_grp = "place"
    all_rows = build_pnl_rows(ctx["pnl"], items_v, items_grp)
    # Сортировка по группе ДО пагинации: иначе группа, разрезанная страницей,
    # появлялась бы на двух страницах двумя половинами.
    all_rows.sort(key=lambda e: e["group"].lower())
    # Итоги считаются по ВСЕМ строкам (ctx["pnl"]), показывается страница:
    # иначе «ИТОГО» под таблицей означало бы итог по видимому куску лота.
    page = paginate(all_rows, request)
    # Позиции этой страницы: по ним же строится таблица реквизитов, иначе
    # она осталась бы на все 1950 строк и одна весила бы три мегабайта.
    by_id = {it["id"]: it for it in items}
    page_ids = dict.fromkeys(e["row"]["id"] for e in page["rows"])
    # Засор в факте 1С — деньги («Списание засора при продаже/перемещении»),
    # у нас — только объём. Показываем фактический руб/т по типу сделки рядом
    # с полем засора, чтобы план продажи и деньги в сверке не расходились.
    contam_hint = cost_matrix.hint(conn, bp["bp_type"], "Переменные", "Списание засора")
    # Ориентир цены по КОНКРЕТНОЙ номенклатуре 1С (регистр продаж): в группе
    # «цветной лом» медь и алюминий различаются вдвое (КП 1956).
    nomen_hints = {it["id"]: pricing.nomen_price_hint(conn, it["nomen_1c"])
                   for it in items if it["nomen_1c"]}
    ctx.update({
        "contam_hint": contam_hint,
        "nomen_hints": {k: v for k, v in nomen_hints.items() if v},
        "assist_on": assist.enabled(),
        "assist_preview": _read_assist_json(bp, "parsed.json"),
        "assist_matches": _read_assist_json(bp, "matches.json"),
        "cost_matrix_from": cost_matrix.period_from(conn),
        "pnl_rows": page["rows"],
        "items_grp": items_grp,
        "items_grp_modes": ITEM_GROUP_MODES,
        "items_page": page,
        "page_items": [by_id[i] for i in page_ids if i in by_id],
        "bulk_undo": conn.execute(
            "SELECT * FROM bp_bulk_undo WHERE bp_id = ? ORDER BY id DESC LIMIT 1",
            (bp_id,)).fetchone(),
        "matching": matching_rows(conn, bp_id, items),
        "nomenclature": conn.execute(
            "SELECT * FROM nomenclature ORDER BY name").fetchall(),
        "sale_types": calc.SALE_TYPES,
        "analytic_groups": groups,
        "aggregates": aggregate_rows(
            conn, bp_id, items, variant,
            by_place=request.query_params.get("agg") == "place"),
        "agg_by_place": request.query_params.get("agg") == "place",
        "units": [r["unit"] for r in conn.execute(
            "SELECT unit, COUNT(*) n FROM ref_nomenclature_1c "
            "WHERE unit IS NOT NULL AND unit <> '' GROUP BY unit "
            "ORDER BY n DESC LIMIT 20").fetchall()],
        "cargo_groups": [r["cargo_group"] for r in conn.execute(
            "SELECT cargo_group, COUNT(*) n FROM ref_nomenclature_1c "
            "WHERE cargo_group IS NOT NULL AND cargo_group <> '' "
            "GROUP BY cargo_group ORDER BY n DESC").fetchall()],
        # Матрица «группа × покупатель»: выбор канала сбыта.
        "price_matrix": pricing.price_matrix(
            conn, [g for g in dict.fromkeys(
                [(it["sale_group"] or "").strip() or groups.get(it["id"]) or ""
                 for it in items]) if g][:6]),
        "photos": conn.execute(
            "SELECT * FROM bp_attachments WHERE bp_id = ? AND kind = 'фото' "
            "ORDER BY COALESCE(taken_at, uploaded_at), id", (bp_id,)).fetchall(),
        # План продажи активного варианта и свободный объём позиций.
        "item_sales": conn.execute(
            "SELECT s.*, i.nomenclature, i.division FROM bp_item_sales s "
            "JOIN bp_items i ON i.id = s.item_id "
            "WHERE s.bp_id = ? AND s.variant = ? ORDER BY s.item_id, s.sort, s.id",
            (bp_id, variant)).fetchall(),
        "item_free": {
            r["id"]: r["free"] for r in conn.execute(
                "SELECT i.id, ROUND(i.volume_t - COALESCE((SELECT SUM(s.volume_t) "
                "FROM bp_item_sales s WHERE s.item_id = i.id AND s.variant = ?), 0), 6) "
                "AS free FROM bp_items i WHERE i.bp_id = ?",
                (variant, bp_id)).fetchall()},
    })
    conn.close()
    return templates.TemplateResponse(request, "bp_lot.html", ctx)


@app.get("/bp/{bp_id}/logistics", response_class=HTMLResponse)
def bp_logistics(request: Request, bp_id: int):
    """Вкладка «Логистика»: пункты отгрузки, плечи, рейсы и машины."""
    conn = connect()
    ctx, bp, items, costs, variant = bp_base(request, conn, bp_id, "logistics")
    if ctx is None:
        conn.close()
        return redirect("/", "БП не найден.")
    _, items_v, _ = calc.apply_variant(bp, items, costs, variant)
    ctx.update({
        "ship_points": logistics.points_for_bp(conn, bp_id),
        "trips": norms.trips_plan(items_v, conn),
        "geo_enabled": geo.enabled(),
        "geo_provider": geo.provider_label(),
    })
    conn.close()
    return templates.TemplateResponse(request, "bp_logistics.html", ctx)


@app.get("/bp/{bp_id}/economics", response_class=HTMLResponse)
def bp_economics(request: Request, bp_id: int):
    """Вкладка «Экономика»: затраты, P&L, базы, аукцион, сценарии."""
    conn = connect()
    ctx, bp, items, costs, variant = bp_base(request, conn, bp_id, "economics")
    if ctx is None:
        conn.close()
        return redirect("/", "БП не найден.")
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)
    # Ставки перевозчика из приказа 1С — подсказки к плечам маршрута.
    rate_edges = conn.execute(
        "SELECT e.id, e.transport, e.volume_t, a.label AS from_label, "
        "b.label AS to_label FROM bp_edges e "
        "JOIN bp_nodes a ON a.id = e.from_node "
        "JOIN bp_nodes b ON b.id = e.to_node WHERE e.bp_id = ?",
        (bp_id,)).fetchall()
    # Нормативная модель считается один раз: она же даёт этапы для разреза
    # затрат по базам (логистика / переработка).
    cost_model = norms.evaluate_model(items_v, conn,
                                      load_cost_overrides(conn, bp_id))
    model_stages = {s["name"]: {b["base"]: b["amount"] for b in s["bases"]}
                    for s in cost_model["stages"]}
    ctx.update({
        "pnl_rows": build_pnl_rows(ctx["pnl"], items_v),
        "cost_model": cost_model,
        # Фактический слой: ставки факта × тоннаж сделки — рядом с моделью по
        # нормативам и с суммами в статьях (три слоя: книга / нормативы / факт).
        "fact_layer": fact_model.evaluate(bp_v, items_v, conn),
        # Независимая модель результата: что покажет сделка, если пойдёт как
        # похожие закрытые (тип + площадка), с диапазоном и точностью модели.
        "outcome": outcome.expect(conn, bp_v, items_v, fact_model.resolve_site(conn, bp_v, items_v)[0]),
        "outcome_accuracy": {r["bp_type"]: r for r in outcome.accuracy(conn)},
        "book_type_plan": book_archive.for_type(conn, bp["bp_type"]),
        "book_versions": book_archive.for_deal(conn, deals.request_no(bp)),
        "cost_hints": cost_hints_for_items(items),
        # Ориентиры матрицы затрат по статьям — сколько эта статья стоила в
        # тонне у сделок такого же типа и по факту 1С. Только показываем.
        "matrix_hints": {(c["section"], c["item"]):
                         cost_matrix.hint(conn, bp["bp_type"], c["section"],
                                          c["item"])
                         for c in costs}
                        | {(c["section"], c["article"]):
                           cost_matrix.hint(conn, bp["bp_type"], c["section"],
                                            c["article"])
                           for c in cost_model["components"]},
        # Пара «факт регистра 1С» / «что закладывали в расчётах» по статье:
        # разница между ними — то, чему директор просил учиться.
        "matrix_pairs": {(c["section"], c["item"]):
                         cost_matrix.hint_pair(conn, bp["bp_type"], c["section"], c["item"])
                         for c in costs}
                        | {(c["section"], c["article"]):
                           cost_matrix.hint_pair(conn, bp["bp_type"], c["section"], c["article"])
                           for c in cost_model["components"]},
        "cost_matrix_from": cost_matrix.period_from(conn),
        "assist_on": assist.enabled(),
        "assist_explain": _read_assist(bp, "explain.txt"),
        "rate_hints": rates.hints_for_edges(conn, rate_edges),
        "rates_loaded": rates.summary(conn)["total"],
        # Объём реализации: по нему статьи затрат переводятся в руб/тн.
        "sale_volume": ctx["pnl"]["sale_volume"],
        # Происхождение каждой суммы и ручные отклонения от накопленного
        # уровня: цифра без источника — то, что директор просил исключить.
        "cost_origins": origin.for_bp(conn, bp_id, variant),
        # Модельная таблица показывает статьи по названию, а источник
        # хранится по идентификатору строки затрат — нужен мостик.
        "cost_id_by_item": {c["item"]: c["id"] for c in costs},
        "origin_deviations": origin.deviations(
            conn, bp_id, variant, costs, ctx["pnl"]["sale_volume"],
            {(c["section"], c["item"]):
             cost_matrix.hint(conn, bp["bp_type"], c["section"], c["item"])
             for c in costs}),
        "cost_sections": ["Переменные", "Персонал", "Постоянные",
                          "Административные", "Прочие"],
        "model_articles": norms.MODEL_ARTICLES,
        "base_pnl": calc.base_pnl(bp_v, items_v, costs_v, conn, model_stages),
        "type_summary": calc.division_summary(bp_v, items_v, costs_v, conn,
                                              model_stages),
        "ladder": calc.auction_ladder(bp_v, items_v, costs_v, conn),
        "scenarios": calc.price_scenarios(bp_v, items_v, costs_v, conn),
        "scenario_variants": calc.scenario_variants(
            bp_v, items_v, costs_v, conn,
            conn.execute("SELECT * FROM bp_scenarios WHERE bp_id = ? "
                         "ORDER BY sort, id", (bp_id,)).fetchall()),
        "nomenclature": conn.execute(
            "SELECT * FROM nomenclature ORDER BY name").fetchall(),
    })
    conn.close()
    return templates.TemplateResponse(request, "bp_economics.html", ctx)


@app.get("/bp/{bp_id}/decision", response_class=HTMLResponse)
def bp_decision(request: Request, bp_id: int):
    """Вкладка «Решение»: риски, итог, статус, согласование и версии."""
    conn = connect()
    ctx, bp, items, _, _ = bp_base(request, conn, bp_id, "decision")
    if ctx is None:
        conn.close()
        return redirect("/", "БП не найден.")
    risks = conn.execute("SELECT * FROM bp_risks WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    ctx.update({
        "risks": risks,
        "risk_integral": calc.integral_risk(risks),
        # Вероятность и влияние показываются словами, хранятся буквами.
        "probability_labels": calc.PROBABILITY_LABELS,
        "impact_labels": calc.IMPACT_LABELS,
        "analytic_groups": analytic_groups_for(conn, items),
        "approvals": conn.execute(
            "SELECT * FROM bp_approvals WHERE bp_id = ? ORDER BY id",
            (bp_id,)).fetchall(),
        "versions": conn.execute(
            "SELECT id, version, author, created_at, variant, scenario, "
            "net_profit_bsp, net_profit_luk, change_note, approved_at, "
            "approved_by FROM bp_versions "
            "WHERE bp_id = ? ORDER BY version DESC", (bp_id,)).fetchall(),
    })
    conn.close()
    return templates.TemplateResponse(request, "bp_decision.html", ctx)


@app.get("/bp/{bp_id}/versions/diff", response_class=HTMLResponse)
def versions_diff(request: Request, bp_id: int):
    """Что изменилось между двумя версиями расчёта.

    Версии выбираются номерами в параметрах a и b; по умолчанию —
    предпоследняя против последней («что поменяла последняя правка»)."""
    conn = connect()
    ctx = base_ctx(request, conn)
    bp = conn.execute("SELECT * FROM business_plans WHERE id = ?",
                      (bp_id,)).fetchone()
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    rows = conn.execute(
        "SELECT id, version, author, created_at, variant, scenario, "
        "net_profit_bsp, net_profit_luk, change_note FROM bp_versions "
        "WHERE bp_id = ? ORDER BY version", (bp_id,)).fetchall()
    if len(rows) < 2:
        conn.close()
        # Без якоря: msg добавляется параметром, а после «#» он не читается.
        return back(request, bp_id,
                        "Для сравнения нужны хотя бы две версии расчёта.")

    def _num(name: str, default: int) -> int:
        try:
            return int(request.query_params.get(name, default))
        except (TypeError, ValueError):
            return default

    known = {r["version"] for r in rows}
    a = _num("a", rows[-2]["version"])
    b = _num("b", rows[-1]["version"])
    if a not in known:
        a = rows[-2]["version"]
    if b not in known:
        b = rows[-1]["version"]
    if a == b:
        a = rows[-2]["version"] if b == rows[-1]["version"] else rows[-1]["version"]
    # Слева всегда более ранняя версия — иначе «стало/было» читается наоборот.
    if a > b:
        a, b = b, a

    snaps = {}
    for version in (a, b):
        raw = conn.execute(
            "SELECT snapshot_json FROM bp_versions WHERE bp_id = ? AND version = ?",
            (bp_id, version)).fetchone()
        try:
            snaps[version] = json.loads(raw["snapshot_json"]) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            snaps[version] = {}
    conn.close()

    by_version = {r["version"]: r for r in rows}
    ctx.update({
        "bp": bp,
        "versions": rows,
        "a": by_version[a], "b": by_version[b],
        "diff": versions.diff(snaps[a], snaps[b]),
        "variant_labels": calc.VARIANTS,
    })
    return templates.TemplateResponse(request, "versions_diff.html", ctx)


@app.post("/bp/{bp_id}/versions/{version}/approve")
def approve_version(request: Request, bp_id: int, version: int,
                    action: str = Form("set")):
    """Утвердить версию расчёта (или снять утверждение тем же маршрутом).

    Утверждённая версия — одна на БП: по её снимку формируется печатная
    форма, и надпись «версия N» в ней означает именно согласованный расчёт,
    а не «что было в базе в момент печати». Новое утверждение снимает
    прежнее автоматически — две «утверждённые» версии сразу читались бы
    как противоречие.
    """
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return back(request, bp_id,
                    "Утверждать версию может экономист или руководитель.",
                    default_tab="decision")
    conn = connect()
    row = conn.execute(
        "SELECT id, approved_at FROM bp_versions WHERE bp_id = ? AND version = ?",
        (bp_id, version)).fetchone()
    if row is None:
        conn.close()
        return back(request, bp_id, f"Версия {version} не найдена.",
                    default_tab="decision")
    if action == "clear":
        conn.execute("UPDATE bp_versions SET approved_at = NULL, "
                     "approved_by = NULL WHERE id = ?", (row["id"],))
        msg = f"Утверждение с версии {version} снято."
        log(conn, actor(request), bp_id, "version_unapprove", f"версия {version}")
    else:
        conn.execute("UPDATE bp_versions SET approved_at = NULL, "
                     "approved_by = NULL WHERE bp_id = ?", (bp_id,))
        conn.execute("UPDATE bp_versions SET approved_at = datetime('now'), "
                     "approved_by = ? WHERE id = ?",
                     (actor_name(request), row["id"]))
        msg = (f"Версия {version} утверждена — печатная форма теперь "
               f"формируется по ней.")
        log(conn, actor(request), bp_id, "version_approve", f"версия {version}")
    conn.commit()
    conn.close()
    return back(request, bp_id, msg, default_tab="decision")


@app.post("/bp/{bp_id}/section/{section}")
async def save_section(request: Request, bp_id: int, section: str):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or section not in SECTION_FIELDS:
        conn.close()
        return redirect("/", "Раздел не найден.")
    if not workflow.can_edit_section(section, role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на редактирование этого раздела.")

    form = await read_form(request)
    stale = edit_conflict(conn, request, bp, form)
    if stale:
        conn.close()
        return back(request, bp_id, stale)

    values, columns = [], []
    for field in SECTION_FIELDS[section]:
        # Поле, не пришедшее в отправке, не трогаем. Раньше сюда попадали ВСЕ
        # поля раздела, и неполная отправка (оборванная передача, форма без
        # части полей) обнуляла весь раздел разом: так 17.08.2026 обнулились
        # все 13 статей затрат боевого БП.
        if not form.has(field):
            continue
        values.append(form.num(field) if field in FLOAT_FIELDS
                      else form.text(field))
        columns.append(field)

    # Ошибка ввода — не пишем НИЧЕГО: раздел сохраняется целиком либо никак,
    # иначе часть полей уедет, а часть останется, и расчёт будет смешанным.
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    if not columns:
        conn.close()
        return back(request, bp_id, "Форма пришла без полей — ничего не сохранено.")

    # Контрагент: по id из автокомплита, иначе — поиск по введённому имени
    if section == "parties":
        for id_col, name_col in [("seller_id", "seller_name"), ("buyer_id", "buyer_name")]:
            if id_col not in columns or name_col not in columns:
                continue
            cid = values[columns.index(id_col)]
            cname = values[columns.index(name_col)]
            if cid:
                ref = conn.execute("SELECT name FROM ref_counterparties WHERE id = ?",
                                   (cid,)).fetchone()
                if ref and not cname:
                    values[columns.index(name_col)] = ref["name"]
            elif cname:
                ref = conn.execute("SELECT id FROM ref_counterparties WHERE name = ? "
                                   "LIMIT 1", (cname,)).fetchone()
                if ref:
                    values[columns.index(id_col)] = ref["id"]

    sets = ", ".join(f"{c} = ?" for c in columns)
    conn.execute(f"UPDATE business_plans SET {sets}, updated_at = datetime('now') "
                 f"WHERE id = ?", (*values, bp_id))
    log(conn, actor(request), bp_id, "edit_section", section)
    conn.commit()
    conn.close()
    return back(request, bp_id, "Раздел сохранён.")


@app.post("/bp/{bp_id}/type")
def set_bp_type(request: Request, bp_id: int, bp_type: str = Form("")):
    """Тип сделки: выбор человеком или возврат к определению по составу лота.

    Ручной выбор запоминается и не перетирается повторной загрузкой перечня —
    экономист мог назвать сделку кабельной, хотя по тоннажу она смешанная.
    """
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("header", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение шапки сделки.")
    code = (bp_type or "").strip()
    if code == "auto":
        bp_types.set_manual(conn, bp_id, None)
        result = bp_types.apply_auto(conn, bp_id, items)
        msg = (f"Тип определён по составу лота: "
               f"{bp_types.label(conn, result['code'])} — {result['reason']}")
        log(conn, actor(request), bp_id, "bp_type_auto", str(result["code"]))
    elif code and bp_types.by_code(conn, code) is None:
        conn.close()
        return back(request, bp_id, "Такого типа сделки нет в справочнике.")
    else:
        bp_types.set_manual(conn, bp_id, code or None)
        msg = (f"Тип сделки задан вручную: {bp_types.label(conn, code)}."
               if code else "Тип сделки снят.")
        log(conn, actor(request), bp_id, "bp_type_manual", code or "снят")
    conn.commit()
    conn.close()
    return back(request, bp_id, msg)


@app.post("/bp/{bp_id}/share")
def set_lot_share(request: Request, bp_id: int, lot_share_pct: str = Form("")):
    """Доля лота: число из шапки или возврат к реестру сделок («auto»).

    Ручная доля не перетирается импортом реестра — экономист может знать о
    договорённости с партнёром раньше, чем она появится в Битриксе.
    """
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("header", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение шапки сделки.")
    raw = (lot_share_pct or "").strip().replace(",", ".").replace("%", "")
    if raw in ("", "auto"):
        deals.set_manual(conn, bp_id, None)
        info = deals.describe(conn, bp)
        res = deals.apply_auto(conn, bp_id)
        if res.get("found") and res.get("share_pct") is not None:
            msg = f"Доля лота взята из реестра сделок: {res['share_pct']:g} % (сделка № {res['deal_no']})."
        else:
            msg = ("Сделка в реестре не найдена, доля не задана — считаем весь лот своим."
                   if info["registry_rows"] else
                   "Реестр сделок ещё не загружен — считаем весь лот своим.")
        log(conn, actor(request), bp_id, "lot_share_auto", str(res.get("share_pct")))
    else:
        try:
            pct = float(raw)
        except ValueError:
            conn.close()
            return back(request, bp_id, "Доля лота должна быть числом процентов, например 50.")
        if not 0 < pct <= 100:
            conn.close()
            return back(request, bp_id, "Доля лота — от 0 до 100 процентов.")
        deals.set_manual(conn, bp_id, pct)
        msg = f"Доля лота задана вручную: {pct:g} %."
        log(conn, actor(request), bp_id, "lot_share_manual", f"{pct:g}")
    conn.commit()
    conn.close()
    return back(request, bp_id, msg)


@app.post("/bp/{bp_id}/site")
def set_site(request: Request, bp_id: int, site: str = Form("")):
    """Площадка сделки: из справочника «Цеха и базы» 1С или «по факту»."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("header", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение шапки сделки.")
    val = (site or "").strip()
    if val and val != "auto" and val not in fact_costs.sites(conn):
        conn.close()
        return back(request, bp_id, "Такой площадки нет в справочнике «Цеха и базы».")
    conn.execute("UPDATE business_plans SET site = ?, site_source = ? WHERE id = ?",
                 (val or None, "manual" if val and val != "auto" else None, bp_id))
    log(conn, actor(request), bp_id, "site", val or "по факту")
    conn.commit()
    conn.close()
    if val and val != "auto":
        return back(request, bp_id, f"Площадка задана: {val}.")
    conn = connect()
    d = fact_model.detect_site(conn, bp)
    conn.close()
    return back(request, bp_id, f"Площадка по факту: {d['site'] or 'не определена'} ({d['reason']}).")


@app.post("/bp/{bp_id}/costs/fact")
def costs_from_fact(request: Request, bp_id: int, variant: str = Form("bsp")):
    """Записать в статьи затрат суммы фактического слоя: ставки факта × тоннаж
    сделки на долю. Явная кнопка, как у модели по нормативам."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("costs", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение затрат.")
    variant = variant if variant in ("bsp", "luk") else "bsp"
    res = fact_model.write(conn, bp, items, variant, author=actor(request))
    log(conn, actor(request), bp_id, "costs_fact",
        f"{res.get('written', 0)} статей, {res.get('total', 0):,.0f} руб".replace(",", " "))
    conn.commit()
    conn.close()
    if not res.get("articles"):
        return redirect(f"/bp/{bp_id}/economics", "Факта для этой сделки нет: " + "; ".join(res.get("missing") or ["нет данных"]))
    msg = (f"Записано по факту: {res['written']} статей на {res['total']:,.0f} руб "
           f"({res['tons']:,.1f} тн на долю {res['share_pct']:g} %).").replace(",", " ")
    if res.get("missing"):
        msg += " Не учтено: " + "; ".join(res["missing"])
    return redirect(f"/bp/{bp_id}/economics", msg)


@app.post("/bp/{bp_id}/lot/apply-hints")
def apply_lot_hints(request: Request, bp_id: int):
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("lot", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение параметров лота.")
    hints = lot_hints_for_bp(conn, bp, items)["items"]
    sets, values, applied = [], [], 0
    allowed = set(SECTION_FIELDS["lot"])
    for h in hints:
        field = h["field"]
        if field in allowed and not bp[field]:
            sets.append(f"{field} = ?")
            values.append(h["value"])
            applied += 1
    if sets:
        conn.execute(f"UPDATE business_plans SET {', '.join(sets)}, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (*values, bp_id))
    log(conn, actor(request), bp_id, "apply_lot_hints", f"{applied} полей")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/deal",
                    f"Применено подсказок по параметрам лота: {applied}.")


def analytic_groups_for(conn, items: list) -> dict[int, str | None]:
    """Группа аналитического учёта (справочник из файла 1С) по позициям:
    группа грузов сопоставленной номенклатуры 1С, при её отсутствии в 1С —
    группа из синергии номенклатуры БП. Ключ — id позиции."""
    by_guid = {r["guid"]: r["cargo_group"] for r in conn.execute(
        "SELECT guid, cargo_group FROM ref_nomenclature_1c "
        "WHERE guid IN (%s)" % ",".join("?" * len(items)),
        tuple(it["nomen_1c_guid"] for it in items)).fetchall()} if items else {}
    out = {}
    for it in items:
        # Заданная в карточке группа — главнее справочной: экономист мог
        # уточнить классификацию вручную.
        group = (it["cargo_group"] or "").strip() if "cargo_group" in it.keys() else ""
        if not group:
            group = by_guid.get(it["nomen_1c_guid"])
        if not group:
            ref = lookup_nomenclature(conn, it["nomenclature"])
            group = ref["group_1c"] if ref else None
        out[it["id"]] = group or None
    return out


def lookup_nomenclature(conn, name: str):
    """Позиция справочника номенклатуры БП: точное имя или синонимы (синергия с 1С)."""
    ref = conn.execute("SELECT * FROM nomenclature WHERE lower(name) = lower(?)",
                       (name.strip(),)).fetchone()
    if ref is None:
        low = name.strip().lower()
        for r in conn.execute("SELECT * FROM nomenclature").fetchall():
            aliases = [a.strip() for a in (r["aliases"] or "").split(";") if a.strip()]
            if low in aliases or any(a in low for a in aliases if len(a) > 5):
                return r
    return ref


def infer_item_attrs(conn, name: str) -> dict:
    """Категория и тип закупки из справочника или типовой строки КП."""
    ref = lookup_nomenclature(conn, name)
    if ref:
        return {"category": ref["category"], "purchase_type": ref["purchase_type"],
                "ref": ref}
    text = (name or "").lower().replace("ё", "е")
    category = None
    purchase_type = "лом"
    cat = re.search(r"(?<!\d)(\d{1,2})\s*[аa]\b", text)
    if cat and "металлолом" in text:
        category = f"{cat.group(1)}А"
    elif "черн" in text and "металлолом" in text:
        category = "черный металл"
    elif "мед" in text:
        category, purchase_type = "медь", "цветмет"
    elif "алюмин" in text:
        category, purchase_type = "алюминий", "цветмет"
    elif "кабел" in text:
        category, purchase_type = "кабель", "кабель"
    elif "труб" in text or "нкт" in text:
        category, purchase_type = "труба", "труба"
    return {"category": category, "purchase_type": purchase_type, "ref": None}


ITEM_GROUP_MODES = {
    "place": "по месту / поставщику",
    "nomen": "по номенклатуре",
    "place_nomen": "место × номенклатура",
}


def build_pnl_rows(pnl: dict, items: list, mode: str = "place") -> list[dict]:
    """Связка расчётной строки с исходной позицией для группировки в интерфейсе.

    mode задаёт разрез групп таблицы: по месту (как в перечне), по
    номенклатуре (1000 строк одной трубы складываются в одну группу с
    итогами) или место × номенклатура. Правятся строки одинаково в любом
    разрезе — меняется только порядок и заголовки групп."""
    if mode not in ITEM_GROUP_MODES:
        mode = "place"
    items_by_id = {it["id"]: it for it in items}
    rows = []
    seen: set = set()
    for r in pnl["rows"]:
        item = items_by_id.get(r["id"])
        place = (r["supplier"] or (item["supplier"] if item else "") or
                 r["division"] or (item["division"] if item else "") or
                 "Без поставщика").strip()
        nomen = (r["nomenclature"] or "Без наименования").strip()
        group = (nomen if mode == "nomen"
                 else f"{place} · {nomen}" if mode == "place_nomen"
                 else place)
        # Позиция, разделённая между покупателями, даёт несколько строк.
        # Атрибуты самой позиции (единица, группа, засор) правятся только в
        # первой строке — иначе одно поле дублировалось бы в каждой части.
        first = r["id"] not in seen
        seen.add(r["id"])
        rows.append({"row": r, "item": item, "group": group,
                     "first_of_item": first})
    return rows


def _plain_text(*values) -> str:
    return " ".join(str(v or "") for v in values).lower().replace("ё", "е")


def _km_from_text(text: str) -> float | None:
    match = re.search(r"(\d{1,4}(?:[,.]\d{1,2})?)\s*(?:км|km)\b", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def cost_hints_for_items(items: list) -> dict:
    """Предложения по драйверам затрат из текста перечня и карточек позиций.

    Подсказки не меняют расчёты сами по себе: пользователь применяет их явно.
    """
    hints = []
    summary = {"own": 0, "cut": 0, "distance": 0, "shipment": 0}
    for it in items:
        text = _plain_text(it["nomenclature"], it["note"], it["division"],
                           it["warehouse"], it["shipment"], it["buyer"])
        fields = {}
        reasons = []

        if it["own_transport_pct"] is None:
            if any(w in text for w in ("самовывоз", "собственн", "своим транспорт")):
                fields["own_transport_pct"] = 100.0
                summary["own"] += 1
                reasons.append("перевозка похожа на собственный транспорт")
            elif any(w in text for w in ("наем", "наемн", "перевозчик", "доставка")):
                fields["own_transport_pct"] = 0.0
                summary["own"] += 1
                reasons.append("перевозка похожа на наёмный транспорт")

        if it["workshop_cut_pct"] is None:
            if any(w in text for w in ("резк", "подрез", "разделк", "демонтаж")):
                fields["workshop_cut_pct"] = 100.0
                summary["cut"] += 1
                reasons.append("есть признаки обязательной обработки на цехе")
            elif any(w in text for w in ("негабарит", "труба", "металлоконструкц")):
                fields["workshop_cut_pct"] = 50.0
                summary["cut"] += 1
                reasons.append("есть признаки частичной обработки на цехе")

        if it["distance_km"] is None:
            km = _km_from_text(text)
            if km is not None:
                fields["distance_km"] = km
                summary["distance"] += 1
                reasons.append(f"найдено расстояние {km:g} км")

        if not (it["shipment"] or "").strip():
            if any(w in text for w in ("вагон", "ж/д", "жд ")):
                fields["shipment"] = "вагоны"
                summary["shipment"] += 1
                reasons.append("есть признаки железнодорожной отгрузки")
            elif "самовывоз" in text:
                fields["shipment"] = "самовывоз"
                summary["shipment"] += 1
                reasons.append("есть признак самовывоза")
            elif "доставка" in text:
                fields["shipment"] = "доставка"
                summary["shipment"] += 1
                reasons.append("есть признак доставки")
            elif "вывоз" in text:
                fields["shipment"] = "вывоз с базы"
                summary["shipment"] += 1
                reasons.append("есть признак вывоза с базы")

        if fields:
            hints.append({"item": it, "fields": fields, "reasons": reasons})

    return {"items": hints, "summary": summary, "total": len(hints)}


def _avg_nonzero(rows: list, field: str) -> tuple[float | None, int]:
    vals = [float(r[field]) for r in rows if r[field] not in (None, 0)]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


def latest_attachment_path(conn, bp) -> Path | None:
    row = conn.execute(
        "SELECT filename FROM bp_attachments WHERE bp_id = ? ORDER BY id DESC LIMIT 1",
        (bp["id"],),
    ).fetchone()
    if not row:
        return None
    path = (ATTACH_DIR / bp["bp_number"] / row["filename"]).resolve()
    return path if path.is_file() and ATTACH_DIR.resolve() in path.parents else None


def historical_purchase_sale_ratio(conn, bp_id: int) -> tuple[float | None, int]:
    rows = conn.execute(
        "SELECT bp.id, bp.lot_cost, SUM(i.sale_price * i.volume_t) AS sale_plan "
        "FROM business_plans bp JOIN bp_items i ON i.bp_id = bp.id "
        "WHERE bp.id != ? AND bp.lot_cost IS NOT NULL AND bp.lot_cost > 0 "
        "AND i.sale_price IS NOT NULL AND i.sale_price > 0 "
        "GROUP BY bp.id HAVING sale_plan > 0",
        (bp_id,),
    ).fetchall()
    if not rows:
        return None, 0
    purchase = sum(float(r["lot_cost"]) for r in rows)
    sale = sum(float(r["sale_plan"]) for r in rows)
    return (purchase / sale if sale else None), len(rows)


def lot_hints_for_bp(conn, bp, items: list) -> dict:
    """Подсказки параметров лота на основе накопленной истории БП."""
    hints: list[dict] = []
    if not items:
        return {"items": hints, "total": 0}

    current_volume = sum(float(it["volume_t"] or 0) for it in items)
    history = conn.execute(
        "SELECT bp.id, bp.lot_cost, bp.auction_step, bp.contamination_pct, "
        "bp.removal_months, bp.shipment_loss_pct, SUM(i.volume_t) AS volume "
        "FROM business_plans bp JOIN bp_items i ON i.bp_id = bp.id "
        "WHERE bp.id != ? AND bp.lot_cost IS NOT NULL AND bp.lot_cost > 0 "
        "GROUP BY bp.id",
        (bp["id"],),
    ).fetchall()

    price_by_category = conn.execute(
        "SELECT i.category, AVG(bp.lot_cost / totals.volume) AS price_per_t, "
        "COUNT(DISTINCT bp.id) AS bp_count "
        "FROM bp_items i "
        "JOIN business_plans bp ON bp.id = i.bp_id "
        "JOIN (SELECT bp_id, SUM(volume_t) AS volume FROM bp_items GROUP BY bp_id) totals "
        "ON totals.bp_id = bp.id "
        "WHERE bp.id != ? AND bp.lot_cost IS NOT NULL AND bp.lot_cost > 0 "
        "AND i.category IS NOT NULL AND totals.volume > 0 "
        "GROUP BY i.category",
        (bp["id"],),
    ).fetchall()
    cat_price = {r["category"]: r for r in price_by_category}
    market_prices = {}
    attachment_path = latest_attachment_path(conn, bp)
    if attachment_path:
        try:
            market_prices = list_import.detect_market_prices(attachment_path)
        except Exception:
            market_prices = {}
    purchase_sale_ratio, ratio_sample = historical_purchase_sale_ratio(conn, bp["id"])

    if not bp["lot_cost"] and current_volume:
        market_weighted, market_covered, market_samples = 0.0, 0.0, 0
        market_categories = set()
        if purchase_sale_ratio:
            for it in items:
                row = market_prices.get(it["category"])
                vol = float(it["volume_t"] or 0)
                if row and vol:
                    market_weighted += vol * float(row["price"]) * purchase_sale_ratio
                    market_covered += vol
                    market_categories.add(it["category"])
            market_samples = sum(int(market_prices[c]["samples"]) for c in market_categories)

        weighted, covered, sample = 0.0, 0.0, 0
        for it in items:
            row = cat_price.get(it["category"])
            vol = float(it["volume_t"] or 0)
            if row and vol:
                weighted += vol * float(row["price_per_t"])
                covered += vol
                sample = max(sample, int(row["bp_count"]))

        if market_covered:
            history_price = (weighted / covered) if covered else None
            market_price = market_weighted / market_covered
            overall_price = (market_price * 0.7 + history_price * 0.3
                             if history_price else market_price)
            sample = max(ratio_sample, sample)
            reason = ("по ценам реализации из текущего КП и историческому "
                      "соотношению закупка/реализация")
            source = f"{market_samples} ценовых строк КП"
        elif covered:
            overall_price = weighted / covered
            reason = "по исторической цене закупки похожих категорий"
            source = f"{sample} БП в базе"
        elif history:
            history_volume = sum(float(r["volume"] or 0) for r in history)
            overall_price = (sum(float(r["lot_cost"]) for r in history) /
                             history_volume) if history_volume else None
            sample = len(history)
            reason = "по средней цене закупки заполненных БП"
            source = f"{sample} БП в базе"
        else:
            overall_price = None
            reason = ""
            source = ""
        if overall_price:
            hints.append({
                "field": "lot_cost",
                "label": "Стоимость лота без НДС",
                "value": round(overall_price * current_volume, 0),
                "display": f"{overall_price:,.0f}".replace(",", " ") + " руб/тн",
                "reason": reason,
                "confidence": min(95, 45 + sample * 10),
                "sample": sample,
                "source": source,
            })

    avg_fields = [
        ("auction_step", "Шаг аукциона", "руб"),
        ("contamination_pct", "Засор черного лома", "%"),
        ("removal_months", "Срок вывоза", "мес."),
        ("shipment_loss_pct", "Потери при отгрузке", "%"),
    ]
    for field, label, unit in avg_fields:
        if bp[field]:
            continue
        avg, count = _avg_nonzero(history, field)
        if avg is not None:
            hints.append({
                "field": field,
                "label": label,
                "value": round(avg, 2),
                "display": f"{avg:,.2f}".replace(",", " ") + f" {unit}",
                "reason": "среднее по ранее заполненным БП",
                "confidence": min(90, 40 + count * 10),
                "sample": count,
                "source": f"{count} БП в базе",
            })

    # Доля невозмещённого НДС — правило экономистов (запись 10.08.2026):
    # доходный БП принимает 100%; убыточный — долю, выводящую ЧП в ноль
    # (РИТЭК 1935: 30%). Подсказка справочная, применяется вручную.
    hint = vat_share_hint(conn, bp, items)
    if hint:
        hints.append(hint)

    # Рыночная статистика из выгрузок 1С (import_1c_csv.py): фактические
    # цены реализации по группам грузов и цены закупки у этого продавца.
    stats_1c = market_stats_1c(conn, bp, items)
    return {"items": hints, "total": len(hints), "stats_1c": stats_1c}


def vat_share_hint(conn, bp, items: list) -> dict | None:
    """Предложение доли невозмещённого НДС по правилу экономистов: если при
    100% чистая прибыль не уходит в минус — 100%; иначе наибольшая доля
    (шаг 5%), при которой ЧП ещё неотрицательна."""
    costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ? ORDER BY id",
                         (bp["id"],)).fetchall()

    def np_at(share: float) -> float:
        bpd = calc._to_dict(bp)
        bpd["vat_unrecovered_pct"] = share
        return calc.pnl(bpd, items, costs, conn)["net_profit"]

    base = calc.pnl(bp, items, costs, conn)
    if base["vat_unrecovered_full"] <= 0:
        return None                     # смены типа нет — доля не влияет
    current = base["vat_unrecovered_pct"]
    if np_at(100.0) >= 0:
        suggest = 100.0
        reason = ("доходность позволяет отнести на затраты весь "
                  "невозмещённый НДС (правило экономистов: доходный БП — 100%)")
    else:
        suggest = 0.0
        for share in range(95, -5, -5):
            if np_at(float(share)) >= 0:
                suggest = float(share)
                break
        reason = ("при большей доле БП уходит в минус — предложена "
                  "наибольшая доля с неотрицательной ЧП (как РИТЭК 1935: 30%)")
    if abs(suggest - current) < 0.5:
        return None                     # текущая доля уже соответствует
    return {
        "field": "vat_unrecovered_pct",
        "label": "Доля невозмещённого НДС на затраты",
        "value": suggest,
        "display": f"{suggest:.0f}% (сейчас {current:.0f}%)",
        "reason": reason,
        "confidence": 70,
        "sample": 0,
        "source": "правило из встречи с экономистом 10.08.2026",
    }


# Тип покупки позиции БП → группы аналитического учёта 1С в stat-таблицах.
_STAT_GROUPS = {
    "лом": ["Лом черных металлов", "Лом легированной стали"],
    "труба": ["Труба НКТ", "Труба малых диаметров (до 159 мм)",
              "Труба больших диаметров (более 159 мм)"],
    "цветмет": ["Лом цветных металлов"],
    "кабель": ["Кабель"],
    "ДХНО": ["ДХНО"],
}


def market_stats_1c(conn, bp, items: list) -> list[dict]:
    """Ориентиры цены из фактических данных 1С.

    Цена берётся по КОНКРЕТНОЙ группе аналитического учёта, а не средняя по
    типу груза: в выгрузке труба НКТ уходит по 19 218, а трубы больших
    диаметров — по 30 338 руб/тн, и общее среднее (22 844) не описывает ни
    одну сделку. Разрез сужается до дивизиона и покупателя, свежие месяцы
    весят больше, прогноз учитывает тренд (см. app/pricing.py).

    Группа определяется по ПЛАНУ ПРОДАЖИ: труба, продаваемая ломом, стоит
    как лом. Если позиция сопоставлена с номенклатурой 1С — берём её группу
    из справочника, иначе по типу продажи.
    """
    out: list[dict] = []
    try:
        division = (bp["division"] or "").strip() or None
        # Группы аналитического учёта позиций лота и объём по каждой.
        by_item = analytic_groups_for(conn, items)
        groups: dict[str, float] = {}
        item_groups: dict[int, str] = {}
        for it in items:
            vol = float(it["volume_t"] or 0)
            # Ориентир берётся по группе, в которой позиция ПРОДАЁТСЯ:
            # труба, уходящая ломом, стоит как лом, а не как труба.
            grp = (it["sale_group"] or "").strip() or by_item.get(it["id"])
            if not grp:
                # Не сопоставлено с 1С — группы по ПЛАНУ ПРОДАЖИ: труба,
                # продаваемая ломом, стоит как лом, а не как труба.
                stype = ((it["sale_type"] or it["purchase_type"]
                          or "лом")).strip()
                for g in _STAT_GROUPS.get(stype, []):
                    groups[g] = groups.get(g, 0.0) + vol
                continue
            item_groups[it["id"]] = grp
            groups[grp] = groups.get(grp, 0.0) + vol
        for grp in sorted(groups, key=lambda g: -groups[g]):
            # Единственный покупатель группы сужает разрез до канала сбыта.
            buyers = {(it["buyer"] or "").strip() for it in items
                      if item_groups.get(it["id"]) == grp and it["buyer"]}
            buyer = next(iter(buyers)) if len(buyers) == 1 else None
            h = pricing.sale_price_hints(conn, grp, division, buyer or None)
            if not h:
                continue
            best, tr = h["best"], h.get("trend")
            out.append({
                "kind": "sale", "label": f"Реализация «{grp}» (факт 1С)",
                "price": best["price"], "sample": best["samples"],
                "scope": best["scope"], "qty": best["qty"],
                "period": f"{best['period_from']}…{best['period_to']}",
                "forecast": h.get("forecast"), "anchor": h.get("anchor"),
                "trend_pct": tr["pct"] if tr else None,
                "trend_delta": tr["delta"] if tr else None,
                "buyers": h.get("buyers") or [],
                "history": h.get("history") or [],
                "hint": ("Средневзвешенная цена фактических продаж по разрезу "
                         f"«{best['scope']}»: свежие месяцы весят больше, "
                         "сделки старше 24 месяцев не учитываются. "
                         "Источник: выгрузка «Выручка на загрузку»."),
            })
        # Закупка у продавца сделки — прежней логикой по типам груза.
        ptypes = {(it["purchase_type"] or "лом").strip() for it in items}
        for ptype in sorted(ptypes):
            groups_p = _STAT_GROUPS.get(ptype)
            if not groups_p:
                continue
            marks = ",".join("?" for _ in groups_p)
            seller = (bp["seller_name"] or "").strip()
            if seller:
                toks = [t for t in re.sub(r'[^а-яёa-z0-9 ]', ' ',
                                          seller.lower()).split()
                        if len(t) > 2][:2]
                if toks:
                    where = " AND ".join("lower_ru(counterparty) LIKE ?"
                                         for _ in toks)
                    prow = conn.execute(
                        f"SELECT SUM(total_sum) AS s, SUM(total_qty_t) AS q, "
                        f"SUM(samples) AS n FROM stat_purchase_price "
                        f"WHERE nomen_group IN ({marks}) AND {where}",
                        (*groups_p, *[f"%{t}%" for t in toks])).fetchone()
                    if prow and prow["q"] and prow["s"]:
                        out.append({
                            "kind": "purchase",
                            "label": f"Закупка «{ptype}» у этого продавца (факт 1С)",
                            "price": prow["s"] / prow["q"],
                            "sample": prow["n"] or 0,
                            "hint": ("Средняя фактическая цена закупки без НДС "
                                     "по документам 1С у контрагента "
                                     f"«{seller}». Источник: выгрузка «Закупки»."),
                        })
    except sqlite3.Error:
        return []
    return out


# Признаки цветного лома в наименовании (переклассификация «лом» → «цветмет»).
_NONFERROUS_WORDS = ("медь", "меди", "медн", "латун", "бронз", "нержав",
                     "алюмин", "цинк", "свинец", "свинц", "титан", "никел")


def _is_nonferrous(name: str) -> bool:
    low = (name or "").lower().replace("ё", "е")
    return any(w in low for w in _NONFERROUS_WORDS)


# ─────────────────────────────────────── Позиции лота ───────────────
def apply_sale_instructions(conn, bp_id: int, instructions: list) -> int:
    """Указания перечня («БП ДСП»/«БП Лукойл»/«ШУМЕЙКО») — в позиции БП.

    Привязка по нормализованному имени номенклатуры: позиции создаются из
    расчётного листа, а указания лежат в перечне, общего ключа между ними
    нет. Несколько разных указаний одного имени склеиваются через «; » —
    лучше показать оба, чем молча выбрать одно. Уже заполненное указание
    не перетирается (его мог уточнить человек)."""
    if not instructions:
        return 0
    by_name: dict[str, dict[str, list[str]]] = {}
    for ins in instructions:
        key = logistics.norm_name(ins["name"])
        if not key:
            continue
        slot = by_name.setdefault(key, {"bsp": [], "luk": []})
        for v in ("bsp", "luk"):
            t = (ins.get(v) or "").strip()
            if t and t not in slot[v]:
                slot[v].append(t)
    updated = 0
    for it in conn.execute("SELECT id, nomenclature FROM bp_items "
                           "WHERE bp_id = ?", (bp_id,)).fetchall():
        hit = by_name.get(logistics.norm_name(it["nomenclature"] or ""))
        if not hit:
            continue
        bsp = "; ".join(hit["bsp"]) or None
        luk = "; ".join(hit["luk"]) or None
        if bsp or luk:
            conn.execute(
                "UPDATE bp_items SET "
                "sale_instruction = COALESCE(sale_instruction, ?), "
                "sale_instruction_luk = COALESCE(sale_instruction_luk, ?) "
                "WHERE id = ?", (bsp, luk, it["id"]))
            updated += 1
    return updated


def insert_parsed_items(conn, bp_id: int, parsed: list, buyer_name,
                        supplier_default, seller_name: str | None = None) -> tuple[int, int]:
    """Вставка распознанных позиций в bp_items + автосопоставление с 1С.
    Возвращает (сопоставлено с 1С, у скольких проставлена цена реализации).
    Общая для загрузки КП/спецификации и импорта готового БП."""
    def norm(s):
        return " ".join((s or "").lower().replace("«", '"').replace("»", '"').split())

    bp_row = conn.execute("SELECT contamination_pct FROM business_plans WHERE id = ?",
                          (bp_id,)).fetchone()
    bp_cont = float(bp_row["contamination_pct"] or 0) if bp_row else 0.0

    matched = priced = 0
    for p in parsed:
        is_spec = (p.get("category") or p.get("sale_price") is not None
                   or p.get("purchase_type"))
        if is_spec:                       # спецификация/БП: явные поля из файла
            ptype = p.get("purchase_type") or "лом"
            category = p.get("category")
            sale_type = p.get("sale_type") or ptype
            # Цветной лом, записанный экономистом с типом «лом» (БП 1785:
            # латунь/медь/нержавейка — тип «лом», чтобы применялся засор):
            # относим к «цветмет», иначе выручка ляжет в строку чёрного лома.
            if sale_type == "лом" and _is_nonferrous(p["name"]):
                sale_type = "цветмет"
                category = category or list_import.category_from_text(p["name"]) \
                    or "цветмет"
            sale_price = p.get("sale_price")
            row_supplier = p.get("supplier") or supplier_default
            # «Поставщик» = сам продавец сделки (объединённая ячейка в
            # спецификациях Лукойла) — не база позиции: базой станет
            # подразделение, как у остальных строк.
            if seller_name and row_supplier and norm(row_supplier) == norm(seller_name):
                row_supplier = None
            row_division = p.get("division") or p["place"]
        else:                             # обычный перечень: атрибуты по имени
            attrs = infer_item_attrs(conn, p["name"])
            ptype = attrs["purchase_type"] or "лом"
            category = attrs["category"]
            sale_type = ptype
            sale_price = None
            row_supplier = supplier_default
            row_division = p["place"]
        if sale_price is not None:
            priced += 1
        # Засор позиции из файла: J/G («объём продажи»/«объём закупки»).
        # Записываем, только если отличается от того, что применил бы расчёт:
        # для чёрного лома — от общего засора БП (БП 1738: 5,8–5,9% по
        # строкам при 5% в шапке), для остальных типов — от нуля (БП 1785:
        # цветной лом теряет 5%).
        item_cont = None
        sale_vol = p.get("sale_volume")
        if (sale_vol is not None and p["qty"] and 0 < sale_vol <= p["qty"]):
            cont = round((1 - sale_vol / p["qty"]) * 100, 4)
            default_cont = bp_cont if sale_type == "лом" else 0.0
            if abs(cont - default_cont) > 0.005:
                item_cont = cont
        cur = conn.execute(
            "INSERT INTO bp_items (bp_id, supplier, division, warehouse, nomenclature, "
            "seller_code, category, purchase_type, unit, volume_t, sale_type, "
            "sale_price, buyer, shipment, price_owner, distance_km, contamination_pct, "
            "balance_price, balance_cost, tech_doc, storage_conditions, "
            "condition_note, extra_works, sale_period, origin_reason, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bp_id, row_supplier or None, row_division, p["place"],
             p["name"], p.get("code"), category, ptype, p["unit"], p["qty"],
             sale_type, sale_price,
             p.get("pos_buyer") or p["dest"] or buyer_name,
             p.get("shipment"), p.get("price_owner"),
             p.get("distance_km"), item_cont,
             p.get("balance_price"), p.get("balance_cost"), p.get("tech_doc"),
             p.get("storage_conditions"), p.get("condition_note"),
             p.get("extra_works"), p.get("sale_period"), p.get("origin_reason"),
             " / ".join(filter(None, [p.get("base_point") and
                                      f"базовый логистический пункт {p['base_point']}",
                                      p.get("distance_km") is not None and
                                      f"расстояние {p['distance_km']:g} км",
                                      p["note"]])) or None))
        sugs = matcher.suggest(conn, p["name"], limit=1,
                               seller_code=p.get("code"))
        if sugs:
            # Группа аналитического учёта сопоставленной номенклатуры 1С —
            # единая классификация позиции (по ней цена, план продажи, НДС).
            grp = conn.execute(
                "SELECT cargo_group FROM ref_nomenclature_1c WHERE guid = ?",
                (sugs[0]["guid"],)).fetchone() if sugs[0]["guid"] else None
            conn.execute(
                "UPDATE bp_items SET nomen_1c = ?, nomen_1c_guid = ?, "
                "match_source = ?, cargo_group = COALESCE(cargo_group, ?) "
                "WHERE id = ?", (sugs[0]["name"], sugs[0]["guid"], sugs[0]["source"],
                                 grp["cargo_group"] if grp else None,
                                 cur.lastrowid))
            matched += 1
    return matched, priced


# ── Помощник ИИ (app/assist.py): разбор КП, спорные позиции, объяснение ─────

def _read_assist(bp, name: str) -> str | None:
    p = ATTACH_DIR / bp["bp_number"] / "_assist" / name
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None


def _read_assist_json(bp, name: str):
    t = _read_assist(bp, name)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


def _assist_dir(bp) -> Path:
    d = ATTACH_DIR / bp["bp_number"] / "_assist"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.post("/bp/{bp_id}/assist/parse")
async def assist_parse(request: Request, bp_id: int, file: UploadFile):
    """Разбор КП любого формата через ИИ → предпросмотр позиций (ничего не
    записывается, пока человек не подтвердит)."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Нет прав на загрузку позиций.")
    fname = Path(file.filename or "kp").name
    d = _assist_dir(bp)
    path = d / fname
    path.write_bytes(await file.read())
    res = assist.parse_kp(conn, path, actor(request), bp_id)
    conn.commit()
    conn.close()
    if not res.get("ok"):
        return redirect(f"/bp/{bp_id}/lot", f"Разбор не удался: {res.get('reason')}")
    (d / "parsed.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return redirect(f"/bp/{bp_id}/lot#assist-preview",
                    f"ИИ разобрал «{fname}»: {len(res['items'])} позиций. Проверьте и подтвердите загрузку.")


@app.post("/bp/{bp_id}/assist/apply")
def assist_apply(request: Request, bp_id: int, supplier_default: str = Form(""),
                 replace: str = Form("")):
    """Подтверждение: позиции из предпросмотра → штатная вставка (та же, что
    у загрузки xlsx: сопоставление с 1С, тип сделки, точки, доля)."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Нет прав на загрузку позиций.")
    pj = _assist_dir(bp) / "parsed.json"
    if not pj.is_file():
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Нет разобранного перечня — сначала загрузите файл для разбора.")
    res = json.loads(pj.read_text(encoding="utf-8"))
    parsed = []
    for it in res["items"]:
        unit = (it.get("unit") or "тн").strip().lower()
        qty = float(it.get("qty") or 0)
        if unit in ("кг", "kg"):
            unit, qty = "тн", qty / 1000.0
        parsed.append({"name": it.get("name") or "", "code": it.get("code") or None,
                       "unit": unit, "qty": qty, "place": it.get("place") or "",
                       "note": (it.get("condition") or "") or None,
                       "purchase_price": it.get("price")})
    if replace == "1":
        conn.execute("DELETE FROM bp_items WHERE bp_id = ?", (bp_id,))
    seller = (res.get("seller") or "").strip() or None
    matched, priced = insert_parsed_items(conn, bp_id, parsed, None,
                                          supplier_default.strip() or seller or "", seller)
    if seller and not (bp["seller_name"] or "").strip():
        conn.execute("UPDATE business_plans SET seller_name = ? WHERE id = ?", (seller, bp_id))
    if res.get("request_no") and not (bp["tender_ref"] or "").strip():
        conn.execute("UPDATE business_plans SET tender_ref = ? WHERE id = ?", (res["request_no"], bp_id))
    if not (bp["source_name"] or "").strip():
        conn.execute("UPDATE business_plans SET source_name = ?, source_type = ? WHERE id = ?",
                     (f"{res.get('request_no') or ''} {res.get('file') or ''} (разбор ИИ)".strip(), "Перечень продавца", bp_id))
    loaded = conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id", (bp_id,)).fetchall()
    for it in loaded:
        try:
            logistics.register_point(conn, it["division"] or it["warehouse"] or "")
        except Exception:
            pass
    detected = bp_types.apply_auto(conn, bp_id, loaded)
    deals.apply_auto(conn, bp_id)
    log(conn, actor(request), bp_id, "assist_apply", f"{len(parsed)} позиций из «{res.get('file')}»")
    conn.commit()
    conn.close()
    pj.unlink(missing_ok=True)
    msg = f"Загружено позиций: {len(parsed)}, сопоставлено с 1С: {matched}."
    if detected.get("code"):
        msg += f" Тип сделки: {detected['code']}."
    if res.get("warnings"):
        msg += " ИИ отметил: " + "; ".join(res["warnings"][:3])
    return redirect(f"/bp/{bp_id}/lot", msg)


@app.post("/bp/{bp_id}/assist/discard")
def assist_discard(request: Request, bp_id: int):
    conn = connect()
    bp = conn.execute("SELECT * FROM business_plans WHERE id = ?", (bp_id,)).fetchone()
    conn.close()
    if bp is not None:
        (_assist_dir(bp) / "parsed.json").unlink(missing_ok=True)
    return redirect(f"/bp/{bp_id}/lot", "Разобранный перечень отброшен.")


@app.post("/bp/{bp_id}/assist/match")
def assist_match(request: Request, bp_id: int):
    """Спорные позиции (не сопоставлены или сопоставлены нечётко) → предложения
    ИИ строго из кандидатов матчера; применяются отдельным подтверждением."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Нет прав на изменение позиций.")
    doubtful = [it for it in items if not it["match_confirmed"] and
                (not it["nomen_1c"] or (it["match_source"] or "").startswith("авто (похожее"))]
    res = assist.suggest_matches(conn, doubtful, actor(request), bp_id)
    conn.commit()
    if not res.get("ok"):
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", f"Подсказки не получены: {res.get('reason')}")
    (_assist_dir(bp) / "matches.json").write_text(json.dumps(res["matches"], ensure_ascii=False), encoding="utf-8")
    conn.close()
    return redirect(f"/bp/{bp_id}/lot#assist-matches",
                    f"ИИ предложил сопоставление для {len(res['matches'])} позиций — проверьте и примените.")


@app.post("/bp/{bp_id}/assist/match/apply")
def assist_match_apply(request: Request, bp_id: int):
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Нет прав на изменение позиций.")
    mj = _assist_dir(bp) / "matches.json"
    if not mj.is_file():
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Нет предложений ИИ.")
    matches = json.loads(mj.read_text(encoding="utf-8"))
    by_id = {it["id"]: it for it in items}
    applied = 0
    for m in matches:
        it = by_id.get(int(m["item_id"]))
        if not it or not m.get("nomen_1c") or m.get("confidence") == "низкая":
            continue
        row = conn.execute("SELECT name, guid FROM ref_nomenclature_1c WHERE name = ? LIMIT 1",
                           (m["nomen_1c"],)).fetchone()
        if not row:
            continue
        conn.execute("UPDATE bp_items SET nomen_1c = ?, nomen_1c_guid = ?, match_source = ? WHERE id = ?",
                     (row["name"], row["guid"], f"ИИ ({m['confidence']}): {m['why']}"[:120], it["id"]))
        matcher.confirm(conn, it["nomenclature"], row["name"], row["guid"], it["seller_code"])
        applied += 1
    log(conn, actor(request), bp_id, "assist_match_apply", f"{applied} из {len(matches)}")
    conn.commit()
    conn.close()
    mj.unlink(missing_ok=True)
    return redirect(f"/bp/{bp_id}/lot", f"Применено сопоставлений: {applied} (низкая уверенность пропущена).")


@app.post("/bp/{bp_id}/assist/explain")
def assist_explain(request: Request, bp_id: int):
    """Объяснение расхождений четырёх слоёв — из уже посчитанного."""
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    variant = resolve_variant(request, bp)
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)
    pnl = calc.pnl(bp_v, items_v, costs_v, conn)
    fl = fact_model.evaluate(bp_v, items_v, conn)
    site = fact_model.resolve_site(conn, bp_v, items_v)[0]
    oc = outcome.expect(conn, bp_v, items_v, site)
    tp = book_archive.for_type(conn, bp["bp_type"])
    ctx = {
        "сделка": {"номер": bp["bp_number"], "источник": bp["source_name"], "тип": bp["bp_type"],
                   "доля лота %": pnl["lot_share_pct"], "площадка": site, "вариант": calc.VARIANTS.get(variant),
                   "тоннаж на долю": pnl["purchase_volume"]},
        "расчёт (текущие суммы в карточке)": {
            "выручка": pnl["revenue"], "стоимость лота": pnl["lot_cost"], "прямые затраты": pnl["direct_costs"],
            "операционная прибыль": pnl["operating_profit"], "чистая прибыль": pnl["net_profit"],
            "ROS %": pnl["ros_pct"], "валовая маржа %": pnl["gross_margin_pct"],
            "затраты по статьям": {f"{c['section']} / {c['item']}": calc._f(c["amount"]) for c in costs_v if calc._f(c["amount"])},
            "цены реализации руб/т": {it["nomenclature"]: it["sale_price"] for it in items_v if it["sale_price"]},
        },
        "факт 1С (ставки × тоннаж)": {"итого": fl.get("total"), "площадка": fl.get("site"),
                                      "статьи": {f"{l['section']} / {l['item']}": l["amount"] for l in fl.get("lines", [])},
                                      "распределяемые руб/т": fl.get("overhead", {}).get("rate_per_t") if fl.get("overhead") else None},
        "независимая оценка по похожим закрытым сделкам": (
            {"похожих": oc["n"], "уровень": oc["level"], "цена руб/т медиана": oc["price"]["median"],
             "валовая % медиана": oc["gross"]["median"], "ожидаемый результат": oc["profit"],
             "диапазон": [oc["profit_lo"], oc["profit_hi"]]} if oc.get("ok") else None),
        "в книгах экономистов по типу": ({"сделок": tp["n"], "затраты % выручки": tp["costs_pct"],
                                          "валовая %": tp["gross_pct"], "прибыль %": tp["profit_pct"]} if tp else None),
        "факт по типу (медиана валовой)": pnl.get("type_fact"),
    }
    res = assist.explain(conn, ctx, actor(request), bp_id)
    conn.commit()
    conn.close()
    if not res.get("ok"):
        return redirect(f"/bp/{bp_id}/economics", f"Объяснение не получено: {res.get('reason')}")
    (_assist_dir(bp) / "explain.txt").write_text(res["text"], encoding="utf-8")
    return redirect(f"/bp/{bp_id}/economics#assist-explain", "ИИ объяснил расхождения — ниже, под P&L.")


@app.post("/bp/{bp_id}/items/upload")
async def upload_list(request: Request, bp_id: int, file: UploadFile,
                      supplier_default: str = Form(""), replace: str = Form("")):
    """Загрузка КП/спецификации продавца: файл распознаётся, а его данные
    (позиции, цены реализации, стороны сделки, стоимость лота) переносятся в
    карточку. Файл не сохраняется как отдельный документ-основание."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    fname = upload_name(file.filename, "спецификация.xlsx")
    if not fname.lower().endswith((".xlsx", ".xlsm", ".xls")):
        conn.close()
        return back(request, bp_id, "Поддерживается только xlsx/xlsm/xls.")

    # Разбираем во временном файле — на диск как основание не сохраняем.
    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=Path(fname).suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        # Перечни Лукойла приходят и в старом .xls — переводим в xlsx на лету.
        if tmp_path.suffix.lower() == ".xls":
            try:
                tmp_path = _xls_to_xlsx_tmp(tmp_path)
            except Exception as e:
                conn.close()
                return back(request, bp_id, f"Файл .xls не прочитан: {type(e).__name__}: {e}. "
                                            "Сохраните его в Excel как xlsx и загрузите снова.")
        metadata = list_import.detect_metadata(tmp_path)
        parsed, warnings = list_import.parse_list(tmp_path)
        try:
            instructions = list_import.collect_instructions(tmp_path)
        except Exception:
            instructions = []
    finally:
        tmp_path.unlink(missing_ok=True)

    if not parsed:
        conn.close()
        return back(request, bp_id,
                        "Позиции не распознаны: " + (" ".join(warnings) or
                        "не найдена таблица позиций."))

    if replace == "1":
        conn.execute("DELETE FROM bp_items WHERE bp_id = ?", (bp_id,))

    # Продавец: форма → колонка «Поставщик» спецификации → реквизиты из шапки.
    spec_suppliers = [p["supplier"] for p in parsed if p.get("supplier")]
    spec_divisions = [p["division"] for p in parsed if p.get("division")]
    lot_cost_sum = sum(p["purchase_cost"] for p in parsed
                       if p.get("purchase_cost")) or None
    detected_seller = metadata.get("seller_name") or (
        spec_suppliers[0] if spec_suppliers else None)
    # Продавца НЕ пишем поставщиком в каждую позицию: «Поставщик» — первый
    # приоритет группировки по базам (calc.base_of), и продавец в этом поле
    # склеивал все места перечня в одну «базу» (КП 1578: 1349 позиций одной
    # группой). Продавец живёт в шапке БП; в позицию поставщик попадает
    # только из колонки файла или из поля формы, заполненного человеком.
    supplier_for_items = supplier_default.strip() or None
    seller_updated = False
    tender_updated = False
    division_updated = False
    lot_updated = False
    seller_id = None
    if detected_seller and not (bp["seller_name"] or "").strip():
        ref = conn.execute(
            "SELECT id, name FROM ref_counterparties "
            "WHERE lower_ru(name) = lower_ru(?) LIMIT 1",
            (detected_seller,),
        ).fetchone()
        seller_id = ref["id"] if ref else None
        seller_name = ref["name"] if ref else detected_seller
        conn.execute("UPDATE business_plans SET seller_id = ?, seller_name = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (seller_id, seller_name, bp_id))
        seller_updated = True
    if metadata.get("tender_ref") and not (bp["tender_ref"] or "").strip():
        conn.execute("UPDATE business_plans SET tender_ref = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (metadata["tender_ref"], bp_id))
        tender_updated = True
    if spec_divisions and not (bp["division"] or "").strip():
        conn.execute("UPDATE business_plans SET division = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (spec_divisions[0], bp_id))
        division_updated = True
    if lot_cost_sum and not bp["lot_cost"]:
        conn.execute("UPDATE business_plans SET lot_cost = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (round(lot_cost_sum, 2), bp_id))
        lot_updated = True

    matched, priced = insert_parsed_items(conn, bp_id, parsed, bp["buyer_name"],
                                          supplier_for_items,
                                          seller_name=detected_seller or bp["seller_name"])
    instructed = apply_sale_instructions(conn, bp_id, instructions)

    # Места отгрузки — в реестр пунктов, как при импорте книги: без этого
    # у БП, заведённого от КП (главный путь «с нуля»), не работали роли
    # цех/база, автограф маршрута и подсказки ставок (найдено на КП 1578).
    points_n = 0
    for p in parsed:
        place = p.get("division") or p.get("place")
        if not place:
            continue
        if logistics.register_point(
                conn, place, base_point=p.get("base_point"),
                distance_km=p.get("distance_km"),
                seller_name=detected_seller or bp["seller_name"]):
            points_n += 1

    log(conn, actor(request), bp_id, "upload_list", f"{fname}: {len(parsed)} позиций")
    loaded_items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id",
                                (bp_id,)).fetchall()
    # Тип сделки по составу лота: от него зависят статьи затрат, процессы
    # переработки и то, чьё участие обязательно. Ручной выбор не перетирается.
    detected = bp_types.apply_auto(conn, bp_id, loaded_items)
    if detected.get("written") and detected.get("code"):
        log(conn, actor(request), bp_id, "bp_type_auto",
            f"{detected['code']}: {detected['reason']}")
    # Доля лота из реестра сделок Битрикса — по номеру запроса в названии
    # файла или перечня; ручная доля не перетирается.
    share_res = deals.apply_auto(conn, bp_id)
    if share_res.get("written"):
        log(conn, actor(request), bp_id, "lot_share_auto",
            f"{share_res.get('share_pct')} (сделка {share_res.get('deal_no')})")
    # Подписи берём до закрытия соединения: сообщение собирается ниже.
    type_label = bp_types.label(conn, detected.get("code"))
    kept_label = bp_types.label(conn, detected.get("kept"))
    hints_total = cost_hints_for_items(loaded_items)["total"]
    conn.commit()
    conn.close()
    msg = f"Загружено позиций: {len(parsed)}, сопоставлено с 1С: {matched}."
    if share_res.get("found") and share_res.get("share_pct") is not None:
        msg += (f" Доля лота по реестру сделок: {share_res['share_pct']:g} %"
                f"{' (в шапке задана вручную, оставлена)' if share_res.get('kept') else ''}.")
    if detected.get("code"):
        if detected.get("written"):
            msg += f" Тип сделки определён: {type_label} — {detected['reason']}"
        else:
            msg += f" Тип сделки оставлен как задан вручную: {kept_label}."
    if priced:
        msg += f" Цены реализации подставлены: {priced}."
    if seller_updated:
        msg += f" Продавец добавлен в «Стороны сделки»: {detected_seller}."
        if seller_id is None:
            msg += " Контрагент 1С не найден, поле можно уточнить вручную."
    if division_updated:
        msg += " Подразделение заполнено из файла."
    if lot_updated:
        msg += (" Стоимость лота проставлена из файла: "
                f"{round(lot_cost_sum):,}".replace(",", " ") + " руб.")
    if tender_updated:
        msg += f" Номер перечня добавлен в шапку: {metadata['tender_ref']}."
    if hints_total:
        msg += f" Найдены подсказки по затратам: {hints_total}, см. блок «Затраты»."
    if instructed:
        msg += f" Указания перечня по реализации у {instructed} позиций."
    if points_n:
        msg += (f" Мест отгрузки в реестре: {points_n} — задайте им роли "
                "цех/база на вкладке «Логистика».")
    if warnings:
        msg += " Внимание: " + " ".join(warnings[:3])
    return redirect(f"/bp/{bp_id}/economics" if hints_total else f"/bp/{bp_id}", msg)


# ─────────────────────────── Фотографии лота ────────────────────────
# Фото с места (состояние лома, засор, подъездные пути) — обоснование
# оценки: по БП 1785/1885 реальный засор был виден только на фотографиях.
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
THUMB_MAX = 480              # длинная сторона миниатюры, px


def photo_dir(bp_number: str) -> Path:
    return ATTACH_DIR / bp_number / "фото"


def exif_taken_at(path: Path) -> str | None:
    """Дата съёмки из EXIF (DateTimeOriginal), иначе None."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get(36867) or exif.get(306)     # DateTimeOriginal / DateTime
            if not raw:
                ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                raw = ifd.get(36867) if ifd else None
        if not raw:
            return None
        text = str(raw).strip().replace(":", "-", 2)   # 2026:08:10 → 2026-08-10
        datetime.fromisoformat(text.replace(" ", "T"))  # проверка формата
        return text
    except Exception:
        return None


def make_thumb(src: Path, dst: Path) -> bool:
    """Миниатюра для галереи (полное фото открывается по клику)."""
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)          # учесть поворот камеры
            img.thumbnail((THUMB_MAX, THUMB_MAX))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(dst, "JPEG", quality=82)
        return True
    except Exception:
        return False


@app.post("/bp/{bp_id}/photos")
async def upload_photos(request: Request, bp_id: int):
    """Загрузка фотографий лота (несколько файлов за раз)."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на загрузку фотографий.")
    form = await request.form()
    files = [f for f in form.getlist("photos") if getattr(f, "filename", "")]
    note = (form.get("note") or "").strip() or None
    manual_date = (form.get("taken_at") or "").strip() or None
    if not files:
        conn.close()
        return redirect(f"/bp/{bp_id}/lot", "Выберите файлы фотографий.")

    target = photo_dir(bp["bp_number"])
    target.mkdir(parents=True, exist_ok=True)
    saved, skipped = 0, 0
    for f in files:
        name = upload_name(f.filename, "фото.jpg")
        if Path(name).suffix.lower() not in PHOTO_EXT:
            skipped += 1
            continue
        path = target / name
        stem, suffix, n = path.stem, path.suffix, 1
        while path.exists():                     # не затираем одноимённые
            path = target / f"{stem}_{n}{suffix}"
            n += 1
        path.write_bytes(await f.read())
        taken = manual_date or exif_taken_at(path)
        make_thumb(path, target / (path.stem + ".thumb.jpg"))
        conn.execute(
            "INSERT INTO bp_attachments (bp_id, filename, kind, taken_at, note, "
            "uploaded_by) VALUES (?, ?, 'фото', ?, ?, ?)",
            (bp_id, path.name, taken, note, role))
        saved += 1
    log(conn, actor(request), bp_id, "upload_photos", f"{saved} фото")
    conn.commit()
    conn.close()
    msg = f"Загружено фотографий: {saved}."
    if skipped:
        msg += f" Пропущено файлов неподдерживаемого формата: {skipped}."
    return redirect(f"/bp/{bp_id}/lot", msg)


@app.get("/bp/{bp_id}/photo/{att_id}")
def show_photo(bp_id: int, att_id: int, thumb: str = ""):
    """Просмотр фотографии в браузере (миниатюра или оригинал)."""
    conn = connect()
    bp = conn.execute("SELECT bp_number FROM business_plans WHERE id = ?",
                      (bp_id,)).fetchone()
    row = conn.execute(
        "SELECT filename FROM bp_attachments WHERE id = ? AND bp_id = ? "
        "AND kind = 'фото'", (att_id, bp_id)).fetchone()
    conn.close()
    if bp is None or row is None:
        return back(request, bp_id, "Фотография не найдена.")
    base = photo_dir(bp["bp_number"])
    path = base / row["filename"]
    if thumb:
        preview = base / (Path(row["filename"]).stem + ".thumb.jpg")
        if preview.is_file():
            path = preview
    path = path.resolve()
    if not path.is_file() or ATTACH_DIR.resolve() not in path.parents:
        return back(request, bp_id, "Файл фотографии не найден.")
    return FileResponse(path)


@app.post("/bp/{bp_id}/photos/{att_id}/delete")
def delete_photo(request: Request, bp_id: int, att_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на удаление фотографий.")
    row = conn.execute(
        "SELECT filename FROM bp_attachments WHERE id = ? AND bp_id = ? "
        "AND kind = 'фото'", (att_id, bp_id)).fetchone()
    if row:
        base = photo_dir(bp["bp_number"])
        for p in (base / row["filename"],
                  base / (Path(row["filename"]).stem + ".thumb.jpg")):
            path = p.resolve()
            if path.is_file() and ATTACH_DIR.resolve() in path.parents:
                path.unlink(missing_ok=True)
        conn.execute("DELETE FROM bp_attachments WHERE id = ?", (att_id,))
        log(conn, actor(request), bp_id, "delete_photo", row["filename"])
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/lot", "Фотография удалена.")


@app.get("/bp/{bp_id}/attachments/{filename}")
def download_attachment(bp_id: int, filename: str):
    conn = connect()
    bp = conn.execute("SELECT bp_number FROM business_plans WHERE id = ?",
                      (bp_id,)).fetchone()
    conn.close()
    if bp is None:
        return redirect("/", "БП не найден.")
    path = (ATTACH_DIR / bp["bp_number"] / filename).resolve()
    if not path.is_file() or ATTACH_DIR.resolve() not in path.parents:
        return back(request, bp_id, "Файл не найден.")
    return FileResponse(path, filename=path.name)
@app.post("/bp/{bp_id}/items")
def add_item(request: Request, bp_id: int, supplier: str = Form(""),
             division: str = Form(""), warehouse: str = Form(""),
             nomenclature: str = Form(...), volume_t: str = Form("0"),
             purchase_type: str = Form(""), sale_type: str = Form(""),
             sale_price: str = Form(""), own_transport_pct: str = Form(""),
             workshop_cut_pct: str = Form(""), buyer: str = Form(""),
             shipment: str = Form(""), price_owner: str = Form(""),
             distance_km: str = Form(""), note: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    attrs = infer_item_attrs(conn, nomenclature)
    category = attrs["category"]
    ptype = purchase_type.strip() or attrs["purchase_type"] or "лом"

    # Числа новой позиции проверяются до вставки: отрицательный объём или
    # цена «abc» раньше уходили в базу как 0 и None и портили расчёт лота.
    check = forms.Form({"volume_t": volume_t, "sale_price": sale_price,
                        "own_transport_pct": own_transport_pct,
                        "workshop_cut_pct": workshop_cut_pct,
                        "distance_km": distance_km})
    num = {f: check.num(f) for f in
           ("volume_t", "sale_price", "own_transport_pct",
            "workshop_cut_pct", "distance_km")}
    if not check.ok:
        conn.close()
        return back(request, bp_id, check.message())

    conn.execute(
        "INSERT INTO bp_items (bp_id, supplier, division, warehouse, nomenclature, "
        "category, purchase_type, volume_t, sale_type, sale_price, "
        "own_transport_pct, workshop_cut_pct, buyer, shipment, price_owner, "
        "distance_km, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (bp_id, supplier.strip() or None, division.strip() or None,
         warehouse.strip() or None, nomenclature.strip(), category, ptype,
         num["volume_t"] or 0, sale_type.strip() or ptype, num["sale_price"],
         num["own_transport_pct"], num["workshop_cut_pct"], buyer.strip() or None,
         shipment.strip() or None, price_owner.strip() or None,
         num["distance_km"], note.strip() or None))
    log(conn, actor(request), bp_id, "add_item", nomenclature)
    conn.commit()
    conn.close()
    return back(request, bp_id, "" if attrs.get("ref") else
                    "Позиция добавлена, но не найдена в справочнике номенклатуры — "
                    "проверьте категорию и тип.")


@app.post("/bp/{bp_id}/items/{item_id}/split")
async def split_item(request: Request, bp_id: int, item_id: int):
    """Разделение позиции: часть объёма уходит в новую строку (свой покупатель
    и цена). Делить можно неограниченно."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    it = conn.execute("SELECT * FROM bp_items WHERE id = ? AND bp_id = ?",
                      (item_id, bp_id)).fetchone()
    form = await read_form(request)
    vol = form.num(f"split_{item_id}", field="volume_t")
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    if it is None or vol is None or not (0 < vol < float(it["volume_t"] or 0)):
        conn.close()
        return back(request, bp_id,
                        "Для разделения укажите объём новой строки больше 0 и "
                        "меньше объёма позиции.")
    conn.execute("UPDATE bp_items SET volume_t = volume_t - ? WHERE id = ?",
                 (vol, item_id))
    cols = ["bp_id", "supplier", "division", "warehouse", "nomenclature", "category",
            "purchase_type", "unit", "sale_type", "sale_price", "own_transport_pct",
            "workshop_cut_pct", "nomen_1c", "nomen_1c_guid", "match_source",
            "match_confirmed", "buyer", "shipment", "price_owner", "distance_km",
            "note"]
    conn.execute(
        f"INSERT INTO bp_items ({', '.join(cols)}, volume_t) "
        f"VALUES ({', '.join('?' * len(cols))}, ?)",
        (*[it[c] for c in cols], vol))
    log(conn, actor(request), bp_id, "split_item", f"{it['nomenclature']}: −{vol} тн в новую строку")
    conn.commit()
    conn.close()
    return back(request, bp_id,
                    f"Позиция разделена: {vol:g} тн выделено в новую строку — "
                    "укажите ей покупателя и цену.")


@app.post("/bp/{bp_id}/items/{item_id}/delete")
def delete_item(request: Request, bp_id: int, item_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("items", role, bp["status"]):
        conn.execute("DELETE FROM bp_items WHERE id = ? AND bp_id = ?", (item_id, bp_id))
        log(conn, actor(request), bp_id, "delete_item", str(item_id))
        conn.commit()
    conn.close()
    return back(request, bp_id)


@app.post("/bp/{bp_id}/points/{point_id}/warehouse")
async def set_point_warehouse(request: Request, bp_id: int, point_id: int):
    """Подтверждение склада 1С для пункта отгрузки. Сопоставление хранится в
    реестре, а не в сделке: один куст встречается в разных лотах."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    form = await request.form()
    wh_id = (form.get("warehouse_id") or "").strip()
    wh = conn.execute("SELECT id, name FROM ref_warehouses WHERE id = ?",
                      (wh_id,)).fetchone() if wh_id.isdigit() else None
    conn.execute(
        "UPDATE shipping_points SET warehouse_id = ?, warehouse_name = ?, "
        "match_source = 'вручную', match_confirmed = 1, "
        "updated_at = datetime('now') WHERE id = ?",
        (wh["id"] if wh else None, wh["name"] if wh else None, point_id))
    log(conn, actor(request), bp_id, "point_warehouse",
        f"пункт {point_id} → склад {wh['name'] if wh else '—'}")
    conn.commit()
    conn.close()
    return back(request, bp_id, "Склад для пункта отгрузки сохранён.")


@app.post("/bp/{bp_id}/points/{point_id}/role")
async def set_point_role(request: Request, bp_id: int, point_id: int):
    """Роль места в модели затрат: база (переработка и отгрузка здесь) или
    цех (только вывоз на указанную базу). Роль живёт в общем реестре
    пунктов — задав её один раз, её получают все БП с этим местом."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    form = await request.form()
    kind = (form.get("kind") or "").strip()
    base_name = (form.get("base_name") or "").strip()
    if kind not in ("", "база", "цех"):
        conn.close()
        return back(request, bp_id, "Не сохранено: роль — «база» или «цех».")
    if kind == "цех" and not base_name:
        conn.close()
        return back(request, bp_id,
                    "Не сохранено: у цеха укажите базу, куда он свозит.")
    point = conn.execute("SELECT name FROM shipping_points WHERE id = ?",
                         (point_id,)).fetchone()
    if point is None:
        conn.close()
        return back(request, bp_id, "Пункт отгрузки не найден.")
    conn.execute(
        "UPDATE shipping_points SET kind = ?, base_name = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (kind or None,
         base_name or (point["name"] if kind == "база" else None), point_id))
    log(conn, actor(request), bp_id, "point_role",
        f"{point['name']}: {kind or 'роль снята'}"
        + (f" → {base_name}" if kind == "цех" else ""))
    conn.commit()
    conn.close()
    return back(request, bp_id, "Роль места сохранена — модель затрат "
                                "пересчитает этапы по ней.")


@app.post("/bp/{bp_id}/points/{point_id}/route")
async def set_point_route(request: Request, bp_id: int, point_id: int):
    """Плечо от пункта отгрузки до базы/покупателя. Километраж человек
    сверяет по ссылке 2ГИС (маршрут по доступным дорогам) и вносит сюда —
    сервис не выдумывает расстояния."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    form = await read_form(request)
    to_name = form.text("to_name") or ""
    distance = form.num("distance_km")
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    if not to_name or distance is None:
        conn.close()
        return back(request, bp_id, "Укажите пункт назначения и расстояние.")
    logistics.save_route(conn, point_id, to_name, distance,
                         source=(form.raw("source") or "2gis"),
                         note=form.text("note"))
    log(conn, actor(request), bp_id, "point_route", f"пункт {point_id} → {to_name}: {distance} км")
    conn.commit()
    conn.close()
    return back(request, bp_id, f"Плечо до «{to_name}» сохранено: {distance:g} км.")


@app.post("/bp/{bp_id}/points/{point_id}/route/2gis")
async def point_route_2gis(request: Request, bp_id: int, point_id: int):
    """Плечо по дорогам из 2ГИС: адреса геокодируются, расстояние берётся
    из Distance Matrix и сохраняется как обычное плечо с источником «2gis».

    Ключ хранится в окружении сервиса; без него кнопка не показывается, а
    роут отвечает понятным сообщением, а не ошибкой."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    form = await request.form()
    to_name = (form.get("to_name") or "").strip()
    point = conn.execute("SELECT * FROM shipping_points WHERE id = ?",
                         (point_id,)).fetchone()
    if point is None or not to_name:
        conn.close()
        return back(request, bp_id, "Укажите пункт назначения.")
    try:
        result = geo.distance_for_point(conn, point, to_name)
    except geo.GeoError as exc:
        conn.commit()            # координаты, если успели геокодироваться
        conn.close()
        return back(request, bp_id, str(exc))
    # Если карта нашла точку не по полному адресу, расстояние считано до
    # центра населённого пункта — пишем это в примечание и в сообщение,
    # иначе разница с перечнем («17 км вместо 11») выглядит как ошибка.
    note = (f"по дорогам, {result['duration_min']} мин в пути"
            if result["duration_min"] else "по дорогам")
    if result["rough"]:
        note += "; координаты по: " + ", ".join(result["rough"])
    logistics.save_route(conn, point_id, to_name, result["distance_km"],
                         source=result["provider"], note=note)
    if result["duration_min"] is not None:
        conn.execute("UPDATE shipping_routes SET duration_min = ? "
                     "WHERE from_point = ? AND to_name = ?",
                     (result["duration_min"], point_id, to_name))
    log(conn, actor(request), bp_id, "point_route_auto",
        f"{result['provider']}: {point['name']} → {to_name}: "
        f"{result['distance_km']} км")
    conn.commit()
    conn.close()
    message = (f"{result['provider']}: {point['name']} → {to_name} = "
               f"{result['distance_km']:g} км"
               + (f", {result['duration_min']} мин в пути"
                  if result["duration_min"] else ""))
    if result["rough"]:
        message += (". Точный адрес карта не нашла, считали до: "
                    + ", ".join(result["rough"])
                    + " — проверьте километраж.")
    return back(request, bp_id, message)


@app.post("/bp/{bp_id}/items/{item_id}/sales")
async def add_item_sale(request: Request, bp_id: int, item_id: int):
    """Покупатель в плане продажи позиции: часть объёма уходит этому
    покупателю по своей цене и отгрузке. Строки свои у каждого варианта —
    в 1935 «ДСП» везёт на Чермет-Волжский, «Лукойл» на ВТЗ."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    it = conn.execute("SELECT * FROM bp_items WHERE id = ? AND bp_id = ?",
                      (item_id, bp_id)).fetchone()
    if it is None:
        conn.close()
        return back(request, bp_id, "Позиция не найдена.")
    form = await read_form(request)
    variant = resolve_variant(request, bp)
    volume = form.num("volume_t") or 0.0
    price = form.num("sale_price")
    contamination = form.num("contamination_pct")
    if not form.ok:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}", form.message())
    if volume <= 0:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}",
                        "Укажите объём для покупателя.")
    # Больше нераспределённого остатка отдать нельзя: сумма строк не должна
    # превышать объём позиции, иначе тоннаж лота «раздуется».
    assigned = conn.execute(
        "SELECT COALESCE(SUM(volume_t), 0) AS v FROM bp_item_sales "
        "WHERE item_id = ? AND variant = ?", (item_id, variant)).fetchone()["v"]
    free = round(float(it["volume_t"] or 0) - assigned, 6)
    if volume > free + 1e-6:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}",
                        f"Свободный объём позиции {free:g} тн — больше отдать нельзя.")
    conn.execute(
        "INSERT INTO bp_item_sales (bp_id, item_id, variant, buyer, volume_t, "
        "sale_price, sale_type, shipment, contamination_pct, note, sort) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT COALESCE(MAX(sort), 0) + 1 FROM bp_item_sales "
        " WHERE item_id = ? AND variant = ?))",
        (bp_id, item_id, variant, form.text("buyer"),
         volume, price, form.text("sale_type"),
         form.text("shipment"), contamination,
         form.text("note"), item_id, variant))
    log(conn, actor(request), bp_id, "add_item_sale",
        f"позиция {item_id}, {variant}: {form.text('buyer') or '—'} {volume} тн")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant={variant}", "Покупатель добавлен в план продажи.")


@app.post("/bp/{bp_id}/sales/{sale_id}/delete")
def delete_item_sale(request: Request, bp_id: int, sale_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    variant = resolve_variant(request, bp) if bp else "bsp"
    if bp and workflow.can_edit_section("items", role, bp["status"]):
        conn.execute("DELETE FROM bp_item_sales WHERE id = ? AND bp_id = ?",
                     (sale_id, bp_id))
        log(conn, actor(request), bp_id, "delete_item_sale", str(sale_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant={variant}")


@app.post("/bp/{bp_id}/items/prices")
async def update_prices(request: Request, bp_id: int):
    """Массовое обновление цен и типов реализации (экономист)."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение цен.")
    # Правки идут в активный вариант: засор у «Лукойл» свой.
    variant = resolve_variant(request, bp)
    form = await read_form(request)
    stale = edit_conflict(conn, request, bp, form)
    if stale:
        conn.close()
        return back(request, bp_id, stale)

    # Поле, не пришедшее в форме, не трогаем (частичная отправка); пустое
    # поле — осознанная очистка значения. Числа проверяются до записи: цена
    # `abc` раньше стирала цену, `-5` принималась как отрицательная выручка.
    pending = []
    for it in items:
        i = it["id"]
        sets, values = [], []

        def put(column, value, _s=sets, _v=values):
            _s.append(f"{column} = ?")
            _v.append(value)

        if form.has(f"sale_price_{i}"):
            put("sale_price", form.num(f"sale_price_{i}", it["sale_price"]))
        # Цена варианта «Лукойл»: пустое поле = NULL (наследуется цена ДСП).
        if form.has(f"sale_price_luk_{i}"):
            put("sale_price_luk", form.num(f"sale_price_luk_{i}",
                                           it["sale_price_luk"]))
        # Кратность партии покупателя: пустое поле — партия не кратится.
        if form.has(f"batch_{i}"):
            put("batch_size_t", form.num(f"batch_{i}", it["batch_size_t"]))
        if form.has(f"own_{i}"):
            put("own_transport_pct", form.num(f"own_{i}", it["own_transport_pct"]))
        if form.has(f"cut_{i}"):
            put("workshop_cut_pct", form.num(f"cut_{i}", it["workshop_cut_pct"]))
        if form.has(f"buyer_{i}"):
            put("buyer", form.text(f"buyer_{i}"))
        # Группа аналитического учёта: единая классификация позиции.
        # Пустое поле — очистка (группа подтянется из справочника по
        # сопоставленной номенклатуре 1С).
        if form.has(f"cargo_group_{i}"):
            put("cargo_group", form.text(f"cargo_group_{i}"))
        # Единица измерения позиции: в одном лоте они разные (тонны, штуки,
        # метры) — храним как задано, пустое поле оставляет прежнюю.
        if form.has(f"unit_{i}"):
            put("unit", form.text(f"unit_{i}") or it["unit"])
        # Группа продажи (план продажи в терминах аналитического учёта):
        # из неё выводим тип, чтобы НДС и засор остались согласованными.
        stype = (form.raw(f"sale_type_{i}").strip() or it["sale_type"])
        if form.has(f"sale_group_{i}"):
            sale_group = form.text(f"sale_group_{i}")
            if sale_group:
                stype = calc.type_of_group(sale_group, stype)
            put("sale_group", sale_group)
        if form.has(f"sale_type_{i}") or form.has(f"sale_group_{i}"):
            put("sale_type", stype)
        # Засор позиции: пустое поле — берётся общий засор БП. В варианте
        # «Лукойл» правится свой засор, базовый при этом не трогаем.
        if form.has(f"contamination_{i}"):
            column = ("contamination_pct_luk" if variant == "luk"
                      else "contamination_pct")
            put(column, form.num(f"contamination_{i}", field="contamination_pct"))
        if sets:
            pending.append((f"UPDATE bp_items SET {', '.join(sets)} WHERE id = ?",
                            (*values, i)))
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    for sql, args in pending:
        conn.execute(sql, args)
    log(conn, actor(request), bp_id, "update_prices", "")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant={variant}",
                    "Цены реализации сохранены.")


# ─────────────────────────────────────── Сопоставление номенклатуры ─
def matching_rows(conn, bp_id: int, items: list) -> list[dict]:
    """Строки блока сопоставления: уникальные номенклатуры продавца в БП."""
    groups: dict[tuple, dict] = {}
    for it in items:
        # Ключ — наименование И код продавца: под одним названием у продавца
        # могут идти позиции с разными кодами (и наоборот), а сопоставлять
        # с 1С нужно каждую из них.
        code = (it["seller_code"] or "").strip() or None
        g = groups.setdefault((it["nomenclature"], code), {
            "seller_name": it["nomenclature"], "seller_code": code,
            "count": 0, "volume": 0.0,
            "nomen_1c": it["nomen_1c"], "match_source": it["match_source"],
            "confirmed": True,
        })
        g["count"] += 1
        g["volume"] += float(it["volume_t"] or 0)
        if not it["match_confirmed"]:
            g["confirmed"] = False
        if it["nomen_1c"]:                      # показываем заполненное значение
            g["nomen_1c"] = it["nomen_1c"]
            g["match_source"] = it["match_source"]
    rows = []
    for g in groups.values():
        sugs = matcher.suggest(conn, g["seller_name"], limit=1,
                               seller_code=g["seller_code"])
        top = sugs[0] if sugs else None
        # предложение показываем, если поле пусто или отличается
        g["suggestion"] = top if top and top["name"] != g["nomen_1c"] else None
        rows.append(g)
    return rows


@app.post("/bp/{bp_id}/match/auto")
def match_auto(request: Request, bp_id: int):
    """Автоподбор номенклатуры 1С для всех неподтверждённых позиций."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на сопоставление.")
    filled = missed = 0
    for it in items:
        if it["match_confirmed"]:
            continue
        sugs = matcher.suggest(conn, it["nomenclature"], limit=1,
                               seller_code=it["seller_code"])
        if sugs:
            s = sugs[0]
            conn.execute(
                "UPDATE bp_items SET nomen_1c = ?, nomen_1c_guid = ?, match_source = ? "
                "WHERE id = ?", (s["name"], s["guid"], s["source"], it["id"]))
            filled += 1
        else:
            missed += 1
    log(conn, actor(request), bp_id, "match_auto", f"filled={filled} missed={missed}")
    conn.commit()
    conn.close()
    return back(request, bp_id,
                    f"Подобрано автоматически: {filled}."
                    + (f" Без предложений: {missed} — укажите вручную." if missed else ""))


@app.post("/bp/{bp_id}/match/set")
def match_set(request: Request, bp_id: int, seller_name: str = Form(...),
              nomen_1c: str = Form(""), confirm: str = Form("")):
    """Ручная корректировка/подтверждение: применяется ко всем позициям БП
    с этой номенклатурой продавца. Подтверждение обучает историю («ИИ»)."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на сопоставление.")
    name = nomen_1c.strip()
    if not name:
        conn.close()
        return back(request, bp_id, "Укажите номенклатуру 1С.")
    ref = conn.execute("SELECT guid FROM ref_nomenclature_1c WHERE name = ?",
                       (name,)).fetchone()
    guid = ref["guid"] if ref else None
    is_confirm = confirm == "1"
    source = "вручную" if not is_confirm else "подтверждено мастером"
    code_row = conn.execute(
        "SELECT seller_code FROM bp_items WHERE bp_id = ? AND nomenclature = ? "
        "AND seller_code IS NOT NULL LIMIT 1", (bp_id, seller_name)).fetchone()
    seller_code = code_row["seller_code"] if code_row else None
    conn.execute(
        "UPDATE bp_items SET nomen_1c = ?, nomen_1c_guid = ?, match_source = ?, "
        "match_confirmed = ? WHERE bp_id = ? AND nomenclature = ?",
        (name, guid, source, 1 if is_confirm else 0, bp_id, seller_name))
    if is_confirm:
        matcher.confirm(conn, seller_name, name, guid, seller_code)
    log(conn, actor(request), bp_id, "match_set", f"{seller_name} → {name} ({source})")
    conn.commit()
    conn.close()
    return back(request, bp_id,
                    ("Сопоставление подтверждено — учтено в истории ИИ."
                     if is_confirm else "Сопоставление сохранено.")
                    + ("" if ref else " Внимание: позиция не найдена в 1С — GUID пуст."))


@app.get("/api/nomen1c")
def nomen1c_search(q: str = ""):
    """Поиск по номенклатуре 1С для автокомплита (топ-20)."""
    conn = connect()
    toks = [t for t in matcher.normalize(q).split() if t]
    rows = []
    if toks:
        where = " AND ".join("norm LIKE ?" for _ in toks)
        rows = conn.execute(
            f"SELECT name, unit, cargo_group FROM ref_nomenclature_1c WHERE {where} "
            "ORDER BY (unit = 'т') DESC, length(name) LIMIT 20",
            tuple(f"%{t}%" for t in toks)).fetchall()
    conn.close()
    return JSONResponse([{"name": r["name"], "unit": r["unit"],
                          "group": r["cargo_group"]} for r in rows])


def _tokens_where(field_expr: str, q: str) -> tuple[str, tuple]:
    """AND-поиск по токенам без учёта регистра (кириллица)."""
    toks = [t for t in q.lower().split() if t]
    where = " AND ".join(f"lower_ru({field_expr}) LIKE ?" for _ in toks) or "1=0"
    return where, tuple(f"%{t}%" for t in toks)


@app.get("/api/counterparties")
def counterparties_search(q: str = ""):
    """Поиск контрагентов 1С для автокомплита (топ-20)."""
    conn = connect()
    rows = []
    if len(q.strip()) >= 2:
        where, params = _tokens_where("name", q)
        rows = conn.execute(
            f"SELECT id, name, ctype FROM ref_counterparties WHERE {where} "
            "ORDER BY length(name) LIMIT 20", params).fetchall()
    conn.close()
    return JSONResponse([{"id": r["id"], "name": r["name"],
                          "hint": r["ctype"] if r["ctype"] not in (None, "-") else ""}
                         for r in rows])


@app.get("/api/buyers")
def buyers_search(q: str = ""):
    """Покупатели для автокомплита: сначала те, кого заполняли ранее в позициях
    БП (по частоте использования), затем совпадения из контрагентов 1С.
    Отдельный справочник покупателей не ведётся."""
    conn = connect()
    out, seen = [], set()
    if len(q.strip()) >= 2:
        where, params = _tokens_where("buyer", q)
        for r in conn.execute(
                f"SELECT buyer, COUNT(*) AS uses FROM bp_items "
                f"WHERE buyer IS NOT NULL AND ({where}) "
                "GROUP BY buyer ORDER BY uses DESC LIMIT 20", params).fetchall():
            seen.add(r["buyer"])
            out.append({"id": None, "name": r["buyer"],
                        "hint": f"использован ранее ({r['uses']})"})
        if len(out) < 20:
            where, params = _tokens_where("name", q)
            for r in conn.execute(
                    f"SELECT id, name, ctype FROM ref_counterparties WHERE {where} "
                    "ORDER BY length(name) LIMIT ?",
                    (*params, 20 - len(out))).fetchall():
                if r["name"] not in seen:
                    out.append({"id": r["id"], "name": r["name"],
                                "hint": r["ctype"] if r["ctype"] not in (None, "-")
                                else ""})
    conn.close()
    return JSONResponse(out)


@app.get("/api/warehouses")
def warehouses_search(q: str = ""):
    """Поиск складов 1С (только конечные, не группы): по имени, дивизиону,
    группе — токенами, без учёта регистра (топ-20)."""
    conn = connect()
    rows = []
    if len(q.strip()) >= 2:
        where, params = _tokens_where(
            "name || ' ' || COALESCE(top_parent, '') || ' ' || "
            "COALESCE(parent_name, '')", q)
        rows = conn.execute(
            f"SELECT id, name, top_parent, parent_name FROM ref_warehouses "
            f"WHERE is_group = 0 AND ({where}) "
            "ORDER BY top_parent, length(name) LIMIT 20", params).fetchall()
    conn.close()
    return JSONResponse([{"id": r["id"], "name": r["name"],
                          "hint": " / ".join(filter(None, [r["top_parent"],
                                                           r["parent_name"]]))}
                         for r in rows])


# Поля-подсказки: где брать значения. Источник — справочник 1С и/или то, что
# уже встречалось в загруженных файлах (колонка таблицы БП): экономист выбирает
# из выпадающего списка вместо ручного ввода.
SUGGEST_SOURCES: dict[str, dict] = {
    "division":    {"ref": ("ref_divisions", "name"),  "used": ("bp_items", "division")},
    "warehouse":   {"ref": ("ref_warehouses", "name"), "used": ("bp_items", "warehouse")},
    "supplier":    {"used": ("bp_items", "supplier")},
    "shipment":    {"used": ("bp_items", "shipment")},
    "price_owner": {"used": ("bp_items", "price_owner")},
    "unit":        {"used": ("bp_items", "unit")},
    "manager":     {"used": ("business_plans", "manager")},
    "bp_division": {"ref": ("ref_divisions", "name"), "used": ("business_plans", "division")},
    "shipment_type": {"used": ("business_plans", "shipment_type")},
    "relocation":  {"used": ("business_plans", "relocation")},
    "start_month": {"used": ("business_plans", "start_month")},
    "source_type": {"used": ("business_plans", "source_type")},
    "nomen_group": {"ref": ("ref_nomen_groups", "name")},
}


def suggest_values(conn, field: str, q: str = "", limit: int = 20) -> list[dict]:
    """Значения для выпадающего списка поля: сначала то, что уже использовали
    (по частоте — самые ходовые сверху), затем справочник 1С."""
    src = SUGGEST_SOURCES.get(field)
    if not src:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    like = f"%{q.strip().lower()}%"
    used = src.get("used")
    if used:
        table, col = used
        where = f"{col} IS NOT NULL AND TRIM({col}) <> ''"
        params: tuple = ()
        if q.strip():
            # lower_ru — регистр кириллицы (штатный lower() SQLite знает
            # только латиницу, «Тестбаза» не нашлась бы по «тест»).
            where += f" AND lower_ru({col}) LIKE ?"
            params = (like,)
        for r in conn.execute(
                f"SELECT {col} AS v, COUNT(*) AS uses FROM {table} WHERE {where} "
                f"GROUP BY {col} ORDER BY uses DESC, {col} LIMIT ?",
                (*params, limit)).fetchall():
            seen.add(r["v"])
            out.append({"name": r["v"], "hint": f"использовано {r['uses']}×"})
    ref = src.get("ref")
    if ref and len(out) < limit:
        table, col = ref
        where, params = ("1", ())
        if q.strip():
            where, params = f"lower_ru({col}) LIKE ?", (like,)
        extra = "AND is_group = 0" if table == "ref_warehouses" else ""
        for r in conn.execute(
                f"SELECT {col} AS v FROM {table} WHERE {where} {extra} "
                f"ORDER BY length({col}) LIMIT ?",
                (*params, limit - len(out))).fetchall():
            if r["v"] not in seen:
                seen.add(r["v"])         # в справочниках 1С имена повторяются
                out.append({"name": r["v"], "hint": "справочник 1С"})
    return out


@app.get("/api/suggest/{field}")
def suggest_api(field: str, q: str = ""):
    """Подсказки для полей карточки (дивизион, склад, поставщик, ед. изм. …).
    Пустой запрос допустим: короткие перечни (ед. изм., вид отгрузки)
    показываются списком сразу, без ввода."""
    conn = connect()
    try:
        return JSONResponse(suggest_values(conn, field, q))
    finally:
        conn.close()


@app.get("/api/field-rules")
def field_rules():
    """Границы числовых полей для проверки прямо в форме.

    Браузер берёт правила ОТСЮДА, а не держит свой список: иначе проверка на
    странице и проверка на сервере разойдутся, и человек будет получать отказ
    в сохранении там, где форма его пропустила. Сервер всё равно проверяет
    заново — это защита, а не удобство; страница лишь ловит опечатку до
    отправки, чтобы не потерять уже набранное.
    """
    return JSONResponse({
        "limit": forms.LIMIT,
        "aliases": forms.ALIASES,
        "rules": {name: {"label": label, "lo": lo, "hi": hi}
                  for name, (label, lo, hi) in forms.RULES.items()},
    })


# ─────────────────────────────────────── Затраты (P&L) ──────────────
@app.post("/bp/{bp_id}/costs/update")
async def update_costs(request: Request, bp_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, costs = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("costs", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение затрат.")
    form = await read_form(request)
    stale = edit_conflict(conn, request, bp, form)
    if stale:
        conn.close()
        return back(request, bp_id, stale)

    # Пишем только те поля, которые реально пришли. Раньше отсутствующая
    # сумма считалась нулём, и одна неполная отправка обнулила все 13 статей
    # боевого БП-0018 (241 358 → 0, чистая прибыль уехала на 190 тыс.).
    pending = []
    # Источник суммы: правка руками помечается отклонением от норматива, а
    # обоснование сохраняется рядом с цифрой. Значение фиксируем ТОЛЬКО если
    # сумма действительно изменилась — иначе повторное сохранение формы
    # переписывало бы происхождение модельных сумм в «вручную».
    origins: list[tuple] = []
    notes: list[tuple] = []
    for c in costs:
        sets, values = [], []
        if form.has(f"amount_{c['id']}"):
            amount = form.num(f"amount_{c['id']}", c["amount"])
            amount = 0.0 if amount is None else amount
            sets.append("amount = ?")
            values.append(amount)
            if abs(amount - float(c["amount"] or 0)) > 0.005:
                origins.append((c["id"], "bsp", amount))
        # Сумма варианта «Лукойл»: пустое поле = NULL (наследуется сумма ДСП).
        if form.has(f"amount_luk_{c['id']}"):
            amount_luk = form.num(f"amount_luk_{c['id']}", c["amount_luk"])
            sets.append("amount_luk = ?")
            values.append(amount_luk)
            was = c["amount_luk"]
            if (amount_luk is None) != (was is None) or (
                    amount_luk is not None and was is not None
                    and abs(amount_luk - float(was)) > 0.005):
                origins.append((c["id"], "luk", amount_luk))
        if form.has(f"comment_{c['id']}"):
            sets.append("comment = ?")
            values.append(form.text(f"comment_{c['id']}"))
        # Обоснование отклонения — своё у каждого варианта расчёта.
        for suffix, var in (("", "bsp"), ("_luk", "luk")):
            field = f"why{suffix}_{c['id']}"
            if form.has(field):
                notes.append((c["id"], var, form.text(field)))
        if sets:
            pending.append((f"UPDATE bp_costs SET {', '.join(sets)} WHERE id = ?",
                            (*values, c["id"])))
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    # Форма блока «Отклонения от нормативов» присылает только объяснения —
    # сумм в ней нет, и раньше такая отправка отсекалась как пустая.
    if not pending and not notes:
        conn.close()
        return back(request, bp_id, "Форма пришла без сумм — ничего не сохранено.")
    for sql, args in pending:
        conn.execute(sql, args)
    who = actor(request)
    for cost_id, var, amount in origins:
        origin.set_origin(conn, cost_id, var, origin.MANUAL,
                          "введено вручную", amount, author=who)
    for cost_id, var, note in notes:
        origin.set_note(conn, cost_id, var, note)
    log(conn, actor(request), bp_id, "update_costs",
        f"вручную изменено статей: {len(origins)}" if origins else "")
    conn.commit()
    conn.close()
    msg = "Затраты сохранены." if pending else "Объяснения сохранены."
    if origins:
        msg += (f" Помечено как ручной ввод: {len(origins)}. "
                "Отклонения от накопленного уровня показаны в блоке "
                "«Отклонения от нормативов» — их стоит объяснить.")
    return back(request, bp_id, msg)


def load_cost_overrides(conn, bp_id: int) -> dict[str, float]:
    return {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM bp_cost_params WHERE bp_id = ?", (bp_id,)).fetchall()}


def control_gap(conn, bp, items: list, variant: str = "bsp") -> float | None:
    """Расхождение расчёта с контрольной чистой прибылью из книги, руб.

    None — сверять не с чем (у БП нет контрольной суммы этого варианта).
    Ноль означает «сходится до рубля» — именно это состояние и нельзя терять
    молча, поэтому цифра считается до и после действий, меняющих затраты.
    """
    control = bp[f"control_net_profit_{variant}"] if bp is not None else None
    if control is None or not items:
        return None
    costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ?",
                         (bp["id"],)).fetchall()
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)
    return calc.pnl(bp_v, items_v, costs_v, conn)["net_profit"] - control


def model_costs(conn, bp_id: int, items: list, variant: str = "bsp") -> dict:
    """Модель затрат по нормативам — ТОЛЬКО расчёт, в базу ничего не пишет.

    Показ модели и запись сумм в расчёт разделены намеренно. Раньше это было
    одно действие, и любой ввод в поле параметра (даже значения, равного
    нормативу) переписывал все статьи варианта суммами по модели. На боевом
    БП-0018 так затёрло суммы, взятые из книги экономистов: сумма статей
    «Лукойл» 1 665 970 → 2 001 424, чистая прибыль 31 714 → −294 715, и
    расхождение с книгой 326 429 руб появилось без единой осознанной правки.
    Человек при этом видел только «Готово: пересчитано и сохранено».
    """
    overrides = load_cost_overrides(conn, bp_id)
    return norms.evaluate_model(items, conn, overrides)


def write_model_costs(conn, bp_id: int, items: list,
                      variant: str = "bsp", author: str | None = None) -> dict:
    """Записать суммы модели в статьи затрат (кнопка «Затраты по нормативам»).

    Вызывается только явным действием человека: суммы из книги заменяются
    нормативными, и это меняет расчёт. При активном варианте «Лукойл» пишется
    колонка amount_luk — базовый расчёт ДСП не трогается.
    """
    overrides = load_cost_overrides(conn, bp_id)
    model = norms.evaluate_model(items, conn, overrides)
    computed = norms.compute_norm_costs(items, conn, overrides)
    existing = {c["item"]: c["id"] for c in conn.execute(
        "SELECT id, item FROM bp_costs WHERE bp_id = ?", (bp_id,)).fetchall()}
    sections = {c["article"]: c["section"] for c in model["components"]}
    col = "amount_luk" if variant == "luk" else "amount"
    # Дата нормативов: по ней потом видно, на какой версии справочника
    # считали — норматив мог смениться уже после записи суммы.
    norms_at = (conn.execute(
        "SELECT value FROM settings WHERE key = 'norms_updated_at'").fetchone()
        or {"value": None})["value"]
    ref = f"модель затрат, нормативы на {norms_at[:16]}" if norms_at \
        else "модель затрат"
    for article, (amount, detail) in computed.items():
        if article in existing:
            cost_id = existing[article]
            conn.execute(f"UPDATE bp_costs SET {col} = ?, comment = ? WHERE id = ?",
                         (amount, f"по модели: {detail}"[:300], cost_id))
        else:
            cur = conn.execute(
                f"INSERT INTO bp_costs (bp_id, section, item, {col}, comment) "
                "VALUES (?, ?, ?, ?, ?)",
                (bp_id, sections.get(article, "Переменные"), article, amount,
                 f"по модели: {detail}"[:300]))
            cost_id = cur.lastrowid
        origin.set_origin(conn, cost_id, variant, origin.MODEL, ref, amount,
                          author=author)
    return model


@app.post("/bp/{bp_id}/costs/norms")
def costs_from_norms(request: Request, bp_id: int, variant: str = Form("bsp")):
    """Пересчёт статей затрат по единой модели (драйверы × нормативы).
    Суммы записываются в активный вариант расчёта."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("costs", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение затрат.")
    if not items:
        conn.close()
        return back(request, bp_id, "Сначала добавьте позиции лота.")
    variant = variant if variant in calc.VARIANTS else "bsp"
    # Сходимость с книгой ДО записи: если она была, человек должен увидеть,
    # что именно этим действием он её потерял.
    control = bp[f"control_net_profit_{variant}"]
    before = control_gap(conn, bp, items, variant)
    write_model_costs(conn, bp_id, items, variant, author=actor(request))
    log(conn, actor(request), bp_id, "costs_norms",
        f"пересчёт по модели ({calc.VARIANTS[variant]})")
    conn.commit()
    after = control_gap(conn, load_bp(conn, bp_id)[0], items, variant)
    conn.close()
    msg = f"Затраты пересчитаны по модели (вариант {calc.VARIANTS[variant]})."
    if control is not None and after is not None:
        if before is not None and abs(before) < 0.5 and abs(after) >= 0.5:
            msg += (f" ВНИМАНИЕ: расчёт больше не сходится с книгой — "
                    f"расхождение {after:,.0f} руб. Суммы из книги заменены "
                    f"нормативными; прежние остались в предыдущей версии "
                    f"расчёта.".replace(",", " "))
        else:
            msg += f" Расхождение с книгой: {after:,.0f} руб.".replace(",", " ")
    return back(request, bp_id, msg)


@app.post("/bp/{bp_id}/costs/apply-hints")
def apply_cost_hints(request: Request, bp_id: int):
    """Применение подсказок из перечня к пустым драйверам затрат по позициям."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    can_edit = (workflow.can_edit_section("costs", role, bp["status"]) or
                workflow.can_edit_section("items", role, bp["status"])) if bp else False
    if bp is None or not can_edit:
        conn.close()
        return back(request, bp_id, "Нет прав на применение подсказок.")

    applied = 0
    for hint in cost_hints_for_items(items)["items"]:
        it = hint["item"]
        fields = hint["fields"]
        sets, values = [], []
        if "own_transport_pct" in fields and it["own_transport_pct"] is None:
            sets.append("own_transport_pct = ?")
            values.append(fields["own_transport_pct"])
        if "workshop_cut_pct" in fields and it["workshop_cut_pct"] is None:
            sets.append("workshop_cut_pct = ?")
            values.append(fields["workshop_cut_pct"])
        if "distance_km" in fields and it["distance_km"] is None:
            sets.append("distance_km = ?")
            values.append(fields["distance_km"])
        if "shipment" in fields and not (it["shipment"] or "").strip():
            sets.append("shipment = ?")
            values.append(fields["shipment"])
        if sets:
            conn.execute(f"UPDATE bp_items SET {', '.join(sets)} WHERE id = ?",
                         (*values, it["id"]))
            applied += len(sets)

    if applied:
        items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ? ORDER BY id",
                             (bp_id,)).fetchall()
        write_model_costs(conn, bp_id, items)
    log(conn, actor(request), bp_id, "apply_cost_hints", f"{applied} полей")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/economics",
                    f"Применено подсказок по затратам: {applied}.")


# Поля, которые можно подставить в предварительный расчёт. Список закрытый:
# предпросмотр ничего не сохраняет, но и считать по произвольным полям он не
# должен — иначе форма начнёт «показывать» то, чего расчёт не умеет.
PREVIEW_BP_FIELDS = {"lot_cost", "auction_step", "contamination_pct",
                     "removal_months", "vat_rate", "vat_unrecovered_pct",
                     "capital_rate", "capital_base", "payment_delay_months",
                     "tax_rate", "payroll_tax_rate", "shipment_loss_pct"}
PREVIEW_ITEM_FIELDS = {"volume_t", "sale_price", "sale_price_luk",
                       "contamination_pct", "contamination_pct_luk",
                       "own_transport_pct", "workshop_cut_pct", "batch_size_t"}


@app.post("/bp/{bp_id}/preview")
async def bp_preview(request: Request, bp_id: int):
    """Пересчёт «на лету»: что получится при введённых значениях.

    Раньше экономист считал итог сам и вписывал результат, а увидеть цифру
    сервиса мог только после сохранения. Здесь значения из формы
    накладываются на КОПИЮ данных и прогоняются через тот же `calc.pnl` —
    формулы остаются в одном месте, а в базу ничего не пишется до нажатия
    «Сохранить»."""
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return JSONResponse({"error": "БП не найден."}, status_code=404)
    data = await request.json()
    variant = data.get("variant")
    variant = variant if variant in calc.VARIANTS else "bsp"
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)

    # Негодное значение в предварительный расчёт не подставляется, а
    # называется человеку: иначе `1e999` в цене давал inf, и все строки
    # показывали nan — «расчёт сломался», хотя сломан был ввод.
    errors: list[str] = []

    def preview_value(key, raw, current):
        try:
            return forms.number(raw, key)
        except forms.Invalid as exc:
            errors.append(str(exc))
            return current

    bp_d = calc._to_dict(bp_v)
    for key, raw in (data.get("bp") or {}).items():
        if key in PREVIEW_BP_FIELDS:
            bp_d[key] = preview_value(key, raw, bp_d.get(key))

    edits = {int(k): v for k, v in (data.get("items") or {}).items() if str(k).isdigit()}
    items_d = []
    for it in items_v:
        row = calc._to_dict(it)
        for key, raw in (edits.get(row["id"]) or {}).items():
            if key in PREVIEW_ITEM_FIELDS:
                row[key] = preview_value(key, raw, row.get(key))
        items_d.append(row)

    pnl = calc.pnl(bp_d, items_d, costs_v, conn)
    conn.close()
    return JSONResponse({
        # Ошибки ввода: показываются в полосе предварительного расчёта, чтобы
        # человек увидел опечатку сразу, а не после сохранения.
        "errors": errors[:3],
        # Строки расчёта: у позиции, разделённой между покупателями, их
        # несколько — ключ строки тот же, что в таблице (id позиции + sale_id).
        "rows": [{"id": r["id"], "sale_id": r.get("sale_id"),
                  "revenue": r["revenue"], "alloc_costs": r["alloc_costs"],
                  "profit": r["profit"], "cost_per_t": r["cost_per_t"],
                  "profit_per_t": r["profit_per_t"],
                  "sale_volume_t": r["sale_volume_t"]}
                 for r in pnl["rows"]],
        "totals": {
            "purchase_volume": pnl["purchase_volume"],
            "sale_volume": pnl["sale_volume"],
            "revenue": pnl["revenue"],
            "direct_costs": pnl["direct_costs"],
            "profit_before_tax": pnl["profit_before_tax"],
            "net_profit": pnl["net_profit"],
            "net_profit_per_t": pnl["net_profit_per_t"],
            "ros_pct": pnl["ros_pct"],
            "operating_profit": pnl["operating_profit"],
            "operating_margin_pct": pnl["operating_margin_pct"],
            "lot_cost": pnl["lot_cost"],
            "purchase_price_per_t": pnl["purchase_price_per_t"],
            "above_threshold": pnl["above_threshold"],
        },
    })


@app.post("/bp/{bp_id}/costs/param")
async def set_cost_param(request: Request, bp_id: int):
    """Живой пересчёт: сохранение параметра расчёта этой сделки и возврат
    обновлённой модели (суммы компонентов/статей/секций)."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("costs", role, bp["status"]):
        conn.close()
        return JSONResponse({"error": "Нет прав на изменение затрат."},
                            status_code=403)
    data = await request.json()
    key = (data.get("key") or "").strip()
    raw = str(data.get("value") or "").strip()
    if key:
        # Опечатка в параметре раньше молча превращалась в «пусто» и
        # возвращала норматив: человек видел, что цифра изменилась, и считал,
        # что его значение принято.
        try:
            value = forms.number(raw, key, label="Параметр расчёта", lo=0.0)
        except forms.Invalid as exc:
            conn.close()
            return JSONResponse({"error": str(exc)}, status_code=400)
        if value is None:                       # пусто — вернуть норматив
            conn.execute("DELETE FROM bp_cost_params WHERE bp_id = ? AND key = ?",
                         (bp_id, key))
        else:
            conn.execute(
                "INSERT INTO bp_cost_params (bp_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(bp_id, key) DO UPDATE SET value = excluded.value",
                (bp_id, key, value))
        log(conn, actor(request), bp_id, "cost_param", f"{key} = {raw or 'норматив'}")
    variant = data.get("variant")
    variant = variant if variant in calc.VARIANTS else "bsp"
    model = model_costs(conn, bp_id, items, variant)
    conn.commit()
    conn.close()
    # Суммы статей намеренно не тронуты: об этом говорим прямо, иначе человек
    # решит, что расчёт уже изменился (а раньше он и правда молча менялся).
    model["status"] = ("Параметр сохранён. Статьи затрат не изменены — "
                       "запишите их кнопкой «Записать суммы по модели»")
    return JSONResponse(model)


@app.post("/bp/{bp_id}/costs/add")
def add_cost(request: Request, bp_id: int, section: str = Form(...),
             item: str = Form(...), amount: str = Form("0"), base: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("costs", role, bp["status"]):
        check = forms.Form({"amount": amount})
        amt = check.num("amount") or 0.0
        if not check.ok:
            conn.close()
            return back(request, bp_id, check.message())
        conn.execute(
            "INSERT INTO bp_costs (bp_id, section, item, amount, base) "
            "VALUES (?, ?, ?, ?, ?)",
            (bp_id, section, item.strip(), amt, base.strip() or None))
        log(conn, actor(request), bp_id, "add_cost", item)
        conn.commit()
    conn.close()
    return back(request, bp_id)


@app.post("/bp/{bp_id}/costs/{cost_id}/delete")
def delete_cost(request: Request, bp_id: int, cost_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("costs", role, bp["status"]):
        conn.execute("DELETE FROM bp_costs WHERE id = ? AND bp_id = ?", (cost_id, bp_id))
        conn.commit()
    conn.close()
    return back(request, bp_id)


# ─────────────────────────────────── Сценарии-варианты ──────────────
@app.post("/bp/{bp_id}/scenarios")
async def add_scenario(request: Request, bp_id: int):
    """Добавление сценария-варианта: имя + сдвиг цены + переопределения
    параметров сделки (пустое поле = наследуется от БП)."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("lot", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение сценариев.")
    form = await read_form(request)
    num = form.num
    name = form.text("name")
    values = [num(k) for k in
              ("price_delta", "lot_cost", "vat_unrecovered_pct", "vat_rate",
               "capital_rate", "removal_months", "contamination_pct",
               "tax_rate", "control_net_profit")]
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    if not name:
        conn.close()
        return back(request, bp_id, "Укажите название сценария.")
    conn.execute(
        "INSERT INTO bp_scenarios (bp_id, name, price_delta, lot_cost, "
        "vat_unrecovered_pct, vat_rate, capital_rate, removal_months, "
        "contamination_pct, tax_rate, control_net_profit, comment, sort) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT COALESCE(MAX(sort), 0) + 1 FROM bp_scenarios WHERE bp_id = ?))",
        (bp_id, name, *values, form.text("comment"), bp_id))
    log(conn, actor(request), bp_id, "add_scenario", name)
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/economics", f"Сценарий «{name}» добавлен.")


@app.post("/bp/{bp_id}/scenarios/{scen_id}/delete")
def delete_scenario(request: Request, bp_id: int, scen_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("lot", role, bp["status"]):
        conn.execute("DELETE FROM bp_scenarios WHERE id = ? AND bp_id = ?",
                     (scen_id, bp_id))
        log(conn, actor(request), bp_id, "delete_scenario", str(scen_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/economics")


# ─────────────────────────────────── Вариант «Лукойл» ───────────────
@app.post("/bp/{bp_id}/variant")
def variant_action(request: Request, bp_id: int, action: str = Form(...)):
    """Управление вариантом «Лукойл»: enable — завести (цены и суммы
    наследуются от ДСП, дальше правятся отдельно), disable — убрать вместе
    с его ценами/суммами/переопределениями."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("lot", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение вариантов.")
    if action == "enable":
        conn.execute("UPDATE business_plans SET has_luk = 1, "
                     "active_variant = 'luk' WHERE id = ?", (bp_id,))
        log(conn, actor(request), bp_id, "variant_enable", "Лукойл")
        msg = ("Вариант «Лукойл» заведён: пока он повторяет ДСП — задайте "
               "свои цены реализации и суммы статей в колонках «Лукойл».")
    elif action == "disable":
        conn.execute("UPDATE business_plans SET has_luk = 0, "
                     "active_variant = 'bsp', luk_overrides = NULL, "
                     "control_op_profit_luk = NULL, "
                     "control_net_profit_luk = NULL WHERE id = ?", (bp_id,))
        conn.execute("UPDATE bp_items SET sale_price_luk = NULL "
                     "WHERE bp_id = ?", (bp_id,))
        conn.execute("UPDATE bp_costs SET amount_luk = NULL "
                     "WHERE bp_id = ?", (bp_id,))
        log(conn, actor(request), bp_id, "variant_disable", "Лукойл")
        msg = "Вариант «Лукойл» удалён, карточка показывает ДСП."
    else:
        msg = "Неизвестное действие."
    conn.commit()
    conn.close()
    return back(request, bp_id, msg)


@app.post("/bp/{bp_id}/variant/overrides")
async def variant_overrides(request: Request, bp_id: int):
    """Параметры сделки, отличающиеся в варианте «Лукойл» (срок вывоза,
    ставки, порог закупки): пустое поле = как в ДСП."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("lot", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение вариантов.")
    form = await read_form(request)
    overrides = {}
    for key in calc.VARIANT_OVERRIDE_FIELDS:
        if not form.raw(key).strip():
            continue                     # пусто = «как в ДСП», а не ноль
        value = form.num(key)
        if value is not None:
            overrides[key] = value
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    conn.execute("UPDATE business_plans SET luk_overrides = ? WHERE id = ?",
                 (json.dumps(overrides, ensure_ascii=False) if overrides else None,
                  bp_id))
    log(conn, actor(request), bp_id, "variant_overrides", json.dumps(overrides))
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant=luk", "Параметры варианта сохранены.")


# ─────────────────────────────────────── Риски ──────────────────────
@app.post("/bp/{bp_id}/risks")
def add_risk(request: Request, bp_id: int, risk_type: str = Form(...),
             description: str = Form(""), probability: str = Form("С"),
             impact: str = Form("З"), mitigation: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("risks", role, bp["status"]):
        conn.execute(
            "INSERT INTO bp_risks (bp_id, risk_type, description, probability, impact, "
            "mitigation) VALUES (?, ?, ?, ?, ?, ?)",
            (bp_id, risk_type, description, probability, impact, mitigation))
        conn.commit()
    conn.close()
    return back(request, bp_id)


@app.post("/bp/{bp_id}/risks/{risk_id}/delete")
def delete_risk(request: Request, bp_id: int, risk_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("risks", role, bp["status"]):
        conn.execute("DELETE FROM bp_risks WHERE id = ? AND bp_id = ?", (risk_id, bp_id))
        conn.commit()
    conn.close()
    return back(request, bp_id)


# ─────────────────────────────────────── График вывоза ──────────────
@app.get("/bp/{bp_id}/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, bp_id: int):
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    role = current_role(request)
    sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id = ?",
                              (bp_id,)).fetchall()
    # Срок вывоза может отличаться в варианте «Лукойл» (luk_overrides).
    variant = resolve_variant(request, bp)
    bp_v, items_v, _ = calc.apply_variant(bp, items, [], variant)
    ctx = {
        **base_ctx(request, conn),
        "bp": bp,
        "variant": variant, "variant_label": calc.VARIANTS.get(variant),
        "schedule": calc.removal_schedule(bp_v, items_v, sched_rows, conn),
        "month_names": MONTH_NAMES,
        # Мощность переработки по нормативам базы: видно, успевает ли база
        # переработать приходящий объём (блок «переработка на базе»).
        "capacity": {b: norms.base_capacity(conn, b) for b in
                     {calc.base_of(it) for it in items_v}},
        "can_edit": workflow.can_edit_section("route", role, bp["status"]),
    }
    conn.close()
    return templates.TemplateResponse(request, "schedule.html", ctx)


@app.post("/bp/{bp_id}/schedule/auto")
def schedule_autofill(request: Request, bp_id: int):
    """Автораспределение объёма по месяцам с учётом сезонности мест: на
    зимники технику пускают только зимой, поэтому объём таких баз кладётся
    в доступные месяцы, а не делится поровну на весь срок."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/schedule", "Нет прав на изменение графика.")
    sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id = ?",
                              (bp_id,)).fetchall()
    # Срок вывоза берётся из активного варианта (у «Лукойл» может отличаться).
    bp_v, items_v, _ = calc.apply_variant(bp, items, [],
                                          resolve_variant(request, bp))
    sched = calc.removal_schedule(bp_v, items_v, sched_rows, conn)
    seasonal, warn = 0, []
    for b in sched["bases"]:
        open_months = [r["month"] for r in b["rows"] if r["available"]]
        if not open_months:                      # окна нет — делим на весь срок
            open_months = [r["month"] for r in b["rows"]]
            warn.append(b["base"])
        if b["seasonal"]:
            seasonal += 1
        share = b["volume"] / len(open_months) if open_months else 0.0
        for m in range(1, sched["months"] + 1):
            value = round(share, 3) if m in open_months else 0.0
            conn.execute(
                "INSERT INTO bp_schedule (bp_id, base, month, shipment_t) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(bp_id, base, month) DO UPDATE "
                "SET shipment_t = excluded.shipment_t",
                (bp_id, b["base"], m, value))
    log(conn, actor(request), bp_id, "schedule_auto",
        f"баз {len(sched['bases'])}, сезонных {seasonal}")
    conn.commit()
    conn.close()
    msg = (f"Объём распределён по месяцам: баз {len(sched['bases'])}"
           + (f", из них сезонных {seasonal}" if seasonal else "") + ".")
    if not sched["start_month_number"]:
        msg += (" Месяц начала вывоза не указан — сезонность не учтена, "
                "заполните поле в блоке «Лот».")
    if warn:
        msg += (" Нет доступных месяцев в сроке вывоза у баз: "
                + ", ".join(warn[:3]) + " — распределено равномерно.")
    return redirect(f"/bp/{bp_id}/schedule", msg)


@app.post("/bp/{bp_id}/schedule")
async def schedule_save(request: Request, bp_id: int):
    """Сохранение сетки плана отгрузки: поля ship_{i}_{m} / reloc_{i}_{m},
    имена баз — в hidden base_{i}."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/schedule", "Нет прав на изменение графика.")
    form = await read_form(request)
    # Сначала разбираем всю сетку, потом пишем: одна опечатка в дальней
    # клетке не должна оставить график наполовину сохранённым.
    grid = []
    i = 0
    while form.has(f"base_{i}"):
        base = form.raw(f"base_{i}")
        m = 1
        while form.has(f"ship_{i}_{m}"):
            grid.append((base, m,
                         form.num(f"ship_{i}_{m}", 0.0, field="volume_t",
                                  label=f"Отгрузка {base}, месяц {m}") or 0.0,
                         form.num(f"reloc_{i}_{m}", 0.0, field="volume_t",
                                  label=f"Перемещение {base}, месяц {m}") or 0.0))
            m += 1
        i += 1
    if not form.ok:
        conn.close()
        return redirect(f"/bp/{bp_id}/schedule", form.message())
    for base, m, ship, reloc in grid:
        conn.execute(
            "INSERT INTO bp_schedule (bp_id, base, month, shipment_t, relocation_t) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(bp_id, base, month) DO UPDATE SET "
            "shipment_t = excluded.shipment_t, relocation_t = excluded.relocation_t",
            (bp_id, base, m, ship, reloc))
    log(conn, actor(request), bp_id, "schedule_save", "")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/schedule", "График вывоза сохранён.")


# ─────────────────────────────────────── План-факт (заготовка) ──────
def cost_plan_fact(conn, bp, costs) -> dict:
    """Статьи затрат: план сервиса (на долю лота) / факт регистра 1С по серии.

    Факт — из stat_fact_costs по номеру запроса сделки; статьи 1С уже сложены
    в статьи сервиса по ref_cost_item_map. Статьи «только факт» (списание
    засора) и не сопоставленные показываются отдельно — плана по ним нет.
    """
    from . import fact_costs
    deal_no = deals.request_no(bp)
    fact_rows = fact_costs.for_deal(conn, deal_no) if deal_no else []
    plan: dict[tuple, float] = {}
    for c in calc.share_costs(bp, costs):
        key = (c["section"], c["item"])
        plan[key] = plan.get(key, 0.0) + calc._f(c["amount"])
    fact: dict[tuple, dict] = {}
    extra = []
    for r in fact_rows:
        if r["section"] is None:
            extra.append({**r, "kind": "не относится к сделке"})
        elif r["is_fact_only"]:
            extra.append({**r, "kind": "только в факте"})
        else:
            fact[(r["section"], r["item"])] = r
    rows = []
    for key in sorted(set(plan) | set(fact), key=lambda k: (k[0] or "", k[1] or "")):
        p = plan.get(key, 0.0)
        f = fact.get(key)
        fa = float(f["amount"]) if f else 0.0
        rows.append({"section": key[0], "item": key[1], "plan": p, "fact": fa,
                     "diff": fa - p, "pct": (fa / p * 100.0 if p else None),
                     "items_1c": f["items_1c"] if f else [],
                     "period": (f["period_min"], f["period_max"]) if f else None})
    tot_plan = sum(r["plan"] for r in rows)
    tot_fact = sum(r["fact"] for r in rows)
    return {"deal_no": deal_no, "has_fact": bool(fact_rows), "rows": rows, "extra": extra,
            "total_plan": tot_plan, "total_fact": tot_fact,
            "total_pct": (tot_fact / tot_plan * 100.0 if tot_plan else None),
            "extra_total": sum(float(e["amount"]) for e in extra)}


@app.get("/bp/{bp_id}/planfact", response_class=HTMLResponse)
def planfact_page(request: Request, bp_id: int):
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    role = current_role(request)
    sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id = ?",
                              (bp_id,)).fetchall()
    fact_rows = conn.execute("SELECT * FROM bp_fact WHERE bp_id = ?",
                             (bp_id,)).fetchall()
    # План строится по активному варианту расчёта (цены/затраты варианта).
    variant = resolve_variant(request, bp)
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)
    ctx = {
        **base_ctx(request, conn),
        "bp": bp,
        "variant": variant, "variant_label": calc.VARIANTS.get(variant),
        "pf": calc.plan_fact(bp_v, items_v, costs_v, sched_rows, fact_rows, conn),
        # Факт из 1С — снимок «Реализации» (ночная сборка), против нашего P&L на долю.
        "snap": factsnap.compare(conn, bp_v, items_v, calc.pnl(bp_v, items_v, costs_v, conn)),
        # План по статьям (на долю) против факта регистра затрат 1С по серии.
        "cost_pf": cost_plan_fact(conn, bp_v, costs_v),
        "pnl_share": round(calc.lot_share(bp_v) * 100.0, 2),
        # факт вносится и после согласования БП
        "can_edit": role in ("economist", "director", "admin"),
    }
    conn.close()
    return templates.TemplateResponse(request, "planfact.html", ctx)


@app.post("/bp/{bp_id}/planfact")
async def planfact_save(request: Request, bp_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or role not in ("economist", "director", "admin"):
        conn.close()
        return redirect(f"/bp/{bp_id}/planfact", "Факт вносит экономист.")
    form = await read_form(request)
    months = []
    m = 1
    while form.has(f"vol_{m}"):
        months.append((m, form.num(f"vol_{m}", field="volume_t",
                                   label=f"Факт, объём за месяц {m}"),
                       form.num(f"rev_{m}", field="revenue",
                                label=f"Факт, выручка за месяц {m}"),
                       form.num(f"costs_{m}", field="cost",
                                label=f"Факт, себестоимость за месяц {m}"),
                       form.text(f"comment_{m}")))
        m += 1
    if not form.ok:
        conn.close()
        return redirect(f"/bp/{bp_id}/planfact", form.message())
    for m, volume, revenue, item_costs, comment in months:
        conn.execute(
            "INSERT INTO bp_fact (bp_id, month, volume_t, revenue, costs, comment) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bp_id, month) DO UPDATE SET volume_t = excluded.volume_t, "
            "revenue = excluded.revenue, costs = excluded.costs, "
            "comment = excluded.comment",
            (bp_id, m, volume, revenue, item_costs, comment))
    log(conn, actor(request), bp_id, "planfact_save", "")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/planfact", "Факт сохранён.")


@app.post("/bp/{bp_id}/planfact/import", response_class=HTMLResponse)
async def planfact_import(request: Request, bp_id: int, file: UploadFile):
    """Разбор выгрузки факта из 1С и предпросмотр перед записью.

    Ничего не пишет в базу: показывает, что распозналось и куда ляжет.
    Записывает только следующий шаг (/planfact/apply) после подтверждения —
    факт правит согласованный БП, вслепую его перезаписывать нельзя."""
    role = current_role(request)
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None or role not in ("economist", "director", "admin"):
        conn.close()
        return redirect(f"/bp/{bp_id}/planfact", "Факт вносит экономист.")

    fname = upload_name(file.filename, "факт.xlsx")
    if not fname.lower().endswith((".xlsx", ".xlsm", ".csv")):
        conn.close()
        return redirect(f"/bp/{bp_id}/planfact",
                        "Поддерживаются xlsx, xlsm и csv.")
    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=Path(fname).suffix,
                                     delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        parsed = fact_import.parse_file(tmp_path)
    except Exception as exc:                 # формат чужой — сказать, а не упасть
        conn.close()
        tmp_path.unlink(missing_ok=True)
        return redirect(f"/bp/{bp_id}/planfact",
                        f"Файл не разобран: {type(exc).__name__}. "
                        "Нужны колонки с объёмом, выручкой и периодом.")
    finally:
        tmp_path.unlink(missing_ok=True)

    months = int(_f(bp["removal_months"])
                 or get_setting(conn, "removal_months", 6.0))
    summary = fact_import.aggregate(
        parsed["rows"], bp["bp_number"],
        calc.start_month_number(bp["start_month"]),
        calc.start_year_number(bp["start_month"]), months)
    existing = {r["month"]: r for r in conn.execute(
        "SELECT * FROM bp_fact WHERE bp_id = ?", (bp_id,)).fetchall()}
    ctx = {
        **base_ctx(request, conn),
        "bp": bp, "file_name": fname, "parsed": parsed, "summary": summary,
        "existing": existing, "months_limit": months,
        "calendar": bool(calc.start_month_number(bp["start_month"])),
    }
    conn.close()
    log_conn = connect()
    log(log_conn, actor(request), bp_id, "planfact_import_preview",
        f"{fname}: {len(parsed['rows'])} строк")
    log_conn.commit()
    log_conn.close()
    return templates.TemplateResponse(request, "planfact_import.html", ctx)


@app.post("/bp/{bp_id}/planfact/apply")
async def planfact_apply(request: Request, bp_id: int):
    """Запись подтверждённого свода в факт. Месяцы приходят из предпросмотра."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or role not in ("economist", "director", "admin"):
        conn.close()
        return redirect(f"/bp/{bp_id}/planfact", "Факт вносит экономист.")
    form = await read_form(request)
    mode = form.raw("mode", "replace")
    note = form.raw("source").strip()
    # Свод пришёл из предпросмотра, но проходит ту же проверку: подменённое
    # поле не должно попасть в факт согласованного БП.
    rows = []
    for key in form.data:
        if not key.startswith("vol_") or not key[4:].isdigit():
            continue
        month = int(key[4:])
        rows.append((month,
                     form.num(f"vol_{month}", 0.0, field="volume_t",
                              label=f"Объём за месяц {month}") or 0.0,
                     form.num(f"rev_{month}", 0.0, field="revenue",
                              label=f"Выручка за месяц {month}") or 0.0,
                     form.num(f"costs_{month}", 0.0, field="cost",
                              label=f"Себестоимость за месяц {month}") or 0.0))
    if not form.ok:
        conn.close()
        return redirect(f"/bp/{bp_id}/planfact", form.message())
    written = 0
    for month, volume, revenue, item_costs in rows:
        if mode == "add":
            old = conn.execute(
                "SELECT * FROM bp_fact WHERE bp_id = ? AND month = ?",
                (bp_id, month)).fetchone()
            if old:
                volume += _f(old["volume_t"])
                revenue += _f(old["revenue"])
                item_costs += _f(old["costs"])
        conn.execute(
            "INSERT INTO bp_fact (bp_id, month, volume_t, revenue, costs, comment) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(bp_id, month) DO UPDATE SET "
            "volume_t = excluded.volume_t, revenue = excluded.revenue, "
            "costs = excluded.costs, comment = excluded.comment",
            (bp_id, month, volume, revenue, item_costs,
             f"загружено из {note}" if note else None))
        written += 1
    log(conn, actor(request), bp_id, "planfact_import",
        f"{note}: {written} месяцев, режим {mode}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/planfact",
                    f"Факт загружен: {written} мес.")


# ─────────────────────────────────────── Граф маршрута ──────────────
@app.get("/bp/{bp_id}/route", response_class=HTMLResponse)
def route_page(request: Request, bp_id: int):
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    role = current_role(request)
    nodes = conn.execute("SELECT * FROM bp_nodes WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    edges = conn.execute("SELECT * FROM bp_edges WHERE bp_id = ? ORDER BY id",
                         (bp_id,)).fetchall()
    processes = conn.execute("SELECT * FROM bp_processes WHERE bp_id = ? ORDER BY id",
                             (bp_id,)).fetchall()
    node_labels = {n["id"]: n["label"] for n in nodes}
    # Строки затрат: привязанные к стрелкам (edge_id) и свободные для привязки.
    costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ? ORDER BY section, id",
                         (bp_id,)).fetchall()
    edge_costs: dict[int, list] = {}
    for c in costs:
        if c["edge_id"]:
            edge_costs.setdefault(c["edge_id"], []).append(c)
    ctx = {
        **base_ctx(request, conn),
        "bp": bp, "nodes": nodes, "edges": edges, "processes": processes,
        "node_labels": node_labels, "flows": flow_groups(items),
        "route_reco": route_recommendation(bp, items, conn),
        "node_kinds": workflow.NODE_KINDS, "node_colors": NODE_COLORS,
        "can_edit": workflow.can_edit_section("route", role, bp["status"]),
        "work_types": conn.execute("SELECT name FROM ref_work_types ORDER BY name").fetchall(),
        "nomenclature": conn.execute("SELECT name FROM nomenclature ORDER BY name").fetchall(),
        "edge_costs": edge_costs,
        "free_costs": [c for c in costs if not c["edge_id"]],
        "cost_sections": ["Переменные", "Персонал", "Постоянные",
                          "Административные", "Прочие"],
        "cost_articles": [item for _, item in calc.DEFAULT_COST_ITEMS],
        "route_checks": route_checks(bp, items, nodes, edges, costs),
        # Затраты стрелок и узлов, посчитанные из таблиц (позиции → модель →
        # этапы), а не привязанные вручную.
        "econ": route_economics(
            bp, items, nodes, edges, costs,
            norms.evaluate_model(items, conn, load_cost_overrides(conn, bp_id))),
    }
    conn.close()
    return templates.TemplateResponse(request, "route.html", ctx)


@app.post("/bp/{bp_id}/route/recommend")
def apply_route_recommendation(request: Request, bp_id: int):
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав на изменение маршрута.")
    reco = route_recommendation(bp, items, conn)
    if not reco["locations"]:
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Недостаточно данных КП для маршрута.")

    conn.execute("DELETE FROM bp_processes WHERE bp_id = ?", (bp_id,))
    conn.execute("DELETE FROM bp_edges WHERE bp_id = ?", (bp_id,))
    conn.execute("DELETE FROM bp_nodes WHERE bp_id = ?", (bp_id,))

    seller_id = _insert_route_node(conn, bp_id, "seller", reco["seller"], -520, 0)
    buyer_id = _insert_route_node(conn, bp_id, "buyer", reco["buyer"], 620, 0)

    point_ids = {}
    point_count = max(len(reco["route_points"]), 1)
    for idx, point in enumerate(reco["route_points"]):
        y = int((idx - (point_count - 1) / 2) * 170)
        point_ids[point["key"]] = _insert_route_node(
            conn, bp_id, point["kind"], point["label"][:90], 220, y,
            point["ref_kind"], point["ref_id"],
        )

    loc_count = max(len(reco["locations"]), 1)
    for idx, loc in enumerate(reco["locations"]):
        y = int((idx - (loc_count - 1) / 2) * 135)
        loc_label = loc["place"][:80]
        loc_id = _insert_route_node(conn, bp_id, "custody", loc_label, -180, y)
        conn.execute(
            "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, volume_t, comment) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bp_id, seller_id, loc_id, "право собственности / КП", loc["volume"],
             f"{loc['items']} позиций в перечне"),
        )
        point_id = point_ids.get(loc["route_key"])
        dist = f"{loc['avg_dist']:.0f} км" if loc["avg_dist"] is not None else "км не указаны"
        for cat, vol in loc["categories"].items():
            transport = "авто до рекомендованного склада"
            if loc["avg_dist"] and loc["avg_dist"] >= 50:
                transport = "авто до склада, далее консолидация"
            conn.execute(
                "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, flow_group, "
                "volume_t, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bp_id, loc_id, point_id, transport, cat, vol,
                 f"рекомендация: {dist}; точка из справочника — {loc['route_point']['source']}"),
            )

    for point in reco["route_points"]:
        point_id = point_ids[point["key"]]
        conn.execute(
            "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, volume_t, comment) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bp_id, point_id, buyer_id, "отгрузка покупателю / завод", point["volume"],
             "консолидация потоков после рекомендованного склада/подразделения"),
        )

    log(conn, actor(request), bp_id, "route_recommend", f"{len(reco['locations'])} мест хранения")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route",
                    "Граф маршрута построен по рекомендации из КП.")


@app.get("/bp/{bp_id}/route/data")
def route_data(bp_id: int):
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    nodes = conn.execute("SELECT * FROM bp_nodes WHERE bp_id = ?", (bp_id,)).fetchall()
    edges = conn.execute("SELECT * FROM bp_edges WHERE bp_id = ?", (bp_id,)).fetchall()
    procs = conn.execute(
        "SELECT node_id, GROUP_CONCAT(work_type, '\n') AS wt, COUNT(*) AS c "
        "FROM bp_processes WHERE bp_id = ? GROUP BY node_id", (bp_id,)).fetchall()
    proc_info = {p["node_id"]: p for p in procs}
    # Затраты стрелок и узлов считаются из таблиц (позиции → нормативная
    # модель → этапы); статьи вне модели, привязанные вручную, добавляются.
    costs = conn.execute("SELECT * FROM bp_costs WHERE bp_id = ?",
                         (bp_id,)).fetchall()
    econ = route_economics(
        bp, items, nodes, edges, costs,
        norms.evaluate_model(items, conn, load_cost_overrides(conn, bp_id)))
    edge_cost_sum = {eid: a["total"] for eid, a in econ["edges"].items()}
    edge_cost_lines: dict[int, list[str]] = {}
    for eid, a in econ["edges"].items():
        lines = [f"{stage}: {amount:,.0f} руб".replace(",", " ")
                 for stage, amount in a["stages"].items() if amount]
        edge_cost_lines[eid] = lines + a["lines"]
    max_edge_vol = max((e["volume_t"] or 0 for e in edges), default=0.0)
    conn.close()

    flows = {g["name"]: g for g in flow_groups(items)}
    kind_prefix = {"seller": "Продавец", "base": "База", "workshop": "Цех",
                   "production": "Производство", "custody": "Хранение",
                   "buyer": "Покупатель"}
    return JSONResponse({
        "flows": [{"name": g["name"], "color": g["color"], "ptype": g["ptype"],
                   "volume": round(g["volume"], 1), "count": g["count"]}
                  for g in flows.values()],
        "nodes": [{
            "id": n["id"],
            "label": f"{kind_prefix.get(n['kind'], 'Узел')}: {n['label']}"
                     + (f"\n{proc_info[n['id']]['c']} проц."
                        if n["id"] in proc_info else "")
                     + (f"\nпереработка {econ['nodes'][n['id']]['model']:,.0f} руб"
                        .replace(",", " ")
                        + (f" · {econ['nodes'][n['id']]['per_t']:,.0f} руб/тн"
                           .replace(",", " ")
                           if econ["nodes"][n["id"]]["per_t"] else "")
                        if econ["nodes"].get(n["id"], {}).get("model") else ""),
            "title": "\n".join(filter(None, [
                (proc_info[n["id"]]["wt"] if n["id"] in proc_info else None),
                ("Переработка на базе (из нормативов): "
                 + f"{econ['nodes'][n['id']]['model']:,.0f} руб".replace(",", " ")
                 if econ["nodes"].get(n["id"], {}).get("model") else None)])) or None,
            "color": NODE_COLORS.get(n["kind"], "#4da3c7"),
            "kind": n["kind"],
            "x": n["x"], "y": n["y"],
        } for n in nodes],
        "edges": [{
            "id": e["id"], "from": e["from_node"], "to": e["to_node"],
            "label": " · ".join(filter(None, [
                e["flow_group"] or "",
                e["transport"] or "",
                f"{e['volume_t']:g} тн" if e["volume_t"] else "",
                (f"{edge_cost_sum[e['id']]:,.0f} руб".replace(",", " ")
                 if edge_cost_sum.get(e["id"]) else ""),
                (f"{edge_cost_sum[e['id']] / e['volume_t']:,.0f} руб/тн".replace(",", " ")
                 if edge_cost_sum.get(e["id"]) and e["volume_t"] else "")])) or None,
            "title": ("Затраты пути (из таблиц БП):\n"
                      + "\n".join(edge_cost_lines[e["id"]])
                      if edge_cost_lines.get(e["id"]) else None),
            "color": (flows.get(e["flow_group"] or "", {}) or {}).get("color", "#607d8b"),
            # Толщина стрелки — по тоннажу: крупные потоки видны сразу.
            "width": (1.5 + min(4.5, 5.0 * (e["volume_t"] or 0) / max_edge_vol)
                      if max_edge_vol else 2.5),
            "arrows": "to",
        } for e in edges],
    })


def _clean_ref_name(raw: str) -> tuple[str, str]:
    """'Пермь [3.Пермь]' → ('Пермь', '3.Пермь'); без скобок — hint пустой."""
    import re
    m = re.match(r"^(.*?)\s*\[(.+)\]\s*$", raw.strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else (raw.strip(), "")


def _base_point_from_note(note: str | None) -> str | None:
    m = re.search(r"базовый логистический пункт\s+([^/]+)", note or "",
                  flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def _route_point_from_refs(conn, base_point: str | None, place: str | None) -> dict:
    """Прогнозируемая точка маршрута из наших справочников складов/подразделений."""
    if conn is None:
        return {"kind": "base", "label": base_point or "Склад не определён",
                "ref_kind": None, "ref_id": None, "source": "без справочника"}

    query = (base_point or place or "").strip()
    candidates = []
    if query:
        like = f"%{query}%"
        candidates = conn.execute(
            "SELECT id, name, top_parent, parent_name FROM ref_warehouses "
            "WHERE is_group = 0 AND (lower(name) LIKE lower(?) "
            "OR lower(COALESCE(parent_name, '')) LIKE lower(?) "
            "OR lower(COALESCE(top_parent, '')) LIKE lower(?)) "
            "LIMIT 50",
            (like, like, like),
        ).fetchall()

    def score(row) -> int:
        text_name = (row["name"] or "").lower()
        text_parent = (row["parent_name"] or "").lower()
        text_top = (row["top_parent"] or "").lower()
        q = query.lower()
        s = 0
        if q and q in text_name:
            s += 100
        if q and q in text_parent:
            s += 70
        if q and q in text_top:
            s += 35
        if "собственные склады" in text_top:
            s += 90
        if "база" in text_name or "база" in text_parent or "склад" in text_name:
            s += 45
        if "1.западная сибирь" in text_top:
            s += 25
        if "лукойл" in text_top:
            s -= 35
        if "не используем" in text_top or "не используем" in text_parent:
            s -= 100
        return s

    if candidates:
        best = max(candidates, key=score)
        label = best["parent_name"] if best["parent_name"] and "база" in best["parent_name"].lower() \
                else best["name"]
        label = label or best["name"]
        detail = " / ".join(x for x in [best["name"], best["parent_name"], best["top_parent"]]
                            if x and x != label)
        return {"kind": "base", "label": label, "detail": detail,
                "ref_kind": "warehouse",
                "ref_id": best["id"], "source": f"склад из справочника по «{query}»"}

    if query:
        div = conn.execute(
            "SELECT id, name, parent_name FROM ref_divisions "
            "WHERE lower(name) LIKE lower(?) OR lower(COALESCE(parent_name, '')) LIKE lower(?) "
            "ORDER BY name LIMIT 1",
            (f"%{query}%", f"%{query}%"),
        ).fetchone()
        if div:
            label = " / ".join(filter(None, [div["name"], div["parent_name"]]))
            return {"kind": "production", "label": label, "detail": "",
                    "ref_kind": "division",
                    "ref_id": div["id"], "source": f"подразделение из справочника по «{query}»"}

    return {"kind": "base", "label": f"Точка не найдена в справочнике: {query or 'не указано'}",
            "detail": "", "ref_kind": None, "ref_id": None,
            "source": "нет совпадения в справочниках"}


def route_recommendation(bp, items: list, conn=None) -> dict:
    """Рекомендованный маршрут: место хранения → склад/подразделение из справочника."""
    groups: dict[str, dict] = {}
    for it in items:
        vol = float(it["volume_t"] or 0)
        if vol <= 0:
            continue
        place = (it["warehouse"] or it["division"] or it["supplier"] or "Место хранения").strip()
        g = groups.setdefault(place, {
            "place": place, "volume": 0.0, "dist_w": 0.0, "dist_vol": 0.0,
            "base_points": {}, "categories": {}, "items": 0,
        })
        g["volume"] += vol
        g["items"] += 1
        if it["distance_km"] is not None:
            g["dist_w"] += vol * float(it["distance_km"])
            g["dist_vol"] += vol
        base_point = _base_point_from_note(it["note"]) or "логистическая точка не указана"
        g["base_points"][base_point] = g["base_points"].get(base_point, 0.0) + vol
        cat = (it["category"] or it["purchase_type"] or "поток").strip()
        g["categories"][cat] = g["categories"].get(cat, 0.0) + vol

    locations = []
    for g in groups.values():
        avg_dist = g["dist_w"] / g["dist_vol"] if g["dist_vol"] else None
        base_point = max(g["base_points"].items(), key=lambda x: x[1])[0]
        route_point = _route_point_from_refs(conn, base_point, g["place"])
        route_key = f"{route_point['ref_kind']}:{route_point['ref_id']}" \
                    if route_point["ref_id"] else route_point["label"]
        locations.append({**g, "avg_dist": avg_dist, "base_point": base_point,
                          "route_point": route_point, "route_key": route_key})
    locations.sort(key=lambda x: x["volume"], reverse=True)

    known = [x["avg_dist"] for x in locations if x["avg_dist"] is not None]
    max_dist = max(known) if known else None
    weighted_dist = (sum(x["volume"] * (x["avg_dist"] or 0) for x in locations
                         if x["avg_dist"] is not None) /
                     sum(x["volume"] for x in locations if x["avg_dist"] is not None)) \
                    if known else None
    avoided_tkm = sum(x["volume"] * max(max_dist - (x["avg_dist"] or max_dist), 0)
                      for x in locations) if max_dist is not None else 0.0
    fuel_price = get_setting(conn, "fuel_price", 76.52) if conn else 76.52
    fuel_rate = get_setting(conn, "fuel_rate_l_km", 0.55) if conn else 0.55
    truck_capacity = get_setting(conn, "truck_capacity_t", 16.0) if conn else 16.0
    transport_tkm_rate = fuel_rate * fuel_price * 2 / truck_capacity if truck_capacity else 0.0
    weighted_saving_km = avoided_tkm / sum(x["volume"] for x in locations) \
                         if locations and avoided_tkm else 0.0
    saving_rub = avoided_tkm * transport_tkm_rate

    route_points = {}
    for x in locations:
        key = x["route_key"]
        rp = x["route_point"]
        rec = route_points.setdefault(key, {"key": key, **rp, "volume": 0.0,
                                            "locations": 0, "dist_w": 0.0})
        rec["volume"] += x["volume"]
        rec["locations"] += 1
        if x["avg_dist"] is not None:
            rec["dist_w"] += x["avg_dist"] * x["volume"]
    for rec in route_points.values():
        rec["avg_dist"] = rec["dist_w"] / rec["volume"] if rec["volume"] else None

    return {
        "locations": locations,
        "route_points": sorted(route_points.values(), key=lambda x: x["volume"], reverse=True),
        "total_volume": sum(x["volume"] for x in locations),
        "weighted_dist": weighted_dist,
        "max_dist": max_dist,
        "avoided_tkm": avoided_tkm,
        "weighted_saving_km": weighted_saving_km,
        "transport_rate": transport_tkm_rate,
        "saving_rub": saving_rub,
        "seller": bp["seller_name"] or "Продавец",
        "buyer": bp["buyer_name"] or "Покупатель / завод",
    }


def _insert_route_node(conn, bp_id: int, kind: str, label: str, x: int, y: int,
                       ref_kind: str | None = None, ref_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO bp_nodes (bp_id, kind, label, ref_kind, ref_id, x, y) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bp_id, kind, label, ref_kind, ref_id, x, y),
    )
    return cur.lastrowid


# ─────────────────── Граф из данных БП (а не шаблон) ─────────────────
def _route_base_groups(items: list) -> list[dict]:
    """Базы БП с их объёмами и логистикой из позиций: вид отгрузки и
    покупатель — по преобладающему тоннажу. Именно эти данные определяют
    топологию графа конкретного БП."""
    groups: dict[str, dict] = {}
    for it in items:
        b = calc.base_of(it)
        g = groups.setdefault(b, {"base": b, "volume": 0.0, "count": 0,
                                  "shipments": {}, "buyers": {}})
        vol = float(it["volume_t"] or 0)
        g["volume"] += vol
        g["count"] += 1
        for key, value in (("shipments", it["shipment"]), ("buyers", it["buyer"])):
            value = (value or "").strip()
            if value:
                g[key][value] = g[key].get(value, 0.0) + vol
    out = []
    for g in groups.values():
        g["shipment"] = (max(g["shipments"], key=g["shipments"].get)
                         if g["shipments"] else None)
        g["buyer"] = max(g["buyers"], key=g["buyers"].get) if g["buyers"] else None
        out.append(g)
    out.sort(key=lambda g: -g["volume"])
    return out


def _match_cost_edge(conn, bp_id: int) -> int:
    """Автопривязка статей затрат к стрелкам по базе статьи: статья с
    заполненной базой цепляется к стрелке, выходящей из узла этой базы.
    Возвращает число привязанных статей."""
    from .calc import _base_tokens, match_score
    edges = conn.execute(
        "SELECT e.id, e.from_node, n.label FROM bp_edges e "
        "JOIN bp_nodes n ON n.id = e.from_node "
        "WHERE e.bp_id = ? AND n.kind != 'seller'", (bp_id,)).fetchall()
    linked = 0
    for c in conn.execute(
            "SELECT id, base FROM bp_costs WHERE bp_id = ? AND edge_id IS NULL "
            "AND COALESCE(base, '') != ''", (bp_id,)).fetchall():
        want = _base_tokens(c["base"])
        if not want:
            continue
        scored = [(match_score(want, _base_tokens(e["label"])), e["id"])
                  for e in edges]
        best = max((s for s, _ in scored), default=0)
        hits = {eid for s, eid in scored if s == best and s > 0}
        if len(hits) == 1:                 # лучший и однозначный — привязываем
            conn.execute("UPDATE bp_costs SET edge_id = ? WHERE id = ?",
                         (hits.pop(), c["id"]))
            linked += 1
    return linked


def _match_base_node(nodes: list, base: str) -> int | None:
    """Узел графа для базы позиций: лучший по рангу совпадения имён."""
    from .calc import _base_tokens, match_score
    want = _base_tokens(base)
    best, best_score = None, 0
    for n in nodes:
        if n["kind"] == "seller":
            continue
        low_n, low_b = (n["label"] or "").lower(), (base or "").lower()
        s = 5 if (low_b and (low_b in low_n or low_n in low_b)) else \
            match_score(want, _base_tokens(n["label"]))
        if s > best_score:
            best, best_score = n["id"], s
    return best


def route_economics(bp, items: list, nodes: list, edges: list, costs: list,
                    model: dict) -> dict:
    """Экономика графа маршрута: затраты на стрелках и узлах считаются ИЗ
    ТАБЛИЦ (позиции → нормативная модель → этапы), а не привязываются руками.

    Этап «Вывоз с цеха на базу» ложится на стрелки, входящие в узел базы;
    «Переработка на базе» — на сам узел базы; «Отгрузка с базы» — на
    исходящие стрелки (в первую очередь путь к покупателю). Внутри этапа
    сумма базы делится между её стрелками пропорционально тоннажу.
    Статьи вне модели (административные, прочие), привязанные к стрелке
    вручную, добавляются сверху — модель их не считает.
    """
    node_by_id = {n["id"]: n for n in nodes}
    edge_alloc: dict[int, dict] = {
        e["id"]: {"model": 0.0, "manual": 0.0, "stages": {}, "lines": []}
        for e in edges}
    node_alloc: dict[int, dict] = {
        n["id"]: {"model": 0.0, "stages": {}, "volume": 0.0} for n in nodes}

    incoming: dict[int, list] = {}
    outgoing: dict[int, list] = {}
    for e in edges:
        incoming.setdefault(e["to_node"], []).append(e)
        outgoing.setdefault(e["from_node"], []).append(e)

    def spread(amount: float, targets: list, stage: str) -> None:
        """Сумма этапа делится между стрелками пропорционально тоннажу."""
        if not targets or amount <= 0:
            return
        weights = [max(float(e["volume_t"] or 0), 0.0) for e in targets]
        total = sum(weights)
        for e, w in zip(targets, weights):
            part = amount * (w / total if total else 1.0 / len(targets))
            a = edge_alloc[e["id"]]
            a["model"] += part
            a["stages"][stage] = a["stages"].get(stage, 0.0) + part

    unmatched: list[str] = []
    for st in model.get("stages", []):
        for row in st["bases"]:
            node_id = _match_base_node(nodes, row["base"])
            if node_id is None:
                unmatched.append(f"{row['base']} ({st['name']})")
                continue
            if st["name"] == "Переработка на базе":
                na = node_alloc[node_id]
                na["model"] += row["amount"]
                na["stages"][st["name"]] = na["stages"].get(st["name"], 0.0) \
                    + row["amount"]
                continue
            if st["name"] == "Вывоз с цеха на базу":
                targets = incoming.get(node_id, [])
            else:                                   # «Отгрузка с базы»
                outs = outgoing.get(node_id, [])
                to_buyer = [e for e in outs
                            if (node_by_id.get(e["to_node"]) or {})["kind"] == "buyer"]
                targets = to_buyer or outs
            spread(row["amount"], targets, st["name"])

    # Ручные статьи вне модели (административные, прочие) — как есть.
    for c in costs:
        if c["edge_id"] and c["edge_id"] in edge_alloc \
                and c["item"] not in norms.MODEL_ARTICLES:
            a = edge_alloc[c["edge_id"]]
            a["manual"] += float(c["amount"] or 0)
            a["lines"].append(f"{c['item']}: "
                              + f"{float(c['amount'] or 0):,.0f} руб".replace(",", " "))

    for e in edges:
        a = edge_alloc[e["id"]]
        a["total"] = a["model"] + a["manual"]
        vol = float(e["volume_t"] or 0)
        a["per_t"] = a["total"] / vol if vol else 0.0
    for n in nodes:
        node_alloc[n["id"]]["per_t"] = 0.0

    # Объём переработки узла — по входящим потокам (для руб/тн узла).
    for n in nodes:
        vol = sum(float(e["volume_t"] or 0) for e in incoming.get(n["id"], []))
        na = node_alloc[n["id"]]
        na["volume"] = vol
        na["per_t"] = na["model"] / vol if vol else 0.0

    on_edges = sum(a["total"] for a in edge_alloc.values())
    on_nodes = sum(a["model"] for a in node_alloc.values())
    model_total = float(model.get("total") or 0)
    return {"edges": edge_alloc, "nodes": node_alloc,
            "on_edges": round(on_edges, 2), "on_nodes": round(on_nodes, 2),
            "placed": round(on_edges + on_nodes, 2),
            "model_total": round(model_total, 2),
            "diff": round(on_edges + on_nodes - model_total, 2),
            "unmatched": unmatched}


def route_checks(bp, items: list, nodes: list, edges: list, costs: list) -> list[dict]:
    """Проверка графа против данных БП: маршрут должен отражать сделку, а не
    быть декорацией. Уровни: ok / warn / bad."""
    from .calc import _base_tokens, match_score
    checks: list[dict] = []
    groups = _route_base_groups(items)
    node_by_id = {n["id"]: n for n in nodes}

    def add(level, text):
        checks.append({"level": level, "text": text})

    def fmt(v):
        return f"{v:,.1f}".replace(",", " ")

    # 1. У каждой базы из позиций есть узел графа.
    # Жадное сопоставление база ↔ узел по рангу совпадения: сначала точные
    # пары, затем остальные; один узел не достаётся двум базам.
    matched_nodes: dict[str, int] = {}
    pair_scores: list[tuple[int, str, int]] = []
    for g in groups:
        want = _base_tokens(g["base"])
        for n in nodes:
            if n["kind"] == "seller":
                continue
            s = match_score(want, _base_tokens(n["label"]))
            if s:
                pair_scores.append((s, g["base"], n["id"]))
    used_nodes: set[int] = set()
    for s, base, node_id in sorted(pair_scores, key=lambda p: -p[0]):
        if base in matched_nodes or node_id in used_nodes:
            continue
        matched_nodes[base] = node_id
        used_nodes.add(node_id)
    for g in groups:
        if g["base"] not in matched_nodes:
            add("bad", f"База «{g['base'][:60]}» ({fmt(g['volume'])} тн, "
                       f"{g['count']} поз.) не имеет узла в графе.")

    # 2. Баланс каждого узла: выход = транзит с других баз + свой объём
    #    позиций. Вход от продавца — переход права собственности, он и есть
    #    «свой объём», в транзит не считается.
    out_by_node: dict[int, float] = {}
    transit_in: dict[int, float] = {}
    in_by_node: dict[int, float] = {}
    for e in edges:
        out_by_node[e["from_node"]] = out_by_node.get(e["from_node"], 0.0) + (e["volume_t"] or 0)
        in_by_node[e["to_node"]] = in_by_node.get(e["to_node"], 0.0) + (e["volume_t"] or 0)
        src = node_by_id.get(e["from_node"])
        if src and src["kind"] != "seller":
            transit_in[e["to_node"]] = transit_in.get(e["to_node"], 0.0) + (e["volume_t"] or 0)
    for n in nodes:
        if n["kind"] in ("seller", "buyer"):
            continue
        own = next((g["volume"] for g in groups
                    if matched_nodes.get(g["base"]) == n["id"]), 0.0)
        transit = transit_in.get(n["id"], 0.0)
        out = out_by_node.get(n["id"], 0.0)
        expected = transit + own
        if not out and not expected:
            continue
        detail = f"свой объём {fmt(own)}" + (f" + транзит {fmt(transit)}" if transit else "")
        if abs(out - expected) > max(0.5, expected * 0.01):
            add("warn", f"Узел «{n['label'][:50]}»: выход {fmt(out)} тн ≠ "
                        f"{detail} тн.")
        else:
            add("ok", f"Узел «{n['label'][:50]}»: выход {fmt(out)} тн = {detail} тн.")

    # 4. Покупатель получает весь тоннаж закупки.
    total = sum(g["volume"] for g in groups)
    for n in nodes:
        if n["kind"] == "buyer":
            got = in_by_node.get(n["id"], 0.0)
            if abs(got - total) > max(0.5, total * 0.01):
                add("warn", f"Покупатель «{n['label'][:40]}» получает {fmt(got)} тн "
                            f"при тоннаже лота {fmt(total)} тн.")
            else:
                add("ok", f"Покупатель получает весь тоннаж лота: {fmt(got)} тн.")

    # 5. Статьи затрат с базой, не привязанные к стрелкам.
    unlinked = [c for c in costs
                if (c["base"] or "").strip() and not c["edge_id"] and c["amount"]]
    for c in unlinked:
        add("warn", f"Статья «{c['item'][:40]}» ({fmt(c['amount'])} руб, база "
                    f"«{c['base'][:35]}») не привязана к стрелке.")
    if not unlinked and any((c["base"] or "").strip() for c in costs):
        add("ok", "Все статьи затрат с базой привязаны к стрелкам графа.")

    # 6. Транспортные стрелки без затрат — подсказка, не ошибка.
    linked_edges = {c["edge_id"] for c in costs if c["edge_id"]}
    for e in edges:
        src = node_by_id.get(e["from_node"])
        dst = node_by_id.get(e["to_node"])
        if src is None or dst is None or src["kind"] == "seller":
            continue                        # переход права собственности
        if e["id"] not in linked_edges:
            add("warn", f"Стрелка «{src['label'][:30]} → {dst['label'][:30]}» "
                        "без строк затрат.")
    return checks


@app.post("/bp/{bp_id}/route/build")
def build_route_from_data(request: Request, bp_id: int):
    """Построение графа из ДАННЫХ БП с учётом РОЛЕЙ точек: продавец → цеха →
    их базы → покупатель. Цех несёт только вывоз на свою базу, база
    собирает объём (свой лежак + цеховые тонны) и отгружает покупателю —
    ровно так же этапы считает модель затрат. Место без роли входит
    самостоятельной базой, как раньше (роли задаются на «Логистике»).
    Статьи затрат с базой автоматически привязываются к стрелкам;
    промежуточные пункты добавляются операцией «вставить узел в путь»."""
    role = current_role(request)
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав на изменение маршрута.")
    groups = _route_base_groups(items)
    if not groups:
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Сначала добавьте позиции лота.")

    conn.execute("DELETE FROM bp_processes WHERE bp_id = ?", (bp_id,))
    conn.execute("DELETE FROM bp_edges WHERE bp_id = ?", (bp_id,))
    conn.execute("DELETE FROM bp_nodes WHERE bp_id = ?", (bp_id,))

    seller_id = _insert_route_node(conn, bp_id, "seller",
                                   bp["seller_name"] or "Продавец", -680, 0)
    buyer_label = (bp["buyer_name"]
                   or next((g["buyer"] for g in groups if g["buyer"]), None)
                   or "Покупатель")
    buyer_id = _insert_route_node(conn, bp_id, "buyer", buyer_label[:90], 680, 0)

    # Роли точек группируют места: цеховые тонны стекаются в узел базы.
    roles = norms.point_roles(conn)
    bases: dict[str, dict] = {}
    unassigned = 0
    for g in groups:
        r = roles.get(logistics.norm_name(g["base"]))
        if r and r["kind"] == "цех" and r["base_name"]:
            key = r["base_name"]
            bases.setdefault(key, {"own": [], "workshops": []})["workshops"].append(g)
        else:
            key = (r["base_name"] if r and r["base_name"] else g["base"])
            bases.setdefault(key, {"own": [], "workshops": []})["own"].append(g)
            if not r:
                unassigned += 1

    def edge(a, b, transport, volume, comment=None):
        conn.execute(
            "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, "
            "volume_t, comment) VALUES (?, ?, ?, ?, ?, ?)",
            (bp_id, a, b, transport, round(volume, 3), comment))

    n_workshops = 0
    proc_nodes: dict[str, int] = {}          # place → узел переработки (база)
    y_cursor = 0.0
    order = sorted(bases.items(),
                   key=lambda kv: -(sum(g["volume"] for g in kv[1]["own"])
                                    + sum(g["volume"] for g in kv[1]["workshops"])))
    total_rows = sum(max(1, len(b["workshops"])) for _, b in order)
    y_cursor = -(total_rows - 1) * 75
    for base_label, b in order:
        rows = max(1, len(b["workshops"]))
        base_y = int(y_cursor + (rows - 1) * 75)
        point = _route_point_from_refs(conn, None, base_label)
        base_node = _insert_route_node(conn, bp_id, "base", base_label[:90],
                                       -140, base_y,
                                       point.get("ref_kind"), point.get("ref_id"))
        own_vol = sum(g["volume"] for g in b["own"])
        own_cnt = sum(g["count"] for g in b["own"])
        if own_vol:
            edge(seller_id, base_node, "переход права собственности", own_vol,
                 f"{own_cnt} позиций на базе"
                 + (f" ({len(b['own'])} мест)" if len(b["own"]) > 1 else ""))
        for j, g in enumerate(b["workshops"]):
            w_y = int(y_cursor + j * 150)
            w_node = _insert_route_node(conn, bp_id, "workshop",
                                        g["base"][:90], -450, w_y)
            edge(seller_id, w_node, "переход права собственности",
                 g["volume"], f"{g['count']} позиций лота")
            edge(w_node, base_node, "авто (вывоз на базу)", g["volume"])
            proc_nodes[logistics.norm_name(g["base"])] = base_node
            n_workshops += 1
        for g in b["own"]:
            proc_nodes[logistics.norm_name(g["base"])] = base_node
        total_vol = own_vol + sum(g["volume"] for g in b["workshops"])
        shipments = [g["shipment"] for g in b["own"] + b["workshops"]
                     if g["shipment"]]
        transport = (max(set(shipments), key=shipments.count) if shipments
                     else bp["shipment_type"] or "вывоз")
        buyers = {g["buyer"] for g in b["own"] + b["workshops"] if g["buyer"]}
        buyer_note = (f"покупатели по позициям: {', '.join(sorted(buyers))}"
                      if buyers and bp["buyer_name"]
                      and buyers != {bp["buyer_name"]} else None)
        edge(base_node, buyer_id, transport, total_vol, buyer_note)
        y_cursor += rows * 150

    # Переработка: если позиция куплена одним типом, а продаётся другим
    # (труба режется в лом), это работа НА БАЗЕ места — процесс цепляется к
    # узлу базы, куда цех свозит металл (по ролям), а не к самому цеху.
    procs = 0
    for it in items:
        ptype = (it["purchase_type"] or "").strip()
        stype = (it["sale_type"] or "").strip()
        if not ptype or not stype or ptype == stype:
            continue
        base = (it["division"] or it["supplier"] or it["warehouse"] or "").strip()
        node_id = proc_nodes.get(logistics.norm_name(base))
        if node_id is None:
            node = conn.execute(
                "SELECT id FROM bp_nodes WHERE bp_id = ? AND label = ? LIMIT 1",
                (bp_id, base[:90])).fetchone()
            node_id = node["id"] if node else None
        if node_id is None:
            continue
        conn.execute(
            "INSERT INTO bp_processes (bp_id, node_id, work_type, input_nomen, "
            "output_nomen, volume_t, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bp_id, node_id, f"переработка: {ptype} в {stype}",
             it["nomenclature"], it["sale_group"] or stype,
             round(float(it["volume_t"] or 0), 3),
             "создано автоматически: тип покупки отличается от типа продажи"))
        procs += 1

    linked = _match_cost_edge(conn, bp_id)
    n_bases = len(bases)
    log(conn, actor(request), bp_id, "route_build",
        f"{n_bases} баз, {n_workshops} цехов, статей привязано {linked}, "
        f"процессов {procs}")
    conn.commit()
    conn.close()
    return redirect(
        f"/bp/{bp_id}/route",
        f"Граф построен по ролям точек: {n_bases} "
        + ("база" if n_bases == 1 else "базы" if 2 <= n_bases <= 4 else "баз")
        + (f", цехов {n_workshops}" if n_workshops else "")
        + f", статей затрат привязано к стрелкам: {linked}"
        + (f", процессов переработки: {procs}" if procs else "")
        + (f". Роли не заданы у {unassigned} мест — они вошли самостоятельными "
           "базами, роли задаются на вкладке «Логистика»" if unassigned else "")
        + ". Промежуточные пункты добавляйте операцией "
        "«вставить узел в путь» у нужной стрелки.")


@app.post("/bp/{bp_id}/route/edges/{edge_id}/insert-node")
async def insert_node_into_edge(request: Request, bp_id: int, edge_id: int):
    """Вставка промежуточного узла в путь (консолидация, перегруз, цех):
    стрелка A→B превращается в A→M и M→B. Затраты стрелки остаются на первом
    плече; отмеченные галочками статьи переезжают на второе. Если стрелка
    M→B уже есть — потоки сливаются (консолидация), тоннаж складывается."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав на изменение маршрута.")
    edge = conn.execute("SELECT * FROM bp_edges WHERE id = ? AND bp_id = ?",
                        (edge_id, bp_id)).fetchone()
    if edge is None:
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Путь не найден.")
    form = await request.form()

    node_id = (form.get("node_id") or "").strip()
    if node_id:
        # Значение из выпадающего списка, но приходит оно из браузера:
        # нечисловое `int()` уронило бы сохранение пятисотой ошибкой.
        if not node_id.isdigit():
            conn.close()
            return redirect(f"/bp/{bp_id}/route", "Узел не найден.")
        mid = conn.execute("SELECT id FROM bp_nodes WHERE id = ? AND bp_id = ?",
                           (int(node_id), bp_id)).fetchone()
        if mid is None:
            conn.close()
            return redirect(f"/bp/{bp_id}/route", "Узел не найден.")
        mid_id = mid["id"]
    else:
        label = (form.get("label") or "").strip()
        if not label:
            conn.close()
            return redirect(f"/bp/{bp_id}/route",
                            "Выберите узел или укажите название нового.")
        kind = (form.get("kind") or "custody").strip()
        if kind not in workflow.NODE_KINDS:
            kind = "custody"
        pos = conn.execute(
            "SELECT (a.x + b.x) / 2 AS x, (a.y + b.y) / 2 AS y FROM bp_edges e "
            "JOIN bp_nodes a ON a.id = e.from_node JOIN bp_nodes b ON b.id = e.to_node "
            "WHERE e.id = ?", (edge_id,)).fetchone()
        mid_id = _insert_route_node(conn, bp_id, kind, label[:90],
                                    int(pos["x"] or 0) if pos else 0,
                                    int((pos["y"] or 0)) - 40 if pos else 0)

    old_to = edge["to_node"]
    transport1 = (form.get("transport1") or "").strip() or edge["transport"]
    transport2 = (form.get("transport2") or "").strip() or edge["transport"]

    # Плечо 1: сохраняем id стрелки — привязанные статьи остаются на нём.
    conn.execute("UPDATE bp_edges SET to_node = ?, transport = ? WHERE id = ?",
                 (mid_id, transport1, edge_id))

    # Плечо 2: слияние с существующей стрелкой M→B (консолидация потоков).
    second = conn.execute(
        "SELECT * FROM bp_edges WHERE bp_id = ? AND from_node = ? AND to_node = ? "
        "AND id != ? LIMIT 1", (bp_id, mid_id, old_to, edge_id)).fetchone()
    if second is not None:
        merged_vol = (second["volume_t"] or 0) + (edge["volume_t"] or 0)
        conn.execute("UPDATE bp_edges SET volume_t = ?, comment = ? WHERE id = ?",
                     (round(merged_vol, 3),
                      ((second["comment"] or "") +
                       " + консолидация потока")[:300].strip(" +"),
                      second["id"]))
        second_id = second["id"]
    else:
        cur = conn.execute(
            "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, flow_group, "
            "volume_t, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bp_id, mid_id, old_to, transport2, edge["flow_group"],
             edge["volume_t"], "второе плечо после вставки узла"))
        second_id = cur.lastrowid

    moved = 0
    for c in conn.execute("SELECT id FROM bp_costs WHERE bp_id = ? AND edge_id = ?",
                          (bp_id, edge_id)).fetchall():
        if form.get(f"move_cost_{c['id']}"):
            conn.execute("UPDATE bp_costs SET edge_id = ? WHERE id = ?",
                         (second_id, c["id"]))
            moved += 1
    log(conn, actor(request), bp_id, "route_insert_node",
        f"путь {edge_id}: узел {mid_id}, статей на 2-е плечо: {moved}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route",
                    "Узел вставлен в путь." +
                    (f" Статей перенесено на второе плечо: {moved}." if moved else ""))


@app.post("/bp/{bp_id}/route/nodes")
def add_node(request: Request, bp_id: int, kind: str = Form(...),
             label: str = Form(""), counterparty_id: str = Form(""),
             counterparty_name: str = Form(""), warehouse_id: str = Form(""),
             warehouse_name: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав на изменение маршрута.")
    ref_kind = ref_id = None
    name = label.strip()
    if kind in ("seller", "buyer"):
        ref = None
        if counterparty_id:
            ref = conn.execute("SELECT * FROM ref_counterparties WHERE id = ?",
                               (counterparty_id,)).fetchone()
        elif counterparty_name.strip():           # id не заполнился — ищем по имени
            cname, _ = _clean_ref_name(counterparty_name)
            ref = conn.execute("SELECT * FROM ref_counterparties WHERE name = ? "
                               "LIMIT 1", (cname,)).fetchone()
        if ref:
            ref_kind, ref_id, name = "counterparty", ref["id"], name or ref["name"]
        elif counterparty_name.strip():
            name = name or _clean_ref_name(counterparty_name)[0]
    else:
        ref = None
        if warehouse_id:
            ref = conn.execute("SELECT * FROM ref_warehouses WHERE id = ?",
                               (warehouse_id,)).fetchone()
        elif warehouse_name.strip():
            wname, whint = _clean_ref_name(warehouse_name)
            rows = conn.execute(
                "SELECT * FROM ref_warehouses WHERE is_group = 0 AND name = ?",
                (wname,)).fetchall()
            if len(rows) > 1 and whint:           # одноимённые склады: сверяем дивизион
                rows = [r for r in rows
                        if whint in (r["top_parent"] or "")
                        or whint in (r["parent_name"] or "")] or rows
            ref = rows[0] if rows else None
        if ref:
            ref_kind, ref_id = "warehouse", ref["id"]
            name = name or f"{ref['name']} ({ref['top_parent'] or ''})".strip(" ()")
        elif warehouse_name.strip():
            name = name or _clean_ref_name(warehouse_name)[0]
    if not name:
        name = workflow.NODE_KINDS.get(kind, kind)
    conn.execute(
        "INSERT INTO bp_nodes (bp_id, kind, label, ref_kind, ref_id) "
        "VALUES (?, ?, ?, ?, ?)", (bp_id, kind, name, ref_kind, ref_id))
    log(conn, actor(request), bp_id, "add_node", name)
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route")


@app.post("/bp/{bp_id}/route/nodes/{node_id}/update")
def update_node(request: Request, bp_id: int, node_id: int, label: str = Form(...),
                kind: str = Form(...)):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("route", role, bp["status"]) \
            and kind in workflow.NODE_KINDS and label.strip():
        conn.execute("UPDATE bp_nodes SET label = ?, kind = ? WHERE id = ? AND bp_id = ?",
                     (label.strip(), kind, node_id, bp_id))
        log(conn, actor(request), bp_id, "update_node", f"{node_id}: {label}")
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route", "Узел обновлён.")


@app.post("/bp/{bp_id}/route/nodes/{node_id}/delete")
def delete_node(request: Request, bp_id: int, node_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("route", role, bp["status"]):
        conn.execute("DELETE FROM bp_nodes WHERE id = ? AND bp_id = ?", (node_id, bp_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route")


@app.post("/bp/{bp_id}/route/edges")
def add_edge(request: Request, bp_id: int, from_node: int = Form(...),
             to_node: int = Form(...), transport: str = Form(""),
             flow_group: str = Form(""), volume_t: str = Form(""),
             comment: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав на изменение маршрута.")
    vol, err = single("volume_t", volume_t)
    if err:
        conn.close()
        return redirect(f"/bp/{bp_id}/route", err)
    conn.execute(
        "INSERT INTO bp_edges (bp_id, from_node, to_node, transport, flow_group, "
        "volume_t, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bp_id, from_node, to_node, transport.strip() or None,
         flow_group.strip() or None, vol, comment.strip() or None))
    log(conn, actor(request), bp_id, "add_edge", f"{from_node}→{to_node}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route")


@app.post("/bp/{bp_id}/route/edges/{edge_id}/update")
def update_edge(request: Request, bp_id: int, edge_id: int,
                transport: str = Form(""), flow_group: str = Form(""),
                volume_t: str = Form(""), comment: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("route", role, bp["status"]):
        vol, err = single("volume_t", volume_t)
        if err:
            conn.close()
            return redirect(f"/bp/{bp_id}/route", err)
        conn.execute(
            "UPDATE bp_edges SET transport = ?, flow_group = ?, volume_t = ?, "
            "comment = ? WHERE id = ? AND bp_id = ?",
            (transport.strip() or None, flow_group.strip() or None, vol,
             comment.strip() or None, edge_id, bp_id))
        log(conn, actor(request), bp_id, "update_edge", str(edge_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route", "Путь обновлён.")


@app.post("/bp/{bp_id}/route/edges/{edge_id}/delete")
def delete_edge(request: Request, bp_id: int, edge_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("route", role, bp["status"]):
        # Привязанные статьи затрат остаются в БП (edge_id → NULL по FK).
        conn.execute("DELETE FROM bp_edges WHERE id = ? AND bp_id = ?", (edge_id, bp_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route")


# ── Затраты на стрелках графа: каждая стрелка перемещения связана с
#    конкретными строками затрат (статьями P&L). ─────────────────────
@app.post("/bp/{bp_id}/route/edges/{edge_id}/costs")
def edge_add_cost(request: Request, bp_id: int, edge_id: int,
                  cost_id: str = Form(""), section: str = Form("Переменные"),
                  item: str = Form(""), amount: str = Form("0"),
                  comment: str = Form("")):
    """Привязка строки затрат к стрелке: существующей (cost_id) или новой
    (секция, статья, сумма). База новой строки — узел-источник стрелки,
    так затраты распределяются на позиции этой базы."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    can = bp is not None and (workflow.can_edit_section("route", role, bp["status"])
                              or workflow.can_edit_section("costs", role, bp["status"]))
    edge = conn.execute("SELECT * FROM bp_edges WHERE id = ? AND bp_id = ?",
                        (edge_id, bp_id)).fetchone() if can else None
    if edge is None:
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав или путь не найден.")

    if cost_id.strip():                          # привязать существующую статью
        conn.execute(
            "UPDATE bp_costs SET edge_id = ? WHERE id = ? AND bp_id = ?",
            (edge_id, int(cost_id), bp_id))
        log(conn, actor(request), bp_id, "edge_link_cost", f"путь {edge_id} ← статья {cost_id}")
        msg = "Статья затрат привязана к пути."
    else:                                        # создать новую строку затрат
        if not item.strip():
            conn.close()
            return redirect(f"/bp/{bp_id}/route", "Укажите статью затрат.")
        amt, err = single("amount", amount)
        if err:
            conn.close()
            return redirect(f"/bp/{bp_id}/route", err)
        amt = amt or 0.0
        from_label = conn.execute("SELECT label FROM bp_nodes WHERE id = ?",
                                  (edge["from_node"],)).fetchone()
        conn.execute(
            "INSERT INTO bp_costs (bp_id, section, item, amount, base, edge_id, comment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bp_id, section, item.strip(), amt,
             from_label["label"] if from_label else None, edge_id,
             comment.strip() or None))
        log(conn, actor(request), bp_id, "edge_add_cost", f"путь {edge_id}: {item} {amt:,.0f}")
        msg = "Строка затрат добавлена и привязана к пути."
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route", msg)


@app.post("/bp/{bp_id}/route/edges/{edge_id}/costs/{cost_id}/unlink")
def edge_unlink_cost(request: Request, bp_id: int, edge_id: int, cost_id: int):
    """Отвязка строки затрат от стрелки (сама статья остаётся в БП)."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    can = bp is not None and (workflow.can_edit_section("route", role, bp["status"])
                              or workflow.can_edit_section("costs", role, bp["status"]))
    if can:
        conn.execute(
            "UPDATE bp_costs SET edge_id = NULL "
            "WHERE id = ? AND bp_id = ? AND edge_id = ?",
            (cost_id, bp_id, edge_id))
        log(conn, actor(request), bp_id, "edge_unlink_cost", f"путь {edge_id} × статья {cost_id}")
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route", "Статья отвязана от пути.")


@app.post("/bp/{bp_id}/route/processes")
def add_process(request: Request, bp_id: int, node_id: int = Form(...),
                work_type: str = Form(...), input_nomen: str = Form(""),
                output_nomen: str = Form(""), volume_t: str = Form(""),
                comment: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return redirect(f"/bp/{bp_id}/route", "Нет прав на изменение маршрута.")
    vol, err = single("volume_t", volume_t)
    if err:
        conn.close()
        return redirect(f"/bp/{bp_id}/route", err)
    conn.execute(
        "INSERT INTO bp_processes (bp_id, node_id, work_type, input_nomen, output_nomen, "
        "volume_t, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bp_id, node_id, work_type.strip(), input_nomen.strip() or None,
         output_nomen.strip() or None, vol, comment.strip() or None))
    log(conn, actor(request), bp_id, "add_process", work_type)
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route")


@app.post("/bp/{bp_id}/route/processes/{proc_id}/update")
def update_process(request: Request, bp_id: int, proc_id: int,
                   work_type: str = Form(...), input_nomen: str = Form(""),
                   output_nomen: str = Form(""), volume_t: str = Form(""),
                   comment: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("route", role, bp["status"]) and work_type.strip():
        vol, err = single("volume_t", volume_t)
        if err:
            conn.close()
            return redirect(f"/bp/{bp_id}/route", err)
        conn.execute(
            "UPDATE bp_processes SET work_type = ?, input_nomen = ?, output_nomen = ?, "
            "volume_t = ?, comment = ? WHERE id = ? AND bp_id = ?",
            (work_type.strip(), input_nomen.strip() or None,
             output_nomen.strip() or None, vol, comment.strip() or None,
             proc_id, bp_id))
        log(conn, actor(request), bp_id, "update_process", str(proc_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route", "Процесс обновлён.")


@app.post("/bp/{bp_id}/route/processes/{proc_id}/delete")
def delete_process(request: Request, bp_id: int, proc_id: int):
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp and workflow.can_edit_section("route", role, bp["status"]):
        conn.execute("DELETE FROM bp_processes WHERE id = ? AND bp_id = ?",
                     (proc_id, bp_id))
        conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}/route")


@app.post("/bp/{bp_id}/route/positions")
async def save_positions(request: Request, bp_id: int):
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    role = current_role(request)
    if bp is None or not workflow.can_edit_section("route", role, bp["status"]):
        conn.close()
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    for node_id, pos in data.items():
        conn.execute("UPDATE bp_nodes SET x = ?, y = ? WHERE id = ? AND bp_id = ?",
                     (pos.get("x"), pos.get("y"), int(node_id), bp_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────── Workflow ───────────────────
@app.post("/bp/{bp_id}/status")
def change_status(request: Request, bp_id: int, action: str = Form(...),
                  comment: str = Form("")):
    role = current_role(request)
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    key = (bp["status"], action)
    if key not in workflow.TRANSITIONS or role not in workflow.TRANSITIONS[key][1]:
        conn.close()
        return back(request, bp_id, "Действие недоступно для вашей роли.")

    if action == "to_review":
        problems = []
        if not items:
            problems.append("нет позиций лота")
        if not bp["lot_cost"]:
            problems.append("не указана стоимость лота")
        if any(not it["sale_price"] for it in items):
            problems.append("не у всех позиций есть цена реализации")
        if problems:
            conn.close()
            return back(request, bp_id, "Нельзя отправить: " + "; ".join(problems) + ".")

    new_status = workflow.TRANSITIONS[key][0]
    new_version = bp["version"] + (1 if bp["status"] == "Доработка" else 0)

    if action == "to_review":
        risks = conn.execute("SELECT * FROM bp_risks WHERE bp_id = ?", (bp_id,)).fetchall()
        snapshot = {
            "bp": {k: bp[k] for k in bp.keys()},
            "items": [dict(it) for it in items],
            "costs": [dict(c) for c in costs],
            "risks": [dict(r) for r in risks],
            "pnl": {k: v for k, v in calc.pnl(bp, items, costs, conn).items()
                    if k not in ("rows", "capital_schedule")},
        }
        conn.execute(
            "INSERT INTO bp_versions (bp_id, version, snapshot_json, author) "
            "VALUES (?, ?, ?, ?)",
            (bp_id, new_version, json.dumps(snapshot, ensure_ascii=False, default=str),
             workflow.ROLES[role]))

    conn.execute(
        "UPDATE business_plans SET status = ?, version = ?, updated_at = datetime('now') "
        "WHERE id = ?", (new_status, new_version, bp_id))
    if action == "approve":   # дата согласования проставляется автоматически
        conn.execute("UPDATE business_plans SET approved_date = date('now') "
                     "WHERE id = ? AND approved_date IS NULL", (bp_id,))
    conn.execute(
        "INSERT INTO bp_approvals (bp_id, role, user_name, action, comment) "
        "VALUES (?, ?, ?, ?, ?)",
        (bp_id, workflow.ROLES[role], workflow.ROLES[role],
         f"{bp['status']} → {new_status}", comment or None))
    log(conn, actor(request), bp_id, "status", f"{bp['status']} → {new_status}")
    conn.commit()
    conn.close()
    return back(request, bp_id, f"Статус: {new_status}.")


# ─────────────────────────────────────── Выгрузка документов ────────
@app.get("/bp/{bp_id}/export", response_class=HTMLResponse)
def export_bp(request: Request, bp_id: int):
    conn = connect()
    bp, items, costs = load_bp(conn, bp_id)
    if bp is None:
        conn.close()
        return redirect("/", "БП не найден.")
    risks = conn.execute("SELECT * FROM bp_risks WHERE bp_id = ?", (bp_id,)).fetchall()
    edges = conn.execute(
        "SELECT e.*, a.label AS from_label, b.label AS to_label FROM bp_edges e "
        "JOIN bp_nodes a ON a.id = e.from_node JOIN bp_nodes b ON b.id = e.to_node "
        "WHERE e.bp_id = ?", (bp_id,)).fetchall()
    processes = conn.execute(
        "SELECT p.*, n.label AS node_label FROM bp_processes p "
        "JOIN bp_nodes n ON n.id = p.node_id WHERE p.bp_id = ?", (bp_id,)).fetchall()
    # Основной пакет — по активному варианту расчёта; при наличии второго
    # варианта дополнительно формируются его БП xlsx и docx (с пометкой).
    variant = resolve_variant(request, bp)
    bp_v, items_v, costs_v = calc.apply_variant(bp, items, costs, variant)
    pnl = calc.pnl(bp_v, items_v, costs_v, conn)
    risk_integral = calc.integral_risk(risks)
    sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id = ?",
                              (bp_id,)).fetchall()
    schedule = calc.removal_schedule(bp_v, items_v, sched_rows, conn)

    # Предупреждение о пустых выгрузках: без графа маршрута таблицы
    # транспорта и переработки уходят в 1С пустыми, и заметить это можно
    # было только открыв файл.
    warnings: list[str] = []
    if not edges:
        warnings.append("Граф маршрута не построен — таблицы «план_транспорт» "
                        "и «план_переработка» уйдут в 1С пустыми. Постройте "
                        "маршрут на странице «Маршрут».")
    elif not processes and any(
            (it["purchase_type"] or "") != (it["sale_type"] or "")
            for it in items):
        warnings.append("В лоте есть позиции со сменой типа (труба продаётся "
                        "ломом), но процессы переработки не заведены — "
                        "перестройте граф маршрута.")
    if not any(it["nomen_1c"] for it in items):
        warnings.append("Позиции не сопоставлены с номенклатурой 1С — "
                        "выгрузка не примется.")

    out_dir = OUTPUT_DIR / bp["bp_number"]
    out_dir.mkdir(parents=True, exist_ok=True)
    # Печатная форма — по УТВЕРЖДЁННОЙ версии, если она назначена: надпись
    # «версия N» в шапке означает согласованный расчёт, а не состояние базы
    # в момент печати. Остальные документы (xlsx, docx, выгрузки) — рабочие,
    # они всегда по текущим данным.
    approved = conn.execute(
        "SELECT version, snapshot_json FROM bp_versions "
        "WHERE bp_id = ? AND approved_at IS NOT NULL", (bp_id,)).fetchone()
    pdf_bp, pdf_items, pdf_costs, pdf_label = bp, items, costs, None
    if approved:
        try:
            snap = json.loads(approved["snapshot_json"])
            pdf_bp, pdf_items, pdf_costs = snap["bp"], snap["items"], snap["costs"]
            pdf_label = f"версия {approved['version']} (утв.)"
        except (ValueError, KeyError, TypeError):
            warnings.append(f"Снимок утверждённой версии {approved['version']} "
                            "не прочитался — печатная форма сформирована по "
                            "текущим данным.")

    def make_pdf(code: str, suffix: str = "") -> Path:
        b, i, c = calc.apply_variant(pdf_bp, pdf_items, pdf_costs, code)
        return build_bp_pdf(
            b, i, c, calc.pnl(b, i, c, conn),
            out_dir / f"{bp['bp_number']}_печатная форма{suffix}.pdf",
            variant_label=calc.VARIANTS[code], version_label=pdf_label)

    files = [
        build_bp_xlsx(bp_v, items_v, costs_v, pnl,
                      out_dir / f"{bp['bp_number']}_бизнес-план.xlsx",
                      schedule=schedule),
        build_bp_docx(bp_v, items_v, costs_v, risks, pnl, risk_integral,
                      out_dir / f"{bp['bp_number']}_бизнес-план.docx"),
        make_pdf(variant),
        *build_upload_tables(bp_v, items_v, pnl, costs_v, edges, processes,
                             out_dir),
    ]
    if bp["has_luk"]:
        other = "bsp" if variant == "luk" else "luk"
        bp_o, items_o, costs_o = calc.apply_variant(bp, items, costs, other)
        pnl_o = calc.pnl(bp_o, items_o, costs_o, conn)
        label = calc.VARIANTS[other]
        files.append(build_bp_xlsx(
            bp_o, items_o, costs_o, pnl_o,
            out_dir / f"{bp['bp_number']}_бизнес-план ({label}).xlsx",
            schedule=calc.removal_schedule(bp_o, items_o, sched_rows, conn)))
        files.append(make_pdf(other, f" ({label})"))
    log(conn, actor(request), bp_id, "export", ", ".join(f.name for f in files))
    conn.commit()
    ctx = {**base_ctx(request, conn), "bp": bp, "files": [f.name for f in files],
           "export_warnings": warnings}
    conn.close()
    return templates.TemplateResponse(request, "export.html", ctx)


@app.get("/bp/{bp_id}/download/{filename}")
def download(bp_id: int, filename: str):
    conn = connect()
    bp = conn.execute("SELECT bp_number FROM business_plans WHERE id = ?",
                      (bp_id,)).fetchone()
    conn.close()
    if bp is None:
        return redirect("/", "БП не найден.")
    path = (OUTPUT_DIR / bp["bp_number"] / filename).resolve()
    if not path.is_file() or OUTPUT_DIR.resolve() not in path.parents:
        return back(request, bp_id, "Файл не найден. Сформируйте документы заново.")
    return FileResponse(path, filename=path.name)


def last_1c_pull() -> dict | None:
    """Итог последней ночной выгрузки из Extractor (пишет pull_1c.py в 1c/)."""
    try:
        return json.loads((BASE_DIR / "1c" / "_last_pull.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ─────────────────────────────────────── Справочники ────────────────
@app.get("/references", response_class=HTMLResponse)
def references(request: Request):
    q = (request.query_params.get("q") or "").strip()
    like = f"%{q}%"
    try:
        cp_page = max(0, int(request.query_params.get("cp_page", 0)))
    except ValueError:
        cp_page = 0
    conn = connect()
    ctx = {
        **base_ctx(request, conn),
        "q": q,
        "nomenclature": conn.execute("SELECT * FROM nomenclature ORDER BY purchase_type, "
                                     "name").fetchall(),
        "nomen_groups": conn.execute(
            "SELECT *, COALESCE(parent_name, '— корневая —') AS parent_group "
            "FROM ref_nomen_groups ORDER BY parent_group, name").fetchall(),
        "counterparties": conn.execute(
            "SELECT * FROM ref_counterparties WHERE lower_ru(name) LIKE lower_ru(?) "
            "ORDER BY name LIMIT 50 OFFSET ?", (like, cp_page * 50)).fetchall(),
        "counterparties_total": conn.execute(
            "SELECT COUNT(*) AS c FROM ref_counterparties "
            "WHERE lower_ru(name) LIKE lower_ru(?)", (like,)).fetchone()["c"],
        "cp_page": cp_page,
        "warehouses": conn.execute(
            "SELECT *, COALESCE(parent_name, 'Без подразделения') AS parent_group "
            "FROM ref_warehouses WHERE is_group = 0 AND name LIKE ? "
            "ORDER BY parent_group, name LIMIT 50", (like,)).fetchall(),
        "warehouses_total": conn.execute(
            "SELECT COUNT(*) AS c FROM ref_warehouses WHERE is_group = 0").fetchone()["c"],
        "warehouse_groups": conn.execute(
            "SELECT top_parent, COUNT(*) AS c FROM ref_warehouses WHERE is_group = 0 "
            "GROUP BY top_parent ORDER BY c DESC LIMIT 15").fetchall(),
        "divisions": conn.execute(
            "SELECT * FROM ref_divisions WHERE name LIKE ? ORDER BY name LIMIT 50",
            (like,)).fetchall(),
        "divisions_total": conn.execute(
            "SELECT COUNT(*) AS c FROM ref_divisions").fetchone()["c"],
        "work_types": conn.execute("SELECT * FROM ref_work_types ORDER BY name").fetchall(),
        # Типы бизнес-планов: справочник, порог доминирования и сколько
        # сделок каждого типа уже заведено (видно, что типизация работает).
        "bp_types_list": bp_types.all_types(conn),
        "bp_type_names": {t["code"]: t["name"] for t in bp_types.all_types(conn)},
        "bp_type_threshold": bp_types.threshold(conn),
        "bp_types_usage": {r["bp_type"]: r["n"] for r in conn.execute(
            "SELECT bp_type, COUNT(*) AS n FROM business_plans "
            "WHERE bp_type IS NOT NULL GROUP BY bp_type")},
        # Матрица фактических затрат: тип сделки → статья → руб/тн.
        "cost_matrix": cost_matrix.all_rows(conn),
        "cost_matrix_from": cost_matrix.period_from(conn),
        # Факт затрат по сделкам из регистра 1С и справочник соответствия статей.
        "fact_costs_summary": fact_costs.summary(conn),
        "transport_km": transport_km.rows(conn),
        "cost_item_map": conn.execute(
            "SELECT * FROM ref_cost_item_map ORDER BY section IS NULL, section, item, item_1c").fetchall(),
        "cost_item_map_usage": {r["item_1c"]: (r["amount"], r["n"]) for r in conn.execute(
            "SELECT item_1c, SUM(amount) AS amount, COUNT(DISTINCT series) AS n "
            "FROM stat_fact_costs GROUP BY item_1c")},
        # Фактическая рентабельность по типам сделок (сборка «Реализации»).
        "type_margins": type_margin.rows(conn),
        "type_plans": {r["bp_type"]: r for r in book_archive.type_rows(conn)},
        "outcome_accuracy_rows": outcome.accuracy(conn),
        "outcome_train_n": (conn.execute("SELECT COUNT(*) AS n FROM stat_outcome_train").fetchone() or {"n": 0})["n"],
        "type_margin_closed_pct": type_margin.closed_pct(conn),
        "neighbor_fact": os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "out", "sales_data.json"),
        "cost_matrix_updated": (conn.execute(
            "SELECT MAX(updated_at) AS u FROM stat_cost_matrix").fetchone()
            or {"u": None})["u"],
        # Техника, фактическая загрузка рейсов и категории груза (нормы погрузки).
        "vehicle_types": loading.vehicle_types(conn),
        "vehicles_total": conn.execute(
            "SELECT COUNT(*) AS c FROM ref_vehicles").fetchone()["c"],
        "cargo_categories": loading.categories(conn),
        "load_stats": loading.load_stats(conn),
        # Состояние справочников: когда обновлялся каждый и кто отвечает.
        "ref_state": refsources.state(conn),
        "ref_summary": refsources.summary(conn),
        "last_pull": last_1c_pull(),
        "can_edit_sources": current_role(request) in ("economist", "director", "admin"),
        "settings": conn.execute("SELECT * FROM settings ORDER BY key").fetchall(),
        "norms": conn.execute(
            "SELECT * FROM cost_norms ORDER BY base IS NOT NULL, base, label").fetchall(),
        "norm_facts": norm_calib.for_norms(conn),
        "norm_fact_deviation": norm_calib.DEVIATION_PCT,
        "norms_updated_at": (conn.execute(
            "SELECT value FROM settings WHERE key = 'norms_updated_at'").fetchone()
            or {"value": None})["value"],
        # Коэффициенты по номенклатуре (резка штанги/НКТ) и тарифы погрузки.
        "nomen_factors": conn.execute(
            "SELECT * FROM norm_nomen_factors ORDER BY key, pattern").fetchall(),
        "loading_tariffs": conn.execute(
            "SELECT * FROM loading_tariffs ORDER BY place").fetchall(),
        "seasonal_rules": conn.execute(
            "SELECT * FROM seasonal_access ORDER BY pattern").fetchall(),
        # Транспортные ставки перевозчика из приказа 1С.
        "transport_rates": rates.summary(conn),
        "transport_rates_list": rates.current_rates(conn),
        # Производственные подразделения 1С (цех → база) и роли пунктов.
        "prod_units": conn.execute(
            "SELECT * FROM ref_prod_units ORDER BY "
            "COALESCE(NULLIF(analytic_base, ''), NULLIF(functional_base, ''), "
            "'яя'), name").fetchall(),
        "point_roles": conn.execute(
            "SELECT * FROM shipping_points WHERE kind IS NOT NULL "
            "ORDER BY base_name, kind DESC, name").fetchall(),
        # Обоснование ставки распределяемых расходов фактическими данными 1С.
        "overhead": norms.overhead_rate(conn),
        "overhead_bases": fact_costs.base_overhead_rates(
            conn, os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "out", "sales_data.json")),
    }
    conn.close()
    return templates.TemplateResponse(request, "references.html", ctx)


@app.post("/references/norms/calibrate")
def calibrate_norms(request: Request):
    """Пересчитать фактические значения нормативов из загруженных регистров."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Калибровку запускает экономист.")
    conn = connect()
    res = norm_calib.build(conn)
    log(conn, actor(request), None, "norm_calib", f"{res['written']} фактов")
    conn.commit()
    conn.close()
    return redirect("/references#ref-norms",
                    f"Факт по нормативам пересчитан: {res['written']} значений с {res['since']}.")


@app.post("/references/norms/refresh")
def refresh_norms(request: Request):
    """Обновление нормативов: дозагрузка недостающих значений (существующие
    не трогаются) + фиксация даты последней загрузки."""
    role = current_role(request)
    if role not in ("economist", "admin"):
        return redirect("/references", "Нормативы обновляет экономист или администратор.")
    conn = connect()
    norms.seed_norms(conn)
    conn.execute(
        "INSERT INTO settings (key, value, comment) VALUES ('norms_updated_at', "
        "datetime('now', 'localtime'), 'Дата последней загрузки нормативов.') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    log(conn, actor(request), None, "refresh_norms", "")
    conn.commit()
    conn.close()
    return redirect("/references", "Нормативы обновлены, дата загрузки зафиксирована.")


@app.post("/references/norms/{norm_id}")
def update_norm(request: Request, norm_id: int, value: str = Form(...)):
    role = current_role(request)
    if role not in ("economist", "admin"):
        return redirect("/references", "Нормативы меняет экономист или администратор.")
    val, err = single("value", value, required=True)
    if err:
        return redirect("/references", err)
    conn = connect()
    conn.execute("UPDATE cost_norms SET value = ? WHERE id = ?", (val, norm_id))
    log(conn, actor(request), None, "update_norm", f"id={norm_id} → {val}")
    conn.commit()
    conn.close()
    return redirect("/references", "Норматив обновлён.")


def _norm_editor(request: Request) -> bool:
    return current_role(request) in ("economist", "admin")


@app.post("/references/sources/{key}")
def update_ref_source(request: Request, key: str, owner: str = Form(""),
                      period_days: str = Form(""), comment: str = Form("")):
    """Владелец справочника и ожидаемая периодичность обновления."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Назначать владельца может экономист.")
    check = forms.Form({"period_days": period_days})
    days = check.num("period_days")
    if not check.ok:
        return redirect("/references#ref-sources", check.message())
    conn = connect()
    refsources.set_meta(conn, key, owner.strip(), days, comment.strip())
    log(conn, actor(request), None, "ref_source", f"{key}: владелец {owner.strip()}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-sources", "Справочник сохранён.")


@app.post("/references/cargo-categories/{cat_id}")
def update_cargo_category(request: Request, cat_id: int, name: str = Form(...),
                          match_words: str = Form(""), density: str = Form("0"),
                          source: str = Form(""), comment: str = Form("")):
    """Правка категории груза: слова распознавания и насыпная плотность.

    Плотность решает, упрётся рейс в тоннаж или в кубатуру, поэтому у
    каждого значения обязателен источник — на типовое значение погрузку
    перед проверкой не обоснуешь.
    """
    role = current_role(request)
    if role not in ("logist", "economist", "director", "admin"):
        return redirect("/references", "Править нормы погрузки может логист.")
    check = forms.Form({"density": density})
    value = check.num("density")
    if not check.ok or not name.strip():
        return redirect("/references#ref-cargo",
                        check.message() if not check.ok
                        else "Название категории не может быть пустым.")
    conn = connect()
    conn.execute(
        "UPDATE ref_cargo_categories SET name = ?, match_words = ?, "
        "density_t_m3 = ?, source = ?, comment = ? WHERE id = ?",
        (name.strip(), match_words.strip(), value or 0.0, source.strip() or None,
         comment.strip() or None, cat_id))
    log(conn, actor(request), None, "ref_cargo", f"{cat_id}: {name.strip()}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-cargo", "Категория груза сохранена.")


@app.post("/references/cost-matrix/rebuild")
def rebuild_cost_matrix(request: Request):
    """Пересборка матрицы затрат по расчётам сервиса и выгрузкам 1С."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Пересобирать матрицу может экономист.")
    conn = connect()
    result = cost_matrix.build(conn)
    log(conn, actor(request), None, "cost_matrix",
        f"{result['rows']} строк из {result['observations']} наблюдений")
    conn.commit()
    conn.close()
    return redirect("/references#ref-cost-matrix",
                    f"Матрица пересобрана: {result['rows']} строк "
                    f"из {result['observations']} наблюдений с {result['since']}.")


@app.post("/references/book-archive/rebuild")
def rebuild_book_archive(request: Request):
    """Пересборка «что закладывали» из архива книг (парсер Битрикса)."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Пересобирать архив книг может экономист.")
    folder = os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "data")
    path = book_archive.newest(folder)
    if path is None:
        return redirect("/references#ref-type-margin", "Архив книг не найден: нет БП_версии_*.json у соседа.")
    snap = os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "out", "sales_data.json")
    conn = connect()
    res = book_archive.build(conn, path, snap if os.path.isfile(snap) else None)
    log(conn, actor(request), None, "book_archive", f"{res['versions']} версий, {res['deals']} сделок")
    conn.commit()
    conn.close()
    return redirect("/references#ref-type-margin",
                    f"Архив книг пересобран: {res['versions']} версий по {res['deals']} сделкам ({path.name}).")


@app.post("/references/type-margin/rebuild")
def rebuild_type_margin(request: Request):
    """Пересборка фактической рентабельности по типам из снимка «Реализации»."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Пересобирать факт по типам может экономист.")
    path = os.path.join(os.environ.get("BP_NEIGHBOR_DIR", "/neighbor"), "out", "sales_data.json")
    if not os.path.isfile(path):
        return redirect("/references#ref-type-margin",
                        "Снимок «Реализации» недоступен: нет файла out/sales_data.json.")
    conn = connect()
    result = type_margin.build(conn, path)
    log(conn, actor(request), None, "type_margin",
        f"{result['typed']} типизированных из {result['closed']} закрытых сделок")
    conn.commit()
    conn.close()
    return redirect("/references#ref-type-margin",
                    f"Факт по типам пересобран: сделок с фактом {result['bps']}, закрытых "
                    f"{result['closed']}, с определённым типом {result['typed']} "
                    f"(сборка {result['generated']}).")


@app.post("/references/cost-item-map")
def update_cost_item_map(request: Request, item_1c: str = Form(...), section: str = Form(""),
                         item: str = Form(""), is_fact_only: str = Form("0")):
    """Соответствие статьи 1С статье сервиса. Пустая секция и статья —
    статья не относится к экономике сделки. После правки матрица
    пересобирается ночью; кнопка пересборки — в карточке матрицы."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Править соответствие статей может экономист.")
    conn = connect()
    key = item_1c.strip()
    if not key:
        conn.close()
        return redirect("/references#ref-fact-costs", "Статья 1С не может быть пустой.")
    sec, it = section.strip() or None, item.strip() or None
    if (sec is None) != (it is None):
        conn.close()
        return redirect("/references#ref-fact-costs",
                        "Секция и статья задаются вместе; обе пустые — статья не относится к сделке.")
    conn.execute(
        "INSERT INTO ref_cost_item_map (item_1c, section, item, is_fact_only, note, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(item_1c) DO UPDATE SET "
        "section = excluded.section, item = excluded.item, is_fact_only = excluded.is_fact_only, "
        "note = excluded.note, updated_at = excluded.updated_at",
        (key, sec, it, 1 if is_fact_only == "1" else 0, f"правка: {actor(request)}"))
    # Перепривязать уже загруженный факт, чтобы не ждать ночи.
    conn.execute("UPDATE stat_fact_costs SET section = ?, item = ? WHERE item_1c = ? "
                 "OR (item_1c LIKE ? || '%' AND NOT EXISTS (SELECT 1 FROM ref_cost_item_map m "
                 "WHERE m.item_1c = stat_fact_costs.item_1c))", (sec, it, key, key))
    log(conn, actor(request), None, "cost_item_map", f"{key} -> {sec} / {it}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-fact-costs", f"Соответствие для «{key}» сохранено.")


@app.post("/references/bp-types/{type_id}")
def update_bp_type(request: Request, type_id: int, name: str = Form(...),
                   item_types: str = Form(""), cost_items: str = Form(""),
                   processes: str = Form(""), required_roles: str = Form("")):
    """Правка типа сделки: название, какие позиции к нему относятся, что
    характерно по затратам, процессам и чьё участие обязательно."""
    role = current_role(request)
    if role not in ("economist", "director", "admin"):
        return redirect("/references", "Править справочник типов может экономист.")
    if not name.strip():
        return redirect("/references#ref-bp-types", "Название типа не может быть пустым.")
    conn = connect()
    conn.execute(
        "UPDATE ref_bp_types SET name = ?, item_types = ?, cost_items = ?, "
        "processes = ?, required_roles = ? WHERE id = ?",
        (name.strip(), item_types.strip(), cost_items.strip(), processes.strip(),
         required_roles.strip(), type_id))
    log(conn, actor(request), None, "ref_bp_type", f"{type_id}: {name.strip()}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-bp-types", "Тип сделки сохранён.")


@app.post("/references/nomen-factors")
def save_nomen_factor(request: Request, pattern: str = Form(""),
                      factor: str = Form("1"), comment: str = Form(""),
                      factor_id: str = Form(""), delete: str = Form("")):
    """Коэффициент норматива по маске номенклатуры (резка штанги/НКТ)."""
    if not _norm_editor(request):
        return redirect("/references#ref-factors",
                        "Нормативы меняет экономист или администратор.")
    conn = connect()
    role = current_role(request)
    if delete and factor_id:
        conn.execute("DELETE FROM norm_nomen_factors WHERE id = ?", (factor_id,))
        log(conn, actor(request), None, "delete_nomen_factor", factor_id)
    else:
        val, err = single("factor", factor, required=True)
        if err:
            conn.close()
            return redirect("/references#ref-factors", err)
        if factor_id:
            conn.execute("UPDATE norm_nomen_factors SET factor = ?, comment = ? "
                         "WHERE id = ?", (val, comment.strip() or None, factor_id))
        elif pattern.strip():
            conn.execute(
                "INSERT INTO norm_nomen_factors (pattern, key, factor, comment) "
                "VALUES (?, 'cut_factor', ?, ?) "
                "ON CONFLICT(pattern, key) DO UPDATE SET factor = excluded.factor, "
                "comment = excluded.comment",
                (pattern.strip().lower(), val, comment.strip() or None))
        log(conn, actor(request), None, "save_nomen_factor", f"{pattern} → {val}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-factors", "Коэффициенты по номенклатуре сохранены.")


@app.post("/references/transport-rates")
async def transport_rates_preview(request: Request, file: UploadFile):
    """Разбор выгрузки транспортных ставок 1С и предпросмотр перед записью.

    Ничего не пишет: показывает документы, классификацию (рабочий прайс /
    повышение «на согласовании») и все строки. Запись — следующий шаг
    после подтверждения, справочник заменяется целиком."""
    if not _norm_editor(request):
        return redirect("/references#ref-transport",
                        "Ставки загружает экономист или администратор.")
    fname = upload_name(file.filename, "ставки.csv")
    if not fname.lower().endswith(".csv"):
        return redirect("/references#ref-transport",
                        "Нужна выгрузка 1С в csv (УстановкаТранспортныхСтавок…).")
    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        parsed = rates.parse_file(tmp_path)
    except (ValueError, UnicodeDecodeError, csv.Error) as exc:
        return redirect("/references#ref-transport",
                        f"Файл не разобран: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)
    payload = base64.b64encode(json.dumps(
        parsed["rows"], ensure_ascii=False).encode("utf-8")).decode("ascii")
    conn = connect()
    ctx = {**base_ctx(request, conn), "file_name": fname, "parsed": parsed,
           "payload": payload, "existing": rates.summary(conn)}
    log(conn, actor(request), None, "transport_rates_preview",
        f"{fname}: {len(parsed['rows'])} строк")
    conn.commit()
    conn.close()
    return templates.TemplateResponse(request, "rates_import.html", ctx)


@app.post("/references/transport-rates/apply")
def transport_rates_apply(request: Request, payload: str = Form(""),
                          file_name: str = Form("")):
    """Запись подтверждённых ставок: полная замена справочника."""
    if not _norm_editor(request):
        return redirect("/references#ref-transport",
                        "Ставки загружает экономист или администратор.")
    try:
        rows = json.loads(base64.b64decode(payload))
        if not isinstance(rows, list) or not rows:
            raise ValueError
    except (ValueError, TypeError):
        return redirect("/references#ref-transport",
                        "Не сохранено: данные предпросмотра не прочитались — "
                        "загрузите файл заново.")
    conn = connect()
    n = rates.replace_all(conn, rows, file_name or "выгрузка 1С")
    docs = {r.get("doc_number") for r in rows}
    log(conn, actor(request), None, "transport_rates_apply",
        f"{file_name}: {n} строк, документов: {len(docs)}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-transport",
                    f"Загружено {n} ставок из {len(docs)} документов. "
                    "Прежнее содержимое справочника заменено.")


@app.post("/references/loading-tariffs")
def save_loading_tariff(request: Request, place: str = Form(""),
                        own_crane: str = Form(""), rate_per_trip: str = Form(""),
                        part_load_pct: str = Form("50"),
                        cargo_type: str = Form(""), comment: str = Form(""),
                        tariff_id: str = Form(""), delete: str = Form("")):
    """Тариф погрузки по месту вывоза (свой кран поставщика или наём)."""
    if not _norm_editor(request):
        return redirect("/references#ref-tariffs",
                        "Тарифы меняет экономист или администратор.")

    check = forms.Form({"rate_per_trip": rate_per_trip,
                        "part_load_pct": part_load_pct})
    rate = check.num("rate_per_trip")
    part = check.num("part_load_pct")
    if not check.ok:
        return redirect("/references#ref-tariffs", check.message())

    conn = connect()
    role = current_role(request)
    if delete and tariff_id:
        conn.execute("DELETE FROM loading_tariffs WHERE id = ?", (tariff_id,))
        log(conn, actor(request), None, "delete_loading_tariff", tariff_id)
    else:
        values = (1 if own_crane else 0, rate,
                  50 if part is None else part,
                  cargo_type.strip() or None, comment.strip() or None)
        if tariff_id:
            conn.execute(
                "UPDATE loading_tariffs SET own_crane = ?, rate_per_trip = ?, "
                "part_load_pct = ?, cargo_type = ?, comment = ? WHERE id = ?",
                (*values, tariff_id))
        elif place.strip():
            conn.execute(
                "INSERT INTO loading_tariffs (own_crane, rate_per_trip, "
                "part_load_pct, cargo_type, comment, place) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(place) DO UPDATE SET "
                "own_crane = excluded.own_crane, "
                "rate_per_trip = excluded.rate_per_trip, "
                "part_load_pct = excluded.part_load_pct, "
                "cargo_type = excluded.cargo_type, comment = excluded.comment",
                (*values, place.strip()))
        log(conn, actor(request), None, "save_loading_tariff", place)
    conn.commit()
    conn.close()
    return redirect("/references#ref-tariffs", "Тарифы погрузки сохранены.")


@app.post("/references/seasonal")
def save_seasonal(request: Request, pattern: str = Form(""),
                  months: str = Form(""), comment: str = Form(""),
                  rule_id: str = Form(""), delete: str = Form("")):
    """Сезонный доступ к месту вывоза: месяцы, когда туда можно заехать."""
    if not _norm_editor(request):
        return redirect("/references#ref-seasonal",
                        "Сезонность меняет экономист или администратор.")
    clean = ",".join(str(int(x)) for x in months.replace(" ", "").split(",")
                     if x.isdigit() and 1 <= int(x) <= 12)
    conn = connect()
    role = current_role(request)
    if delete and rule_id:
        conn.execute("DELETE FROM seasonal_access WHERE id = ?", (rule_id,))
        log(conn, actor(request), None, "delete_seasonal", rule_id)
    elif not clean:
        conn.close()
        return redirect("/references#ref-seasonal",
                        "Укажите месяцы через запятую, например 12,1,2,3,4.")
    elif rule_id:
        conn.execute("UPDATE seasonal_access SET months = ?, comment = ? "
                     "WHERE id = ?", (clean, comment.strip() or None, rule_id))
        log(conn, actor(request), None, "save_seasonal", f"id={rule_id} → {clean}")
    elif pattern.strip():
        conn.execute(
            "INSERT INTO seasonal_access (pattern, months, comment) "
            "VALUES (?, ?, ?) ON CONFLICT(pattern) DO UPDATE SET "
            "months = excluded.months, comment = excluded.comment",
            (pattern.strip().lower(), clean, comment.strip() or None))
        log(conn, actor(request), None, "save_seasonal", f"{pattern} → {clean}")
    conn.commit()
    conn.close()
    return redirect("/references#ref-seasonal", "Сезонность мест сохранена.")


@app.post("/references/nomenclature")
def add_nomenclature(request: Request, name: str = Form(...), category: str = Form(""),
                     purchase_type: str = Form("лом"), group_1c: str = Form(""),
                     aliases: str = Form("")):
    conn = connect()
    guid_row = conn.execute("SELECT guid FROM ref_nomen_groups WHERE name = ?",
                            (group_1c.strip(),)).fetchone()
    try:
        conn.execute(
            "INSERT INTO nomenclature (name, category, purchase_type, group_1c, "
            "group_guid, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            (name.strip(), category.strip() or None, purchase_type,
             group_1c.strip() or None, guid_row["guid"] if guid_row else None,
             aliases.strip() or None))
        log(conn, actor(request), None, "add_nomenclature", name)
        conn.commit()
        msg = "Позиция добавлена." + ("" if guid_row else
                                      " Группа 1С не найдена — привязка по GUID пуста.")
    except Exception:
        msg = "Такое наименование уже есть."
    conn.close()
    return redirect("/references", msg)


def buyers_summary(conn, bp_id: int, items: list, variant: str) -> list[dict]:
    """Кому фактически уходит лот: покупатели с объёмами и долями.

    Покупатель в «Сторонах» один — он идёт в документы и подставляется по
    умолчанию. Но объём делится между несколькими: план продажи разбивает
    позицию по покупателям (в 1935 труба уходит и на Чермет-Волжский, и на
    ВТЗ), а у позиций без разбивки покупатель задан в самой строке. Здесь
    оба источника сводятся в одну картину — иначе «с кем сделка» видно
    только вглубь, в составе лота.

    Показывается объём ЗАКУПКИ: объём реализации меньше на засор, и мешать
    их в одной таблице нельзя — цифры разойдутся с P&L.
    """
    sales = conn.execute(
        "SELECT item_id, buyer, volume_t, sale_price FROM bp_item_sales "
        "WHERE bp_id = ? AND variant = ?", (bp_id, variant)).fetchall()
    price_col = "sale_price_luk" if variant == "luk" else "sale_price"
    rows: dict[str, dict] = {}

    def add(name: str, volume: float, price, source: str, item_id) -> None:
        if volume <= 0:
            return
        key = (name or "").strip() or "не назначен"
        acc = rows.setdefault(key, {"buyer": key, "volume": 0.0, "amount": 0.0,
                                    "priced": 0.0, "rows": set(),
                                    "sources": set()})
        acc["volume"] += volume
        acc["rows"].add(item_id)
        acc["sources"].add(source)
        if price:
            acc["amount"] += volume * float(price)
            acc["priced"] += volume

    distributed: dict[int, float] = {}
    for s in sales:
        distributed[s["item_id"]] = distributed.get(s["item_id"], 0.0) + _f(s["volume_t"])
    by_item = {it["id"]: it for it in items}
    for s in sales:
        item = by_item.get(s["item_id"])
        price = s["sale_price"] or (item[price_col] if item else None) \
            or (item["sale_price"] if item else None)
        add(s["buyer"], _f(s["volume_t"]), price, "план продажи", s["item_id"])
    for it in items:
        free = _f(it["volume_t"]) - distributed.get(it["id"], 0.0)
        if free <= 0.0001:
            continue
        price = it[price_col] or it["sale_price"]
        add(it["buyer"], free, price, "покупатель позиции", it["id"])

    total = sum(r["volume"] for r in rows.values())
    out = []
    for r in sorted(rows.values(), key=lambda r: -r["volume"]):
        out.append({
            "buyer": r["buyer"],
            "volume": r["volume"],
            "share_pct": r["volume"] / total * 100 if total else 0.0,
            # Цена средневзвешенная и только по тому объёму, у которого она
            # задана: иначе позиции без цены занижали бы среднюю.
            "price": r["amount"] / r["priced"] if r["priced"] else None,
            "no_price_volume": r["volume"] - r["priced"],
            "positions": len(r["rows"]),
            "source": ", ".join(sorted(r["sources"])),
        })
    return out


def aggregate_rows(conn, bp_id: int, items: list, variant: str,
                   by_place: bool = False) -> list[dict]:
    """Свод позиций по группам: «что продаём» × «в какой группе продаём».

    В перечне может быть 500 строк одной трубы НКТ, которые уходят по двум
    планам (частью трубой, частью ломом). Заполнять 500 строк вручную
    бессмысленно — экономист задаёт цену и план продажи на свод, а сервис
    разносит их по всем позициям группы.

    by_place добавляет к ключу место вывоза: на больших лотах (1929 — 3000
    позиций) засор и цена зависят от площадки, и свод «место × группа»
    позволяет ставить их одним действием на место, а не строкам вручную.
    """
    by_item = analytic_groups_for(conn, items)
    groups: dict[tuple, dict] = {}
    for it in items:
        base_group = by_item.get(it["id"]) or "Без группы"
        sale_group = (it["sale_group"] or "").strip() or None
        place = ((it["division"] or "").strip() or (it["warehouse"] or "").strip()
                 or "Без места") if by_place else None
        key = (place, base_group,
               sale_group or (it["sale_type"] or "").strip())
        price = (it["sale_price_luk"] if variant == "luk"
                 and it["sale_price_luk"] is not None else it["sale_price"])
        cont = (it["contamination_pct_luk"] if variant == "luk"
                and it["contamination_pct_luk"] is not None
                else it["contamination_pct"])
        g = groups.setdefault(key, {
            "cargo_group": base_group, "sale_group": sale_group,
            "place": place,
            "sale_type": (it["sale_type"] or "").strip(),
            "positions": 0, "volume": 0.0, "revenue_base": 0.0,
            "priced_volume": 0.0, "item_ids": [], "prices": set(),
            "units": set(), "contaminations": set(),
        })
        vol = float(it["volume_t"] or 0)
        g["positions"] += 1
        g["volume"] += vol
        g["item_ids"].append(it["id"])
        g["units"].add((it["unit"] or "").strip() or "тн")
        if price:
            g["prices"].add(round(float(price), 2))
            g["revenue_base"] += vol * float(price)
            g["priced_volume"] += vol
        if cont is not None:
            g["contaminations"].add(round(float(cont), 2))
    out = []
    for g in groups.values():
        g["price_avg"] = (g["revenue_base"] / g["priced_volume"]
                          if g["priced_volume"] else None)
        # Одна цена на всю группу или разнобой — это видно экономисту сразу.
        g["price_uniform"] = len(g["prices"]) <= 1
        g["unit"] = ", ".join(sorted(g["units"]))
        g["no_price"] = round(g["volume"] - g["priced_volume"], 3)
        # Засор группы: одно число, диапазон или «не задан» (берётся общий).
        conts = sorted(g["contaminations"])
        g["contamination"] = (f"{conts[0]:g}" if len(conts) == 1
                              else f"{conts[0]:g}–{conts[-1]:g}" if conts
                              else None)
        out.append(g)
    out.sort(key=lambda x: ((x["place"] or ""), -x["volume"]))
    return out


# Колонки позиции, которые правятся оптом (и, значит, возвращаются отменой).
# Список закрытый: имя колонки подставляется в SQL, а payload отмены лежит в
# базе — принимать оттуда произвольное имя нельзя.
BULK_COLUMNS = {"sale_price", "sale_price_luk", "sale_group", "sale_type",
                "buyer", "cargo_group", "own_transport_pct",
                "workshop_cut_pct", "contamination_pct", "contamination_pct_luk"}


def remember_bulk(conn, bp_id: int, author: str, action: str, note: str,
                  rows: list, columns: list[str]) -> None:
    """Запомнить прежние значения строк перед массовой правкой.

    В книге протяжку формулы отменяют одним Ctrl+Z. Массовое действие здесь
    правит сотню строк разом, и без такого же возврата единственным способом
    исправить промах было бы восстановление всего БП из версии — вместе с
    правками, которые отменять никто не просил.
    """
    payload = [{"id": r["id"], **{c: r[c] for c in columns}} for r in rows]
    conn.execute(
        "INSERT INTO bp_bulk_undo (bp_id, author, action, note, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (bp_id, author, action, note, json.dumps(payload, ensure_ascii=False)))
    # Это отмена последнего действия, а не история: снимки старше двадцатого
    # уже никто не откатит, а места они занимают столько же.
    conn.execute(
        "DELETE FROM bp_bulk_undo WHERE bp_id = ? AND id NOT IN "
        "(SELECT id FROM bp_bulk_undo WHERE bp_id = ? ORDER BY id DESC LIMIT 20)",
        (bp_id, bp_id))


@app.post("/bp/{bp_id}/items/bulk")
async def bulk_edit_items(request: Request, bp_id: int):
    """Цена, план продажи и покупатель — сразу всем выделенным позициям.

    Пустое поле не меняет ничего: форма правит только то, что заполнено, —
    иначе «задать цену» заодно стирало бы покупателей.
    """
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    variant = resolve_variant(request, bp)
    form = await read_form(request)
    stale = edit_conflict(conn, request, bp, form)
    if stale:
        conn.close()
        return back(request, bp_id, stale)

    picked = {int(x) for x in form.raw("item_ids").split(",")
              if x.strip().isdigit()}
    # Только позиции ЭТОГО БП: список идентификаторов приходит из браузера.
    rows = [it for it in items if it["id"] in picked]
    price = form.num("sale_price", field="sale_price")
    sale_group = form.text("sale_group")
    buyer = form.text("buyer")
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message(), "lot")
    if not rows:
        conn.close()
        return back(request, bp_id,
                    "Не сохранено. Выделите строки флажками слева, "
                    "потом задайте значение.", "lot")

    columns: list[str] = []
    values: list = []
    changed: list[str] = []
    if price is not None:
        column = "sale_price_luk" if variant == "luk" else "sale_price"
        columns.append(column)
        values.append(price)
        changed.append(f"цена {price:g} руб/тн")
    if sale_group:
        # Тип продажи выводится из плана: иначе НДС и засор разойдутся с ним.
        columns += ["sale_group", "sale_type"]
        values += [sale_group, calc.type_of_group(sale_group)]
        changed.append(f"план продажи «{sale_group}»")
    if buyer:
        columns.append("buyer")
        values.append(buyer)
        changed.append(f"покупатель «{buyer}»")
    if not columns:
        conn.close()
        return back(request, bp_id,
                    "Не сохранено. Заполните хотя бы одно поле массовой правки: "
                    "цену, план продажи или покупателя.", "lot")

    note = f"{'; '.join(changed)} — {len(rows)} поз."
    remember_bulk(conn, bp_id, actor_name(request), "bulk", note, rows, columns)
    assign = ", ".join(f"{c} = ?" for c in columns)
    for row in rows:
        conn.execute(f"UPDATE bp_items SET {assign} WHERE id = ? AND bp_id = ?",
                     (*values, row["id"], bp_id))
    log(conn, actor(request), bp_id, "bulk_items", note)
    conn.commit()
    conn.close()
    return back(request, bp_id, f"Массовая правка: {note}. "
                f"Отменить можно кнопкой над таблицей.", "lot")


@app.post("/bp/{bp_id}/items/bulk/undo")
def undo_bulk_edit(request: Request, bp_id: int):
    """Возврат последнего массового действия по позициям."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    row = conn.execute(
        "SELECT * FROM bp_bulk_undo WHERE bp_id = ? ORDER BY id DESC LIMIT 1",
        (bp_id,)).fetchone()
    if row is None:
        conn.close()
        return back(request, bp_id, "Отменять нечего.", "lot")
    restored = 0
    for entry in json.loads(row["payload"]):
        columns = [c for c in entry if c != "id" and c in BULK_COLUMNS]
        if not columns:
            continue
        assign = ", ".join(f"{c} = ?" for c in columns)
        cur = conn.execute(
            f"UPDATE bp_items SET {assign} WHERE id = ? AND bp_id = ?",
            (*[entry[c] for c in columns], entry["id"], bp_id))
        restored += cur.rowcount
    conn.execute("DELETE FROM bp_bulk_undo WHERE id = ?", (row["id"],))
    log(conn, actor(request), bp_id, "bulk_undo", row["note"] or "")
    conn.commit()
    conn.close()
    return back(request, bp_id,
                f"Отменено: {row['note']}. Восстановлено позиций: {restored}.",
                "lot")


@app.post("/bp/{bp_id}/items/aggregate")
async def apply_aggregate(request: Request, bp_id: int):
    """Массовое заполнение по своду: цена и план продажи разносятся на все
    позиции группы. Так 500 строк одной номенклатуры заполняются одной."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    variant = resolve_variant(request, bp)
    form = await read_form(request)
    ids = [int(x) for x in form.raw("item_ids").split(",") if x.strip().isdigit()]
    if not ids:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}", "Группа не найдена.")
    price = form.num("price", field="sale_price")
    sale_group = form.text("sale_group")
    contamination = form.num("contamination", field="contamination_pct")
    if not form.ok:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}", form.message())
    marks = ",".join("?" for _ in ids)
    changed, columns = [], []
    if price is not None:
        columns.append("sale_price_luk" if variant == "luk" else "sale_price")
        changed.append(f"цена {price:g} руб")
    if sale_group:
        columns += ["sale_group", "sale_type"]
        changed.append(f"план продажи «{sale_group}»")
    if contamination is not None:
        # Засор — вариантный, как в форме цен: пишется в тот вариант,
        # который открыт в карточке.
        columns.append("contamination_pct_luk" if variant == "luk"
                       else "contamination_pct")
        changed.append(f"засор {contamination:g}%")
    if not changed:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}",
                        "Укажите цену, план продажи или засор для свода.")
    # Заполнение по своду — такое же массовое действие, как правка выделенных
    # строк, и отменяется той же кнопкой.
    note = f"свод: {'; '.join(changed)} — {len(ids)} поз."
    remember_bulk(conn, bp_id, actor_name(request), "aggregate", note,
                  [it for it in items if it["id"] in set(ids)], columns)
    if price is not None:
        column = "sale_price_luk" if variant == "luk" else "sale_price"
        conn.execute(f"UPDATE bp_items SET {column} = ? WHERE bp_id = ? "
                     f"AND id IN ({marks})", (price, bp_id, *ids))
    if sale_group:
        conn.execute(f"UPDATE bp_items SET sale_group = ?, sale_type = ? "
                     f"WHERE bp_id = ? AND id IN ({marks})",
                     (sale_group, calc.type_of_group(sale_group), bp_id, *ids))
    if contamination is not None:
        column = ("contamination_pct_luk" if variant == "luk"
                  else "contamination_pct")
        conn.execute(f"UPDATE bp_items SET {column} = ? WHERE bp_id = ? "
                     f"AND id IN ({marks})", (contamination, bp_id, *ids))
    log(conn, actor(request), bp_id, "apply_aggregate",
        f"{len(ids)} позиций: {'; '.join(changed)}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant={variant}",
                    f"Заполнено по своду: {len(ids)} позиций — {'; '.join(changed)}.")


def fill_schedule(conn, bp_id: int, bp, items) -> int:
    """Разложить объём по месяцам вывоза. Вызывается и кнопкой, и импортом:
    пустой график — это незаполненный обязательный раздел БП, а экономист не
    обязан догадываться, что нужно нажать кнопку."""
    sched_rows = conn.execute("SELECT * FROM bp_schedule WHERE bp_id = ?",
                              (bp_id,)).fetchall()
    bp_v, items_v, _ = calc.apply_variant(bp, items, [], "bsp")
    sched = calc.removal_schedule(bp_v, items_v, sched_rows, conn)
    for b in sched["bases"]:
        open_months = [r["month"] for r in b["rows"] if r["available"]] or \
                      [r["month"] for r in b["rows"]]
        share = b["volume"] / len(open_months) if open_months else 0.0
        for m in range(1, sched["months"] + 1):
            conn.execute(
                "INSERT INTO bp_schedule (bp_id, base, month, shipment_t) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(bp_id, base, month) DO UPDATE "
                "SET shipment_t = excluded.shipment_t",
                (bp_id, b["base"], m, round(share, 3) if m in open_months else 0.0))
    return len(sched["bases"])


def autofill_items(conn, bp_id: int) -> dict[str, int]:
    """Автозаполнение полей позиций из уже известных данных.

    Одно и то же значение живёт в нескольких таблицах, и вводить его руками
    повторно незачем: единица измерения и группа есть в справочнике 1С после
    сопоставления, расстояние — в реестре пунктов отгрузки и в маршрутах,
    покупатель — в плане продажи. Заполняются ТОЛЬКО пустые поля: то, что
    экономист ввёл руками, не перетирается.
    """
    filled = {"unit": 0, "cargo_group": 0, "distance_km": 0, "buyer": 0,
              "seller_code": 0}
    items = conn.execute("SELECT * FROM bp_items WHERE bp_id = ?",
                         (bp_id,)).fetchall()
    for it in items:
        sets: dict[str, object] = {}
        # 1С-номенклатура даёт единицу измерения и группу аналитического учёта.
        ref = conn.execute(
            "SELECT unit, cargo_group FROM ref_nomenclature_1c WHERE guid = ?",
            (it["nomen_1c_guid"],)).fetchone() if it["nomen_1c_guid"] else None
        if ref:
            if not (it["unit"] or "").strip() and ref["unit"]:
                sets["unit"] = ref["unit"]
            if not (it["cargo_group"] or "").strip() and ref["cargo_group"]:
                sets["cargo_group"] = ref["cargo_group"]
        # Расстояние — из реестра пунктов отгрузки (перечень продавца) или из
        # сохранённого плеча маршрута до базы.
        if it["distance_km"] is None:
            place = (it["division"] or it["warehouse"] or "").strip()
            if place:
                point = conn.execute(
                    "SELECT id, distance_km FROM shipping_points "
                    "WHERE name_norm = ?", (logistics.norm_name(place),)).fetchone()
                km = point["distance_km"] if point else None
                if km is None and point:
                    route = conn.execute(
                        "SELECT distance_km FROM shipping_routes "
                        "WHERE from_point = ? AND distance_km IS NOT NULL "
                        "ORDER BY id LIMIT 1", (point["id"],)).fetchone()
                    km = route["distance_km"] if route else None
                if km is not None:
                    sets["distance_km"] = km
        # Код продавца — из истории сопоставлений по той же номенклатуре.
        if not (it["seller_code"] or "").strip() and it["nomen_1c"]:
            hist = conn.execute(
                "SELECT seller_code FROM nomen_match_history "
                "WHERE nomen_1c = ? AND seller_code IS NOT NULL "
                "ORDER BY uses DESC LIMIT 1", (it["nomen_1c"],)).fetchone()
            if hist:
                sets["seller_code"] = hist["seller_code"]
        # Покупатель — из плана продажи позиции (если он там один).
        if not (it["buyer"] or "").strip():
            # Только строки базового варианта: bp_items.buyer — покупатель
            # ДСП, у «Лукойл» покупатель свой и живёт в плане продажи.
            rows = conn.execute(
                "SELECT DISTINCT buyer FROM bp_item_sales WHERE item_id = ? "
                "AND variant = 'bsp' AND buyer IS NOT NULL AND buyer <> ''",
                (it["id"],)).fetchall()
            if len(rows) == 1:
                sets["buyer"] = rows[0]["buyer"]
        if sets:
            assign = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE bp_items SET {assign} WHERE id = ?",
                         (*sets.values(), it["id"]))
            for k in sets:
                filled[k] = filled.get(k, 0) + 1
    # Покупатель сделки — если у всех позиций он один и в шапке пусто.
    bp = conn.execute("SELECT buyer_name FROM business_plans WHERE id = ?",
                      (bp_id,)).fetchone()
    if bp and not (bp["buyer_name"] or "").strip():
        buyers = conn.execute(
            "SELECT DISTINCT buyer FROM bp_items WHERE bp_id = ? AND buyer "
            "IS NOT NULL AND buyer <> ''", (bp_id,)).fetchall()
        if len(buyers) == 1:
            conn.execute("UPDATE business_plans SET buyer_name = ? WHERE id = ?",
                         (buyers[0]["buyer"], bp_id))
            filled["buyer_name"] = 1
    return {k: v for k, v in filled.items() if v}


@app.post("/bp/{bp_id}/autofill")
def autofill_route(request: Request, bp_id: int):
    """Подтянуть данные из связанных таблиц (кнопка в составе лота)."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    filled = autofill_items(conn, bp_id)
    log(conn, actor(request), bp_id, "autofill", str(filled))
    conn.commit()
    conn.close()
    labels = {"unit": "единица измерения", "cargo_group": "группа учёта",
              "distance_km": "расстояние", "buyer": "покупатель",
              "seller_code": "код продавца", "buyer_name": "покупатель сделки"}
    if not filled:
        return back(request, bp_id, "Всё уже заполнено — подтягивать нечего.")
    parts = ", ".join(f"{labels.get(k, k)}: {v}" for k, v in filled.items())
    return back(request, bp_id, f"Подтянуто из связанных таблиц — {parts}.")


@app.post("/bp/{bp_id}/bases/split")
async def set_base_split(request: Request, bp_id: int):
    """Корректировка разреза затрат базы: логистика и переработка в рублях.

    Итог по базе задан фактическими статьями БП и не меняется — правится
    только деление между «путями» и «площадкой», разница уходит в «прочие».
    Так сумма прибыли баз по-прежнему равна прибыли до налогообложения.
    """
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("costs", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Корректировать затраты может экономист.")
    variant = resolve_variant(request, bp)
    form = await read_form(request)
    base = form.text("base") or ""
    if not base:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}", "База не указана.")
    logistics = form.num("logistics", field="amount", label="Логистика по базе")
    processing = form.num("processing", field="amount",
                          label="Переработка по базе")
    comment = form.text("comment")
    if not form.ok:
        conn.close()
        return redirect(f"/bp/{bp_id}?variant={variant}", form.message())
    if logistics is None and processing is None:
        conn.execute("DELETE FROM bp_base_split WHERE bp_id = ? AND base = ? "
                     "AND variant = ?", (bp_id, base, variant))
        msg = f"Корректировка по базе «{base[:30]}» снята — вернулся расчёт по модели."
    else:
        conn.execute(
            "INSERT INTO bp_base_split (bp_id, base, variant, logistics, "
            "processing, comment) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (bp_id, base, variant) DO UPDATE SET "
            "logistics = excluded.logistics, processing = excluded.processing, "
            "comment = excluded.comment, updated_at = datetime('now')",
            (bp_id, base, variant, logistics, processing, comment))
        msg = f"Разрез затрат по базе «{base[:30]}» скорректирован."
    log(conn, actor(request), bp_id, "base_split", f"{base}: лог={logistics} перераб={processing}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant={variant}", msg)


@app.post("/bp/{bp_id}/items/requisites")
async def update_requisites(request: Request, bp_id: int):
    """Реквизиты перечня продавца по позициям: балансовая стоимость, условия
    хранения, состояние, документы и дополнительные работы. Приходят из
    перечня при импорте, но продавец часто уточняет их позже — поэтому
    правятся прямо в таблице."""
    role = current_role(request)
    conn = connect()
    bp, items, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("items", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Нет прав на изменение позиций.")
    variant = resolve_variant(request, bp)
    form = await read_form(request)
    stale = edit_conflict(conn, request, bp, form)
    if stale:
        conn.close()
        return back(request, bp_id, stale)
    changed = 0
    pending = []
    for it in items:
        sets: dict[str, object] = {}
        if form.has(f"balance_cost_{it['id']}"):
            sets["balance_cost"] = form.num(f"balance_cost_{it['id']}")
        for field in ("storage_conditions", "condition_note", "tech_doc",
                      "extra_works", "sale_period", "origin_reason",
                      "liquidity", "expert_note", "price_set_by"):
            if form.has(f"{field}_{it['id']}"):
                sets[field] = form.text(f"{field}_{it['id']}")
        # Дата цены проставляется сама, когда указали, кто её дал: вручную
        # её не ведут, а для спора «когда согласовали» она и нужна.
        if sets.get("price_set_by") and not it["price_set_at"]:
            sets["price_set_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if sets:
            assign = ", ".join(f"{k} = ?" for k in sets)
            pending.append((f"UPDATE bp_items SET {assign} WHERE id = ?",
                            (*sets.values(), it["id"])))
    if not form.ok:
        conn.close()
        return back(request, bp_id, form.message())
    for sql, args in pending:
        conn.execute(sql, args)
        changed += 1
    log(conn, actor(request), bp_id, "update_requisites", f"позиций: {changed}")
    conn.commit()
    conn.close()
    return redirect(f"/bp/{bp_id}?variant={variant}",
                    f"Реквизиты перечня сохранены ({changed} позиций).")


@app.post("/bp/{bp_id}/risks/{risk_id}/update")
async def update_risk(request: Request, bp_id: int, risk_id: int):
    """Правка риска на месте: формулировки уточняются по ходу проработки."""
    role = current_role(request)
    conn = connect()
    bp, _, _ = load_bp(conn, bp_id)
    if bp is None or not workflow.can_edit_section("risks", role, bp["status"]):
        conn.close()
        return back(request, bp_id, "Риски правит экономист или директор.")
    form = await request.form()
    conn.execute(
        "UPDATE bp_risks SET risk_type = ?, description = ?, probability = ?, "
        "impact = ?, mitigation = ? WHERE id = ? AND bp_id = ?",
        ((form.get("risk_type") or "").strip() or "Риск",
         (form.get("description") or "").strip() or None,
         (form.get("probability") or "С").strip(),
         (form.get("impact") or "Н").strip(),
         (form.get("mitigation") or "").strip() or None, risk_id, bp_id))
    log(conn, actor(request), bp_id, "update_risk", str(risk_id))
    conn.commit()
    conn.close()
    return back(request, bp_id, "Риск сохранён.")
