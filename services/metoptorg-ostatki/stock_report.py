#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_report.py — расчёт фактических остатков металлолома с привязкой к серии
и документу-партии 1С. Источник правды (готового отчёта в 1С нет).

Запуск:  python3 stock_report.py [--data DIR] [--out DIR]
Находит свежие файлы в DIR:
  СебестоимостьТоваровОбороты_*.csv, Остатки на складах*.xlsx,
  Номенклатура_*.csv, СерииНоменклатуры_*.csv, Склады_*.csv
Пишет out/dashboard_data.json + печатает сверку.
"""
import os, sys, glob, json, argparse, time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import pipeline as P

def newest(pattern, d):
    files = glob.glob(os.path.join(d, pattern))
    if not files:
        raise SystemExit("Не найден файл по шаблону: " + pattern + " в " + d)
    return max(files, key=os.path.getmtime)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "data"))
    ap.add_argument("--out",  default=os.path.join(os.path.dirname(__file__), "out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()

    f_ob   = newest("СебестоимостьТоваровОбороты_*.csv", args.data)
    f_fact = newest("Остат*.xlsx", args.data)   # «Остатки на складах…» и «Остаток на начало…»
    f_nom  = newest("Номенклатура_*.csv", args.data)
    f_ser  = newest("СерииНоменклатуры_*.csv", args.data)
    f_skl  = newest("Склады_*.csv", args.data)
    print("Обороты :", os.path.basename(f_ob))
    print("Факт    :", os.path.basename(f_fact))

    nom_by_name, nom_by_guid = P.load_nomenklatura(f_nom)
    series_ref = P.load_series_ref(f_ser)
    sklad2base, sklad2parent = P.load_sklady(f_skl)
    print("Справочники: номенклатура=%d, серии=%d, склады(->5)=%d"
          % (len(nom_by_name), len(series_ref), len(sklad2base)))

    fact_rows, actual = P.load_fact(f_fact)
    print("Факт строк: %d, дата актуальности: %s"
          % (len(fact_rows), actual.strftime("%d.%m.%Y") if actual else "—"))

    prihods, part_map, lot_dist, reg_dt, nrows, check_agg, series_repr = P.load_movements(f_ob, actual)
    mixes = sum(1 for d in lot_dist.values() if len(d) > 1)
    print("Движения: строк=%d, приходов=%d, карта(партия+код->серия)=%d, лотов переработки=%d (из них смесей=%d)"
          % (nrows, len(prihods), len(part_map), len(lot_dist), mixes))

    overrides = P.load_overrides(os.path.join(args.data, "overrides.json"))
    edits = []
    results = P.distribute(fact_rows, prihods, part_map, lot_dist, sklad2base,
                           nom_by_name, nom_by_guid, series_ref,
                           overrides=overrides, edits_log=edits, series_repr=series_repr,
                           recount_date=actual.strftime("%d.%m.%Y") if actual else None)
    if overrides:
        print("Ручные привязки (оверрайды): задано=%d, применено к %d док-покрытиям"
              % (len(overrides), len(edits)))

    # ---- сверка ----
    tot_fact_qty_t = sum(P.qty_to_tonnes(fr["qty"], fr["unit"], nom_by_name.get(fr["name"]))
                         for fr in fact_rows)
    tot_res_t = sum(r["tonnes"] for r in results)
    by_badge = defaultdict(float)
    for r in results: by_badge[r["badge"]] += r["tonnes"]
    real_t = sum(r["tonnes"] for r in results if r["series"] not in ("БЕЗ СЕРИИ",)
                 and not str(r["series"]).startswith("площадка:"))
    print("\n=== СВЕРКА (тонны) ===")
    print("Факт (сумма по номенклатуре)  : %.3f" % tot_fact_qty_t)
    print("Распределено (сумма позиций)  : %.3f" % tot_res_t)
    print("  с реальной серией           : %.3f (%.1f%%)"
          % (real_t, 100*real_t/tot_res_t if tot_res_t else 0))
    print("По меткам происхождения:")
    for b in sorted(by_badge, key=lambda x: -by_badge[x]):
        print("   %-12s %.3f" % (b, by_badge[b]))

    payload = {
        "meta": {
            "actual_date": actual.strftime("%d.%m.%Y") if actual else "",
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_obor": os.path.basename(f_ob),
            "source_fact": os.path.basename(f_fact),
            "total_tonnes": round(tot_res_t, 3),
            "real_series_tonnes": round(real_t, 3),
        },
        "rows": results,
        "edits": edits,
        "check": [
            {"code": c, "name": v["name"],
             "prihod": round(v["prihod"], 3), "rashod": round(v["rashod"], 3),
             "ratio": round(v["rashod"]/v["prihod"], 3) if v["prihod"] else None}
            for c, v in sorted(check_agg.items(), key=lambda kv: -kv[1]["prihod"])
            if v["prihod"] > 0
        ],
        "series_ref": series_ref,
    }
    outp = os.path.join(args.out, "dashboard_data.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("\nЗаписано: %s (%.1f МБ) за %.1f c"
          % (outp, os.path.getsize(outp)/1e6, time.time()-t0))

if __name__ == "__main__":
    main()
