"""Учётки панели из PANEL_USERS — общие для панели и бота.

Формат в .env:  PANEL_USERS=login:пароль:роль:имя:max_id, ...
Роль — одна или несколько через «+» (chief+head). max_id — id человека в
MAX: по нему панель шлёт уведомления, а бот узнаёт согласующего, когда тот
жмёт кнопку под запиской. Это и есть проверка личности при согласовании
через MAX: id в мессенджере подделать нельзя, он приходит от сервера MAX.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def parse_users(raw: str) -> dict:
    users = {}
    for item in raw.split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 3 or not parts[0]:
            continue
        login, pwd, role = parts[0], parts[1], parts[2]
        roles = [r.strip() for r in role.split("+") if r.strip()]
        name = parts[3] if len(parts) > 3 and parts[3] else login
        max_id = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        users[login] = {"password": pwd, "role": roles[0] if roles else "logist",
                        "roles": roles or ["logist"], "name": name, "max_id": max_id}
    return users


USERS = parse_users(os.getenv("PANEL_USERS", ""))


def has_role(who: dict, role: str) -> bool:
    return role in (who.get("roles") or [who.get("role")])


def role_max_ids(role: str) -> list[int]:
    """Все, у кого есть роль и известен id в MAX."""
    return [u["max_id"] for u in USERS.values()
            if role in u.get("roles", []) and u.get("max_id")]


def by_max_id(user_id: int) -> dict | None:
    """Учётка по id в MAX (с логином внутри) или None, если человек не заведён."""
    for login, u in USERS.items():
        if u.get("max_id") == user_id:
            return {**u, "login": login}
    return None
