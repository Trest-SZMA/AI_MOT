# -*- coding: utf-8 -*-
"""Источники данных из MS SQL (база «Extractor», куда 1С складывает таблицы).

Зачем: до сих пор сервис жил на файловых выгрузках — экономист выгружал csv/xlsx
и клал их на страницу обновления. Отсюда две болячки, обе стоили тонн:
справочник серий отставал от движений (7,3 тыс. т без проекта), а снимок
остатков был на другую дату, чем обороты. Прямое чтение из «Extractor» снимает
и то, и другое.

⚠️ ПАРОЛЬ ЖИВЁТ ТОЛЬКО В ОКРУЖЕНИИ (`METOPTORG_SQL_PASSWORD`), рядом с паролем
самого сервиса в `/etc/metoptorg-realizaciya.env` (права 600). Ни в
`sql_sources.json`, ни в git, ни в интерфейсе его нет и быть не должно: файл
настроек редактируется из браузера и лежит в `data/`.

⚠️ РАСЧЁТ НЕ ЗНАЕТ ПРО SQL. Таблица выгружается в тот же csv, который раньше
клали руками, и дальше работает прежний конвейер. Это осознанно: так у SQL и у
файла ровно один код разбора, и переключение источника не может сдвинуть цифры.

⚠️ ИМЕНА ТАБЛИЦ НЕ УГАДЫВАЮТСЯ. В «Extractor» они свои, поэтому таблица для
каждого входа задаётся руками на вкладке «Обновление данных»: рядом с подписью
входа — поле с именем таблицы и кнопка «проверить». `list_tables()` показывает,
из чего выбирать.
"""
import os, re, csv, json, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(_HERE, "..", "data")
CONFIG = os.path.join(DATA, "sql_sources.json")

# Ключ входа -> (подпись, шаблон имени файла, куда выгружать).
# Порядок = порядок строк на странице обновления.
# `Проверка_БП_*` (Битрикс) здесь нет намеренно: это не 1С, файл приходит из
# другой системы и остаётся ручным.
SOURCES = [
    ("movements", "Движения (обороты регистра «Себестоимость товаров»)",
     "СебестоимостьТоваровОбороты_%s.csv"),
    ("stock",     "Остатки на складах (эталон сверки)",
     "СебестоимостьТоваровОстатки_%s.csv"),
    ("nomen",     "Номенклатура (единицы, коэффициент, отчётная группа)",
     "Номенклатура_%s.csv"),
    ("series",    "Серии номенклатуры (серия → проект = бизнес-план)",
     "СерииНоменклатуры_%s.csv"),
    ("sklady",    "Склады (склад → высший родитель)",
     "Склады_%s.csv"),
    ("divisions", "Производственные подразделения (цех / база / заготовка)",
     "_Производственные_подразделения__%s.csv"),
    ("payments",  "Оплаты за лом (месяц платежа → точка на Ганте)",
     "ОплатаЗаЛом_%s.csv"),
    # ⚠️ Имя файла должно совпасть с тем, что ищет sales_report.py
    # (`*Объемы_на_загрузку*.csv`), иначе выгрузка ляжет на диск и останется
    # незамеченной: расчёт её просто не увидит, а человек будет думать, что
    # план подтянулся.
    ("prodplan",  "План по объёмам производства (рамки на Ганте)",
     "_Объемы_на_загрузку__%s.csv"),
    # ⚠️ То же правило про имя файла: расчёт ищет
    # `*ЗначенияНефинансовыхПоказателей*.csv`. Комментарии — это ИСТОРИЯ (39
    # срезов за полтора года в выгрузке 19.08.2026), поэтому выгружать надо всю
    # таблицу, а не последний срез: лента как раз и показывает, как менялась
    # причина, по которой остаток не вывозят.
    ("stock_notes", "Комментарии по запасам (лента у остатка на Ганте)",
     "ЗначенияНефинансовыхПоказателей_%s.csv"),
    # ФАКТ ВЫРУЧКИ (14.09.2026): регистр «Выручка и себестоимость продаж» —
    # выручка без НДС и себестоимость по документу реализации и номенклатуре.
    # Расчёт ищет `ВыручкаИСебестоимостьПродаж_*.csv`; таблицу в «Extractor»
    # заказчик привязывает сам на странице обновления.
    ("revenue",   "Выручка и себестоимость продаж (факт выручки по сериям)",
     "ВыручкаИСебестоимостьПродаж_%s.csv"),
]
SOURCE_LABEL = {k: lbl for k, lbl, _ in SOURCES}
SOURCE_FILE = {k: pat for k, _, pat in SOURCES}

DEFAULT_CONFIG = {
    "enabled": False,
    "host": "10.100.1.110",
    "port": 1433,
    "database": "Extractor",
    "user": "pyatunin",
    "timeout": 60,
    "sources": {},          # ключ -> {"table": "...", "enabled": bool}
}

PASSWORD_ENV = "METOPTORG_SQL_PASSWORD"
# Имя таблицы приходит из браузера и подставляется в SQL. Пускаем внутрь только
# буквы (любого алфавита — в «Extractor» имена русские), цифры, подчёркивание,
# ПРОБЕЛ, дефис, точку (schema.table) и квадратные скобки. Всё остальное —
# кавычки, точка с запятой, скобки, звёздочка, знак равенства — отсекается,
# дописать свой запрос через имя нельзя. Само имя всегда оборачивается в
# `[...]` (см. `_quote`), поэтому пробелы и дефисы внутри безопасны.
#
# ⚠️ ПРОБЕЛ ОБЯЗАТЕЛЕН, И ЭТО НЕ ПОСЛАБЛЕНИЕ. В «Extractor» таблицы называются
# «Производственные подразделения», «Производственная себестоимость» — с
# пробелом. Без него не только не проверялась эта строка: сохранение формы
# отваливалось ЦЕЛИКОМ (одно плохое имя -> 400 на весь запрос -> «не
# сохранилось»), и пользователь терял все семь заполненных полей.
# `А-Яа-я` тоже мало: «ё» и «Ё» в эти диапазоны не входят, поэтому `\w`.
SAFE_NAME = re.compile(r"^[\w \-.$#\[\]]{1,200}$", re.UNICODE)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k != "password"})
    except FileNotFoundError:
        pass
    except Exception:
        pass                        # битый файл не должен ронять сервис
    cfg.pop("password", None)       # на всякий случай: пароль тут не хранится
    cfg["sources"] = {k: {"table": (cfg.get("sources", {}).get(k) or {}).get("table", ""),
                          "enabled": bool((cfg.get("sources", {}).get(k) or {}).get("enabled"))}
                      for k, _, _ in SOURCES}
    return cfg


def save_config(cfg):
    cur = load_config()
    for k in ("enabled", "host", "port", "database", "user", "timeout"):
        if k in cfg:
            cur[k] = cfg[k]
    for k, _, _ in SOURCES:
        s = (cfg.get("sources") or {}).get(k)
        if isinstance(s, dict):
            cur["sources"][k] = {"table": str(s.get("table") or "").strip(),
                                 "enabled": bool(s.get("enabled"))}
    cur.pop("password", None)
    os.makedirs(DATA, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)
    return cur


def password():
    return os.environ.get(PASSWORD_ENV, "")


# ⚠️ ДРАЙВЕР — python-tds, А НЕ pymssql, И ЭТО ВАЖНО ДЛЯ НЕ-ASCII ПАРОЛЕЙ.
# SQL Server хранит пароль как UTF-16, и по протоколу TDS его так и надо
# передавать. `pymssql` (обёртка над FreeTDS) кириллицу в пароле не
# перекодирует и получает отказ входа 18456 при верном пароле — проверено на
# этой установке на всех кодировках (UTF-8, CP1251, ISO-8859-5, CP866).
# `python-tds` — чистый Python, кодирует пароль по спецификации, как это делает
# JDBC-драйвер Microsoft (которым работает DBeaver).
# pymssql оставлен запасным: если python-tds почему-то не встанет, ASCII-пароль
# он отработает.
def _driver():
    try:
        import pytds
        return "pytds", pytds
    except Exception:
        pass
    try:
        import pymssql
        return "pymssql", pymssql
    except Exception:
        return None, None


def driver_status():
    """-> (есть ли драйвер, что показать человеку)."""
    name, mod = _driver()
    if not name:
        return False, ("драйвер не установлен: нужен python-tds "
                       "(pip install python-tds)")
    if name == "pymssql":
        return True, ("pymssql %s — ⚠️ запасной драйвер: пароль с кириллицей "
                      "он не отработает, поставьте python-tds"
                      % getattr(mod, "__version__", "?"))
    return True, "python-tds (кодирует пароль в UTF-16, как драйвер Microsoft)"


def connect(cfg=None):
    cfg = cfg or load_config()
    pwd = password()
    if not pwd:
        raise RuntimeError(
            "пароль не задан: добавьте %s=… в /etc/metoptorg-realizaciya.env "
            "(права 600) и перезапустите службу" % PASSWORD_ENV)
    name, mod = _driver()
    if not name:
        raise RuntimeError(driver_status()[1])
    host, port = str(cfg.get("host") or ""), int(cfg.get("port") or 1433)
    user, db = str(cfg.get("user") or ""), str(cfg.get("database") or "")
    tmo = int(cfg.get("timeout") or 60)
    if name == "pytds":
        return mod.connect(dsn=host, port=port, database=db, user=user,
                           password=pwd, login_timeout=15, timeout=tmo,
                           autocommit=True)
    return mod.connect(server=host, port=str(port), user=user, password=pwd,
                       database=db, timeout=tmo, login_timeout=15,
                       charset="UTF-8")


def test_connection(cfg=None):
    """-> (ок, сообщение). Ничего не читает, только проверяет вход."""
    ok, drv = driver_status()
    if not ok:
        return False, drv
    try:
        with connect(cfg) as cn:
            cur = cn.cursor()
            cur.execute("SELECT @@VERSION, DB_NAME(), SUSER_SNAME()")
            ver, db, who = cur.fetchone()
        return True, "подключение есть · база %s · пользователь %s · %s" % (
            db, who, str(ver).splitlines()[0][:80])
    except Exception as e:
        msg = str(e).replace("\n", " ").strip()
        # 18456 — сервер ДОШЁЛ и отверг пару логин/пароль. Это не сеть и не
        # драйвер: сообщение приходит от самого SQL Server, часто по-русски.
        if "18456" in msg or "вход" in msg.lower() or "login failed" in msg.lower():
            msg += ("  ← сервер отверг логин/пароль. Сеть и драйвер ни при чём: "
                    "проверьте значение %s в /etc/metoptorg-realizaciya.env "
                    "(перезапуск службы обязателен) и что вход «%s» не заблокирован"
                    % (PASSWORD_ENV, (cfg or load_config()).get("user")))
        return False, "%s: %s" % (type(e).__name__, msg)


def list_tables(cfg=None):
    """Таблицы и представления базы с числом строк — из чего выбирать."""
    with connect(cfg) as cn:
        cur = cn.cursor()
        cur.execute("""
            SELECT s.name, t.name, ISNULL(p.rows, 0)
              FROM sys.objects t
              JOIN sys.schemas s ON s.schema_id = t.schema_id
              LEFT JOIN (SELECT object_id, SUM(rows) rows
                           FROM sys.partitions WHERE index_id IN (0,1)
                          GROUP BY object_id) p ON p.object_id = t.object_id
             WHERE t.type IN ('U','V')
             ORDER BY s.name, t.name""")
        return [{"schema": a, "name": b, "rows": int(c or 0)} for a, b, c in cur.fetchall()]


def _quote(table):
    """`schema.table` -> `[schema].[table]`. Имя уже проверено SAFE_NAME."""
    parts = [p.strip("[]") for p in str(table).split(".") if p.strip("[]")]
    return ".".join("[%s]" % p.replace("]", "]]") for p in parts)


def probe(table, cfg=None, limit=3):
    """Сколько строк и какие колонки — чтобы человек убедился, что таблица та."""
    if not SAFE_NAME.match(str(table or "")):
        raise ValueError("недопустимое имя таблицы")
    with connect(cfg) as cn:
        cur = cn.cursor()
        cur.execute("SELECT COUNT(*) FROM %s" % _quote(table))
        n = cur.fetchone()[0]
        cur.execute("SELECT TOP %d * FROM %s" % (int(limit), _quote(table)))
        cols = [d[0] for d in cur.description]
        rows = [[("" if v is None else str(v))[:60] for v in r] for r in cur.fetchall()]
    return {"rows": int(n), "columns": cols, "sample": rows}


def dump_to_csv(key, table, cfg=None, stamp=None, log=print):
    """Таблица -> csv в `data/` с тем же именем, что у ручной выгрузки.

    Дальше файл читает прежний загрузчик — расчёт про SQL не знает.
    Пишем UTF-8 без BOM и с теми же кавычками, что у выгрузок 1С.
    """
    if not SAFE_NAME.match(str(table or "")):
        raise ValueError("недопустимое имя таблицы: %r" % table)
    pat = SOURCE_FILE.get(key)
    if not pat:
        raise ValueError("неизвестный источник: %r" % key)
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d%H%M")
    path = os.path.join(DATA, pat % stamp)
    tmp = path + ".part"
    n = 0
    with connect(cfg) as cn:
        cur = cn.cursor()
        cur.execute("SELECT * FROM %s" % _quote(table))
        cols = [d[0] for d in cur.description]
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            w.writerow(cols)
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                for r in batch:
                    w.writerow(["" if v is None else v for v in r])
                n += len(batch)
                if n % 50000 == 0:
                    log("    %s: %d строк…" % (key, n))
    os.replace(tmp, path)
    _drop_older(key, path, log)
    log("  %-10s %-40s %7d строк -> %s" % (key, table, n, os.path.basename(path)))
    return path, n


def _drop_older(key, keep, log=print):
    """Прежние выгрузки того же входа — УДАЛИТЬ, оставив только текущую.

    ⚠️ Раньше уводили в `data/archive/`, как при ручной загрузке через браузер.
    Для SQL это не годится: файл движений весит ~325 МБ, и КАЖДАЯ ночная
    пересборка создаёт новый — за месяц это десять гигабайт архива, который
    никто не смотрит. Смысл архива был в том, что руками положенный файл нечем
    заменить, если он битый; из базы он перекачивается за двадцать минут.
    Поэтому здесь именно удаление (решение заказчика 12.08.2026).
    ⚠️ Удаляем ТОЛЬКО файлы того же входа и только не тот, что сейчас записан.
    """
    import glob as _glob
    pat = SOURCE_FILE.get(key)
    if not pat:
        return
    for old in _glob.glob(os.path.join(DATA, pat % "*")):
        if os.path.abspath(old) == os.path.abspath(keep):
            continue
        try:
            mb = os.path.getsize(old) / 1048576.0
            os.remove(old)
            log("    удалена прежняя выгрузка %s (%.1f МБ)" % (os.path.basename(old), mb))
        except Exception as e:
            log("    не смог удалить %s: %s" % (os.path.basename(old), e))


def pull_enabled(cfg=None, log=print):
    """Выгрузить все включённые источники. -> (список (ключ, файл, строк), ошибки)."""
    cfg = cfg or load_config()
    done, errors = [], []
    if not cfg.get("enabled"):
        return done, errors
    ok, drv = driver_status()
    if not ok:
        return done, ["SQL включён, но %s" % drv]
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M")
    for key, label, _ in SOURCES:
        s = (cfg.get("sources") or {}).get(key) or {}
        if not s.get("enabled") or not s.get("table"):
            continue
        try:
            path, n = dump_to_csv(key, s["table"], cfg, stamp=stamp, log=log)
            done.append((key, path, n))
        except Exception as e:
            errors.append("%s (%s): %s: %s" % (label, s.get("table"), type(e).__name__, e))
    return done, errors
