#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор двух вопросов заказчика:
   1) сколько тоннажа сидит в «— серия восстановлена по лоту —» и можно ли
      подставить настоящую строку серии из справочника;
   2) откуда у площадки заготовки приписка «База Когалым».
"""
import os, sys, json, glob
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))
import pipeline as P, config as C

D = json.load(open(os.path.join(HERE, "out", "sales_data.json"), encoding="utf-8"))
ITEMS, META = D["items"], D["meta"]
PH = "— серия восстановлена по лоту —"

print("=" * 76)
print("1) ПОЗИЦИИ БЕЗ СТРОКИ СЕРИИ")
print("=" * 76)
ph = [i for i in ITEMS if i["series_variant"] == PH]
real_ph = [i for i in ph if i["series"] != "БЕЗ СЕРИИ" and not str(i["series"]).startswith("площадка:")]
print(f"позиций с заглушкой: {len(ph)}, тоннаж {sum(i['tonnes'] for i in ph):,.3f}".replace(",", " "))
print(f"  из них с РЕАЛЬНЫМ проектом: {len(real_ph)}, "
      f"тоннаж {sum(i['tonnes'] for i in real_ph):,.3f}".replace(",", " "))

# у скольких проектов есть другой (нормальный) вариант в тех же данных
vars_by_proj = defaultdict(set)
for i in ITEMS:
    if i["series_variant"] != PH:
        vars_by_proj[i["series"]].add(i["series_variant"])
has_other = [i for i in real_ph if vars_by_proj.get(i["series"])]
one_other = [i for i in real_ph if len(vars_by_proj.get(i["series"], ())) == 1]
print(f"  у проекта есть другой вариант в продажах: {len(has_other)} позиций, "
      f"{sum(i['tonnes'] for i in has_other):,.3f} т".replace(",", " "))
print(f"  причём РОВНО ОДИН вариант: {len(one_other)} позиций, "
      f"{sum(i['tonnes'] for i in one_other):,.3f} т".replace(",", " "))
for i in sorted(real_ph, key=lambda x: -x["tonnes"])[:6]:
    other = vars_by_proj.get(i["series"], set())
    print(f"    {i['tonnes']:>10.3f} т  {i['series_full'][:44]:<44} "
          f"склад {i['sklad'][:26]:<26} другие варианты: {len(other)}")
    for v in list(other)[:2]:
        print(f"                 -> {v[:70]}")

# что говорит справочник серий
sref, S2P = P.load_series_ref(sorted(glob.glob(os.path.join(HERE, "data", "СерииНоменклатуры_*.csv")))[-1])
known = sum(1 for i in real_ph if i["series"] in sref)
print(f"  проект есть в справочнике серий: {known} из {len(real_ph)} позиций")

print()
print("=" * 76)
print("2) «ЗАГОТОВКА · БАЗА КОГАЛЫМ» — откуда база")
print("=" * 76)
div = P.load_divisions(sorted(glob.glob(os.path.join(HERE, "data", "*Производственные_подразделения*.csv")))[-1])
s2b, s2p2 = P.load_sklady(sorted(glob.glob(os.path.join(HERE, "data", "Склады_*.csv")))[-1])
CAN = P.base_canon_set(div)
for sk in ("Когалым КНПО", "Урай НПО Сервис", "Сандибинское м/р", "Пуровская группа месторождений"):
    it = next((i for i in ITEMS if i["sklad"] == sk), None)
    print(f"\n  склад: {sk}")
    print(f"    справочник складов -> база «5.»: {s2b.get(sk, '—')} ; родитель: {s2p2.get(sk, '—')}")
    if it:
        print(f"    в отчёте: тип «{it['site']}», база «{it['base']}», "
              f"аналитическое «{it['analytical']}»")
    print(f"    подразделение склада есть в справочнике подразделений: "
          f"{'да' if sk in div else 'нет'}")
    print(f"    sk_site: {D.get('sk_site', {}).get(sk, '—')}")
# что вообще в справочнике про Когалым
print("\n  записи справочника подразделений со словом «Когалым»:")
for n, d in div.items():
    if "огалым" in n:
        print(f"    {n[:44]:<44} kind={d['kind']:<10} base={d['base']:<16} "
              f"analytical={d['analytical']}")

print()
print("=" * 76)
print("3) ПУСТОЙ ТИП ПОДРАЗДЕЛЕНИЯ («· —»)")
print("=" * 76)
sk_site = D.get("sk_site", {})
empty = [w for w, s in sk_site.items() if not s or s == "—"]
print(f"  складов без типа в sk_site: {len(empty)}; примеры: {empty[:5]}")
noname = [w for w in sk_site if not w]
print(f"  пустое имя склада в движениях: {'да' if noname else 'нет'}")
cnt = Counter(i["site"] for i in ITEMS)
print(f"  типы у позиций: {dict(cnt)}")
