#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ночная пересборка отчёта по реализации — через API самой службы
(замок «уже идёт», снимок в builds/, лог), как и systemd-вариант.
Запускается планировщиком внутри контейнера realizaciya в 04:00.
Пароль берётся из окружения контейнера, в командную строку не попадает."""
import base64, os, sys, urllib.request

url = "http://127.0.0.1:8092/api/rebuild"
req = urllib.request.Request(url, method="POST", data=b"")
user, pw = os.environ.get("METOPTORG_USER", "metoptorg"), os.environ.get("METOPTORG_PASSWORD", "")
if pw:
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        print("rebuild:", r.status, r.read(300).decode(errors="replace"))
except urllib.error.HTTPError as e:
    print("rebuild:", e.code, e.read(300).decode(errors="replace"))
    sys.exit(0 if e.code == 409 else 1)   # 409 = уже идёт, это не ошибка
