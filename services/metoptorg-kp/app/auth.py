"""Аутентификация и роли.

HTTP Basic поверх собственной таблицы пользователей: браузер сам показывает
окно входа, отдельная страница логина не нужна, а сервис остаётся без внешних
зависимостей. Пароли хранятся как pbkdf2-хеш с индивидуальной солью.

Роли:
  * `оценщик`  — загрузка КП, сопоставление, ручные корректировки, экспорт;
  * `директор` — всё то же плюс утверждение нормативов, цен лома и выходов,
    управление пользователями.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from .db.models import AuditLog, User
from .db.session import get_session

ITERATIONS = 200_000
ROLES = ("оценщик", "директор")

security = HTTPBasic(realm="MetOptTorg", auto_error=False)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def create_user(session: Session, login: str, password: str,
                role: str = "оценщик", full_name: str = "") -> User:
    if role not in ROLES:
        raise ValueError(f"роль должна быть одной из {ROLES}")
    if session.query(User).filter(User.login == login).first():
        raise ValueError(f"пользователь «{login}» уже существует")
    digest, salt = hash_password(password)
    user = User(login=login, password_hash=digest, salt=salt, role=role,
                full_name=full_name)
    session.add(user)
    session.commit()
    return user


def set_password(session: Session, login: str, password: str) -> None:
    user = session.query(User).filter(User.login == login).first()
    if user is None:
        raise ValueError(f"пользователь «{login}» не найден")
    user.password_hash, user.salt = hash_password(password)
    session.commit()


def auth_disabled() -> bool:
    """METOPTTORG_AUTH=off — работа без входа (решение владельца сервиса).

    Учётные записи при этом сохраняются: чтобы вернуть вход, достаточно убрать
    строку из /etc/metoptorg-kp.env и перезапустить службу.
    """
    return os.environ.get("METOPTTORG_AUTH", "").lower() == "off"


def current_user(credentials: HTTPBasicCredentials | None = Depends(security),
                 session: Session = Depends(get_session)) -> User:
    if auth_disabled():
        # вход выключен: все действия доступны, в журнале аудита так и пишем
        return User(login="без входа", role="директор",
                    full_name="режим без авторизации")
    user = None
    if credentials is not None:
        user = (session.query(User)
                .filter(User.login == credentials.username,
                        User.is_active.is_(True)).first())
    ok = (credentials is not None and user is not None
          and verify_password(credentials.password, user.password_hash,
                              user.salt))
    if not ok:
        # одинаковый ответ на неверный логин и неверный пароль
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль",
            headers={"WWW-Authenticate": 'Basic realm="MetOptTorg"'})
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    return user


def require_director(user: User = Depends(current_user)) -> User:
    if user.role != "директор":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Действие доступно только роли «директор» — обратитесь к нему "
            "за утверждением.")
    return user


def log_action(session: Session, user: User | None, action: str,
               entity: str | None = None, entity_id: int | None = None,
               details: str | None = None) -> None:
    session.add(AuditLog(user=user.login if user else None, action=action,
                         entity=entity, entity_id=entity_id, details=details))
    session.commit()
