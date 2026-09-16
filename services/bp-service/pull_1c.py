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

После таблиц 1С подхватываются данные соседа — сервиса «Реализация», чей
каталог смонтирован в контейнер как `BP_NEIGHBOR_DIR` (`/neighbor`):
  data/DEAL_*.xlsx       реестр сделок Битрикса → доля лота по каждому БП;
  out/sales_data.json    ночная сборка факта → снимок план/факт по каждому БП.

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
    ("Контрагенты", "Контрагенты"),
    ("ГруппыАналитическогоУчетаНоменклатуры", "ГруппыАналитическогоУчетаНоменклатуры"),
    ("УстановкаТранспортныхСтавокУчетМеталлоломаСтавки", "УстановкаТранспортныхСтавокУчетМеталлоломаСтавки"),
]
DEFAULT_FOLDER = "1c"
NEIGHBOR_DIR = os.environ.get("BP_NEIGHBOR_DIR", "/neighbor")
DEALS_DIR = os.path.join(NEIGHBOR_DIR, "data")
FACT_JSON = os.path.join(NEIGHBOR_DIR, "out", "sales_data.json")
FOLDER_FOR_FACT = DEFAULT_FOLDER          # переопределяется в main() из --folder
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


def dump_fact_costs(cur, folder: Path, stamp: str, since: str, log=print) -> tuple[Path, int]:
    """Регистр «Прочие расход (все)»: только строки с серией с даты since и
    только нужные колонки — полный регистр 823 тыс. строк и 56 колонок для
    матрицы не нужен."""
    from app.fact_costs import COLUMNS, FILE_PREFIX
    path = folder / f"{FILE_PREFIX}_{stamp}.csv"
    tmp = path.with_suffix(".csv.part")
    cols_sql = ", ".join(f"[{c}]" for c in COLUMNS)
    cur.execute(f"SELECT {cols_sql} FROM [dbo].[Прочие расход (все)] "
                f"WHERE [Серия] <> '' AND [Серия] IS NOT NULL AND [Период] >= %s", (since,))
    n = 0
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(COLUMNS)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            w.writerows([fmt(v) for v in row] for row in batch)
            n += len(batch)
    os.replace(tmp, path)
    return path, n


def dump_overheads(cur, folder: Path, stamp: str, since: str, log=print) -> tuple[Path, int]:
    """Статьи БЕЗ серии, агрегат по подразделению, месяцу и статье (25 тыс.
    строк вместо 500 тыс.) — для ставки распределяемых по базам."""
    from app.fact_costs import OVERHEAD_PREFIX
    path = folder / f"{OVERHEAD_PREFIX}_{stamp}.csv"
    tmp = path.with_suffix(".csv.part")
    cur.execute("SELECT [Подразделение], LEFT(CONVERT(varchar, [Период], 120), 7) AS m, "
                "[СтатьяРасходов], SUM(TRY_CAST([СуммаБезНДС] AS float)), COUNT(*) "
                "FROM [dbo].[Прочие расход (все)] WHERE ([Серия] = '' OR [Серия] IS NULL) "
                "AND [Период] >= %s GROUP BY [Подразделение], LEFT(CONVERT(varchar, [Период], 120), 7), "
                "[СтатьяРасходов]", (since,))
    n = 0
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(["Подразделение", "Месяц", "СтатьяРасходов", "Сумма", "Строк"])
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            w.writerows([fmt(v) for v in row] for row in batch)
            n += len(batch)
    os.replace(tmp, path)
    return path, n


SALES_PREFIX = "_Продажи_регистр_"


def dump_sales(cur, folder: Path, stamp: str, since: str, log=print) -> tuple[Path, int]:
    """Регистр «ВыручкаИСебестоимостьПродаж» — агрегат по месяцу, номенклатуре,
    подразделению, покупателю и серии (10 тыс. строк вместо 250 тыс.): ориентир
    цены реализации по факту и цена по сделке для план/факта."""
    path = folder / f"{SALES_PREFIX}_{stamp}.csv"
    tmp = path.with_suffix(".csv.part")
    cur.execute(
        "SELECT LEFT(CONVERT(varchar, [Период], 120), 7), "
        "LEFT([АналитикаУчетаНоменклатуры], CHARINDEX(';', [АналитикаУчетаНоменклатуры] + ';') - 1), "
        "[Подразделение], "
        "LEFT([АналитикаУчетаПоПартнерам], CHARINDEX(';', [АналитикаУчетаПоПартнерам] + ';') - 1), "
        "[Серия], SUM([Количество]), SUM([СуммаВыручкиБезНДС]), SUM([СтоимостьБезНДС]), COUNT(*) "
        "FROM [dbo].[ВыручкаИСебестоимостьПродаж] WHERE [Период] >= %s AND [Количество] > 0 "
        "AND [ХозяйственнаяОперация] IN ('Реализация', 'Реализация (товары в пути)', 'Реализация в розницу') "
        "GROUP BY LEFT(CONVERT(varchar, [Период], 120), 7), "
        "LEFT([АналитикаУчетаНоменклатуры], CHARINDEX(';', [АналитикаУчетаНоменклатуры] + ';') - 1), "
        "[Подразделение], LEFT([АналитикаУчетаПоПартнерам], CHARINDEX(';', [АналитикаУчетаПоПартнерам] + ';') - 1), "
        "[Серия]", (since,))
    n = 0
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(["Месяц", "Номенклатура", "Подразделение", "Покупатель", "Серия",
                    "Количество", "ВыручкаБезНДС", "СебестоимостьБезНДС", "Строк"])
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            w.writerows([fmt(v) for v in row] for row in batch)
            n += len(batch)
    os.replace(tmp, path)
    return path, n


def _matrix_since() -> str:
    """С какой даты берём регистр затрат — настройка cost_matrix_from сервиса."""
    from app import cost_matrix
    from app.db import connect as db_connect, init_db
    init_db()
    c = db_connect()
    try:
        return cost_matrix.period_from(c)
    finally:
        c.close()


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
        # Регистр затрат по сериям — для матрицы фактических затрат.
        if not only or "затраты" in only:
            t1 = time.time()
            try:
                path, n = dump_fact_costs(cur, folder, stamp, _matrix_since(), log)
                from app.fact_costs import FILE_PREFIX
                drop_older(folder, FILE_PREFIX, keep, log)
                status["tables"].append({"prefix": FILE_PREFIX, "table": "Прочие расход (все) [серии]",
                                         "file": path.name, "rows": n, "seconds": round(time.time() - t1, 1)})
                log(f"  {'Прочие расход (все) [серии]':34s} {n:8d} строк -> {path.name}")
            except Exception as e:
                status["errors"].append(f"регистр затрат: {type(e).__name__}: {e}")
                log(f"  ОШИБКА регистр затрат: {type(e).__name__}: {e}")
            t1 = time.time()
            try:
                path, n = dump_overheads(cur, folder, stamp, _matrix_since(), log)
                from app.fact_costs import OVERHEAD_PREFIX
                drop_older(folder, OVERHEAD_PREFIX, keep, log)
                status["tables"].append({"prefix": OVERHEAD_PREFIX, "table": "Прочие расход (все) [без серии]",
                                         "file": path.name, "rows": n, "seconds": round(time.time() - t1, 1)})
                log(f"  {'Прочие расход (все) [без серии]':34s} {n:8d} строк -> {path.name}")
            except Exception as e:
                status["errors"].append(f"распределяемые: {type(e).__name__}: {e}")
                log(f"  ОШИБКА распределяемые: {type(e).__name__}: {e}")
            t1 = time.time()
            try:
                path, n = dump_sales(cur, folder, stamp, _matrix_since(), log)
                drop_older(folder, SALES_PREFIX, keep, log)
                status["tables"].append({"prefix": SALES_PREFIX, "table": "ВыручкаИСебестоимостьПродаж [агрегат]",
                                         "file": path.name, "rows": n, "seconds": round(time.time() - t1, 1)})
                log(f"  {'Продажи (регистр) [агрегат]':34s} {n:8d} строк -> {path.name}")
            except Exception as e:
                status["errors"].append(f"продажи: {type(e).__name__}: {e}")
                log(f"  ОШИБКА продажи: {type(e).__name__}: {e}")
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


def pull_deals(dry: bool = False) -> dict:
    """Реестр сделок Битрикса: самый свежий DEAL_*.xlsx из папки соседа → ref_deals,
    затем доля лота по всем БП. -> {file, rows, applied, error}."""
    from app import deals
    path = deals.newest(DEALS_DIR) if os.path.isdir(DEALS_DIR) else None
    if path is None:
        return {"file": None, "error": f"DEAL_*.xlsx не найден в {DEALS_DIR}"}
    if dry:
        return {"file": path.name, "rows": None, "applied": None}
    from app.db import connect, init_db
    init_db()
    conn = connect()
    try:
        st = deals.import_file(conn, path)
        applied = deals.apply_all(conn)
        conn.commit()
        print(f"  сделки Битрикса: {path.name}: {st['written']} с номером, "
              f"{st['with_share']} с долей; доля проставлена у {applied['written']} БП")
        return {"file": path.name, "rows": st["written"], "with_share": st["with_share"],
                "applied": applied["written"]}
    except Exception as e:
        print(f"  ОШИБКА реестра сделок: {type(e).__name__}: {e}")
        return {"file": path.name, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def pull_fact(dry: bool = False) -> dict:
    """Снимок факта реализации из сборки «Реализации» → bp_fact_snapshot."""
    if not os.path.isfile(FACT_JSON):
        return {"file": None, "error": f"нет файла {FACT_JSON}"}
    if dry:
        return {"file": os.path.basename(FACT_JSON)}
    from app import factsnap
    from app.db import connect, init_db
    init_db()
    conn = connect()
    try:
        st = factsnap.import_file(conn, FACT_JSON)
        from app import type_margin
        tm = type_margin.build(conn, FACT_JSON)
        conn.commit()
        # Факт затрат по сделкам (регистр 1С) → матрица затрат.
        from app import fact_costs, cost_matrix
        fc_path = fact_costs.newest_file(Path(FOLDER_FOR_FACT))
        if fc_path is not None:
            fc = fact_costs.import_file(conn, fc_path, FACT_JSON)
            oh_path = fact_costs.newest_overhead_file(Path(FOLDER_FOR_FACT))
            if oh_path is not None:
                oh = fact_costs.import_overhead_file(conn, oh_path)
                print(f"  распределяемые без серии: {oh['rows']} строк, {round(oh['amount'] / 1e6)} млн")
            cm = cost_matrix.build(conn)
            from app import norm_calib
            nc = norm_calib.build(conn)
            conn.commit()
            print(f"  калибровка нормативов: {nc['written']} фактов")
        # Архив книг экономистов (парсер Битрикса) — «что закладывали» по всем сделкам.
        from app import book_archive
        ba_path = book_archive.newest(DEALS_DIR)
        if ba_path is not None:
            ba = book_archive.build(conn, ba_path, FACT_JSON)
            conn.commit()
            print(f"  архив книг: {ba['versions']} версий, {ba['deals']} сделок, типов {ba['types']}")
        from app import outcome
        oc = outcome.build(conn, FACT_JSON)
        conn.commit()
        print(f"  обучающая выборка результата: {oc['deals']} закрытых сделок")
        # Сверка книг с фактом по каждой сделке — после архива, регистра и ставок.
        from app import audit
        au = audit.build(conn, FACT_JSON)
        conn.commit()
        print(f"  сверка книг с фактом: {au['deals']} сделок, с книгой {au['with_book']}, "
              f"с полным фактом затрат {au['with_costs']}")
            print(f"  факт затрат: {fc['series']} серий, {fc['rows']} строк, "
                  f"{round(fc['amount'] / 1e6, 1)} млн; не сопоставлено статей: {len(fc['unmapped'])}; "
                  f"матрица: {cm['rows']} строк из {cm['observations']} наблюдений")
            st["fact_costs"] = {"series": fc["series"], "rows": fc["rows"], "unmapped": fc["unmapped"],
                                "matrix_rows": cm["rows"]}
        print(f"  снимок «Реализации» {st['generated']}: БП в файле {st['bps_in_file']}, "
              f"наших с фактом {st['matched']}; факт по типам: {tm['typed']} из "
              f"{tm['closed']} закрытых сделок")
        return {"file": os.path.basename(FACT_JSON), **st, "type_margin": tm}
    except Exception as e:
        print(f"  ОШИБКА снимка факта: {type(e).__name__}: {e}")
        return {"file": os.path.basename(FACT_JSON), "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="выгрузка 1С из MSSQL Extractor и импорт")
    ap.add_argument("--folder", default=DEFAULT_FOLDER, help="куда класть CSV (по умолчанию 1c/)")
    ap.add_argument("--no-import", action="store_true", help="только выгрузить файлы")
    ap.add_argument("--only", nargs="*", help="только эти таблицы/префиксы")
    ap.add_argument("--keep", type=int, default=2, help="сколько прежних выгрузок оставлять")
    a = ap.parse_args(argv)
    folder = Path(a.folder)
    global FOLDER_FOR_FACT
    FOLDER_FOR_FACT = str(folder)
    print(f"Выгрузка из {settings()['host']}/{settings()['database']} в {folder.resolve()}")
    status = pull(folder, set(a.only) if a.only else None, a.keep)
    status["imported"] = False
    # Порядок важен: сначала справочники и статистика из таблиц 1С (в том
    # числе тоннаж подразделений по отвесным, ставки переработки), потом
    # реестр сделок и снимок факта — матрица и калибровка считаются в конце
    # и опираются на всё загруженное.
    if not a.no_import and status["tables"]:
        from import_1c_csv import import_all
        try:
            import_all(str(folder), author="ночной импорт из Extractor")
            status["imported"] = True
        except Exception as e:
            status["errors"].append(f"импорт: {type(e).__name__}: {e}")
            print(f"ОШИБКА импорта: {type(e).__name__}: {e}")
    status["deals"] = pull_deals(a.no_import)
    status["fact"] = pull_fact(a.no_import)
    if status["deals"].get("error"):
        status["errors"].append("реестр сделок: " + status["deals"]["error"])
    if status["fact"].get("error"):
        status["errors"].append("снимок факта: " + status["fact"]["error"])
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
