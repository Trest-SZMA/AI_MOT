# -*- coding: utf-8 -*-
"""Проверка подключения к MS SQL «Extractor» из командной строки сервера.

    set -a; . /etc/metoptorg-realizaciya.env; set +a
    /opt/metoptorg-realizaciya/.venv/bin/python3 tools/sql_check.py

⚠️ ПАРОЛЬ НЕ ПЕЧАТАЕТСЯ НИ В КАКОМ ВИДЕ. Показываются только его признаки —
длина, состав по типам символов — этого хватает, чтобы поймать частые причины
отказа и при этом не выложить пароль в лог или в историю команд.

Зачем отдельный инструмент, а не только кнопка в браузере: когда вход не
проходит, надо отличить три разные беды, и по одному сообщению они не
различаются —
  * до сервера не достучались (сеть, порт, файрвол);
  * достучались, но сервер отверг логин/пароль (18456);
  * вход прошёл, но именно к базе «Extractor» прав нет (тоже 18456, состояние
    38/40 — поэтому проверяем вход БЕЗ базы отдельно).
"""
import os, sys, socket, unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import sqlsrc


def main():
    cfg = sqlsrc.load_config()
    host, port = cfg.get("host"), int(cfg.get("port") or 1433)
    user, db = cfg.get("user"), cfg.get("database")
    print("=" * 74)
    print("ПРОВЕРКА ПОДКЛЮЧЕНИЯ: %s:%s · база %s · пользователь %s" % (host, port, db, user))
    print("=" * 74)

    ok, drv = sqlsrc.driver_status()
    print("  драйвер : %s" % drv)

    pwd = sqlsrc.password()
    if not pwd:
        print("  пароль  : НЕ ЗАДАН — переменная %s пуста." % sqlsrc.PASSWORD_ENV)
        print("            Служба читает её из /etc/metoptorg-realizaciya.env,")
        print("            а этот скрипт — из окружения: не забудьте `set -a; . …`")
        return 1
    kinds = {}
    for ch in pwd:
        k = ("кириллица" if "CYRILLIC" in unicodedata.name(ch, "") else
             "латиница" if ch.isascii() and ch.isalpha() else
             "цифра" if ch.isdigit() else
             "знак" if ch.isascii() else "иное не-ASCII")
        kinds[k] = kinds.get(k, 0) + 1
    print("  пароль  : %d символов — %s" % (len(pwd), ", ".join(
        "%s×%d" % kv for kv in sorted(kinds.items()))))
    if not pwd.isascii():
        print("            ⚠️ есть не-ASCII: с таким паролем работает только")
        print("            python-tds; FreeTDS/pymssql отвергается сервером.")
    if pwd != pwd.strip():
        print("            ⚠️ по краям пробелы — почти наверняка лишние.")

    # 1. сеть
    try:
        with socket.create_connection((host, port), timeout=5):
            print("  сеть    : порт %s открыт" % port)
    except Exception as e:
        print("  сеть    : ПОРТ НЕДОСТУПЕН — %s" % e)
        print("\nДальше проверять нечего: сначала сеть и файрвол.")
        return 1

    # 2. вход без базы — отделяет «пароль не тот» от «нет прав на базу»
    cfg_nodb = dict(cfg, database="")
    ok_nodb, msg_nodb = sqlsrc.test_connection(cfg_nodb)
    print("  вход    : %s" % ("принят (без указания базы)" if ok_nodb else "ОТКАЗ"))
    if not ok_nodb:
        print("            %s" % msg_nodb[:200])

    # 3. вход с базой
    ok_db, msg_db = sqlsrc.test_connection(cfg)
    print("  база    : %s" % ("доступна" if ok_db else "ОТКАЗ"))
    if not ok_db:
        print("            %s" % msg_db[:200])

    print()
    if ok_db:
        print("ИТОГ: всё работает. Можно указывать таблицы на вкладке «Обновление данных».")
        return 0
    if ok_nodb:
        print("ИТОГ: логин и пароль ВЕРНЫЕ — сервер пускает.")
        print("      Не пускает именно база «%s»: у пользователя нет на неё прав" % db)
        print("      либо она не назначена ему базой по умолчанию. Это к администратору 1С/SQL.")
        return 2
    print("ИТОГ: сервер отверг пару логин/пароль (18456).")
    print("      Сеть и драйвер здесь ни при чём — отказ пришёл от самого SQL Server.")
    print("      Что проверить:")
    print("        1) значение %s в /etc/metoptorg-realizaciya.env" % sqlsrc.PASSWORD_ENV)
    print("           (после правки обязателен `systemctl restart metoptorg-realizaciya`);")
    print("        2) тот ли это пароль, что сохранён в DBeaver, — они могли разойтись;")
    print("        3) не заблокирован ли вход «%s» и разрешена ли ему" % user)
    print("           аутентификация SQL Server с адреса этого сервера.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
