#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка представлений одним процессом: дашборд + Excel + контрольные примеры.

Тот же результат, что «python3 make_dash.py && python3 make_excel.py», но одним
запуском — удобно, когда сборку нужно выполнить как единый процесс.
После сборки поднимает статический сервер на 8766, чтобы готовый дашборд можно
было сразу открыть в браузере.

    python3 tools/build_all.py
"""
import os, sys, runpy, functools, http.server, socketserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

def step(title, path):
    print("\n" + "=" * 70, flush=True)
    print(title, flush=True)
    print("=" * 70, flush=True)
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        print("код возврата: %s" % (e.code,), flush=True)
    except Exception as e:                      # пусть падение одного шага
        print("ОШИБКА: %r" % (e,), flush=True)  # не рушит остальные
        import traceback; traceback.print_exc()

step("ДАШБОРД", "make_dash.py")
step("EXCEL", "make_excel.py")
step("КОНТРОЛЬНЫЕ ПРИМЕРЫ §12", "tools/control_check.py")
step("ВЫГРУЗКА В 1С", "tools/check_1c_xlsx.py")

print("\nФайлы в out/:", flush=True)
for n in sorted(os.listdir("out")):
    p = os.path.join("out", n)
    if os.path.isfile(p):
        print("   %-42s %7.1f МБ" % (n, os.path.getsize(p) / 1e6), flush=True)
print("\n=== СБОРКА ЗАВЕРШЕНА ===", flush=True)

PORT = 8766
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
    print("дашборд открыт: http://localhost:%d/out/dashboard.html" % PORT, flush=True)
    httpd.serve_forever()
