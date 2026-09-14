#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Можно ли свести площадочные псевдо-серии («22ESP0397К БП 1001») с настоящими
сериями 1С («1001 (22ESP0397К от 08.06.22 г. (ЭПУ))»)?

Ключ сопоставления: номер договора + ведущий номер серии = номер БП из имени склада.
Скрипт считает, сколько псевдо-серий и тоннажа так закрывается.
"""
import os, sys, glob, json
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))
import pipeline as P, config as C

sref, S2P = P.load_series_ref(sorted(glob.glob(os.path.join(HERE, "data", "СерииНоменклатуры_*.csv")))[-1])

# (ключ договора, ведущий номер) -> проект;  ведущий номер -> проекты
by_pair, by_lead = {}, defaultdict(set)
for pk, rec in sref.items():
    for raw in [rec.get("name", "")] + list(rec.get("variants", [])):
        if not raw:
            continue
        lead = P.canon_key(raw)
        c = P.extract_contract(raw)
        if not lead:
            continue
        for part in lead.split("/"):
            by_lead[part].add(pk)
            if c:
                by_pair.setdefault((P.contract_key(c), part), pk)
print(f"пар (договор + номер БП) в справочнике серий: {len(by_pair)}")
print(f"ведущих номеров: {len(by_lead)}, из них однозначных: "
      f"{sum(1 for v in by_lead.values() if len(v) == 1)}")

D = json.load(open(os.path.join(HERE, "out", "sales_data.json"), encoding="utf-8"))
plat = [i for i in D["items"] if str(i["series"]).startswith("площадка:")]
tot = sum(i["tonnes"] for i in plat)
print(f"\nпозиций с псевдо-серией площадки: {len(plat)}, тоннаж {tot:,.3f}".replace(",", " "))

hit_pair = hit_lead = 0.0
n_pair = n_lead = n_no_bp = n_amb = 0
examples = []
seen = set()
for i in plat:
    contract, bps = P.platform_info_from_sklad(i["sklad"])
    if not bps:
        n_no_bp += 1
        continue
    ck = P.contract_key(contract)
    pair_hits = {by_pair.get((ck, b)) for b in bps}
    pair_hits.discard(None)
    lead_hits = set()
    for b in bps:
        s = by_lead.get(b) or set()
        if len(s) == 1:
            lead_hits |= s
    if len(pair_hits) == 1:
        n_pair += 1; hit_pair += i["tonnes"]
        pk = next(iter(pair_hits))
        if pk not in seen and len(examples) < 8:
            seen.add(pk)
            examples.append((i["sklad"][:46], contract, ",".join(bps),
                             sref[pk]["name"][:52], round(i["tonnes"], 3)))
    elif len(lead_hits) == 1:
        n_lead += 1; hit_lead += i["tonnes"]
    elif len(pair_hits) > 1 or len(lead_hits) > 1:
        n_amb += 1

print(f"  сводится по паре «договор + БП»      : {n_pair:>5} позиций, "
      f"{hit_pair:>12,.3f} т".replace(",", " "))
print(f"  сводится только по номеру БП (однозн.): {n_lead:>5} позиций, "
      f"{hit_lead:>12,.3f} т".replace(",", " "))
print(f"  в имени склада нет номера БП          : {n_no_bp:>5} позиций")
print(f"  неоднозначно (несколько проектов)     : {n_amb:>5} позиций")

print("\nпримеры сведения:")
for sklad, c, bp, proj, t in examples:
    print(f"  склад «{sklad}»")
    print(f"      договор {c}, БП {bp}  ->  серия «{proj}»   ({t} т)")
