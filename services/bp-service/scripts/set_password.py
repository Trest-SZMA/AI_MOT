"""Задать пароль пользователю из командной строки.

Нужен, когда забыт пароль администратора: через интерфейс его не сбросить —
в базе лежит только хеш. Запускать на том же сервере, где стоит сервис:

    .venv/bin/python scripts/set_password.py admin
    .venv/bin/python scripts/set_password.py ivanov --role economist --name "Иванов И. И."

Пароль спрашивается скрытым вводом. Все сессии пользователя закрываются.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, workflow                                  # noqa: E402
from app.db import connect, init_db                             # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Пароль пользователя сервиса БП")
    ap.add_argument("login", help="логин учётной записи")
    ap.add_argument("--role", choices=sorted(workflow.ROLES),
                    help="роль (для новой учётной записи или смены роли)")
    ap.add_argument("--name", help="ФИО")
    ap.add_argument("--force-change", action="store_true",
                    help="потребовать смену пароля при первом входе")
    args = ap.parse_args()

    login = args.login.strip().lower()
    problem = auth.login_problem(login)
    if problem:
        print(problem)
        return 1

    init_db()
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    if row is None and not args.role:
        print(f"Пользователя «{login}» нет. Укажите --role, чтобы создать его.")
        return 1

    password = getpass.getpass("Новый пароль: ")
    if password != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают.")
        return 1
    problem = auth.password_problem(password)
    if problem:
        print(problem)
        return 1

    must_change = 1 if args.force_change else 0
    if row is None:
        conn.execute(
            "INSERT INTO users (login, full_name, role, password_hash, "
            "is_active, must_change_password) VALUES (?, ?, ?, ?, 1, ?)",
            (login, (args.name or login).strip(), args.role,
             auth.hash_password(password), must_change))
        action = "создан"
    else:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ?, "
            "is_active = 1, role = COALESCE(?, role), "
            "full_name = COALESCE(?, full_name) WHERE id = ?",
            (auth.hash_password(password), must_change, args.role, args.name,
             row["id"]))
        auth.drop_user_sessions(conn, row["id"])
        action = "обновлён"
    conn.commit()
    conn.close()
    print(f"Пользователь «{login}» {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
