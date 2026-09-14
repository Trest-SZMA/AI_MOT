"""Авторизация: пользователи, пароли, сессии.

Роль больше не выбирается селектором в шапке — она задана учётной записью.
Пароль хранится хешем PBKDF2-HMAC-SHA256 (стандартная библиотека, без
зависимостей), сессия — случайный токен в таблице `user_sessions` и cookie
HttpOnly. Все входы и неудачные попытки пишутся в `audit_log`, поэтому
журнал показывает, кто именно правил БП, а не «какая роль была выбрана».
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SESSION_COOKIE = "bp_session"
SESSION_DAYS = 14                      # срок жизни сессии от последнего входа
PBKDF2_ITERATIONS = 240_000

# Защита от подбора: после LOCK_ATTEMPTS неудач подряд вход по этому логину
# закрыт на LOCK_MINUTES минут (считается по журналу, без отдельной таблицы).
LOCK_ATTEMPTS = 5
LOCK_MINUTES = 15

MIN_PASSWORD_LEN = 8

LOGIN_RE = re.compile(r"^[a-z0-9._-]{3,32}$")


# ── Пароли ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Хеш в формате `pbkdf2_sha256$итерации$соль$хеш` (как в Django)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), int(iterations))
    return hmac.compare_digest(dk.hex(), digest)


def password_problem(password: str) -> str:
    """Понятная причина, почему пароль не годится («» = годится)."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Пароль короче {MIN_PASSWORD_LEN} символов."
    if password.isdigit():
        return "Пароль из одних цифр слишком простой — добавьте буквы."
    if password.lower() in {"password", "пароль", "12345678", "qwertyui",
                            "metopttorg", "bpservice"}:
        return "Слишком распространённый пароль."
    return ""


def login_problem(login: str) -> str:
    if not LOGIN_RE.match(login or ""):
        return ("Логин: 3–32 символа, латиница в нижнем регистре, цифры, "
                "точка, дефис или подчёркивание.")
    return ""


# ── Сессии ──────────────────────────────────────────────────────────

def _now() -> datetime:
    """Время в UTC — как `datetime('now')` в схеме: сроки сессий и записи
    журнала должны сравниваться в одной шкале."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_session(conn: sqlite3.Connection, user_id: int, ip: str = "",
                   user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO user_sessions (token, user_id, created_at, expires_at, ip, "
        "user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (token, user_id, _now().strftime("%Y-%m-%d %H:%M:%S"),
         expires.strftime("%Y-%m-%d %H:%M:%S"), ip[:64], (user_agent or "")[:200]))
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                 (_now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    # Мусор от прошлых входов не копится.
    conn.execute("DELETE FROM user_sessions WHERE expires_at < ?",
                 (_now().strftime("%Y-%m-%d %H:%M:%S"),))
    return token


def session_user(conn: sqlite3.Connection, token: str | None):
    """Пользователь живой сессии либо None (нет токена / истёк / отключён)."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.*, s.token AS session_token, s.expires_at AS session_expires "
        "FROM user_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1",
        (token, _now().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    return row


def drop_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM user_sessions WHERE token = ?", (token,))


def drop_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """Все сессии пользователя — при смене пароля и отключении учётной записи."""
    conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))


# ── Вход ────────────────────────────────────────────────────────────

def locked_minutes(conn: sqlite3.Connection, login: str) -> int:
    """Сколько минут осталось до разблокировки логина (0 — не заблокирован).

    Считается по журналу: LOCK_ATTEMPTS неудач подряд за последние
    LOCK_MINUTES минут. Успешный вход обнуляет счётчик, потому что
    записи после него в выборку уже не попадают."""
    since = (_now() - timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT ts, action FROM audit_log WHERE action IN ('login', 'login_failed') "
        "AND user_name = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
        (login, since, LOCK_ATTEMPTS)).fetchall()
    if len(rows) < LOCK_ATTEMPTS or any(r["action"] != "login_failed" for r in rows):
        return 0
    oldest = rows[-1]["ts"]
    try:
        started = datetime.strptime(oldest, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    left = LOCK_MINUTES - int((_now() - started).total_seconds() // 60)
    return max(left, 1)


def authenticate(conn: sqlite3.Connection, login: str, password: str):
    """(пользователь, ошибка). Пользователь — sqlite3.Row либо None."""
    login = (login or "").strip().lower()
    if not login or not password:
        return None, "Введите логин и пароль."
    left = locked_minutes(conn, login)
    if left:
        return None, (f"Слишком много неудачных попыток. Вход по логину "
                      f"заблокирован ещё на {left} мин.")
    row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None, "Неверный логин или пароль."
    if not row["is_active"]:
        return None, "Учётная запись отключена. Обратитесь к администратору."
    return row, ""


# ── Первоначальная учётная запись ───────────────────────────────────

def ensure_admin(conn: sqlite3.Connection) -> str | None:
    """Если ни у кого нет пароля — завести вход администратора.

    Пароль берётся из переменной окружения BP_ADMIN_PASSWORD, иначе
    генерируется случайный и возвращается для показа в логе запуска
    (в базе он хранится только хешем). Флаг must_change_password
    заставит сменить его при первом входе.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE password_hash IS NOT NULL "
        "AND password_hash <> ''").fetchone()
    if row["n"]:
        return None
    password = os.environ.get("BP_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
    exists = conn.execute("SELECT id FROM users WHERE login = 'admin'").fetchone()
    if exists:
        conn.execute(
            "UPDATE users SET password_hash = ?, role = 'admin', is_active = 1, "
            "must_change_password = 1 WHERE id = ?",
            (hash_password(password), exists["id"]))
    else:
        conn.execute(
            "INSERT INTO users (login, full_name, role, password_hash, "
            "is_active, must_change_password) "
            "VALUES ('admin', 'Администратор', 'admin', ?, 1, 1)",
            (hash_password(password),))
    return password
