#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py — сервис остатков: раздача дашборда + страница «Обновление данных».
Только stdlib (+ src/sqlsrc.py, которому нужен python-tds для SQL). Без авторизации.
Порт 8090. Соседей не трогает: fines-service :8077, logistmot :8080, реализация :8092.

Два источника загрузки данных:
  1) SQL «Extractor» — движения (обороты) и справочники берутся напрямую из базы
     (страница «Обновление данных» → блок SQL). Расчёт про SQL не знает: таблица
     выгружается в тот же csv, что и раньше.
  2) Excel локально — «Остатки на складах ДД.ММ.ГГ.xlsx» загружается через браузер
     (дата отчёта = дата из имени файла − 1 день).
Кнопка «Пересобрать» тянет включённые SQL-источники и запускает конвейер
(stock_report.py → make_dash.py → make_excel.py) фоновым потоком под замком.
"""
import os, sys, json, re, time, threading, subprocess, datetime, http.server, socketserver
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
WEB = os.path.join(ROOT, "web")
PORT = 8090
PY = sys.executable

try:
    import sqlsrc
except Exception:
    sqlsrc = None

# файлы, которые можно загрузить локально (Excel-остатки — основной; csv — как фолбэк)
UPLOAD_PATTERNS = [
    ("stock",     re.compile(r"^Остат(ки|ок).*\.xlsx?$", re.I)),   # «Остатки на складах…» и «Остаток на начало…»
    ("movements", re.compile(r"^СебестоимостьТоваровОбороты_.*\.csv$", re.I)),
    ("nomen",     re.compile(r"^Номенклатура_.*\.csv$", re.I)),
    ("series",    re.compile(r"^СерииНоменклатуры_.*\.csv$", re.I)),
    ("sklady",    re.compile(r"^Склады_.*\.csv$", re.I)),
]

# ---- состояние пересборки ----
LOCK = threading.Lock()
STATE = {"building": False, "started": "", "finished": "", "ok": None, "log": []}

def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _log(msg):
    line = "[%s] %s" % (_now(), msg)
    STATE["log"].append(line)
    if len(STATE["log"]) > 4000:
        del STATE["log"][:len(STATE["log"]) - 4000]
    print(line, flush=True)

def rebuild():
    if not LOCK.acquire(blocking=False):
        return False, "пересборка уже идёт"
    try:
        STATE.update(building=True, started=_now(), finished="", ok=None, log=[])
        _log("=== старт пересборки ===")
        # 1) SQL Extractor — включённые источники
        if sqlsrc is not None:
            try:
                cfg = sqlsrc.load_config()
                if cfg.get("enabled"):
                    _log("SQL Extractor включён — тяну таблицы…")
                    done, errors = sqlsrc.pull_enabled(cfg, log=_log)
                    for e in errors:
                        _log("SQL ОШИБКА: " + e)
                    if not done and not errors:
                        _log("SQL: ни один источник не включён")
                else:
                    _log("SQL Extractor выключен — использую файлы из data/")
            except Exception as e:
                _log("SQL: не удалось (%s) — использую файлы" % e)
        # 2) конвейер
        for script in ("stock_report.py", "make_dash.py", "make_excel.py"):
            _log("→ %s" % script)
            p = subprocess.Popen([PY, os.path.join(ROOT, script)], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace")
            for ln in p.stdout:
                _log("   " + ln.rstrip())
            rc = p.wait()
            if rc != 0:
                raise RuntimeError("%s завершился с кодом %d" % (script, rc))
        STATE["ok"] = True
        _log("=== пересборка завершена успешно ===")
    except Exception as e:
        STATE["ok"] = False
        _log("!!! пересборка прервана: %s" % e)
    finally:
        STATE.update(building=False, finished=_now())
        LOCK.release()
    return True, "ok"

# ---- HTTP ----
class H(http.server.BaseHTTPRequestHandler):
    server_version = "metoptorg-ostatki"
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        if not os.path.exists(path):
            return self._send(503, "не рассчитано: запустите пересборку", "text/plain; charset=utf-8")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200); self.send_header("Content-Type", ctype)
        # против кэша браузера: после пересборки всегда отдаём свежую версию
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path); path = u.path
        if path in ("/", "/index.html", "/dashboard.html"):
            return self._file(os.path.join(OUT, "dashboard.html"), "text/html; charset=utf-8")
        if path in ("/update", "/admin", "/admin.html"):
            return self._file(os.path.join(WEB, "admin.html"), "text/html; charset=utf-8")
        if path == "/data.json":
            return self._file(os.path.join(OUT, "dashboard_data.json"), "application/json; charset=utf-8")
        if path == "/excel":
            xl = sorted([f for f in os.listdir(OUT) if f.endswith(".xlsx")]) if os.path.isdir(OUT) else []
            if not xl: return self._send(404, "нет Excel", "text/plain; charset=utf-8")
            return self._file(os.path.join(OUT, xl[-1]),
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if path == "/health":
            return self._send(200, "ok", "text/plain")
        if path == "/api/state":
            return self._send(200, {"building": STATE["building"], "started": STATE["started"],
                                    "finished": STATE["finished"], "ok": STATE["ok"],
                                    "tail": STATE["log"][-40:]})
        if path == "/api/log":
            return self._send(200, "\n".join(STATE["log"]), "text/plain; charset=utf-8")
        if path == "/api/sql/config":
            if sqlsrc is None: return self._send(200, {"available": False})
            cfg = sqlsrc.load_config(); ok, drv = sqlsrc.driver_status()
            return self._send(200, {"available": True, "config": cfg, "driver": drv,
                                    "sources": [{"key": k, "label": l} for k, l, _ in sqlsrc.SOURCES],
                                    "password_set": bool(sqlsrc.password())})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == "/api/upload":
            name = (self.headers.get("X-Filename") or (q.get("name") or [""])[0])
            name = os.path.basename(unquote(name).strip())
            if not name or not any(p.match(name) for _, p in UPLOAD_PATTERNS):
                return self._send(400, {"ok": False,
                    "error": "имя файла не распознано. Ожидается «Остатки на складах ДД.ММ.ГГ.xlsx» "
                             "или выгрузка 1С (СебестоимостьТоваровОбороты_*.csv, Номенклатура_*.csv и т.д.)"})
            os.makedirs(DATA, exist_ok=True)
            dst = os.path.join(DATA, name); tmp = dst + ".part"
            n = int(self.headers.get("Content-Length") or 0)
            written = 0
            with open(tmp, "wb") as f:
                while written < n:
                    chunk = self.rfile.read(min(1 << 20, n - written))
                    if not chunk: break
                    f.write(chunk); written += len(chunk)
            os.replace(tmp, dst)
            _log("загружен файл: %s (%.1f МБ)" % (name, written / 1048576.0))
            return self._send(200, {"ok": True, "name": name, "bytes": written})
        if path == "/api/rebuild":
            if STATE["building"]:
                return self._send(409, {"ok": False, "error": "пересборка уже идёт"})
            threading.Thread(target=rebuild, daemon=True).start()
            return self._send(200, {"ok": True, "started": True})
        if path == "/api/sql/config" and sqlsrc is not None:
            try:
                cfg = json.loads(self._body() or b"{}")
                saved = sqlsrc.save_config(cfg)
                return self._send(200, {"ok": True, "config": saved})
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
        if path == "/api/sql/test" and sqlsrc is not None:
            try:
                cfg = json.loads(self._body() or b"{}") or None
                ok, msg = sqlsrc.test_connection(cfg)
                return self._send(200, {"ok": ok, "message": msg})
            except Exception as e:
                return self._send(200, {"ok": False, "message": str(e)})
        if path == "/api/sql/tables" and sqlsrc is not None:
            try:
                cfg = json.loads(self._body() or b"{}") or None
                return self._send(200, {"ok": True, "tables": sqlsrc.list_tables(cfg)})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/sql/probe" and sqlsrc is not None:
            try:
                d = json.loads(self._body() or b"{}")
                return self._send(200, {"ok": True, "probe": sqlsrc.probe(d.get("table"), d.get("cfg"))})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        return self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == "__main__":
    with Server(("0.0.0.0", PORT), H) as httpd:
        print("metoptorg-ostatki на :%d" % PORT, flush=True)
        httpd.serve_forever()
