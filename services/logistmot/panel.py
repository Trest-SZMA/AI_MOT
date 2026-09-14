"""Живая панель ЛогистМОТ: http://localhost:8080

Запуск:  ./.venv/bin/python panel.py   (Windows: .venv\\Scripts\\python panel.py)

Читает bot.db и показывает реальные данные: заявки, отклики, телефоны.
Только стандартная библиотека — никаких лишних зависимостей.
Слушает 127.0.0.1 (только этот компьютер). Чтобы открыть доступ по офисной
сети: PANEL_HOST=0.0.0.0 в .env (учтите — без пароля, только для доверенной сети).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

import accounts
import db
import geo
import maxfmt as mf
import memo
import price

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

HOST = os.getenv("PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("PANEL_PORT", "8080"))
SILENT_N = 3  # «молчун» = не откликнулся на последние N заявок
# ссылка «домой» из панели — основной портал компании
PORTAL_URL = os.getenv("PORTAL_URL", "http://192.168.6.157:8079")
REPLY_WINDOW_SECONDS = int(float(os.getenv("REPLY_WINDOW_HOURS", "48")) * 3600)

# Доступ по паролю (обязателен, если панель видна не только с этого компьютера).
# Старый вариант — один общий вход:  PANEL_USER=logist  PANEL_PASSWORD=<пароль>
PANEL_USER = os.getenv("PANEL_USER", "logist")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")

# Персональные учётки с ролями. В .env одной строкой через запятую:
#   PANEL_USERS=login:пароль:роль:имя:max_id, ...
# роль: chief (аналитика) | head (согласует за отдел логистики)
#     | director (согласует за исп. директора) | logist
#
# Ролей может быть несколько — через «+»: «chief+head». Это не украшение:
# Елена и ведёт аналитику, и согласует записки первым шагом, а одна роль
# заставила бы выбирать между двумя её обязанностями.
_parse_users = accounts.parse_users


def has_role(who: dict, role: str) -> bool:
    return role in (who.get("roles") or [who.get("role")])


# Панель закрыта паролем: у каждого своя учётка, и записка подписывается его
# именем. PANEL_REQUIRE_LOGIN=0 вернёт прежний свободный вход для всей
# локальной сети (тогда записки снова будут подписываться «Логист»).
REQUIRE_LOGIN = os.getenv("PANEL_REQUIRE_LOGIN", "1") != "0"

# Что человек видит и куда его пускает API. Ключи — id вкладок в panel.html.
#   home  — очередь звонков (предложения)   new   — форма новой заявки
#   bc    — заявки                          memo  — служебные записки
#   users — перевозчики                     price — прайс
#   stats — аналитика по заявкам и логистам
#   memo_stats — сводка по запискам и превышению прайса
SECTIONS = {
    # логисты: всё, с чем работают каждый день, без аналитики
    "logist": {"home", "bc", "users", "new", "memo", "price"},
    # Елена: то же плюс согласование записок и статистика работы логистов
    "head": {"home", "bc", "users", "new", "memo", "price", "stats", "memo_stats"},
    # Пуганов: только согласование записок и сводка по превышениям
    "director": {"memo", "price", "memo_stats"},
    # администратор: всё
    "chief": {"home", "bc", "users", "new", "memo", "price", "stats", "memo_stats"},
    # документооборот (Колегова): только записки — выкачивает согласованные
    # и заводит их в 1С; ни заявок, ни прайса, ни аналитики
    "clerk": {"memo"},
}


# Какой раздел нужен для каждого действия. Пусто = доступно всем вошедшим
# (смена своего пароля, выход).
ACTION_SECTION = {
    "/api/create_zayavka": "new",
    "/api/raise_rate": "new",
    "/api/edit_broadcast": "home",
    "/api/delete_broadcast": "home",
    "/api/ask_phone": "home",
    "/api/send_info": "home",
    "/api/mark_done": "home",
    "/api/price_import": "price",
    "/api/memo_preview": "memo",
    "/api/memo_create": "new",       # записку создаёт тот, кто ведёт заявки
    "/api/memo_update": "new",
    "/api/memo_submit": "new",
    "/api/memo_decide": "memo",      # роль шага проверяется отдельно
    "/api/memo_attach": "memo",
    "/api/memo_file_delete": "memo",
}


def sections_for(who: dict) -> set:
    out: set = set()
    for role in (who.get("roles") or [who.get("role")] or []):
        out |= SECTIONS.get(role, set())
    return out


def can_see(who: dict, section: str) -> bool:
    return section in sections_for(who)


INITIALS = re.compile(r"(?:[А-ЯЁA-Z]\.){1,2}")


def short_name(full: str | None) -> str:
    """«Гамирзанова Светлана Александровна» → «Гамирзанова С. А.».

    Так подписывают служебные записки. Если в учётке уже короткая форма
    («Васильева Е.В.»), оставляем как есть — второй раз сокращать нечего.
    """
    parts = [p for p in (full or "").split() if p]
    if len(parts) < 2:
        return full or ""
    if any(INITIALS.fullmatch(p) for p in parts[1:]):
        return full
    return parts[0] + " " + " ".join(p[0].upper() + "." for p in parts[1:3])


PANEL_USERS = accounts.USERS

# --- вход формой (cookie), чтобы не зависеть от браузерного окна Basic-auth ---
SESSION_TTL = 12 * 3600
_SECRET = (os.getenv("PANEL_SECRET")
           or hashlib.sha256((os.getenv("MAX_BOT_TOKEN", "") +
                              os.getenv("PANEL_USERS", "")).encode()).hexdigest())


# --- смена пароля -----------------------------------------------------------
#
# Пароли задаются в `.env` открытым текстом — так их удобно раздавать при
# заведении учётки. Но человек должен уметь сменить свой пароль сам, и после
# смены хранить его открытым уже нельзя. Поэтому новый пароль ложится в базу
# ХЕШЕМ (PBKDF2-HMAC-SHA256, 200 000 итераций), а `.env` остаётся запасным
# входом: если в базе для логина записи нет — сверяем со строкой из `.env`.

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS panel_auth (
    login      TEXT PRIMARY KEY,   -- логин из PANEL_USERS
    salt       TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    changed_at INTEGER NOT NULL
);
"""
PBKDF_ROUNDS = 200_000


def init_auth():
    with db.connect() as conn:
        conn.executescript(AUTH_SCHEMA)


def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                               salt, PBKDF_ROUNDS).hex()


def set_password(login: str, password: str):
    salt = os.urandom(16)
    init_auth()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO panel_auth (login, salt, pw_hash, changed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(login) DO UPDATE SET
                   salt = excluded.salt, pw_hash = excluded.pw_hash,
                   changed_at = excluded.changed_at""",
            (login, salt.hex(), _hash_pw(password, salt), int(time.time())),
        )


def stored_hash(login: str):
    try:
        init_auth()
        with db.connect() as conn:
            return conn.execute(
                "SELECT salt, pw_hash FROM panel_auth WHERE login = ?", (login,)
            ).fetchone()
    except Exception:  # noqa: BLE001 — база недоступна: пустим по .env
        return None


def check_password(login: str, given: str) -> bool:
    """Сменённый пароль — из базы, иначе исходный из .env."""
    u = PANEL_USERS.get(login)
    if u is None:
        return False
    row = stored_hash(login)
    if row is not None:
        return hmac.compare_digest(_hash_pw(given, bytes.fromhex(row["salt"])),
                                   row["pw_hash"])
    return same_secret(given, u["password"])


PW_MIN = 8


def api_change_password(payload: dict, who: dict) -> tuple[bool, str, dict]:
    login = who.get("login")
    if not login or login == "guest":
        return False, "сначала войдите", {}
    current = str(payload.get("current") or "")
    new = str(payload.get("new") or "")
    again = str(payload.get("again") or "")
    if not check_password(login, current):
        time.sleep(0.4)
        return False, "текущий пароль не подходит", {}
    if len(new) < PW_MIN:
        return False, f"новый пароль короче {PW_MIN} символов", {}
    if new != again:
        return False, "новый пароль и повтор не совпадают", {}
    if new == current:
        return False, "новый пароль совпадает со старым", {}
    set_password(login, new)
    print(f"# пароль изменён: {who.get('name')} ({login})", flush=True)
    return True, "пароль изменён", {}


def same_secret(given: str, stored: str) -> bool:
    """Сравнение пароля за постоянное время.

    ⚠️ Через bytes, а не строками: `hmac.compare_digest` на строке с не-ASCII
    символами бросает TypeError. Пароль с кириллицей (а его вполне могут
    набрать по-русски или просто не той раскладкой) ронял обработчик запроса,
    и вместо «неверный пароль» человек видел «нет связи с сервером».
    """
    return hmac.compare_digest((given or "").encode("utf-8"),
                               (stored or "").encode("utf-8"))


def make_token(login: str) -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(_SECRET.encode(), f"{login}|{exp}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{login}|{exp}|{sig}".encode()).decode()


def read_token(token: str) -> str | None:
    try:
        login, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except Exception:  # noqa: BLE001
        return None
    want = hmac.new(_SECRET.encode(), f"{login}|{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, want) or int(exp) < time.time():
        return None
    return login
# общий вход logist/пароль больше не нужен — панель открыта всем в локальной
# сети, пароль спрашивается только у руководителей (PANEL_USERS с role=chief)


# --- действия панели: бот пишет человеку в личку ---

ASK_TEXT = (
    "Логист МетОптТорг хочет связаться с вами по вашему предложению.\n"
    "Напишите, пожалуйста, ваш номер телефона — он нужен, чтобы уточнить "
    "детали рейса. Например: 8 912 345-67-89"
)
DOCS_EMAIL = os.getenv("DOCS_EMAIL", "")
VEHICLE_EMAIL = os.getenv("VEHICLE_EMAIL", "")
ASK_COOLDOWN = 3600  # запрос телефона — не чаще раза в час на человека
_asked: dict[int, float] = {}


def _send_dm(user_id: int, text: str,
             buttons: list[tuple[str, str]] | None = None,
             md: bool = False) -> str | None:
    """Отправить личное сообщение от имени бота. None = успех, иначе ошибка.

    buttons — [(подпись, payload)], по кнопке в строке; нажатие обрабатывает
    bot.py (он один слушает события MAX). md — текст с разметкой maxfmt;
    тексты перевозчикам (почта с «_» внутри) шлём без неё."""
    async def _send():
        from maxapi import Bot
        bot = Bot(os.environ["MAX_BOT_TOKEN"])
        try:
            attachments = None
            if buttons:
                from maxapi.types import CallbackButton
                from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
                kb = InlineKeyboardBuilder()
                for label, payload in buttons:
                    kb.row(CallbackButton(text=label, payload=payload))
                attachments = [kb.as_markup()]
            if md:
                try:
                    await bot.send_message(user_id=user_id, text=text,
                                           attachments=attachments, parse_mode=mf.MD)
                    return
                except Exception:  # noqa: BLE001 — вдруг дело в разметке
                    text = mf.plain(text)
            await bot.send_message(user_id=user_id, text=text,
                                   attachments=attachments)
        finally:
            for attr in ("session", "_session"):
                s = getattr(bot, attr, None)
                if s is not None and hasattr(s, "close"):
                    try:
                        await s.close()
                    except Exception:  # noqa: BLE001
                        pass
    try:
        asyncio.run(_send())
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)


def _get_user(user_id: int):
    with db.connect() as conn:
        return conn.execute(
            "SELECT name, phone, active FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def ask_phone(user_id: int) -> tuple[bool, str]:
    row = _get_user(user_id)
    if row is None:
        return False, "человек не найден в базе"
    if row["phone"]:
        return False, "телефон уже есть"
    if not row["active"]:
        return False, "не подписан на бота — в личку писать нельзя (правило MAX)"
    if time.time() - _asked.get(user_id, 0) < ASK_COOLDOWN:
        return False, "уже запрашивали недавно — подождите час"
    err = _send_dm(user_id, ASK_TEXT)
    if err:
        return False, f"не доставлено: {err}"
    _asked[user_id] = time.time()
    print(f"# запрос телефона отправлен: {user_id} {row['name']}", flush=True)
    return True, "отправлено"


def _delete_chat_message(mid: str):
    async def _run():
        from maxapi import Bot
        bot = Bot(os.environ["MAX_BOT_TOKEN"])
        try:
            await bot.delete_message(mid)
        finally:
            for attr in ("session", "_session"):
                s = getattr(bot, attr, None)
                if s is not None and hasattr(s, "close"):
                    try:
                        await s.close()
                    except Exception:  # noqa: BLE001
                        pass
    try:
        asyncio.run(_run())
    except Exception:  # noqa: BLE001
        pass


def _edit_chat_message(mid: str, text: str, bid: int, kind: str | None) -> bool:
    """Переписать карточку заявки в чате. Кнопку «Готов взять» отдаём заново:
    MAX заменяет вложения целиком, без неё отклик из чата пропал бы."""
    ok = False

    async def _run():
        nonlocal ok
        from maxapi import Bot
        from maxapi.enums.parse_mode import ParseMode
        from maxapi.types import CallbackButton
        from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
        import zayavka
        cfg = zayavka.KINDS.get(kind or "normal", zayavka.KINDS["normal"])
        btn = CallbackButton(text=cfg["button"], payload=f"take:{bid}")
        if cfg["intent"]:
            btn.intent = cfg["intent"]
        kb = InlineKeyboardBuilder().add(btn).as_markup()
        pmode = ParseMode.MARKDOWN if (zayavka.USE_MARKDOWN and cfg["emoji"]) else None
        bot = Bot(os.environ["MAX_BOT_TOKEN"])
        try:
            try:
                await bot.edit_message(mid, text=text, attachments=[kb],
                                       parse_mode=pmode)
            except Exception:  # noqa: BLE001 — вдруг дело в разметке
                await bot.edit_message(mid, text=text, attachments=[kb])
            ok = True
        finally:
            for attr in ("session", "_session"):
                s = getattr(bot, attr, None)
                if s is not None and hasattr(s, "close"):
                    try:
                        await s.close()
                    except Exception:  # noqa: BLE001
                        pass
    try:
        asyncio.run(_run())
    except Exception:  # noqa: BLE001
        pass
    return ok


def _refresh_digest():
    """Актуализировать закреплённый дайджест в чате (после правок с панели)."""
    async def _run():
        from maxapi import Bot
        import zayavka
        bot = Bot(os.environ["MAX_BOT_TOKEN"])
        try:
            await zayavka.refresh_digest(bot)
        finally:
            for attr in ("session", "_session"):
                s = getattr(bot, attr, None)
                if s is not None and hasattr(s, "close"):
                    try:
                        await s.close()
                    except Exception:  # noqa: BLE001
                        pass
    try:
        asyncio.run(_run())
    except Exception:  # noqa: BLE001
        pass


def _actor(payload: dict, who: dict) -> tuple[int | None, str | None]:
    """От чьего аккаунта MAX действие: выбор в панели → учётка → None."""
    env_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    accounts = {a["user_id"]: a["name"] for a in db.logist_accounts(env_ids)}
    raw = payload.get("actor_id")
    try:
        aid = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        aid = None
    if aid in accounts:
        return aid, accounts[aid]
    if who.get("max_id") in accounts:       # руководитель под своей учёткой
        return who["max_id"], accounts[who["max_id"]]
    return None, None


def api_create_zayavka(payload: dict, who: dict) -> tuple[bool, str]:
    """Создать и разослать заявку прямо с панели (чат + личка + кнопки)."""
    fields = {k: (payload.get(k) or "").strip()
              for k in ("route", "dates", "cargo", "rate", "ts", "load",
                        "unload", "note", "contacts", "important")}
    fields = {k: v for k, v in fields.items() if v}
    if not fields.get("route"):
        return False, "укажите маршрут"
    actor_id, actor_name = _actor(payload, who)
    kind = payload.get("kind") or "auto"

    result: dict = {}

    async def _run():
        from maxapi import Bot
        import zayavka
        bot = Bot(os.environ["MAX_BOT_TOKEN"])
        try:
            fixed, fixes = zayavka.fix_typos(fields)
            result["summary"] = await zayavka.dispatch_zayavka(
                bot, fixed, author_id=actor_id, author_name=actor_name, kind=kind)
            result["fixes"] = fixes
        finally:
            for attr in ("session", "_session"):
                s = getattr(bot, attr, None)
                if s is not None and hasattr(s, "close"):
                    try:
                        await s.close()
                    except Exception:  # noqa: BLE001
                        pass

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return False, f"не отправилось: {exc}"
    msg = result.get("summary", "отправлено")
    if result.get("fixes"):
        msg += " · поправлены опечатки: " + "; ".join(result["fixes"][:3])
    print(f"# заявка с панели ({who.get('name')}): {msg}", flush=True)
    return True, msg


def api_raise_rate(payload: dict, who: dict) -> tuple[bool, str]:
    """Поднять ставку у заявки: публикуем обновление «было → стало»."""
    try:
        bid = int(payload.get("bid"))
    except (TypeError, ValueError):
        return False, "не указана заявка"
    b = db.get_broadcast(bid)
    if b is None:
        return False, "заявка не найдена"
    new_rate = (payload.get("rate") or "").strip()
    if not new_rate:
        return False, "укажите новую ставку"

    fields = {k: b[k] for k in ("route", "dates", "cargo") if b[k]}
    fields["rate"] = new_rate
    for extra in ("ts", "contacts"):
        val = (payload.get(extra) or "").strip()
        if val:
            fields[extra] = val
    actor_id, actor_name = _actor(payload, who)
    result: dict = {}

    async def _run():
        from maxapi import Bot
        import zayavka
        bot = Bot(os.environ["MAX_BOT_TOKEN"])
        try:
            result["summary"] = await zayavka.dispatch_zayavka(
                bot, fields, author_id=actor_id, author_name=actor_name,
                kind="rate_up")
            # старую версию заявки убираем: в чате остаётся актуальная ставка
            db.set_broadcast_closed(bid)
            if b["chat_mid"]:
                try:
                    await bot.delete_message(b["chat_mid"])
                except Exception:  # noqa: BLE001
                    pass
                db.clear_chat_mid(bid)
            await zayavka.refresh_digest(bot)
        finally:
            for attr in ("session", "_session"):
                s = getattr(bot, attr, None)
                if s is not None and hasattr(s, "close"):
                    try:
                        await s.close()
                    except Exception:  # noqa: BLE001
                        pass

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return False, f"не отправилось: {exc}"
    print(f"# ставка поднята по #{bid} ({actor_name or who.get('name')}): {new_rate}",
          flush=True)
    return True, result.get("summary", "отправлено") + " · старая версия убрана из чата"


def api_delete_broadcast(bid: int) -> tuple[bool, str]:
    """«Удаление» с панели = перенос в архив: из чата и активных убирается,
    отклики и статистика сохраняются."""
    b = db.get_broadcast(bid)
    if b is None:
        return False, "заявка не найдена"
    db.set_broadcast_closed(bid)
    if b["chat_mid"]:
        _delete_chat_message(b["chat_mid"])
        db.clear_chat_mid(bid)
    _refresh_digest()
    print(f"# заявка #{bid} перенесена в архив с панели", flush=True)
    return True, "перенесена в архив (из чата и закрепа убрана)"


def api_edit_broadcast(bid: int, payload: dict) -> tuple[bool, str]:
    ok = db.update_broadcast(
        bid,
        (payload.get("route") or "").strip() or None,
        (payload.get("dates") or "").strip() or None,
        (payload.get("cargo") or "").strip() or None,
        (payload.get("rate") or "").strip() or None,
    )
    if not ok:
        return False, "заявка не найдена"
    _refresh_digest()
    # карточка в чате должна совпадать с закрепом: правим её на месте
    import zayavka
    b = db.get_broadcast(bid)
    card = zayavka.retext_card(
        b["text"] or "",
        route=b["route"], dates=b["dates"], cargo=b["cargo"], rate=b["rate"])
    if card != (b["text"] or ""):
        db.set_broadcast_text(bid, card)
    chat = ""
    if b["chat_mid"]:
        chat = (" и карточка в чате" if _edit_chat_message(
            b["chat_mid"], card, bid, b["kind"]) else
            ", но карточку в чате обновить не удалось")
    print(f"# заявка #{bid} отредактирована с панели", flush=True)
    return True, f"сохранено, обновлён закреп{chat}"


def send_info(user_id: int, kind: str) -> tuple[bool, str]:
    """Отправить перевозчику адрес почты: закрывающие документы / данные по ТС."""
    row = _get_user(user_id)
    if row is None:
        return False, "человек не найден в базе"
    if not row["active"]:
        return False, "не подписан на бота — в личку писать нельзя"
    if kind == "docs":
        if not DOCS_EMAIL:
            return False, "почта не настроена (DOCS_EMAIL в .env)"
        text = ("Здравствуйте! Закрывающие документы по рейсу отправьте, "
                f"пожалуйста, на почту: {DOCS_EMAIL}")
    elif kind == "vehicle":
        if not VEHICLE_EMAIL:
            return False, "почта не настроена (VEHICLE_EMAIL в .env)"
        text = ("Здравствуйте! Данные по машине и водителю (марка и госномер ТС, "
                "ФИО водителя, телефон) отправьте, пожалуйста, на почту: "
                f"{VEHICLE_EMAIL}")
    else:
        return False, "неизвестный тип"
    err = _send_dm(user_id, text)
    if err:
        return False, f"не доставлено: {err}"
    print(f"# отправлен адрес ({kind}): {user_id} {row['name']}", flush=True)
    return True, "отправлено"


# --- прайс транспортных ставок -------------------------------------------

# Смотреть прайс могут все, кто дошёл до панели: логисту он нужен, чтобы
# понимать, укладывается ли ставка рейса в согласованные цены.
# А вот перезаливать выгрузку — только руководители: подменённый прайс тихо
# перекосил бы согласование записок и всю аналитику по превышению ставок.
PRICE_ROLES = ("chief", "head", "director")


def can_import_price(who: dict) -> bool:
    return any(has_role(who, r) for r in PRICE_ROLES)


def api_price() -> dict:
    return {
        "status": price.price_status(),
        "docs": price.docs_summary(),
        "rates": price.effective_rates(),
    }


def api_price_import(payload: dict, who: dict) -> tuple[bool, str]:
    if not can_import_price(who):
        return False, "загрузка прайса — только для руководителей (войдите по паролю)"
    text = payload.get("csv") or ""
    if not text.strip():
        return False, "пустой файл"
    try:
        res = price.import_csv(text, source="csv")
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"не разобрал файл: {exc}"
    st = price.price_status()
    msg = (f"загружено: документов {res['docs']}, строк {res['rates']}, "
           f"маршрутов {res['routes']}")
    if st.get("note"):
        msg += " · " + st["note"]
    print(f"# прайс загружен ({who.get('name')}): {msg}", flush=True)
    return True, msg


# --- служебные записки ----------------------------------------------------

def memo_actor(who: dict) -> tuple[str, str | None]:
    """Чьим именем подписывать действие в записке — короткой формой ФИО."""
    return who.get("short") or who.get("name") or "Логист", who.get("login")


def api_memos(who: dict, query: str = "") -> dict:
    from urllib.parse import parse_qs
    q = parse_qs(query)
    status = (q.get("status") or [""])[0]
    roles = who.get("roles") or [who.get("role")]
    items = memo.list_memos(status or None, limit=200)
    return {
        "memos": items,
        "role": who.get("role"),
        "roles": roles,
        # что ждёт решения лично этого человека
        "my_queue": [i["id"] for i in items
                     if memo.STEP_ROLE.get(i["status"]) in roles],
        "statuses": memo.STATUS_LABEL,
    }


def api_memo_preview(payload: dict, who: dict) -> tuple[bool, str, dict]:
    """Отклонение от прайса прямо в форме — логист видит его до отправки."""
    return True, "", memo.preview(payload)


def api_memo_create(payload: dict, who: dict) -> tuple[bool, str, dict]:
    if not (payload.get("route") or "").strip():
        return False, "укажите маршрут", {}
    author, login = memo_actor(who)
    # исполнителя логист может подписать своим ФИО: панель открыта без пароля,
    # и «Логист» в записке никому ничего не скажет
    author = (payload.get("author") or "").strip() or author
    submit = payload.get("submit") is not False
    mid = memo.create(payload, author=author, login=login, submit=submit)
    row = memo.as_dict(memo.get(mid))
    try:    # записка уже создана — уведомление не может её «отменить»
        print(f"# записка {row['number']} создана ({author}): "
              f"{row['route']} · {row['status_label']}", flush=True)
        notify_memo(row, "submitted")
    except Exception as exc:  # noqa: BLE001
        print(f"# записка {row['number']}: уведомление не ушло: {exc}", flush=True)
    return True, f"записка {row['number']} " + (
        "подана на согласование" if submit else "сохранена черновиком"), {"id": mid}


def api_memo_update(payload: dict, who: dict) -> tuple[bool, str, dict]:
    mid = int(payload.get("id") or 0)
    if not memo.update(mid, payload):
        return False, "правка возможна только у черновика или отклонённой записки", {}
    return True, "сохранено", {"id": mid}


def api_memo_submit(payload: dict, who: dict) -> tuple[bool, str, dict]:
    mid = int(payload.get("id") or 0)
    author, login = memo_actor(who)
    # ФИО исполнителя берём из самой записки: панель открыта без пароля, и у
    # сессии логиста имя всегда «Логист» — оно затёрло бы подпись в PDF
    row = memo.get(mid)
    if row is not None and row["author"]:
        author = row["author"]
    ok, msg = memo.submit(mid, author, login)
    if ok:
        try:
            notify_memo(memo.as_dict(memo.get(mid)), "submitted")
        except Exception as exc:  # noqa: BLE001
            print(f"# записка #{mid}: уведомление не ушло: {exc}", flush=True)
    return ok, msg, {"id": mid}


def api_memo_decide(payload: dict, who: dict) -> tuple[bool, str, dict]:
    """Решение согласующего. Роль берётся из шага записки, а не из запроса."""
    mid = int(payload.get("id") or 0)
    row = memo.get(mid)
    if row is None:
        return False, "записка не найдена", {}
    # роль определяет ШАГ записки, а не запрос: иначе человек с обеими ролями
    # мог бы закрыть за один клик оба согласования
    step_role = memo.STEP_ROLE.get(row["status"])
    if step_role is None:
        return False, "по этой записке решение уже принято", {}
    if not has_role(who, step_role):
        return False, "сейчас записку согласует не вы", {}
    approve = bool(payload.get("approve"))
    ok, msg = memo.decide(mid, step_role, who.get("name") or step_role,
                          who.get("login"), approve, payload.get("comment") or "")
    if ok:
        # ⚠️ Решение уже записано в базу. Сбой лога или уведомления в MAX не
        # должен возвращать «ошибка»: человек нажмёт ещё раз, а шаг закрыт —
        # и он решит, что панель сломана.
        try:
            row = memo.as_dict(memo.get(mid))
            print(f"# записка {row['number']}: {who.get('name')} ({step_role}) — {msg}",
                  flush=True)
            notify_memo(row, "approved" if approve else "rejected")
        except Exception as exc:  # noqa: BLE001
            print(f"# записка #{mid}: решение записано, но уведомление не ушло: {exc}",
                  flush=True)
    return ok, msg, {"id": mid}


PANEL_LINK = os.getenv("PANEL_LINK", f"http://192.168.6.157:{PORT}")


def api_memo_attach(payload: dict, who: dict) -> tuple[bool, str, dict]:
    """Приложить лист согласования (файл приходит base64 из браузера)."""
    import base64
    try:
        mid = int(payload.get("id") or 0)
        raw = base64.b64decode(str(payload.get("data") or ""), validate=True)
    except Exception:  # noqa: BLE001
        return False, "файл не дошёл (повреждён при передаче)", {}
    ok, msg = memo.attach_file(mid, str(payload.get("filename") or ""),
                               str(payload.get("mime") or ""), raw,
                               who.get("short") or who.get("name"))
    if ok:
        print(f"# записка #{mid}: {who.get('name')} приложил файл "
              f"{payload.get('filename')!r} ({len(raw)} байт)", flush=True)
    return ok, msg, {"id": mid}


def api_memo_file_delete(payload: dict, who: dict) -> tuple[bool, str, dict]:
    fid = int(payload.get("file_id") or 0)
    row = memo.get_file(fid)
    if row is None:
        return False, "файл не найден", {}
    memo.delete_file(fid)
    print(f"# файл {row['filename']!r} записки #{row['memo_id']} удалён "
          f"({who.get('name')})", flush=True)
    return True, "файл удалён", {}


def notify_memo(row: dict, event: str):
    """Уведомить в MAX того, чей сейчас ход. Молча пропускаем, если человек
    не подписан на бота: бот не может писать первым (ограничение MAX)."""
    n = memo.notice(row, event, PANEL_LINK)
    if n is None:
        return  # логисту ответ виден в панели; в личку его не дёргаем
    role = n["role"]
    targets = accounts.role_max_ids(role)
    if not targets:
        # у согласующего не задан max_id: чаще всего он ещё не нажал «Начать»
        # у бота. Пишем в журнал ЧТО делать, а не просто «не ушло».
        print(f"# записка {row['number']}: у роли {role} не задан max_id — "
              f"уведомление в MAX не отправлено. Когда человек нажмёт «Начать» "
              f"у бота, его id появится в журнале logistmot-bot "
              f"(строка «+ подписчик лички»); добавьте id пятым полем его "
              f"учётки в PANEL_USERS.", flush=True)
        return
    for uid in targets:
        err = _send_dm(uid, n["text"], n["buttons"], md=True)
        if err:
            print(f"# записка {row['number']}: уведомление {uid} не ушло ({err})",
                  flush=True)
        else:
            print(f"# записка {row['number']}: уведомление ушло в MAX ({role}, "
                  f"id {uid})", flush=True)


_role_max_ids = accounts.role_max_ids


def admin_ids(conn) -> list[int]:
    """Логисты (из .env и назначенные) — их сообщения не показываем в ленте."""
    ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    try:
        ids |= {r["user_id"] for r in conn.execute("SELECT user_id FROM admins")}
    except Exception:  # noqa: BLE001 — таблицы может не быть в старой базе
        pass
    return sorted(ids) or [0]


def collect_data() -> dict:
    with db.connect() as conn:
        admins = admin_ids(conn)
        adm_ph = ",".join("?" * len(admins))
        users = conn.execute(
            f"""SELECT u.user_id, u.name, u.phone, u.active, u.username,
                  (SELECT COUNT(*) FROM deliveries d
                     WHERE d.user_id = u.user_id AND d.sent_ok = 1)              AS got,
                  (SELECT COUNT(DISTINCT r.broadcast_id) FROM replies r
                     WHERE r.user_id = u.user_id AND r.relevant = 1
                       AND r.broadcast_id IS NOT NULL)                           AS replied
               FROM users u
               WHERE u.user_id NOT IN ({adm_ph})
               ORDER BY replied DESC, got DESC""",
            admins,
        ).fetchall()

        broadcasts = conn.execute(
            f"""SELECT b.id, b.route, b.dates, b.cargo, b.rate, b.sent_at, b.chat_posted,
                  b.closed, b.kind,
                  (SELECT COUNT(*) FROM deliveries d
                     WHERE d.broadcast_id = b.id AND d.sent_ok = 1)              AS dm_sent,
                  (SELECT COUNT(DISTINCT r.user_id) FROM replies r
                     WHERE r.broadcast_id = b.id AND r.relevant = 1
                       AND r.user_id NOT IN ({adm_ph}))                          AS responders
               FROM broadcasts b ORDER BY b.sent_at DESC LIMIT 30""",
            admins,
        ).fetchall()

        # две группы с раздельными лимитами: иначе отмеченная «обработано»
        # запись вылетает за общий лимит и пропадает с панели
        feed_sql = f"""SELECT r.id AS rid, r.at, r.text, r.source, r.offer, r.processed,
                      u.name, u.phone, u.user_id, u.username, u.active,
                      b.route, b.id AS bid
               FROM replies r
               JOIN users u ON u.user_id = r.user_id
               LEFT JOIN broadcasts b ON b.id = r.broadcast_id
               WHERE r.relevant = 1
                 AND r.user_id NOT IN ({adm_ph})
                 AND r.processed = ?
               ORDER BY {{order}} LIMIT ?"""
        pending = conn.execute(feed_sql.format(order="r.at DESC"),
                               admins + [0, 60]).fetchall()
        done = conn.execute(
            feed_sql.format(order="COALESCE(r.processed_at, r.at) DESC"),
            admins + [1, 40]).fetchall()
        feed = list(pending) + list(done)

        last_ids = [r["id"] for r in broadcasts[:SILENT_N]]
        silent = []
        if last_ids:
            ph = ",".join("?" * len(last_ids))
            silent = conn.execute(
                f"""SELECT u.user_id, u.name, u.phone,
                           COUNT(d.broadcast_id) AS got
                    FROM users u JOIN deliveries d
                      ON d.user_id = u.user_id AND d.sent_ok = 1
                     AND d.broadcast_id IN ({ph})
                    WHERE u.active = 1
                      AND u.user_id NOT IN ({adm_ph})
                      AND NOT EXISTS (SELECT 1 FROM replies r
                                       WHERE r.user_id = u.user_id AND r.relevant = 1
                                         AND r.broadcast_id IN ({ph}))
                    GROUP BY u.user_id ORDER BY got DESC LIMIT 15""",
                last_ids + admins + last_ids,
            ).fetchall()

    subs_active = sum(1 for u in users if u["active"])
    phones = sum(1 for u in users if u["phone"])
    env_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    logists = db.logist_accounts(env_ids)

    with db.connect() as conn:
        row = conn.execute("SELECT MAX(at) AS m FROM replies").fetchone()
        last_ev = row["m"] if row and row["m"] else None

    return {
        "portal_url": PORTAL_URL,
        "logists": logists,
        "generated_at": time.strftime("%d.%m.%Y %H:%M:%S"),
        "last_event": time.strftime("%d.%m %H:%M", time.localtime(last_ev)) if last_ev else None,
        "last_event_age_min": int((time.time() - last_ev) / 60) if last_ev else None,
        "kpi": {
            "people": len(users),
            "subscribed": subs_active,
            "zayavki": len(broadcasts),
            "phones": phones,
            "silent": len(silent),
        },
        "broadcasts": [
            {
                "id": b["id"], "route": b["route"], "dates": b["dates"],
                "kind": b["kind"],
                "cargo": b["cargo"], "rate": b["rate"],
                "sent": time.strftime("%d.%m %H:%M", time.localtime(b["sent_at"])),
                "chat": bool(b["chat_posted"]), "dm_sent": b["dm_sent"],
                "responders": b["responders"],
                "status": ("закрыта" if b["closed"]
                           else "активна" if db.broadcast_is_active(b, REPLY_WINDOW_SECONDS)
                           else "истекла"),
            } for b in broadcasts
        ],
        "feed": [
            {
                "rid": r["rid"], "processed": bool(r["processed"]),
                "at": time.strftime("%d.%m %H:%M", time.localtime(r["at"])),
                "user_id": r["user_id"], "active": bool(r["active"]),
                "name": r["name"] or str(r["user_id"]), "phone": r["phone"],
                "username": r["username"], "offer": r["offer"],
                "source": r["source"], "text": r["text"][:300],
                "route": r["route"], "bid": r["bid"],
            } for r in feed
        ],
        "silent": [
            {"name": s["name"] or str(s["user_id"]), "phone": s["phone"], "got": s["got"]}
            for s in silent
        ],
        "users": [
            {
                "user_id": u["user_id"],
                "name": u["name"] or str(u["user_id"]), "phone": u["phone"],
                "username": u["username"],
                "active": bool(u["active"]), "got": u["got"], "replied": u["replied"],
            } for u in users
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # тише в консоли
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _as_user(self, login: str) -> dict | None:
        u = PANEL_USERS.get(login)
        if u is None:
            return None
        who = {"login": login, "name": u["name"], "role": u["role"],
               "roles": u["roles"], "max_id": u["max_id"]}
        who["short"] = short_name(u["name"])
        who["sections"] = sorted(sections_for(who))
        return who

    def _user(self) -> dict:
        """Кто пришёл. Панель открыта всем в локальной сети (роль «логист»),
        пароль нужен только для функций руководителя."""
        guest = {"login": "guest", "name": "Логист", "role": "logist",
                 "roles": ["logist"], "max_id": None, "short": "Логист",
                 "sections": sorted(SECTIONS["logist"])}

        # 1) вход формой — так входят руководители
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "lm_session" and v:
                login = read_token(v)
                if login:
                    who = self._as_user(login)
                    if who:
                        return who

        # 2) браузерный Basic-auth — на случай сохранённых учёток
        raw = self.headers.get("Authorization", "")
        if raw.startswith("Basic "):
            try:
                login, _, pwd = base64.b64decode(raw[6:]).decode().partition(":")
            except Exception:  # noqa: BLE001
                return guest
            u = PANEL_USERS.get(login)
            if u is not None and check_password(login, pwd):
                return self._as_user(login) or guest
        return guest

    ROLE_LABEL = {"chief": "администратор",
                  "head": "руководитель отдела логистики",
                  "director": "исполнительный директор",
                  "clerk": "специалист по документообороту",
                  "logist": "логист"}
    ROLE_ORDER = {"logist": 0, "clerk": 1, "head": 2, "director": 3, "chief": 4}

    def _users_list(self) -> list[dict]:
        """Кого показать на экране входа. Пароли сюда не попадают."""
        out = []
        for login, u in PANEL_USERS.items():
            out.append({
                "login": login, "name": u["name"],
                "role": u["role"],
                "role_label": " · ".join(self.ROLE_LABEL.get(r, r) for r in u["roles"]),
            })
        out.sort(key=lambda x: (self.ROLE_ORDER.get(x["role"], 9), x["name"]))
        return out

    def _need_login(self) -> bool:
        return REQUIRE_LOGIN

    def _unauthorized(self):
        self._send(401, json.dumps({"error": "нужно войти", "login_required": True},
                                   ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self):
        who = self._user()
        # экран входа должен знать, из кого выбирать, — это единственное,
        # что отдаётся без пароля
        if self.path.startswith("/api/users_list"):
            self._send(200, json.dumps({"users": self._users_list(),
                                        "require_login": REQUIRE_LOGIN},
                                       ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if (self._need_login() and who["login"] == "guest"
                and self.path.startswith("/api/")):
            self._unauthorized()
            return
        if self.path.split("?")[0] == "/api/geo":
            # подсказка города с регионом для формы заявки
            # (ровно /api/geo: startswith съедал бы и /api/geo_reverse)
            if not can_see(who, "new"):
                self._send(403, json.dumps({"error": "этот раздел вам не доступен"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            from urllib.parse import parse_qs
            _, _, query = self.path.partition("?")
            q = (parse_qs(query).get("q") or [""])[0]
            res = geo.suggest(q)
            for it in res["items"]:
                it["label"] = geo.label(it)
            self._send(200, json.dumps(res, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if self.path.split("?")[0] == "/api/geo_reverse":
            if not can_see(who, "new"):
                self._send(403, json.dumps({"error": "этот раздел вам не доступен"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            from urllib.parse import parse_qs
            _, _, query = self.path.partition("?")
            q = parse_qs(query)
            try:
                lat, lon = float(q["lat"][0]), float(q["lon"][0])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (KeyError, ValueError, IndexError):
                self._send(400, b'{"error":"lat/lon"}', "application/json")
                return
            try:
                res = geo.reverse(lat, lon)
            except Exception as exc:  # noqa: BLE001
                res = {"name": "", "label": "", "error": str(exc)[:120]}
            self._send(200, json.dumps(res, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/stats"):
            if not can_see(who, "stats"):
                self._send(403, json.dumps({"error": "только для руководителя"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            body = json.dumps(db.stats_summary(30), ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/memo_stats"):
            # сводка по запискам — руководителям: Павел, Елена, Пуганов
            if not can_see(who, "memo_stats"):
                self._send(403, json.dumps({"error": "только для руководителей"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            from urllib.parse import parse_qs
            _, _, query = self.path.partition("?")
            q = parse_qs(query)
            try:
                days = int((q.get("days") or ["90"])[0])
            except ValueError:
                days = 90
            route = (q.get("route") or [""])[0]
            try:
                body = json.dumps(memo.stats(days, route or None),
                                  ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/memo_sources"):
            if not can_see(who, "new"):
                self._send(403, json.dumps({"error": "недоступно"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            try:
                body = json.dumps({"broadcasts": memo.choices()},
                                  ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/memo_prefill"):
            from urllib.parse import parse_qs
            _, _, query = self.path.partition("?")
            try:
                bid = int((parse_qs(query).get("bid") or ["0"])[0])
            except ValueError:
                bid = 0
            data = memo.prefill(bid) if bid else {}
            if not data:
                self._send(404, json.dumps({"error": "заявка не найдена"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/memos"):
            _, _, query = self.path.partition("?")
            try:
                body = json.dumps(api_memos(who, query), ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/memo_pdf"):
            from urllib.parse import parse_qs
            _, _, query = self.path.partition("?")
            try:
                mid = int((parse_qs(query).get("id") or ["0"])[0])
                row = memo.get(mid)
                if row is None:
                    raise ValueError("записка не найдена")
                data = memo.pdf(mid, with_price=True)
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            # ⚠️ Заголовки HTTP — latin-1. «СЗ-2026-0001.pdf» кириллицей в
            # filename роняет send_header, поэтому ASCII-имя как запасное и
            # настоящее — в filename* по RFC 5987.
            from urllib.parse import quote
            # Имя файла — номер И маршрут: в папке «Загрузки» десяток
            # «СЗ-2026-000N.pdf» неразличим, а Мария заводит их в 1С пачкой.
            nice = " ".join(x for x in (row["number"],
                                        (row["route"] or "").replace("→", "-"),
                                        row["work_date"] or "") if x)
            nice = re.sub(r"[\\/:*?\"<>|]", "-", nice).strip() or str(mid)
            ascii_name = re.sub(r"[^0-9A-Za-z._-]+", "-",
                                "SZ-" + (row["number"] or str(mid))).strip("-")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}.pdf"; '
                f"filename*=UTF-8''{quote(nice)}.pdf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/memo_file?"):
            from urllib.parse import parse_qs, quote
            q = parse_qs(self.path.partition("?")[2])
            row = memo.get_file(int((q.get("id") or ["0"])[0]) or 0)
            if row is None:
                self._send(404, b"not found", "text/plain")
                return
            data = row["data"]
            ascii_name = re.sub(r"[^0-9A-Za-z._-]", "_", row["filename"]) or "file"
            self.send_response(200)
            self.send_header("Content-Type",
                             row["mime"] or "application/octet-stream")
            # кириллица в заголовке — те же грабли, что у PDF (latin-1)
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(row['filename'])}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/price"):
            if not can_see(who, "price"):
                self._send(403, json.dumps({"error": "прайс вам не доступен"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            try:
                data = api_price()
                data["can_import"] = can_import_price(who)
                self._send(200, json.dumps(data, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/data"):
            try:
                data = collect_data()
                # исп. директору лента откликов, заявки и перевозчики не
                # положены: прятать вкладку мало, данные не должны и уезжать
                if not can_see(who, "home"):
                    for key in ("feed", "broadcasts", "users", "silent"):
                        data[key] = []
                    data["kpi"] = {}   # счётчики людей и телефонов — тоже не его
                data["me"] = who
                body = json.dumps(data, ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(BASE, "panel.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/static/"):
            # библиотека карты (Leaflet) лежит рядом с панелью: без внешних
            # CDN, снаружи грузятся только тайлы OpenStreetMap
            name = os.path.basename(self.path.split("?")[0])
            path = os.path.join(BASE, "static", name)
            ctype = {"js": "application/javascript", "css": "text/css",
                     "png": "image/png"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
            if name and os.path.isfile(path):
                with open(path, "rb") as f:
                    self._send(200, f.read(), ctype + ("; charset=utf-8" if ctype != "image/png" else ""))
            else:
                self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # вход формой доступен без авторизации — иначе не войти
        if self.path == "/api/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                p = json.loads(self.rfile.read(length) or b"{}")
                login = str(p.get("login", "")).strip()
                pwd = str(p.get("password", ""))
            except Exception:  # noqa: BLE001
                login, pwd = "", ""
            u = PANEL_USERS.get(login)
            if u is None or not check_password(login, pwd):
                time.sleep(0.4)  # притормозить подбор
                self._send(401, json.dumps({"ok": False, "message": "неверный логин или пароль"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            body = json.dumps({"ok": True, "name": u["name"], "role": u["role"]},
                              ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie",
                             f"lm_session={make_token(login)}; Path=/; Max-Age={SESSION_TTL}; "
                             "SameSite=Lax")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"# вход в панель: {u['name']} ({u['role']})", flush=True)
            return
        if self.path == "/api/logout":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "lm_session=; Path=/; Max-Age=0; SameSite=Lax")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        who = self._user()
        if self._need_login() and who["login"] == "guest":
            self._unauthorized()
            return
        # ⚠️ Права проверяются НА ДЕЙСТВИИ, а не только прятаньем вкладок.
        # Поймано на живом примере: у специалиста по документообороту нет
        # раздела «Новая заявка», но POST /api/create_zayavka проходил — и
        # заявка ушла в боевой чат 185 перевозчикам.
        need = ACTION_SECTION.get(self.path)
        if need and not can_see(who, need):
            self._send(403, json.dumps(
                {"ok": False, "message": "этот раздел вам не доступен"},
                ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            payload = None
        if self.path == "/api/ask_phone" and payload and "user_id" in payload:
            ok, msg = ask_phone(int(payload["user_id"]))
        elif self.path == "/api/send_info" and payload and "user_id" in payload:
            ok, msg = send_info(int(payload["user_id"]), str(payload.get("kind", "")))
        elif self.path == "/api/mark_done" and payload and "reply_id" in payload:
            done = bool(payload.get("done", True))
            _, actor_name = _actor(payload, who)
            ok = db.set_reply_processed(int(payload["reply_id"]), done,
                                        by=actor_name or who.get("name"))
            msg = ("обработано" if done else "возвращено") if ok else "не найдено"
        elif self.path == "/api/create_zayavka" and payload:
            ok, msg = api_create_zayavka(payload, who)
        elif self.path == "/api/raise_rate" and payload:
            ok, msg = api_raise_rate(payload, who)
        elif self.path == "/api/delete_broadcast" and payload and "bid" in payload:
            ok, msg = api_delete_broadcast(int(payload["bid"]))
        elif self.path == "/api/edit_broadcast" and payload and "bid" in payload:
            ok, msg = api_edit_broadcast(int(payload["bid"]), payload)
        elif self.path == "/api/price_import" and payload:
            ok, msg = api_price_import(payload, who)
        elif self.path == "/api/change_password" and payload is not None:
            ok, msg, _ = api_change_password(payload, who)
        elif self.path.startswith("/api/memo_") and payload is not None:
            handler = {
                "/api/memo_preview": api_memo_preview,
                "/api/memo_create": api_memo_create,
                "/api/memo_update": api_memo_update,
                "/api/memo_submit": api_memo_submit,
                "/api/memo_decide": api_memo_decide,
                "/api/memo_attach": api_memo_attach,
                "/api/memo_file_delete": api_memo_file_delete,
            }.get(self.path)
            if handler is None:
                self._send(404, json.dumps({"ok": False, "error": "нет такого метода"},
                                           ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            try:
                ok, msg, extra = handler(payload, who)
            except Exception as exc:  # noqa: BLE001
                ok, msg, extra = False, f"ошибка: {exc}", {}
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok, "message": msg} | extra,
                                  ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        else:
            self._send(400, json.dumps({"ok": False, "error": "bad request"}).encode(),
                       "application/json; charset=utf-8")
            return
        self._send(200 if ok else 400,
                   json.dumps({"ok": ok, "message": msg}, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")


if __name__ == "__main__":
    db.init_db()
    price.init()
    memo.init()
    init_auth()
    chiefs = [l for l, u in PANEL_USERS.items() if u["role"] == "chief"]
    print(f"Панель ЛогистМОТ: http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}"
          f" · вход свободный, аналитика — по паролю ({', '.join(chiefs) or 'не задан'})",
          flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
