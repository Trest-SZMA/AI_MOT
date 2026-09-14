# -*- coding: utf-8 -*-
"""Сверка НАШЕГО остатка по движениям с ФАКТИЧЕСКИМ остатком 1С.

Вход:
  out/sales_data.json                          — наш расчёт (построчные движения)
  data/СебестоимостьТоваровОстатки_*.csv       — снимок остатков 1С (самый свежий)

Зачем: до сих пор факт 1С брался из «Остатки на складах *.xlsx», а он покрывал
91 склад из 1 852 и только строки с заполненной серией — сверять было почти не с
чем (§6, «ДВА РАЗНЫХ ОСТАТКА»). Новая выгрузка даёт остаток по всем складам с
серией и складской территорией, то есть впервые позволяет проверить модель
движений против 1С построчно.

⚠️ СРАВНИВАЕМ В РОДНЫХ ЕДИНИЦАХ. «КоличествоОстаток» 1С — количество в базовой
единице номенклатуры, ровно как наш `qty` («количество как в документе»). Тонны
считаются отдельно и только для итогов: у части номенклатуры коэффициент
пересчёта в 1С не заполнен, и перевод в тонны добавил бы к расхождению свою
ошибку поверх сверяемой.

⚠️ ДАТЫ РАЗНЫЕ. Наш остаток — на конец периода выгрузки движений, снимок 1С — на
свой момент; разрыв печатается в шапке.

⚠️ ГРАНИЦА ПЕРИОДА. Выгрузка движений начинается 2021-10-31. Металл, купленный
раньше и не проведённый «вводом остатков», в приходе не виден, а его расход
виден — отсюда ОТРИЦАТЕЛЬНЫЕ остатки. Это не ошибка модели, а нехватка входных
данных, поэтому отрицательная часть считается отдельно и в общий процент
сходимости не подмешивается.

Уровни, от самого честного к самому требовательному:
  1. склад + код              — проверяет МОДЕЛЬ ДВИЖЕНИЙ, серия не участвует;
  2. склад + код + строка серии — сверх модели проверяет восстановление серии;
  3. проект (бизнес-план)     — как остаток разложился по БП.
"""
import sys, os, csv, json, io, glob
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
from pipeline import (read_text_lines, to_float, load_nomenklatura, load_series_ref,
                      qty_to_tonnes, norm_series, series_key)
import config as C

EPS = 0.0005


def fmt(x, n=3):
    s = ("%%.%df" % n) % x
    a, _, b = s.partition(".")
    neg = a.startswith("-")
    a = a.lstrip("-")
    a = " ".join([a[max(0, i - 3):i] for i in range(len(a), 0, -3)][::-1])
    return ("−" if neg else "") + a + ("," + b if b else "")


def pct(a, b):
    return 100.0 * a / b if b else 0.0


# ---------------------------------------------------------------------------
# НАШ остаток из построчных движений
# ---------------------------------------------------------------------------
def our_balance(s2p):
    """Сальдо по тем же спискам, что и в дашборде: BALANCE_IN − BALANCE_OUT из
    `src/config.py`. Возврат поставщику в BALANCE_OUT остаётся — в сырых потоках
    он ещё не вычтен из «купили» (в оболочке наоборот, см. комментарий у BAL_OUT
    в `web/tabs.js`: вычесть надо ровно один раз, но в каждом месте по-своему)."""
    d = json.load(io.open(os.path.join(ROOT, "out", "sales_data.json"), encoding="utf-8"))
    S, meta = d["S"], d["meta"]
    ci = {c: i for i, c in enumerate(meta["move_cols"])}
    flows = meta["move_flows"]
    sgn = {}
    for f in C.BALANCE_IN:
        sgn[f] = sgn.get(f, 0.0) + 1.0
    for f in C.BALANCE_OUT:
        sgn[f] = sgn.get(f, 0.0) - 1.0

    o = dict(wc=defaultdict(float), wcv=defaultdict(float), proj=defaultdict(float),
             wc_t=defaultdict(float), proj_t=defaultdict(float), site_t=defaultdict(float),
             wc_unp=defaultdict(float), unpaired=defaultdict(float))
    # ⚠️ ЗНАКИ ЯВНО, А НЕ ПО ПОРЯДКУ C.REWORK_UNPAIRED: там расход стоит ПЕРВЫМ,
    # и zip с (+1, −1) переворачивает их местами.
    sgn_unp = dict(sgn)
    sgn_unp["переработка_приход_без_пары"] = +1.0
    sgn_unp["переработка_расход_без_пары"] = -1.0
    sk_site = d.get("sk_site", {})
    o["checks"] = d.get("checks", [])
    last = ""
    for r in d["moves"]:
        dt = S[r[ci["dt"]]]
        if dt > last:
            last = dt
        _fl = flows[r[ci["flow"]]]
        if _fl in C.REWORK_UNPAIRED:
            o["unpaired"][_fl] += r[ci["qty"]]
        _ku = sgn_unp.get(_fl)
        if _ku:
            o["wc_unp"][(S[r[ci["sklad"]]], S[r[ci["code"]]])] += _ku * r[ci["qty"]]
        k = sgn.get(_fl)
        if not k:
            continue
        w = S[r[ci["sklad"]]]
        c = S[r[ci["code"]]]
        v = S[r[ci["variant"]]]
        p = S[r[ci["series"]]]
        q = k * r[ci["qty"]]
        t = k * r[ci["tonnes"]]
        o["wc"][(w, c)] += q
        o["wc_t"][(w, c)] += t
        o["wcv"][(w, c, norm_series(v))] += q
        o["proj"][p] += q
        o["proj_t"][p] += t
        o["site_t"][sk_site.get(w, "—")] += t
    return o, last, sk_site


# ---------------------------------------------------------------------------
# ФАКТ 1С из снимка остатков
# ---------------------------------------------------------------------------
def fact_1c(path, by_name, by_guid, s2p, sk_site):
    rd = csv.reader(read_text_lines(path))
    hdr = next(rd)
    I = {h: i for i, h in enumerate(hdr)}
    for n in ("АналитикаУчетаНоменклатурыСкладскаяТерритория",
              "АналитикаУчетаНоменклатурыНоменклатураКод",
              "АналитикаУчетаНоменклатурыСерия", "КоличествоОстаток"):
        if n not in I:
            raise SystemExit("в выгрузке нет колонки %s" % n)
    f = dict(wc=defaultdict(float), wcv=defaultdict(float), proj=defaultdict(float),
             wc_t=defaultdict(float), proj_t=defaultdict(float), site_t=defaultdict(float))
    snap, rows, noserie_t = "", 0, 0.0
    for r in rd:
        rows += 1
        w = r[I["АналитикаУчетаНоменклатурыСкладскаяТерритория"]].strip()
        c = r[I["АналитикаУчетаНоменклатурыНоменклатураКод"]].strip()
        s = r[I["АналитикаУчетаНоменклатурыСерия"]].strip()
        q = to_float(r[I["КоличествоОстаток"]])
        g = r[I["АналитикаУчетаНоменклатурыНоменклатураГуид"]].strip().upper()
        nm = r[I["АналитикаУчетаНоменклатурыНоменклатура"]].strip()
        if not snap:
            snap = (r[I["ДатаВыгрузки"]] or "")[:10]
        info = by_guid.get(g) or by_name.get(nm) or {}
        t = qty_to_tonnes(q, info.get("unit_base", ""), info)
        f["wc"][(w, c)] += q
        f["wc_t"][(w, c)] += t
        f["site_t"][sk_site.get(w, "—")] += t
        if s:
            f["wcv"][(w, c, norm_series(s))] += q
            f["proj"][series_key(s, s2p) or ("n:" + norm_series(s))] += q
            f["proj_t"][series_key(s, s2p) or ("n:" + norm_series(s))] += t
        else:
            f["proj"]["БЕЗ СЕРИИ"] += q
            f["proj_t"]["БЕЗ СЕРИИ"] += t
            noserie_t += t
    return f, snap, rows, noserie_t


def main():
    st = sorted(glob.glob(os.path.join(ROOT, "data", "СебестоимостьТоваровОстатки_*.csv")))
    if not st:
        raise SystemExit("нет data/СебестоимостьТоваровОстатки_*.csv")
    path = st[-1]
    by_name, by_guid = load_nomenklatura(
        sorted(glob.glob(os.path.join(ROOT, "data", "Номенклатура_*.csv")))[-1])
    by_project, s2p = load_series_ref(
        sorted(glob.glob(os.path.join(ROOT, "data", "СерииНоменклатуры_*.csv")))[-1])

    pname = {k: (v.get("name") or k) for k, v in by_project.items()}

    o, last_move, sk_site = our_balance(s2p)
    data_checks = o["checks"]
    f, snap, rows, noserie_t = fact_1c(path, by_name, by_guid, s2p, sk_site)

    print("=" * 78)
    print("СВЕРКА ОСТАТКОВ: наш расчёт по движениям  ↔  факт 1С")
    print("=" * 78)
    print("  снимок 1С        : %s · %s · %d строк" % (snap, os.path.basename(path), rows))
    print("  наши движения по : %s" % last_move)
    if snap and last_move and snap > last_move:
        print("  ⚠️ разрыв %s … %s — движения этих суток в наш остаток НЕ вошли"
              % (last_move, snap))

    # ---- итог ----
    ot, ft = sum(o["wc_t"].values()), sum(f["wc_t"].values())
    op = sum(v for v in o["wc_t"].values() if v > 0)
    on = sum(v for v in o["wc_t"].values() if v < 0)
    print("\n--- ИТОГО ПО КОМПАНИИ, тонны ---")
    print("  наш остаток по движениям : %14s т   (плюсы %s, минусы %s)"
          % (fmt(ot), fmt(op), fmt(on)))
    print("  факт 1С                  : %14s т" % fmt(ft))
    print("  Δ                        : %14s т  (%.2f %% от факта)"
          % (fmt(ot - ft), pct(ot - ft, ft)))
    print("  из факта 1С БЕЗ серии    : %14s т  (%.1f %%)"
          % (fmt(noserie_t), pct(noserie_t, ft)))

    # ---- по типу площадки ----
    # Периметр варианта 1 диаграммы Ганта — только «Заготовка» (§5.3), поэтому
    # сходимость именно этой строки важнее общего итога.
    print("\n--- ПО ТИПУ ПЛОЩАДКИ, тонны ---")
    print("  %-22s %13s %13s %13s" % ("тип", "наш", "1С", "Δ"))
    for k in sorted(set(o["site_t"]) | set(f["site_t"]),
                    key=lambda x: -abs(o["site_t"].get(x, 0.0) - f["site_t"].get(x, 0.0))):
        a, b = o["site_t"].get(k, 0.0), f["site_t"].get(k, 0.0)
        print("  %-22s %13s %13s %13s" % (k, fmt(a), fmt(b), fmt(a - b)))

    # ---- покрытие ----
    ow, fw = set(w for w, _ in o["wc"]), set(w for w, _ in f["wc"])
    print("\n--- ПОКРЫТИЕ ---")
    print("  складов: в движениях %d · в остатках 1С %d · общих %d · только у 1С %d"
          % (len(ow), len(fw), len(ow & fw), len(fw - ow)))
    print("  пар склад+код: у нас %d · у 1С %d · общих %d"
          % (len(o["wc"]), len(f["wc"]), len(set(o["wc"]) & set(f["wc"]))))

    # ---- уровень 1 ----
    print("\n" + "=" * 78)
    print("УРОВЕНЬ 1. СКЛАД + КОД — проверка модели движений (серия не участвует)")
    print("=" * 78)
    keys = set(k for k, v in o["wc"].items() if abs(v) > EPS) | \
           set(k for k, v in f["wc"].items() if abs(v) > EPS)
    cat, cat_t = defaultdict(int), defaultdict(float)
    rowsd, neg_keys = [], []
    for k in keys:
        a, b = o["wc"].get(k, 0.0), f["wc"].get(k, 0.0)
        # тоннаж расхождения берём из тоннажной пары тех же ключей
        dt_ = o["wc_t"].get(k, 0.0) - f["wc_t"].get(k, 0.0)
        if a < -EPS:
            neg_keys.append((abs(a - b), k, a, b))
            cat["наш остаток ОТРИЦАТЕЛЬНЫЙ (граница периода)"] += 1
            cat_t["наш остаток ОТРИЦАТЕЛЬНЫЙ (граница периода)"] += dt_
            continue
        if abs(a - b) <= EPS:
            name = "сошлось точно"
        elif abs(b) <= EPS:
            name = "есть у нас, у 1С нуль"
            rowsd.append((abs(a - b), k, a, b))
        elif abs(a) <= EPS:
            name = "есть у 1С, у нас нуль"
            rowsd.append((abs(a - b), k, a, b))
        else:
            name = "обе стороны есть, величина расходится"
            rowsd.append((abs(a - b), k, a, b))
        cat[name] += 1
        cat_t[name] += dt_
    print("  сверяемых пар склад+код : %d" % len(keys))
    print("  %-44s %6s %8s %14s" % ("", "пар", "доля", "Δ, тонн"))
    for k in sorted(cat, key=lambda x: -cat[x]):
        print("    %-44s %6d %7.1f %% %14s"
              % (k, cat[k], pct(cat[k], len(keys)), fmt(cat_t[k])))
    pos = len(keys) - cat["наш остаток ОТРИЦАТЕЛЬНЫЙ (граница периода)"]
    print("  сходимость без отрицательных: %d из %d (%.1f %%)"
          % (cat["сошлось точно"], pos, pct(cat["сошлось точно"], pos)))

    rowsd.sort(reverse=True)
    print("\n  ТОП-15 расхождений там, где наш остаток НЕ отрицательный"
          " (родная единица):")
    print("  %-32s %-13s %13s %13s %13s" % ("склад", "код", "наш", "1С", "Δ"))
    for _, (w, c), a, b in rowsd[:15]:
        print("  %-32s %-13s %13s %13s %13s" % (w[:32], c, fmt(a), fmt(b), fmt(a - b)))

    neg_keys.sort(reverse=True)
    print("\n  ТОП-10 отрицательных (нехватка прихода до 2021-10-31):")
    print("  %-32s %-13s %13s %13s" % ("склад", "код", "наш", "1С"))
    for _, (w, c), a, b in neg_keys[:10]:
        print("  %-32s %-13s %13s %13s" % (w[:32], c, fmt(a), fmt(b)))

    print("\n  ТОП-12 СКЛАДОВ по расхождению, тонны:")
    bysk = defaultdict(lambda: [0.0, 0.0])
    for (w, c), v in o["wc_t"].items():
        bysk[w][0] += v
    for (w, c), v in f["wc_t"].items():
        bysk[w][1] += v
    top = sorted(bysk.items(), key=lambda kv: -abs(kv[1][0] - kv[1][1]))[:12]
    print("  %-36s %13s %13s %13s" % ("склад", "наш, т", "1С, т", "Δ, т"))
    for w, (a, b) in top:
        print("  %-36s %13s %13s %13s" % (w[:36], fmt(a), fmt(b), fmt(a - b)))

    # ---- три формулы остатка ----
    # ⚠️ САМАЯ ВАЖНАЯ ТАБЛИЦА ОТЧЁТА. 1С считает остаток буквально: приход минус
    # расход по КАЖДОЙ строке регистра. Мы считаем по КЛАССИФИЦИРОВАННЫМ потокам
    # (BALANCE_IN − BALANCE_OUT), и односторонние переработки
    # (`REWORK_UNPAIRED`) в баланс НЕ входят — правило заведено 11.08.2026 под
    # БП 1586, чтобы диагностический расход без весовой пары не уменьшал остаток
    # ПРОЕКТА. Но то же правило применяется и к остатку СКЛАДА, а там оно
    # неверно: если 1С оприходовала металл на склад, он на складе лежит.
    # Таблица показывает цену этого решения в ключах и тоннах.
    print("\n" + "=" * 78)
    print("ПОЧЕМУ РАСХОДИТСЯ: три формулы остатка на одних и тех же ключах")
    print("=" * 78)
    raw = {}
    for c in data_checks:
        raw[(c["sklad"], c["code"])] = c["calc"]
    variants = [
        ("как сейчас (BALANCE_IN − BALANCE_OUT)", o["wc"]),
        ("+ односторонние переработки", o["wc_unp"]),
        ("сырой регистр 1С (приход − расход)", raw),
    ]
    print("  %-40s %8s %8s %9s" % ("формула", "сошлось", "из", "в минусе"))
    for nm, B in variants:
        ok = sum(1 for k in keys if abs(B.get(k, 0.0) - f["wc"].get(k, 0.0)) <= EPS)
        neg = sum(1 for k in keys if B.get(k, 0.0) < -EPS)
        print("  %-40s %8d %8d %9d" % (nm, ok, len(keys), neg))
    print("  односторонние потоки, исключённые из баланса (в родных единицах):")
    for fl, v in sorted(o["unpaired"].items()):
        print("    %-34s %16.3f" % (fl, v))

    # ---- уровень 2 ----
    print("\n" + "=" * 78)
    print("УРОВЕНЬ 2. СКЛАД + КОД + СТРОКА СЕРИИ — наше восстановление серии")
    print("=" * 78)
    k2 = set(k for k, v in f["wcv"].items() if abs(v) > EPS)
    hit = miss = wrong = 0
    d2 = []
    for k in k2:
        a, b = o["wcv"].get(k, 0.0), f["wcv"][k]
        if abs(a - b) <= EPS:
            hit += 1
        elif abs(a) <= EPS:
            miss += 1
            d2.append((abs(a - b), k, a, b))
        else:
            wrong += 1
            d2.append((abs(a - b), k, a, b))
    print("  строк 1С с заполненной серией : %d" % len(k2))
    print("    совпало один в один          : %6d  (%.1f %%)" % (hit, pct(hit, len(k2))))
    print("    этой серии у нас на складе нет: %6d  (%.1f %%)" % (miss, pct(miss, len(k2))))
    print("    серия та же, количество иное  : %6d  (%.1f %%)" % (wrong, pct(wrong, len(k2))))
    d2.sort(reverse=True)
    print("\n  ТОП-10 расхождений:")
    for _, (w, c, v), a, b in d2[:10]:
        print("  %-26s %-13s %-34s наш %11s · 1С %11s"
              % (w[:26], c, str(v)[:34], fmt(a), fmt(b)))

    # ---- уровень 3 ----
    print("\n" + "=" * 78)
    print("УРОВЕНЬ 3. ПО ПРОЕКТУ (бизнес-плану), тонны")
    print("=" * 78)
    ks = set(k for k, v in o["proj_t"].items() if abs(v) > 0.001) | \
         set(k for k, v in f["proj_t"].items() if abs(v) > 0.001)
    rs = sorted(((abs(o["proj_t"].get(k, 0.0) - f["proj_t"].get(k, 0.0)), k) for k in ks),
                reverse=True)
    both = sum(1 for k in ks if abs(o["proj_t"].get(k, 0.0)) > 0.001
               and abs(f["proj_t"].get(k, 0.0)) > 0.001)
    print("  проектов в сверке: %d · есть в обеих сторонах: %d" % (len(ks), both))
    print("\n  ТОП-15 расхождений по проекту:")
    print("  %-44s %12s %12s %12s" % ("проект", "наш, т", "1С, т", "Δ, т"))
    for _, k in rs[:15]:
        a, b = o["proj_t"].get(k, 0.0), f["proj_t"].get(k, 0.0)
        print("  %-44s %12s %12s %12s"
              % (str(pname.get(k, k))[:44], fmt(a), fmt(b), fmt(a - b)))


if __name__ == "__main__":
    main()
