"""Ночная выгрузка таблиц 1С из MSSQL «Extractor» и импорт в базу сервиса.

Зачем: до 15.09.2026 выгрузки 1С клали в `1c/` руками (CSV из 1С). База
«Extractor» (1С заполняет её около 01:00) содержит те же таблицы с теми же
именами колонок, поэтому файл пишется в тот же CSV, который читал ручной
импорт, и дальше работает прежний `import_1c_csv.py`. Расчёт про SQL не
знает — у файла и у SQL один код разбора, переключение источника не сдвигает
цифры (тот же приём, что в «Реализации», `src/sqlsrc.py`).

Запуск:  python pull_1c.py [--folder 1c] [--no-import] [--only Отвесная ТС] [--keep 2]

Настройки — только окружение (файл env/bp-service.env на сервере, права 600):
  BP_SQL_HOST      10.100.1.110      BP_SQL_PORT      1433
  BP_SQL_DATABASE  Extractor         BP_SQL_USER      (обязательно)
  BP_SQL_PASSWORD  (обязательно; в код, git и интерфейс не попадает)

Драйвер — python-tds: он передаёт пароль в UTF-16 по спецификации TDS, как
драйвер Microsoft; pymssql с кириллицей в пароле получает отказ 18456.

Итог каждой выгрузки пишется в `1c/_last_pull.json` — его показывает
страница «Справочники».
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import decimal
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

# (префикс файла как у ручной выгрузки, таблица в Extractor).
# Префиксы с подчёркиваниями — так 1С называет файлы таблиц с пробелами в имени.
TABLES: list[tuple[str, str]] = [
    ("СерииНоменклатуры", "СерииНоменклатуры"),
    ("Номенклатура", "Номенклатура"),
    ("Склады", "Склады"),
    ("СтатьиРасходов", "СтатьиРасходов"),
    ("_Производственные_подразделения_", "Производственные подразделения"),
    ("Отвесная", "Отвесная"),
    ("_Производственная_себестоимость_", "Производственная себестоимость"),
    ("Закупки", "Закупки"),
    ("_Выручка_на_загрузку_", "Выручка на загрузку"),
    ("_Распределение_прочих_расходов_", "Распределение прочих расходов"),
    ("ТС", "ТС"),
]
DEFAULT_FOLDER = "1c"
STATUS_FILE = "_last_pull.json"
_SAFE_TABLE = re.compile(r"^[\w .\-]+$")


def settings() -> dict:
    return {
        "host": os.environ.get("BP_SQL_HOST", "10.100.1.110"),
        "port": int(os.environ.get("BP_SQL_PORT", "1433")),
        "database": os.environ.get("BP_SQL_DATABASE", "Extractor"),
        "user": os.environ.get("BP_SQL_USER", ""),
        "password": os.environ.get("BP_SQL_PASSWORD", ""),
    }


def connect():
    cfg = settings()
    if not cfg["user"] or not cfg["password"]:
        raise RuntimeError("не заданы BP_SQL_USER / BP_SQL_PASSWORD в окружении "
                           "(env/bp-service.env на сервере)")
    try:
        import pytds
    except ImportError as e:
        raise RuntimeError("драйвер python-tds не установлен: pip install python-tds") from e
    return pytds.connect(dsn=cfg["host"], port=cfg["port"], database=cfg["database"],
                         user=cfg["user"], password=cfg["password"],
                         login_timeout=15, timeout=900, autocommit=True)


def fmt(v) -> str:
    """Значение SQL → строка в формате ручной выгрузки 1С.

    Разбор в import_1c_csv терпим (дата берётся первыми 10 символами, булево
    понимает 'true'), но пишем как 1С, чтобы файлы из двух источников не
    отличались глазами: '1'/'0', '2026-04-30 00:00:00.000', GUID заглавными.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, dt.datetime):
        s = v.strftime("%Y-%m-%d %H:%M:%S.") + f"{v.microsecond // 1000:03d}"
        if v.tzinfo is not None:
            s += " " + v.strftime("%z")
        return s
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v).upper()
    if isinstance(v, decimal.Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def dump_table(cur, prefix: str, table: str, folder: Path, stamp: str, log=print) -> tuple[Path, int]:
    if not _SAFE_TABLE.match(table):
        raise ValueError(f"недопустимое имя таблицы: {table!r}")
    path = folder / f"{prefix}_{stamp}.csv"
    tmp = path.with_suffix(".csv.part")
    n = 0
    cur.execute(f"SELECT * FROM [dbo].[{table}]")
    cols = [d[0] for d in cur.description]
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(cols)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            w.writerows([fmt(v) for v in row] for row in batch)
            n += len(batch)
            if n % 100000 == 0:
                log(f"    {table}: {n} строк...")
    os.replace(tmp, path)
    return path, n


def drop_older(folder: Path, prefix: str, keep: int, log=print) -> None:
    """Оставить `keep` последних выгрузок префикса — файлы Отвесной весят десятки МБ."""
    files = sorted(folder.glob(prefix + "_*.csv"))
    for old in files[:-keep] if keep > 0 else []:
        try:
            old.unlink()
            log(f"    удалена прежняя выгрузка {old.name}")
        except OSError as e:
            log(f"    не смог удалить {old.name}: {e}")


def pull(folder: Path, only: set[str] | None = None, keep: int = 2, log=print) -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M")
    status = {"started_at": dt.datetime.now().isoformat(timespec="seconds"),
              "stamp": stamp, "host": settings()["host"], "database": settings()["database"],
              "tables": [], "errors": []}
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        cn = connect()
    except Exception as e:
        status["errors"].append(f"подключение: {type(e).__name__}: {e}")
        return status
    with cn:
        cur = cn.cursor()
        for prefix, table in TABLES:
            if only and prefix not in only and table not in only:
                continue
            t1 = time.time()
            try:
                path, n = dump_table(cur, prefix, table, folder, stamp, log)
                drop_older(folder, prefix, keep, log)
                status["tables"].append({"prefix": prefix, "table": table, "file": path.name,
                                         "rows": n, "seconds": round(time.time() - t1, 1)})
                log(f"  {table:34s} {n:8d} строк -> {path.name}")
            except Exception as e:
                status["errors"].append(f"{table}: {type(e).__name__}: {e}")
                log(f"  ОШИБКА {table}: {type(e).__name__}: {e}")
    status["seconds"] = round(time.time() - t0, 1)
    return status


def write_status(folder: Path, status: dict) -> None:
    tmp = folder / (STATUS_FILE + ".part")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, folder / STATUS_FILE)


def read_status(folder: str | Path) -> dict | None:
    """Для страницы «Справочники»: итог последней выгрузки или None."""
    try:
        return json.loads((Path(folder) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="выгрузка 1С из MSSQL Extractor и импорт")
    ap.add_argument("--folder", default=DEFAULT_FOLDER, help="куда класть CSV (по умолчанию 1c/)")
    ap.add_argument("--no-import", action="store_true", help="только выгрузить файлы")
    ap.add_argument("--only", nargs="*", help="только эти таблицы/префиксы")
    ap.add_argument("--keep", type=int, default=2, help="сколько прежних выгрузок оставлять")
    a = ap.parse_args(argv)
    folder = Path(a.folder)
    print(f"Выгрузка из {settings()['host']}/{settings()['database']} в {folder.resolve()}")
    status = pull(folder, set(a.only) if a.only else None, a.keep)
    status["imported"] = False
    if not a.no_import and status["tables"]:
        from import_1c_csv import import_all
        try:
            import_all(str(folder), author="ночной импорт из Extractor")
            status["imported"] = True
        except Exception as e:
            status["errors"].append(f"импорт: {type(e).__name__}: {e}")
            print(f"ОШИБКА импорта: {type(e).__name__}: {e}")
    status["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_status(folder, status)
    if status["errors"]:
        print("Ошибки:")
        for e in status["errors"]:
            print("  -", e)
        return 1
    print("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
