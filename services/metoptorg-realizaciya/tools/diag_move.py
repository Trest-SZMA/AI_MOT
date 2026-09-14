#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор расхождений «уехало ≠ приехало» — по документам, вариантам и БП.

Читает out/sales_data.json (или указанный первым аргументом) и отвечает на два
вопроса заказчика:

  1) у каких бизнес-планов «уехало» не сходится с «приехало» и ПОЧЕМУ —
     по каждому документу перемещения отдельно;
  2) принадлежит ли строка серии (вариант) тому же проекту, что и сама серия.

Пара сторон перемещения считается по ключу «документ + ДАТА + код + партия» —
тому же, что у move_var в sales_report.py. Номера документов 1С повторяются
из года в год, без даты склеятся чужие движения.

Каждая сторона меряется в СВОЕЙ единице (колонка mq/mc): тонны против штук —
это не расхождение, а смена номенклатуры.
"""
import json
import sys
from collections import defaultdict, Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else "out/sales_data.json"
EPS = 0.0005


def load(path):
    d = json.load(open(path, encoding="utf-8"))
    S = d["S"]
    cols = d["meta"]["move_cols"]
    flows = d["meta"]["move_flows"]
    ix = {c: i for i, c in enumerate(cols)}
    # ⚠️ `flow` — индекс в meta.move_flows, остальные строковые колонки — в S
    STR = {"dt", "doc", "code", "from", "to", "series",
           "variant", "badge", "lot", "sklad", "mc"}
    rows = []
    for r in d["moves"]:
        o = {}
        for c, i in ix.items():
            v = r[i] if i < len(r) else None
            if c == "flow":
                o[c] = flows[v] if isinstance(v, int) and v < len(flows) else v
            elif c in STR:
                o[c] = S[v] if isinstance(v, int) and v < len(S) else v
            else:
                o[c] = v
        rows.append(o)
    return d, rows


def main():
    d, rows = load(PATH)
    # ключ проекта в movesax — ПроектГуид; человеческое имя лежит в flows
    NAME = {f["series"]: f.get("series_full") or f["series"] for f in d["flows"]}
    nm = lambda k: NAME.get(k, k)
    mv = [r for r in rows if r["flow"] in ("уехало", "приехало")]
    print("Строк перемещения: %d (уехало %d, приехало %d)"
          % (len(mv),
             sum(1 for r in mv if r["flow"] == "уехало"),
             sum(1 for r in mv if r["flow"] == "приехало")))

    # ---------- 1. свод по проектам: уехало vs приехало ----------
    # По КАЖДОЙ единице отдельно: тонны со штуками не складываются.
    proj = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # серия -> ед -> [ушло, пришло]
    for r in mv:
        i = 0 if r["flow"] == "уехало" else 1
        proj[r["series"]][r["mc"] or "—"][i] += r["mq"] or 0.0

    gaps = []
    for ser, byu in proj.items():
        for unit, (u, a) in byu.items():
            if abs(u - a) > EPS:
                gaps.append((abs(u - a), ser, unit, u, a))
    gaps.sort(reverse=True)
    tot = sum(g[0] for g in gaps)
    tot_t = sum(g[0] for g in gaps if g[2] == "т")
    print("\n=== 1. ПРОЕКТЫ, ГДЕ УЕХАЛО ≠ ПРИЕХАЛО ===")
    print("расхождений %d (в тоннах %d на %.3f т; всего по всем единицам %.3f)"
          % (len(gaps), sum(1 for g in gaps if g[2] == "т"), tot_t, tot))
    print("%-58s %6s %14s %14s %12s" % ("проект", "ед", "уехало", "приехало", "разница"))
    for delta, ser, unit, u, a in gaps[:30]:
        print("%-58s %6s %14.3f %14.3f %12.3f" % (nm(ser)[:58], unit, u, a, u - a))
    if len(gaps) > 30:
        print("… ещё %d" % (len(gaps) - 30))

    # ---------- 2. разбор по документам ----------
    # ключ пары: документ + дата + код + партия (лот). Партия обязательна:
    # один документ двигает одну номенклатуру под несколькими сериями.
    pair = defaultdict(lambda: {"out": [], "in": []})
    for r in mv:
        k = (r["doc"], (r["dt"] or "")[:10], r["code"], r["lot"] or "")
        pair[k]["out" if r["flow"] == "уехало" else "in"].append(r)

    cause = Counter()
    cause_qty = defaultdict(float)          # только тонны
    detail = defaultdict(list)              # причина -> примеры
    ser_move = defaultdict(float)           # (из проекта -> в проект) -> тонны

    for k, sides in pair.items():
        out, inn = sides["out"], sides["in"]
        o_ser = {r["series"] for r in out}
        i_ser = {r["series"] for r in inn}
        o_t = sum(r["tonnes"] or 0.0 for r in out)
        i_t = sum(r["tonnes"] or 0.0 for r in inn)
        o_q = sum(r["qty"] or 0.0 for r in out)
        i_q = sum(r["qty"] or 0.0 for r in inn)

        if not inn:
            c = "нет приходной стороны в выгрузке"
        elif not out:
            c = "нет расходной стороны в выгрузке"
        elif o_ser == i_ser:
            c = ("сошлось" if abs(o_q - i_q) <= EPS
                 else "серии те же, количество разошлось")
        else:
            # серии сторон разные — тоннаж переезжает между проектами
            only_o = o_ser - i_ser
            only_i = i_ser - o_ser
            if "БЕЗ СЕРИИ" in only_o and only_i:
                c = "расход БЕЗ СЕРИИ, приход опознан"
            elif "БЕЗ СЕРИИ" in only_i and only_o:
                c = "приход БЕЗ СЕРИИ, расход опознан"
            else:
                c = "стороны в РАЗНЫХ проектах"
            for a in sorted(only_o):
                for b in sorted(only_i):
                    ser_move[(a, b)] += o_t / max(len(only_o), 1)
        cause[c] += 1
        cause_qty[c] += max(o_t, i_t)
        if c != "сошлось" and len(detail[c]) < 6:
            detail[c].append((k, sorted(o_ser), sorted(i_ser),
                              round(o_q, 3), round(i_q, 3),
                              round(o_t, 3), round(i_t, 3),
                              sorted({r["badge"] for r in out}),
                              sorted({r["badge"] for r in inn})))

    print("\n=== 2. ПОЧЕМУ РАСХОДЯТСЯ — ПО ДОКУМЕНТАМ ===")
    print("пар «документ+дата+код+партия»: %d" % len(pair))
    print("%-42s %8s %14s" % ("причина", "пар", "тонн"))
    for c, n in cause.most_common():
        print("%-42s %8d %14.3f" % (c, n, cause_qty[c]))

    for c in cause:
        if c == "сошлось" or not detail[c]:
            continue
        print("\n-- %s — примеры:" % c)
        for k, o_s, i_s, oq, iq, ot, it, ob, ib in detail[c]:
            print("   %s %s · %s · лот %s" % (k[0], k[1], k[2], k[3] or "(нет)"))
            print("      уехало  %10.3f (%.3f т) %-28s %s"
                  % (oq, ot, ",".join(ob), " | ".join(nm(s)[:40] for s in o_s)))
            print("      приехало%10.3f (%.3f т) %-28s %s"
                  % (iq, it, ",".join(ib), " | ".join(nm(s)[:40] for s in i_s)))

    if ser_move:
        print("\n=== 3. КУДА ПЕРЕЕЗЖАЕТ ТОННАЖ (расход в одном проекте, приход в другом) ===")
        top = sorted(ser_move.items(), key=lambda kv: -kv[1])[:20]
        for (a, b), q in top:
            print("   %-40s → %-40s %10.3f т" % (nm(a)[:40], nm(b)[:40], q))

    # ---------- 4. подпись серии принадлежит своему проекту? ----------
    print("\n=== 4. ПОДПИСЬ ВАРИАНТА И ЕЁ ПРОЕКТ ===")
    var_of = defaultdict(Counter)
    for r in rows:
        if r["series"] and r["variant"]:
            var_of[r["variant"]][r["series"]] += 1
    bad = {v: c for v, c in var_of.items() if len(c) > 1}
    synth = d["meta"].get("var_synth_mark", "")
    unknown = d["meta"].get("var_unknown", "")
    real_bad = {v: c for v, c in bad.items()
                if v != unknown and not v.endswith(synth or "\0")}
    print("вариантов всего %d; встречаются больше чем в одном проекте: %d "
          "(без служебных подписей — %d)"
          % (len(var_of), len(bad), len(real_bad)))
    for v, c in sorted(real_bad.items(), key=lambda kv: -sum(kv[1].values()))[:15]:
        print("   %-60s -> %s" % (v[:60], {nm(k2)[:40]: n2 for k2, n2 in c.items()}))


if __name__ == "__main__":
    main()


def detail_report():
    """Подробный разбор двух групп расхождений — для решения по зеркальному правилу."""
    d, rows = load(PATH)
    NAME = {f["series"]: f.get("series_full") or f["series"] for f in d["flows"]}
    nm = lambda k: NAME.get(k, k)
    mv = [r for r in rows if r["flow"] in ("уехало", "приехало")]
    pair = defaultdict(lambda: {"out": [], "in": []})
    for r in mv:
        k = (r["doc"], (r["dt"] or "")[:10], r["code"], r["lot"] or "")
        pair[k]["out" if r["flow"] == "уехало" else "in"].append(r)

    grp_nos, grp_diff = [], []
    for k, s in pair.items():
        out, inn = s["out"], s["in"]
        if not out or not inn:
            continue
        o_ser = {r["series"] for r in out}
        i_ser = {r["series"] for r in inn}
        if o_ser == i_ser:
            continue
        (grp_nos if "БЕЗ СЕРИИ" in (o_ser - i_ser) and (i_ser - o_ser)
         else grp_diff).append((k, out, inn))

    print("\n\n########## РАСХОД БЕЗ СЕРИИ, ПРИХОД ОПОЗНАН — %d пар ##########" % len(grp_nos))
    by_badge = Counter(); by_badge_t = defaultdict(float)
    one_ser = 0; multi = 0
    for k, out, inn in grp_nos:
        i_ser = {r["series"] for r in inn} - {"БЕЗ СЕРИИ"}
        if len(i_ser) == 1: one_ser += 1
        else: multi += 1
        b = ",".join(sorted({r["badge"] for r in inn}))
        by_badge[b] += 1
        by_badge_t[b] += sum(r["tonnes"] or 0.0 for r in out)
    print("приход опознан ОДНОЙ серией: %d пар · несколькими: %d пар" % (one_ser, multi))
    print("%-24s %8s %14s   ← чем опознан ПРИХОД" % ("бейдж прихода", "пар", "тонн расхода"))
    for b, n in by_badge.most_common():
        print("%-24s %8d %14.3f" % (b, n, by_badge_t[b]))
    print("ИТОГО тоннаж расхода: %.3f т"
          % sum(sum(r["tonnes"] or 0.0 for r in o) for _, o, _ in grp_nos))
    print("в какие проекты уйдёт (топ-15):")
    dst = defaultdict(float)
    for k, out, inn in grp_nos:
        i_ser = {r["series"] for r in inn} - {"БЕЗ СЕРИИ"}
        if len(i_ser) != 1: continue
        dst[next(iter(i_ser))] += sum(r["tonnes"] or 0.0 for r in out)
    for s_, q in sorted(dst.items(), key=lambda kv: -kv[1])[:15]:
        print("   %-52s %10.3f т" % (nm(s_)[:52], q))

    print("\n\n########## СТОРОНЫ В РАЗНЫХ ПРОЕКТАХ — %d пар ##########" % len(grp_diff))
    print("%-13s %-10s %-11s %9s  %-34s %-34s" %
          ("документ", "дата", "код", "тонн", "уехало (бейдж)", "приехало (бейдж)"))
    tot = 0.0
    for k, out, inn in sorted(grp_diff, key=lambda x: -sum(r["tonnes"] or 0 for r in x[1])):
        t = sum(r["tonnes"] or 0.0 for r in out)
        tot += t
        ob = ",".join(sorted({r["badge"] for r in out}))
        ib = ",".join(sorted({r["badge"] for r in inn}))
        print("%-13s %-10s %-11s %9.3f  %-34s %-34s"
              % (k[0], k[1], k[2][:11], t,
                 (nm(sorted({r["series"] for r in out})[0])[:24] + " [" + ob + "]"),
                 (nm(sorted({r["series"] for r in inn})[0])[:24] + " [" + ib + "]")))
    print("ИТОГО: %.3f т" % tot)
    cb = Counter()
    for k, out, inn in grp_diff:
        cb[(",".join(sorted({r["badge"] for r in out})),
            ",".join(sorted({r["badge"] for r in inn})))] += 1
    print("\nпары бейджей (расход → приход):")
    for (a, b), n in cb.most_common():
        print("   %-18s → %-18s %4d" % (a, b, n))
