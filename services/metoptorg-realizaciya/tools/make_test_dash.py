#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уменьшенная копия дашборда для проверки интерфейса (не для заказчика).

Полный дашборд весит ~41 МБ — это нормально для локального файла, но неудобно
щёлкать в автотесте. Здесь берётся срез данных с сохранением структуры, чтобы
прогнать все вкладки и убедиться, что ни одна не падает.
    python3 tools/make_test_dash.py   ->  out/dashboard_test.html

⚠️ НА СТЕНДЕ ВКЛАДКА «ПЛАН-ФАКТ (ГАНТА)» ПОЧТИ ПУСТАЯ — ЭТО НОРМА, А НЕ ПОЛОМКА.
Движения здесь урезаны до KEEP_MOVES, а фильтр «только с поступлением» (включён
по умолчанию) определяет завоз именно по строкам движений — в срезе их почти ни
у кого нет. На полном дашборде та же вкладка показывает 909 планов из 999.
Стенд годится, чтобы поймать падение вкладки; проверять НАПОЛНЕНИЕ Ганты надо на
полном `out/dashboard.html` через javascript_exec.
"""
import os, json, random
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = json.load(open(os.path.join(HERE, "out", "sales_data.json"), encoding="utf-8"))

KEEP_ITEMS, KEEP_MOVES = 400, 8000
# берём позиции, где есть и продажи, и распознанная серия, плюс проблемные
items = sorted(D["items"], key=lambda i: -i["tonnes"])[:KEEP_ITEMS]
keep_ser = {i["series"] for i in items}
keep_var = {i["series_variant"] for i in items}
S = D["S"]
vi = {S[i]: i for i in range(len(S))}
CI = {c: i for i, c in enumerate(D["meta"]["move_cols"])}
moves = [r for r in D["moves"]
         if S[r[CI["series"]]] in keep_ser or S[r[CI["variant"]]] in keep_var][:KEEP_MOVES]
# ⚠️ ДОКУМЕНТ РЕЖЕМ ТОЛЬКО ЦЕЛИКОМ. Интерфейс сверяет пару «ушло в производство →
# вышло из производства» внутри документа (ключ «номер + дата»), и половина документа в срезе
# давала бы на стенде выдуманные расхождения «−100 %, не вернулось».
PAIR = {"переработка_забрали", "переработка_вернули", "уехало", "приехало"}
FL = D["meta"]["move_flows"]
pair_keys = {(r[CI["doc"]], r[CI["dt"]]) for r in moves if FL[r[CI["flow"]]] in PAIR}
have = {id(r) for r in moves}
moves += [r for r in D["moves"]
          if FL[r[CI["flow"]]] in PAIR and id(r) not in have
          and (r[CI["doc"]], r[CI["dt"]]) in pair_keys]
D2 = dict(D)
D2["items"] = items
D2["moves"] = moves
D2["checks"] = D["checks"][:400]
D2["flows"] = [f for f in D["flows"] if f["series"] in keep_ser][:400]
D2["buys"] = [b for b in D["buys"] if b["series"] in keep_ser][:2000]
D2["flows_var"] = [f for f in D["flows_var"] if f["series"] in keep_ser][:2000]
D2["meta"] = dict(D["meta"], move_rows=len(moves))

tpl = open(os.path.join(HERE, "web", "app_template.html"), encoding="utf-8").read()
tabs = open(os.path.join(HERE, "web", "tabs.js"), encoding="utf-8").read()
a = tpl.index("/*__DATA__*/"); b = tpl.index("/*__END__*/") + len("/*__END__*/")
html = tpl[:a] + json.dumps(D2, ensure_ascii=False, separators=(",", ":")) + tpl[b:]
html = html.replace("<!--__TABS_JS__-->", "<script>\n" + tabs + "\n</script>")
out = os.path.join(HERE, "out", "dashboard_test.html")
open(out, "w", encoding="utf-8").write(html)
print("Готово: %s (%.1f МБ), позиций %d, движений %d"
      % (out, os.path.getsize(out) / 1e6, len(items), len(moves)))
