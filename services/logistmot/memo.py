"""Служебная записка на перевозку: форма, маршрут согласования, PDF.

Заменяет бумажный круг: логист печатал записку в Word → нёс на подпись
начальнику отдела логистики (Васильева Е.В.) → нёс исполнительному директору
(Пуганов В.И.) → сканировал подписанное и прикреплял в 1С.

Здесь записка живёт в базе, согласуется в панели под личным паролем, а в 1С
уходит готовый PDF с отметками «кто и когда согласовал».

Поля повторяют бумажную форму — её и подписывают, менять состав нельзя без
согласия отдела логистики.

⚠️ Ставка прайса записывается СНИМКОМ в момент подачи (`price_*`, `diff`).
Прайс со временем меняется, и без снимка согласованная записка задним числом
превратилась бы в «превышающую» — а согласовывали её по другим цифрам.
"""

import hashlib
import io
import os
import re
import time

import accounts
import db
import maxfmt as mf
import price

# --- маршрут согласования -------------------------------------------------

# draft → on_head → on_director → approved. Отказ на любом шаге возвращает
# записку логисту (rejected): он правит и подаёт заново.
DRAFT, ON_HEAD, ON_DIRECTOR, APPROVED, REJECTED = (
    "draft", "on_head", "on_director", "approved", "rejected")

STATUS_LABEL = {
    DRAFT: "черновик",
    ON_HEAD: "у начальника отдела логистики",
    ON_DIRECTOR: "у исполнительного директора",
    APPROVED: "согласована",
    REJECTED: "отклонена",
}

# Кто какой шаг закрывает. Роли — те же, что в PANEL_USERS.
STEP_ROLE = {ON_HEAD: "head", ON_DIRECTOR: "director"}
NEXT_STATUS = {ON_HEAD: ON_DIRECTOR, ON_DIRECTOR: APPROVED}

ROLE_TITLE = {
    "author": "Исполнитель",
    "head": "Руководитель отдела логистики",
    "director": "Согласовано (исп. директор)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    number        TEXT,                -- «СЗ-2026-0001»
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    author        TEXT,                -- ФИО исполнителя (как в записке)
    author_login  TEXT,
    broadcast_id  INTEGER,             -- заявка, из которой собрана
    status        TEXT NOT NULL DEFAULT 'draft',

    work_date     TEXT,                -- Дата работы
    task          TEXT,                -- Задача
    customer      TEXT,                -- Заказчик
    city          TEXT,                -- Город
    route         TEXT,                -- Маршрут
    work_kind     TEXT,                -- Вид работы
    cargo         TEXT,                -- Характер груза
    transport     TEXT,                -- Вид транспорта
    vehicles      TEXT,                -- Кол-во машин
    vehicle_model TEXT,                -- модель ТС — нужна для ключа прайса
    crane_min_h   TEXT,                -- Кран мин час
    supply_h      TEXT,                -- Подача час
    rate_unit     TEXT,                -- час | тонна | рейс | смена
    rate          REAL,                -- ставка как введена
    rate_vat_incl INTEGER NOT NULL DEFAULT 0,
    vat_rate      TEXT,                -- «22%»
    rate_net      REAL,                -- ставка без НДС — по ней сравнение
    riggers       TEXT,                -- Стропальщики, кол-во
    total_cost    TEXT,                -- Итого стоимость
    planned       INTEGER,             -- 1 = план, 0 = вне плана
    comment       TEXT,

    price_base    REAL,                -- снимок прайса на момент подачи
    price_unit    TEXT,
    price_doc     TEXT,
    price_note    TEXT,                -- по какой строке прайса считали
    diff          REAL,
    diff_pct      REAL
);

CREATE TABLE IF NOT EXISTS memo_approvals (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id  INTEGER NOT NULL REFERENCES memos(id),
    role     TEXT,      -- author | head | director
    who      TEXT,      -- имя, как показывать в записке
    login    TEXT,
    decision TEXT,      -- submitted | approved | rejected
    comment  TEXT,
    at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memo_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id     INTEGER NOT NULL REFERENCES memos(id),
    filename    TEXT NOT NULL,
    mime        TEXT,
    size        INTEGER NOT NULL,
    data        BLOB NOT NULL,       -- сам файл: листы согласования небольшие
    uploaded_by TEXT,
    at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_memos_status ON memos (status);
CREATE INDEX IF NOT EXISTS ix_memo_appr ON memo_approvals (memo_id);
CREATE INDEX IF NOT EXISTS ix_memo_files ON memo_files (memo_id);
"""

FIELDS = ("work_date", "task", "customer", "city", "route", "work_kind",
          "cargo", "transport", "vehicles", "vehicle_model", "crane_min_h",
          "supply_h", "rate_unit", "rate", "rate_vat_incl", "vat_rate",
          "riggers", "total_cost", "planned", "comment")


def init():
    price.init()
    with db.connect() as conn:
        conn.executescript(SCHEMA)


# --- ставка и сравнение с прайсом ----------------------------------------

# Единицы в записке названы по-человечески, в прайсе — сокращённо.
UNIT_TO_PRICE = {"тонна": "т", "тонн": "т", "т": "т", "час": "ч", "ч": "ч",
                 "рейс": "рейс", "смена": "смена"}


def to_net(rate: float | None, vat_incl: bool, vat_rate: str | None) -> float | None:
    """Ставка без НДС: общий знаменатель для сравнения с прайсом.

    ⚠️ «Ставка с НДС» без указанной ставки НДС — это НЕ повод сравнивать как
    есть: 4200 с НДС против прайсовых 3450 без НДС дадут «+21,7%», хотя на
    деле рейс в прайс укладывается. Возвращаем None, и сверка честно скажет,
    что не хватает данных, вместо выдуманного превышения.
    """
    if rate is None:
        return None
    if vat_incl and not price.VAT_RATES.get((vat_rate or "").strip()):
        return None
    return price.net_price(float(rate), bool(vat_incl), vat_rate)


def price_snapshot(route: str, rate_net: float | None, rate_unit: str | None,
                   vehicle: str | None, rate: float | None = None,
                   vat_incl: bool = False, vat_rate: str | None = None) -> dict:
    """Что показать и что сохранить про отклонение от прайса."""
    if rate_net is None and rate is not None and vat_incl and not vat_rate:
        return {"price_note": "ставка указана с НДС, но ставка НДС не выбрана — "
                              "без неё сверить с прайсом нельзя"}
    if rate_net is None or not route:
        return {}
    cmp = price.compare(route, rate_net,
                        unit=UNIT_TO_PRICE.get((rate_unit or "").strip().lower()),
                        vehicle=vehicle)
    if not cmp.get("found"):
        return {"price_note": cmp.get("reason") or "в прайсе такого маршрута нет"}
    note = f"{cmp['route']} · {cmp['vehicle'] or '—'} · {cmp['unit']}"
    if cmp.get("cond"):
        note += f" · {cmp['cond']}"
    note += f" · док. № {cmp['doc']} от {cmp['doc_date']} ({cmp['doc_status']})"
    return {
        "price_base": cmp["price"],
        "price_unit": cmp["unit"],
        "price_doc": cmp["doc"],
        "price_note": note,
        "diff": cmp["diff"],
        "diff_pct": cmp["pct"],
    }


def preview(payload: dict) -> dict:
    """Отклонение от прайса до сохранения — чтобы логист видел его в форме."""
    rate = _num(payload.get("rate"))
    vat_incl = payload.get("rate_vat_incl") in (1, "1", True, "true")
    vat_rate = payload.get("vat_rate")
    net = to_net(rate, vat_incl, vat_rate)
    snap = price_snapshot(payload.get("route") or "", net,
                          payload.get("rate_unit"), payload.get("vehicle_model"),
                          rate=rate, vat_incl=vat_incl, vat_rate=vat_rate)
    return {"rate_net": net} | snap


# --- подстановка данных из заявки ----------------------------------------
#
# Записка пишется по уже отработанной заявке: маршрут, груз, ставка и машины
# известны. Перебивать их руками — лишние полминуты и лишний шанс опечататься,
# поэтому собираем всё, что можем, прямо из заявки и её откликов.
#
# ⚠️ Ставку в заявках логисты пишут свободным текстом, и вид у неё разный:
# «за рейс 35000 без НДС, 42700 с НДС», «100000 ₽ без НДС», «3508 без НДС,
# 4280 с НДС». Разбираем по словам «без НДС» / «с НДС», а не по позиции.

RATE_NO_VAT = re.compile(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб\S*)?\s*без\s+НДС",
                         re.IGNORECASE)
RATE_WITH_VAT = re.compile(r"(?<!без )(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб\S*)?\s*с\s+НДС",
                           re.IGNORECASE)
UNIT_WORDS = ((r"за\s+рейс|/\s*рейс|\bрейс\b", "рейс"),
              (r"за\s+тонну|/\s*тонн|\bтонн\w*\b|\bт\b", "тонна"),
              (r"за\s+смену|\bсмена\b", "смена"),
              (r"за\s+час|/\s*час|\bчас\b", "час"))

# Заявка «до 20000» почти всегда за тонну, выше — за рейс. Грубо, поэтому
# идёт последней, после явных слов и после подсказки самого прайса.
RATE_PER_TRIP_FROM = 20000


def _parse_rate(raw: str | None) -> dict:
    """«за рейс 35000 без НДС, 42700 с НДС» → ставка, признак НДС, единица.

    Предпочитаем цифру БЕЗ НДС: с ней сверка с прайсом однозначна, а ставку
    НДС угадывать не приходится.
    """
    text = raw or ""
    out: dict = {}
    m = RATE_NO_VAT.search(text)
    if m:
        out["rate"] = re.sub(r"\s", "", m.group(1)).replace(",", ".")
        out["rate_vat_incl"] = "0"
        out["vat_rate"] = ""
    else:
        m = RATE_WITH_VAT.search(text)
        if m:
            out["rate"] = re.sub(r"\s", "", m.group(1)).replace(",", ".")
            out["rate_vat_incl"] = "1"
            out["vat_rate"] = ""     # какая именно — знает только логист
            out["needs_vat"] = True
    for pattern, unit in UNIT_WORDS:
        if re.search(pattern, text, re.IGNORECASE):
            out["rate_unit"] = unit
            break
    return out


def _unit_from_price(route: str, guessed: str | None, rate: float | None) -> tuple[str, str]:
    """Единица ставки, если в заявке её не написали словами. -> (единица, как узнали)."""
    if guessed:
        return guessed, "из текста заявки"
    units = {r["unit"] for r in price.effective_rates()
             if r["route_key"] == price.route_key(route)}
    named = {u for u in units if u}
    if len(named) == 1:                      # по маршруту в прайсе одна единица
        only = named.pop()
        back = {v: k for k, v in UNIT_TO_PRICE.items() if k != "т" or v != "т"}
        return {"т": "тонна", "рейс": "рейс", "ч": "час",
                "смена": "смена"}.get(only, "тонна"), "по прайсу этого маршрута"
    if rate is not None:
        return ("рейс" if rate >= RATE_PER_TRIP_FROM else "тонна"), "по величине ставки — проверьте"
    return "тонна", "по умолчанию — проверьте"


def _guess_vehicle(ts_line: str | None) -> str:
    """«тент / открытые» → «тент/борт»: в прайсе модель называется иначе."""
    low = (ts_line or "").lower()
    if not low:
        return ""
    vehicles = sorted({r["vehicle"] for r in price.effective_rates() if r["vehicle"]},
                      key=len, reverse=True)
    for v in vehicles:                       # прямое совпадение названия
        if v.lower() in low:
            return v
    if any(w in low for w in ("тент", "борт", "открыт", "шаланд", "полуприцеп")):
        return next((v for v in vehicles if v.lower() == "тент/борт"), "")
    for word in ("кран", "ломовоз", "кму", "погрузчик", "вышк"):
        if word in low:
            hit = [v for v in vehicles if word in v.lower()]
            if len(hit) == 1:
                return hit[0]
    return ""


def _card_field(text: str | None, emoji: str) -> str:
    """Достать строку карточки заявки: «🚚 ТС: тент / открытые» → значение."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(emoji):
            return (line.split(":", 1)[1] if ":" in line else line[len(emoji):]).strip()
    return ""


def _logist_ids() -> set[int]:
    ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    try:
        with db.connect() as conn:
            ids |= {r["user_id"] for r in conn.execute("SELECT user_id FROM admins")}
    except Exception:  # noqa: BLE001
        pass
    return ids


def _vehicles_from_replies(bid: int) -> tuple[str, str]:
    """Сколько машин набралось по откликам. -> (количество, пояснение).

    Перевозчик обычно жмёт кнопку («готов взять» — это одна машина), реже
    пишет «2 ТС». Считаем по людям: у кого названо число — берём его,
    у остальных — одну машину.
    """
    skip = _logist_ids() or {0}
    ph = ",".join("?" * len(skip))
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT user_id, offer, text FROM replies
                WHERE broadcast_id = ? AND relevant = 1
                  AND user_id NOT IN ({ph})""",
            [bid, *sorted(skip)]).fetchall()
    per_user: dict[int, int] = {}
    for r in rows:
        n = 1
        m = re.search(r"(\d{1,2})\s*ТС", (r["offer"] or "") + " " + (r["text"] or ""),
                      re.IGNORECASE)
        if m and 1 <= int(m.group(1)) <= 50:
            n = int(m.group(1))
        per_user[r["user_id"]] = max(per_user.get(r["user_id"], 0), n)
    if not per_user:
        return "", "откликов по заявке пока нет"
    total = sum(per_user.values())
    return str(total), (f"{total} по откликам от {len(per_user)} перевозчик"
                        + ("а" if 2 <= len(per_user) <= 4 else "ов"
                           if len(per_user) != 1 else "а"))


def prefill(bid: int) -> dict:
    """Поля записки, собранные из заявки #bid и её откликов."""
    init()
    b = db.get_broadcast(bid)
    if b is None:
        return {}
    route = (b["route"] or "").replace("→", "-")
    fields: dict = {
        "route": route,
        "cargo": b["cargo"] or "",
        "transport": "авто",
        "work_kind": "реализация",
    }
    hints: list[str] = []

    # Город — пункт отправления: именно из него едет машина
    if route:
        first = route.split("-")[0].split(",")[0].strip()
        fields["city"] = re.sub(r"^(?:г|гор)\.\s*", "", first, flags=re.IGNORECASE)

    # Дата работы — первая дата заявки («26.08-28.08» → «26.08»)
    m = re.search(r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?", b["dates"] or "")
    if m:
        fields["work_date"] = m.group(0)

    parsed = _parse_rate(b["rate"])
    fields.update({k: v for k, v in parsed.items() if k not in ("needs_vat",)})
    if parsed.get("needs_vat"):
        hints.append("в заявке ставка только с НДС — выберите ставку НДС, "
                     "иначе сверка с прайсом не посчитается")
    unit, how = _unit_from_price(route, parsed.get("rate_unit"),
                                 _num(parsed.get("rate")))
    fields["rate_unit"] = unit
    if "проверьте" in how:
        hints.append(f"единица ставки «{unit}» определена {how}")

    veh = _guess_vehicle(_card_field(b["text"], "🚚"))
    if veh:
        fields["vehicle_model"] = veh

    count, note = _vehicles_from_replies(bid)
    if count:
        fields["vehicles"] = count
    hints.append("кол-во машин: " + note)

    return {"fields": fields, "hints": hints,
            "label": f"#{b['id']} {b['route'] or ''}".strip()}


def choices(limit: int = 40) -> list[dict]:
    """Заявки для выпадающего списка: сначала актуальные, потом недавние."""
    init()
    now = int(time.time())
    window = int(float(os.getenv("REPLY_WINDOW_HOURS", "48")) * 3600)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT b.id, b.route, b.dates, b.cargo, b.rate, b.sent_at, b.closed,
                      (SELECT COUNT(DISTINCT r.user_id) FROM replies r
                        WHERE r.broadcast_id = b.id AND r.relevant = 1) AS responders
               FROM broadcasts b WHERE b.sent_at >= ?
               ORDER BY b.sent_at DESC LIMIT ?""",
            (now - 30 * 86400, limit)).fetchall()
    out = []
    for r in rows:
        active = db.broadcast_is_active(r, window, now)
        out.append({
            "id": r["id"], "route": r["route"] or "маршрут не распознан",
            "dates": r["dates"] or "", "cargo": r["cargo"] or "",
            "rate": r["rate"] or "", "responders": r["responders"],
            "active": bool(active),
        })
    out.sort(key=lambda x: (not x["active"], -x["id"]))
    return out


# --- создание и правка ----------------------------------------------------

def _num(raw) -> float | None:
    if raw is None or raw == "":
        return None
    s = re.sub(r"[^\d,.\-]", "", str(raw)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def next_number() -> str:
    """«СЗ-2026-0001». Нумерация сквозная внутри года."""
    year = time.strftime("%Y")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT number FROM memos WHERE number LIKE ? ORDER BY id DESC LIMIT 1",
            (f"СЗ-{year}-%",),
        ).fetchone()
    n = 0
    if row and row["number"]:
        m = re.search(r"(\d+)$", row["number"])
        if m:
            n = int(m.group(1))
    return f"СЗ-{year}-{n + 1:04d}"


def _clean(payload: dict) -> dict:
    """Поля формы → значения для базы (ставка и флаги приводятся к числам)."""
    out = {}
    for k in FIELDS:
        v = payload.get(k)
        if k == "rate":
            out[k] = _num(v)
        elif k in ("rate_vat_incl", "planned"):
            out[k] = 1 if v in (1, "1", True, "true", "да", "план") else 0
        else:
            out[k] = (str(v).strip() if v is not None else "")
    out["rate_net"] = to_net(out["rate"], out["rate_vat_incl"], out["vat_rate"])
    _snap_args = dict(rate=out["rate"], vat_incl=bool(out["rate_vat_incl"]),
                      vat_rate=out["vat_rate"])
    # снимок прайса задаём всегда целиком: при правке записки старые цифры
    # должны обнулиться, а не остаться от прошлого маршрута
    snap = {"price_base": None, "price_unit": None, "price_doc": None,
            "price_note": None, "diff": None, "diff_pct": None}
    snap.update(price_snapshot(out["route"], out["rate_net"], out["rate_unit"],
                               out["vehicle_model"], **_snap_args))
    out |= snap
    return out


def create(payload: dict, author: str, login: str | None,
           submit: bool = True) -> int:
    """Создать записку. submit=True — сразу подать начальнику отдела."""
    init()
    data = _clean(payload)
    now = int(time.time())
    bid = payload.get("broadcast_id")
    try:
        bid = int(bid) if bid not in (None, "", "0") else None
    except (TypeError, ValueError):
        bid = None
    status = ON_HEAD if submit else DRAFT
    cols = ["number", "created_at", "updated_at", "author", "author_login",
            "broadcast_id", "status"] + list(data)
    vals = [next_number(), now, now, author, login, bid, status] + \
           [data[k] for k in data]
    with db.connect() as conn:
        cur = conn.execute(
            f"INSERT INTO memos ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", vals)
        mid = cur.lastrowid
        conn.execute(
            """INSERT INTO memo_approvals (memo_id, role, who, login, decision,
                                           comment, at)
               VALUES (?, 'author', ?, ?, ?, '', ?)""",
            (mid, author, login, "submitted" if submit else "saved", now),
        )
    return mid


def update(memo_id: int, payload: dict) -> bool:
    """Правка записки. Разрешена только пока она не ушла дальше логиста."""
    init()
    row = get(memo_id)
    if row is None or row["status"] not in (DRAFT, REJECTED):
        return False
    data = _clean(payload)
    sets = ", ".join(f"{k} = ?" for k in data)
    with db.connect() as conn:
        conn.execute(f"UPDATE memos SET {sets}, updated_at = ? WHERE id = ?",
                     list(data.values()) + [int(time.time()), memo_id])
    return True


def submit(memo_id: int, who: str, login: str | None) -> tuple[bool, str]:
    """Подать (или подать заново после отказа) начальнику отдела логистики."""
    row = get(memo_id)
    if row is None:
        return False, "записка не найдена"
    if row["status"] not in (DRAFT, REJECTED):
        return False, f"записка уже {STATUS_LABEL.get(row['status'], row['status'])}"
    now = int(time.time())
    with db.connect() as conn:
        conn.execute("UPDATE memos SET status = ?, updated_at = ? WHERE id = ?",
                     (ON_HEAD, now, memo_id))
        conn.execute(
            """INSERT INTO memo_approvals (memo_id, role, who, login, decision,
                                           comment, at)
               VALUES (?, 'author', ?, ?, 'submitted', '', ?)""",
            (memo_id, who, login, now),
        )
    return True, "подана на согласование"


def decide(memo_id: int, role: str, who: str, login: str | None,
           approve: bool, comment: str = "") -> tuple[bool, str]:
    """Решение согласующего. Роль должна совпадать с текущим шагом записки."""
    row = get(memo_id)
    if row is None:
        return False, "записка не найдена"
    status = row["status"]
    if status not in STEP_ROLE:
        return False, f"записка {STATUS_LABEL.get(status, status)} — решение не требуется"
    if STEP_ROLE[status] != role:
        return False, "сейчас записку согласует не вы"
    if not approve and not (comment or "").strip():
        return False, "при отказе нужен комментарий — логисту надо понять, что править"
    now = int(time.time())
    new_status = NEXT_STATUS[status] if approve else REJECTED
    with db.connect() as conn:
        conn.execute("UPDATE memos SET status = ?, updated_at = ? WHERE id = ?",
                     (new_status, now, memo_id))
        conn.execute(
            """INSERT INTO memo_approvals (memo_id, role, who, login, decision,
                                           comment, at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memo_id, role, who, login,
             "approved" if approve else "rejected", (comment or "").strip(), now),
        )
    if not approve:
        return True, "отклонена, вернулась логисту"
    if new_status == APPROVED:
        return True, "согласована окончательно — можно выгружать PDF"
    return True, "согласована, ушла исполнительному директору"


# --- согласование через MAX ------------------------------------------------
#
# Уведомление согласующему несёт две кнопки: «Согласовать» и «Отклонить».
# Личность — по id в MAX, привязанному к учётке (accounts.by_max_id): это
# тот же человек, что входит в панель, только без пароля — id выдаёт сервер
# мессенджера, подделать его из чата нельзя. Шаг записки по-прежнему
# определяет статус, а не кнопка: нажать «Согласовать» на чужом шаге нельзя.

CB_OK, CB_NO = "memo_ok", "memo_no"


def notice(row: dict, event: str, link: str) -> dict | None:
    """Кому и что писать в MAX после события по записке.

    Возвращает {"role", "text", "buttons"} или None, если писать некому:
    ответ логисту виден в панели, в личку его не дёргаем."""
    number, route = row["number"], row.get("route") or ""
    if event == "submitted" and row["status"] == ON_HEAD:
        role = "head"
        head = f"📝 {mf.b(f'{number} ждёт вашего согласования')}"
    elif event == "approved" and row["status"] == ON_DIRECTOR:
        role = "director"
        head = f"📝 {mf.b(f'{number} согласована отделом, ждёт вашего решения')}"
    else:
        return None
    lines = [head, mf.esc(" · ".join(x for x in (route, row.get("cargo")) if x))]
    if row.get("customer"):
        lines.append(f"Заказчик: {mf.esc(row['customer'])}")
    if row.get("rate") is not None:
        lines.append(f"Ставка: {mf.b(mf.money(row['rate']) + ' ₽/' + (row.get('rate_unit') or 'ед.'))}"
                     f" {'с НДС' if row.get('rate_vat_incl') else 'без НДС'}")
    pct = row.get("diff_pct")
    if pct is not None:
        verdict = "выше прайса" if pct > 0 else "в прайсе"
        lines.append(f"К прайсу: {mf.b(mf.sign_pct(pct))} — {verdict} "
                     f"({mf.money(row.get('price_base'))} ₽/{row.get('price_unit')})")
    elif row.get("price_note"):
        lines.append(f"К прайсу: {mf.esc(row['price_note'])}")
    if row.get("author"):
        lines.append(f"Исполнитель: {mf.esc(row['author'])}")
    lines.append(mf.link("Открыть записку на панели", f"{link}/#memo={row['id']}")
                 + " · или решите кнопками ниже")
    return {"role": role, "text": "\n".join(lines),
            "buttons": [("Согласовать", f"{CB_OK}:{row['id']}"),
                        ("Отклонить (нужен комментарий)", f"{CB_NO}:{row['id']}")]}


def can_decide_from_max(memo_id: int, max_user_id: int) -> tuple[bool, str]:
    """Может ли человек с этим id в MAX решать по записке сейчас.
    (True, номер записки) либо (False, объяснение для ответа в личку)."""
    acc = accounts.by_max_id(max_user_id)
    if acc is None:
        return False, ("Ваш аккаунт MAX не привязан к учётке панели — "
                       "решение принять нельзя.")
    row = get(memo_id)
    if row is None:
        return False, "Записка не найдена."
    step_role = STEP_ROLE.get(row["status"])
    if step_role is None:
        return False, (f"По записке {row['number']} решение уже принято: "
                       f"{STATUS_LABEL.get(row['status'], row['status'])}.")
    if step_role not in acc.get("roles", []):
        return False, f"Сейчас записку {row['number']} согласует не вы."
    return True, row["number"]


def decide_from_max(memo_id: int, max_user_id: int, approve: bool,
                    comment: str = "") -> tuple[bool, str]:
    """Решение по кнопке в MAX. Возвращает (ok, текст ответа человеку)."""
    ok, why = can_decide_from_max(memo_id, max_user_id)
    if not ok:
        return False, why
    acc = accounts.by_max_id(max_user_id)
    row = get(memo_id)
    step_role = STEP_ROLE[row["status"]]
    ok, msg = decide(memo_id, step_role, acc["name"], acc["login"],
                     approve, comment)
    if not ok:
        return False, msg[0].upper() + msg[1:] + "."
    row = get(memo_id)
    if row["status"] == APPROVED:
        text = (f"✓ {mf.b(f'{row['number']} согласована окончательно')}\n"
                f"Логист может выгружать PDF с отметками обоих согласующих.")
    elif row["status"] == REJECTED:
        text = (f"✗ {mf.b(f'{row['number']} отклонена')}\n"
                f"Вернулась логисту на доработку с вашим комментарием:\n"
                f"{mf.quote(comment)}")
    else:
        text = (f"✓ {mf.b(f'{row['number']} согласована')}\n"
                f"Ушла исполнительному директору.")
    return True, text


# --- чтение ---------------------------------------------------------------

def get(memo_id: int):
    init()
    with db.connect() as conn:
        return conn.execute("SELECT * FROM memos WHERE id = ?", (memo_id,)).fetchone()


def approvals(memo_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memo_approvals WHERE memo_id = ? ORDER BY at, id",
            (memo_id,)).fetchall()
    return [dict(r) for r in rows]


def signatures(memo_id: int) -> list[dict]:
    """Отметки для подвала PDF: по одной строке на роль, последнее решение."""
    out = []
    for role in ("author", "head", "director"):
        acts = [a for a in approvals(memo_id)
                if a["role"] == role and a["decision"] in
                ("submitted", "approved", "rejected")]
        if not acts:
            out.append({"role": role, "title": ROLE_TITLE[role], "who": None})
            continue
        a = acts[-1]
        out.append({
            "role": role, "title": ROLE_TITLE[role], "who": a["who"],
            "decision": a["decision"], "comment": a["comment"],
            "at": time.strftime("%d.%m.%Y %H:%M", time.localtime(a["at"])),
        })
    return out


def as_dict(row) -> dict:
    d = dict(row)
    d["status_label"] = STATUS_LABEL.get(d["status"], d["status"])
    d["created"] = time.strftime("%d.%m.%Y %H:%M", time.localtime(d["created_at"]))
    d["updated"] = time.strftime("%d.%m.%Y %H:%M", time.localtime(d["updated_at"]))
    d["signatures"] = signatures(d["id"])
    d["files"] = files_of(d["id"])
    d["over"] = bool(d["diff"] and d["diff"] > 0)
    return d


def list_memos(status: str | None = None, limit: int = 100,
               author_login: str | None = None) -> list[dict]:
    init()
    sql = "SELECT * FROM memos"
    where, args = [], []
    if status == "queue":                 # всё, что ждёт решения
        where.append("status IN (?, ?)")
        args += [ON_HEAD, ON_DIRECTOR]
    elif status:
        where.append("status = ?")
        args.append(status)
    if author_login:
        where.append("author_login = ?")
        args.append(author_login)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [as_dict(r) for r in rows]


def queue_for(role: str) -> list[dict]:
    """Что ждёт решения именно этой роли."""
    status = next((s for s, r in STEP_ROLE.items() if r == role), None)
    return list_memos(status) if status else []


def stats(days: int = 90, route: str | None = None) -> dict:
    """Сводка по запискам для исполнительного директора.

    Главный вопрос отчёта — насколько ставки уходят выше подписанного прайса
    и как это меняется по месяцам.

    ⚠️ Суммарной «переплаты в рублях» здесь нет и быть не может: отклонение
    считается на ЕДИНИЦУ (тонну, рейс, час), а тоннаж рейса в записке не
    хранится. Сложить их в общий рубль — значит выдумать цифру, на которую
    потом будут ссылаться. Показываем то, что знаем: процент, количество
    превышений и худший случай.
    """
    init()
    since = int(time.time()) - days * 86400
    where, args = ["created_at >= ?"], [since]
    if route:
        where.append("route = ?")
        args.append(route)
    cond = " AND ".join(where)

    with db.connect() as conn:
        totals = conn.execute(
            f"""SELECT COUNT(*) AS memos,
                   SUM(status = 'approved')                    AS approved,
                   SUM(status = 'rejected')                    AS rejected,
                   SUM(status IN ('on_head', 'on_director'))   AS pending,
                   IFNULL(SUM(diff > 0), 0)                    AS over_count,
                   AVG(diff_pct)                               AS avg_pct,
                   MAX(diff_pct)                               AS max_pct,
                   SUM(diff_pct IS NULL)                       AS no_price
               FROM memos WHERE {cond}""", args).fetchone()

        by_route = conn.execute(
            f"""SELECT route, COUNT(*) AS n,
                   AVG(diff_pct) AS avg_pct, MAX(diff_pct) AS max_pct,
                   IFNULL(SUM(diff > 0), 0) AS over_n,
                   MAX(created_at) AS last_at
               FROM memos WHERE {cond} AND route IS NOT NULL AND route != ''
               GROUP BY route ORDER BY avg_pct IS NULL, avg_pct DESC, n DESC
               LIMIT 40""", args).fetchall()

        by_month = conn.execute(
            f"""SELECT strftime('%Y-%m', created_at, 'unixepoch', 'localtime') AS m,
                   COUNT(*) AS n, AVG(diff_pct) AS avg_pct,
                   IFNULL(SUM(diff > 0), 0) AS over_n
               FROM memos WHERE {cond}
               GROUP BY m ORDER BY m""", args).fetchall()

        # сколько записка лежит на каждом шаге: подана → решение
        speed = conn.execute(
            """SELECT a.role,
                      AVG(a.at - (SELECT MAX(p.at) FROM memo_approvals p
                                   WHERE p.memo_id = a.memo_id AND p.at <= a.at
                                     AND p.id != a.id)) AS avg_sec,
                      COUNT(*) AS n
               FROM memo_approvals a
               WHERE a.decision IN ('approved', 'rejected') AND a.at >= ?
               GROUP BY a.role""", (since,)).fetchall()

        routes = [r["route"] for r in conn.execute(
            """SELECT DISTINCT route FROM memos
               WHERE route IS NOT NULL AND route != '' ORDER BY route""")]

    def rows(rs):
        return [dict(r) for r in rs]

    # Действующая ставка прайса по каждому направлению — рядом с отклонением.
    # Иначе «+11,8%» приходится держать в уме и лезть во вкладку «Прайс».
    eff = price.effective_rates()
    by_route_rows = rows(by_route)
    for r in by_route_rows:
        pool = [x for x in eff if x["route_key"] == price.route_key(r["route"])]
        if not pool:
            r["price_now"], r["price_unit"] = None, None
            continue
        # если по маршруту несколько единиц — показываем самую частую в прайсе
        units = {}
        for x in pool:
            units.setdefault(x["unit"], []).append(x)
        unit = max(units, key=lambda u: len(units[u]))
        cheapest = min(units[unit], key=lambda x: x["price_net"])
        r["price_now"], r["price_unit"] = cheapest["price_net"], unit

    return {
        "days": days,
        "route": route or "",
        "routes": routes,
        "totals": dict(totals),
        "by_route": by_route_rows,
        "by_month": rows(by_month),
        "speed": {r["role"]: {"avg_sec": r["avg_sec"], "n": r["n"]}
                  for r in speed},
    }


# --- приложенные файлы ----------------------------------------------------
#
# «Лист согласования» из 1С или Битрикса: логист прикладывает его к записке,
# когда он есть (есть не у всех). Файл лежит в базе рядом с запиской — при
# копировании bot.db уезжает и он, отдельной папки с сиротами не бывает.

FILE_MAX = 15 * 1024 * 1024   # больше листа согласования быть не должно

def attach_file(memo_id: int, filename: str, mime: str, data: bytes,
                who: str | None) -> tuple[bool, str]:
    if get(memo_id) is None:
        return False, "записка не найдена"
    if not data:
        return False, "пустой файл"
    if len(data) > FILE_MAX:
        return False, f"файл больше {FILE_MAX // (1024 * 1024)} МБ"
    name = os.path.basename(filename or "").strip() or "file"
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO memo_files (memo_id, filename, mime, size, data,
                                       uploaded_by, at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memo_id, name[:200], (mime or "")[:100], len(data), data,
             who, int(time.time())),
        )
    return True, f"файл «{name}» приложен"


def files_of(memo_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, filename, mime, size, uploaded_by, at
               FROM memo_files WHERE memo_id = ? ORDER BY at, id""",
            (memo_id,)).fetchall()
    return [dict(r) | {"when": time.strftime("%d.%m.%Y %H:%M",
                                             time.localtime(r["at"]))}
            for r in rows]


def get_file(file_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM memo_files WHERE id = ?",
                            (file_id,)).fetchone()


def delete_file(file_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM memo_files WHERE id = ?",
                            (file_id,)).rowcount > 0


def fingerprint(memo_id: int) -> str:
    """Отпечаток содержимого: если PDF поправят, он перестанет сходиться."""
    row = get(memo_id)
    if row is None:
        return ""
    parts = [str(row[k]) for k in row.keys() if k not in ("updated_at",)]
    for a in approvals(memo_id):
        parts += [a["role"] or "", a["who"] or "", a["decision"] or "",
                  str(a["at"])]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]


# --- PDF ------------------------------------------------------------------

# Шрифт нужен с кириллицей: встроенные в reportlab Helvetica и Vera её не
# содержат — вместо букв будут чёрные квадраты. На сервере есть DejaVu,
# на рабочем маке — Arial; берём первый найденный.
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
_FONTS_READY = False


def _fonts():
    """Зарегистрировать шрифт с кириллицей. -> (обычный, жирный)."""
    global _FONTS_READY
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if _FONTS_READY:
        return "MemoSans", "MemoSans-Bold"
    for regular, bold in FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("MemoSans", regular))
            pdfmetrics.registerFont(TTFont("MemoSans-Bold", bold))
            _FONTS_READY = True
            return "MemoSans", "MemoSans-Bold"
    raise RuntimeError(
        "не нашёл шрифт с кириллицей: поставьте fonts-dejavu-core "
        "(apt install fonts-dejavu-core)")


def money(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")


ROWS = [
    ("Дата работы", "work_date"), ("Задача", "task"),
    ("Заказчик", "customer"), ("Город", "city"),
    ("Маршрут", "route"), ("Вид работы", "work_kind"),
    ("Характер груза", "cargo"), ("Вид транспорта", "transport"),
    ("Кол-во машин", "vehicles"), ("Модель ТС", "vehicle_model"),
    ("Кран мин час", "crane_min_h"), ("Подача час", "supply_h"),
    ("Стропальщики, кол-во", "riggers"), ("Итого стоимость", "total_cost"),
]


def pdf(memo_id: int, with_price: bool = False) -> bytes:
    """Готовая записка одной страницей: поля, отметки о согласовании.

    with_price=True добавляет блок сверки с прайсом. По умолчанию его НЕТ:
    прайс — закупочные цены, их видят только Павел, Елена и Пуганов, а PDF
    выгружает и подшивает в 1С обычный логист. На бумажной форме такого
    блока тоже не было.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    row = get(memo_id)
    if row is None:
        raise ValueError("записка не найдена")
    reg, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    left, right = 20 * mm, W - 20 * mm
    y = H - 20 * mm

    c.setFont(bold, 15)
    c.drawCentredString(W / 2, y, "Служебная записка")
    y -= 7 * mm
    c.setFont(reg, 9)
    c.drawRightString(right, y, f"№ {row['number']} от {as_dict(row)['created']}")
    y -= 8 * mm

    val_x = left + 52 * mm
    val_w = right - val_x

    def wrap(text: str, font: str, size: float, width: float) -> list[str]:
        """Перенос по словам и по ФАКТИЧЕСКОЙ ширине строки.

        По числу символов считать нельзя: у пропорционального шрифта «Прошу
        согласовать ставку…» уезжает за правое поле, а «1» не доходит и до
        середины. Слово длиннее строки (длинный адрес) режем посимвольно.
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth
        out, cur = [], ""
        for word in str(text).split():
            probe = f"{cur} {word}".strip()
            if stringWidth(probe, font, size) <= width:
                cur = probe
                continue
            if cur:
                out.append(cur)
            while stringWidth(word, font, size) > width:
                cut = len(word)
                while cut > 1 and stringWidth(word[:cut], font, size) > width:
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            cur = word
        if cur:
            out.append(cur)
        return out or ["—"]

    def line(label, value, gap=6.0):
        nonlocal y
        c.setFont(reg, 10)
        c.drawString(left, y, f"{label}:")
        c.setFont(bold, 10)
        for i, chunk in enumerate(wrap(value or "—", bold, 10, val_w)):
            if i:
                y -= 5 * mm
            c.drawString(val_x, y, chunk)
        c.setFont(reg, 10)
        y -= gap * mm

    for label, key in ROWS:
        line(label, row[key])

    vat = ("с НДС" + (f" {row['vat_rate']}" if row["vat_rate"] else "")
           if row["rate_vat_incl"] else "без НДС")
    line("Ставка за " + (row["rate_unit"] or "—"),
         f"{money(row['rate'])} руб. {vat}"
         + (f"  (без НДС: {money(row['rate_net'])} руб.)"
            if row["rate_vat_incl"] else ""))
    line("План / вне плана", "план" if row["planned"] else "вне плана")
    if row["comment"]:
        line("Комментарии", row["comment"])

    # --- сверка с прайсом (только для руководителей) ---
    c.setStrokeColorRGB(.75, .78, .84)
    if with_price:
        y -= 2 * mm
        c.line(left, y, right, y)
        y -= 6 * mm
        c.setFont(bold, 10)
        c.drawString(left, y, "Сверка с прайсом")
        y -= 6 * mm
        c.setFont(reg, 9)
    if with_price and row["price_base"] is None:
        c.drawString(left, y, row["price_note"] or "прайс не загружен")
        y -= 5 * mm
    elif with_price:
        c.drawString(left, y, f"Прайс: {money(row['price_base'])} руб./"
                              f"{row['price_unit']} без НДС")
        y -= 5 * mm
        diff, pct = row["diff"], row["diff_pct"]
        sign = "+" if (diff or 0) > 0 else ""
        if (diff or 0) > 0:
            c.setFillColorRGB(.70, .23, .18)
        else:
            c.setFillColorRGB(.09, .48, .31)
        c.setFont(bold, 9)
        c.drawString(left, y, f"Отклонение: {sign}{money(diff)} руб. "
                              f"({sign}{pct}%)" if pct is not None else
                              f"Отклонение: {sign}{money(diff)} руб.")
        c.setFillColorRGB(0, 0, 0)
        c.setFont(reg, 8)
        y -= 5 * mm
        for i, chunk in enumerate(wrap(row["price_note"] or "", reg, 8, right - left)):
            if i:
                y -= 4 * mm
            c.drawString(left, y, chunk)
        y -= 5 * mm

    # --- отметки о согласовании ---
    y -= 4 * mm
    c.line(left, y, right, y)
    y -= 6 * mm
    c.setFont(bold, 10)
    c.drawString(left, y, "Согласовано в системе ЛогистМОТ")
    y -= 7 * mm
    for s in signatures(memo_id):
        c.setFont(reg, 9)
        c.drawString(left, y, s["title"])
        if s.get("who") and s.get("decision") != "rejected":
            c.setFont(bold, 9)
            c.drawString(left + 62 * mm, y, s["who"])
            c.setFont(reg, 9)
            c.drawString(left + 120 * mm, y, s["at"])
        elif s.get("decision") == "rejected":
            c.setFillColorRGB(.70, .23, .18)
            c.drawString(left + 62 * mm, y, f"отклонил(а) {s['who']} · {s['at']}")
            c.setFillColorRGB(0, 0, 0)
        else:
            c.setFillColorRGB(.55, .58, .66)
            c.drawString(left + 62 * mm, y, "ожидает согласования")
            c.setFillColorRGB(0, 0, 0)
        y -= 6 * mm
        if s.get("comment"):
            c.setFont(reg, 8)
            c.setFillColorRGB(.35, .38, .45)
            c.drawString(left + 62 * mm, y, f"«{s['comment'][:90]}»")
            c.setFillColorRGB(0, 0, 0)
            y -= 5 * mm

    st = STATUS_LABEL.get(row["status"], row["status"])
    c.setFont(reg, 7.5)
    c.setFillColorRGB(.45, .48, .55)
    c.drawString(left, 14 * mm,
                 f"Записка № {row['number']} · статус: {st} · "
                 f"отпечаток {fingerprint(memo_id)} · "
                 f"сформировано {time.strftime('%d.%m.%Y %H:%M')}")
    c.drawString(left, 10 * mm,
                 "Отметки заменяют подписи на бумаге: журнал решений хранится "
                 "в базе ЛогистМОТ. Правка файла ломает отпечаток.")
    c.showPage()
    c.save()
    return buf.getvalue()
