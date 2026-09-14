#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка CSS в web/app_template.html: комментарии и осиротевшие «*/».

Повод: правка стилей дважды ломала диаграмму молча. Комментарий закрывался
раньше времени, дальше шёл обычный текст и ещё одно «*/» — браузер съедал
СЛЕДУЮЩЕЕ ЗА НИМ ПРАВИЛО целиком и никакой ошибки не показывал. Так пропали
дорожка нашего плана и флаг срока 1С. Проверка дешёвая, ставим в чек-лист.
"""
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "web/app_template.html"
src = open(PATH, encoding="utf-8").read()
css = "\n".join(m.group(1) for m in re.finditer(r"<style>(.*?)</style>", src, re.S)) or src

bad = []
i, line, opened = 0, 1, None
while i < len(css):
    if css.startswith("/*", i):
        if opened is None:
            opened = line
        i += 2
        continue
    if css.startswith("*/", i):
        if opened is None:
            bad.append("строка %d: «*/» без открывающего «/*» — правило после "
                       "него браузер съест целиком" % line)
        opened = None
        i += 2
        continue
    if css[i] == "\n":
        line += 1
    i += 1
if opened is not None:
    bad.append("строка %d: «/*» так и не закрыт — весь хвост стилей выключен" % opened)

print("CSS %s: /* = %d, */ = %d" % (PATH, css.count("/*"), css.count("*/")))
if bad:
    for b in bad:
        print("  ✗ " + b)
    sys.exit("ИТОГ: CSS сломан")
print("ИТОГ: комментарии парные, осиротевших «*/» нет")
