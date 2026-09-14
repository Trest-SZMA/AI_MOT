#!/usr/bin/env python3
"""Управление пользователями сервиса.

Запускать на сервере от root (база лежит в StateDirectory службы):

    sudo METOPTTORG_DATA_DIR=/var/lib/private/metoptorg-kp \
        /opt/metoptorg-kp/.venv/bin/python /opt/metoptorg-kp/scripts/user_admin.py list
    ... add <логин> <роль: оценщик|директор> [ФИО]     — пароль спросит скрыто
    ... passwd <логин>                                  — сменить пароль
    ... disable <логин> / enable <логин>
"""
import getpass
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import ROLES, create_user, set_password  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402


def ask_password(login: str) -> str:
    # неинтерактивный путь для скриптов развёртывания
    env_pw = os.environ.get("METOPTTORG_PASSWORD")
    if env_pw:
        return env_pw
    if not sys.stdin.isatty():
        sys.exit("Нет терминала: задайте пароль через METOPTTORG_PASSWORD")
    pw = getpass.getpass(f"Пароль для «{login}» (пусто — сгенерировать): ")
    if not pw:
        pw = secrets.token_urlsafe(12)
        print(f"  сгенерирован пароль: {pw}")
        print("  сохраните его — повторно показан не будет")
        return pw
    if len(pw) < 8:
        sys.exit("Пароль короче 8 символов — слишком слабый")
    if pw != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпали")
    return pw


def main() -> None:
    init_db()
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd, *rest = args
    s = SessionLocal()
    try:
        if cmd == "list":
            users = s.query(User).order_by(User.login).all()
            if not users:
                print("Пользователей нет — сервис никого не пустит.")
            for u in users:
                status = "активен" if u.is_active else "ОТКЛЮЧЁН"
                last = u.last_login_at.strftime("%d.%m.%Y %H:%M") if u.last_login_at else "—"
                print(f"{u.login:20} {u.role:10} {status:9} вход: {last}  {u.full_name or ''}")
        elif cmd == "add":
            if len(rest) < 2 or rest[1] not in ROLES:
                sys.exit(f"Использование: add <логин> <{'|'.join(ROLES)}> [ФИО]")
            login, role = rest[0], rest[1]
            full_name = " ".join(rest[2:])
            create_user(s, login, ask_password(login), role, full_name)
            print(f"Создан пользователь «{login}» с ролью «{role}»")
        elif cmd == "passwd":
            if not rest:
                sys.exit("Использование: passwd <логин>")
            set_password(s, rest[0], ask_password(rest[0]))
            print("Пароль изменён")
        elif cmd in ("disable", "enable"):
            if not rest:
                sys.exit(f"Использование: {cmd} <логин>")
            u = s.query(User).filter(User.login == rest[0]).first()
            if u is None:
                sys.exit("Пользователь не найден")
            u.is_active = cmd == "enable"
            s.commit()
            print(f"Пользователь «{u.login}»: {'включён' if u.is_active else 'отключён'}")
        else:
            sys.exit(__doc__)
    except ValueError as e:
        sys.exit(f"Ошибка: {e}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
