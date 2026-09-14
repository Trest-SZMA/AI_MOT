#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""portal.py — главная страница «Рабочие сервисы МетОптТорг».

Что делает:
  * отдаёт web/index.html под HTTP Basic-авторизацией;
  * отдаёт /api/status — какие сервисы сейчас отвечают (проверка TCP-коннектом);
  * читает web/services.json НА КАЖДЫЙ ЗАПРОС — добавить сервис можно без
    перезапуска портала.

Чего НЕ делает и делать не должен:
  * не хранит пароли сервисов и не входит за пользователя. Плитка — обычная
    ссылка, сервис сам спросит свой логин и пароль. Поэтому любой сервис можно
    обновлять и перезапускать независимо: портал о нём знает только порт.
  * не проксирует трафик сервисов. Падение портала не роняет ни один сервис.

Только стандартная библиотека — как у соседних служб на этой машине.
"""
import base64
import hmac
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")

HOST = os.environ.get("PORTAL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORTAL_PORT", "8079"))
USER = os.environ.get("PORTAL_USER", "")
PASSWORD = os.environ.get("PORTAL_PASSWORD", "")
REALM = "MetOptTorg"

# Портал будет смотреть в интернет — пускать его без пароля нельзя даже разово.
if not USER or not PASSWORD:
    sys.exit("ОШИБКА: не заданы PORTAL_USER и PORTAL_PASSWORD — "
             "портал без пароля не запускается")

EXPECTED = "Basic " + base64.b64encode(
    ("%s:%s" % (USER, PASSWORD)).encode("utf-8")).decode("ascii")


def load_services():
    """Реестр читается заново на каждый запрос: правка services.json
    подхватывается сразу, перезапуск службы не нужен."""
    try:
        with open(os.path.join(WEB, "services.json"), encoding="utf-8") as f:
            data = json.load(f)
        return [s for s in data.get("services", []) if not s.get("hidden")]
    except Exception as e:
        print("services.json не прочитан: %s" % e, flush=True)
        return []


# --- проверка живости: результат кешируем, чтобы не долбить сервисы на каждый F5 ---
_cache = {"at": 0.0, "data": []}
_lock = threading.Lock()
TTL = 15.0


def probe(port, timeout=1.2):
    """Жив ли сервис. Только TCP-коннект: HTTP-запрос потребовал бы авторизации,
    а пароли сервисов порталу не принадлежат и знать он их не должен."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def status():
    with _lock:
        if time.time() - _cache["at"] < TTL:
            return _cache["data"]
    services = load_services()
    out = []
    threads = []
    result = {}

    def check(s):
        result[s["id"]] = probe(s.get("port"))

    for s in services:
        t = threading.Thread(target=check, args=(s,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=2.0)
    for s in services:
        out.append({"id": s["id"], "up": bool(result.get(s["id"], False))})
    with _lock:
        _cache["at"] = time.time()
        _cache["data"] = out
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "MetOptTorgPortal"
    sys_version = ""

    def log_message(self, fmt, *args):
        # тише журнал: без строки на каждую картинку, но отказы видно
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            print("%s %s" % (self.address_string(), fmt % args), flush=True)

    # ---------- авторизация ----------
    def authorized(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, EXPECTED)

    def ask_password(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="%s", charset="UTF-8"' % REALM)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send(self, body, ctype, code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # портал не встраивают в чужие страницы
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        # мониторинг и curl -I ходят HEAD; без этого метода базовый класс
        # отвечает 501 и проверка живости портала выглядит как поломка
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):
        if not self.authorized():
            return self.ask_password()

        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/":
            try:
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self.send(f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self.send("index.html не найден", "text/plain; charset=utf-8", 500)

        if path == "/api/status":
            body = json.dumps({
                "user": USER,
                "services": status(),
                "generated_at": time.strftime("%d.%m.%Y %H:%M:%S"),
            }, ensure_ascii=False)
            return self.send(body, "application/json; charset=utf-8")

        if path == "/api/services":
            return self.send(json.dumps({"services": load_services()},
                                        ensure_ascii=False),
                             "application/json; charset=utf-8")

        if path == "/healthz":                      # для мониторинга, без данных
            return self.send("ok", "text/plain; charset=utf-8")

        self.send("Нет такой страницы", "text/plain; charset=utf-8", 404)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    n = len(load_services())
    print("Портал МетОптТорг: http://%s:%d  (сервисов в реестре: %d)"
          % (HOST, PORT, n), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
