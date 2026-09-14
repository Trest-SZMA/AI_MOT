"""Авторизация: логин/пароль из .env, подписанная cookie-сессия.

Права у всех пользователей равные (решение Павла).
"""
from __future__ import annotations

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import SECRET_KEY, USERS_RAW

COOKIE = "mlp_session"
MAX_AGE = 60 * 60 * 24 * 30      # 30 дней

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="mlp-auth")


def _users() -> dict[str, str]:
    out = {}
    for pair in USERS_RAW.split(","):
        if ":" in pair:
            login, pw = pair.split(":", 1)
            out[login.strip()] = pw.strip()
    return out


def check_login(username: str, password: str) -> bool:
    return _users().get(username) == password


def set_session(response: Response, username: str) -> None:
    token = _serializer.dumps({"u": username})
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE)


def current_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=MAX_AGE)
        u = data.get("u")
        return u if u in _users() else None
    except BadSignature:
        return None


def require_user(request: Request) -> str:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return u
