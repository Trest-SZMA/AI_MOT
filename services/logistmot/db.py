"""Хранилище: подписчики, заявки/рассылки, ответы, телефоны. SQLite.

Схема:
  users      — люди (подписчики лички и/или участники чата), телефон для логиста
  broadcasts — отправленные заявки/рассылки (текст + структурные поля заявки)
  deliveries — доставка в личку конкретному человеку (для метрики отклика)
  replies    — база ответов: кто, на какую заявку, что написал, откуда (личка/чат)

Статуса «прочитано» в MAX Bot API нет — измеряем только ответы.
"""

import re
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT,
    phone       TEXT,                       -- распознанный из ответов номер
    joined_at   INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1  -- 1 = можно слать в личку
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,              -- итоговый отформатированный текст
    route       TEXT,                       -- «Покачи → Полевской»
    dates       TEXT,                       -- «23.07–24.07»
    cargo       TEXT,                       -- «НКТ 73»
    rate        TEXT,                       -- «2500 без НДС / 3050 с НДС»
    sent_at     INTEGER NOT NULL,
    chat_posted INTEGER NOT NULL DEFAULT 0  -- публиковалась ли в групповой чат
);

CREATE TABLE IF NOT EXISTS deliveries (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id),
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    sent_ok      INTEGER NOT NULL DEFAULT 0,
    replied_at   INTEGER,
    PRIMARY KEY (broadcast_id, user_id)
);

CREATE TABLE IF NOT EXISTS replies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id INTEGER REFERENCES broadcasts(id),  -- NULL = вне окна заявки
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    text         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'dm',         -- 'dm' | 'chat'
    at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    user_id  INTEGER PRIMARY KEY,   -- логисты, назначенные командой «логист …»
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prefs (
    user_id       INTEGER PRIMARY KEY,  -- запомненные значения логиста
    last_contacts TEXT                  -- контакты из последней заявки
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,             -- служебные значения (id дайджеста и т.п.)
    value TEXT
);
"""

# Миграции для баз, созданных ранней версией схемы
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN phone TEXT",
    "ALTER TABLE broadcasts ADD COLUMN route TEXT",
    "ALTER TABLE broadcasts ADD COLUMN dates TEXT",
    "ALTER TABLE broadcasts ADD COLUMN cargo TEXT",
    "ALTER TABLE broadcasts ADD COLUMN rate TEXT",
    "ALTER TABLE broadcasts ADD COLUMN chat_posted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE replies ADD COLUMN relevant INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE users ADD COLUMN username TEXT",   # для ссылки на чат в MAX
    "ALTER TABLE replies ADD COLUMN offer TEXT",    # распознанное «2 ТС · 20 т»
    "ALTER TABLE broadcasts ADD COLUMN closed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE replies ADD COLUMN processed INTEGER NOT NULL DEFAULT 0",  # логист отработал
    "ALTER TABLE replies ADD COLUMN processed_at INTEGER",
    "ALTER TABLE broadcasts ADD COLUMN chat_mid TEXT",  # id сообщения в чате (для удаления)
    "ALTER TABLE broadcasts ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0",  # напоминали ли
    "ALTER TABLE broadcasts ADD COLUMN author_id INTEGER",   # кто из логистов создал
    "ALTER TABLE broadcasts ADD COLUMN author_name TEXT",
    "ALTER TABLE replies ADD COLUMN processed_by TEXT",      # кто обработал отклик
    # тип заявки: normal | urgent (горящая) | new_route | rate_up (ставка выше)
    "ALTER TABLE broadcasts ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'",
]


def rate_number(rate: str | None) -> int | None:
    """Первое число из ставки: «2 800 ₽ без НДС · 3 400 с НДС» → 2800."""
    if not rate:
        return None
    m = re.search(r"\d[\d\s]{2,}", rate)
    if not m:
        return None
    try:
        return int(re.sub(r"\s", "", m.group(0)))
    except ValueError:
        return None


def route_history(route: str | None, exclude_id: int | None = None):
    """Прошлые заявки по этому же маршруту (для «было → стало» и «новое направление»)."""
    if not route:
        return []
    key = route.split("→")[0].strip().lower()[:12], route.split("→")[-1].strip().lower()[:12]
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, route, rate, sent_at FROM broadcasts ORDER BY sent_at DESC LIMIT 200"
        ).fetchall()
    out = []
    for r in rows:
        if exclude_id and r["id"] == exclude_id:
            continue
        rt = r["route"] or ""
        if "→" not in rt:
            continue
        a, b = rt.split("→")[0].strip().lower(), rt.split("→")[-1].strip().lower()
        if a.startswith(key[0][:6]) and b.startswith(key[1][:6]):
            out.append(r)
    return out

# Ключевые слова, по которым ответ из шумного чата считается «предложением».
# Ответы из лички релевантны всегда (человек пишет боту осознанно).
OFFER_WORDS = (
    "возьм", "готов", "машин", "тс ", " тс", "дам ", "поед", "еду",
    "тонн", "загруз", "ставк", "камаз", "тент", "шаланд", "полуприцеп",
    "борт", "могу", "интерес", "актуальн", "телефон", "звоните",
)


def looks_like_offer(text: str) -> bool:
    """Похоже ли сообщение на отклик по заявке (для фильтра шума в чате)."""
    if extract_phone(text):
        return True
    low = f" {text.lower()} "
    return any(w in low for w in OFFER_WORDS)


def parse_offer(text: str) -> str | None:
    """Достать из ответа «сколько и чего»: «2 машины» → «2 ТС», «20 тонн» → «20 т».

    Голая цифра («2») — перевозчики так отвечают на вопрос «сколько ТС» — тоже ТС.
    """
    m = re.fullmatch(r"(\d{1,2})", text.strip())
    if m and 1 <= int(m.group(1)) <= 50:
        return m.group(1) + " ТС"
    parts = []
    m = re.search(r"(\d+)\s*(?:машин\w*|тс\b|ТС\b|авто\b|камаз\w*|шаланд\w*|фур\w*|борт\w*|тент\w*)",
                  text, re.IGNORECASE)
    if m:
        parts.append(m.group(1) + " ТС")
    m = re.search(r"(\d+)\s*(?:тонн\w*|тн|т)\b", text, re.IGNORECASE)
    if m:
        parts.append(m.group(1) + " т")
    if not parts and re.search(r"возьм|готов|поед|еду|дам\b|могу", text, re.IGNORECASE):
        parts.append("готов взять")
    return " · ".join(parts) or None

PHONE_RE = re.compile(
    r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
)


def extract_phone(text: str) -> str | None:
    """Найти телефон в тексте ответа. Возвращает в виде +7XXXXXXXXXX."""
    m = PHONE_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 11:
        return "+7" + digits[1:]
    return None


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for mig in MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # колонка уже есть


def upsert_user(user_id: int, name: str | None, username: str | None = None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO users (user_id, name, joined_at, active, username)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   name = excluded.name,
                   username = COALESCE(excluded.username, users.username)""",
            (user_id, name, int(time.time()), username),
        )


def activate_user(user_id: int):
    with connect() as conn:
        conn.execute("UPDATE users SET active = 1 WHERE user_id = ?", (user_id,))


def deactivate_user(user_id: int):
    with connect() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user_id,))


def set_phone(user_id: int, phone: str):
    with connect() as conn:
        conn.execute("UPDATE users SET phone = ? WHERE user_id = ?", (phone, user_id))


def active_users() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT user_id, name FROM users WHERE active = 1"
        ).fetchall()


def create_broadcast(text: str, route: str | None = None, dates: str | None = None,
                     cargo: str | None = None, rate: str | None = None,
                     chat_posted: bool = False, author_id: int | None = None,
                     author_name: str | None = None, kind: str = "normal") -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO broadcasts (text, route, dates, cargo, rate, sent_at,
                                       chat_posted, author_id, author_name, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (text, route, dates, cargo, rate, int(time.time()), int(chat_posted),
             author_id, author_name, kind),
        )
        return cur.lastrowid


def record_delivery(broadcast_id: int, user_id: int, sent_ok: bool):
    with connect() as conn:
        conn.execute(
            """INSERT INTO deliveries (broadcast_id, user_id, sent_ok)
               VALUES (?, ?, ?)
               ON CONFLICT(broadcast_id, user_id) DO UPDATE SET sent_ok = excluded.sent_ok""",
            (broadcast_id, user_id, 1 if sent_ok else 0),
        )


def logist_accounts(env_ids: set[int] | None = None) -> list[dict]:
    """Аккаунты MAX, которым разрешено вести заявки: для атрибуции статистики."""
    ids = set(env_ids or ())
    with connect() as conn:
        ids |= {r["user_id"] for r in conn.execute("SELECT user_id FROM admins")}
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT user_id, name FROM users WHERE user_id IN ({ph})",
            sorted(ids),
        ).fetchall()
    known = {r["user_id"]: r["name"] for r in rows}
    return [{"user_id": i, "name": known.get(i) or f"id {i}"} for i in sorted(ids)]


def match_logist(text: str, accounts: list[dict]) -> dict | None:
    """Чьё имя стоит в заявке: «…по тел. 8 919 460-00-24 Дарья» → Дарья.

    Нужно, когда заявку отправили с панели под общим логином: имя логиста
    в контактах — самый надёжный признак авторства.
    """
    if not text:
        return None
    low = f" {text.lower()} "
    best = None
    for acc in accounts:
        first = (acc["name"] or "").split()[0].lower() if acc["name"] else ""
        if len(first) < 3:
            continue
        # ищем как отдельное слово, чтобы «Ольга» не ловилась внутри других слов
        if re.search(rf"(?<![а-яё]){re.escape(first)}(?![а-яё])", low):
            if best is None or low.index(first) < low.index(
                    (best["name"] or "").split()[0].lower()):
                best = acc
    return best


def db_admins() -> set[int]:
    with connect() as conn:
        return {r["user_id"] for r in conn.execute("SELECT user_id FROM admins")}


def add_admin(user_id: int):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)",
            (user_id, int(time.time())),
        )


def remove_admin(user_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))


def find_user(query: str):
    """Найти человека по id или части имени (для команды «логист …»).

    Имя сравниваем в Python: LIKE в SQLite не понимает регистр кириллицы.
    """
    with connect() as conn:
        if query.isdigit():
            return conn.execute(
                "SELECT user_id, name FROM users WHERE user_id = ?", (int(query),)
            ).fetchone()
        q = query.lower()
        rows = [r for r in conn.execute(
                    "SELECT user_id, name FROM users ORDER BY joined_at DESC")
                if q in (r["name"] or "").lower()]
        return rows[0] if len(rows) == 1 else (rows or None)


def get_pref_contacts(user_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT last_contacts FROM prefs WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["last_contacts"] if row else None


def set_pref_contacts(user_id: int, contacts: str):
    with connect() as conn:
        conn.execute(
            """INSERT INTO prefs (user_id, last_contacts) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_contacts = excluded.last_contacts""",
            (user_id, contacts),
        )


def record_reply(user_id: int, text: str, source: str, when: int,
                 window_seconds: int,
                 bid_override: int | None = None) -> tuple[int, int | None, bool]:
    """Записать ответ. Привязка: bid_override (точная — кнопка/уточнение),
    иначе последняя заявка в окне.

    Возвращает (reply_id, broadcast_id, relevant). Ответы из лички и по кнопке
    релевантны всегда; из чата — только если похожи на предложение.
    """
    relevant = source in ("dm", "button") or looks_like_offer(text)
    offer = parse_offer(text) if relevant else None
    if bid_override is not None:
        bid = bid_override
    else:  # последняя АКТУАЛЬНАЯ заявка (не закрытая, не истёкшая по датам)
        act = active_broadcasts(window_seconds, limit=1)
        bid = act[0]["id"] if act else None
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO replies (broadcast_id, user_id, text, source, at, relevant, offer)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bid, user_id, text, source, when, int(relevant), offer),
        )
        if bid is not None and relevant:
            conn.execute(
                """UPDATE deliveries SET replied_at = COALESCE(replied_at, ?)
                   WHERE broadcast_id = ? AND user_id = ?""",
                (when, bid, user_id),
            )
        return cur.lastrowid, bid, relevant


def has_button_reply(user_id: int, bid: int, window_seconds: int = 12 * 3600) -> bool:
    """Уже жал «Готов взять» по этой заявке недавно? (защита от дублей)"""
    with connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM replies
               WHERE user_id = ? AND broadcast_id = ? AND source = 'button'
                 AND at >= ? LIMIT 1""",
            (user_id, bid, int(time.time()) - window_seconds),
        ).fetchone()
        return row is not None


def dedupe_button_replies() -> int:
    """Схлопнуть накопившиеся дубли нажатий: один человек — одна заявка —
    один отклик (остаётся самый ранний). Возвращает число удалённых."""
    with connect() as conn:
        cur = conn.execute(
            """DELETE FROM replies WHERE source = 'button' AND id NOT IN (
                   SELECT MIN(id) FROM replies WHERE source = 'button'
                   GROUP BY user_id, broadcast_id)""")
        return cur.rowcount


def reassign_reply(reply_id: int, new_bid: int, when: int):
    """Перепривязать ответ к другой заявке (уточнение «ответьте цифрой»)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, broadcast_id FROM replies WHERE id = ?", (reply_id,)
        ).fetchone()
        if row is None:
            return
        uid, old_bid = row["user_id"], row["broadcast_id"]
        conn.execute("UPDATE replies SET broadcast_id = ? WHERE id = ?",
                     (new_bid, reply_id))
        conn.execute(
            """UPDATE deliveries SET replied_at = COALESCE(replied_at, ?)
               WHERE broadcast_id = ? AND user_id = ?""",
            (when, new_bid, uid),
        )
        if old_bid and old_bid != new_bid:
            left = conn.execute(
                """SELECT COUNT(*) AS c FROM replies
                   WHERE user_id = ? AND broadcast_id = ? AND relevant = 1""",
                (uid, old_bid),
            ).fetchone()["c"]
            if left == 0:  # других откликов на старую заявку нет — снимаем отметку
                conn.execute(
                    "UPDATE deliveries SET replied_at = NULL WHERE broadcast_id = ? AND user_id = ?",
                    (old_bid, uid),
                )


def _last_date_ts(dates: str, ref_ts: int) -> int | None:
    """Конец последнего дня из строки дат («28.07–31.07» → 31.07 23:59)."""
    import datetime as dt
    found = re.findall(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", dates)
    if not found:
        return None
    d, m, y = found[-1]
    ref_year = dt.datetime.fromtimestamp(ref_ts).year
    year = int(y) + (2000 if y and int(y) < 100 else 0) if y else ref_year
    try:
        end = dt.datetime(year, int(m), int(d), 23, 59, 59)
    except ValueError:
        return None
    # «30.12-2.01» без года: конец раньше публикации → это следующий год
    if not y and end.timestamp() < ref_ts - 2 * 86400:
        try:
            end = dt.datetime(year + 1, int(m), int(d), 23, 59, 59)
        except ValueError:
            return None
    return int(end.timestamp())


def broadcast_expires(row, window_seconds: int) -> int | None:
    """Когда заявка перестаёт быть актуальной. None = бессрочная («ежедневно»)."""
    dates = row["dates"] or ""
    if "ежедневн" in dates.lower():
        return None
    if dates:
        ts = _last_date_ts(dates, row["sent_at"])
        if ts is not None:
            return ts
    return row["sent_at"] + window_seconds


def broadcast_is_active(row, window_seconds: int, now: int | None = None) -> bool:
    if row["closed"]:
        return False
    now = now or int(time.time())
    exp = broadcast_expires(row, window_seconds)
    return exp is None or now <= exp


def active_broadcasts(window_seconds: int, limit: int = 4):
    """Актуальные заявки: не закрыты и не истекли по датам."""
    now = int(time.time())
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM broadcasts
               WHERE closed = 0 AND sent_at >= ?
               ORDER BY sent_at DESC, id DESC LIMIT 50""",
            (now - 30 * 86400,),
        ).fetchall()
    return [r for r in rows if broadcast_is_active(r, window_seconds, now)][:limit]


def set_reply_processed(reply_id: int, done: bool = True,
                        by: str | None = None) -> bool:
    """Отметить предложение обработанным логистом (или вернуть в очередь)."""
    with connect() as conn:
        cur = conn.execute(
            """UPDATE replies SET processed = ?, processed_at = ?, processed_by = ?
               WHERE id = ?""",
            (int(done), int(time.time()) if done else None,
             by if done else None, reply_id),
        )
        return cur.rowcount > 0


# --- статистика для руководителя -----------------------------------------

def stats_summary(days: int = 30) -> dict:
    """Сводка работы: по логистам, по дням, скорость реакции."""
    since = int(time.time()) - days * 86400
    with connect() as conn:
        by_logist = conn.execute(
            """SELECT COALESCE(b.author_name, 'через бота') AS who,
                      COUNT(*) AS zayavok,
                      SUM((SELECT COUNT(DISTINCT r.user_id) FROM replies r
                            WHERE r.broadcast_id = b.id AND r.relevant = 1)) AS otklikov
               FROM broadcasts b WHERE b.sent_at >= ?
               GROUP BY who ORDER BY zayavok DESC""",
            (since,),
        ).fetchall()

        handled = conn.execute(
            """SELECT COALESCE(processed_by, 'не указан') AS who,
                      COUNT(*) AS obrabotano,
                      AVG(processed_at - at) AS avg_sec
               FROM replies
               WHERE processed = 1 AND processed_at >= ?
               GROUP BY who ORDER BY obrabotano DESC""",
            (since,),
        ).fetchall()

        by_day = conn.execute(
            """SELECT date(sent_at, 'unixepoch', 'localtime') AS d,
                      COUNT(*) AS zayavok
               FROM broadcasts WHERE sent_at >= ?
               GROUP BY d ORDER BY d DESC LIMIT 14""",
            (since,),
        ).fetchall()

        replies_day = conn.execute(
            """SELECT date(at, 'unixepoch', 'localtime') AS d,
                      COUNT(*) AS otklikov,
                      SUM(processed) AS obrabotano
               FROM replies WHERE relevant = 1 AND at >= ?
               GROUP BY d ORDER BY d DESC LIMIT 14""",
            (since,),
        ).fetchall()

        totals = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM broadcasts WHERE sent_at >= ?)         AS zayavok,
                 (SELECT COUNT(*) FROM replies WHERE relevant = 1 AND at >= ?) AS otklikov,
                 (SELECT COUNT(*) FROM replies WHERE processed = 1 AND processed_at >= ?) AS obrabotano,
                 (SELECT COUNT(*) FROM users WHERE active = 1)                AS podpisano,
                 (SELECT COUNT(*) FROM users WHERE phone IS NOT NULL)         AS telefonov""",
            (since, since, since),
        ).fetchone()

        silent_z = conn.execute(
            """SELECT COUNT(*) c FROM broadcasts b
               WHERE b.sent_at >= ? AND NOT EXISTS (
                   SELECT 1 FROM replies r WHERE r.broadcast_id = b.id AND r.relevant = 1)""",
            (since,),
        ).fetchone()["c"]

    def rows(rs):
        return [dict(r) for r in rs]

    return {
        "days": days,
        "totals": dict(totals) | {"zayavok_bez_otklikov": silent_z},
        "by_logist": rows(by_logist),
        "handled": rows(handled),
        "by_day": rows(by_day),
        "replies_day": rows(replies_day),
    }


def set_broadcast_closed(bid: int, closed: bool = True) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE broadcasts SET closed = ? WHERE id = ?",
                           (int(closed), bid))
        return cur.rowcount > 0


def delete_broadcast(bid: int) -> tuple[bool, str | None]:
    """Удалить заявку целиком (с панели, из привязок). Отклики сохраняются,
    но отвязываются. Возвращает (нашлась ли, chat_mid для удаления из чата)."""
    with connect() as conn:
        row = conn.execute("SELECT chat_mid FROM broadcasts WHERE id = ?",
                           (bid,)).fetchone()
        if row is None:
            return False, None
        conn.execute("UPDATE replies SET broadcast_id = NULL WHERE broadcast_id = ?",
                     (bid,))
        conn.execute("DELETE FROM deliveries WHERE broadcast_id = ?", (bid,))
        conn.execute("DELETE FROM broadcasts WHERE id = ?", (bid,))
        return True, row["chat_mid"]


def update_broadcast(bid: int, route: str | None, dates: str | None,
                     cargo: str | None, rate: str | None) -> bool:
    """Правка полей заявки (панель). Смена дат снимает флаг «напоминали» —
    продлённая заявка снова живёт по новым датам."""
    with connect() as conn:
        old = conn.execute("SELECT dates FROM broadcasts WHERE id = ?", (bid,)).fetchone()
        if old is None:
            return False
        cur = conn.execute(
            """UPDATE broadcasts SET route = ?, dates = ?, cargo = ?, rate = ?,
                   reminded = CASE WHEN ? THEN 0 ELSE reminded END
               WHERE id = ?""",
            (route, dates, cargo, rate, int((dates or "") != (old["dates"] or "")), bid),
        )
        return cur.rowcount > 0


def recent_broadcasts(limit: int = 10):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM broadcasts ORDER BY sent_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_kv(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_kv(key: str, value: str):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                     (key, value))


def set_broadcast_text(bid: int, text: str):
    """Сохранить пересобранный текст карточки (правка с панели)."""
    with connect() as conn:
        conn.execute("UPDATE broadcasts SET text = ? WHERE id = ?", (text, bid))


def clear_chat_mid(bid: int):
    with connect() as conn:
        conn.execute("UPDATE broadcasts SET chat_mid = NULL WHERE id = ?", (bid,))


def stale_chat_messages(window_seconds: int):
    """Неактуальные заявки, чьи сообщения ещё висят в чате (для уборки)."""
    now = int(time.time())
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM broadcasts WHERE chat_mid IS NOT NULL AND chat_mid != ''"
        ).fetchall()
    return [r for r in rows if not broadcast_is_active(r, window_seconds, now)]


def expired_unanswered(window_seconds: int):
    """Истёкшие заявки без единого отклика, о которых логистам ещё не напоминали."""
    now = int(time.time())
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.*, (SELECT COUNT(DISTINCT r.user_id) FROM replies r
                             WHERE r.broadcast_id = b.id AND r.relevant = 1) AS resp
               FROM broadcasts b
               WHERE b.closed = 0 AND b.reminded = 0
                 AND b.sent_at >= ?""",
            (now - 7 * 86400,),
        ).fetchall()
    return [r for r in rows
            if r["resp"] == 0 and not broadcast_is_active(r, window_seconds, now)]


def mark_reminded(bid: int):
    with connect() as conn:
        conn.execute("UPDATE broadcasts SET reminded = 1 WHERE id = ?", (bid,))


def get_broadcast(bid: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM broadcasts WHERE id = ?", (bid,)).fetchone()
