#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Веб-приложение сервиса реализации: выгрузки 1С загружаются через браузер,
отчёт пересобирается на сервере, дашборд и Excel раздаются готовыми.

    python3 server.py                      # локально, http://127.0.0.1:8080
    METOPTORG_PASSWORD=… uvicorn server:app --host 0.0.0.0 --port 8080

Что важно в устройстве:
* Файл движений весит ~300 МБ, поэтому загрузка идёт ПОТОКОМ прямо на диск
  (никакого multipart и чтения в память): браузер шлёт тело файла как есть,
  имя — в query-параметре. Это же даёт честный прогресс загрузки.
* Пересборка запускается ОДНА за раз (файловый замок) и живёт отдельным
  процессом; лог пишется на диск и отдаётся в браузер по мере появления.
* Готовая сборка снимается в builds/<дата>/, а builds/current указывает на
  последнюю удачную. Пока идёт пересборка, пользователи видят предыдущую —
  недособранный дашборд наружу не попадает.
* Прежние выгрузки не удаляются, а уезжают в data/archive/ — если новый файл
  окажется битым, есть к чему вернуться.
"""
import os, re, sys, json, shutil, secrets, subprocess, threading, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
try:
    import sqlsrc                      # источники из MS SQL «Extractor»
except Exception:                      # сервис обязан подниматься и без драйвера
    sqlsrc = None

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import (HTMLResponse, JSONResponse, FileResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.security import HTTPBasic, HTTPBasicCredentials

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
BUILDS = os.path.join(ROOT, "builds")
LOGS = os.path.join(ROOT, "logs")
LOCK = os.path.join(LOGS, "build.lock")
STATE = os.path.join(LOGS, "state.json")

for d in (DATA, OUT, BUILDS, LOGS, os.path.join(DATA, "archive")):
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------------------
# Какие файлы принимаем. Имя должно совпасть с шаблоном — иначе конвейер его
# просто не увидит, и пользователь будет думать, что данные обновились.
# ---------------------------------------------------------------------------
ACCEPT = [
    ("movements", re.compile(r"^СебестоимостьТоваровОбороты_.*\.csv$", re.I),
     "Движения (обороты регистра «Товары на складах»)"),
    ("stock",     re.compile(r"^Остатки на складах.*\.xlsx?$", re.I),
     "Остатки на сегодня — якорь обратного хода"),
    ("nomen",     re.compile(r"^Номенклатура_.*\.csv$", re.I),
     "Справочник номенклатуры"),
    ("series",    re.compile(r"^СерииНоменклатуры_.*\.csv$", re.I),
     "Справочник серий (проект = бизнес-план, дата вывоза)"),
    ("sklady",    re.compile(r"^Склады_.*\.csv$", re.I),
     "Справочник складов"),
    ("divisions", re.compile(r"^.*Производственные_подразделения.*\.csv$", re.I),
     "Производственные подразделения (цех / база / заготовка)"),
    ("payments",  re.compile(r"^ОплатаЗаЛом_.*\.csv$", re.I),
     "Оплаты за лом (когда и по какому БП платили)"),
    ("overrides", re.compile(r"^overrides\.json$", re.I),
     "Ручные привязки серий"),
]
STEPS = [
    ("Расчёт (sales_report.py)", [sys.executable, "sales_report.py"]),
    ("Дашборд (make_dash.py)",   [sys.executable, "make_dash.py"]),
    ("Excel (make_excel.py)",    [sys.executable, "make_excel.py"]),
    ("Контрольные примеры §12",  [sys.executable, "tools/control_check.py"]),
    ("Проверка выгрузки в 1С",   [sys.executable, "tools/check_1c_xlsx.py"]),
]

app = FastAPI(title="МЕТОПТОРГ · Реализация", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)
PASSWORD = os.environ.get("METOPTORG_PASSWORD", "")
USER = os.environ.get("METOPTORG_USER", "metoptorg")


# АДМИНИСТРАТОРЫ — ПОИМЁННО (решение заказчика 14.09.2026: «при открытии сервиса
# сразу дашборд, а страница с пересборками — только у администраторов, меня и
# Владимира, без дополнительных страниц и авторизаций»). Никаких сессий, форм
# и куки: HTTP Basic, который браузер спрашивает ОДИН раз на странице /admin и
# дальше подставляет сам. Экономист, открывший «/», пароля не видит никогда.
# Список — в окружении службы: METOPTORG_ADMINS="pavel:пароль,vladimir:пароль".
# Старая пара METOPTORG_USER/PASSWORD остаётся как ещё один администратор.
def _parse_admins(raw):
    out = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        u, pw = pair.split(":", 1)
        if u.strip() and pw:
            out[u.strip()] = pw
    return out


ADMINS = _parse_admins(os.environ.get("METOPTORG_ADMINS", ""))
if PASSWORD:
    ADMINS.setdefault(USER, PASSWORD)
# ⚠️ Дальше по коду «PASSWORD задан» значит «есть хоть один администратор».
PASSWORD = PASSWORD or ("x" if ADMINS else "")


# Открытое чтение: смотреть отчёт можно без пароля, а менять данные — нет.
# Просить пароль у экономиста, который просто открыл дашборд, незачем: сервис
# живёт во внутренней сети. Но заливка выгрузки и пересборка подменяют отчёт
# ЦЕЛИКОМ для всех сразу, поэтому они остаются под паролем даже в этом режиме.
# Флаг снимается тем же способом, что и ставится, — правкой env и рестартом.
OPEN_READ = (os.environ.get("METOPTORG_OPEN_READ", "").strip().lower()
             not in ("", "0", "нет", "false", "off"))

# Адрес общего портала: значок в шапке страницы управления ведёт туда (решение
# заказчика 13.08.2026, цепочка «отчёт -> управление -> портал»). В коде адреса
# нет — репозиторий на GitHub публичный. Если переменную не задать, страница
# сама подставит ТОТ ЖЕ хост и порт 8079: портал живёт на этой же машине.
PORTAL_URL = os.environ.get("METOPTORG_PORTAL_URL", "").strip()


def _password_ok(cred):
    """Сверка пары логин/пароль. compare_digest на строках падает на не-ASCII,
    поэтому оборачиваем: чужой ввод не должен ронять сервер пятисоткой."""
    if cred is None or not ADMINS:
        return False
    try:
        pw = ADMINS.get(cred.username)
        return bool(pw) and secrets.compare_digest(cred.password, pw)
    except TypeError:
        return False


def _need_login():
    raise HTTPException(status_code=401, detail="Требуется вход",
                        headers={"WWW-Authenticate": 'Basic realm="metoptorg"'})


def auth(request: Request, cred: HTTPBasicCredentials = Depends(security)):
    """ЧТЕНИЕ: дашборд, Excel, снимок, история сборок, страница обновления и
    правка срока вывоза. Пароль задаётся переменной окружения; если он пуст
    или включён METOPTORG_OPEN_READ — вход открыт. Это допустимо только внутри
    доверенной сети, о чём говорим прямо в UI."""
    if not PASSWORD or OPEN_READ:
        return "открытый доступ"
    if not _password_ok(cred):
        _need_login()
    return cred.username


def auth_write(request: Request, cred: HTTPBasicCredentials = Depends(security)):
    """ЗАПИСЬ, меняющая отчёт целиком: заливка выгрузки 1С и пересборка.
    Пароль спрашивается всегда, когда он задан, — даже при открытом чтении."""
    if not PASSWORD:
        return "открытый доступ"
    if not _password_ok(cred):
        _need_login()
    return cred.username


# ---------------------------------------------------------------------------
# Состояние сборки
# ---------------------------------------------------------------------------
_lock = threading.Lock()


def read_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"state": "нет данных", "step": "", "started": "", "finished": "",
                "ok": None, "log": ""}


def write_state(**kw):
    st = read_state()
    st.update(kw)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, STATE)


def human(n):
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or u == "ГБ":
            return f"{n:.1f} {u}" if u != "Б" else f"{n} Б"
        n /= 1024


def data_files():
    """Что сейчас лежит во входных данных — по ролям, с датой и размером."""
    out = []
    names = sorted(os.listdir(DATA))
    for key, pat, title in ACCEPT:
        hits = [n for n in names if pat.match(n)]
        hits.sort(key=lambda n: os.path.getmtime(os.path.join(DATA, n)))
        cur = hits[-1] if hits else None
        rec = {"key": key, "title": title, "pattern": pat.pattern, "name": cur,
               "size": "", "mtime": "", "extra": len(hits) - 1 if hits else 0}
        if cur:
            p = os.path.join(DATA, cur)
            rec["size"] = human(os.path.getsize(p))
            rec["mtime"] = datetime.datetime.fromtimestamp(
                os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        out.append(rec)
    return out


def builds_list():
    """История сборок: свежие сверху."""
    out = []
    for n in sorted(os.listdir(BUILDS), reverse=True):
        d = os.path.join(BUILDS, n)
        if n == "current" or not os.path.isdir(d) or os.path.islink(d):
            continue
        meta = {}
        try:
            meta = json.load(open(os.path.join(d, "build.json"), encoding="utf-8"))
        except Exception:
            pass
        out.append({"id": n, "when": meta.get("finished", n),
                    "ok": meta.get("ok"), "total": meta.get("total_tonnes"),
                    "checks": meta.get("checks", ""),
                    "published": meta.get("published", True),
                    "hold": meta.get("hold", []),
                    "size": human(sum(os.path.getsize(os.path.join(d, f))
                                      for f in os.listdir(d)
                                      if os.path.isfile(os.path.join(d, f))))})
    return out[:30]


def current_dir():
    """Каталог последней УДАЧНОЙ сборки (её и раздаём, пока идёт новая)."""
    cur = os.path.join(BUILDS, "current")
    if os.path.isdir(cur):
        return cur
    return OUT


def _run_build(logfile):
    """Последовательный прогон конвейера. Всё пишем в лог: и шаги, и ошибки."""
    ok = True
    with open(logfile, "w", encoding="utf-8", buffering=1) as lg:
        def say(s):
            lg.write(s + "\n"); lg.flush()
        say("Пересборка начата: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # ---- источники из MS SQL ----
        # Таблицы выгружаются в те же csv, что раньше клали руками, и дальше
        # работает прежний конвейер: у SQL и у файла один код разбора, поэтому
        # смена источника не может сдвинуть цифры.
        # ⚠️ Ошибка выгрузки НЕ валит сборку: если SQL недоступен, отчёт
        # соберётся на прошлых файлах — это лучше, чем остаться без отчёта.
        # В логе она видна, и на странице обновления тоже.
        if sqlsrc is not None:
            try:
                cfg = sqlsrc.load_config()
                if cfg.get("enabled"):
                    write_state(step="Забираю таблицы из SQL")
                    say("\n" + "=" * 70 + "\nИсточники из MS SQL («%s» на %s)\n"
                        % (cfg.get("database"), cfg.get("host")) + "=" * 70)
                    done, errs = sqlsrc.pull_enabled(cfg, log=say)
                    for e in errs:
                        say("  !! " + e)
                    if not done and not errs:
                        say("  ни один источник не включён — работаем на файлах")
                    if errs:
                        say("  ⚠️ часть таблиц не выгрузилась: сборка пойдёт "
                            "на прежних файлах из data/")
            except Exception as e:
                say("  !! источники SQL: %s: %s" % (type(e).__name__, e))
        for title, cmd in STEPS:
            write_state(step=title)
            say("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)
            try:
                p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
                for line in p.stdout:
                    lg.write(line); lg.flush()
                code = p.wait()
            except Exception as e:
                say(f"ОШИБКА запуска: {e!r}")
                code = 1
            if code != 0:
                say(f"\n!! шаг «{title}» завершился с кодом {code}")
                # −15 = SIGTERM. Сами шаги так не падают: их сносит systemd
                # вместе со службой, когда во время сборки делают restart
                # (то есть выкладку). Пишем прямо, иначе ищут ошибку в расчёте.
                if code == -15:
                    say("   Это SIGTERM: процесс убит извне, а не упал сам.")
                    say("   Почти всегда — `systemctl restart` во время сборки")
                    say("   (выкладка). Данные целы, надо просто пересобрать.")
                elif code == -9:
                    say("   Это SIGKILL: почти всегда нехватка памяти "
                        "(MemoryMax юнита).")
                # контрольные проверки не валят сборку — они информативные
                if "control_check" in cmd[-1] or "check_1c" in cmd[-1]:
                    say("   (проверка информативная, сборка продолжается)")
                else:
                    ok = False
                    break
        say("\nИтог: " + ("успешно" if ok else "СБОРКА НЕ УДАЛАСЬ"))
    return ok


# ---------------------------------------------------------------------------
# Предохранитель публикации
# ---------------------------------------------------------------------------
# 13.08.2026 «Extractor» перезалил dbo.СебестоимостьТоваровОбороты окном в
# 2,5 месяца: 306 817 строк превратились в 20 585, история с 2021 года пропала.
# Расчёт отработал ЧЕСТНО по тому, что ему дали, шаги конвейера не упали,
# сборка сама себя объявила успешной — и заказчик увидел 28 935 т вместо
# 541 032 т без единого предупреждения. Контрольные примеры просели с 10 до 2,
# но они помечены «информативные» и публикацию не останавливают.
#
# Отсюда правило: сборка ПУБЛИКУЕТСЯ (builds/current переключается) только
# если она похожа на предыдущую опубликованную. Снимок сохраняется всегда —
# его видно в истории сборок и можно опубликовать вручную, когда обвал
# объяснён. Порог — доля падения, 0.10 = «упало больше чем на 10 % — держим».
PUBLISH_MAX_DROP = float(os.environ.get("METOPTORG_PUBLISH_MAX_DROP", "0.20"))
PUBLISH_GUARD = (os.environ.get("METOPTORG_PUBLISH_GUARD", "").strip().lower()
                 not in ("0", "нет", "false", "off"))


def _passed_examples(logtext):
    """Сколько контрольных примеров прошло: «ИТОГ: пройдено 10 из 10» -> 10."""
    m = re.findall(r"ИТОГ:\s*пройдено\s+(\d+)\s+из\s+(\d+)", logtext)
    return (int(m[-1][0]), int(m[-1][1])) if m else (None, None)


def _publish_hold(prev, meta):
    """Причины НЕ публиковать сборку. Пустой список — публикуем.

    Сравниваем с предыдущей опубликованной сборкой, а не с абсолютными
    числами: пороги «не меньше 500 000 т» пришлось бы править руками каждый
    раз, когда данные законно меняются, и однажды их бы просто отключили."""
    if not PUBLISH_GUARD or not prev:
        return []
    holds = []

    def drop(name, key, unit="", frac=0):
        a, b = prev.get(key), meta.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or a <= 0:
            return
        if b < a * (1.0 - PUBLISH_MAX_DROP):
            num = lambda v: f"{v:,.{frac}f}".replace(",", " ")
            holds.append("%s: было %s%s, стало %s%s — падение %.1f %%"
                         % (name, num(a), unit, num(b), unit, (a - b) / a * 100))

    drop("продано", "total_tonnes", " т", frac=3)
    drop("строк движений", "rows_total")
    drop("позиций", "positions")

    # Начало периода уехало вперёд — значит потеряна ИСТОРИЯ. Это самый
    # надёжный признак обрезанного источника: тоннаж может законно упасть
    # после чистки в 1С, а вот дата первого движения — нет.
    a, b = prev.get("period_min"), meta.get("period_min")
    if a and b and str(b) > str(a):
        holds.append("начало периода сдвинулось вперёд: было %s, стало %s — "
                     "источник отдал обрезанную историю" % (a, b))

    # Контрольные примеры: просели против предыдущей сборки.
    pa, pb = prev.get("examples_passed"), meta.get("examples_passed")
    if isinstance(pa, int) and isinstance(pb, int) and pb < pa:
        holds.append("контрольные примеры: было пройдено %d, стало %d" % (pa, pb))
    return holds


KEEP_EXCEL = int(os.environ.get("METOPTORG_KEEP_EXCEL", "3"))


def _rotate_excel(keep=KEEP_EXCEL):
    """В out/ живут только последние Excel: остальные есть в своих сборках."""
    xls = sorted(f for f in os.listdir(OUT) if f.endswith(".xlsx"))
    for f in xls[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(OUT, f))
        except OSError:
            pass


MIN_FREE_MB = int(os.environ.get("METOPTORG_MIN_FREE_MB", "1500"))


def _free_mb(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize // (1024 * 1024)


def _snapshot(build_id, ok, logfile):
    """Снимок готовой сборки в builds/<id>/ + переключение builds/current."""
    d = os.path.join(BUILDS, build_id)
    os.makedirs(d, exist_ok=True)
    shutil.copy2(logfile, os.path.join(d, "build.log"))
    meta = {"finished": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "ok": ok}
    # Чем была предыдущая ОПУБЛИКОВАННАЯ сборка — читаем до переключения ссылки.
    prev = {}
    try:
        prev = json.load(open(os.path.join(BUILDS, "current", "build.json"),
                              encoding="utf-8"))
    except Exception:
        pass
    if ok:
        # ⚠️ EXCEL — ТОЛЬКО СВОЙ, ОДИН. make_excel.py каждый день кладёт в out/
        # новый «Реализация_metoptorg_<дата>.xlsx» (34 МБ), старые никто не
        # убирал, а снимок копировал ВСЕ подряд: к 14.09.2026 каждая сборка
        # весила 1,2 ГБ (34 одинаковых Excel), builds/ — 12 ГБ, и диск сервера
        # кончился ровно на копировании снимка. Сборка при этом отчиталась
        # «успешно» — упал только снимок, и в истории повисло «не удалась».
        xls = sorted(f for f in os.listdir(OUT) if f.endswith(".xlsx"))
        keep_xlsx = xls[-1:]                       # свежий по дате в имени
        for f in os.listdir(OUT):
            src = os.path.join(OUT, f)
            if not os.path.isfile(src):
                continue
            if f.endswith(".html") or f == "sales_data.json" or f in keep_xlsx:
                shutil.copy2(src, os.path.join(d, f))
        _rotate_excel()
        try:
            sd = json.load(open(os.path.join(OUT, "sales_data.json"), encoding="utf-8"))
            m = sd["meta"]
            meta["total_tonnes"] = m.get("total_tonnes")
            meta["checks"] = "; ".join(
                f"{a['name'].split(':')[0]}: {a['value']}" for a in m.get("autochecks", [])
                if a.get("status") != "ok")[:400]
            meta["autochecks"] = m.get("autochecks", [])
            meta["period"] = f"{m.get('period_min','')} … {m.get('period_max','')}"
            # Поля для сверки со следующей сборкой (см. _publish_hold).
            for k in ("rows_total", "positions", "period_min", "period_max"):
                meta[k] = m.get(k)
        except Exception:
            pass
        try:
            p, t = _passed_examples(open(logfile, encoding="utf-8", errors="replace").read())
            meta["examples_passed"], meta["examples_total"] = p, t
        except Exception:
            pass

        holds = _publish_hold(prev, meta)
        meta["published"] = not holds
        meta["hold"] = holds
        if holds:
            # Снимок остаётся в builds/<id>/, но НЕ становится текущим:
            # заказчик продолжает видеть прежний верный отчёт.
            with open(os.path.join(d, "build.log"), "a", encoding="utf-8") as lg:
                lg.write("\n" + "=" * 70 + "\n")
                lg.write("СБОРКА НЕ ОПУБЛИКОВАНА: она слишком не похожа на предыдущую\n")
                lg.write("=" * 70 + "\n")
                for h in holds:
                    lg.write("  • " + h + "\n")
                lg.write("\nЧаще всего это значит, что ИСТОЧНИК отдал неполные данные,\n"
                         "а не что сломался расчёт: проверьте таблицы в «Extractor»\n"
                         "(tools/sql_columns.py). Отчёт на сайте остался прежним —\n"
                         "сборка %s.\nКогда обвал объяснён, сборку можно "
                         "опубликовать вручную:\n  ln -sfn %s builds/current\n"
                         % (prev.get("finished", "предыдущая"), d))
        else:
            cur = os.path.join(BUILDS, "current")
            tmp = cur + ".new"
            if os.path.islink(tmp) or os.path.exists(tmp):
                os.remove(tmp)
            os.symlink(d, tmp)
            os.replace(tmp, cur)
    json.dump(meta, open(os.path.join(d, "build.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    return meta


KEEP_BUILDS = int(os.environ.get("METOPTORG_KEEP_BUILDS", "10"))


def _prune_builds(keep=KEEP_BUILDS):
    """Каждая сборка весит ~180 МБ (дашборд + свой Excel + снимок), поэтому
    старые чистим — иначе диск кончится незаметно (кончился 14.09.2026: диск
    35 ГБ на шестерых соседей, наши builds/ выросли до 12 ГБ). Десять сборок
    — полторы недели истории. Текущую не трогаем никогда."""
    cur = os.path.realpath(os.path.join(BUILDS, "current"))
    dirs = sorted((d for d in os.listdir(BUILDS)
                   if os.path.isdir(os.path.join(BUILDS, d)) and d != "current"),
                  reverse=True)
    for d in dirs[keep:]:
        p = os.path.join(BUILDS, d)
        if os.path.realpath(p) == cur:
            continue
        shutil.rmtree(p, ignore_errors=True)


def build_worker():
    build_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = os.path.join(LOGS, f"build-{build_id}.log")
    write_state(state="идёт", step="старт", started=datetime.datetime.now()
                .strftime("%Y-%m-%d %H:%M:%S"), finished="", ok=None, hold=[],
                log=os.path.basename(logfile), build=build_id)
    try:
        # ⚠️ МЕСТО ПРОВЕРЯЕМ ДО СТАРТА. Сборке нужно ~1 ГБ на выгрузку, снимок
        # и Excel; когда 14.09.2026 диск кончился, 25 минут расчёта прошли
        # впустую, а состояние зависло в «идёт» — снимок упал с исключением,
        # которое никто не ловил. Лучше сразу честно сказать, чего не хватает.
        free = _free_mb(ROOT)
        if free < MIN_FREE_MB:
            with open(logfile, "w", encoding="utf-8") as lg:
                lg.write("Пересборка не начата: на диске сервера свободно %d МБ, "
                         "нужно не меньше %d МБ.\nОсвободите место (старые сборки "
                         "в builds/, архив в data/archive/) и запустите снова.\n"
                         % (free, MIN_FREE_MB))
            write_state(state="ошибка", step="", ok=False,
                        finished=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        hold=["мало места на диске: свободно %d МБ" % free])
            return
        ok = _run_build(logfile)
        try:
            meta = _snapshot(build_id, ok, logfile)
        except Exception as e:           # диск, права — что угодно, но не «идёт»
            with open(logfile, "a", encoding="utf-8") as lg:
                lg.write("\n!! снимок сборки не записан: %s\n" % e)
            shutil.rmtree(os.path.join(BUILDS, build_id), ignore_errors=True)
            write_state(state="ошибка", step="", ok=False,
                        finished=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        hold=["снимок сборки не записан: %s" % e])
            return
        _prune_builds()
        holds = meta.get("hold") or []
        write_state(state=("не опубликовано" if holds else "готово") if ok else "ошибка",
                    step="", ok=ok,
                    finished=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    total=meta.get("total_tonnes"), hold=holds)
    finally:
        try: os.remove(LOCK)
        except OSError: pass


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/")
def index(request: Request, user: str = Depends(auth)):
    """Корень — сразу отчёт (заказчик 14.09.2026): экономист открывает адрес и
    видит дашборд, а не страницу пересборок. Управление переехало на /admin."""
    return _serve("dashboard.html", request=request)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(user: str = Depends(auth_write)):
    """Страница управления: пересборка, источники, история сборок — только
    администраторам (см. ADMINS). Браузер спросит пароль один раз."""
    page = os.path.join(ROOT, "web", "admin.html")
    return HTMLResponse(open(page, encoding="utf-8").read())


@app.get("/admin/rules", response_class=HTMLResponse)
def admin_rules(user: str = Depends(auth_write)):
    """Алгоритмы расчётов и действующие правила — вкладка страницы управления
    (заказчик 16.09.2026). Фрагмент web/rules.html, admin.html вставляет его в
    свою разметку."""
    page = os.path.join(ROOT, "web", "rules.html")
    return HTMLResponse(open(page, encoding="utf-8").read())


@app.get("/api/state")
def api_state(user: str = Depends(auth),
              cred: HTTPBasicCredentials = Depends(security)):
    st = read_state()
    st["files"] = data_files()
    st["builds"] = builds_list()
    st["portal"] = PORTAL_URL
    st["protected"] = bool(PASSWORD) and not OPEN_READ
    # запись под паролем, а вход в неё ещё не выполнен -> страница покажет
    # кнопку «Войти», а не непонятную ошибку при нажатии «Пересобрать»
    st["write_protected"] = bool(PASSWORD)
    st["can_write"] = (not PASSWORD) or _password_ok(cred)
    st["open_read"] = OPEN_READ
    cur = os.path.join(BUILDS, "current")
    st["has_dashboard"] = os.path.exists(os.path.join(current_dir(), "dashboard.html"))
    if os.path.islink(cur):
        try:
            st["current"] = json.load(open(os.path.join(cur, "build.json"), encoding="utf-8"))
        except Exception:
            st["current"] = {}
    return JSONResponse(st)


@app.get("/api/log", response_class=PlainTextResponse)
def api_log(tail: int = 400, user: str = Depends(auth)):
    st = read_state()
    p = os.path.join(LOGS, st.get("log") or "")
    if not st.get("log") or not os.path.exists(p):
        return PlainTextResponse("Лог пока пуст — сборка ещё не запускалась.")
    lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    return PlainTextResponse("\n".join(lines[-tail:]))


@app.post("/api/upload")
async def api_upload(request: Request, name: str, user: str = Depends(auth_write)):
    """Приём файла ПОТОКОМ: тело запроса — сам файл, имя в параметре.
    Так 300-мегабайтная выгрузка не оседает в памяти и виден прогресс."""
    name = os.path.basename(name or "").strip()
    if not name or "/" in name or "\\" in name:
        raise HTTPException(400, "Недопустимое имя файла")
    role = next((t for _k, pat, t in ACCEPT if pat.match(name)), None)
    if not role:
        raise HTTPException(400,
            "Имя не подходит ни под один шаблон входных файлов. "
            "Конвейер ищет файлы по именам — переименуйте выгрузку.")
    dst = os.path.join(DATA, name)
    tmp = dst + ".part"
    size = 0
    with open(tmp, "wb") as fh:
        async for chunk in request.stream():
            fh.write(chunk); size += len(chunk)
    if size == 0:
        os.remove(tmp)
        raise HTTPException(400, "Пустой файл")
    # прежний файл не удаляем — уводим в архив
    if os.path.exists(dst):
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(dst)).strftime("%Y%m%d-%H%M%S")
        shutil.move(dst, os.path.join(DATA, "archive", f"{stamp}__{name}"))
    os.replace(tmp, dst)
    return {"ok": True, "name": name, "role": role, "size": human(size)}


# ---------------------------------------------------------------------------
# Срок вывоза: ручные корректировки экономиста
# ---------------------------------------------------------------------------
# «ДатаВывоза» в 1С заполнена меньше чем у половины проектов и иногда устарела,
# поэтому срок правится прямо в диаграмме Ганта. Правки лежат отдельным файлом
# data/plan_dates.json и подхватываются пересборкой — исходные выгрузки 1С
# остаются нетронутыми, и правку всегда видно как правку.
PLAN_DATES = os.path.join(DATA, "plan_dates.json")
_plan_lock = threading.Lock()


def _read_plan_dates():
    if not os.path.exists(PLAN_DATES):
        return {}
    try:
        d = json.load(open(PLAN_DATES, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


@app.get("/api/plan-dates")
def api_plan_dates(user: str = Depends(auth)):
    return _read_plan_dates()


@app.post("/api/plan-dates")
async def api_plan_dates_set(request: Request, user: str = Depends(auth)):
    """Сохранить или снять срок вывоза по одному бизнес-плану.

    {series, date, note}          — поставить срок ('' = снять срок совсем)
    {series, reset: true}         — вернуть «ДатуВывоза» из 1С (убрать правку)"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ожидается JSON")
    skey = str(body.get("series") or "").strip()
    if not skey:
        raise HTTPException(400, "Не указан бизнес-план")
    date = str(body.get("date") or "").strip()[:10]
    if date and not re.fullmatch(r"20[1-3]\d-[01]\d-[0-3]\d", date):
        raise HTTPException(400, "Дата должна быть в формате ГГГГ-ММ-ДД, годы 2010–2039")
    with _plan_lock:
        cur = _read_plan_dates()
        if body.get("reset"):
            cur.pop(skey, None)
        else:
            cur[skey] = {
                "date": date,
                "name": str(body.get("name") or "")[:200],
                "note": str(body.get("note") or "")[:300],
                "by": user,
                "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        tmp = PLAN_DATES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, PLAN_DATES)
    return {"ok": True, "count": len(cur)}


# ---------------------------------------------------------------------------
# ВЫБОР ВЕРНОЙ ВЕРСИИ РАСЧЁТА БП (вкладка «Версии БП», 25.08.2026)
# ---------------------------------------------------------------------------
# У одного файла БП несколько листов-версий расчёта с разной выручкой; парсер
# отдаёт их все, а КАКАЯ верна — знает только экономист. Отметки хранятся здесь
# и подхватываются пересборкой: выбранная версия перекрывает правило «последняя
# по дате», отклонённая — исключается из кандидатов (load_bp_months).
# Ключ — полное имя версии из парсера («БП 361_23.08.2022 | v0 …»): оно же
# стоит в своде «Проверка месяцев», связь точная.
BP_VER_CHOICE = os.path.join(DATA, "bp_version_choice.json")
_bpver_lock = threading.Lock()


def _read_bpver():
    if not os.path.exists(BP_VER_CHOICE):
        return {}
    try:
        d = json.load(open(BP_VER_CHOICE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


@app.get("/api/bp-versions")
def api_bpver(user: str = Depends(auth)):
    return _read_bpver()


@app.post("/api/bp-versions")
async def api_bpver_set(request: Request, user: str = Depends(auth)):
    """Отметить версию расчёта БП.

    {name, bp, verdict: "ok", tracks: ["luk","dsp"]}
        — галочка: версия верна. ⚠️ ВЕРНАЯ ВЕРСИЯ — ОДНА НА ТРЕК (правка
          заказчика 01.09.2026: «одна у лук и одна у дсп»): прежняя галочка
          того же БП снимается, только если ПЕРЕСЕКАЕТСЯ по трекам. Версия
          лук+дсп занимает оба трека; без признаков — трек "other".
    {name, bp, verdict: "bad"}   — крестик: версия неверна, в расчёт не брать.
    {name, reset: true}          — снять отметку."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ожидается JSON")
    name = str(body.get("name") or "").strip()[:250]
    if not name:
        raise HTTPException(400, "Не указана версия")
    verdict = str(body.get("verdict") or "").strip()
    if not body.get("reset") and verdict not in ("ok", "bad"):
        raise HTTPException(400, "verdict должен быть ok или bad")
    bp = str(body.get("bp") or "").strip()[:10]
    tracks = [t for t in (body.get("tracks") or [])
              if t in ("luk", "dsp", "other")] or ["other"]
    with _bpver_lock:
        cur = _read_bpver()
        if body.get("reset"):
            cur.pop(name, None)
        else:
            if verdict == "ok" and bp:
                for k, v in list(cur.items()):
                    if (v.get("bp") == bp and v.get("verdict") == "ok"
                            and set(v.get("tracks") or ["other"]) & set(tracks)):
                        cur.pop(k)
            cur[name] = {
                "verdict": verdict, "bp": bp, "tracks": tracks,
                "by": user,
                "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        tmp = BP_VER_CHOICE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, BP_VER_CHOICE)
    return {"ok": True, "count": len(cur)}


# ---------------------------------------------------------------------------
# ИСТОЧНИКИ ИЗ MS SQL: настройка с вкладки «Обновление данных»
# ---------------------------------------------------------------------------
# ⚠️ ПАРОЛЬ ЧЕРЕЗ ЭТИ РУЧКИ НЕ ХОДИТ НИ В ОДНУ СТОРОНУ. Он читается только из
# окружения (METOPTORG_SQL_PASSWORD в /etc/metoptorg-realizaciya.env, права
# 600). Наружу отдаём лишь признак «задан / не задан».
# Менять настройки и дёргать базу может только тот, кто может пересобирать
# отчёт (auth_write): чтение открыто всем в сети, а это — нет.
def _sql_or_503():
    if sqlsrc is None:
        raise HTTPException(503, "модуль источников SQL не загрузился")
    return sqlsrc


@app.get("/api/sql/config")
def api_sql_config(user: str = Depends(auth)):
    m = _sql_or_503()
    cfg = m.load_config()
    ok, drv = m.driver_status()
    return {"config": cfg,
            "sources": [{"key": k, "label": lbl, "file": pat} for k, lbl, pat in m.SOURCES],
            "driver": drv, "driver_ok": ok,
            "password_set": bool(m.password()),
            "password_env": m.PASSWORD_ENV}


@app.post("/api/sql/config")
async def api_sql_config_set(request: Request, user: str = Depends(auth_write)):
    m = _sql_or_503()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "ожидался объект")
    # ⚠️ Называем ПРОВИНИВШИЙСЯ вход, а не просто «недопустимое имя». Форма
    # шлёт все семь строк разом, и одно плохое имя роняет сохранение целиком —
    # без имени входа человек не понимает, какое поле чинить.
    bad = []
    for key, s in (body.get("sources") or {}).items():
        t = str((s or {}).get("table") or "").strip()
        if t and not m.SAFE_NAME.match(t):
            bad.append("%s: «%s»" % (m.SOURCE_LABEL.get(key, key), t[:60]))
    if bad:
        raise HTTPException(400, "недопустимое имя таблицы — " + "; ".join(bad))
    return {"ok": True, "config": m.save_config(body)}


@app.post("/api/sql/test")
def api_sql_test(user: str = Depends(auth_write)):
    m = _sql_or_503()
    ok, msg = m.test_connection()
    return {"ok": ok, "message": msg}


@app.get("/api/sql/tables")
def api_sql_tables(user: str = Depends(auth_write)):
    m = _sql_or_503()
    try:
        return {"ok": True, "tables": m.list_tables()}
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}


@app.post("/api/sql/probe")
async def api_sql_probe(request: Request, user: str = Depends(auth_write)):
    m = _sql_or_503()
    table = str((await request.json()).get("table") or "").strip()
    if not m.SAFE_NAME.match(table):
        raise HTTPException(400, "недопустимое имя таблицы")
    try:
        return dict({"ok": True}, **m.probe(table))
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def recover_stale_lock():
    """Снять замок от сборки, которую убили вместе со службой.

    ⚠️ ПОЧЕМУ ЭТО ОБЯЗАТЕЛЬНО. Шаги конвейера — дочерние процессы uvicorn и
    живут в одном с ним cgroup. `systemctl restart` (то есть любая выкладка)
    сносит их вместе со службой: шаг падает с кодом −15, `_run_build` до конца
    не доходит, и `build.lock` остаётся навсегда. После этого `/api/rebuild`
    отвечает 409, кнопка «Пересобрать отчёт» больше НИКОГДА не нажимается, а в
    состоянии вечно висит «идёт». Ровно так и случилось 12.08.2026: сборка
    15:45:32, рестарт 15:46:55, замок с мёртвым pid 60481.
    Чинится только тут, при старте: сам по себе замок не рассосётся."""
    if not os.path.exists(LOCK):
        return
    try:
        pid = open(LOCK).read().strip()
    except Exception:
        pid = ""
    if pid and _pid_alive(pid):
        return                      # сборка и правда идёт — не трогаем
    try:
        os.remove(LOCK)
    except FileNotFoundError:
        pass
    st = read_state()
    if st.get("state") == "идёт":
        write_state(state="прервана", ok=False,
                    finished=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    step="прервана перезапуском службы на шаге «%s»"
                         % (st.get("step") or "—"))
    print("Снят замок от прерванной сборки (pid %s не жив)" % (pid or "?"))


recover_stale_lock()


@app.post("/api/rebuild")
def api_rebuild(user: str = Depends(auth_write)):
    with _lock:
        recover_stale_lock()        # вдруг замок протух уже после старта
        if os.path.exists(LOCK):
            raise HTTPException(409, "Пересборка уже идёт")
        open(LOCK, "w").write(str(os.getpid()))
    threading.Thread(target=build_worker, daemon=True).start()
    return {"ok": True}


def _serve(fname: str, download=False, request: Request = None):
    p = os.path.join(current_dir(), fname)
    if not os.path.exists(p):
        raise HTTPException(404, "Файл ещё не собран")
    # ⚠️ БЕЗ Cache-Control БРАУЗЕР ПОКАЗЫВАЕТ СТАРЫЙ ОТЧЁТ. Адрес /dashboard
    # постоянный, файл за ним — 61 МБ, и браузер охотно берёт его из кэша, не
    # спрашивая сервер: экономист жмёт «обновить», видит вчерашние цифры и
    # решает, что пересборка не прошла. Так было дважды.
    # «no-cache» — это не «не кэшировать», а «кэшируй, но КАЖДЫЙ РАЗ спрашивай,
    # не изменился ли». ETag и Last-Modified FileResponse уже отдаёт, поэтому
    # неизменившийся файл вернётся как 304 без повторной передачи 61 МБ.
    # ⚠️ FastAPI отдаёт файл ЦЕЛИКОМ даже на условный запрос: ни If-None-Match,
    # ни If-Modified-Since он не проверяет. С «no-cache» это значило бы 61 МБ на
    # каждое обновление страницы. Поэтому сверяем сами: метка = размер + время
    # файла, и если у браузера та же — отвечаем 304 без тела.
    st = os.stat(p)
    tag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
    hdr = {"Cache-Control": "no-cache, must-revalidate", "ETag": tag}
    if request is not None and request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=hdr)
    return FileResponse(p, filename=fname if download else None, headers=hdr)


@app.get("/dashboard")
def dashboard(request: Request, user: str = Depends(auth)):
    return _serve("dashboard.html", request=request)


@app.get("/excel")
def excel(user: str = Depends(auth)):
    d = current_dir()
    xl = [f for f in os.listdir(d) if f.endswith(".xlsx")]
    if not xl:
        raise HTTPException(404, "Excel ещё не собран")
    return _serve(sorted(xl)[-1], download=True)


@app.get("/snapshot")
def snapshot(request: Request, user: str = Depends(auth)):
    return _serve("sales_data.json", download=True, request=request)


@app.get("/builds/{build_id}/{fname}")
def build_file(build_id: str, fname: str, user: str = Depends(auth)):
    if not re.fullmatch(r"[0-9\-]{8,20}", build_id) or "/" in fname:
        raise HTTPException(400, "Некорректный запрос")
    p = os.path.join(BUILDS, build_id, os.path.basename(fname))
    if not os.path.exists(p):
        raise HTTPException(404, "Нет такого файла в этой сборке")
    return FileResponse(p, filename=fname)


@app.get("/login")
def login(user: str = Depends(auth_write)):
    """Единственное назначение — заставить браузер показать окно ввода пароля.
    Через fetch этого не добиться: 401 на XHR браузеры показывают по-разному,
    и пользователь видел бы просто ошибку. После успешного входа браузер сам
    подставляет пароль в последующие запросы к /api/upload и /api/rebuild."""
    return RedirectResponse("/admin", status_code=303)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    if not PASSWORD:
        print("!! METOPTORG_PASSWORD не задан — вход без пароля. "
              "Для сервера обязательно задайте пароль и поставьте HTTPS.")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=port)
