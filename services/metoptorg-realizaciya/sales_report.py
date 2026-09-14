#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Расчёт реализации: data/*.csv + data/*.xlsx  ->  out/sales_data.json
Плюс сверка в консоль (продано всего, доля с реальной серией, бейджи, баланс).

    python3 sales_report.py
"""
import os, sys, json, glob, re
from collections import defaultdict, Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))
import config as C
import pipeline as P

DATA = os.path.join(HERE, "data")
OUT  = os.path.join(HERE, "out")


# --- интернирование строк: таблица S + индексы (файл ужимается в ~3 раза) ---
_STR, _STR_IDX = [], {}
def SI(s):
    """Строка -> индекс в общей таблице строк."""
    s = s or ""
    i = _STR_IDX.get(s)
    if i is None:
        i = _STR_IDX[s] = len(_STR)
        _STR.append(s)
    return i


def newest(pattern):
    files = sorted(glob.glob(os.path.join(DATA, pattern)))
    return files[-1] if files else None


def find_inputs():
    """Автодетект входных файлов по шаблонам имён."""
    stocks = sorted(glob.glob(os.path.join(DATA, "Остатки на складах*.xls*")))
    def key(p):
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", os.path.basename(p))
        return (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else (0, 0, 0)
    stocks.sort(key=key)
    return {
        "movements": newest("СебестоимостьТоваровОбороты_*.csv"),
        "nomen":     newest("Номенклатура_*.csv"),
        "series":    newest("СерииНоменклатуры_*.csv"),
        "sklady":    newest("Склады_*.csv"),
        "divisions": newest("*Производственные_подразделения*.csv"),
        # оплаты за лом: когда и по какому БП платили. Файл необязательный —
        # без него диаграмма Ганта просто не покажет значки оплат.
        "payments":  newest("ОплатаЗаЛом_*.csv") or newest("*Оплата*Лом*.csv"),
        # НАШ план вывоза: сколько месяцев мы закладывали на вывоз по каждому БП
        # (выгрузка из Битрикса, лист «Проверка месяцев»). Файл необязательный —
        # без него на Ганте просто не будет нашей дорожки.
        "bp_months": newest("Проверка_БП_*.xlsx") or newest("*Проверка*месяц*.xlsx"),
        # ПЛАН ПО ОБЪЁМАМ ПРОИЗВОДСТВА («Объемы на загрузку»): что мы сами
        # планируем сделать по каждому БП по месяцам. Файл необязательный —
        # без него на Ганте просто не будет рамок плана.
        "prodplan": newest("*Объемы_на_загрузку*.csv") or newest("*Объемы*загрузк*.csv"),
        # КОММЕНТАРИИ ПО ЗАПАСАМ («Значения нефинансовых показателей»): что
        # экономист написал про каждый остаток и когда. Файл необязательный —
        # без него на Ганте просто не будет значка комментариев у остатка.
        "stock_notes": (newest("*ЗначенияНефинансовыхПоказателей*.csv")
                        or newest("*Нефинансов*.csv")),
        # ВЕРСИИ РАСЧЁТОВ БП (вкладка «Версии БП»): все листы-версии каждого
        # файла БП с выручкой и прибылью — пишет bp_auto_export на сервере.
        "bp_versions": newest("БП_версии_*.json"),
        # ФАКТ ВЫРУЧКИ: регистр «Выручка и себестоимость продаж» (14.09.2026).
        # Файл необязательный — без него рублей в отчёте просто не будет.
        "revenue": newest("ВыручкаИСебестоимостьПродаж_*.csv"),
        # СДЕЛКИ БИТРИКСА (DEAL_*.xlsx): доля лота и юрлицо-победитель по БП
        "deals": newest("DEAL_*.xlsx"),
        # выбор экономистов «какая версия верна» — пишет server.py
        "bp_ver_choice": os.path.join(DATA, "bp_version_choice.json"),
        # ЭТАЛОН СВЕРКИ: фактический остаток на сегодня. В расчёт остатка НЕ
        # входит — наш ОСТАТОК считается из оборотов (приход − расход). Отсюда
        # берутся: колонка «остаток (факт 1С)» и «невязка» на «Закупках /
        # продажах» и обратный ход. Остатки на начало периода не нужны: они
        # выводятся расчётом.
        # Предпочитаем НОВЫЙ формат — «СебестоимостьТоваровОстатки_*.csv»
        # (275 складов, с серией и партией) прежнему «Остатки на складах *.xlsx»
        # (фильтрованный отчёт на 91 склад из 1 852).
        "stock_end": newest("СебестоимостьТоваровОстатки_*.csv") or (stocks[-1] if stocks else None),
        "overrides": os.path.join(DATA, C.OVERRIDES_FILE),
        # ручные корректировки срока вывоза (правятся из дашборда)
        "plan_dates": os.path.join(DATA, C.PLAN_DATES_FILE),
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    f = find_inputs()
    missing = [k for k in ("movements", "nomen", "series", "sklady") if not f[k]]
    if missing:
        sys.exit("Не найдены входные файлы: " + ", ".join(missing))

    print("Входные файлы:")
    for k, v in f.items():
        if v: print(f"  {k:<10} {os.path.basename(v)}")

    # ---------------- справочники ----------------
    nom_by_name, nom_by_guid = P.load_nomenklatura(f["nomen"])
    series_ref, S2P, SG2P = P.load_series_ref(f["series"])
    sklad2base, sklad2parent, sklad2top = P.load_sklady(f["sklady"])
    divisions = P.load_divisions(f["divisions"])
    BASE_CANON = P.base_canon_set(divisions)
    overrides = P.load_overrides(f["overrides"])
    print(f"\nСправочники: номенклатура {len(nom_by_name)}, серии {len(series_ref)}, "
          f"склады с базой «5.» {len(sklad2base)}, ручных привязок {len(overrides)}")
    print(f"Проектов (= бизнес-планов) в справочнике серий: {len(series_ref)}; сопоставлений серия->проект: {len(S2P)}")
    if divisions:
        from collections import Counter as _C
        kk = _C(d["kind"] for d in divisions.values())
        print(f"Производственные подразделения: {len(divisions)} — " +
              ", ".join(f"{k}: {v}" for k, v in kk.most_common()))
    else:
        print("Производственные подразделения: справочник не найден — "
              "цех/база определяются эвристикой по имени склада")

    # ---------------- остатки (текущие) ----------------
    stock_rows, stock_date = ([], None)
    if f["stock_end"]:
        if f["stock_end"].lower().endswith(".csv"):
            stock_rows, stock_date = P.load_stock_csv(f["stock_end"], nom_by_name, nom_by_guid)
        else:
            stock_rows, stock_date = P.load_stock_xlsx(f["stock_end"])
        _sk = len(set(r["sklad"] for r in stock_rows))
        print(f"Остатки (эталон сверки) на {stock_date:%d.%m.%Y}: "
              f"{len(stock_rows)} строк, {_sk} складов — "
              f"{os.path.basename(f['stock_end'])}")

    # ⚠️ ЕДИНИЦА ИЗМЕРЕНИЯ — ТОЛЬКО ИЗ СПРАВОЧНИКА НОМЕНКЛАТУРЫ, НЕ ИЗ ФАЙЛА
    # ОСТАТКОВ (12.08.2026). Раньше здесь строился `unit_by_code`: единица бралась
    # из колонки «Ед. изм.» файла остатков и ПЕРЕБИВАЛА справочник в семи местах
    # расчёта. Файл остатков — эталон для сверки (колонка «остаток (факт 1С)»,
    # «невязка», обратный ход); он не должен влиять на измеряемое, иначе сверка
    # частично сверяет данные сами с собой. К тому же ключ там КОД, а единица
    # правильно определяется ПОСТРОЧНО: у одного кода в разных строках бывает
    # разное имя (дубли справочника), а значит и разная единица.
    # Расходились 10 кодов из 1 647: «Задвижка б/у» т↔шт, «Лом нирезист» т↔кг,
    # «Кабель КВЭВ 1х2х1» км↔м, «Трансформатор ТМПН-63 б/у» шт↔т.
    # Все коды файла остатков есть в справочнике номенклатуры, поэтому единица
    # не теряется ни у одной строки.

    # ---------------- регистр оборотов ----------------
    anchor = P.parse_dt(C.BALANCE_ANCHOR) if C.BALANCE_ANCHOR else None
    print("\nЧитаю регистр оборотов…")
    # Тоннаж строки нужен ещё ВНУТРИ прохода: правило «выпуск без тоннажа не
    # наследует серию у тоннажного сырья» (C.INHERIT_NEEDS_TONNAGE) решается там
    # же, где строится наследование. Считаем той же функцией и по тем же
    # источникам, что и весь остальной тоннаж, — построчно, а не по коду.
    def _tonnes_of(qty, code, name, guid):
        info = nom_by_name.get(name) or nom_by_guid.get(guid or "")
        unit = C.UNIT_BY_CODE.get(code) or (info or {}).get("unit_base", "")
        return P.qty_to_tonnes(qty, unit, info)

    M = P.scan_movements(f["movements"], anchor=anchor, divisions=divisions, s2p=S2P,
                         sklad_top=sklad2top, tonnes_of=_tonnes_of)
    print(f"  строк: {M['rows']}, период {M['period_min']:%d.%m.%Y} … {M['period_max']:%d.%m.%Y}")
    print(f"  строк реализации (внешних): {len(M['sales'])}, "
          f"внутренний оборот «проект Базы»: {len(M['internal_sales'])}")
    print(f"  карта (Партия+Код): {len(M['part_map'])}, "
          f"лотов с наследованием: {len(M['lot_dist'])} (итераций {M['prod_inherit_iters']})")

    # --- как подписывать серию, если в самой строке 1С её нет ---------------
    # Проект известен (его дал каскад), поэтому «— серия восстановлена по лоту —»
    # писать незачем: если во всей выгрузке у проекта РОВНО ОДНА строка серии,
    # берём её — это и есть распределение. Если строк несколько, честно пишем
    # имя проекта с пометкой: выбрать между «…ДС35» и «…ДС35 V» данных нет.
    # Считаем по вариантам ИЗ САМИХ СТРОК движений (series_variants_own), а не по
    # общему счётчику: тот дополнен вариантами со спецификаций складов, и у
    # проекта с одной-единственной строкой серии в движениях там оказывалось две.
    # Из-за этого восстановленные каскадом строки не сливались со своей серией,
    # а висели отдельным узлом «серия не указана» — 2 113 позиций, 172 752 т.
    # ⚠️ Считаем по СТРОКАМ ДВИЖЕНИЙ, а не по счётчику вариантов: тот дополнен
    # вариантами со спецификаций складов и знает про серии, которых в движениях
    # нет. Здесь — только те строки серии, которые 1С реально написала в
    # документах, и проект каждой из них по справочнику.
    #
    # Если у проекта в движениях РОВНО ОДНА такая строка серии — все восстановленные
    # каскадом строки этого проекта относятся именно к ней, и держать их отдельным
    # узлом «серия не указана» незачем: они сливаются со своей серией и суммируются
    # с ней (1 822 позиции, 156 446,613 т).
    # Если строк несколько (845 позиций, 59 515 т) — выбрать между «…ДС35» и
    # «…ДС35 V» данных нет, и они честно остаются отдельной строкой с бейджем.
    _real_var = defaultdict(set)
    for _fr in M["flow_rows"]:
        _sr = (_fr.get("series_raw") or "").strip()
        if not _sr:
            continue
        _pk = P.series_key(_sr, S2P)
        if _pk:
            _real_var[_pk].add(_sr)
    uniq_variant = {k: next(iter(v)) for k, v in _real_var.items() if len(v) == 1}
    # проекты, чьё имя в справочнике САМО является серией этого же проекта
    proj_is_series = {k for k, v in series_ref.items()
                      if (v or {}).get("name")
                      and S2P.get(P.norm_series(v["name"])) == k}
    # ⚠️ ИМЯ ПРОЕКТА В 1С БЫВАЕТ ОПЕЧАТКОЙ СВОЕЙ ЖЕ СЕРИИ — и тогда в дереве
    # появляется лишняя строка, которой в 1С нет. У БП 1491/1493 проект назван
    # «1491/1493 (23y0618 от 05.04.2023 (Лукойл Коми)» — БЕЗ закрывающей скобки,
    # а серия «…(Лукойл Коми))» со скобкой. Формально это разные строки, поэтому
    # подпись падала на имя проекта, и экономисты видели ЧЕТВЁРТЫЙ вариант на
    # 578,545 т при трёх сериях в 1С. Тот же класс, что «третья серия у 1578».
    # Сверяем без скобок, кавычек и лишних пробелов; берём написание СЕРИИ и
    # только если подходит РОВНО ОДНА. Таких проектов 8: лишняя или недостающая
    # скобка (1400, 1368, 1284, 767, 1521, 1124), двойная скобка, разный регистр.
    _LOOSE = lambda s: re.sub(r"\s+", " ", re.sub(r"[()\[\]«»\"]", "", s or "")).strip().lower()
    proj_name_fix = {}
    for k, v in series_ref.items():
        nm = (v or {}).get("name")
        sers = sorted(set(v.get("series") or []))
        if not nm or nm in sers:
            continue
        cand = [s for s in sers if _LOOSE(s) == _LOOSE(nm)]
        if len(cand) == 1:
            proj_name_fix[k] = cand[0]
    if proj_name_fix:
        print("Имя проекта — опечатка своей же серии, подпись взята из серии: %d"
              % len(proj_name_fix))
    print("Проектов, названных по своей же серии: %d из %d "
          "(их восстановленные строки подписываются настоящей серией)"
          % (len(proj_is_series), len(series_ref)))
    print(f"Проектов с единственной строкой серии в движениях: {len(uniq_variant)} "
          f"(их восстановленные строки сливаются со своей серией)")

    def var_label(skey, svar):
        # ⚠️ ПОДПИСЬ ВАРИАНТА ОБЯЗАНА ПРИНАДЛЕЖАТЬ ЭТОМУ ЖЕ ПРОЕКТУ.
        # Карта «лот + код -> строка серии» (part_variant) не знает, какой проект
        # выбрал каскад. Если каскад отнёс строку к проекту 170, а карта лота
        # говорит «1491/1493», мы подписывали строку «1491/1493», но клали её в
        # проект 170 — и тонны пропадали из своего БП и всплывали в чужом с
        # чужой подписью. Реальный случай: приход 18,172 т «Труба НКТ 73*5,5»
        # на Склад МАТ Б.Усинск (документы РУ00-004626) ушёл под проект 170,
        # а у БП 1491/1493 из-за этого остаток стал −18,172.
        # Всего таких строк 81 на 259,698 т.
        # ⚠️ Строка, которая СЕРИЕЙ не является, подписью тоже быть не может.
        # «Объем №1» отсеян из справочника (C.NON_SERIES) и в S2P не попадает —
        # поэтому проверка «принадлежит ли подпись проекту» его пропускала:
        # сверять было не с чем. В 1С у этой серии пустой Проект и пустой
        # контрагент, а карта «лот + код -> строка серии» её всё же запоминала,
        # и 7 строк на 1,650 т стояли в проектах 1401/1519/1552/1555/1689 с
        # подписью «Объем №1». Сами эти строки в БЕЗ СЕРИИ уходят правильно.
        if svar and C.NON_SERIES.match(svar.strip()):
            svar = ""
        if svar:
            pv = S2P.get(P.norm_series(svar))
            if pv is None or pv == skey:
                return svar
            # подпись от чужого проекта — не берём её вовсе
        hit = uniq_variant.get(skey)
        if hit:
            return hit
        # имя проекта — опечатка своей серии: подписываем настоящей серией,
        # иначе в дереве встаёт лишняя строка, которой в 1С нет (см. выше)
        nm = proj_name_fix.get(skey) or (series_ref.get(skey) or {}).get("name")
        if nm:
            # ⚠️ Если ИМЯ ПРОЕКТА само является серией этого проекта (736 из 742
            # проектов в справочнике названы по своей же серии), подпись —
            # НАСТОЯЩАЯ строка 1С, и пометка «серия не указана» тут лишняя: в
            # дереве появлялась вторая строка с ТЕМ ЖЕ текстом, и заказчик видел
            # у БП 918 «918 (23y0618 …)» дважды — одну настоящую, вторую с
            # пометкой. Происхождение по-прежнему видно по бейджу («площадка»,
            # «док», «переработка»): он и говорит, что серия восстановлена.
            # Пометка остаётся там, где имя проекта серией НЕ является (6 проектов,
            # среди них 1578: проект «… (ЗС-УВМ) ДС35» при сериях «… ДС35 V» и
            # «…-Армада ДС35») — там выбрать одну из серий действительно нельзя.
            if skey in proj_is_series:
                return nm
            return nm + C.VAR_SYNTH_MARK
        return C.VAR_UNKNOWN

    # ---- ПОДПИСЬ ВЫПУСКА ПЕРЕРАБОТКИ — ОТ СЫРЬЯ ТОГО ЖЕ ДОКУМЕНТА ----
    # ПРОЕКТ выпуск наследует от доминирующего сырья (бейдж «переработка»), а
    # СТРОКА серии терялась: в строке выпуска 1С серию не пишет, лот у выпуска
    # НОВЫЙ (равен номеру документа), и var_label падал на имя проекта с
    # пометкой. У проекта с двумя сериями это выглядело как ТРЕТЬЯ, несуществующая
    # серия: у БП 1578 рядом с «(ЗС-УВМ) ДС35 V» и «(ЗС-УВМ-Армада) ДС35» стояла
    # «(ЗС-УВМ) ДС35» — имя ПРОЕКТА из справочника, и экономист шёл сверять её
    # с 1С, где такой серии нет. Пример: РУ00-006579 от 16.12.2025, в резку
    # 11,646 т серии «…(ЗС-УВМ-Армада) ДС35» + 9,404 т серии 1539, из резки
    # 21,050 т без серии — проект унаследован верно, подпись потеряна.
    #
    # ⚠️ Берём строку от сырья ТОГО ЖЕ ПРОЕКТА в ТОМ ЖЕ документе и только если
    # она там ОДНА. Если сырьё пришло двумя сериями одного проекта, выбирать
    # нельзя — это была бы выдумка, и такие строки остаются с пометкой.
    def _parts_of(fr_):
        return P.recover_series_parts(
            fr_, M["part_map"], M["lot_dist"], M["uniq_sklad_code"], overrides, None,
            mix_alloc=M["mix_alloc"], move_pair=None,
            sklad_series=M["sklad_series"], plat_proj=M["plat_proj"],
            move_in=M["move_in"], unpartied_alloc=M["unpartied_alloc"])

    # Каскад по строкам считаем ОДИН раз: между проходами он не меняется, а
    # 98 тыс. вызовов на проход — это минуты.
    _rework = []                       # (поток, документ+дата, партия, код, проект, своя строка)
    _moves_v = []                      # то же для сторон ПЕРЕМЕЩЕНИЯ
    for fr in M["flow_rows"]:
        if fr["flow"] not in ("переработка_забрали", "переработка_вернули",
                              "уехало", "приехало"):
            continue
        raw_var = (fr.get("series_raw") or "").strip()
        for skey_raw, _q, _b in _parts_of(fr):
            if not skey_raw:
                continue
            own = (raw_var
                   or M["part_variant_ser"].get(
                       (fr["partia_norm"], fr["code"], skey_raw))
                   or M["part_variant"].get((fr["partia_norm"], fr["code"]), ""))
            rec = (fr["flow"], (fr["regnum"], fr["dt"][:10]),
                   fr.get("partia_norm") or "", fr["code"], skey_raw, own,
                   bool(raw_var))          # строка записана в самой 1С?
            (_moves_v if fr["flow"] in ("уехало", "приехало")
             else _rework).append(rec[:6])
            if fr["flow"] in ("уехало", "приехало"):
                _moves_v[-1] = rec

    # ⚠️ ПЕРЕДЕЛОВ БЫВАЕТ НЕСКОЛЬКО, поэтому проход ИТЕРАТИВНЫЙ. Лом режут повторно:
    # выпуск первой резки становится сырьём второй. Его строка серии не записана в
    # 1С — она унаследована здесь же, и одним проходом вторая резка её уже не видит:
    # у БП 1578 так и осталась «третья серия» на 99,744 т (документы РУМЛ-000287,
    # РУМЛ-000364 — сырьё с бейджем «переработка», то есть строка сама наследованная).
    # Крутим, пока карта растёт (обычно 3–4 прохода), как и наследование серии.
    lot_var = {}                                      # (партия, проект) -> строка серии
    doc_var = {}                                      # (документ+дата) -> проект -> {строки}
    _inh_rows = 0
    for _pass in range(8):
        doc_var = defaultdict(lambda: defaultdict(set))
        for flow, dkey, part, code, skey_raw, own in _rework:
            if flow != "переработка_забрали":
                continue
            v = own or lot_var.get((part, skey_raw), "")
            if not v:
                continue
            # то же правило, что в var_label: подпись от ЧУЖОГО проекта не берём
            pv = S2P.get(P.norm_series(v))
            if pv is not None and pv != skey_raw:
                continue
            doc_var[dkey][skey_raw].add(v)
        # Лот, рождённый выпуском, забирает подпись с собой: иначе продажа и
        # повторная резка того же лота остались бы с пометкой, хотя серия известна.
        grew, _inh_rows = 0, 0
        for flow, dkey, part, code, skey_raw, own in _rework:
            if flow != "переработка_вернули" or own:
                continue                              # своя подпись есть — не трогаем
            vs = doc_var.get(dkey, {}).get(skey_raw)
            if not vs or len(vs) != 1:
                continue
            _inh_rows += 1
            if part and (part, skey_raw) not in lot_var:
                lot_var[(part, skey_raw)] = next(iter(vs))
                grew += 1
        if not grew:
            break
    print("Строк серии, унаследованных выпуском от сырья своего документа: %d "
          "(лотов с такой подписью %d, проходов %d)"
          % (_inh_rows, len(lot_var), _pass + 1))

    # ---- ПОДПИСЬ СТОРОН ПЕРЕМЕЩЕНИЯ — ОДНА НА ДОКУМЕНТ ----
    # «Уехало» и «приехало» — две строки ОДНОГО документа: это один и тот же
    # металл, серия у него не меняется. Проект приходной стороне мы уже отдаём с
    # расходной (move_pair), а СТРОКУ серии каждая сторона получала сама — и они
    # расходились. У БП 918 из-за этого вариант «… VS» показывал «уехало 208,923
    # против приехало 201,043», а по проекту в целом всё сходилось: 132 документа
    # подписаны с одной стороны настоящей строкой 1С, а с другой — заглушкой
    # «строка серии в документе не указана» (РУ00-003670 от 06.05.2024 и др.).
    # Ключ тот же, что у move_pair: документ + дата + код + партия — и обязательно
    # ОДИН проект: под одним документом ездят разные серии.
    # ⚠️ Если строки у сторон РАЗНЫЕ, выбираем не наугад: сначала ту, что записана
    # в самой 1С (501 документ, 915,577 т — там одна сторона из 1С, другая наша
    # реконструкция по лоту), затем — сторону РАСХОДА: проект приходной стороне
    # и так отдаётся с неё же (move_pair), подпись должна идти следом.
    move_var = defaultdict(lambda: defaultdict(
        lambda: {"1c": set(), "out": set(), "any": set()}))
    for flow, dkey, part, code, skey_raw, own, is1c in _moves_v:
        v = own or lot_var.get((part, skey_raw), "")
        if not v:
            continue
        pv = S2P.get(P.norm_series(v))
        if pv is not None and pv != skey_raw:
            continue                      # подпись от чужого проекта не берём
        slot = move_var[dkey + (code, part)][skey_raw]
        slot["any"].add(v)
        if is1c:
            slot["1c"].add(v)
        if flow == "уехало":
            slot["out"].add(v)

    def _mv_pick(regnum, dt10, code, part, skey_raw):
        """Одна строка серии на обе стороны документа перемещения."""
        slot = move_var.get((regnum, dt10, code, part), {}).get(skey_raw)
        if not slot:
            return ""
        for name in ("1c", "out", "any"):
            if len(slot[name]) == 1:
                return next(iter(slot[name]))
        return ""
    _mv_ok = sum(1 for d in move_var.values() for x in d.values()
                 if len(x["any"]) == 1 or len(x["1c"]) == 1 or len(x["out"]) == 1)
    print("Строк серии, общих у сторон перемещения: %d" % _mv_ok)

    # Серия приходной стороны перемещения = серия расходной стороны ТОГО ЖЕ
    # документа: «уехало» и «приехало» — две строки одного документа 1С.
    # Считаем отдельным быстрым проходом: сначала разрешаем «уехало» (там серия
    # чаще всего стоит прямо в 1С), и уже этой картой опознаём «приехало».
    # Без этого приход опознавался по складу-получателю и по серии возникал
    # ложный недоезд: уехала серия X, «приехала» серия Y.
    move_pair = defaultdict(Counter)
    for fr in M["flow_rows"]:
        if fr["flow"] != "уехало":
            continue
        for skey_raw, q, _b in P.recover_series_parts(
                fr, M["part_map"], M["lot_dist"], M["uniq_sklad_code"], overrides, None,
                mix_alloc=M["mix_alloc"], move_pair=M["move_pair"],
                sklad_series=M["sklad_series"], plat_proj=M["plat_proj"],
                move_in=M["move_in"], unpartied_alloc=M["unpartied_alloc"]):
            if skey_raw:
                # ⚠️ ДАТА в ключе обязательна: номера документов 1С повторяются
                # из года в год (8 133 номера перемещений из 23 312), и без даты
                # приход получал серию чужого документа с тем же номером.
                _d = fr["dt"][:10]
                move_pair[(fr["regnum"], _d, fr["code"])][skey_raw] += q
                # и с учётом партии: у одного документа по одному коду бывает
                # несколько партий с разными сериями (см. комментарий в
                # recover_series_parts). Партия у обеих сторон перемещения одна.
                move_pair[(fr["regnum"], _d, fr["code"],
                           fr.get("partia_norm") or "")][skey_raw] += q

    # ---- ЗЕРКАЛО: ВОССТАНОВЛЕННЫЙ ПРИХОД -> РАСХОДНАЯ СТОРОНА (ступень 7б) ----
    # Обратный проход к move_pair: сначала разрешаем «приехало» (теперь уже с
    # картой move_pair), и полученной картой опознаём «уехало», у которого своей
    # серии нет ни в строке, ни в лоте, ни через переработку. Так закрываются
    # документы, где 1С записала серию ТОЛЬКО на приходной стороне или где приход
    # опознан по имени склада-получателя: «11_КОМИ Труба» → «11_КОМИ Труба
    # (21Y1425) БП 14 (на базе)» — это один и тот же склад, которому в имя
    # дописали договор и номер БП.
    # ⚠️ Бейдж запоминаем ТОТ ЖЕ, что у приходной стороны: расход не имеет права
    # выглядеть крепче обоснованным, чем строка, от которой он взят.
    move_rec = defaultdict(dict)
    if C.MOVE_MIRROR_RECOVERED:
        for fr in M["flow_rows"]:
            if fr["flow"] != "приехало":
                continue
            parts_ = P.recover_series_parts(
                fr, M["part_map"], M["lot_dist"], M["uniq_sklad_code"], overrides, None,
                mix_alloc=M["mix_alloc"], move_pair=move_pair,
                sklad_series=M["sklad_series"], plat_proj=M["plat_proj"],
                move_in=M["move_in"], unpartied_alloc=M["unpartied_alloc"])
            for skey_raw, _q, badge_ in parts_:
                if not skey_raw:
                    continue
                _d = fr["dt"][:10]
                move_rec[(fr["regnum"], _d, fr["code"])][skey_raw] = badge_
                move_rec[(fr["regnum"], _d, fr["code"],
                          fr.get("partia_norm") or "")][skey_raw] = badge_
    print("Зеркало перемещения: приходных ключей с одной серией %d"
          % sum(1 for v in move_rec.values() if len(v) == 1))

    # ---- СЕРИЯ-ДОНОР ПО ОСТАТКУ СКЛАДА (правило заказчика 14.09.2026) ----
    # См. C.STOCK_DONOR_MIN_T. Каскад по всем строкам движений считаем здесь
    # ОДИН раз (parts_by_rid) — им же пользуется главный проход ниже, так что
    # лишнего прогона нет. Решение принимается по всему ключу «склад + код»
    # сразу и применяется ступенью 0б каскада: и к движениям, и к строкам
    # реализации (у них общий rid), иначе продажи и остатки разошлись бы.
    parts_by_rid = {}
    for fr in M["flow_rows"]:
        parts_by_rid[fr["rid"]] = P.recover_series_parts(
            fr, M["part_map"], M["lot_dist"], M["uniq_sklad_code"], overrides, None,
            mix_alloc=M["mix_alloc"], move_pair=move_pair,
            sklad_series=M["sklad_series"], plat_proj=M["plat_proj"],
            move_in=M["move_in"], move_rec=move_rec,
            unpartied_alloc=M["unpartied_alloc"])

    def _tonnes_of(fr_, q_):
        info_ = nom_by_name.get(fr_["name"]) or nom_by_guid.get(fr_.get("guid", ""))
        unit_ = C.UNIT_BY_CODE.get(fr_["code"]) or (info_ or {}).get("unit_base", "")
        return P.qty_to_tonnes(q_, unit_, info_)

    stock_fix, stock_log = P.build_stock_fix(M["flow_rows"], parts_by_rid, _tonnes_of)
    for _rid, _parts in stock_fix.items():
        parts_by_rid[_rid] = _parts
    M["stock_fix"] = stock_fix
    _sl_t = sum(x[4] for x in stock_log)
    print("\nСерия-донор по остатку склада: %d правок на %d ключах «склад+код», "
          "%.3f т, строк переписано %d"
          % (len(stock_log), len({(x[0], x[1]) for x in stock_log}), _sl_t,
             len(stock_fix)))
    for sk_, code_, a_, b_, t_, n_ in sorted(stock_log, key=lambda x: -x[4])[:8]:
        print("  %-40s %s: %s -> %s  %.3f т (%d стр.)"
              % (sk_[:40], code_, P.series_display(a_)[:28], P.series_display(b_)[:28], t_, n_))

    # ---------------- ФАКТ ВЫРУЧКИ (регистр «Выручка и себестоимость продаж») ----
    # См. P.load_revenue / P.match_revenue. Рубли ключа «документ + номенклатура»
    # делятся между НАШИМИ строками этого ключа пропорционально нашему
    # количеству — так серию рубли получают ту же, что и тонны.
    rev_map, rev_link, rev_stats = {}, {}, {}
    rev_key_qty = defaultdict(float)
    if f.get("revenue"):
        rev_map, rev_stats = P.load_revenue(f["revenue"])
        for s_ in M["sales"]:
            rev_key_qty[(s_["regnum"], s_["name"])] += s_["qty"]
        rev_link, link_st = P.match_revenue(rev_map, rev_key_qty)
        _linked = sum(rev_map[v][1] for v in set(rev_link.values()))
        print("\nФакт выручки: %d строк регистра, выручка без НДС %.0f ₽, "
              "себестоимость %.0f ₽, по %s; ключей «документ+номенклатура» %d"
              % (rev_stats["rows"], rev_stats["rev"], rev_stats["cost"],
                 rev_stats["max_date"] or "—", len(rev_map)))
        print("  связано с нашими продажами: по имени %d, по документу %d, по "
              "количеству %d, не связано %d наших ключей; выручка на связанных "
              "ключах %.0f ₽ (%.1f %% регистра)"
              % (link_st["exact"], link_st["by_doc"], link_st["by_qty"],
                 link_st["unmatched"], _linked,
                 100.0 * _linked / rev_stats["rev"] if rev_stats["rev"] else 0))
    rev_stats["linked"] = sum(rev_map[v][1] for v in set(rev_link.values())) if rev_link else 0.0

    # ---------------- атрибуция серий продажам ----------------
    edits_log = []
    positions = defaultdict(lambda: {"qty": 0.0, "tonnes": 0.0, "badge": None, "docs": [], "mix": None,
                                     "rub": 0.0, "cost": 0.0})
    badge_qty = Counter(); badge_rows = Counter()

    direct_tonnes = 0.0        # прямой пересчёт по сырым строкам (автопроверка §11)
    no_lot_rows = 0            # строк реализации, где в 1С нет описания партии
    alloc_qty_gap = []
    for s in M["sales"]:
        if not s["partia_norm"]:
            no_lot_rows += 1
        parts = P.recover_series_parts(
            s, M["part_map"], M["lot_dist"], M["uniq_sklad_code"], overrides, edits_log,
            mix_alloc=M["mix_alloc"], sklad_series=M["sklad_series"],
            plat_proj=M["plat_proj"], move_in=M["move_in"],
            unpartied_alloc=M["unpartied_alloc"], stock_fix=M["stock_fix"])
        _pq = sum(x[1] for x in parts)
        if abs(_pq - s["qty"]) > 1e-9:
            alloc_qty_gap.append((s["regnum"], s["code"], s["qty"], _pq,
                                  s.get("partia_norm") or ""))
        # База — только у базы и цеха. У заготовки («Когалым КНПО») базы нет:
        # раньше её подставлял справочник складов (ВысшийРодитель «5. База
        # Когалым»), и в дереве появлялось «Заготовка · База Когалым» — неверно.
        if s.get("site_src") == "справочник" and s["site"] not in ("База", "Цех"):
            base = P.normalize_base(s.get("div_base"), BASE_CANON) or "—"
        else:
            base = P.normalize_base(s.get("div_base") or sklad2base.get(s["sklad"]), BASE_CANON) or "—"
        division = s.get("div_label") or (base if base != "—" else "") or \
            P.normalize_base(sklad2base.get(s["sklad"]), BASE_CANON) or "—"
        info = nom_by_name.get(s["name"]) or nom_by_guid.get(s["guid"])
        unit = C.UNIT_BY_CODE.get(s["code"]) or (info or {}).get("unit_base", "")
        direct_tonnes += P.qty_to_tonnes(s["qty"], unit, info)
        # Смешанный лот разложен ЗАРАНЕЕ (P.build_mix_alloc): сначала точное
        # количество, потом ёмкость компонент, и только в крайнем случае деление.
        # Поэтому в документах не появляется сумм, которых нет в 1С.
        badge = None
        for _s, _q, b in parts:
            badge = P._best_badge(badge, b)
        mix = [(p[0], p[1]) for p in parts] if len(parts) > 1 else None

        for comp_key, amt, part_badge in parts:
            if abs(amt) < 1e-9: continue
            skey = comp_key if comp_key else "БЕЗ СЕРИИ"
            # Конкретная строка серии из документа. Проект её обобщает («1578 …ДС35»),
            # а вариант может отличаться на несколько символов («…ДС35 V»).
            # Вариант берём под СВОЮ серию: у смешанного лота самое тяжёлое
            # написание принадлежит доминирующей серии, и 8 кг серии 1660
            # подписывались строкой «1578 … ДС35 V» — чужой серией.
            svar = (s.get("series_raw") or "").strip()
            if not svar:
                svar = (M["part_variant_ser"].get((s["partia_norm"], s["code"], skey))
                        or M["part_variant"].get((s["partia_norm"], s["code"]), "")
                        # лот родился в переработке — подпись унаследована от сырья
                        or lot_var.get((s.get("partia_norm") or "", skey), ""))
            svar = var_label(skey, svar)
            key = (base, s["sklad"], s["site"], s.get("analytical") or "",
                   s["code"], skey, svar, division)
            p = positions[key]
            p["qty"] += amt
            # Тоннаж считаем ПОСТРОЧНО: у одного кода в разных строках бывает разное
            # имя номенклатуры (дубли в справочнике) => разная единица/коэффициент.
            p["tonnes"] += P.qty_to_tonnes(amt, unit, info)
            # выручка и себестоимость части — доля ключа по нашему количеству
            _rk = rev_link.get((s["regnum"], s["name"]))
            _rq = rev_key_qty.get((s["regnum"], s["name"]), 0.0)
            if _rk and abs(_rq) > 1e-12:
                _share = amt / _rq
                p["rub"] += rev_map[_rk][1] * _share
                p["cost"] += rev_map[_rk][2] * _share
            p["badge"] = P._best_badge(p["badge"], part_badge)
            if mix and not p["mix"]: p["mix"] = mix
            p["docs"].append({"regnum": s["regnum"], "dt": s["dt"].strftime("%Y-%m-%d"),
                              "month": s["month"], "qty": round(amt, 3),
                              "_t": P.qty_to_tonnes(amt, unit, info),
                              "buyer": s["buyer"], "org": s["org"],
                              "partia": s["partia"][:90], "badge": part_badge})
            badge_qty[part_badge] += amt
        badge_rows[badge] += 1

    if alloc_qty_gap:
        print("ОШИБКА раскладки продаж: строк с потерей количества %d, первые: %s"
              % (len(alloc_qty_gap), alloc_qty_gap[:8]))

    # ---------------- сборка позиций ----------------
    # имя/гуид номенклатуры по коду — один раз
    name_by_code, guid_by_code = {}, {}
    for s in M["sales"]:
        name_by_code.setdefault(s["code"], s["name"])
        guid_by_code.setdefault(s["code"], s["guid"])

    # --- договор (БП) для каждой серии: перебираем ВСЕ варианты написания,
    #     встреченные в движениях и в справочнике серий («Серия» + «Проект») ---
    contract_of, raw_of, csrc_of = {}, {}, {}
    # ⚠️ ПО ВСЕМ СЕРИЯМ, А НЕ ТОЛЬКО ПО ПРОДАННЫМ. Раньше здесь стояло
    # `set(k[5] for k in positions)` — только серии, по которым были ПРОДАЖИ.
    # У серии, которой нет в справочнике («сама себе проект», ключ `n:…`) и по
    # которой продаж ещё не было, `raw_of` оставался пустым, и в извлекатели
    # уходила ПУСТАЯ строка: договор «—», направление «Прочее», хотя в самой
    # строке серии всё написано. Пример — «1929 2026022983 от 27.07.2026
    # (Лукойл-Коми)», 1 224,425 т: `extract_contract` на ней даёт 2026022983,
    # `extract_direction` — «Лукойл-Коми».
    # `M["series_repr"]` собран в проходе по регистру и покрывает ВСЕ серии,
    # встреченные в движениях, поэтому объединяем с ним.
    # Заказчик 12.08.2026: справочник серий актуален и обороты выгружены с самой
    # ранней даты — просить у 1С нечего, разбирать строку серии это наша работа.
    for skey in set(k[5] for k in positions) | set(M["series_repr"]):
        if skey == "БЕЗ СЕРИИ" or str(skey).startswith("площадка:"):
            continue
        c, ck, src, raw = P.resolve_contract(skey, M["series_variants"], series_ref)
        contract_of[skey] = (c, ck); csrc_of[skey] = src
        raw_of[skey] = raw or M["series_repr"].get(skey, "")

    # Отображаемое написание договора — самое «тяжёлое» исходное, а ключ
    # (гомоглифы свёрнуты) используется для группировки: 22С0371 ≡ 22C0371.
    disp_votes = defaultdict(Counter)
    for (base, sklad, site, ana, code, skey, svar, division), p in positions.items():
        c, ck = contract_of.get(skey, ("", ""))
        if ck: disp_votes[ck][c] += p["qty"]
    contract_disp = {k: v.most_common(1)[0][0] for k, v in disp_votes.items()}

    # Направление — свойство ПРОЕКТА, а не отдельной позиции: иначе один и тот же
    # проект попадает в два направления и его закупка считается дважды.
    dir_votes = defaultdict(Counter)
    for (base, sklad, site, ana, code, skey, svar, division), p in positions.items():
        if skey == "БЕЗ СЕРИИ":
            d = "Без серии"
        elif str(skey).startswith("площадка:"):
            d = P.extract_direction(sklad)
        else:
            d = P.extract_direction(raw_of.get(skey, ""))
        dir_votes[skey][d] += p["qty"]
    direction_of = {k: v.most_common(1)[0][0] for k, v in dir_votes.items()}

    out_items = []
    positions_exact = 0.0      # сумма позиций ДО округления до 3 знаков
    for (base, sklad, site, ana, code, skey, svar, division), p in positions.items():
        positions_exact += p["tonnes"]
        name = name_by_code.get(code, "")
        info = nom_by_name.get(name) or nom_by_guid.get(guid_by_code.get(code, ""))
        unit = C.UNIT_BY_CODE.get(code) or (info or {}).get("unit_base", "")
        report_group = (info or {}).get("report_group") or name
        cat = P.category_of(name, report_group)
        is_real = skey != "БЕЗ СЕРИИ" and not str(skey).startswith("площадка:")
        is_plat = str(skey).startswith("площадка:")
        ref = series_ref.get(skey, {}) if is_real else {}
        # Имя проекта — канонический ярлык серии (V/VS уже сведены в 1С).
        raw = (ref.get("name") or raw_of.get(skey, "")) if is_real else ""
        no_bp = False
        if is_real:
            _c, _ck = contract_of.get(skey, ("", ""))
            contract = contract_disp.get(_ck, _c)
            contract_grp = _ck
            direction = direction_of.get(skey, P.extract_direction(raw))
            # «Без БП» в строке серии = бизнес-плана нет по существу
            if not contract and P.no_bp_marked(raw):
                contract = "БЕЗ БП (указано в 1С)"; contract_grp = "@nobp"; no_bp = True
        elif is_plat:
            # площадка: в имени склада есть И договор, И номер БП («22С0371 БП 386»)
            _pk, _pdisp, _pc = P.platform_series(sklad)
            contract_grp = P.contract_key(_pc)
            contract = contract_disp.get(contract_grp, _pc)
            raw = _pdisp
            direction = direction_of.get(skey) or ("Площадка" if not _pc else P.extract_direction(sklad))
        else:
            contract = contract_grp = ""
            direction = "Без серии"
        tn = p["tonnes"]
        docs = sorted(p["docs"], key=lambda d: d["dt"], reverse=True)
        for d in docs:
            # тоннаж документа уже посчитан построчно при накоплении позиции
            d["tonnes"] = round(d.pop("_t", 0.0), 3)
            # партия -> короткий номер лота (то, что заказчик ищет в 1С)
            d["lot"] = P.lot_display(P._norm_partia(d.pop("partia")))
            d.pop("month", None); d.pop("org", None)
            d["buyer"] = SI(d["buyer"])
        out_items.append({
            "base": base, "division": division,
            "sklad": sklad, "site": site, "analytical": ana,
            "code": code, "name": name, "category": cat, "report_group": report_group,
            # unit — единица как она есть в 1С, mc — КЛАСС разреза («т», «шт», …).
            # Класс считает Python, а не оболочка: правило группировки единиц
            # живёт в одном месте (config.MEASURE_GROUPS), иначе разрез позиций
            # и разрез движений разъехались бы.
            "unit": unit, "mc": P.measure_class(unit),
            "series": skey, "series_display": raw if is_plat else P.series_display(skey),
            # series_full — ПРОЕКТ (канон), series_variant — конкретная строка серии
            "series_full": raw or P.series_display(skey),
            # Если в документе продажи серии нет и лот её не дал — НЕ подставляем
            # имя проекта: иначе появлялся фантомный «вариант» с продажами, но без
            # закупки (1050 «…(ЗС-УВМ)» — 3 897 т при нулевом приходе, тогда как
            # закупка лежит на варианте «…ДС21»). Помечаем явно.
            "series_variant": svar or C.VAR_UNKNOWN,
            "contract": contract, "contract_key": contract_grp,
            "contract_src": csrc_of.get(skey, ""), "no_bp": no_bp,
            "direction": direction,
            "contragent": ref.get("contragent", ""),
            "datavyvoza": ref.get("datavyvoza", ""),
            "badge": p["badge"], "qty": round(p["qty"], 3), "tonnes": round(tn, 3),
            # mq — количество В ЕДИНИЦЕ КЛАССА (метры пересчитаны в километры);
            # qty рядом остаётся тем, что стоит в документе 1С
            "mq": round(P.measure_qty(p["qty"], unit), 3),
            # выручка без НДС и себестоимость продаж, ₽ (регистр 1С; 0 — не связано)
            "rub": round(p["rub"], 2), "cost": round(p["cost"], 2),
            # даты крайних продаж — для сортировки «новые сверху»
            "last_dt": docs[0]["dt"] if docs else "",
            "first_dt": docs[-1]["dt"] if docs else "",
            "mix": p["mix"], "docs": docs,
        })

    # ---------------- ОДИН проход по всем движениям ----------------
    # Считаем сразу пять вещей, чтобы не гонять 400 тыс. строк четыре раза и,
    # главное, чтобы все разрезы получились из ОДНОЙ атрибуции серии:
    #   * жизненный цикл по проекту и по варианту серии (купили → … → осталось);
    #   * закупку по ключам дерева (проект · вариант · склад · код);
    #   * цепочку «купили → в производство → из производства» (почему закупка по одному коду,
    #     а продажа по другому);
    #   * ПОСТРОЧНЫЕ ДВИЖЕНИЯ (§5): документ → номенклатура → количество →
    #     откуда → куда, без агрегатов, чтобы сверялось с 1С один в один.
    lifecycle = defaultdict(lambda: defaultdict(float))   # серия -> поток -> тонны
    lc_units = defaultdict(lambda: defaultdict(float))    # серия -> поток -> ед.
    lifecycle_var = defaultdict(lambda: defaultdict(float))  # (серия,вариант) -> поток
    buys = defaultdict(float)
    chain = defaultdict(lambda: {"buy": defaultdict(float),
                                 "in": defaultdict(float),
                                 "out": defaultdict(float)})
    # ОСНОВА ДИАГРАММЫ ГАНТА — один набор узлов, из которого собираются ОБА
    # варианта и вся их декомпозиция:
    #   узел = проект · вариант серии · склад,
    #   внутри — месяц -> вектор потоков в порядке C.GANTT_FLOWS
    #            (купили, уехало, продали, возврат, недостачи, на затраты).
    # Вариант 1 берёт только склады заготовки и считает расходом всё, кроме
    # закупки; вариант 2 берёт все склады и не считает расходом перемещения.
    # Держать оба варианта на одних узлах обязательно: иначе итог диаграммы
    # разойдётся с деревом декомпозиции, которое строится из тех же узлов.
    GIX = {fl: i for i, fl in enumerate(C.GANTT_FLOWS)}
    BIX = GIX[C.GANTT_BALANCE]      # строка «сальдо» — полный оборот склада
    NG = len(C.GANTT_FLOWS)
    # ключ узла: (вариант серии, склад, КЛАСС ЕДИНИЦЫ). Класс в ключе, а не
    # отдельной структурой: иначе итог строки Ганты разошёлся бы с её разбором,
    # который строится из этих же узлов, — то же правило, что для вариантов.
    # Недели: тот же вектор, что у месяцев, но ключ — ISO-неделя «YYYY-Www» и
    # только за последние C.GANTT_WEEKS_MONTHS месяцев.
    # ISO-неделя строки движения: «2026-W32». Год берётся ИЗ КАЛЕНДАРЯ ISO, а не
    # из даты: 31 декабря может принадлежать первой неделе следующего года, и
    # «2026-W01» для 31.12.2025 — это правильно, а «2025-W01» было бы ошибкой.
    def _iso_week(dt10):
        y, m_, d_ = int(dt10[:4]), int(dt10[5:7]), int(dt10[8:10])
        iy, iw, _ = datetime(y, m_, d_).isocalendar()
        return "%04d-W%02d" % (iy, iw)

    # граница «последние N месяцев» — от конца периода выгрузки
    _wk_from = ""
    if C.GANTT_WEEKS_MONTHS:
        _pm = M["period_max"]
        _i = _pm.year * 12 + _pm.month - 1 - (C.GANTT_WEEKS_MONTHS - 1)
        _wk_from = "%04d-%02d" % (_i // 12, _i % 12 + 1)
        print("Недели на Ганте: с %s (последние %d мес.)" % (_wk_from, C.GANTT_WEEKS_MONTHS))

    gantt_weeks = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: [0.0] * (len(C.GANTT_FLOWS) + 1))))
    gantt_nodes = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: [0.0] * NG)))
    # ЧТО ВНУТРИ БИЗНЕС-ПЛАНА: группы аналитического учёта и номенклатура отчёта.
    # Группа («Лом чёрных металлов», «ДХНО», «Труба …») — по ней фильтруются сами
    # БП: показать те, где такая номенклатура есть хоть в одной позиции.
    # Номенклатура отчёта («Лом 5А», «Лом 3А») — значки рядом с названием БП.
    # Считаем по приходу и продаже: это и есть содержимое плана.
    # ещё один уровень — КЛАСС ЕДИНИЦЫ: bp_grp[проект][класс][группа] = величина
    bp_grp = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    bp_tag = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    # ДИВИЗИОН БИЗНЕС-ПЛАНА — для фильтра «покажи все планы, где есть Западная
    # Сибирь». Считается по тем же потокам, что группа учёта (куплено + продано):
    # это «где план закупали и откуда продавали», а не «через что металл проезжал»
    bp_div = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    # ИТОГИ ПЛАНА ПО КАЖДОЙ ЕДИНИЦЕ — чтобы в шапке БП было видно «куплено
    # 27,931 т · 15 шт» и переключение разреза не выглядело потерей данных:
    # в тоннах приход пуст, а в штуках он есть, и это надо сказать прямо.
    bp_tot = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    moves = []                      # построчные движения (колоночный формат)
    sk_site = {}                    # склад -> тип подразделения (для дерева)
    sk_unit = {}                    # склад -> подпись подразделения
    sk_div = {}                     # склад -> (дивизион, подразделение)
    # склад -> подразделение КАК В 1С (колонка «…СкладскаяТерриторияПодразделение»),
    # без нашей нормализации: вкладка «Остатки» сверяется с отчётом 1С, и
    # группировать там надо тем же именем, что печатает сама 1С. У старых
    # складов 1С вписывает в подразделение имя склада — так и оставляем.
    sk_podr = {}
    # склад -> [корень справочника подразделений, регион/база] — родитель-ребёнок
    # для вкладки «Остатки» (заказчик 14.09.2026: «Усинск, Ухта — это Коми, а
    # это производство/заготовка, и так со всеми»). Корень и регион — как в
    # справочнике «Производственные подразделения» 1С («ПРОИЗВОДСТВО /
    # ЗАГОТОВКА» → «Коми» → «Усинск»; «БАЗЫ» → «База Осенцы» → …). У складов,
    # которых в справочнике нет, корень — по типу площадки, регион — из папки
    # справочника складов (div_of), у договорных площадок — свой корень.
    sk_tree = {}
    move_flows, _mf_ix = [], {}
    def FI(fl):
        i = _mf_ix.get(fl)
        if i is None:
            i = _mf_ix[fl] = len(move_flows); move_flows.append(fl)
        return i
    _site_cache = {}
    def site_of(podr, sklad):
        k = (podr, sklad)
        v = _site_cache.get(k)
        if v is None:
            v = _site_cache[k] = P.classify_site(podr, sklad, divisions, sklad2top)
        return v

    def div_of(podr, sklad, st):
        """Склад -> (дивизион, подразделение) для декомпозиции варианта 2.

        Дивизион — звено сразу под корнем справочника подразделений: у заготовки
        это регион («Западная Сибирь»), у баз — сама база. Подразделение —
        аналитическое подразделение заказчика («Когалым»). Договорных площадок
        в справочнике нет по определению, для них дивизион один на всех."""
        d = divisions.get((podr or "").strip())
        if d:
            return (d.get("region") or d.get("root") or "—",
                    d.get("analytical") or (podr or "").strip() or "—")
        # старый склад: подразделения в 1С нет, дивизион берём из папки
        # справочника складов, подразделением честно пишем «старые склады» —
        # настоящего у них не существует, и выдумывать его нельзя
        top = sklad2top.get(sklad, "")
        if top:
            hit = C.TOP_FOLDER_SITE.get(top)
            if hit:
                return hit[1], C.OLD_SKLAD_UNIT
            if C.TOP_FOLDER_BASE.match(top):
                return P.normalize_base(top), C.OLD_SKLAD_UNIT
            return (C.TOP_FOLDER_DEFAULT[1] or "—"), C.OLD_SKLAD_UNIT
        if st == "Площадка":
            return "Договорные площадки", ((podr or "").strip() or sklad or "—")
        return "—", ((podr or "").strip() or sklad or "—")

    # Тип подразделения нужен ЗАРАНЕЕ по всем складам: у строки «уехало» надо
    # знать, куда металл поехал, а склад-получатель может встретиться в выгрузке
    # позже. Проход дешёвый — только словари и кэш classify_site.
    for fr in M["flow_rows"]:
        w = fr["sklad"]
        if not w or w in sk_site:
            continue
        st0, _b0, _a0, _s0, slab0 = site_of(fr.get("podr", ""), w)
        sk_site[w] = st0
        sk_unit[w] = slab0 or st0
        sk_div[w] = div_of(fr.get("podr", ""), w, st0)
        sk_podr[w] = (fr.get("podr") or "").strip() or w
        _d = divisions.get((fr.get("podr") or "").strip())
        _root = ((_d or {}).get("root") or
                 {"Заготовка": "ПРОИЗВОДСТВО / ЗАГОТОВКА", "База": "БАЗЫ",
                  "Цех": "БАЗЫ", "Площадка": "ДОГОВОРНЫЕ ПЛОЩАДКИ",
                  "Демонтаж": "ДЕМОНТАЖ", "Ответхранение": "ОТВЕТХРАНЕНИЕ",
                  "Собственные склады": "СОБСТВЕННЫЕ СКЛАДЫ"}.get(st0) or
                 ("СТАРЫЕ СКЛАДЫ" if sk_div[w][1] == C.OLD_SKLAD_UNIT else "ПРОЧЕЕ"))
        # регион: у старых складов он из папки справочника складов («2.Юг»,
        # «3.Пермь»), а справочник подразделений зовёт те же регионы «Южный
        # регион» и «Пермский край» — сводим к именам справочника подразделений,
        # иначе один регион двоится в дереве. Пустой регион (подразделение
        # прямо под корнем или папка без региона) — так и подписываем.
        _reg = (_d or {}).get("region") or sk_div[w][0] or ""
        _reg = {"Юг": "Южный регион", "Пермь": "Пермский край"}.get(_reg, _reg)
        if not _reg or _reg.lower() == _root.lower() or _reg == "—":
            _reg = "— регион не указан —"
        sk_tree[w] = [_root, _reg]

    name_of = {}
    for fr in M["flow_rows"]:
        if fr["code"] and fr["name"]: name_of.setdefault(fr["code"], fr["name"])
    for s_ in M["sales"]:
        name_of.setdefault(s_["code"], s_["name"])


    # ---- ПАРНЫЙ ТОННАЖ ПЕРЕРАБОТКИ ---------------------------------------
    # После пропорциональной раскладки смешанного документа считаем по каждому
    # «документ + БП», сколько тонн есть на обеих сторонах. Только совпадающая
    # часть является парой «в производство ↔ из производства». Излишек одной стороны не
    # выбрасываем: ниже он получает отдельный поток и остаётся в физическом
    # балансе. Это отделяет настоящую резку от суточных документов, где 1С свела
    # несвязанные операции (20,660 т сырья и 34 шт выпуска без веса у БП 1586).
    rework_io = defaultdict(lambda: [0.0, 0.0])
    for fr in M["flow_rows"]:
        if fr["flow"] not in ("переработка_забрали", "переработка_вернули"):
            continue
        info_ = nom_by_name.get(fr["name"]) or nom_by_guid.get(fr.get("guid", ""))
        unit_ = C.UNIT_BY_CODE.get(fr["code"]) or (info_ or {}).get("unit_base", "")
        parts_ = parts_by_rid[fr["rid"]]
        side = 0 if fr["flow"] == "переработка_забрали" else 1
        for skey_raw, q_, _badge in parts_:
            tn_ = P.qty_to_tonnes(q_, unit_, info_)
            if abs(tn_) > 1e-12:
                rework_io[(fr["regnum"], fr["dt"][:10],
                           skey_raw or "БЕЗ СЕРИИ")][side] += tn_
    rework_ratio = {}
    for key_, (inp_, out_) in rework_io.items():
        paired_ = min(inp_, out_)
        rework_ratio[(key_, 0)] = paired_ / inp_ if inp_ > 1e-12 else 0.0
        rework_ratio[(key_, 1)] = paired_ / out_ if out_ > 1e-12 else 0.0

    # ---- ГДЕ ТОННАЖ РОЖДАЕТСЯ ИЗ ШТУК (см. C.BIRTH_IN) ----
    # Предпроход по документам переработки: внутри одного документа сопоставляем
    # сырьё и выпуск ПО СЕРИИ, которую написала 1С. Если у серии в этом документе
    # тоннажного сырья нет, а тоннажный выпуск есть — значит тонны появились из
    # штучного оборудования (разобрали ёмкость), и это приход, а не перекладывание.
    birth_io = defaultdict(lambda: [0.0, 0.0])      # (номер+дата, серия 1С) -> [сырьё, выпуск]
    for fr in M["flow_rows"]:
        if fr["flow"] not in ("переработка_забрали", "переработка_вернули"):
            continue
        sraw = (fr.get("series_raw") or "").strip()
        if not sraw:
            continue                                 # сопоставлять не с чем
        info_ = nom_by_name.get(fr["name"]) or nom_by_guid.get(fr.get("guid", ""))
        unit_ = C.UNIT_BY_CODE.get(fr["code"]) or (info_ or {}).get("unit_base", "")
        tn_ = P.qty_to_tonnes(fr["qty"], unit_, info_)
        if abs(tn_) <= 1e-9:
            continue
        birth_io[(fr["regnum"], fr["dt"][:10], sraw)][
            0 if fr["flow"] == "переработка_забрали" else 1] += tn_
    birth_docs = {k for k, (i_, o_) in birth_io.items()
                  if o_ > 0.0005 and i_ <= 0.0005}
    print("\nТоннаж, родившийся из штучного сырья при разборке: "
          "%d пар «документ+серия», %.3f т"
          % (len(birth_docs), sum(birth_io[k][1] for k in birth_docs)))


    for fr in M["flow_rows"]:
        fl = fr["flow"]
        # Единицу и справочную запись берём ПОСТРОЧНО: у одного кода в разных
        # строках бывает разное имя (дубли справочника) => разная единица.
        info = nom_by_name.get(fr["name"]) or nom_by_guid.get(fr.get("guid", ""))
        unit = C.UNIT_BY_CODE.get(fr["code"]) or (info or {}).get("unit_base", "")
        source_fl = fl
        # каскад уже посчитан один раз (parts_by_rid, с правкой серии-донора)
        base_parts = parts_by_rid[fr["rid"]]
        parts = []
        for skey_raw_, q_, badge_ in base_parts:
            if source_fl in ("переработка_забрали", "переработка_вернули"):
                side_ = 0 if source_fl == "переработка_забрали" else 1
                rk_ = (fr["regnum"], fr["dt"][:10], skey_raw_ or "БЕЗ СЕРИИ")
                ratio_ = rework_ratio.get((rk_, side_), 0.0)
                paired_q_ = q_ * ratio_
                if paired_q_ > 1e-12:
                    parts.append((skey_raw_, paired_q_, badge_, source_fl))
                if q_ - paired_q_ > 1e-12:
                    extra_fl_ = ("переработка_расход_без_пары" if side_ == 0
                                 else "переработка_приход_без_пары")
                    parts.append((skey_raw_, q_ - paired_q_, badge_, extra_fl_))
            else:
                parts.append((skey_raw_, q_, badge_, source_fl))
        st, _b, _a, _s, slab = site_of(fr.get("podr", ""), fr["sklad"])
        raw_var = (fr.get("series_raw") or "").strip()
        for skey_raw, q, badge_p, fl in parts:
            # Концы зависят от фактического потока части; исходную строку 1С не
            # мутируем, чтобы следующая часть той же строки считалась независимо.
            old_flow_ = fr.flow
            fr.flow = fl
            frm, to = P.move_ends(fr, st)
            fr.flow = old_flow_
            skey = skey_raw if skey_raw else "БЕЗ СЕРИИ"
            # вариант — под свою серию (см. комментарий выше по позициям)
            cand = (raw_var or M["part_variant_ser"].get(
                (fr["partia_norm"], fr["code"], skey_raw)) or
                M["part_variant"].get((fr["partia_norm"], fr["code"]), ""))
            # своей подписи нет — берём унаследованную при переработке: сначала
            # по этому документу, потом по лоту, который в нём родился
            if skey_raw and fl in ("уехало", "приехало") and not raw_var:
                # своей записи 1С у строки нет — берём общую строку документа
                _v = _mv_pick(fr["regnum"], fr["dt"][:10], fr["code"],
                              fr.get("partia_norm") or "", skey_raw)
                if _v:
                    cand = _v
            if not cand and skey_raw:
                if fl == "переработка_вернули":
                    _vs = doc_var.get((fr["regnum"], fr["dt"][:10]), {}).get(skey_raw)
                    if _vs and len(_vs) == 1:
                        cand = next(iter(_vs))
                if not cand:
                    cand = lot_var.get((fr.get("partia_norm") or "", skey_raw), "")
            svar_ = var_label(skey, cand)
            tn = P.qty_to_tonnes(q, unit, info)
            # в каких разрезах видна эта строка и каким числом в каждом
            mpairs = P.measure_pairs(q, tn, unit)
            mcls = P.measure_class(unit)
            lifecycle[skey][fl] += tn
            # узел диаграммы Ганта: проект · вариант серии · склад -> месяц.
            # «Уехало» делится по складу-ПОЛУЧАТЕЛЮ: переезд с одной площадки
            # заготовки на другую — не вывоз, металл остался в периметре.
            # состав плана и дивизион — ПО КАЖДОМУ разрезу: у плана, который
            # весь в штуках, значки и дивизион иначе остались бы пустыми, а
            # смешивать в одной подсказке тонны со штуками нельзя.
            if fl in ("куплено", "продано") and skey_raw:
                gname = (info or {}).get("cargo_group") or "— без группы —"
                tname = (info or {}).get("report_group") or fr["name"] or "—"
                dname = sk_div.get(fr["sklad"], ("—", "—"))[0]
                for mk, mv in mpairs:
                    if mv <= 0.0005:
                        continue
                    bp_tot[skey][mk][fl] += mv
                    bp_grp[skey][mk][gname] += mv
                    bp_tag[skey][mk][tname] += mv
                    bp_div[skey][mk][dname] += mv
            # выпуск, у которого в этом документе нет тоннажного сырья той же
            # серии, — это приход тоннажа, а не внутреннее перекладывание
            born = (source_fl == "переработка_вернули"
                    and (fr["regnum"], fr["dt"][:10],
                         (fr.get("series_raw") or "").strip()) in birth_docs)
            gfl = fl
            if fl == "уехало" and sk_site.get(
                    (fr.get("sp") or "").strip()) in C.GANTT_V1_SITES:
                gfl = "уехало_в_заготовку"
            if skey_raw:
                gi = GIX.get(gfl)
                for mk, mv in mpairs:
                    if abs(mv) <= 1e-9:
                        continue
                    wvec = None
                    vec = gantt_nodes[skey][(svar_, fr["sklad"], mk)][fr["dt"][:7]]
                    # ⚠️ НЕДЕЛИ — ТОЛЬКО ЗА ПОСЛЕДНИЕ 12 МЕСЯЦЕВ (C.GANTT_WEEKS_MONTHS).
                    # По неделям смотрят «что происходило вот сейчас»; 2022 год
                    # понедельно не листает никто, а ключей стало бы в 4,3 раза
                    # больше (251 неделя против 59 месяцев) и снимок вырос бы
                    # на ~15 МБ при нынешних 59. За год — примерно +1 МБ.
                    if C.GANTT_WEEKS_MONTHS and fr["dt"][:7] >= _wk_from:
                        wvec = gantt_weeks[skey][(svar_, fr["sklad"], mk)][_iso_week(fr["dt"])]
                    if gi is not None:
                        vec[gi] += mv
                        if wvec is not None: wvec[gi] += mv
                    # возврат поставщику — отрицательный ПРИХОД (так же в 1С):
                    # свою колонку он сохраняет, но приход варианта уменьшает
                    neg = C.NEGATIVE_IN.get(gfl)
                    if neg is not None and GIX.get(neg) is not None:
                        vec[GIX[neg]] -= mv
                        if wvec is not None: wvec[GIX[neg]] -= mv
                    # приход через разборку — в приходную часть варианта
                    if born and mk == C.MEASURE_MAIN:
                        bi = C.BIRTH_IN.get(source_fl)
                        if bi is not None and GIX.get(bi) is not None:
                            vec[GIX[bi]] += mv
                            if wvec is not None: wvec[GIX[bi]] += mv
                    # сальдо — по ВСЕМ потокам, независимо от варианта диаграммы
                    if fl in C.BALANCE_IN:
                        vec[BIX] += mv
                        if wvec is not None: wvec[BIX] += mv
                    elif fl in C.BALANCE_OUT:
                        vec[BIX] -= mv
                        if wvec is not None: wvec[BIX] -= mv
            lc_units[skey][fl] += q
            # то же ПО ВАРИАНТУ серии (точной строке 1С) — чтобы цепочка читалась
            # на уровне серии: купили 1.406 (V) -> уехало 0.756 -> продали на Осенцах
            if svar_:
                lifecycle_var[(skey, svar_)][fl] += tn
            # Закупка по тем же ключам, что и продажа. Склад закупки — это склад
            # ПРИЁМКИ, он может отличаться от склада отгрузки.
            if fl == "куплено" and abs(tn) > 1e-9:
                buys[(skey, svar_, st, fr["sklad"], fr["code"])] += tn
            if fl in ("куплено", "переработка_забрали", "переработка_вернули") \
                    and abs(tn) > 0.0005:
                bucket = ("buy" if fl == "куплено" else
                          "in" if fl == "переработка_забрали" else "out")
                chain[skey][bucket][(fr["code"], fr["sklad"])] += abs(tn)
            # ⚠️ ТОННАЖ И ВЕЛИЧИНА РАЗРЕЗА — С ШЕСТЬЮ ЗНАКАМИ, а не с тремя.
            # Дерево суммирует ИМЕННО эти строки, а «Закупки / продажи» и Excel
            # берут те же потоки из расчёта с полной точностью. При трёх знаках
            # на каждой строке терялось до 0,0005 т, и на большом проекте суммы
            # расходились: у БП 1491/1493 (3 306 строк) «в производство» показывалась
            # 8 580,411 в дереве против 8 580,409 в расчёте, «продано» — 4 777,815
            # против 4 777,812. Показываем по-прежнему три знака (fmt), точность
            # нужна только для сложения. `qty` остаётся ТРЁХЗНАЧНЫМ: это
            # количество как в документе 1С, его сверяют построчно.
            moves.append([FI(fl), SI(fr["dt"]), SI(fr["regnum"]), SI(fr["code"]),
                          round(q, 3), round(tn, 6), SI(frm), SI(to),
                          SI(skey), SI(svar_), SI(badge_p),
                          SI(P.lot_display(fr["partia_norm"])), SI(fr["sklad"]),
                          SI(mcls), 1 if born else 0,
                          # величина в ЕДИНИЦЕ КЛАССА: метры уже переведены в
                          # километры, а «кол-во» рядом осталось как в документе
                          round(P.measure_qty(q, unit), 6),
                          # ⚠️ ПОЛНЫЙ ключ лота (тип + номер + дата). Колонка
                          # «lot» осталась НОМЕРОМ: по нему строится ключ ручной
                          # привязки в overrides.json, и менять его нельзя.
                          # Группировать и подписывать лот надо по `lotk`:
                          # под одним номером в 1С лежат разные документы —
                          # у РУ00-000481 их четыре (приобретение, две
                          # «Производство без заказа» и «Сборка»), и в дереве
                          # они склеивались в один лот с чужой номенклатурой
                          # и чужими складами.
                          SI(fr["partia_norm"]),
                          # ВИД РАБОТ производственного документа: резка /
                          # сортировка / разборка / производство. 1С заполняет
                          # его только у «Производства без заказа», поэтому у
                          # остальных потоков пусто — формулировка тогда прежняя.
                          SI(fr.get("wk") or "")])
            # тип подразделения склада — чтобы в дереве появлялись и склады,
            # где продаж не было, но металл через них проходил
            # sk_site / sk_unit / sk_div заполнены предварительным проходом выше

    # остаток на сегодня по серии — из файла остатков (колонка «Серия»)
    stock_by_series = defaultdict(float)
    for sr in stock_rows:
        sk = P.series_key(sr["series_raw"], S2P) or "БЕЗ СЕРИИ"
        info = nom_by_name.get(sr["name"])
        stock_by_series[sk] += P.qty_to_tonnes(sr["qty"], sr["unit"], info)

    chain_out = {}
    for skey, d in chain.items():
        rec = {}
        for b in ("buy", "in", "out"):
            rows_ = [{"code": c, "name": name_of.get(c, ""), "sklad": sk,
                      "tonnes": round(v, 3)}
                     for (c, sk), v in d[b].items() if abs(v) > 0.0005]
            rows_.sort(key=lambda x: -x["tonnes"])
            if rows_: rec[b] = rows_[:40]
        if rec: chain_out[skey] = rec

    buys_out = [{"series": k[0], "variant": k[1], "site": k[2], "sklad": k[3],
                 "code": k[4], "tonnes": round(v, 3)}
                for k, v in buys.items() if abs(v) > 0.0005]

    flows_var = [dict({"series": k[0], "variant": k[1]},
                      **{fl: round(v, 3) for fl, v in dd.items() if abs(v) > 0.0005})
                 for k, dd in lifecycle_var.items()]

    # ---------------------------------------------------------------------
    # СВЕРКА ОСТАТКОВ ПО СКЛАДАМ: наш складской ↔ факт 1С
    # ---------------------------------------------------------------------
    # Для вкладки «Проверка данных». Считаем ЗДЕСЬ, а не в оболочке, по одной
    # причине: перевод количества в тонны живёт в расчёте (единица по коду,
    # коэффициент пересчёта, C.UNIT_BY_CODE), и повторять его в JS — заводить
    # второй источник правды для тоннажа.
    #
    # ⚠️ НАША СТОРОНА — «складская» формула (BALANCE_1C_*), а не проектная.
    # С фактом 1С сопоставима только она: проектная намеренно выбрасывает
    # односторонние переработки, а склад их держит. Сравнивать проектный
    # остаток с 1С бессмысленно — треть тоннажа в снимке вообще без серии.
    recon_w = defaultdict(lambda: {"our": 0.0, "fact": 0.0,
                                   "n": 0, "same": 0, "only_our": 0,
                                   "only_1c": 0, "differ": 0, "neg": 0})
    _our_q = defaultdict(float)          # (склад, код) -> наш остаток, родная единица
    _our_t = defaultdict(float)          # то же в тоннах
    for fr in M["flow_rows"]:
        fl_ = fr["flow"]
        k_ = 0.0
        if fl_ in C.BALANCE_1C_IN:  k_ += 1.0
        if fl_ in C.BALANCE_1C_OUT: k_ -= 1.0
        if not k_:
            continue
        info_ = nom_by_name.get(fr["name"]) or nom_by_guid.get(fr.get("guid", ""))
        unit_ = C.UNIT_BY_CODE.get(fr["code"]) or (info_ or {}).get("unit_base", "")
        _our_q[(fr["sklad"], fr["code"])] += k_ * fr["qty"]
        _our_t[(fr["sklad"], fr["code"])] += k_ * P.qty_to_tonnes(fr["qty"], unit_, info_)
    # ⚠️ Свой набор складов снимка: `stock_sklady` ниже по коду ещё не создан,
    # а сверять можно только там, где эталон вообще есть.
    _snap_sklady = set(sr["sklad"] for sr in stock_rows)
    _fact_q, _fact_t = defaultdict(float), defaultdict(float)
    for sr in stock_rows:
        info_ = nom_by_name.get(sr["name"])
        _fact_q[(sr["sklad"], sr["code"])] += sr["qty"]
        _fact_t[(sr["sklad"], sr["code"])] += P.qty_to_tonnes(sr["qty"], sr["unit"], info_)
    for k_ in set(_our_q) | set(_fact_q):
        w_, c_ = k_
        if w_ not in _snap_sklady:       # склада нет в снимке — сверять не с чем
            continue
        oq, fq = _our_q.get(k_, 0.0), _fact_q.get(k_, 0.0)
        if abs(oq) <= 0.0005 and abs(fq) <= 0.0005:
            continue
        r_ = recon_w[w_]
        r_["our"] += _our_t.get(k_, 0.0)
        r_["fact"] += _fact_t.get(k_, 0.0)
        r_["n"] += 1
        if oq < -0.0005:                 r_["neg"] += 1
        elif abs(oq - fq) <= 0.0005:     r_["same"] += 1
        elif abs(fq) <= 0.0005:          r_["only_our"] += 1
        elif abs(oq) <= 0.0005:          r_["only_1c"] += 1
        else:                            r_["differ"] += 1
    recon = [dict({"sklad": w_,
                   "podr": sk_unit.get(w_) or "—",
                   "site": sk_site.get(w_) or "—"},
                  **{kk: (round(vv, 3) if isinstance(vv, float) else vv)
                     for kk, vv in v_.items()})
             for w_, v_ in recon_w.items()]
    recon.sort(key=lambda r: -abs(r["our"] - r["fact"]))

    # сводим в таблицу с невязкой
    # выручка и себестоимость продаж по проекту — из позиций (та же раскладка серий)
    rub_by_proj, cost_by_proj = defaultdict(float), defaultdict(float)
    for it_ in out_items:
        rub_by_proj[it_["series"]] += it_.get("rub", 0.0)
        cost_by_proj[it_["series"]] += it_.get("cost", 0.0)
    flows_tbl = []
    for skey in set(lifecycle) | set(stock_by_series):
        fl = lifecycle.get(skey, {})
        # ФИЗИЧЕСКАЯ модель. Внешний приход — только то, что реально пришло в
        # компанию. Внутренние движения (уехало↔приехало, забрали↔вернули) в
        # приход/расход НЕ входят: они перекладывают один и тот же металл.
        # В баланс от них берётся только НЕТТО — потери переработки и недоезд.
        pin = (fl.get("куплено", 0) + fl.get("излишки", 0)
               + fl.get("ввод_остатков", 0) + fl.get("пересортица", 0))
        pout = (fl.get("продано", 0) + fl.get("возврат", 0) + fl.get("списано", 0)
                + fl.get("списано_на_затраты", 0) + fl.get("внутренний_оборот", 0))
        # Переработка — внутреннее движение. Даже если одна сторона без веса
        # (20,660 т -> 34 шт) или без серии, это не доказанная физическая потеря
        # и не внешний расход БП. Односторонние части остаются диагностикой.
        loss_rework = 0.0
        loss_move = fl.get("уехало", 0) - fl.get("приехало", 0)
        ost = stock_by_series.get(skey, 0.0)
        row = {"series": skey,
               "series_display": P.series_display(skey),
               "series_full": raw_of.get(skey, "") or P.series_display(skey),
               "contract": "", "direction": ""}
        # договор/направление — как у позиций продаж
        _c, _ck = contract_of.get(skey, ("", ""))
        row["contract"] = contract_disp.get(_ck, _c)
        row["direction"] = P.extract_direction(raw_of.get(skey, ""))
        for name in C.FLOW_ORDER + list(C.REWORK_UNPAIRED) + ["внутренний_оборот"]:
            row[name] = round(fl.get(name, 0.0), 3)
        row["поступило"] = round(pin, 3)
        row["выбыло"] = round(pout, 3)
        row["потери_переработки"] = round(loss_rework, 3)
        row["недоезд"] = round(loss_move, 3)
        row["остаток"] = round(ost, 3)
        # Тождество: приход − выбытие − потери переработки − недоезд − остаток = 0
        row["невязка"] = round(pin - pout - loss_rework - loss_move - ost, 3)
        # ВТОРОЙ ОСТАТОК — «как считает 1С»: те же потоки плюс односторонние
        # переработки (C.BALANCE_1C_*). Он сверяется с фактом снимка; проектный
        # «остаток» остаётся прежним и правило БП 1586 не трогает.
        row["остаток_1с"] = round(sum(fl.get(k, 0.0) for k in C.BALANCE_1C_IN)
                                  - sum(fl.get(k, 0.0) for k in C.BALANCE_1C_OUT), 3)
        # ФАКТ ВЫРУЧКИ без НДС и себестоимость продаж, ₽ (14.09.2026)
        row["выручка"] = round(rub_by_proj.get(skey, 0.0), 2)
        row["себестоимость"] = round(cost_by_proj.get(skey, 0.0), 2)
        flows_tbl.append(row)
    flows_tbl.sort(key=lambda r: -(r["куплено"] or r["поступило"]))

    # --- проверка «продано больше, чем поступило» по проекту ---
    # Поступило = куплено + излишки + ввод остатков + вернули ГП + пересортица.
    # Перебор с бейджем «1С» = серия стоит в документах продажи, а прихода под
    # этой серией в регистре нет: куплено под другой серией/учётом. Вопрос к 1С.
    oversold = []
    sold_by_proj = defaultdict(float)
    full_by_proj = {}
    for it_ in out_items:
        if it_["series"] == "БЕЗ СЕРИИ" or str(it_["series"]).startswith("площадка:"):
            continue
        sold_by_proj[it_["series"]] += it_["tonnes"]
        full_by_proj.setdefault(it_["series"], it_["series_full"])
    for fr_ in flows_tbl:
        k = fr_["series"]
        sold = sold_by_proj.get(k, 0.0)
        infl = (fr_.get("куплено", 0) + fr_.get("излишки", 0) + fr_.get("ввод_остатков", 0)
                + fr_.get("переработка_вернули", 0) + fr_.get("пересортица", 0))
        if sold > infl + 1.0 and sold > 5:
            oversold.append({"series_full": full_by_proj.get(k, fr_["series_full"]),
                             "sold": round(sold, 3), "inflow": round(infl, 3),
                             "over": round(sold - infl, 3)})
    oversold.sort(key=lambda x: -x["over"])

    # --- проверка «уехало ≠ приехало» по проекту: недоезд/потери в пути ---
    movegap = []
    for fr_ in flows_tbl:
        u, pr_ = fr_.get("уехало", 0), fr_.get("приехало", 0)
        if abs(u - pr_) > 1.0 and max(u, pr_) > 5:
            movegap.append({"series_full": fr_["series_full"],
                            "left": round(u, 3), "arrived": round(pr_, 3),
                            "gap": round(u - pr_, 3)})
    movegap.sort(key=lambda x: -abs(x["gap"]))

    # ---------------- сверка баланса ----------------
    stock_by_key = defaultdict(float)
    stock_names, stock_units = {}, {}
    for sr in stock_rows:
        stock_by_key[(sr["sklad"], sr["code"])] += sr["qty"]
        stock_names.setdefault(sr["code"], sr["name"])
        stock_units.setdefault(sr["code"], sr["unit"])

    # Файл остатков — ФИЛЬТРОВАННЫЙ отчёт: в нём 91 склад против 1641 в движениях.
    # Поэтому сверять можно только те склады, что реально есть в файле остатков;
    # остальные помечаем «нет данных» и НЕ считаем расхождением.
    stock_sklady = set(sr["sklad"] for sr in stock_rows)
    checks = []
    keys = set(M["balance"]) | set(stock_by_key)
    bad = 0
    for k in keys:
        b = M["balance"].get(k, {"name": "", "prihod": 0.0, "rashod": 0.0,
                                 "prihod_pre": 0.0, "rashod_pre": 0.0, "sale": 0.0})
        sklad, code = k
        fact = stock_by_key.get(k, 0.0)
        # ОБРАТНЫЙ ХОД. Якорь — фактический остаток на сегодня (факт из 1С).
        # Отматываем движения назад: остаток_на_дату = факт + расход_после − приход_после.
        # Поэтому файл «остатки на начало периода» не нужен: начальный остаток —
        # это РЕЗУЛЬТАТ расчёта, а не вход.
        opening = fact + (b["rashod"] - b["rashod_pre"]) - (b["prihod"] - b["prihod_pre"])
        # то же, но вперёд от начала регистра — служит проверкой полноты регистра
        opening_fwd = b["prihod_pre"] - b["rashod_pre"]
        calc = b["prihod"] - b["rashod"]                     # расчётный остаток на конец
        diff = round(calc - fact, 3)
        name = b["name"] or stock_names.get(code, "")
        unit = stock_units.get(code, "")
        info = nom_by_name.get(name)
        covered = sklad in stock_sklady        # склад присутствует в файле остатков
        if covered and abs(diff) > 0.001: bad += 1
        checks.append({
            "sklad": sklad, "base": P.normalize_base(sklad2base.get(sklad), BASE_CANON) or "—", "code": code,
            "name": name, "unit": unit, "covered": covered,
            "opening": round(opening, 3), "opening_fwd": round(opening_fwd, 3),
            "prihod": round(b["prihod"], 3), "rashod": round(b["rashod"], 3),
            "sale": round(b["sale"], 3),
            "calc": round(calc, 3), "fact": round(fact, 3), "diff": diff,
            "tonnes_sale": round(P.qty_to_tonnes(b["sale"], unit, info), 3),
        })
    checks.sort(key=lambda c: (not c["covered"], -abs(c["diff"])))
    n_covered = sum(1 for c in checks if c["covered"])
    # Отрицательный выведенный остаток на начало = регистр по этому ключу
    # противоречив (после даты пришло больше, чем факт + ушло).
    n_negative = sum(1 for c in checks if c["covered"] and c["opening"] < -0.001)

    # ---------------- итоги ----------------
    def tsum(pred=lambda i: True):
        return round(sum(i["tonnes"] for i in out_items if pred(i)), 3)
    total_t = tsum()
    # «Реальная серия» = настоящая серия 1С. Псевдо-серия «площадка:<договор>»
    # реальной НЕ считается: это лишь номер договора из имени склада.
    def _real(i):
        return i["series"] != "БЕЗ СЕРИИ" and not str(i["series"]).startswith("площадка:")
    real_t = tsum(_real)
    platform_t = tsum(lambda i: str(i["series"]).startswith("площадка:"))
    by_site = Counter()
    for i in out_items: by_site[i["site"]] += i["tonnes"]
    by_badge_t = Counter()
    for i in out_items: by_badge_t[i["badge"]] += i["tonnes"]
    by_month = Counter()
    for i in out_items:
        for d in i["docs"]: by_month[d["dt"][:7]] += d["tonnes"]

    flow_totals = {n: round(sum(r[n] for r in flows_tbl), 3)
                   for n in C.FLOW_ORDER + list(C.REWORK_UNPAIRED)
                   + ["внутренний_оборот", "поступило",
                                            "выбыло", "потери_переработки",
                                            "недоезд", "остаток", "невязка"]}

    # ---------------- ДИАГРАММА ГАНТА: два варианта на одних узлах ----------
    # Узел — проект · вариант серии · склад с помесячными потоками. Оба варианта
    # диаграммы и вся их декомпозиция собираются из этого набора, поэтому итог
    # строки всегда равен сумме её разбора.
    #
    #   вариант 1 «заготовка»    — только склады заготовки;
    #                              купили → уехало, продали, возврат, недостачи,
    #                              списание на затраты. «Уехало» и есть вывоз:
    #                              металл покинул объект заказчика.
    #   вариант 2 «вся компания» — все склады;
    #                              купили → продали, возврат, недостачи, затраты.
    #                              Перемещения не расход: по всей компании
    #                              «уехало» и «приехало» гасят друг друга.
    # ⚠️ «СЕГОДНЯ» РАСЧЁТА — НЕ ПОЗЖЕ МОМЕНТА СБОРКИ. В выгрузке 1С встречаются
    # проводки БУДУЩИМ числом (21.08.2026 в движениях лежали 6 строк за 31.08):
    # без ограничения они утаскивают точку отсчёта просрочки и линию «сейчас»
    # на дни вперёд, и «просрочен» ставится планам раньше времени. Момент сборки
    # один для всех смотрящих, поэтому правило «отсчёт от конца выгрузки, чтобы
    # у всех одинаково» не страдает.
    _now = datetime.now()
    if M["period_max"] > _now:
        print("⚠️ в движениях есть строки БУДУЩИМ числом (последняя дата %s > "
              "сегодня) — точка «сейчас» ограничена датой сборки"
              % M["period_max"].strftime("%d.%m.%Y"))
    _today_dt = min(M["period_max"], _now)
    today = _today_dt.strftime("%Y-%m")
    # ⚠️ ДЛЯ «ПРОСРОЧЕН» НУЖЕН ДЕНЬ, А НЕ МЕСЯЦ. Сравнение месяцами говорит
    # «в работе» весь месяц срока, даже когда срок пришёлся на его начало:
    # у БП 1864 срок 08.08.2026, выгрузка идёт по 12.08 — план просрочен на
    # четыре дня, а помесячно выходило «в работе». Отсчёт от конца выгрузки,
    # а не от текущей даты, — чтобы у всех получалось одно и то же.
    today_d = _today_dt.strftime("%Y-%m-%d")
    plan_dates = P.load_plan_dates(f["plan_dates"])
    if plan_dates:
        print(f"Ручных корректировок срока вывоза: {len(plan_dates)} "
              f"({f['plan_dates']})")

    def _mn(m):
        return int(m[:4]) * 12 + int(m[5:7]) if m else None

    # ---- ОПЛАТЫ ЗА ЛОМ: когда и по какому бизнес-плану платили ----
    # Привязка — по ГУИДу бизнес-плана из заявки на расходование: это тот же
    # ПроектГуид, которым здесь опознаётся план. Сводить по договору нельзя —
    # под одним договором лежат десятки проектов (см. README).
    pay_rows, pay_stats = ([], {})
    if f.get("payments"):
        pay_rows, pay_stats = P.load_payments(f["payments"])
        print("  дата платежа: из колонки «ДатаПлатежа» %d, из имени документа %d"
              % (pay_stats.get("from_field", 0), pay_stats.get("from_name", 0)))
    pay_by = defaultdict(lambda: {"m": defaultdict(lambda: [0.0, 0]), "d": []})
    for pr in pay_rows:
        rec = pay_by[pr["pk"]]
        a = rec["m"][pr["d"][:7]]
        a[0] += pr["s"]; a[1] += 1
        rec["d"].append(pr)
    if pay_rows:
        print(f"\nОплаты за лом: {pay_stats['with_bp']} строк с бизнес-планом из "
              f"{pay_stats['rows']} ({pay_stats['sum_bp']:,.2f} ₽ из "
              f"{pay_stats['sum_all']:,.2f} ₽), период "
              f"{pay_stats['date_min']} … {pay_stats['date_max']}".replace(",", " "))

    gantt = []
    for skey, nodes in gantt_nodes.items():
        ref = series_ref.get(skey) or {}
        # ⚠️ В 1С встречаются битые даты вывоза («0204-09-30» вместо «2024-09-30»):
        # без фильтра отставание считалось в 21 852 месяца. Берём только
        # правдоподобные годы, остальное — как будто плана нет, и говорим об этом.
        all_dates = [v[:10] for v in (ref.get("dates") or {}).values() if v]
        full = sorted(d for d in all_dates if "2015-01-01" <= d <= "2040-12-31")
        dates = [d[:7] for d in full]
        bad_dates = len(all_dates) - len(full)
        plan_end, plan_end_min = (dates[-1], dates[0]) if dates else ("", "")
        # полная дата нужна редактору срока: диаграмма помесячная, но правят день
        plan_full = full[-1] if full else ""
        plan_1c = plan_end
        # ручная корректировка срока перекрывает «ДатуВывоза» из 1С: в 1С срок
        # часто не заполнен или заполнен по старой редакции договора. Пустая
        # дата в правке — осмысленна: «плана нет», снять ошибочный срок.
        edit = plan_dates.get(skey)
        plan_src, plan_note = ("1С" if plan_end else ""), ""
        if edit is not None:
            plan_full = edit["date"]
            plan_end = plan_full[:7]
            plan_src = "правка" if plan_end else "снят вручную"
            plan_note = edit["note"]
            if plan_end and (not plan_end_min or plan_end < plan_end_min):
                plan_end_min = plan_end

        def verdict(bought_, rest_, fact_end_):
            """Статус и отставание по паре «сколько привезли / сколько ещё лежит».
            Одна функция на оба варианта, чтобы правила не разъехались.
            Закрыт = вывозить нечего: остаток в пределах 1 % прихода или 0.5 т."""
            closed_ = rest_ <= max(0.5, 0.01 * max(bought_, 0.0))
            if not plan_end:
                st_ = "битая дата вывоза" if bad_dates else "без плана"
            elif closed_:
                st_ = "в срок" if (fact_end_ and fact_end_ <= plan_end) else "с опозданием"
            else:
                # срок сравниваем ПО ДНЮ: точная дата есть у каждого плана со
                # сроком (у 324 из 582 это 30-е или 31-е число). «Закрыт в срок
                # / с опозданием» выше остаётся помесячным — там сравнивается с
                # месяцем последнего расхода, дня у него нет.
                st_ = ("просрочен" if (plan_full or plan_end + "-01") < today_d
                       else "в работе")
            ref_end_ = fact_end_ if closed_ and fact_end_ else today
            return st_, ((_mn(ref_end_) - _mn(plan_end)) if plan_end else None)

        # узлы проекта в компактном виде: склад со своими признаками и месяцами.
        # «q» — класс единицы узла: диаграмма показывает узлы только выбранного
        # разреза, поэтому штуки никогда не попадут в столбик с тоннами.
        out_nodes = []
        wnodes = gantt_weeks.get(skey) or {}
        for (svar_, sklad, mcls_), months in nodes.items():
            mm = {mth: [round(x, 3) for x in v] for mth, v in sorted(months.items())
                  if any(abs(x) > 0.0005 for x in v)}
            if not mm:
                continue
            ww = {wk: [round(x, 3) for x in v]
                  for wk, v in sorted((wnodes.get((svar_, sklad, mcls_)) or {}).items())
                  if any(abs(x) > 0.0005 for x in v)}
            dv, un = sk_div.get(sklad, ("—", "—"))
            out_nodes.append({"v": svar_ or C.VAR_UNKNOWN, "w": sklad,
                              "s": sk_site.get(sklad, "—"), "d": dv, "u": un,
                              "q": mcls_, "m": mm, "wk": ww})
        if not out_nodes:
            continue

        def build(sites_, in_, out_, mcls_):
            """Свод варианта В ОДНОМ разрезе: помесячно приход/расход, итоги, статус."""
            ii = [GIX[x] for x in in_]
            oo = [GIX[x] for x in out_]
            mon = defaultdict(lambda: [0.0, 0.0, 0.0])
            won = defaultdict(lambda: [0.0, 0.0, 0.0])
            for nd in out_nodes:
                if nd["q"] != mcls_:
                    continue
                if sites_ and nd["s"] not in sites_:
                    continue
                for mth, v in nd["m"].items():
                    a = mon[mth]
                    a[0] += sum(v[i] for i in ii)
                    a[1] += sum(v[i] for i in oo)
                    a[2] += v[BIX]
                for wk_, v in (nd.get("wk") or {}).items():
                    a = won[wk_]
                    a[0] += sum(v[i] for i in ii)
                    a[1] += sum(v[i] for i in oo)
                    a[2] += v[BIX]
            ins_ = sorted(m for m, v in mon.items() if v[0] > 0.0005)
            outs_ = sorted(m for m, v in mon.items() if v[1] > 0.0005)
            if not ins_ and not outs_:
                return None
            bought_ = round(sum(v[0] for v in mon.values()), 3)
            sold_ = round(sum(v[1] for v in mon.values()), 3)
            # остаток — по ПОЛНОМУ обороту складов варианта, а не по его потокам
            rest_ = round(sum(v[2] for v in mon.values()), 3)
            fact_end_ = outs_[-1] if outs_ else ""
            st_, dl_ = verdict(bought_, rest_, fact_end_)
            return {"start": (ins_ or outs_)[0], "fact_end": fact_end_,
                    "bought": bought_, "sold": sold_, "rest": rest_,
                    "pct": round(100.0 * sold_ / bought_, 1) if bought_ > 0.0005 else None,
                    "status": st_, "delay": dl_,
                    # ⚠️ ОТБИРАЕМ ПО МОДУЛЮ, А НЕ «> 0». Приход бывает
                    # ОТРИЦАТЕЛЬНЫМ: возврат поставщику вычитается из «купили»
                    # (C.NEGATIVE_IN), и корзина вида [−67,901, 0] — это
                    # настоящий возврат, а не пустое место. Условие `v[0] > 0`
                    # такие корзины молча выбрасывало: по выгрузке 12.08.2026 —
                    # 387 корзин на −18 953,025 т, то есть возвраты просто
                    # пропадали с диаграммы.
                    # Нашлось при сверке недель с месяцами: неделя 29.06–05.07
                    # захватывает расход конца июня и фильтр проходит, а месяц
                    # «июль» с одним возвратом — нет. Отсюда и «недели не
                    # сходятся с месяцами»: виноваты были не недели.
                    "m": {mth: [round(v[0], 3), round(v[1], 3)]
                          for mth, v in sorted(mon.items())
                          if abs(v[0]) > 0.0005 or abs(v[1]) > 0.0005},
                    # недели за последние 12 месяцев — для мелкой шкалы Ганты
                    "w": {wk_: [round(v[0], 3), round(v[1], 3)]
                          for wk_, v in sorted(won.items())
                          if abs(v[0]) > 0.0005 or abs(v[1]) > 0.0005}}


        # вариант -> класс единицы -> свод. Пустые классы не пишем: снимок и так
        # 46 МБ, а у подавляющего большинства планов класс ровно один.
        node_cls = sorted({nd["q"] for nd in out_nodes})
        vs = {}
        for key, _t, _h, sites_, in_, out_ in C.GANTT_VARIANTS:
            per = {}
            for mcls_ in node_cls:
                r = build(sites_, in_, out_, mcls_)
                if r: per[mcls_] = r
            if per: vs[key] = per
        if not vs:
            continue
        # где заготовлено: тип склада, на который пришла закупка
        # (имя своё, не by_site — под этим именем в main уже живёт разрез продаж)
        buy_by_site = defaultdict(float)
        for nd in out_nodes:
            if nd["q"] != C.MEASURE_MAIN:
                continue
            buy_by_site[nd["s"]] += sum(v[GIX["куплено"]] for v in nd["m"].values())
        site_main = max(buy_by_site, key=buy_by_site.get) if buy_by_site else ""
        gantt.append({
            "series": skey,
            "name": (ref.get("name") or P.series_display(skey))[:120],
            "direction": direction_of.get(skey, ""),
            "contract": contract_disp.get(contract_of.get(skey, ("", ""))[1],
                                          contract_of.get(skey, ("", ""))[0]),
            "contragent": ref.get("contragent", ""),
            "plan_end": plan_end, "plan_end_min": plan_end_min,
            "plan_1c": plan_1c, "plan_full": plan_full,
            "plan_src": plan_src, "plan_note": plan_note,
            "site": site_main,
            "sites": {k: round(v, 3) for k, v in sorted(buy_by_site.items()) if v > 0.0005},
            "plans": len(dates), "bad_dates": bad_dates,
            "v": vs, "nodes": out_nodes,
        })
        # оплаты — свойство ПРОЕКТА, а не склада: в выгрузке платёж привязан к
        # бизнес-плану целиком, раскладывать его по складам не на чем, поэтому
        # значки оплат рисуются только на строке проекта, но не в разборе
        pr = pay_by.get(skey)
        if pr:
            gantt[-1]["pay"] = {
                "m": {mth: [round(v[0], 2), v[1]] for mth, v in sorted(pr["m"].items())},
                "t": round(sum(v[0] for v in pr["m"].values()), 2),
                "n": len(pr["d"]),
                "d": [{k: v for k, v in p.items() if k != "pk"} for p in pr["d"]],
            }
    # ---- НАШ ПЛАН ВЫВОЗА: сколько месяцев закладывали по этому БП ----
    # Связь с Битриксом идёт по НОМЕРУ бизнес-плана, другого общего ключа нет:
    # в выгрузке Битрикса нет ни ПроектГуида, ни строки серии 1С.
    # Номер берём двумя путями, и они разной надёжности:
    #   * ЯВНАЯ пометка «БП N» в имени (псевдо-серии договорных площадок
    #     «Площадка 21Y1425·БП14» и серии вида «… ДХНО БП 147») — надёжно;
    #   * ведущий номер строки серии (canon_key) — обычно это и есть номер БП.
    # ⚠️ Если номер ведёт в НЕСКОЛЬКО проектов, план ставим только когда у ВСЕХ
    # них номер помечен явно («БП N»): это один бизнес-план, разложенный по
    # разным договорным площадкам. Иначе не ставим вовсе — ровно случай номера
    # 197: одиннадцать проектов «197 … (Ферум тендер)», и в их именах написано
    # «Без БП», то есть 197 там номер договора, а не бизнес-плана.
    # отметки экономистов с вкладки «Версии БП»: крестик исключает версию,
    # галочка перекрывает «последнюю дату» (правило заказчика 25.08.2026)
    ver_choice = P.load_bp_version_choice(f.get("bp_ver_choice"))
    bp_plan, bp_stats = ({}, {})
    if f.get("bp_months"):
        bp_plan, bp_stats = P.load_bp_months(f["bp_months"], ver_choice)
        if ver_choice:
            print("  выбор версий экономистами: подтверждено %d БП, отклонено "
                  "%d версий" % (bp_stats["ver_forced"], bp_stats["ver_rejected"]))
        print("\nНаш план вывоза (Битрикс): блоков %d, номеров БП %d, "
              "с числом месяцев %d (спорных на одну дату %d), без номера %d"
              % (bp_stats["rows"], bp_stats["bps"], bp_stats["with_plan"],
                 bp_stats["ties"], bp_stats["no_num"]))
    RE_BP_MARK = re.compile(r"БП\s*([\d/,]+)")
    num_proj = defaultdict(set)        # номер БП -> {проект}
    num_expl = defaultdict(set)        # то же, но только по ЯВНОЙ пометке «БП N»
    for g in gantt:
        nm = g["name"]
        lead = P.canon_key(nm)
        if lead:
            for part in re.split(r"[/ ]", lead):
                if part.isdigit():
                    num_proj[part.lstrip("0") or part].add(g["series"])
        mk = RE_BP_MARK.search(nm)
        if mk:
            for part in re.split(r"[/,]", mk.group(1)):
                if part.isdigit():
                    k = part.lstrip("0") or part
                    num_proj[k].add(g["series"])
                    num_expl[k].add(g["series"])
    own_of = {}
    for num, sers in num_proj.items():
        pl = bp_plan.get(num)
        if not pl:
            continue
        if len(sers) > 1 and not sers <= num_expl.get(num, set()):
            continue                   # номер неоднозначен и не помечен явно
        for s in sers:
            own_of[s] = dict(pl, bp=num)
    own_ambig = sum(1 for num, sers in num_proj.items()
                    if bp_plan.get(num) and len(sers) > 1
                    and not sers <= num_expl.get(num, set()))
    for g in gantt:
        own = own_of.get(g["series"])
        if not own:
            continue
        # ЯКОРЬ (решение заказчика 07.08): месяц первой ОПЛАТЫ за лом; если
        # платежей в выгрузке нет — месяц первого прихода, то есть то самое
        # начало плановой линии, что уже нарисовано. Откуда взят якорь, видно
        # в подсказке: у 593 планов из 783 оплат в выгрузке нет вовсе.
        pm = min((g.get("pay") or {}).get("m") or [""]) or ""
        g["own"] = {"m": own["m"], "bp": own["bp"], "ver": own["ver"],
                    "rows": own["rows"], "alt": own["alt"],
                    "status": own["status"], "src": own["name"],
                    "pay": pm}
    # ---- ПЛАНЫ, ПО КОТОРЫМ ДВИЖЕНИЙ ЕЩЁ НЕТ ----
    # Их нет в `gantt`: он строится из узлов движений. Добавляем строку-заготовку
    # с нулями — у неё есть только наш план вывоза, и дорожка считается от
    # текущего месяца (третья ступень якоря, см. ownSpan в web/tabs.js).
    # ⚠️ Статус «без плана» — не описка: срока вывоза 1С у такого плана нет,
    # серии в 1С тоже нет. Врать «в работе» нельзя, вывозить пока нечего.
    plan_only = 0
    if bp_plan and C.GANTT_PLAN_ONLY_MONTHS != 0:
        have_bp = {str((g.get("own") or {}).get("bp")) for g in gantt
                   if (g.get("own") or {}).get("bp")}
        vlim = ""
        if C.GANTT_PLAN_ONLY_MONTHS:
            _i = M["period_max"].year * 12 + M["period_max"].month - 1 \
                 - (C.GANTT_PLAN_ONLY_MONTHS - 1)
            vlim = "%04d-%02d" % (_i // 12, _i % 12 + 1)
        vkeys = [k for k, *_ in C.GANTT_VARIANTS]
        for num, pl in bp_plan.items():
            if str(num) in have_bp:
                continue
            if vlim and str(pl.get("ver") or "") < vlim:
                continue               # старый план, движений по нему уже не будет
            gantt.append({
                "series": "план:%s" % num, "name": "БП %s · план вывоза" % num,
                "direction": "Прочее", "contract": "", "contragent": "",
                "plan_end": "", "plan_end_min": "", "plan_1c": "", "plan_full": "",
                "plan_src": "", "plan_note": "", "site": "—", "sites": {},
                "plans": 0, "bad_dates": 0, "nodes": [],
                "v": {vk: {C.MEASURE_MAIN: {
                        "start": "", "fact_end": "", "bought": 0.0, "sold": 0.0,
                        "rest": 0.0, "pct": None, "status": "без плана",
                        "delay": None, "m": {}, "w": {}}} for vk in vkeys},
                "own": {"m": pl["m"], "bp": num, "ver": pl["ver"],
                        "rows": pl["rows"], "alt": pl["alt"],
                        "status": pl["status"], "src": pl["name"], "pay": ""},
            })
            plan_only += 1
        if plan_only:
            print("  планов БЕЗ ДВИЖЕНИЙ добавлено на диаграмму: %d "
                  "(версия не старше %s)" % (plan_only, vlim or "любая"))

    # ---- ПЛАН ПО ОБЪЁМАМ ПРОИЗВОДСТВА -> НА ГАНТУ РАМКАМИ ----
    # Связь по НОМЕРУ БП в строке серии, тем же приёмом, что и с Битриксом.
    # ⚠️ Кладём план ТОЛЬКО тем строкам Ганты, у которых номер БП уже опознан
    # (`own.bp`): иначе рамка встанет на чужой проект с похожим номером, а
    # проверить это на диаграмме будет нечем.
    prod_plan, prod_base, prod_stats = ({}, {}, {})
    prod_fc, prod_m0, prod_horizon, prod_anchor = {}, "", [], ""
    if f.get("prodplan"):
        prod_plan, prod_base, prod_stats = P.load_prodplan(f["prodplan"])
        hit = 0
        for g in gantt:
            bp = str((g.get("own") or {}).get("bp") or "")
            if bp and bp in prod_plan:
                g["prod"] = prod_plan[bp]
                hit += 1
        print("\nПлан по объёмам производства: %d БП в выгрузке, %d легло на "
              "строки Ганты; месяцы %s … %s"
              % (prod_stats["bps"], hit,
                 (prod_stats["months"] or ["—"])[0], (prod_stats["months"] or ["—"])[-1]))
        print("  отброшено: без номера БП в серии %d строк, не тонны %d, "
              "прочие операции %d" % (prod_stats["no_bp"], prod_stats["no_unit"],
                                      prod_stats["no_op"]))
        prod_stats["matched"] = hit

        # ---- ПРОГНОЗ ОСТАТКА ПО БАЗАМ: ПЛАН СЪЕДАЕТ ОСТАТОК, ФИФО ПО БП ----
        # Заказано 14.08.2026. Смысл: остаток базы — не общая куча, а стопка
        # бизнес-планов, и плановая отгрузка снимает её СВЕРХУ, от самых ранних.
        #
        # ⚠️ НУЛЕВОЙ МЕСЯЦ. Выгрузка плана приходит около 16-го числа каждого
        # месяца; месяц, на который эта волна планирует, и есть НУЛЕВОЙ, от него
        # считаются три (заказчик 14.08.2026: «горизонт 3, нулевой там уже
        # есть»). Берём его из данных — первый период ПОСЛЕДНЕЙ волны
        # планирования, а не «текущий месяц по календарю»: иначе на старой
        # выгрузке горизонт уехал бы в пустоту и прогноз молча обнулился бы.
        # В волне 13.04.2026 это 2026-05, дальше 2026-06 и 2026-07.
        #
        # ⚠️ ОТСЧЁТ ОТ 1-го ЧИСЛА НУЛЕВОГО МЕСЯЦА, а не «от сегодня». План
        # месячный: чтобы вычесть его целиком, остаток надо взять на начало
        # месяца. Считаем его из наших же движений — накопленное сальдо узлов по
        # месяцы СТРОГО РАНЬШЕ нулевого. Следствие, о котором надо знать: то,
        # что уже уехало внутри нулевого месяца, в прогнозе не учтено — это
        # «план против плана», и заказчик выбрал именно так.
        waves = prod_stats.get("waves") or {}
        m0 = ""
        if waves:
            m0 = (waves[sorted(waves)[-1]] or [""])[0]
        if m0 and prod_base:
            def _madd(mon, k):
                n = int(mon[:4]) * 12 + int(mon[5:7]) - 1 + k
                return "%04d-%02d" % (n // 12, n % 12 + 1)
            horizon = [_madd(m0, i) for i in range(C.PRODPLAN_HORIZON)]
            # ---- ЯКОРЬ ПРОГНОЗА: НЕ РАНЬШЕ, ЧЕМ ДОЖИЛИ ДАННЫЕ ----
            # ⚠️ НАЙДЕНО ЗАКАЗЧИКОМ 20.08.2026 НА БП 1491: «остаток почти 3000
            # тонн, как будто где-то ошибка в расчётах». Прогноз считался от
            # 01.нулевого месяца (01.05) и вычитал ПЛАН, хотя движения дожили до
            # августа: май–июль уже ПРОЖИТЫ ФАКТОМ, базы реально вывезли ~2 400 т
            # по этому плану, а прогноз показывал 3 084 т «остатка» из прошлого.
            # План — это ожидание будущего; для прошедших месяцев есть факт, и
            # он всегда прав. Поэтому якорь прогноза — 1-е число ПОЗДНЕЙШЕГО из
            # двух месяцев: нулевого месяца плана и месяца последних движений, а
            # план вычитается только за месяцы горизонта, начиная с якоря.
            # На свежей выгрузке плана (m0 = текущий месяц) поведение прежнее,
            # решённое заказчиком 14.08: остаток на 01.m0 минус весь план.
            anchor_m = max(m0, today)
            fut = [mth for mth in horizon if mth >= anchor_m]
            prod_m0, prod_horizon, prod_anchor = m0, horizon, anchor_m

            # ---- НАШ ОСТАТОК ПО БАЗАМ НА 1-е ЧИСЛО НУЛЕВОГО МЕСЯЦА ----
            # Считаем по УЗЛАМ (проект · склад): ФИФО раскладывает план именно
            # по бизнес-планам, а не по базе целиком. Сальдо узла — полный
            # оборот склада (приход минус расход по всем потокам), как колонка
            # ОСТАТОК в дереве; накопление до нулевого месяца и есть остаток на
            # его 1-е число.
            # ⚠️ Подразделение берём из справочника 1С (`sk_unit`) и отрезаем
            # хвост «· старые склады»: в плане база называется «База Усинск», у
            # нас часть складов подписана «База Усинск · старые склады» — это та
            # же база, и разводить их значило бы потерять её остаток.
            def _base_of(w):
                u = (sk_unit.get(w) or "").split(" · ")[0].strip()
                return u if u.startswith(P.PRODPLAN_BASE_PREFIX) else ""
            bal = defaultdict(float)          # (база, проект) -> остаток на 01.якоря
            first_in = {}                     # проект -> месяц первого завоза
            for g in gantt:
                fi = ""
                for nd in g.get("nodes") or ():
                    if nd["q"] != C.MEASURE_MAIN:
                        continue
                    for mth, v in nd["m"].items():
                        if v[GIX["куплено"]] > 0.0005 and (not fi or mth < fi):
                            fi = mth
                    b = _base_of(nd["w"])
                    if not b:
                        continue
                    s = sum(v[BIX] for mth, v in nd["m"].items()
                            if mth < anchor_m)
                    if s > 0.0005:
                        bal[(b, g["series"])] += s
                # ⚠️ ФИФО СТРОИТСЯ ПО ПЕРВОМУ ЗАВОЗУ БП (решение заказчика
                # 14.08.2026), а не по сроку вывоза и не по дате плана: план
                # снимает с базы то, что легло раньше. Если завоза в выгрузке
                # нет вовсе (металл пришёл перемещением до начала периода),
                # берём первый месяц движений — иначе такой план встал бы в
                # конец очереди и никогда не попал бы под план.
                if not fi:
                    ms = [mth for nd in (g.get("nodes") or ())
                          if nd["q"] == C.MEASURE_MAIN for mth in nd["m"]]
                    fi = min(ms) if ms else "9999-99"
                first_in[g["series"]] = fi

            by_base = defaultdict(list)
            for (b, sk), v in bal.items():
                by_base[b].append(sk)
            alloc = defaultdict(dict)          # проект -> {месяц -> списано, т}
            for b, sks in by_base.items():
                plan_b = prod_base.get(b) or {}
                sks.sort(key=lambda s: (first_in.get(s, "9999-99"), s))
                rest = {s: bal[(b, s)] for s in sks}
                b0 = round(sum(rest.values()), 3)
                short = {}
                for mth in fut:
                    # ⚠️ ЗНАК: в выгрузке величина выбытия ПОЛОЖИТЕЛЬНА (смысл
                    # задаёт операция, а не знак), а отрицательное значение —
                    # это СНЯТИЕ ранее заведённого плана, и оно уже вычтено при
                    # накоплении. Поэтому итог ≤ 0 значит «выбытия по плану нет»,
                    # а не приход: списывать с базы в такой месяц нечего.
                    need = plan_b.get(mth, {}).get("out") or 0.0
                    if need <= 0.0005:
                        continue
                    for s in sks:
                        if need <= 0.0005:
                            break
                        take = min(rest[s], need)
                        if take > 0.0005:
                            alloc[s][mth] = round(alloc[s].get(mth, 0.0) + take, 3)
                            rest[s] -= take
                            need -= take
                    if need > 0.0005:
                        short[mth] = round(need, 3)
                prod_fc[b] = {
                    "b0": b0,
                    "out": {m: round(plan_b.get(m, {}).get("out") or 0.0, 3)
                            for m in horizon},
                    "in": {m: round(plan_b.get(m, {}).get("in") or 0.0, 3)
                           for m in horizon},
                    "mill": {m: round(plan_b.get(m, {}).get("mill") or 0.0, 3)
                             for m in horizon},
                    "left": round(sum(rest.values()), 3),
                    "short": short, "bps": len(sks),
                }
            # ⚠️ ОДИН БИЗНЕС-ПЛАН МОЖЕТ ЛЕЖАТЬ НА НЕСКОЛЬКИХ БАЗАХ. Очередь ФИФО
            # у каждой базы своя, поэтому списания складываются, а в подписи
            # строки стоит база с бо́льшим остатком и счёт остальных: писать одну
            # базу молча значило бы соврать, откуда уйдёт металл.
            per_bp = defaultdict(dict)
            for (b, sk), v in bal.items():
                per_bp[sk][b] = v
            for g in gantt:
                bb = per_bp.get(g["series"])
                if not bb:
                    continue
                a = alloc.get(g["series"]) or {}
                b0 = round(sum(bb.values()), 3)
                gone = round(sum(a.values()), 3)
                g["fc"] = {"b": max(bb, key=bb.get), "bn": len(bb), "b0": b0,
                           "m": a, "left": round(b0 - gone, 3)}
            cov = sum(1 for g in gantt if g.get("fc"))
            hatched = sum(1 for g in gantt if (g.get("fc") or {}).get("m"))
            print("  ПРОГНОЗ ПО БАЗАМ: нулевой месяц %s, горизонт %s, якорь %s; "
                  "баз %d, остаток на 01.%s %.0f т, план выбытия (незапрожитый) "
                  "%.0f т, прогноз %.0f т" %
                  (m0, ", ".join(horizon), anchor_m, len(prod_fc), anchor_m,
                   sum(v["b0"] for v in prod_fc.values()),
                   sum(sum(v["out"].get(mth, 0.0) for mth in fut)
                       for v in prod_fc.values()),
                   sum(v["left"] for v in prod_fc.values())))
            if anchor_m != m0:
                print("  ⚠️ выгрузка плана УСТАРЕЛА: плановые месяцы %s прожиты "
                      "фактом (движения по %s), их план не вычитается — факт "
                      "уже в остатке" %
                      (", ".join(mth for mth in horizon if mth < anchor_m),
                       today_d))
            print("  строк Ганты с прогнозом %d, из них план съедает остаток у %d"
                  % (cov, hatched))
            _sh = sum(sum(v["short"].values()) for v in prod_fc.values())
            if _sh > 0.5:
                print("  ⚠️ плана выбытия больше, чем остатка: %.0f т списать "
                      "не с чего (базы: %s)"
                      % (_sh, ", ".join(sorted(b for b, v in prod_fc.items()
                                               if v["short"]))))
            if prod_stats.get("off_base"):
                print("  мимо баз (строка стоит за цехом/заготовкой, получателя "
                      "в выгрузке нет): %s"
                      % ", ".join("%s %.0f т" % (k, v)
                                  for k, v in prod_stats["off_base"].items()))
        elif prod_base:
            print("  ⚠️ ПРОГНОЗ ПО БАЗАМ НЕ ПОСТРОЕН: в выгрузке нет даты "
                  "планирования, от которой считается нулевой месяц")

    # ---- КОММЕНТАРИИ ПО ЗАПАСАМ -> НА СТРОКУ ГАНТЫ ----
    # Заказано 20.08.2026. Связь по ГУИДУ СЕРИИ через справочник серий: строку
    # серии в этой выгрузке пишут по-своему, и совпадение по тексту теряет
    # четверть планов, а гуид — нет (см. P.load_stock_notes).
    notes, note_stats = ({}, {})
    if f.get("stock_notes"):
        notes, note_stats = P.load_stock_notes(f["stock_notes"], SG2P)
        hit = 0
        for g in gantt:
            cs = notes.get(g["series"])
            if cs:
                g["cm"] = cs
                hit += 1
        note_stats["matched"] = hit
        print("\nКомментарии по запасам: %d строк в выгрузке, с текстом %d, "
              "легли на %d строк Ганты (бизнес-планов в выгрузке %d)"
              % (note_stats["rows"], note_stats["kept"], hit, note_stats["bps"]))
        print("  срезов %d: %s … %s; выгружено %s; без текста %d строк, "
              "серия не опознана %d"
              % (len(note_stats["dates"]), (note_stats["dates"] or ["—"])[0],
                 (note_stats["dates"] or ["—"])[-1], note_stats["dump"] or "—",
                 note_stats["no_text"], note_stats["no_series"]))

    # ---- ВЕРСИИ РАСЧЁТОВ БП -> ВКЛАДКА «ВЕРСИИ БП» ----
    bpver_rows, bpver_stats = ([], {})
    if f.get("bp_versions"):
        bpver_rows, bpver_stats = P.load_bp_versions(f["bp_versions"], ver_choice)
        # ⚠️ ПОЗИЦИИ ОСТАВЛЯЕМ ТОЛЬКО ТАМ, ГДЕ ИХ ПОКАЗЫВАЮТ. Вкладка «Версии
        # БП» показывает выручку и месяцы, а сами позиции нужны «Закупке по БП»
        # — и только у используемой версии трека. Держать 42 тысячи строк в
        # обоих местах значит раздуть снимок с 66 до 80 МБ на ровном месте
        # (замерено): позиции переезжают в bpbuy ниже, а здесь чистятся.
        print("\nВерсии расчётов БП: %d версий по %d БП (выгрузка %s); "
              "подтверждено %d, отклонено %d, с выручкой %d; "
              "с плановыми позициями %d (%d строк)"
              % (bpver_stats["rows"], bpver_stats["bps"],
                 bpver_stats["generated"] or "—", bpver_stats["chosen"],
                 bpver_stats["rejected"], bpver_stats["with_rev"],
                 bpver_stats.get("with_items", 0), bpver_stats.get("items", 0)))

    # ---- ЗАКУПКА ПО БИЗНЕС-ПЛАНУ: план из расчёта против факта 1С ----
    # Заказ финдира (запись 01.09.2026): «делаешь вкладку чисто по поводу
    # закупки и там сравниваешь: что мы собирались купить по Excel и как мы это
    # приходовали здесь; туда же плановая выручка. Сколько было в Excel трубы
    # 73 и на какую сумму, сколько в результате купили».
    #
    # ⚠️ ТОЛЬКО ГОЛОВНАЯ СЕРИЯ (без «V» и «VS») — прямое правило заказчика:
    # «что написано в Excel, то должно пройти по головной серии». Серии V/VS —
    # это довозы и пересорт того же проекта, их с плановым расчётом не сверяют.
    #
    # ⚠️ СУММ ЗАКУПКИ В ВЫГРУЗКЕ 1С НЕТ. В «СебестоимостьТоваровОбороты» только
    # количества (КоличествоПриход / Расход / Оборот) — «на какую сумму купили»
    # посчитать нечем, нужна отдельная колонка из 1С. Поэтому здесь тоннаж, а
    # место под суммы и цены реализации оставлено в структуре.
    # доли лота и юрлицо по БП (выгрузка сделок Битрикса, 14.09.2026)
    deals, deal_stats = ({}, {})
    if f.get("deals"):
        deals, deal_stats = P.load_deals(f["deals"])
        print("\nСделки Битрикса: %d строк, с номером БП %d, с долей %d (%s)"
              % (deal_stats["rows"], deal_stats["with_no"], deal_stats["with_share"],
                 os.path.basename(f["deals"])))
    bpbuy = []
    head_buy = defaultdict(lambda: defaultdict(float))     # проект -> (код, склад) -> т
    tail_t = 0.0
    for (skey_, svar_, _st, sk_, code_), v in buys.items():
        if not code_ or abs(v) <= 0.0005:
            continue
        if not P.is_head_series(svar_):
            tail_t += v                                     # серии V/VS — мимо
            continue
        head_buy[skey_][(code_, sk_)] += v
    # продано по тем же ключам — задел под «сколько выручки с трубы 73»
    head_sold = defaultdict(lambda: defaultdict(float))
    # ФАКТ ВЫРУЧКИ по тем же ключам, ₽ без НДС — из регистра выручки (14.09.2026).
    # ⚠️ Рубли берём по ВСЕМ единицам, не только по тоннам: трансформатор в
    # штуках выручку даёт такую же настоящую, как труба в тоннах.
    head_rub = defaultdict(lambda: defaultdict(float))
    head_cost = defaultdict(float)             # себестоимость продаж по проекту, ₽
    for it in out_items:
        if not P.is_head_series(it.get("series_variant")):
            continue
        head_rub[it["series"]][(it.get("code") or "", it.get("sklad") or "")] \
            += float(it.get("rub") or 0)
        head_cost[it["series"]] += float(it.get("cost") or 0)
        if it.get("mc") != C.MEASURE_MAIN:
            continue
        head_sold[it["series"]][(it.get("code") or "", it.get("sklad") or "")] \
            += float(it.get("tonnes") or 0)
    # плановая выручка версии — по трекам (лук / дсп), из вкладки «Версии БП»
    rev_by_bp = defaultdict(dict)
    for r in bpver_rows:
        if not r["bp"] or r["revenue"] is None:
            continue
        chosen = r["choice"] == "ok"
        for tr in (r["dflt"].split("+") if r["dflt"] else []) + \
                  (["luk"] if chosen and "лук" in (r["sheet"] or "").lower() else []) + \
                  (["dsp"] if chosen and "дсп" in (r["sheet"] or "").lower() else []):
            if not tr:
                continue
            cur = rev_by_bp[r["bp"]].get(tr)
            # выбор экономиста всегда побеждает умолчание
            if cur is None or (chosen and not cur.get("ok")):
                rev_by_bp[r["bp"]][tr] = {
                    "rev": r["revenue"], "ver": r["name"], "ok": chosen,
                    # плановые позиции этой версии — их и сравнивают с фактом
                    "items": (r.get("items") or [])[:60],
                    "plan_t": r.get("plan_t") or 0,
                    "plan_sum": r.get("plan_sum") or 0,
                }
    for _r in bpver_rows:                       # см. ⚠️ выше: чистим после сбора
        _r.pop("items", None)
    name_by_series = {g["series"]: g for g in gantt}
    for skey_, cells in head_buy.items():
        g = name_by_series.get(skey_) or {}
        bp_no = str((g.get("own") or {}).get("bp") or "")
        # ⚠️ НОМЕР БП — ИЗ ИМЕНИ СЕРИИ, ЕСЛИ БИТРИКС ЕГО НЕ ДАЛ (заказчик
        # 14.09.2026: «почему более 300 БП без плана»). `own.bp` есть только у
        # проектов из «Проверки БП» Битрикса; у 59 проектов на 30 тыс. т номер
        # стоял прямо в имени («545 (21/136 от 18.11.22…)», «344/345 (22V0590…)»),
        # а план не подтягивался. Правило то же, что у canon_key и у bpNumber в
        # оболочке: ведущая группа из 2–4 цифр, через «/» — несколько номеров,
        # берём тот, у которого есть версия расчёта, иначе первый.
        if not bp_no:
            _nm = g.get("name") or P.series_display(skey_) or ""
            _m = re.match(r"^\s*(?:БП\s*)?(\d{2,4}(?:\s*/\s*\d{2,4})*)(?!\d)", str(_nm))
            if _m:
                _nums = re.findall(r"\d+", _m.group(1))
                bp_no = next((n_ for n_ in _nums if rev_by_bp.get(n_)), _nums[0])
        by_code = defaultdict(lambda: {"t": 0.0, "s": 0.0, "r": 0.0, "w": defaultdict(float)})
        for (code_, sk_), v in cells.items():
            a = by_code[code_]
            a["t"] += v
            a["w"][sk_] += v
        for (code_, sk_), v in (head_sold.get(skey_) or {}).items():
            by_code[code_]["s"] += v
        for (code_, sk_), v in (head_rub.get(skey_) or {}).items():
            by_code[code_]["r"] += v
        nm_rows = []
        for code_, a in sorted(by_code.items(), key=lambda x: -x[1]["t"]):
            if abs(a["t"]) <= 0.0005 and abs(a["s"]) <= 0.0005 and abs(a["r"]) <= 0.5:
                continue
            nm_rows.append({
                "c": code_, "n": name_of.get(code_, code_),
                "t": round(a["t"], 3), "s": round(a["s"], 3),
                "r": round(a["r"], 2),                      # факт выручки, ₽
                "w": [[w_, round(t_, 3)] for w_, t_ in
                      sorted(a["w"].items(), key=lambda x: -x[1]) if abs(t_) > 0.0005],
            })
        if not nm_rows:
            continue
        rec = {"s": skey_, "bp": bp_no,
               "name": (g.get("name") or P.series_display(skey_))[:120],
               "ca": g.get("contragent", ""), "dir": g.get("direction", ""),
               "t": round(sum(x["t"] for x in nm_rows), 3),
               "sold": round(sum(x["s"] for x in nm_rows), 3),
               # факт выручки по головной серии, ₽ без НДС (все единицы)
               "rub": round(sum(x["r"] for x in nm_rows), 2),
               # себестоимость продаж по головной серии, ₽ — для маржи
               "cost": round(head_cost.get(skey_, 0.0), 2),
               "nm": nm_rows[:60]}
        # ⚠️ СТАРЫЕ БП — ОТДЕЛЬНО (заказчик 14.09.2026: «площадки и серии без
        # номера БП скрой, это старые БП, они уже не актуальны — сделай их во
        # вкладке „старые БП“ и не учитывай в общем счётчике»). Псевдо-серии
        # договорных площадок («Площадка 46/2021»), серии без номера БП
        # («Камасталь», «Сибур-Химпром без БП») и БЕЗ СЕРИИ — в 1С у них
        # настоящей серии либо номера бизнес-плана нет, версию расчёта
        # привязать некуда. Признак пишет расчёт, оболочка только прячет.
        if not bp_no:
            rec["old"] = ("без серии" if skey_ == "БЕЗ СЕРИИ" else
                          "площадка" if str(skey_).startswith("площадка:") else
                          "без номера БП")
        if bp_no and deals.get(bp_no):
            rec["deal"] = deals[bp_no]
        if bp_no and rev_by_bp.get(bp_no):
            # ПЛАН ПРОТИВ ФАКТА В ОДНОЙ СТРОКЕ. Плановые позиции Excel
            # раскладываются по нашей НМК (P.match_plan_items): «mk» — код НМК
            # для каждой плановой строки, «mt» — сколько тонн и рублей плана
            # легло на каждую нашу позицию, «ug»/«un» — что разнести нельзя
            # («металлолом» без марки и вовсе не опознанное). Считается на
            # ТЕХ ЖЕ позициях, что показываются, иначе итог таблицы разойдётся
            # с итогом колонки.
            rec["rev"] = {}
            for tr, d in rev_by_bp[bp_no].items():
                its = d.get("items") or []
                # кандидаты — ровно те строки, что видны в таблице (nm_rows[:60]),
                # иначе план ляжет на НМК, которой на экране нет
                marks, buck = P.match_plan_items(its, rec["nm"])
                rec["rev"][tr] = {
                    "v": d["rev"], "ver": d["ver"], "ok": d["ok"],
                    "pt": d.get("plan_t") or 0, "ps": d.get("plan_sum") or 0,
                    "it": its, "mk": marks,
                    "mt": buck["mt"], "ug": buck["ug"], "un": buck["un"],
                    # план по нашим складам (место хранения Excel -> наш склад)
                    "mw": buck["mw"], "mp": buck["mp"],
                }
        bpbuy.append(rec)
    bpbuy.sort(key=lambda r: -r["t"])
    print("\nЗакупка по бизнес-плану (головная серия): %d проектов, %.0f т; "
          "мимо прошли серии V/VS на %.0f т; с плановой выручкой %d проектов; "
          "старых (без номера БП) %d на %.0f т"
          % (len(bpbuy), sum(r["t"] for r in bpbuy), tail_t,
             sum(1 for r in bpbuy if r.get("rev")),
             sum(1 for r in bpbuy if r.get("old")),
             sum(r["t"] for r in bpbuy if r.get("old"))))

    # ---- ПУЛЬС НЕДЕЛИ: слепок для сравнения «что изменилось с прошлого раза» ----
    # Заказ финдира (запись 02.09.2026): «неделю назад было другое состояние
    # дашборда, и разница показывает, что сделали за неделю — тогда это станет
    # дашбордом для оперативки. Был объём непросроченный, а на этой неделе он
    # уже просроченный. И: сколько остатков перейдёт в просроченные на
    # следующей неделе, если не вывезем».
    #
    # ⚠️ СЛЕПОК ПИШЕТСЯ РЯДОМ С ДАННЫМИ, А НЕ ХРАНИТСЯ В СНИМКЕ ЦЕЛИКОМ:
    # сравнивать надо с ПРОШЛОЙ неделей, а снимок пересобирается каждую ночь.
    # Поэтому расчёт кладёт компактный пульс в data/pulse/ (около 60 КБ) и
    # берёт оттуда ближайший к «неделя назад» для сравнения.
    def _band_of(status_, delay_):
        if status_ in ("без плана", "битая дата вывоза"):
            return "nop"
        if status_ == "в работе":
            return "work"
        if status_ != "просрочен":
            return None                       # закрытые в пульс не идут
        d_ = delay_ or 0
        return ("b0" if d_ <= 3 else "b3" if d_ <= 6 else
                "b6" if d_ <= 9 else "b9" if d_ <= 12 else "b12")

    pulse_now = {"date": datetime.now().strftime("%Y-%m-%d"),
                 "period_max": M["period_max"].strftime("%Y-%m-%d"), "bp": {}}
    for g in gantt:
        rec = {}
        for vk_ in ("v1", "v2"):
            t_ = (g.get("v") or {}).get(vk_, {}).get(C.MEASURE_MAIN)
            if not t_:
                continue
            # закрытые пропускаем теми же правилами, что интерфейс (mClosed)
            if t_["rest"] <= max(0.5, 0.01 * max(t_["bought"], 0)):
                continue
            b_ = _band_of(t_["status"], t_["delay"])
            if not b_:
                continue
            rec[vk_] = [round(t_["rest"], 3), b_, t_["delay"]]
        if rec:
            pulse_now["bp"][g["series"]] = rec
    PULSE_DIR = os.path.join(DATA, "pulse")
    os.makedirs(PULSE_DIR, exist_ok=True)
    _pf = os.path.join(PULSE_DIR, "pulse_%s.json" % pulse_now["date"])
    with open(_pf + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(pulse_now, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(_pf + ".tmp", _pf)
    # ⚠️ ХРАНИМ 60 ПОСЛЕДНИХ СЛЕПКОВ: сравнение недельное, но при пропущенных
    # сборках (сервер лежал, выгрузка не пришла) ближайший подходящий может
    # оказаться и десятидневной давности — запас нужен.
    _all_p = sorted(glob.glob(os.path.join(PULSE_DIR, "pulse_*.json")))
    for old_p in _all_p[:-60]:
        try:
            os.remove(old_p)
        except OSError:
            pass
    # ---- СЛЕПКИ ДЛЯ СРАВНЕНИЯ: ПО ПОНЕДЕЛЬНИКАМ ----
    # ⚠️ УТОЧНЕНИЕ ЗАКАЗЧИКА 02.09.2026: нужен не просто «дельта за неделю», а
    # ВТОРОЙ ДАШБОРД — «результат данных на прошлый понедельник», рядом с
    # сегодняшним, чтобы глазами сравнить картину. Поэтому в снимок кладём не
    # один слепок, а НЕСКОЛЬКО ПОНЕДЕЛЬНИЧНЫХ: оперативка идёт по понедельникам,
    # и сравнивать надо состояние на день совещания, а не «семь дней назад от
    # случайной даты сборки».
    # На каждый из последних PULSE_WEEKS понедельников берём ближайший слепок в
    # пределах ±3 дней (сборка могла не пройти ровно в понедельник).
    PULSE_WEEKS = 8
    _today_d0 = datetime.strptime(pulse_now["date"], "%Y-%m-%d")
    _by_date = {}
    for pth in _all_p:
        d_ = os.path.basename(pth)[6:16]
        try:
            _by_date[datetime.strptime(d_, "%Y-%m-%d")] = pth
        except ValueError:
            pass
    # последний ПРОШЕДШИЙ понедельник (если сегодня понедельник — берём прошлый)
    _mon = _today_d0 - timedelta(days=(_today_d0.weekday() or 7))
    pulses, prev_date, prev_age = {}, "", 0
    for k in range(PULSE_WEEKS):
        want = _mon - timedelta(days=7 * k)
        best = None
        for dt_, pth in _by_date.items():
            # ⚠️ СЕГОДНЯШНИЙ СЛЕПОК В КАНДИДАТЫ НЕ БЕРЁМ: он попадает в окно ±3
            # дня от прошлого понедельника (в среду это два дня), и дашборд
            # начинал сравнивать состояние сам с собой — все дельты по нулям.
            if dt_ >= _today_d0:
                continue
            gap = abs((dt_ - want).days)
            if gap <= 3 and (best is None or gap < best[0]):
                best = (gap, dt_, pth)
        if not best:
            continue
        key = best[1].strftime("%Y-%m-%d")
        if key in pulses:
            continue
        try:
            pulses[key] = json.load(open(best[2], encoding="utf-8")).get("bp") or {}
        except Exception:
            continue
        if not prev_date:                      # ближайший прошедший понедельник
            prev_date = key
            prev_age = (_today_d0 - best[1]).days
    # ⚠️ ЗАПАСНОЙ ВАРИАНТ: пока понедельничных слепков не накопилось (механизм
    # только запущен), берём любые прошлые даты — иначе сравнение не работало бы
    # неделями, и заказчик решил бы, что функции нет. Как только появятся
    # понедельники, они вытеснят эти даты сами.
    if not pulses:
        for dt_ in sorted(_by_date, reverse=True):
            if dt_ >= _today_d0:
                continue
            key = dt_.strftime("%Y-%m-%d")
            try:
                pulses[key] = json.load(open(_by_date[dt_], encoding="utf-8")).get("bp") or {}
            except Exception:
                continue
            if not prev_date:
                prev_date, prev_age = key, (_today_d0 - dt_).days
            if len(pulses) >= 4:
                break
    print("\nПульс недели: слепок %s (%d планов); понедельничных слепков для "
          "сравнения %d%s"
          % (pulse_now["date"], len(pulse_now["bp"]), len(pulses),
             (" (ближайший %s, %d дн. назад)" % (prev_date, prev_age))
             if prev_date else " — прошлых нет"))

    own_matched = sum(1 for g in gantt if g.get("own"))
    if bp_plan:
        print("  бизнес-планов с нашим планом вывоза: %d из %d в Ганте "
              "(с оплатой как якорем %d, по первому приходу %d); "
              "номеров пропущено из-за неоднозначности %d"
              % (own_matched, len(gantt),
                 sum(1 for g in gantt if (g.get("own") or {}).get("pay")),
                 sum(1 for g in gantt if g.get("own") and not g["own"]["pay"]),
                 own_ambig))

    # ---- ПЛАТЕЖИ ПО ДОГОВОРУ, У КОТОРЫХ БП В 1С НЕ ПРОСТАВЛЕН ----
    # Приписывать их плану нельзя — под одним договором лежат десятки проектов
    # (у 23Y0618 это ОДИННАДЦАТЬ планов). Но и молчать нельзя: экономист видит на
    # Ганте одну точку и решает, что расчёт потерял оплаты. Показываем счётчик на
    # строке плана — «в 1С у N платежей по этому договору не проставлен БП».
    _gap = (pay_stats or {}).get("gap_by_contract") or {}
    _gap_hit = 0
    for g in gantt:
        dg = P.contract_no("№" + (g.get("contract") or "")) if g.get("contract") else ""
        hit = _gap.get(dg)
        if hit:
            g["pay_gap"] = hit
            _gap_hit += 1
    if _gap:
        print("  платежей без БП: %d договоров, из них узнаны на строках Ганты: %d"
              % (len(_gap), _gap_hit))

    # сколько бизнес-планов из файла оплат нашлись в этом отчёте: остальные —
    # планы, по которым закупок и продаж ещё не было, значков у них не будет
    pay_projects = len({p["pk"] for p in pay_rows})
    pay_matched = sum(1 for g in gantt if g.get("pay"))
    if pay_rows:
        print("  бизнес-планов с оплатами: %d из %d в файле" % (pay_matched, pay_projects))
    # сортировка и сводная статистика — по ОСНОВНОМУ разрезу (тонны): это
    # опорные цифры §3, они не должны поехать от появления штук и метров.
    MM = C.MEASURE_MAIN
    def _mainv(g, key=None):
        for k in ([key] if key else ["v2", "v1"]):
            per = g["v"].get(k) or {}
            if MM in per:
                return per[MM]
            if per and not key:
                return next(iter(per.values()))
        return None
    gantt.sort(key=lambda g: (g["plan_end"] or "9999",
                              -((_mainv(g) or {}).get("bought") or 0.0)))
    gantt_status = {k: dict(Counter(_mainv(g, k)["status"] for g in gantt
                                    if _mainv(g, k)))
                    for k, *_ in C.GANTT_VARIANTS}

    # какие разрезы по единице реально есть в этой выгрузке
    m_rows, m_qty, m_bps = Counter(), defaultdict(float), defaultdict(set)
    MCI = 13                                   # индекс колонки «mc» в moves
    MQI = 15                                   # величина в единице класса
    for mv in moves:
        mk = _STR[mv[MCI]]
        m_rows[mk] += 1
        # для невесовых разрезов берём величину В ЕДИНИЦЕ КЛАССА (метры уже
        # переведены в километры), иначе счётчик показывал 315 113 «километров»
        m_qty[mk] += mv[MQI] if mk != C.MEASURE_MAIN else mv[5]
        m_bps[mk].add(mv[8])
    measures_out = [{"key": k, "title": P.measure_title(k), "rows": n,
                     "qty": round(m_qty[k], 3), "bps": len(m_bps[k])}
                    for k, n in m_rows.most_common()]
    measures_out.sort(key=lambda x: (x["key"] != C.MEASURE_MAIN, -x["bps"]))
    print("\nРазрезы по единице измерения:")
    for m in measures_out:
        print("  %-18s %7d строк · %16.3f · %4d БП"
              % (m["title"], m["rows"], m["qty"], m["bps"]))
    print("\nДиаграмма Ганта — узлов (проект·вариант серии·склад): %d, "
          "бизнес-планов %d" % (sum(len(g["nodes"]) for g in gantt), len(gantt)))
    for key, title, _h, *_ in C.GANTT_VARIANTS:
        rows = [g for g in gantt if _mainv(g, key)]
        print("  %-18s %4d БП · %s · к вывозу %.3f т"
              % (title, len(rows),
                 ", ".join(f"{k} {v}" for k, v in
                           Counter(_mainv(g, key)["status"] for g in rows).most_common()),
                 sum(_mainv(g, key)["rest"] for g in rows
                     if _mainv(g, key)["rest"] > 0)))

    # Остаток «сколько ещё можно продать» по регистру: приход − расход.
    # Той же формулой считается колонка ОСТАТОК на каждом уровне дерева.
    balance_moves_1c = round(sum(flow_totals.get(k, 0.0) for k in C.BALANCE_1C_IN)
                             - sum(flow_totals.get(k, 0.0) for k in C.BALANCE_1C_OUT), 3)
    balance_moves = round(sum(flow_totals.get(k, 0.0) for k in C.BALANCE_IN)
                          - sum(flow_totals.get(k, 0.0) for k in C.BALANCE_OUT), 3)

    # ---------------- ПОСТОЯННЫЕ АВТОПРОВЕРКИ (§11 ТЗ) ----------------
    # Каждая проверка — вопрос, на который отчёт обязан отвечать сам, без
    # ручной сверки. Ничего не «замазываем»: если проверка не проходит,
    # она так и остаётся красной и с цифрой.
    mix_by_badge = Counter()
    for rid, parts_ in M["mix_alloc"].items():
        mix_by_badge["деление" if len(parts_) > 1 else "точное/ёмкость"] += 1
    no_valid_t = round(sum(i["tonnes"] for i in out_items
                           if i["badge"] in ("нет", "вероятно")), 3)
    no_valid_n = sum(1 for i in out_items if i["badge"] in ("нет", "вероятно"))
    undelivered = flow_totals.get("недоезд", 0.0)
    move_scale = max(abs(flow_totals.get("уехало", 0.0)), 1.0)
    rework_gap = [r for r in flows_tbl
                  if abs(r.get("переработка_забрали", 0)
                         - r.get("переработка_вернули", 0)) > 0.002]
    autochecks = [
        {"name": "Обратный ход: остаток на дату = факт + расход после − приход после",
         "status": "ok" if bad == 0 else "warn",
         "value": f"{bad} из {n_covered} ключей расходится",
         "note": "Якорь — факт на %s. Файл остатков покрывает %d складов из %d, "
                 "сверяется только пересечение; остальное помечено «нет якоря» и "
                 "расхождением НЕ считается."
                 % (stock_date.strftime("%d.%m.%Y") if stock_date else "—",
                    len(stock_sklady), len(set(s for s, _ in M["balance"])))},
        {"name": "Продано > поступило (серия в продаже есть, прихода нет)",
         "status": "ok" if not oversold else "warn",
         "value": f"{len(oversold)} проектов",
         "note": "Серия стоит в документах реализации 1С, а прихода под ней в "
                 "регистре нет: куплено под другой серией или в другом учёте. "
                 "Это вопрос к 1С, а не ошибка расчёта."},
        {"name": "Уехало ≠ приехало по серии (серия сменилась в пути)",
         "status": "ok" if not movegap else "warn",
         "value": f"{len(movegap)} проектов",
         "note": "Перемещение внутреннее: расход со склада-отправителя должен "
                 "совпадать с приходом на склад-получатель. Разрыв по серии "
                 "означает, что в пути сменилась серия."},
        {"name": "В производство = из производства по каждому бизнес-плану",
         "status": "ok" if not rework_gap else "bad",
         "value": ("все БП сходятся; отдельно: расход без весового выхода "
                   f"{flow_totals.get('переработка_расход_без_пары', 0):,.3f} т · "
                   "приход без весового входа "
                   f"{flow_totals.get('переработка_приход_без_пары', 0):,.3f} т")
                  .replace(",", " "),
         "note": "Смешанный документ распределяется между БП по доле тоннажа "
                 "сырья. Совпадающая масса образует пару передела (резка, "
                 "разборка, прочее производство). Строки, где "
                 "одна сторона документа не имеет веса (например, тонны ↔ "
                 "штуки), вынесены отдельно как диагностика и остаток БП не "
                 "увеличивают и не уменьшают."},
        {"name": "Недоезд по всей компании ≈ 0 (контроль модели)",
         "status": "ok" if abs(undelivered) / move_scale < 0.01 else "bad",
         "value": f"{undelivered:,.3f} т при перемещениях {flow_totals.get('уехало', 0):,.3f} т"
                  .replace(",", " "),
         "note": "Перемещения перекладывают тот же металл. Если сумма «уехало» и "
                 "«приехало» по компании не гасится — ошибка в группировке."},
        {"name": "Сумма позиций = прямой пересчёт по сырым строкам",
         "status": "ok" if abs(positions_exact - direct_tonnes) < 0.001 else "bad",
         "value": f"позиции {positions_exact:,.3f} т · строки {direct_tonnes:,.3f} т · Δ "
                  f"{positions_exact - direct_tonnes:+.6f} т".replace(",", " "),
         "note": "Контроль арифметики: тоннаж считается ПОСТРОЧНО (у одного кода "
                 "в разных строках бывает разная единица), агрегат не должен "
                 "расходиться с пересчётом по строкам. Сверяются суммы ДО "
                 "округления; публикация с 3 знаками по %d позициям даёт "
                 "%+.3f т — это округление вывода, а не расхождение расчёта."
                 % (len(out_items), total_t - positions_exact)},
        {"name": "Оплаты за лом привязаны к бизнес-плану",
         "status": "ok" if not pay_rows else "warn",
         "value": ("файл оплат не найден" if not pay_rows else
                   "%d из %d строк с БП · %s ₽ из %s ₽ · опознано планов %d из %d"
                   % (pay_stats["with_bp"], pay_stats["rows"],
                      f"{pay_stats['sum_bp']:,.2f}".replace(",", " "),
                      f"{pay_stats['sum_all']:,.2f}".replace(",", " "),
                      pay_matched, pay_projects)),
         "note": "Платёж относится к плану, только если в заявке на расходование "
                 "проставлен бизнес-план: в выгрузке он есть не везде. Привязка "
                 "по договору невозможна — под одним договором лежат десятки "
                 "проектов. Дата берётся из колонки «ДатаПлатежа»; если она "
                 "пуста — из имени регистратора (в выгрузке 05.08.2026 она была "
                 "пуста везде, и у 72 платежей дата документа отличалась от даты "
                 "платежа, у 11 из них — месяцем). "
                 "Планы, которых нет в этом отчёте (закупок и продаж по ним ещё "
                 "не было), значков не получают."},
        {"name": "Наш план вывоза (Битрикс) привязан к бизнес-плану",
         "status": "ok" if not bp_plan else "warn",
         "value": ("файл «Проверка_БП_*.xlsx» не найден" if not bp_plan else
                   "%d из %d планов Ганты · якорь: оплата %d, первое движение %d, "
                   "текущий месяц %d (движений нет) · "
                   "в файле номеров БП %d, спорных на одну дату %d, "
                   "номеров пропущено как неоднозначные %d"
                   % (own_matched, len(gantt),
                      sum(1 for g in gantt if (g.get("own") or {}).get("pay")),
                      sum(1 for g in gantt if g.get("own") and not g["own"]["pay"]
                          and g.get("nodes")),
                      sum(1 for g in gantt if g.get("own") and not g["own"]["pay"]
                          and not g.get("nodes")),
                      bp_stats.get("bps", 0), bp_stats.get("ties", 0), own_ambig)),
         "note": "Связь с Битриксом идёт по НОМЕРУ бизнес-плана — другого общего "
                 "ключа нет: в выгрузке Битрикса не бывает ни ПроектГуида, ни "
                 "строки серии 1С. Берётся колонка «Кол-во месяцев ПО СТРОКАМ» "
                 "того блока, у которого ПОЗДНЕЙШАЯ дата версии (справа от «|»: "
                 "слева стоит дата самого БП, она у всех версий одна). Если на эту "
                 "дату приходится несколько разных значений, предпочитается статус "
                 "«ОК», остальные показаны в подсказке дорожки. Если номер ведёт в "
                 "несколько проектов и не у всех помечен явно («БП N»), план не "
                 "ставится вовсе — так номер 197 не растащило по одиннадцати "
                 "проектам «Ферум тендер», где в именах написано «Без БП». "
                 "Планы, которых нет в этом отчёте, дорожки не получают."},
        {"name": "Смешанные лоты разложены без дробных хвостов",
         "status": "ok" if not mix_by_badge.get("деление") else "warn",
         "value": "%d строк по точному количеству / ёмкости, %d делением"
                  % (mix_by_badge.get("точное/ёмкость", 0), mix_by_badge.get("деление", 0)),
         "note": "Сначала точное совпадение количества, затем аллокация по "
                 "остатку ёмкости (FIFO по дате). Строки, которые иначе не "
                 "разложить, помечены бейджем «доля» и вынесены в «Проблемы»."},
        {"name": "Остаток по движениям vs факт 1С",
         "status": "warn" if abs(balance_moves_1c - flow_totals.get("остаток", 0)) > 1 else "ok",
         "value": (f"складской (как в 1С) {balance_moves_1c:,.3f} т · "
                   f"факт 1С {flow_totals.get('остаток', 0):,.3f} т · "
                   f"проектный {balance_moves:,.3f} т").replace(",", " "),
         "note": "Сверяются СОПОСТАВИМЫЕ величины: «складской» остаток считается "
                 "как в 1С — приход минус расход, включая односторонние "
                 "переработки. «Проектный» — то, что показано в дереве: из него "
                 "односторонние выброшены намеренно, иначе диагностический расход "
                 "без весовой пары уменьшал бы остаток бизнес-плана потерей, "
                 "которой не было (БП 1586). Проектный с 1С сверять нельзя: в "
                 "снимке треть тоннажа лежит вообще без серии. "
                 "Эталон покрывает %d складов из %d. "
                 "Период выгрузки начинается %s: металл, поступивший раньше, "
                 "в приходе не виден, и по части ключей остаток уходит в минус "
                 "(такие ячейки помечены красным)."
                 % (len(stock_sklady), len(set(s for s, _ in M["balance"])),
                    M["period_min"].strftime("%d.%m.%Y"))},
        {"name": "Позиции с бейджами «нет» / «вероятно» — на валидацию заказчику",
         "status": "ok" if no_valid_t < 1 else "warn",
         "value": f"{no_valid_t:,.3f} т в {no_valid_n} позициях".replace(",", " "),
         "note": "Эвристика или отсутствие серии. Привязать вручную — через "
                 "data/overrides.json (бейдж «ручная»)."},
    ]

    meta = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "period_min": M["period_min"].strftime("%Y-%m-%d"),
        "period_max": M["period_max"].strftime("%Y-%m-%d"),
        # момент снятия регистра оборотов (колонка «ДатаВыгрузки») — плашка
        # свежести на вкладке «Остатки» (заказчик 14.09.2026)
        "dump_dt": M["dump_dt"].strftime("%Y-%m-%d %H:%M") if M.get("dump_dt") else "",
        "stock_date": stock_date.strftime("%Y-%m-%d") if stock_date else "",
        "balance_anchor": C.BALANCE_ANCHOR,
        # ПЛАН ПО ОБЪЁМАМ ПРОИЗВОДСТВА: подписи видов и какие месяцы вообще есть
        # в выгрузке. Месяцы нужны интерфейсу, чтобы честно сказать, за какой
        # период план заведён, — иначе пустые рамки читались бы как «плана нет».
        "prod_ops": {"выпуск": "выпуск продукции",
                     "вывоз": "вывоз с цеха на базу",
                     "отгрузка": "отгрузка покупателю",
                     "приход": "приход и закупка"},
        "prod_months": (prod_stats or {}).get("months", []),
        "prod_bps": (prod_stats or {}).get("matched", 0),
        # ПРОГНОЗ ОСТАТКА ПО БАЗАМ: нулевой месяц, горизонт и свод по каждой
        # базе. Интерфейсу нужны и месяцы, и итоги базы: на строке стоит только
        # доля бизнес-плана, а объяснять её надо картиной всей базы.
        "prod_m0": prod_m0,
        "prod_horizon": prod_horizon,
        # якорь прогноза: 1-е число позднейшего из (нулевой месяц, месяц
        # последних движений). Отличается от prod_m0 — выгрузка плана устарела,
        # и интерфейс обязан сказать об этом словами.
        "prod_anchor": prod_anchor,
        "prod_fc": prod_fc,
        # КОММЕНТАРИИ ПО ЗАПАСАМ: интерфейсу нужны дата выгрузки и охват, чтобы
        # честно сказать, на какое число лента и сколько планов она покрывает.
        "notes_dump": (note_stats or {}).get("dump", ""),
        "notes_last": ((note_stats or {}).get("dates") or [""])[-1],
        "notes_bps": (note_stats or {}).get("matched", 0),
        "notes_total": (note_stats or {}).get("kept", 0),
        # версии расчётов БП: когда выгружено и счётчики для шапки вкладки
        "bpver_generated": (bpver_stats or {}).get("generated", ""),
        "bpver_bps": (bpver_stats or {}).get("bps", 0),
        "bpver_chosen": (bpver_stats or {}).get("chosen", 0),
        # с какой датой сравнивает дашборд (пусто — сравнивать не с чем)
        "pulse_prev_date": prev_date,
        "pulse_prev_age": prev_age,
        # какие даты доступны для сравнения (понедельники, свежие сверху)
        "pulse_dates": sorted(pulses.keys(), reverse=True),
        "rows_total": M["rows"],
        "sale_rows": len(M["sales"]),
        "internal_rows": len(M["internal_sales"]),
        "internal_qty": round(sum(s["qty"] for s in M["internal_sales"]), 3),
        "total_tonnes": total_t,
        "real_series_tonnes": real_t,
        "real_series_pct": round(100.0 * real_t / total_t, 2) if total_t else 0.0,
        "platform_tonnes": platform_t,
        "no_series_tonnes": round(total_t - real_t - platform_t, 3),
        "positions": len(out_items),
        "badge_tonnes": {k: round(v, 3) for k, v in by_badge_t.items()},
        "badge_rows": dict(badge_rows),
        "site_tonnes": {k: round(v, 3) for k, v in by_site.items()},
        "month_tonnes": {k: round(v, 3) for k, v in sorted(by_month.items())},
        "badge_title": C.BADGE_TITLE,
        "check_bad": bad, "check_total": len(checks), "check_covered": n_covered,
        "check_negative": n_negative,
        "stock_sklady": len(stock_sklady), "move_sklady": len(set(s for s, _ in M["balance"])),
        "rashod_by_kind": {k: round(v, 3) for k, v in M["rashod_by_kind"].most_common(20)},
        "flow_order": C.FLOW_ORDER, "flow_title": C.FLOW_TITLE,
        "flow_label": C.FLOW_LABEL, "flow_group_head": C.FLOW_GROUP_HEAD,
        "flow_groups": C.FLOW_GROUPS,
        "flow_totals": flow_totals,
        # факт выручки: сколько в регистре, сколько легло на наши продажи
        "deals": {"rows": deal_stats.get("rows", 0), "with_share": deal_stats.get("with_share", 0),
                  "file": os.path.basename(f["deals"]) if f.get("deals") else ""},
        "revenue": {"rows": rev_stats.get("rows", 0), "total": round(rev_stats.get("rev", 0.0), 2),
                    "cost": round(rev_stats.get("cost", 0.0), 2),
                    "linked": round(rev_stats.get("linked", 0.0), 2),
                    "max_date": rev_stats.get("max_date", ""),
                    "file": os.path.basename(f["revenue"]) if f.get("revenue") else ""},
        # построчные движения: как читать колоночный массив DATA.moves
        "move_cols": ["flow", "dt", "doc", "code", "qty", "tonnes",
                      "from", "to", "series", "variant", "badge", "lot", "sklad",
                      "mc", "born", "mq", "lotk", "wk"],
        # формулировка потока с учётом вида работ: «в производство» -> «в сортировку»
        "work_phrase": {"%s|%s" % k: v for k, v in C.WORK_PHRASE.items()},
        "work_short": C.WORK_SHORT,
        # «прио» -> «Приобретение товаров и услуг»: подпись лота в интерфейсе
        # собирается из ключа, чтобы не тащить её отдельной колонкой на 304 тыс. строк
        "lot_types": M.get("lot_types", {}),
        "move_flows": move_flows,
        # по этой метке интерфейс отличает НАСТОЯЩУЮ строку серии 1С от нашей
        # заглушки «имя проекта + пометка» и не даёт сверять её с 1С построчно
        "var_synth_mark": C.VAR_SYNTH_MARK,
        "var_synth_badge": C.VAR_SYNTH_BADGE,
        "var_unknown": C.VAR_UNKNOWN,
        "negative_in": dict(C.NEGATIVE_IN),
        "birth_in": dict(C.BIRTH_IN),
        "birth_label": C.BIRTH_LABEL,
        # РАЗРЕЗЫ ПО ЕДИНИЦЕ — строятся из самих данных, а не зашиты: после новой
        # выгрузки список не разойдётся с тем, что в ней реально есть. Тонны
        # всегда первыми, дальше по числу бизнес-планов.
        "measures": measures_out,
        "move_phrase": C.MOVE_PHRASE, "move_short": C.MOVE_SHORT,
        "sale_rows_no_lot": no_lot_rows,
        "move_rows": len(moves),
        "autochecks": autochecks,
        "balance_moves": balance_moves,
        "balance_moves_1c": balance_moves_1c,
        "today": M["period_max"].strftime("%Y-%m"),
        # статусы по каждому варианту диаграммы + описания вариантов для шапки
        "gantt_status": gantt_status,
        "gantt_variants": [{"key": k, "title": t, "hint": h,
                            "sites": list(s), "in": list(i), "out": list(o)}
                           for k, t, h, s, i, o in C.GANTT_VARIANTS],
        "gantt_flows": list(C.GANTT_FLOWS),
        "flow_short": C.MOVE_SHORT,
        "direct_tonnes": round(direct_tonnes, 3),
        "mix_lots": len(M["part_mix"]),
        "mix_alloc_rows": len(M["mix_alloc"]),
        "mix_split_rows": mix_by_badge.get("деление", 0),
        "thresholds": {"INHERIT_MIN_SHARE": C.INHERIT_MIN_SHARE,
                       "MIX_MIN_SHARE": C.MIX_MIN_SHARE,
                       "MIX_MAX_COMPONENTS": C.MIX_MAX_COMPONENTS},
        "files": {k: os.path.basename(v) if v else "" for k, v in f.items()},
    }

    data = {"meta": meta, "S": _STR, "items": out_items, "checks": checks,
            "flows": flows_tbl, "buys": buys_out, "chain": chain_out,
            "oversold": oversold, "flows_var": flows_var, "movegap": movegap,
            "recon": recon,
        "moves": moves, "nm": name_of, "mix_notes": M["mix_notes"],
            "gantt": gantt,
            # версии расчётов БП с выручкой и отметками экономистов
            "bpver": bpver_rows,
            # закупка по бизнес-плану: факт 1С по головной серии (НМК × склад)
            # рядом с плановой выручкой версии — вкладка «Закупка по БП»
            "bpbuy": bpbuy,
            # слепок ближайшего прошедшего понедельника — для дельт на дашборде
            "pulse_prev": pulses.get(prev_date) or {},
            # понедельничные слепки: {дата -> {серия -> {вариант:[остаток,полоса,
            # опоздание]}}} — из них дашборд строит ВТОРОЙ экран «как было»
            "pulses": pulses,
            "sk_site": sk_site, "sk_unit": sk_unit,
            # вкладка «Остатки» (14.09.2026): подразделение склада как в 1С и
            # группа аналитического учёта номенклатуры («НоменклатурнаяГруппаГрузов»
            # справочника) — те же разрезы, что в отчёте 1С по остаткам
            "sk_podr": sk_podr, "sk_tree": sk_tree,
            "nm_grp": {c_: (nom_by_name.get(n_) or {}).get("cargo_group") or ""
                       for c_, n_ in name_of.items()},
            # что внутри бизнес-плана: g — группы аналитического учёта (для
            # фильтра), t — номенклатура отчёта, топ-8 по тоннажу (для значков)
            # d — дивизионы (фильтр), c — контрагенты плана из справочника серий
            # (все, а не только первый: у проекта бывает несколько серий)
            # ⚠️ Ключ первого уровня внутри плана — КЛАСС ЕДИНИЦЫ («т», «шт», …):
            # у плана, который весь в штуках, тонн нет вовсе, и в разрезе тонн он
            # обязан быть пустым, а не показывать чужие числа.
            "bp_nm": {sk: dict(
                        {mk: {"g": {k: round(v, 3) for k, v in
                                    sorted(gg.items(), key=lambda kv: -kv[1])},
                              "t": {k: round(v, 3) for k, v in
                                    sorted(bp_tag[sk][mk].items(),
                                           key=lambda kv: -kv[1])[:8]},
                              "d": {k: round(v, 3) for k, v in
                                    sorted(bp_div[sk][mk].items(),
                                           key=lambda kv: -kv[1])},
                              # s — итоги плана в этой единице (куплено/продано):
                              # из них строится сводка «также: 15 шт» в шапке
                              "s": {k: round(v, 3)
                                    for k, v in bp_tot[sk][mk].items()
                                    if abs(v) > 0.0005}}
                         for mk, gg in g.items()},
                        c=(series_ref.get(sk) or {}).get("contragents") or [])
                      for sk, g in bp_grp.items()},
            "edits": edits_log,
            "internal": [{"regnum": s["regnum"], "dt": s["dt"].strftime("%Y-%m-%d"),
                          "code": s["code"], "name": s["name"], "qty": round(s["qty"], 3),
                          "sklad": s["sklad"], "org": s["org"]}
                         for s in M["internal_sales"]]}

    path = os.path.join(OUT, "sales_data.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(path) / 1e6

    # ---------------- консольная сверка ----------------
    print("\n" + "=" * 72)
    print("СВЕРКА РЕАЛИЗАЦИИ")
    print("=" * 72)
    print(f"Период выгрузки        : {meta['period_min']} … {meta['period_max']}")
    print(f"Строк реализации       : {meta['sale_rows']}")
    print(f"Позиций (склад·код·серия): {meta['positions']}")
    print(f"\nПРОДАНО ВСЕГО          : {total_t:,.3f} т".replace(",", " "))
    print(f"  с реальной серией 1С : {real_t:,.3f} т  ({meta['real_series_pct']} %)".replace(",", " "))
    print(f"  псевдо-серия площадки: {platform_t:,.3f} т  (номер договора из имени склада)".replace(",", " "))
    print(f"  БЕЗ СЕРИИ            : {meta['no_series_tonnes']:,.3f} т".replace(",", " "))
    print("\nПо типу площадки (скриншот 1: цех=3, база=4):")
    for k, v in by_site.most_common():
        print(f"  {k:<10} {v:>14,.3f} т".replace(",", " "))
    print("\nПо бейджам происхождения серии:")
    for b, v in sorted(by_badge_t.items(), key=lambda kv: C.BADGE_RANK.get(kv[0], 9)):
        print(f"  {b:<12} {v:>14,.3f} т   {badge_rows[b]:>6} строк   {C.BADGE_TITLE.get(b,'')}".replace(",", " "))
    print(f"\nВнутренний оборот (перепродажа своей орг. «проект Базы»), ИСКЛЮЧЁН из продаж:")
    print(f"  {meta['internal_rows']} строк, {meta['internal_qty']:,.3f} ед.".replace(",", " "))
    ft = meta["flow_totals"]
    print("\nЖИЗНЕННЫЙ ЦИКЛ ПАРТИИ (купили -> продали / вернули / списали / осталось), т:")
    for n in C.FLOW_ORDER:
        if abs(ft.get(n, 0)) > 0.001:
            print(f"  {n:<20} {ft[n]:>14,.3f}   {C.FLOW_TITLE.get(n,'')}".replace(",", " "))
    print(f"  {'внутренний_оборот':<20} {ft['внутренний_оборот']:>14,.3f}   "
          f"перепродажа своей орг. (исключена из продаж)".replace(",", " "))
    print(f"  {'-'*20}")
    print(f"  {'ПОСТУПИЛО':<20} {ft['поступило']:>14,.3f}".replace(",", " "))
    print(f"  {'ВЫБЫЛО':<20} {ft['выбыло']:>14,.3f}".replace(",", " "))
    print(f"  {'ОСТАТОК (факт)':<20} {ft['остаток']:>14,.3f}".replace(",", " "))
    print(f"  {'НЕВЯЗКА':<20} {ft['невязка']:>14,.3f}   поступило − выбыло − остаток".replace(",", " "))

    print(f"\nОБРАТНЫЙ ХОД: якорь — факт на {meta['stock_date']}, движения отматываем назад")
    print(f"  остаток на {C.BALANCE_ANCHOR} = факт + расход после − приход после (результат, не вход)")
    print(f"  ключей (склад·код)   : {len(checks)}")
    print(f"  якорь покрывает {meta['stock_sklady']} складов из "
          f"{meta['move_sklady']} в движениях -> ключей с якорем {n_covered}")
    print(f"  расходится (|Δ|>0.001): {bad}  ({100.0*bad/max(n_covered,1):.1f} % от сверяемых)")
    top = [c for c in checks if c["covered"] and abs(c["diff"]) > 0.001][:5]
    for c in top:
        print(f"    Δ={c['diff']:>12,.3f}  {c['sklad'][:28]:<28} {c['code']} {c['name'][:30]}".replace(",", " "))
    print("\n" + "=" * 72)
    print("АВТОПРОВЕРКИ")
    print("=" * 72)
    mark = {"ok": "✓", "warn": "!", "bad": "✗"}
    for a in autochecks:
        print(f"  [{mark.get(a['status'],'?')}] {a['name']}")
        print(f"        {a['value']}")
    print(f"\nПострочные движения: {len(moves)} строк "
          f"(документ → номенклатура → количество → откуда → куда)")
    print(f"Смешанные лоты: {len(M['part_mix'])}, разложено строк {len(M['mix_alloc'])} "
          f"(делением {mix_by_badge.get('деление', 0)})")
    print(f"\nout/sales_data.json — {size:.1f} МБ")

if __name__ == "__main__":
    main()
