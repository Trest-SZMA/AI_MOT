#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика смешанных лотов: хватает ли ёмкости компонент на расход.

Нужна, чтобы не выдумывать правило вслепую: сколько строк раскладывается по
точному количеству, сколько по ёмкости, а где ёмкость лота реально исчерпана.
    python3 tools/diag_mix.py
"""
import os, sys, glob
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))
import config as C
import pipeline as P

DATA = os.path.join(HERE, "data")
newest = lambda pat: sorted(glob.glob(os.path.join(DATA, pat)))[-1]

series_ref, S2P = P.load_series_ref(newest("СерииНоменклатуры_*.csv"))
divisions = P.load_divisions(newest("*Производственные_подразделения*.csv"))
M = P.scan_movements(newest("СебестоимостьТоваровОбороты_*.csv"),
                     divisions=divisions, s2p=S2P)

caps0 = {}
for key, mix in M["part_mix"].items():
    total = sum((M["part_comp_qty"].get(key) or {}).values())
    if total > 0:
        caps0[key] = {s: total * f for s, f in mix.items()}

out_rows = defaultdict(list)
for fr in M["flow_rows"]:
    if fr["flow"] in P.OUTFLOWS and fr["qty"] > 0 and (fr["partia_norm"], fr["code"]) in caps0:
        out_rows[(fr["partia_norm"], fr["code"])].append(fr)

stat = Counter()
worst = []
for key, rows in out_rows.items():
    cap = sum(caps0[key].values())
    inline = sum(r["qty"] for r in rows if r["ckey"])
    need = sum(r["qty"] for r in rows if not r["ckey"])
    free = cap - inline
    stat["лотов"] += 1
    stat["строк без своей серии"] += sum(1 for r in rows if not r["ckey"])
    if need > free + 0.0005:
        stat["лотов с исчерпанной ёмкостью"] += 1
        stat["не хватило, т"] += need - free
        worst.append((need - free, key, cap, inline, need, len(rows), len(caps0[key])))

print("Смешанных лотов:", len(caps0))
for k, v in stat.most_common():
    print(f"  {k}: {v:,.3f}".replace(",", " ") if isinstance(v, float) else f"  {k}: {v}")

worst.sort(reverse=True)
print("\nТОП-15 лотов, где расход не влезает в ёмкость компонент:")
for gap, key, cap, inline, need, nrows, ncomp in worst[:15]:
    print(f"  не хватает {gap:>12,.3f}  ёмкость {cap:>12,.3f}  своя серия {inline:>11,.3f}"
          f"  надо {need:>12,.3f}  строк {nrows:>5}  компонент {ncomp}"
          f"  {P.lot_display(key[0])} / {key[1]}".replace(",", " "))
    comp = M["part_comp_qty"].get(key) or {}
    for s, q in sorted(comp.items(), key=lambda kv: -kv[1])[:5]:
        print(f"        приход {q:>12,.3f}  серия {str(s)[:60]}".replace(",", " "))

# контрольный лот из ТЗ
print("\nКОНТРОЛЬ: лот Приобретение РУМЛ-000146 / 00000005139")
for key in caps0:
    if "РУМЛ-000146" in key[0] and key[1] == "00000005139":
        print("  ключ:", key)
        print("  состав прихода:", {str(s)[:40]: round(q, 3)
                                    for s, q in (M["part_comp_qty"].get(key) or {}).items()})
        print("  доли (part_mix):", {str(s)[:40]: round(v, 4) for s, v in M["part_mix"][key].items()})
        for fr in sorted(out_rows.get(key, []), key=lambda r: r["dt"]):
            got = M["mix_alloc"].get(fr["rid"])
            print(f"    {fr['dt']}  {fr['flow']:<20} {fr['qty']:>9.3f}  "
                  f"своя серия={str(fr['ckey'])[:28]:<28} -> "
                  + (", ".join(f"{str(s)[:24]}:{q:.3f}[{b}]" for s, q, b in got) if got else "—"))
