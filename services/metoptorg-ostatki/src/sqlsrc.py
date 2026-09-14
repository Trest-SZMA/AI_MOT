# -*- coding: utf-8 -*-
"""Источники данных из MS SQL (база «Extractor», куда 1С складывает таблицы).

Адаптировано из сервиса реализации (metoptorg-realizaciya). Для сервиса ОСТАТКОВ
из SQL берутся ДВИЖЕНИЯ (обороты) и справочники; ФАКТИЧЕСКИЕ ОСТАТКИ приходят
файлом Excel («Остатки на складах ДД.ММ.ГГ.xlsx») — дата отчёта берётся из имени.

⚠️ ПАРОЛЬ ЖИВЁТ ТОЛЬКО В ОКРУЖЕНИИ (`METOPTORG_SQL_PASSWORD`), в
`/etc/metoptorg-ostatki.env` (права 600). Ни в sql_sources.json, ни в git, ни в
интерфейсе его нет.

⚠️ РАСЧЁТ НЕ ЗНАЕТ ПРО SQL. Таблица выгружается в тот же csv, который раньше
клали руками, дальше работает прежний конвейер (stock_report.py). У SQL и у файла
ровно один код разбора — переключение источника не может сдвинуть цифры.
"""
import os, re, csv, json, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(_HERE, "..", "data")
CONFIG = os.path.join(DATA, "sql_sources.json")

# Ключ входа -> (подпись, шаблон имени файла). Порядок = порядок строк на странице.
# ⚠️ «stock» (фактические остатки) здесь НЕТ намеренно: это Excel с датой в имени,
#    он грузится локально. Имена файлов должны совпадать с тем, что ищет
#    stock_report.py (newest по шаблону).
SOURCES = [
    ("movements", "Движения (обороты регистра «Себестоимость товаров»)",
     "СебестоимостьТоваровОбороты_%s.csv"),
    ("nomen",     "Номенклатура (единицы, коэффициент, отчётная группа)",
     "Номенклатура_%s.csv"),
    ("series",    "Серии номенклатуры (серия → контрагент, дата вывоза)",
     "СерииНоменклатуры_%s.csv"),
    ("sklady",    "Склады (склад → высший родитель = база)",
     "Склады_%s.csv"),
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
ENV_FILE = "/etc/metoptorg-ostatki.env"
# Имя таблицы приходит из браузера. Пускаем внутрь только буквы (любой алфавит),
# цифры, подчёркивание, ПРОБЕЛ, дефис, точку и квадратные скобки — остальное
# отсекается. Само имя всегда оборачивается в [...] (см. _quote).
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
    cfg.pop("password", None)
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


# ⚠️ ДРАЙВЕР — python-tds (pytds), а не pymssql: SQL Server хранит пароль как
# UTF-16, python-tds кодирует его по спецификации (как драйвер Microsoft/JDBC),
# поэтому кириллические пароли работают. pymssql оставлен запасным для ASCII.
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
    name, mod = _driver()
    if not name:
        return False, "драйвер не установлен: нужен python-tds (pip install python-tds)"
    if name == "pymssql":
        return True, ("pymssql %s — ⚠️ запасной: пароль с кириллицей не отработает, "
                      "поставьте python-tds" % getattr(mod, "__version__", "?"))
    return True, "python-tds (кодирует пароль в UTF-16, как драйвер Microsoft)"


def connect(cfg=None):
    cfg = cfg or load_config()
    pwd = password()
    if not pwd:
        raise RuntimeError(
            "пароль не задан: добавьте %s=… в %s (права 600) и перезапустите службу"
            % (PASSWORD_ENV, ENV_FILE))
    name, mod = _driver()
    if not name:
        raise RuntimeError(driver_status()[1])
    host, port = str(cfg.get("host") or ""), int(cfg.get("port") or 1433)
    user, db = str(cfg.get("user") or ""), str(cfg.get("database") or "")
    tmo = int(cfg.get("timeout") or 60)
    if name == "pytds":
        return mod.connect(dsn=host, port=port, database=db, user=user,
                           password=pwd, login_timeout=15, timeout=tmo, autocommit=True)
    return mod.connect(server=host, port=str(port), user=user, password=pwd,
                       database=db, timeout=tmo, login_timeout=15, charset="UTF-8")


def test_connection(cfg=None):
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
        if "18456" in msg or "вход" in msg.lower() or "login failed" in msg.lower():
            msg += ("  ← сервер отверг логин/пароль. Проверьте %s в %s "
                    "(перезапуск службы обязателен)" % (PASSWORD_ENV, ENV_FILE))
        return False, "%s: %s" % (type(e).__name__, msg)


def list_tables(cfg=None):
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
    parts = [p.strip("[]") for p in str(table).split(".") if p.strip("[]")]
    return ".".join("[%s]" % p.replace("]", "]]") for p in parts)


def probe(table, cfg=None, limit=3):
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
    """Таблица -> csv в data/ с тем же именем, что у ручной выгрузки. UTF-8 без BOM."""
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
    """Прежние выгрузки того же входа удалить (файл движений ~200 МБ — не копим)."""
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
