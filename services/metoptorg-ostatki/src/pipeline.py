# -*- coding: utf-8 -*-
"""
Ядро расчёта: файлы 1С -> фактические остатки с привязкой к серии и документу.

Порядок (важен, см. ТЗ):
  1. Загрузка справочников (номенклатура/единицы, серии->контрагент/вывоз, склады->база).
  2. Канонизация серий ДО расчёта (ключ = ведущий номер 3–4 цифры).
  3. Движения регистра, срез по дате актуальности (дата факта).
  4. Карта (Партия+Код -> серия) по всей выгрузке.
  5. Каскад восстановления серии для документа-прихода.
  6. Распределение фактических остатков (склад+код), новейшие приходы -> старые.
     Расходы НЕ моделируются. Строки факта с серией — как есть (метка «1С»).
  7. По-документная детализация каждой серии.
"""
import csv, re, os, glob, json, sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(__file__))
import config as C

csv.field_size_limit(1 << 24)

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def read_text_lines(path):
    """1С-выгрузки бывают UTF-8, UTF-8-BOM либо «двойной» cp1251-mojibake.
    Детекция по всему файлу (окно может резать многобайтовый символ)."""
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8").splitlines()      # реальный UTF-8
    except UnicodeDecodeError:
        pass
    try:                                              # cp1251->utf-8 mojibake
        return raw.decode("cp1251").encode("cp1251", "ignore").decode("utf-8").splitlines()
    except Exception:
        return raw.decode("cp1251", "ignore").splitlines()  # честный cp1251

def to_float(s):
    if s is None: return 0.0
    s = str(s).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s: return 0.0
    try: return float(s)
    except ValueError: return 0.0

def parse_dt(s):
    """'2026-02-05 14:00:12.000 +0500' -> datetime (naive, в локальной шкале)."""
    if not s: return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", s)
    if not m:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m: return datetime.strptime(m.group(1), "%Y-%m-%d")
        return None
    return datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------------------
# Канонизация серии
# ---------------------------------------------------------------------------
_LEAD = re.compile(r"^\s*(\d{3,4}(?:\s*/\s*\d{3,4})*)")
_CONTRACT_PATTERNS = [
    re.compile(r"\b(\d{2}[A-Za-zА-Яа-я]{2,4}\d{3,4}[A-Za-zА-Яа-я]?(?:/\d+[A-Za-zА-Яа-я]{2,4})?)\b"),  # 22ESP0581K, 0737K/41ESP
    re.compile(r"\b(\d{9,10})\b"),                 # 2024036839
    re.compile(r"\b(\d{3}/[A-Za-zА-Яа-я]{2}/\d{4})\b"),  # 003/ЛК/2021
    re.compile(r"\b(\d{2,3}[A-Za-zА-Яа-я]\d{3,4})\b"),   # 24Z1245, 23y0618
    re.compile(r"\b(\d{3}/\d{4})\b"),              # 907/2022
]

def canon_key(raw):
    """Ведущий номер серии как канонический ключ. None если это не серия."""
    if not raw: return None
    raw = raw.strip()
    if C.NON_SERIES.match(raw): return None
    m = _LEAD.match(raw)
    if not m: return None
    return re.sub(r"\s+", "", m.group(1))   # '1491 / 1493' -> '1491/1493'

def extract_contract(raw):
    if not raw: return ""
    for pat in _CONTRACT_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1).translate(C.HOMOGLYPH).upper()
    return ""

def extract_direction(raw):
    if not raw: return "Прочее"
    for name, pat in C.DIRECTION_RULES:
        if pat.search(raw): return name
    m = re.search(r"\(([^)]+)\)\s*(?:пер|ДС|СП|$)", raw)  # маркер из скобок
    if m: return m.group(1).strip()
    return "Прочее"

def marker_series(raw):
    """Служебные пометки происхождения без номера."""
    if not raw: return None
    u = raw.upper()
    if "ВВОД ОСТАТК" in u: return ("ВВОД ОСТАТКОВ (без серии)", "ввод")
    if "ИЗЛИШ" in u:       return ("ИЗЛИШКИ", "ввод")
    return None

# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------
def load_nomenklatura(path):
    """name/guid -> {report_group, unit_base, unit_report, coef}."""
    lines = read_text_lines(path)
    r = csv.DictReader(lines)
    by_name, by_guid = {}, {}
    for row in r:
        info = {
            "report_group": (row.get("НоменклатураОтчета") or "").strip(),
            "unit_base": (row.get("ЕдиницаИзмерения") or "").strip().lower(),
            "unit_report": (row.get("ЕдиницаДляОтчетов") or "").strip().lower(),
            "coef": to_float(row.get("КоэффициентПересчета")) or 1.0,
        }
        name = (row.get("Номенклатура") or "").strip()
        guid = (row.get("НоменклатураГуид") or "").strip().upper()
        # дубли номенклатуры: берём более информативную (с отчётной группой/единицей)
        def score(i): return (bool(i["report_group"]), bool(i["unit_report"]))
        if name and (name not in by_name or score(info) > score(by_name[name])):
            by_name[name] = info
        if guid and guid != "00000000-0000-0000-0000-000000000000":
            if guid not in by_guid or score(info) > score(by_guid[guid]):
                by_guid[guid] = info
    return by_name, by_guid

def load_series_ref(path):
    """canon_key -> {contragent, datavyvoza}. Сливаем V-варианты, предпочитая
    заполненного контрагента."""
    lines = read_text_lines(path)
    r = csv.DictReader(lines)
    out = {}
    for row in r:
        key = canon_key(row.get("Серия"))
        if not key: continue
        contr = (row.get("Контрагент") or "").strip()
        vyv = (row.get("ДатаВывоза") or "").strip()[:10]
        if vyv.startswith("0001"): vyv = ""
        cur = out.get(key)
        if cur is None:
            out[key] = {"contragent": contr, "datavyvoza": vyv}
        else:
            if contr and not cur["contragent"]: cur["contragent"] = contr
            if vyv and not cur["datavyvoza"]: cur["datavyvoza"] = vyv
    return out

def load_sklady(path):
    """Склад -> база «5.». Договорные площадки -> база по городу из имени."""
    lines = read_text_lines(path)
    r = csv.DictReader(lines)
    sklad2base = {}
    sklad2parent = {}
    for row in r:
        name = (row.get("Склад") or "").strip()
        top = (row.get("ВысшийРодитель") or "").strip()
        parent = (row.get("Родитель") or "").strip()
        sklad2parent[name] = parent
        base = None
        if top.startswith("5"):
            base = top
        else:
            # площадка/склад вне «5» — попробуем по городу из имени
            for pat, b in C.CITY_TO_BASE:
                if pat.search(name) or (parent and pat.search(parent)):
                    base = b; break
            if base is None:
                if re.search(r"\(коми\)", name, re.I): base = C.FALLBACK_BASE_KOMI
                elif re.search(r"\bЗС\b|ЗапСиб", name, re.I): base = C.FALLBACK_BASE_ZS
        if base: sklad2base[name] = base
    return sklad2base, sklad2parent

# площадка продавца: «БП NNN» / номер договора в имени склада-источника
_PLATFORM_CONTRACT = re.compile(r"\(([0-9]{2}[A-Za-zА-Яа-я]{0,4}\d{3,6}[A-Za-zА-Яа-я]?)\)|БП\s*\d+", re.I)
def platform_contract_from_sklad(name):
    if not name: return ""
    m = _PLATFORM_CONTRACT.search(name)
    if m: return (m.group(1) or m.group(0)).translate(C.HOMOGLYPH).upper()
    return ""

# ---------------------------------------------------------------------------
# Факт (xlsx)
# ---------------------------------------------------------------------------
def load_fact(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h is not None else "" for h in next(it)]
    idx = {h: i for i, h in enumerate(hdr)}
    def g(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else None
    rows = []
    for row in it:
        code = str(g(row, "Номенклатура.Код") or "").strip()
        if not code: continue
        rows.append({
            "sklad": str(g(row, "Склад") or "").strip(),
            "unit": str(g(row, "Ед. изм.") or "").strip().lower(),
            "code": code,
            "podr": str(g(row, "Подразделение") or "").strip(),
            "name": str(g(row, "Номенклатура") or "").strip(),
            "series_raw": str(g(row, "Серия") or "").strip(),
            "qty": to_float(g(row, "Начальный остаток")),
        })
    # дата актуальности = дата из имени файла минус 1 день.
    # Поддерживаем ДД.ММ.ГГГГ (напр. «…24.08.2026…») и ДД.ММ.ГГ (напр. «…18.07.26…»).
    base = os.path.basename(path)
    m4 = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", base)
    m2 = re.search(r"(\d{2})\.(\d{2})\.(\d{2})(?!\d)", base)
    if m4:
        d, mo, y = int(m4.group(1)), int(m4.group(2)), int(m4.group(3))
    elif m2:
        d, mo, y = int(m2.group(1)), int(m2.group(2)), 2000 + int(m2.group(3))
    else:
        return rows, None
    actual = datetime(y, mo, d) - timedelta(days=1)
    return rows, actual

# ---------------------------------------------------------------------------
# Движения регистра
# ---------------------------------------------------------------------------
def load_movements(path, cutoff_dt):
    """Возвращает список приход-строк (для распределения) и карту (партия,код)->серия.
    Срез: период > даты актуальности отбрасывается."""
    lines = read_text_lines(path)
    r = csv.DictReader(lines)
    prihods = []          # строки прихода
    part_map_votes = defaultdict(Counter)  # (partia_norm, code) -> Counter(canon)
    # для наследования при переработке: по регистратору собираем расход-серии (сырьё)
    PROD = ("Документ.ПроизводствоБезЗаказа", "Документ.СборкаТоваров")
    prod_out = defaultdict(set)     # regnum -> {выпущенные лоты (partia_norm, code)}
    prod_in  = defaultdict(list)    # regnum -> [(потреблённый лот (partia_norm, code), вес)]
    reg_dt = {}
    check_agg = {}     # code -> {name, prihod, rashod}  (для вкладки «Проверка»)
    series_repr = {}   # canon_key -> представительная «сырая» строка серии (для договора/направления)
    n = 0
    for row in r:
        n += 1
        dt = parse_dt(row.get("Период"))
        if dt is None: continue
        if cutoff_dt and dt > cutoff_dt: continue
        code = (row.get("АналитикаУчетаНоменклатурыНоменклатураКод") or "").strip()
        series_raw = (row.get("АналитикаУчетаНоменклатурыСерия") or "").strip()
        ckey = canon_key(series_raw)
        sklad = (row.get("АналитикаУчетаНоменклатурыСкладскаяТерритория") or "").strip()
        regnum = (row.get("РегистраторНомер") or "").strip()
        partia = (row.get("Партия") or "").strip()
        ptype = (row.get("ПартияТип") or "").strip()
        prihod = to_float(row.get("КоличествоПриход"))
        rashod = to_float(row.get("КоличествоРасход"))
        guid = (row.get("АналитикаУчетаНоменклатурыНоменклатураГуид") or "").strip().upper()
        name = (row.get("АналитикаУчетаНоменклатурыНоменклатура") or "").strip()
        partia_norm = _norm_partia(partia)
        reg_dt.setdefault(regnum, dt)
        ca = check_agg.get(code)
        if ca is None:
            ca = check_agg[code] = {"name": name, "prihod": 0.0, "rashod": 0.0}
        ca["prihod"] += prihod; ca["rashod"] += rashod
        if ckey:
            cur = series_repr.get(ckey)
            if cur is None or ("(" not in cur and "(" in series_raw):
                series_repr[ckey] = series_raw
        if ckey and prihod > 0:
            part_map_votes[(partia_norm, code)][ckey] += prihod
        # структура переработки: выпуск наследует серию потреблённого сырья
        if ptype in PROD:
            if prihod > 0:
                prod_out[regnum].add((partia_norm, code))
            if rashod > 0:
                prod_in[regnum].append(((partia_norm, code), rashod))
        if prihod > 0:
            prihods.append({
                "dt": dt, "code": code, "series_raw": series_raw, "ckey": ckey,
                "sklad": sklad, "regnum": regnum, "partia": partia,
                "partia_norm": partia_norm, "ptype": ptype, "qty": prihod,
                "guid": guid, "name": name,
            })
    # карта (Партия+Код -> серия): побеждает самая тяжёлая серия
    part_map = {}
    for k, cnt in part_map_votes.items():
        part_map[k] = cnt.most_common(1)[0][0]

    # Наследование при переработке — итеративно (несколько переделов).
    # Для смесей храним РАСПРЕДЕЛЕНИЕ серий выпуска по весам потреблённого сырья:
    #   lot_dist[лот] = {серия: доля}. Сырьё разрешается по part_map (инлайн) или
    #   по уже вычисленному распределению (глубокие переделы).
    lot_dist = {}   # (partia_norm, code) -> {canon_series: доля (сумма=1)}
    def input_dist(k):
        if k in lot_dist: return lot_dist[k]
        if k in part_map: return {part_map[k]: 1.0}
        return None
    def _sig(d):    # сигнатура для проверки сходимости
        return tuple(sorted((s, round(f, 6)) for s, f in d.items()))
    changed, it = True, 0
    while changed and it < 60:
        changed, it = False, it + 1
        for regnum, outs in prod_out.items():
            ins = prod_in.get(regnum)
            if not ins:
                continue
            agg = defaultdict(float)
            for ikey, w in ins:
                di = input_dist(ikey)
                if di:
                    for s, f in di.items():
                        agg[s] += w * f
            tot = sum(agg.values())
            if tot <= 0:
                continue
            dist = {s: v / tot for s, v in agg.items()}
            for okey in outs:
                if okey in part_map:
                    continue   # инлайн-серия приоритетнее наследования
                if okey not in lot_dist or _sig(lot_dist[okey]) != _sig(dist):
                    lot_dist[okey] = dist
                    changed = True
    return prihods, part_map, lot_dist, reg_dt, n, check_agg, series_repr

def load_overrides(path):
    """Ручные привязки серии: (regnum) или (regnum,code) -> запись оверрайда."""
    if not path or not os.path.exists(path):
        return {}
    try:
        items = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for it in items or []:
        rn = (it.get("regnum") or "").strip()
        if not rn or not it.get("series"):
            continue
        key = (rn, (it.get("code") or "").strip())   # code="" => применять ко всем лотам документа
        out[key] = {"series": str(it["series"]).strip(),
                    "author": it.get("author", ""), "note": it.get("note", "")}
    return out

def override_for(overrides, regnum, code):
    if not overrides:
        return None
    return overrides.get((regnum, code)) or overrides.get((regnum, ""))

def _norm_partia(p):
    """Нормализуем описание партии до документа: 'Производство ... РУМЛ-000753 ...'
    -> ключ по номеру документа, иначе по очищенной строке."""
    if not p: return ""
    m = re.search(r"(РУМЛ-\d+|РУ00-\d+|РУ\d+-\d+|МЛ\d+-\d+)", p)
    if m: return m.group(1)
    return re.sub(r"\s+от\s+\d.*$", "", p).strip()

# ---------------------------------------------------------------------------
# Каскад восстановления серии для документа-прихода
# ---------------------------------------------------------------------------
def recover_components(p, part_map, lot_dist, uniq):
    """Каскад восстановления серии -> список компонент [(canon|None, доля, badge)].
    Для смесей переработки — несколько компонент (доминирующая первой), badge «доля».
    Сумма долей = 1."""
    key = (p["partia_norm"], p["code"])
    # 1. серия из строки регистра
    if p["ckey"]:
        return [(p["ckey"], 1.0, "док")]
    # 2. карта (партия+код) -> серия
    hit = part_map.get(key)
    if hit:
        return [(hit, 1.0, "док")]
    # 3. наследование при переработке: доминирующая серия сырья забирает весь выпуск
    #    (как в прошлом варианте отчёта — одна серия на позицию, без дробления смеси).
    dist = lot_dist.get(key)
    if dist:
        best = max(dist.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return [(best, 1.0, "переработка")]
    # 4. уникальная серия документа
    if uniq:
        return [(uniq, 1.0, "ручная")]
    # 5. площадка продавца
    pc = platform_contract_from_sklad(p["sklad"])
    if pc:
        return [("площадка:" + pc, 1.0, "площадка")]
    # 6. служебные пометки
    if marker_series(p["series_raw"]):
        return [(None, 1.0, "ввод")]
    return [(None, 1.0, "нет")]

# ---------------------------------------------------------------------------
# Тоннаж
# ---------------------------------------------------------------------------
def qty_to_tonnes(qty, unit, info):
    """info — запись номенклатуры (может быть None). unit — из факта (приоритет)."""
    u = (unit or "").lower()
    if not u and info: u = info.get("unit_base", "")
    if u in C.UNIT_TONNE:
        return qty * C.UNIT_TONNE[u]
    # труба в метрах, отчётная единица «т» -> делим на коэффициент пересчёта
    if info and info.get("unit_report") == "т" and u in ("м", "пог.м", "пм"):
        coef = info.get("coef") or 1.0
        return qty / coef if coef else 0.0
    if info and info.get("unit_report") == "т" and u not in C.UNIT_NON_WEIGHT:
        coef = info.get("coef") or 1.0
        return qty / coef if coef else 0.0
    return 0.0   # шт/компл/пар/л/… — не тонны

def category_of(name, report_group):
    text = (name or "") + " " + (report_group or "")
    for cat, pat in C.CATEGORIES:
        if pat.search(text): return cat
    return "Прочее"

# ---------------------------------------------------------------------------
# Распределение фактических остатков (финальное правило заказчика)
# ---------------------------------------------------------------------------
def distribute(fact_rows, prihods, part_map, lot_dist, sklad2base,
               nom_by_name, nom_by_guid, series_ref, overrides=None, edits_log=None,
               series_repr=None, recount_date=None):
    """Для каждого ключа (склад, код):
       - строки факта с серией -> как есть, badge «1С»;
       - непокрытый остаток покрывается приходами новейшие->старые;
       - остаток без покрытия -> «БЕЗ СЕРИИ».
       Каждая серия хранит состав документов.
    Расходы не моделируются вовсе."""
    # приходы по ключу (склад, код), отсортированы новейшие->старые.
    # FIFO tie-break: равное время -> больший РегистраторНомер раньше.
    by_key = defaultdict(list)
    for p in prihods:
        by_key[(p["sklad"], p["code"])].append(p)
    def sort_recent_first(lst):
        return sorted(lst, key=lambda p: (p["dt"], _regnum_sort(p["regnum"])), reverse=True)

    # факт по ключу
    fact_by_key = defaultdict(list)
    for fr in fact_rows:
        fact_by_key[(fr["sklad"], fr["code"])].append(fr)

    results = []   # позиции-остатки с сериями и документами
    for (sklad, code), frs in fact_by_key.items():
        name = frs[0]["name"]
        unit = frs[0]["unit"]
        info = nom_by_name.get(name)
        report_group = (info or {}).get("report_group") or name
        cat = category_of(name, report_group)
        base = sklad2base.get(sklad) or _base_from_prihods(by_key.get((sklad, code), []), sklad2base)
        if not base:
            base = _nonfive_base(sklad, frs[0].get("podr", ""))   # склады вне «5» -> Демонтаж/Прочие
        if (base or "—") in C.EXCLUDE_BASES:
            continue

        total_qty = sum(fr["qty"] for fr in frs)
        # 1) строки факта с серией — как есть
        series_acc = defaultdict(lambda: {"qty": 0.0, "badge": None, "docs": []})
        covered = 0.0
        for fr in frs:
            ck = canon_key(fr["series_raw"])
            if ck:
                s = series_acc[ck]
                s["qty"] += fr["qty"]; s["badge"] = _best_badge(s["badge"], "1С")
                covered += fr["qty"]
        remaining = total_qty - covered
        # 2) покрываем остаток приходами новейшие->старые
        plist = sort_recent_first(by_key.get((sklad, code), []))
        # уникальная серия документа (уровень 4) — если под ключом ровно одна серия
        uniq = _unique_series_for_key(by_key.get((sklad, code), []))
        for p in plist:
            if remaining <= 1e-9: break
            take = min(p["qty"], remaining)
            ov = override_for(overrides, p["regnum"], p["code"])
            if ov:
                comps = [(ov["series"], 1.0, "ручная")]
                if edits_log is not None and take > 1e-9:
                    edits_log.append({"base": base, "sklad": sklad, "code": code,
                        "regnum": p["regnum"], "new": ov["series"], "old": "(восстановлено)",
                        "author": ov.get("author", ""), "note": ov.get("note", ""),
                        "qty": round(take, 3), "ts": p["dt"].strftime("%Y-%m-%d %H:%M:%S")})
            else:
                comps = recover_components(p, part_map, lot_dist, uniq)
            # Разбиение take по компонентам с точной суммой (3 знака, без дрейфа):
            # доминирующая компонента первой поглощает остаток, прочие — round(,3).
            others = 0.0
            amts = [0.0] * len(comps)
            for i in range(1, len(comps)):
                amts[i] = round(take * comps[i][1], 3); others += amts[i]
            amts[0] = round(take - others, 3)
            dt_str = p["dt"].strftime("%Y-%m-%d %H:%M:%S")
            for (ck, frac, badge), amt in zip(comps, amts):
                if amt <= 1e-9: continue
                if ck is None:
                    key = "БЕЗ СЕРИИ"; b = "ввод" if badge == "ввод" else "нет"
                else:
                    key = ck; b = badge
                s = series_acc[key]
                s["qty"] += amt; s["badge"] = _best_badge(s["badge"], b)
                s["docs"].append({"regnum": p["regnum"], "dt": dt_str,
                                  "qty": amt, "partia": p["partia"][:80]})
            remaining = round(remaining - take, 3)
        # 3) остаток без покрытия. Может быть отрицательным: в факте бывают
        #    отрицательные «Начальный остаток» (1С-корректировки / переизрасход).
        #    Их НЕ отбрасываем — иначе сумма раздувается (учитываются только плюсы),
        #    а нетто-остаток должен совпадать с файлом факта.
        if remaining > 1e-9:
            s = series_acc["БЕЗ СЕРИИ"]; s["qty"] += remaining; s["badge"] = _best_badge(s["badge"], "нет")
            # Синтетическая пометка «пересчёт»: серии нет ни в строке факта, ни в приходах.
            s["docs"].append({
                "regnum": "ПЕРЕСЧЁТ", "dt": (recount_date or ""),
                "qty": round(remaining, 3),
                "partia": ("Пересчёт остатков" + (" на " + recount_date if recount_date else "")
                           + " · серии нет в движениях — заполнить вручную"),
                "note": True})
        elif remaining < -1e-9:
            s = series_acc["БЕЗ СЕРИИ"]; s["qty"] += remaining; s["badge"] = _best_badge(s["badge"], "нет")
            s["docs"].append({
                "regnum": "ОТРИЦАТЕЛЬНЫЙ ОСТАТОК", "dt": (recount_date or ""),
                "qty": round(remaining, 3),
                "partia": "Отрицательный остаток в файле (1С-корректировка / переизрасход) — проверить",
                "note": True, "negative": True})

        for skey, s in series_acc.items():
            if -1e-9 < s["qty"] < 1e-9: continue   # пропускаем только НУЛЕВЫЕ (минусы сохраняем)
            is_real = skey not in ("БЕЗ СЕРИИ",) and not str(skey).startswith("площадка:")
            ckey = skey if is_real else ""
            ref = series_ref.get(ckey, {}) if ckey else {}
            tn = qty_to_tonnes(s["qty"], unit, info)
            # дата прихода серии = самая ранняя дата среди РЕАЛЬНЫХ документов (для залежалости);
            # синтетическая пометка «пересчёт» в расчёт залежалости не входит.
            real_dts = [d["dt"] for d in s["docs"] if not d.get("note") and d["dt"]]
            doc_dt = min(real_dts) if real_dts else None
            # «сырая» строка серии: локальный приход -> глобальная -> сам ключ
            raw = _raw_for_key(skey, plist)
            if is_real and raw == skey and series_repr and skey in series_repr:
                raw = series_repr[skey]
            results.append({
                "base": base or "—", "sklad": sklad, "code": code, "name": name,
                "category": cat, "report_group": report_group, "unit": unit,
                "series": skey, "series_display": _series_display(skey),
                # полная строка серии (как в загрузочных файлах 1С), для выгрузки
                "series_full": (raw if (is_real and raw) else _series_display(skey)),
                "contract": extract_contract(raw) if is_real else "",
                "direction": extract_direction(raw) if is_real else ("Без серии" if skey=="БЕЗ СЕРИИ" else "Площадка"),
                "contragent": ref.get("contragent", ""),
                "datavyvoza": ref.get("datavyvoza", ""),
                "badge": s["badge"], "qty": round(s["qty"], 3), "tonnes": round(tn, 3),
                "arrival_dt": doc_dt, "docs": sorted(s["docs"], key=lambda d: d["dt"], reverse=True),
            })
    return results

def _regnum_sort(rn):
    """Больший номер РУМЛ = раньше -> для reverse-сортировки используем число."""
    m = re.search(r"(\d+)", rn or "")
    return int(m.group(1)) if m else 0

def _best_badge(a, b):
    """Худшая метка побеждает при агрегации. None = ещё не задана."""
    if a is None: return b
    if b is None: return a
    return a if C.BADGE_RANK.get(a, 9) >= C.BADGE_RANK.get(b, 9) else b

def _unique_series_for_key(plist):
    ck = set(p["ckey"] for p in plist if p["ckey"])
    return next(iter(ck)) if len(ck) == 1 else None

def _nonfive_base(sklad, podr):
    """Склад вне баз «5.» -> узел «6. Демонтаж» (демонтажные площадки) или
    «7. Прочие склады». Номера 6/7 — чтобы сортировались после баз «5»."""
    t = ((sklad or "") + " " + (podr or "")).lower()
    if "демонтаж" in t:
        return "6. Демонтаж"
    return "7. Прочие склады"

def _base_from_prihods(plist, sklad2base):
    for p in plist:
        b = sklad2base.get(p["sklad"])
        if b: return b
    return None

def _raw_for_key(ckey, plist):
    for p in plist:
        if p["ckey"] == ckey: return p["series_raw"]
    return ckey

def _series_display(skey):
    if skey == "БЕЗ СЕРИИ": return "БЕЗ СЕРИИ"
    if str(skey).startswith("площадка:"): return "Площадка " + skey.split(":",1)[1]
    return str(skey)
