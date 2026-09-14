#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разовые запросы к посчитанным данным — чтобы не гадать, а смотреть."""
import os, sys, json
from collections import defaultdict, Counter
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = json.load(open(os.path.join(HERE, "out", "sales_data.json"), encoding="utf-8"))
ITEMS, META, S = D["items"], D["meta"], D.get("S", [])
sstr = lambda i: S[i] if isinstance(i, int) and 0 <= i < len(S) else (i or "")
MF = META["move_flows"]; CI = {c: i for i, c in enumerate(META["move_cols"])}
MV = D["moves"]
def mv(r, c):
    v = r[CI[c]]
    return v if c in ("qty", "tonnes") else (MF[v] if c == "flow" else sstr(v))

print("=== 1) движения варианта «1578 … ДС35 V» построчно ===")
for r in sorted(MV, key=lambda r: sstr(r[CI["dt"]])):
    v = mv(r, "variant")
    if v.startswith("1578") and "ДС35" in v and v.rstrip().upper().endswith(" V"):
        print(f"  {mv(r,'dt')}  {mv(r,'flow'):<20} {mv(r,'qty'):>8.3f}  "
              f"лот {mv(r,'lot'):<14} код {mv(r,'code'):<14} "
              f"{mv(r,'from')[:30]:<30} -> {mv(r,'to')[:30]:<30} [{mv(r,'badge')}]")

print("\n=== 2) все движения лота РУМЛ-000146 / 00000005139 ===")
for r in sorted(MV, key=lambda r: sstr(r[CI["dt"]])):
    if mv(r, "lot") == "РУМЛ-000146" and mv(r, "code") == "00000005139":
        print(f"  {mv(r,'dt')}  {mv(r,'flow'):<20} {mv(r,'qty'):>8.3f}  "
              f"серия={mv(r,'series')[:20]:<20} вариант={mv(r,'variant')[:34]:<34} "
              f"{mv(r,'from')[:26]:<26} -> {mv(r,'to')[:26]:<26} [{mv(r,'badge')}]")

print("\n=== 3) продажи 1578 … ДС35 V с Осенцов (позиции) ===")
for it in ITEMS:
    v = it.get("series_variant", "")
    if v.startswith("1578") and "ДС35" in v and "сенц" in it["sklad"]:
        print(f"  {it['sklad'][:34]:<34} {it['code']:<14} {it['name'][:28]:<28} "
              f"{it['tonnes']:>8.3f} т  [{it['badge']}]  вариант={v[:40]}")
        for d in it["docs"]:
            print(f"        {d['dt']}  {d['regnum']:<14} лот {d.get('lot',''):<14} "
                  f"{d['tonnes']:>8.3f} т  [{d['badge']}]")

print("\n=== 4) код 00000005539 в позициях и движениях ===")
print("  позиций:", sum(1 for it in ITEMS if it["code"] == "00000005539"))
cnt = Counter()
for r in MV:
    if mv(r, "code") == "00000005539":
        cnt[mv(r, "flow")] += mv(r, "qty")
print("  движения по потокам (ед.):", {k: round(v, 3) for k, v in cnt.items()})
print("  имя:", D.get("nm", {}).get("00000005539", "— нет в справочнике движений —"))
# похожие коды со «станцией управления»
sim = [(c, n) for c, n in D.get("nm", {}).items() if "ШГС" in n or "танция управления" in n]
print("  похожие коды:", sim[:8])
