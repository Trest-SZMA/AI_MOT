# -*- coding: utf-8 -*-
"""
Ядро расчёта РЕАЛИЗАЦИИ: файлы 1С -> что, сколько и по какой серии/БП продано.

Порядок (важен, см. ТЗ):
  1. Загрузка справочников (номенклатура/единицы, серии->контрагент/вывоз, склады->база).
  2. Канонизация серий ДО расчёта (ключ = ведущий номер 3–4 цифры).
  3. Один проход по регистру: строки реализации + карта (Партия+Код -> серия)
     + структура переработки + агрегаты для сверки баланса.
  4. Наследование серии при переработке (полный состав сырья, итеративно).
  5. Каскад восстановления серии ДЛЯ КАЖДОЙ СТРОКИ ПРОДАЖИ (FIFO не нужен —
     у строки продажи есть свой лот-партия).
  6. Перевод в тонны, категории, направления, БП (договор).
  7. Сверка: остаток_нач + приход − расход = остаток_текущий; продано vs куплено.
"""
import csv, re, os, json, sys
from datetime import datetime
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(__file__))
import config as C

csv.field_size_limit(1 << 24)

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def read_text_lines(path):
    """1С-выгрузки бывают UTF-8, UTF-8-BOM либо «двойной» cp1251-mojibake.
    Детекция по ВСЕМУ файлу (окно может резать многобайтовый символ)."""
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8").splitlines()          # реальный UTF-8
    except UnicodeDecodeError:
        pass
    try:                                                  # cp1251->utf-8 mojibake
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
    """'2026-02-05 14:00:12.000 +0500' -> datetime (naive, локальная шкала)."""
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
    re.compile(r"\b(\d{9,10})\b"),                        # 2024036839
    re.compile(r"\b(\d{3}/[A-Za-zА-Яа-я]{2}/\d{4})\b"),   # 003/ЛК/2021
    re.compile(r"\b(\d{2,3}[A-Za-zА-Яа-я]\d{3,4})\b"),    # 24Z1245, 23y0618
    re.compile(r"\b(\d{3}/\d{4})\b"),                     # 907/2022
]

def canon_key(raw):
    """Ведущий номер серии. ВНИМАНИЕ: как ключ группировки НЕ годится —
    склеивает разные спецификации с одинаковым ведущим числом
    («1291032 … СП 1/25» и «… СП 115/26» дают один «1291»).
    Используется только там, где нужен именно номер. Группировка — по проекту."""
    if not raw: return None
    raw = raw.strip()
    if C.NON_SERIES.match(raw): return None
    m = _LEAD.match(raw)
    if not m: return None
    return re.sub(r"\s+", "", m.group(1))   # '1491 / 1493' -> '1491/1493'


# --- ПРОЕКТ как идентичность серии -----------------------------------------
# В справочнике серий у каждой серии есть реквизит «Проект». Варианты «… V» и
# «… VS» — это одна и та же серия, и в 1С они сведены в ОДИН проект.
# Бизнес-план = проект (их столько же, сколько проектов), а не номер договора:
# под одним договором бывают десятки спецификаций, и каждая — свой проект.
_VS_TAIL = re.compile(r"\s+(V|VS)\s*$", re.I)

def is_head_series(variant):
    """Головная ли это серия проекта (без хвоста «V» / «VS»).

    ⚠️ ПРАВИЛО ФИНДИРА (запись 01.09.2026): «по проекту может быть три серии —
    головная, В и ВС; сравнение с Excel берём ТОЛЬКО головную». Хвосты в 1С
    пишут латиницей (проверено на выгрузке: V — 966 строк закупки, VS — 328,
    головных — 6 373), но кириллические «В»/«ВС» ловим тоже: в справочнике
    серий их набирают обеими раскладками.
    """
    v = str(variant or "").strip()
    return not re.search(r"\s+(V|VS|В|ВС)\s*$", v, re.I)


def norm_series(raw):
    """Строка серии -> ключ сопоставления: без хвоста V/VS, схлопнутые пробелы."""
    if not raw: return ""
    s = _VS_TAIL.sub("", str(raw).strip())
    return re.sub(r"\s+", " ", s).strip().lower()


def series_key(raw, s2p):
    """Ключ группировки для строки серии = ПРОЕКТ.

    s2p — карта (нормализованная серия -> ключ проекта) из справочника серий.
    Если серии нет в справочнике, ключом становится сама нормализованная строка:
    так серия не сольётся с чужой по совпадению ведущего номера.
    None — если это вообще не серия («Объём №N»)."""
    if not raw: return None
    if C.NON_SERIES.match(str(raw).strip()): return None
    n = norm_series(raw)
    if not n: return None
    hit = s2p.get(n)
    if hit: return hit
    # точного совпадения нет — пробуем ведущий номер, но только однозначный
    # (серия = проект «плюс несколько символов»)
    k = canon_key(n)
    if k:
        hit = s2p.get("lead:" + k)
        if hit: return hit
    # серии нет в справочнике — она сама себе проект
    return "n:" + n

# --- извлечение номера договора (бизнес-плана) из строки серии ------------
# Строка серии почти всегда имеет вид:
#   «<номер серии> [(]<ДОГОВОР> от <дата> ... (<направление>) ...»
# Поэтому надёжнее не перечислять форматы номеров, а взять токен сразу после
# ведущего номера серии и обрезать его по «от <дата>» / скобке / «сп» / «ДС».
#
# Границу после ведущего номера требуем обязательно: иначе «23687А от …»
# (это договор, а не серия) обрежется до «2368» и договор потеряется.
# ведущие номера серий: «1491/1493» и «1819 1820 1821»
_LEAD_STRIP = re.compile(r"^\s*\d{3,4}(?:\s*[/ ]\s*\d{3,4})*(?=[\s(,]|$)\s*")
# «М№», «М_», «М_№», «Дог. №», «(»
_CONTRACT_PREFIX = re.compile(r"^[\s(«\"]*(?:[МM]_?)?(?:дог(?:овор)?\.?\s*)?(?:№\s*)?", re.I)
# запасные правила, если основной разбор не сработал
_FALLBACKS = [
    re.compile(r"дог(?:овор)?\.?\s*№\s*([0-9A-Za-zА-Яа-я/\-\.]{1,32})", re.I),   # «Дог №72», «Дог. №1»
    re.compile(r"\b(\d{9,10}(?:-\d{3,4})?)\b"),                                  # 2024043847-0003
    re.compile(r"\b([A-ZА-Я]{2,4}-\d{2,4}/\d{2,4})\b"),                          # УЗД-149/26
]
# в строке явно написано, что бизнес-плана нет
_NO_BP = re.compile(r"без\s*БП", re.I)
# «(?<=\d)от» — слипшееся «24z3964от 02.12.24»
_CONTRACT_STOP = re.compile(
    r"\s+от\s+\d|(?<=\d)от(?=\s|\d)|\s*\(|\s*\)|\s+сп\b|\s*сп\.|\s+спец|\s+прилож|"
    r"\s+прил\b|\s+ДС\d|\s+пер\b|\s+V\b|\s+VS\b|\s*,|\s+без\s+БП|\s+от\s*$", re.I)
_NOT_CONTRACT = re.compile(
    r"^(?:от|без|бп|дс|сп|спец|прилож|прил|v|vs|г|гг|труба|бочки|лом|кабель)$", re.I)

def _ok_contract(s, minlen=3):
    """Похоже ли на номер договора: есть цифра, разумная длина, только допустимые символы.
    minlen=2 разрешаем только при сильном признаке — «Дог №72» или «57 от 10.12.2024»."""
    if not s or _NOT_CONTRACT.match(s): return False
    if not re.search(r"\d", s): return False
    if not (minlen <= len(s) <= 32): return False
    return bool(re.fullmatch(r"[0-9A-Za-zА-Яа-я/\-\.]+", s))


def no_bp_marked(raw):
    """В строке серии явно указано «Без БП» — бизнес-плана нет по существу,
    а не «не смогли распознать»."""
    return bool(raw and _NO_BP.search(raw))


def extract_contract(raw, strip_lead=True):
    """Номер договора (бизнес-план) в ИСХОДНОМ написании. '' если не нашли."""
    if not raw: return ""
    s = raw.strip()
    if strip_lead:
        s = _LEAD_STRIP.sub("", s)
    s = _CONTRACT_PREFIX.sub("", s)
    m = _CONTRACT_STOP.search(s)
    # «57 от 10.12.2024» — за номером явно идёт дата, значит это договор,
    # даже если он короткий (2 символа).
    minlen = 2 if (m and re.match(r"\s+от\s+\d", m.group(0))) else 3
    if m:
        s = s[:m.start()]
    s = s.strip(" .,;«»\"()")
    s = s.split()[0] if s.split() else ""
    s = s.strip(" .,;«»\"()")
    if not _ok_contract(s, minlen):
        # основной разбор не дал результата — пробуем запасные правила по всей строке
        for i, pat in enumerate(_FALLBACKS):
            m2 = pat.search(raw)
            if m2:
                cand = m2.group(1).strip(" .,;«»\"()")
                # после явного «Дог №…» номер бывает совсем коротким («Дог. №1»)
                if _ok_contract(cand, 1 if i == 0 else 3) and not _NOT_CONTRACT.match(cand):
                    return cand.upper()
        return ""
    # верхний регистр — безопасно (23y0618 -> 23Y0618). Гомоглифы кириллица->
    # латиница здесь НЕ применяем: это исказило бы «УЗД-87/25» -> «YЗД-87/25».
    # Свёртка гомоглифов живёт только в contract_key() — ключе группировки.
    return s.upper()

def contract_key(contract):
    """Ключ слияния договоров: гомоглифы кириллица->латиница, верхний регистр.
    Нужен, чтобы 22С0371 (кир.) и 22C0371 (лат.) считались одним договором.
    Для ОТОБРАЖЕНИЯ используется самое частое исходное написание, а не ключ."""
    return (contract or "").translate(C.HOMOGLYPH).upper()

def extract_direction(raw):
    if not raw: return "Прочее"
    for name, pat in C.DIRECTION_RULES:
        if pat.search(raw): return name
    # Запасное правило: маркер из скобок. ⚠️ БРАТЬ ТОЛЬКО НЕВЛОЖЕННУЮ ГРУППУ
    # и ПОСЛЕДНЮЮ. Прежний шаблон `\(([^)]+)\)` пропускал внутрь открывающую
    # скобку, и у «1441 (23D00348 (УралОйл) СП №5)» направлением становилось
    # «23D00348 (УралОйл» — номер договора вместо направления.
    inner = re.findall(r"\(([^()]+)\)", raw)
    for g in reversed(inner):
        g = g.strip()
        # «(23.05.2024)», «(№5)», «(22C0371)» — это не направление
        if g and not re.fullmatch(r"[\d\W]+|№?\s*\d+|\d{2}[A-ZА-Я]\d+", g, re.I):
            return g
    return "Прочее"

def resolve_contract(ckey, series_variants, series_ref):
    """Договор для серии «по другим движениям».

    У одной и той же серии написание в разных документах различается: где-то
    номер договора есть, где-то строка обрезана. Перебираем ВСЕ варианты —
    сначала из движений (по убыванию частоты), затем из справочника серий
    (поля «Серия» и «Проект»). Возвращает (договор, ключ, источник, лучшая_строка).
    """
    best_raw, contract, source = "", "", ""
    cands = []
    for raw, n in sorted((series_variants.get(ckey) or {}).items(),
                         key=lambda kv: -kv[1]):
        cands.append((raw, "движения"))
    ref = series_ref.get(ckey) or {}
    for raw in ref.get("variants", []):
        cands.append((raw, "справочник серий"))
    for raw, src in cands:
        if not best_raw: best_raw = raw
        c = extract_contract(raw)
        if c:
            # предпочитаем вариант, где договор реально нашёлся
            return c, contract_key(c), src, raw
    # договор не нашёлся — вернём самую информативную строку (с направлением)
    for raw, _ in cands:
        if "(" in raw and len(raw) > len(best_raw): best_raw = raw
    return "", "", "", best_raw


def marker_series(raw):
    """Служебные пометки происхождения без номера."""
    if not raw: return None
    u = raw.upper()
    if "ВВОД ОСТАТК" in u: return ("ВВОД ОСТАТКОВ (без серии)", "ввод")
    if "ИЗЛИШ" in u:       return ("ИЗЛИШКИ", "ввод")
    return None

def site_type(sklad):
    """Запасная эвристика по имени склада — если подразделения нет в справочнике."""
    if not sklad: return C.SITE_TYPE_DEFAULT
    for name, pat in C.SITE_TYPE_RULES:
        if pat.search(sklad): return name
    return C.SITE_TYPE_DEFAULT


# ---------------------------------------------------------------------------
# Производственные подразделения — АВТОРИТЕТНАЯ иерархия цех/база/заготовка
# ---------------------------------------------------------------------------
# Структура справочника: БАЗЫ → «База X» → «Цех Y» (цеха вложены в базы);
# отдельные корни «ПРОИЗВОДСТВО / ЗАГОТОВКА» (площадки заготовки: Когалым,
# Лангепас, Пермь, Юг…), «ДЕМОНТАЖ …», «АУП».
_CEH_NAME = re.compile(r"^\s*(цех|участок)\b", re.I)

def load_divisions(path):
    """Наименование подразделения -> {kind, base, analytical, root}."""
    if not path or not os.path.exists(path):
        return {}
    rows = list(csv.DictReader(read_text_lines(path)))
    parent, analytic = {}, {}
    for r in rows:
        n = (r.get("Наименование") or "").strip()
        if not n: continue
        parent[n] = (r.get("Родитель") or "").strip()
        analytic[n] = (r.get("АналитическоеПодразделение") or "").strip()

    def chain(n):
        """Цепочка вверх до корня (без циклов)."""
        out, seen = [], set()
        cur = n
        while cur and cur not in seen:
            seen.add(cur); out.append(cur)
            cur = parent.get(cur, "")
        return out

    out = {}
    for n in parent:
        ch = chain(n)
        root = ch[-1] if ch else ""
        # ближайший предок (включая себя) вида «База X»
        base = next((x for x in ch if re.match(r"^\s*База\b", x, re.I)), "")
        if root == "БАЗЫ":
            kind = "Цех" if _CEH_NAME.match(n) else "База"
        elif root.startswith("ПРОИЗВОДСТВО"):
            kind = "Заготовка"
        elif root.startswith("ДЕМОНТАЖ"):
            kind = "Демонтаж"
        elif root == "АУП":
            kind = "АУП"
        else:
            kind = "Прочее"
        # регион = звено сразу под корнем: БАЗЫ→«База X», ПРОИЗВОДСТВО→«Западная Сибирь»
        region = ch[-2] if len(ch) >= 2 else ""
        out[n] = {"kind": kind, "base": base or "", "region": region,
                  "analytical": analytic.get(n, "") or base or n, "root": root}
    return out


_BASE_PREFIX = re.compile(r"^\s*\d+\.\s*")

def normalize_base(name, canon=None):
    """Единое имя базы. Справочник складов зовёт её «5.База Осенцы»,
    «5.База МГМ (Камасталь)», справочник подразделений — «База Осенцы»,
    «База МГМ». Приводим к канону, иначе одна база двоится в отчётах.

    canon — множество эталонных имён из справочника подразделений."""
    if not name: return ""
    n = _BASE_PREFIX.sub("", str(name)).strip()
    n = re.sub(r"\s+", " ", n)
    if not n or n == "—": return ""
    if canon:
        if n in canon: return n
        # отбрасываем уточнение в скобках: «База МГМ (Камасталь)» -> «База МГМ»
        short = re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()
        if short in canon: return short
        # частичное совпадение по началу («База СВК (пос. Майский)» -> «База СВК»)
        for c in canon:
            if n.startswith(c) or short.startswith(c): return c
    return n


def base_canon_set(divisions):
    """Эталонные имена баз из справочника производственных подразделений."""
    return {d["base"] for d in (divisions or {}).values() if d.get("base")}


def classify_site(podr, sklad, divisions, sklad_top=None):
    """-> (тип площадки, база, аналитическое подразделение, источник, подпись).

    Приоритет — справочник производственных подразделений (авторитетно).
    Если подразделения там нет, но оно похоже на договорную площадку
    (в имени номер договора/БП) — это «Площадка». Иначе эвристика по складу.

    Подпись — то, что честно писать рядом с типом площадки. У базы и цеха это
    имя базы, у ЗАГОТОВКИ базы нет вообще: «Когалым КНПО» — заготовительная
    площадка региона Западная Сибирь, а не склад базы Когалым. Раньше базу ей
    подставлял справочник складов (ВысшийРодитель «5. База Когалым»), и в дереве
    получалось «Заготовка · База Когалым» — неверно по существу.
    """
    podr = (podr or "").strip()
    d = divisions.get(podr)
    if d:
        kind = d["kind"]
        if kind in ("База", "Цех"):
            label = d["base"] or d["analytical"] or podr
        elif kind == "Заготовка":
            label = " · ".join(x for x in (d.get("region"), podr) if x) or podr
        else:                                   # Демонтаж, АУП, прочее
            label = d["analytical"] or d["root"] or podr
        return kind, d["base"], d["analytical"], "справочник", label
    # СТАРЫЙ СКЛАД: подразделения в 1С нет (вписано имя самого склада), но есть
    # папка верхнего уровня в справочнике складов — она и задаёт тип с дивизионом.
    # Проверять ДО эвристики «договорная площадка»: иначе эти склады так и
    # останутся площадками, а их закупка — вне заготовки.
    top = (sklad_top or {}).get(sklad, "")
    if top:
        hit = C.TOP_FOLDER_SITE.get(top)
        if hit:
            kind, div = hit
            return kind, "", div, "папка складов", div + " · " + C.OLD_SKLAD_UNIT
        if C.TOP_FOLDER_BASE.match(top):
            base = normalize_base(top)
            return "База", top, base, "папка складов", base + " · " + C.OLD_SKLAD_UNIT
        # НОМЕР ПАПКИ задаёт тип: 1–4 заготовка, 5 базы, 6 ответхранение,
        # 7 собственные склады, 8–10 проектные цеха (см. C.TOP_FOLDER_BY_NUM).
        # Точные имена перечислены выше и имеют приоритет — там у заказчика свои
        # дивизионы; сюда попадает всё, что по имени не разобрано.
        mnum = C.TOP_FOLDER_NUM.match(top)
        if mnum:
            kind = C.TOP_FOLDER_BY_NUM.get(int(mnum.group(1)))
            if kind:
                div = (mnum.group(2) or "").strip() or kind
                return kind, "", div, "папка складов (номер)", \
                       div + " · " + C.OLD_SKLAD_UNIT
        # папка есть, но не разобрана — заготовка без дивизиона
        kind, div = C.TOP_FOLDER_DEFAULT
        return kind, "", div, "папка складов", C.OLD_SKLAD_UNIT
    # договорная площадка: номер договора или «БП N» в имени подразделения/склада
    for src in (podr, sklad):
        if src:
            c, bps = platform_info_from_sklad(src)
            if c or bps:
                return "Площадка", "", "Договорные площадки", "договор в имени", \
                       "договорная площадка"
    return site_type(sklad), "", "", "эвристика", ""

# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------
def load_nomenklatura(path):
    """name/guid -> {report_group, unit_base, unit_report, coef}."""
    r = csv.DictReader(read_text_lines(path))
    by_name, by_guid = {}, {}
    # ⚠️ Порядок предпочтения дублей номенклатуры МЕНЯТЬ НЕЛЬЗЯ: от него зависит,
    # какая единица измерения достанется коду, а значит и тоннаж. Добавление
    # сюда cargo_group сдвинуло «продано всего» на +2,85 т (517 808,885 ->
    # 517 811,735). Группа учёта берётся из той записи, что победила по этим
    # двум признакам, — на выбор записи она не влияет.
    def score(i): return (bool(i["report_group"]), bool(i["unit_report"]))
    for row in r:
        info = {
            "report_group": (row.get("НоменклатураОтчета") or "").strip(),
            # группа аналитического учёта (папка справочника): «Лом чёрных
            # металлов», «Лом цветных металлов», «ДХНО», «Труба …». По ней
            # фильтруются бизнес-планы: показать те, где такая номенклатура есть.
            "cargo_group": (row.get("НоменклатурнаяГруппаГрузов") or "").strip(),
            "unit_base": (row.get("ЕдиницаИзмерения") or "").strip().lower(),
            "unit_report": (row.get("ЕдиницаДляОтчетов") or "").strip().lower(),
            "coef": to_float(row.get("КоэффициентПересчета")) or 1.0,
        }
        name = (row.get("Номенклатура") or "").strip()
        guid = (row.get("НоменклатураГуид") or "").strip().upper()
        # дубли номенклатуры: берём более информативную строку
        if name and (name not in by_name or score(info) > score(by_name[name])):
            by_name[name] = info
        if guid and guid != "00000000-0000-0000-0000-000000000000":
            if guid not in by_guid or score(info) > score(by_guid[guid]):
                by_guid[guid] = info
    return by_name, by_guid

def load_series_ref(path):
    """-> (by_project, s2p, sg2p).

    by_project: ключ проекта -> {name, contragent, datavyvoza, variants, series}
    s2p:        нормализованная строка серии -> ключ проекта
    sg2p:       ГУИД СЕРИИ -> ключ проекта. Нужен там, где внешняя выгрузка
                ссылается на серию гуидом, а не строкой (комментарии по
                запасам): строку 1С пишут по-разному, гуид — один.

    Ключ проекта — ПроектГуид (стабилен), иначе нормализованное имя проекта.
    Варианты «… V» / «… VS» одной серии ведут в один проект."""
    r = csv.DictReader(read_text_lines(path))
    by_project, s2p, sg2p = {}, {}, {}
    for row in r:
        ser = (row.get("Серия") or "").strip()
        proj = (row.get("Проект") or "").strip()
        pguid = (row.get("ПроектГуид") or "").strip().upper()
        if not ser: continue
        if C.NON_SERIES.match(ser): continue        # «Объём №N» — не серия
        if not proj: proj = _VS_TAIL.sub("", ser).strip()
        if pguid in ("", "00000000-0000-0000-0000-000000000000"):
            pguid = "n:" + norm_series(proj)
        s2p[norm_series(ser)] = pguid
        s2p.setdefault(norm_series(proj), pguid)
        sguid = (row.get("СерияГуид") or "").strip().upper()
        if sguid and not sguid.startswith("00000000"):
            sg2p[sguid] = pguid

        contr = (row.get("Контрагент") or "").strip()
        vyv = (row.get("ДатаВывоза") or "").strip()[:10]
        if vyv.startswith("0001"): vyv = ""
        cur = by_project.get(pguid)
        if cur is None:
            cur = by_project[pguid] = {"name": proj, "contragent": contr,
                                       # ВСЕ контрагенты проекта, а не только
                                       # первый: у проекта бывает несколько серий,
                                       # и фильтр по контрагенту обязан находить
                                       # план по любому из них
                                       "contragents": [],
                                       "datavyvoza": vyv, "variants": [], "series": [],
                                       # план по каждой серии проекта: серия -> дата
                                       # вывоза. Нужен диаграмме Ганта: у проекта
                                       # серий бывает несколько, и сроки у них разные.
                                       "dates": {}}
        else:
            if contr and not cur["contragent"]: cur["contragent"] = contr
            if vyv and not cur["datavyvoza"]: cur["datavyvoza"] = vyv
        if contr and contr not in cur["contragents"]: cur["contragents"].append(contr)
        if vyv:
            cur["dates"][ser] = vyv
        for v in (ser, proj):
            if v and v not in cur["variants"]: cur["variants"].append(v)
        if ser not in cur["series"]: cur["series"].append(ser)

    # Ведущий номер -> проект. Серия в движениях часто написана как проект
    # «плюс несколько символов»: «1578 … (ЗС-УВМ) ДС35» -> «… (ЗС-УВМ-Армада) ДС35»,
    # «1810 … пер. 181», «1743 Договор № … г.». Точное совпадение строки такие
    # варианты не ловит, а ведущий номер — ловит.
    # ВАЖНО: только когда номер ОДНОЗНАЧЕН. У 12 номеров (1291, 197, 2655…)
    # за одним числом стоят десятки разных спецификаций — их склеивать нельзя.
    lead = defaultdict(set)
    for norm, pk in s2p.items():
        k = canon_key(norm)
        if k: lead[k].add(pk)
    for k, pks in lead.items():
        if len(pks) == 1:
            s2p["lead:" + k] = next(iter(pks))

    # (договор + номер БП) -> проект. Пока серий не заводили, движения шли по
    # ДОГОВОРНОЙ ПЛОЩАДКЕ, и весь признак бизнес-плана — в имени склада:
    # «22ESP0397К БП 1001». Позже тому же БП завели нормальную серию
    # «1001 (22ESP0397К от 08.06.22 г. (ЭПУ))». Это один и тот же бизнес-план,
    # поэтому старые движения сводим с новой серией: ключ — номер договора плюс
    # ведущий номер серии, он же номер БП. Совпадение проверяется по справочнику,
    # неоднозначных пар нет.
    for pk, rec in by_project.items():
        for raw in [rec["name"]] + list(rec["variants"]):
            lead_no = canon_key(raw)
            if not lead_no:
                continue
            c = extract_contract(raw)
            if not c:
                continue
            ck = contract_key(c)
            for part in lead_no.split("/"):
                s2p.setdefault("bp:%s:%s" % (ck, part), pk)
    return by_project, s2p, sg2p


def platform_project(name, s2p):
    """Договорная площадка -> проект (бизнес-план) из справочника серий.

    «22С0371 … Пок. БП 832ДС1» -> «832 (22С0371 от 11.03.22 г. (ЗС-УВМ))».
    Сводим только однозначное: сначала по паре «договор + номер БП», затем —
    если договора в имени нет — по номеру БП, но лишь когда он однозначен во
    всём справочнике. Несколько разных проектов -> не склеиваем.
    """
    if not s2p:
        return None
    contract, bps = platform_info_from_sklad(name)
    if not bps:
        return None
    ck = contract_key(contract) if contract else ""
    if ck:
        hits = {s2p.get("bp:%s:%s" % (ck, b)) for b in bps}
        hits.discard(None)
        if len(hits) == 1:
            return next(iter(hits))
        if hits:
            return None                       # разные проекты — склеивать нельзя
    hits = {s2p.get("lead:" + b) for b in bps}
    hits.discard(None)
    return next(iter(hits)) if len(hits) == 1 else None

def load_sklady(path):
    """-> (склад -> база «5.», склад -> родитель, склад -> ПАПКА верхнего уровня).

    Папка верхнего уровня нужна старым складам: подразделения в 1С у них нет,
    и тип с дивизионом определяются по ней (см. C.TOP_FOLDER_SITE)."""
    r = csv.DictReader(read_text_lines(path))
    sklad2base, sklad2parent, sklad2top = {}, {}, {}
    for row in r:
        name = (row.get("Склад") or "").strip()
        top = (row.get("ВысшийРодитель") or "").strip()
        parent = (row.get("Родитель") or "").strip()
        sklad2parent[name] = parent
        if top: sklad2top[name] = top
        base = None
        if top.startswith("5"):
            base = top
        else:
            for pat, b in C.CITY_TO_BASE:
                if pat.search(name) or (parent and pat.search(parent)):
                    base = b; break
            if base is None:
                if re.search(r"\(коми\)", name, re.I): base = C.FALLBACK_BASE_KOMI
                elif re.search(r"\bЗС\b|ЗапСиб", name, re.I): base = C.FALLBACK_BASE_ZS
        if base: sklad2base[name] = base
    return sklad2base, sklad2parent, sklad2top

# --- договорная площадка: в имени склада есть И договор, И номер(а) БП ------
# Примеры имён:
#   «22С0371 от 11.03.22 г. (ЗС-УВМ) Сов. БП 386»      -> 22С0371 + БП 386
#   «22С0371 от 11.03.22 г. (ЗС-УВМ) Ланг.БП236 248 249» -> 22С0371 + БП 236,248,249
#   «№22Y0048 от 28.02.2022 г. (КОМИ) БП 170»          -> 22Y0048 + БП 170
#   «12_КОМИ ЛОМ (21Y2503) БП 144»                     -> 21Y2503 + БП 144
_BP_NUMS = re.compile(r"БП\s*((?:\d+[\s,]*)+)", re.I)
# договор в скобках, когда имя начинается с кода склада («8_КОМИ ЛОМ зимник ( 21Y0258)»)
_PAREN_CONTRACT = re.compile(r"\(\s*№?\s*(\d{2}[A-Za-zА-Яа-я]{1,4}\d{3,6}[A-Za-zА-Яа-я]?)\s*\)")

def platform_info_from_sklad(name):
    """-> (договор, [номера БП]). Пусто, если имя склада не договорное."""
    if not name: return "", []
    bps = []
    m = _BP_NUMS.search(name)
    if m:
        bps = re.findall(r"\d+", m.group(1))
    contract = extract_contract(name, strip_lead=False)
    if not contract:
        p = _PAREN_CONTRACT.search(name)
        if p: contract = p.group(1)
    # «БП 386» само по себе договором не является
    if contract and re.fullmatch(r"(?i)бп\d*", contract):
        contract = ""
    return contract, bps

def platform_series(name):
    """Псевдо-серия площадки: ключ, отображение и договор.
    Номер БП в имени склада и есть идентификатор партии для таких продаж
    (серии в 1С тогда не заводили) — но настоящей серией 1С он не является."""
    contract, bps = platform_info_from_sklad(name)
    if not contract and not bps:
        return None, "", ""
    ck = contract_key(contract)
    bp = "/".join(bps)
    key = "площадка:" + ck + ("·БП" + bp if bp else "")
    disp = ((contract + " " if contract else "") + ("БП " + bp if bp else "")).strip()
    return key, disp, contract

# ---------------------------------------------------------------------------
# Остатки (xlsx)
# ---------------------------------------------------------------------------
def load_stock_xlsx(path):
    """Остатки на складах -> список строк + дата актуальности из имени файла."""
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
            "series_raw": str(g(row, "Серия") or "").strip() if g(row, "Серия") else "",
            "qty": to_float(g(row, "Начальный остаток")),
        })
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", os.path.basename(path))
    actual = datetime(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    return rows, actual


def load_stock_csv(path, nom_by_name=None, nom_by_guid=None):
    """Снимок остатков 1С «СебестоимостьТоваровОстатки_*.csv» -> те же строки,
    что и `load_stock_xlsx`, плюс дата снимка из колонки «ДатаВыгрузки».

    Зачем отдельный загрузчик: прежний якорь (`Остатки на складах *.xlsx`) —
    фильтрованный отчёт на 91 склад из 1 852, сверять по нему почти нечего.
    Этот снимок покрывает 275 складов с ненулевым остатком, несёт серию,
    партию и складскую территорию.

    ⚠️ В ФАЙЛЕ НЕТ КОЛОНКИ ЕДИНИЦЫ ИЗМЕРЕНИЯ, и это правильно: единица берётся
    из справочника номенклатуры (по ГУИДу, иначе по имени), а спорные коды
    перечислены в `C.UNIT_BY_CODE`. Раньше единицу давал файл остатков, то есть
    ЭТАЛОН СВЕРКИ влиял на измеряемое; с 12.08.2026 эта связь разорвана.

    ⚠️ Остаток берётся ПОСТРОЧНО и суммируется: в снимке одна и та же тройка
    «склад · код · серия» встречается несколькими строками (разные партии).
    """
    import config as _C
    rd = csv.reader(read_text_lines(path))
    hdr = next(rd)
    I = {h: i for i, h in enumerate(hdr)}
    need = ("АналитикаУчетаНоменклатурыСкладскаяТерритория",
            "АналитикаУчетаНоменклатурыНоменклатураКод",
            "АналитикаУчетаНоменклатурыНоменклатура",
            "КоличествоОстаток")
    miss = [n for n in need if n not in I]
    if miss:
        raise ValueError("в выгрузке остатков нет колонок: " + ", ".join(miss))

    def g(row, key):
        i = I.get(key)
        return row[i] if i is not None and i < len(row) else ""

    rows, actual = [], None
    for row in rd:
        code = (g(row, "АналитикаУчетаНоменклатурыНоменклатураКод") or "").strip()
        if not code:
            continue
        name = (g(row, "АналитикаУчетаНоменклатурыНоменклатура") or "").strip()
        guid = (g(row, "АналитикаУчетаНоменклатурыНоменклатураГуид") or "").strip().upper()
        info = ((nom_by_guid or {}).get(guid) or (nom_by_name or {}).get(name) or {})
        unit = (_C.UNIT_BY_CODE.get(code) or info.get("unit_base", "") or "").lower()
        if actual is None:
            actual = parse_dt(g(row, "ДатаВыгрузки"))
        rows.append({
            "sklad": (g(row, "АналитикаУчетаНоменклатурыСкладскаяТерритория") or "").strip(),
            "unit": unit,
            "code": code,
            "podr": (g(row, "АналитикаУчетаНоменклатурыСкладскаяТерриторияПодразделение") or "").strip(),
            "name": name,
            "series_raw": (g(row, "АналитикаУчетаНоменклатурыСерия") or "").strip(),
            "qty": to_float(g(row, "КоличествоОстаток")),
        })
    return rows, actual

# ---------------------------------------------------------------------------
# Один проход по регистру оборотов
# ---------------------------------------------------------------------------
PROD_TYPES = ("Документ.ПроизводствоБезЗаказа", "Документ.СборкаТоваров",
              "Документ.ОтчетПереработчика2_5")

def _norm_partia(p):
    """Описание партии -> ключ «тип документа + номер + дата».

    ВАЖНО: номер БЕЗ типа не уникален. Реальный случай: «Приобретение товаров
    и услуг РУМЛ-000146» (закупка трубы 1578 V в Когалыме) и «Производство без
    заказа РУМЛ-000146» (выпуск 64.5 т лома на Усинске) — РАЗНЫЕ документы с
    одним номером. Кроме того, номера документов повторяются в разные даты:
    так «Сборка РУ00-004321» от 15.07.2024 (БП 1237, Осенцы) склеивалась с
    усинской сборкой 2025 года БП 1491/1493. Поэтому серия Усинска ошибочно
    появлялась в Осенцах, Осе и Березниках. Дата является частью ключа."""
    if not p: return ""
    m = re.search(r"(РУМЛ-\d+|РУ00-\d+|РУУ-\d+|РУ\d+-\d+|МЛ\d+-\d+|СУМЛ-\d+)", p)
    if m:
        # первое слово описания = тип документа («Приобретение», «Производство»…)
        w = re.match(r"\s*([А-Яа-яA-Za-z]+)", p)
        pref = (w.group(1)[:4].lower() + ":") if w else ""
        dm = re.search(r"\bот\s+(\d{2})\.(\d{2})\.(\d{4})\b", p)
        if dm:
            doc_date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        else:
            dm = re.search(r"\bот\s+(\d{4})-(\d{2})-(\d{2})\b", p)
            doc_date = "-".join(dm.groups()) if dm else ""
        return pref + m.group(1) + (("@" + doc_date) if doc_date else "")
    return re.sub(r"\s+от\s+\d.*$", "", p).strip()


def work_kind(raw):
    """«Резка ручная (газокислородная)» -> «резка», «Сортировка ручная» ->
    «сортировка». Пусто, если 1С вид работ не проставила."""
    t = (raw or "").strip().lower()
    if not t:
        return C.WORK_KIND_DEFAULT
    for kind, keys in C.WORK_KIND:
        if any(k in t for k in keys):
            return kind
    return "производство"          # вид работ есть, но незнакомый — не «резка»


def lot_display(pnorm):
    """Ключ лота -> номер для показа (без служебного префикса типа)."""
    if not pnorm: return ""
    shown = pnorm.split(":", 1)[1] if ":" in pnorm else pnorm
    return shown.split("@", 1)[0]


def lot_type(pnorm):
    """Ключ лота -> тип документа-родителя лота («прио», «прои», «пере»…)."""
    if not pnorm: return ""
    return pnorm.split(":", 1)[0] if ":" in pnorm else ""


# ---------------------------------------------------------------------------
# Строка движения. Класс со слотами вместо dict: движений ~400 тыс., и на
# словарях по 18 ключей уходит втрое больше памяти. Интерфейс оставлен
# словарным (fr["flow"], fr.get(...)) — весь остальной код не меняется.
# ---------------------------------------------------------------------------
class FlowRow:
    __slots__ = ("rid", "flow", "qty", "code", "ckey", "partia_norm", "sklad",
                 "ptype", "series_raw", "regnum", "name", "podr", "guid",
                 "dt", "ts", "cp", "so", "sp", "org", "wk")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __getitem__(self, k):
        return getattr(self, k)

    def get(self, k, default=None):
        v = getattr(self, k, default)
        return default if v is None else v


# Потоки, которые РЕАЛЬНО тратят лот: продажа, возврат, недостача, свои нужды и
# резка (сырьё превращается в новую номенклатуру = новый лот). Именно они делят
# ёмкость смешанного лота между сериями (§4.3).
#
# ⚠️ «Уехало» сюда НЕ входит, хотя это тоже расход. Перемещение НЕ тратит лот:
# металл уезжает и тем же лотом приезжает на другой склад, где потом продаётся.
# Если считать перемещение расходом лота, ёмкость выедается вдвое: у лота
# «Приобретение РУМЛ-000146 / 00000005139» (0.756 серии 1578 + 0.008 серии 1660)
# «уехало» уже забирало все 0.764, и продажи 0.444 + 0.008 + 0.312 оставались
# без ёмкости — раскладка вырождалась в доминирующую серию. Тот же лот может
# переезжать несколько раз, поэтому лимита у перемещений нет вообще: серия для
# них берётся из карты лота.
OUTFLOWS = ("продано", "переработка_забрали", "возврат", "списано",
            "списано_на_затраты", "внутренний_оборот")

EPS = 5e-4      # 0.5 кг: точность количества в выгрузке — 3 знака


# ---------------------------------------------------------------------------
# СМЕШАННЫЕ ЛОТЫ: раскладка расхода по компонентам БЕЗ дробных хвостов
# ---------------------------------------------------------------------------
def build_mix_alloc(flow_rows, part_mix, part_comp_qty,
                    inherited_keys=None, preferred=None):
    """Разложить расход из смешанных лотов по сериям так, чтобы в документах
    не появлялось сумм, которых нет в 1С.

    Пропорциональное деление даёт правильные ИТОГИ, но размазывает документы:
    в одном документе появляются строки 0.439 / 0.008 / 0.005 — «лишние 8 кг»,
    которых в 1С нет. Поэтому порядок такой (§4.3 ТЗ):

      1. точное количество — строка продажи 0.008 забирает компонент 0.008;
      2. аллокация по ёмкости — компонент, у которого хватает НЕИЗРАСХОДОВАННОГО
         остатка, отдаёт строку целиком (FIFO: строки идут по дате);
      3. и только если строка не влезает ни в один компонент — режем её по
         остаткам ёмкости и помечаем бейджем «доля» (видно в «Проблемах»).

    Строки со СВОЕЙ серией 1С аллокации не подлежат, но ёмкость своей
    компоненты уменьшают: иначе восстановленные строки заберут уже проданное.

    -> (alloc: rid -> [(серия, кол-во, бейдж)], notes: список проблемных лотов)
    """
    inherited_keys = inherited_keys or set()
    preferred = preferred or {}
    # ёмкость компонент = доля состава × весь приход лота (в единицах 1С)
    caps0 = {}
    for key, mix in part_mix.items():
        total = sum((part_comp_qty.get(key) or {}).values())
        if total <= 0:
            continue
        caps0[key] = {ser: total * frac for ser, frac in mix.items()}

    rows_by_lot = defaultdict(list)
    for fr in flow_rows:
        if fr.flow not in OUTFLOWS or fr.qty <= 0:
            continue
        key = (fr.partia_norm, fr.code)
        if key in caps0:
            rows_by_lot[key].append(fr)

    alloc, notes = {}, []
    for key, rows in rows_by_lot.items():
        cap = dict(caps0[key])
        pend = []
        for fr in sorted(rows, key=lambda r: (r.dt or "", r.rid)):
            if fr.ckey:                      # своя серия 1С — только тратит ёмкость
                if fr.ckey in cap:
                    cap[fr.ckey] -= fr.qty
                continue
            if key in inherited_keys and fr.flow == "переработка_забрали":
                # Этот расход и без аллокатора наследуется пропорционально
                # составу лота. Резервируем те же доли в ёмкости, но не меняем
                # саму строку. Иначе продажи вытесняли сырьё следующей резки в
                # чужой БП и массово меняли уже сверенный «продано».
                for ser, frac in part_mix[key].items():
                    cap[ser] = cap.get(ser, 0.0) - fr.qty * frac
                continue
            pend.append(fr)

        for fr in pend:
            q = fr.qty
            pref = preferred.get(key) if key in inherited_keys else None
            # Унаследованный смешанный выпуск исправляем МИНИМАЛЬНО: пока
            # ёмкости доминирующего БП хватает, сохраняем прежнюю атрибуцию.
            # Делим только строку, которая иначе сделала бы его остаток
            # отрицательным. Это исправляет РУМЛ-003097 (8,380 + 0,620 = 9,000),
            # не перераспределяя тысячи уже сверенных продаж между БП.
            if pref is not None and cap.get(pref, 0.0) >= q - EPS:
                cap[pref] -= q
                alloc[fr.rid] = [(pref, q, "переработка")]
                continue
            # 1) точное количество
            hit = None
            if key not in inherited_keys:
                for ser, v in sorted(cap.items(), key=lambda kv: -kv[1]):
                    if abs(v - q) <= EPS:
                        hit = ser; break
            # 2) ёмкости хватает целиком — отдаём строку одной серии
            if hit is None and key not in inherited_keys:
                fit = [(v, ser) for ser, v in cap.items() if v >= q - EPS]
                if fit:
                    # В составе унаследованного лота может быть компонент None
                    # («часть без серии»); при равных ёмкостях str и None нельзя
                    # сравнивать между собой.
                    hit = max(fit, key=lambda x: x[0])[1]
            if hit is not None:
                cap[hit] -= q
                alloc[fr.rid] = [(hit, q, "док")]
                continue
            # 3) не влезает ни в один компонент — режем по остаткам ёмкости
            parts, left = [], q
            order = sorted(cap.items(), key=lambda kv: (kv[0] != pref, -kv[1]))
            for ser, v in order:
                if v <= EPS or left <= EPS:
                    continue
                take = min(v, left)
                parts.append([ser, take]); cap[ser] -= take; left -= take
            # Остаток до 0,5 кг возникает из плавающих долей. Он меньше EPS,
            # но терять его нельзя: на тысячах смешанных строк набегало 3,9 кг,
            # и автопроверка «позиции = сырые продажи» падала. Возвращаем хвост
            # первой компоненте, сохраняя сумму строки в точности.
            if 1e-12 < left <= EPS and parts:
                parts[0][1] += left
                cap[parts[0][0]] = cap.get(parts[0][0], 0.0) - left
                left = 0.0
            dom = max(caps0[key].items(), key=lambda kv: kv[1])[0]
            if left > EPS or not parts:      # ёмкость лота исчерпана
                if parts and parts[0][0] == dom:
                    parts[0][1] += left
                else:
                    parts.append([dom, left])
                cap[dom] = cap.get(dom, 0.0) - left
                notes.append({"lot": lot_display(key[0]), "code": key[1],
                              "regnum": fr.regnum, "qty": round(q, 3),
                              "reason": "ёмкость лота исчерпана — остаток отдан "
                                        "доминирующей серии"})
            else:
                notes.append({"lot": lot_display(key[0]), "code": key[1],
                              "regnum": fr.regnum, "qty": round(q, 3),
                              "reason": "строка не совпала ни с одним компонентом "
                                        "лота — разделена по остаткам ёмкости"})
            # Части НЕ округляем: сумма частей обязана в точности равняться
            # количеству строки, иначе итог позиций разойдётся с прямым
            # пересчётом по строкам (автопроверка §11 ловит это на 8 кг).
            alloc[fr.rid] = [(ser, v, "доля") for ser, v in parts if abs(v) > 1e-12]
    return alloc, notes

def scan_movements(path, anchor=None, divisions=None, s2p=None, sklad_top=None,
                   tonnes_of=None):
    """Единственный проход по 294k строк. Возвращает всё, что нужно дальше."""
    s2p = s2p or {}
    r = csv.DictReader(read_text_lines(path))
    sales = []                                # строки реализации (внешние)
    internal_sales = []                       # реализация своей же организации
    part_map_votes = defaultdict(Counter)     # (партия,код) -> Counter(серия) по весу прихода
    part_var_votes = defaultdict(Counter)     # (партия,код) -> Counter(ТОЧНАЯ строка серии)
    part_var_ser_votes = defaultdict(Counter) # (партия,код,серия) -> Counter(строка серии)
    # ⚠️ В ключ обеих карт входит ДАТА. 1С нумерует документы заново каждый год:
    # из 23 312 номеров перемещений 8 133 встречаются на РАЗНЫХ датах, и ключ
    # «номер + код» склеивал чужие движения — 1 027 таких ключей на 44 718 т.
    # Реальный случай: «Перемещение РУ00-003592» от 06.05.2024 (Осенцы, серии нет)
    # получал серию «БП 1476 24z4272 от 18.12.2024 г.» от документа с тем же
    # номером от 17.04.2025 — серия, заведённая ПОЗЖЕ самого движения.
    # То же правило, что у ключа лота документов переработки (§4.2).
    move_pair = defaultdict(Counter)          # (документ, дата, код) -> Counter(серия) по расходу
    move_in   = defaultdict(Counter)          # то же по ПРИХОДУ, только запись 1С
    sklad_series_votes = defaultdict(Counter) # склад -> Counter(серия) по строкам 1С
    plat_proj = {}                            # договорная площадка -> проект (БП)
    prod_out = defaultdict(set)               # regnum -> {выпущенные лоты}
    prod_in  = defaultdict(list)              # regnum -> [(потреблённый лот, вес)]
    # ПРОСТОЙ ДОКУМЕНТ ПЕРЕРАБОТКИ (см. C.REWORK_SIMPLE_SAME_BASE): считаем
    # склады и количество выпуска по ВСЕМ приходным строкам документа, а не
    # только по тем, у которых есть лот, — иначе сравнение с сырьём хромает.
    prod_out_w   = defaultdict(set)           # документ -> склады выпуска
    prod_out_qty = defaultdict(float)         # документ -> количество выпуска
    prod_out_t   = defaultdict(float)         # (документ, партия, код, склад) -> тонны
    prod_in_t    = defaultdict(float)         # документ -> тонны сырья
    balance = {}                              # (склад,код) -> агрегаты прихода/расхода
    # (склад,код) -> Counter(серия) по приходам. Замена ступени «серия из остатков
    # на 01.03.2024»: файла остатков на начало периода заказчик не предоставил,
    # поэтому старый лот опознаём по ЕДИНСТВЕННОЙ серии, когда-либо приходившей
    # на этот склад под этим кодом. Бейдж «вероятно» — требует проверки.
    sklad_code_series = defaultdict(Counter)
    # Где серия ФАКТИЧЕСКИ была: (серия, склад) по приходам с ЯВНОЙ серией 1С.
    # Наследовать серию на складе, куда она никогда не поступала, нельзя —
    # именно так «Лом 12АЦ» на СВК (Майский) попадал в БП 1578 (труба из ЗапСиба).
    series_at = set()
    lot_sklad = defaultdict(set)   # (партия,код) -> склады, где лот вообще был
    # ⚠️ Ключ лота хранит ТИП документа четырьмя буквами («прио:РУ00-000481@…»),
    # и по ним человеку не прочесть, что это за документ. Собираем расшифровку
    # «прио» -> «Приобретение товаров и услуг» прямо из сырых описаний партии:
    # так подпись лота в интерфейсе совпадает с тем, что видно в самой 1С.
    lot_type_word = defaultdict(Counter)
    # Записи жизненного цикла партии: куплено / продано / возврат / списано / …
    # Храним компактно; серию восстановим тем же каскадом ПОСЛЕ построения карт.
    flow_rows = []
    purchased = defaultdict(float)            # серия -> куплено (ед.) по «Приобретение»
    purchased_rows = 0
    series_repr = {}                          # ckey -> представительная сырая строка
    # ckey -> Counter(сырая строка серии). Собираем ВСЕ варианты написания,
    # встреченные где угодно в выгрузке (инлайн-серия + колонки мпроСпецификации
    # на перемещениях), чтобы вытащить договор «по другим движениям»: у одной
    # серии часть строк без договора, а часть — с ним.
    series_variants = defaultdict(Counter)
    series_variants_own = defaultdict(Counter)   # только из самих строк движений
    reg_kinds = Counter()                     # тип регистратора -> строк
    rashod_by_kind = Counter()                # тип регистратора -> расход (ед.)
    unit_hint = {}                            # код -> единица (из остатков позже)
    n = 0
    period_min = period_max = None
    # ДАТА ВЫГРУЗКИ — колонка «ДатаВыгрузки» самой таблицы (заказчик 14.09.2026:
    # «чтобы понимать, на какое число сформированы остатки; если выгрузка
    # сломалась — видеть, что данные старые»). Это момент, когда Extractor снял
    # регистр, а не дата последнего движения: движений могло не быть неделю,
    # и period_max тогда врёт о свежести.
    dump_dt = None

    for row in r:
        n += 1
        if n <= 1000 or n % 5000 == 0:      # колонка одна на весь файл, читать каждую строку незачем
            _dd = parse_dt(row.get("ДатаВыгрузки"))
            if _dd and (dump_dt is None or _dd > dump_dt): dump_dt = _dd
        dt = parse_dt(row.get("Период"))
        if dt is None: continue
        if C.CUTOFF_START and dt < parse_dt(C.CUTOFF_START): continue
        if period_min is None or dt < period_min: period_min = dt
        if period_max is None or dt > period_max: period_max = dt

        reg     = (row.get("Регистратор") or "").strip()
        code    = (row.get("АналитикаУчетаНоменклатурыНоменклатураКод") or "").strip()
        name    = (row.get("АналитикаУчетаНоменклатурыНоменклатура") or "").strip()
        s_raw   = (row.get("АналитикаУчетаНоменклатурыСерия") or "").strip()
        sklad   = (row.get("АналитикаУчетаНоменклатурыСкладскаяТерритория") or "").strip()
        podr    = (row.get("АналитикаУчетаНоменклатурыСкладскаяТерриторияПодразделение") or "").strip()
        regnum  = (row.get("РегистраторНомер") or "").strip()
        partia  = (row.get("Партия") or "").strip()
        ptype   = (row.get("ПартияТип") or "").strip()
        org     = (row.get("Организация") or "").strip()
        kontr   = (row.get("РегистраторКонтрагент") or "").strip()
        guid    = (row.get("АналитикаУчетаНоменклатурыНоменклатураГуид") or "").strip().upper()
        # склады-контрагенты перемещения: нужны, чтобы движение читалось
        # однозначно «уехало со склада X → приехало на склад Y» (§5 ТЗ)
        sklad_ot = (row.get("РегистраторСкладОтправитель") or "").strip()
        sklad_pol = (row.get("РегистраторСкладПолучатель") or "").strip()
        prihod  = to_float(row.get("КоличествоПриход"))
        rashod  = to_float(row.get("КоличествоРасход"))
        ckey    = series_key(s_raw, s2p)
        pnorm   = _norm_partia(partia)
        if pnorm and ":" in pnorm:
            _w = re.split(r"\s*(?:РУМЛ-|РУ00-|РУУ-|РУ\d+-|МЛ\d+-|СУМЛ-)", partia, 1)[0]
            _w = re.sub(r"\s+", " ", _w).strip()
            if _w:
                lot_type_word[pnorm.split(":", 1)[0]][_w] += 1
        kind    = reg.split(" РУ")[0].split(" МЛ")[0].split(" 0000-")[0].strip()[:60]

        reg_kinds[kind] += 1
        if rashod > 0: rashod_by_kind[kind] += rashod
        # документ переработки определяется РЕГИСТРАТОРОМ, а не типом партии
        is_prod_doc = any(reg.startswith(x) for x in C.FLOWS_REWORK)
        # ВИД РАБОТ производственного документа: «Резка ручная», «Сортировка
        # ручная», «Разделка кабеля»… 1С заполняет его только у «Производства
        # без заказа», поэтому у остальных потоков он пуст — и это нормально.
        wkind = work_kind(row.get("РегистраторВидРаботОбщий"))
        dts = dt.strftime("%Y-%m-%d")

        # агрегаты для сверки баланса по ключу (склад, код)
        b = balance.get((sklad, code))
        if b is None:
            b = balance[(sklad, code)] = {"name": name, "prihod": 0.0, "rashod": 0.0,
                                          "prihod_pre": 0.0, "rashod_pre": 0.0,
                                          "sale": 0.0}
        b["prihod"] += prihod; b["rashod"] += rashod
        if anchor and dt < anchor:
            b["prihod_pre"] += prihod; b["rashod_pre"] += rashod
        if not b["name"] and name: b["name"] = name

        if sklad and sklad not in plat_proj:
            plat_proj[sklad] = platform_project(sklad, s2p)

        if code and pnorm and (prihod > 0 or rashod > 0):
            lot_sklad[(pnorm, code)].add(sklad)
        if ckey and sklad:
            series_at.add((ckey, sklad))      # серия видна на складе в любую сторону
            # закреплён ли склад за одной серией — считаем по ВСЕМ строкам 1С,
            # где серия проставлена (и приход, и расход)
            sklad_series_votes[sklad][ckey] += prihod + rashod

        # представительная сырая строка серии (для договора/направления)
        if ckey:
            cur = series_repr.get(ckey)
            if cur is None or ("(" not in cur and "(" in s_raw):
                series_repr[ckey] = s_raw
            series_variants[ckey][s_raw] += 1
            # ⚠️ Отдельно копим варианты, встреченные В САМИХ СТРОКАХ движений.
            # series_variants ниже дополняется вариантами со спецификаций складов,
            # и у проекта, где в движениях строка серии РОВНО ОДНА, счётчик мог
            # показать две — тогда правило «одна серия на проект» не срабатывало,
            # и восстановленные каскадом строки висели отдельным узлом «серия не
            # указана» вместо того чтобы слиться со своей серией. Таких позиций
            # 2 113 на 172 752 т.
            series_variants_own[ckey][s_raw] += 1
        # варианты строки серии со спецификаций перемещений — ещё один источник
        for col in ("РегистраторСкладОтправительмпроСпецификацияСерия",
                    "РегистраторСкладПолучательмпроСпецификацияСерия"):
            v = (row.get(col) or "").strip()
            if v:
                vk = series_key(v, s2p)
                if vk: series_variants[vk][v] += 1

        # карта (Партия+Код -> серия): голосуем весом прихода.
        # ⚠️ Пустое описание партии — это НЕ лот. Ключ («», код) склеивал все
        # безпартийные приходы этого кода по всей базе (13 тыс. т на один ключ),
        # и такая «привязка по документу» была фикцией: она означала лишь
        # «где-то когда-то этот код приходил с такой серией».
        if ckey and prihod > 0:
            if pnorm:
                part_map_votes[(pnorm, code)][ckey] += prihod
                part_var_votes[(pnorm, code)][s_raw] += prihod
                # То же, но с привязкой к КОНКРЕТНОЙ серии лота. Иначе у смешанного
                # лота строка серии 1660 (8 кг) подписывалась самым тяжёлым
                # написанием лота — «1578 … ДС35 V», то есть чужой серией.
                part_var_ser_votes[(pnorm, code, ckey)][s_raw] += prihod
            sklad_code_series[(sklad, code)][ckey] += prihod
            series_at.add((ckey, sklad))

        # Структура переработки. Склад запоминаем: если сырьё поступило на склад
        # и там же переработано, то и выпуск принадлежит этому складу — наследовать
        # надо от сырья ТОГО ЖЕ склада, а не от всего документа.
        # (14 762 из 19 430 документов переработки растянуты на несколько складов.)
        # ⚠️ Документ переработки опознаём по РЕГИСТРАТОРУ и ключу «тип + номер».
        #
        # Было две ошибки сразу. Первая: строки отбирались по ТИПУ ПАРТИИ
        # («партия — сборка/производство»), а это совсем другое: под такое условие
        # попадала, например, «Реализация РУ00-010313 … партия Сборка РУ00-006483»
        # — строка ПРОДАЖИ, а не сырьё переработки. Вторая: ключом был голый номер
        # документа, а номера повторяются у разных типов. В итоге «Перемещение
        # РУ00-010313» (04.10.2024, Осенцы) и «Реализация РУ00-010313» (05.12.2025,
        # Когалым, серия 1578) склеивались в один «документ переработки»: выпуск
        # 6.670 т трубы 89*6,5 на Осенцах получал серию 1578 из продажи, которая
        # прошла годом позже за 900 км. Сырьём этой резки была «Труба НКТ 89 мм б/у»
        # вообще без серии — наследовать было нечего.
        if is_prod_doc:
            # В ключ входит и ДАТА: «Сборка (разборка) РУ00-006583» существует
            # и 09.11.2023 (площадка 22С0194), и 16.12.2025 (Осенцы) — 1С нумерует
            # документы заново, и без даты это снова были бы «один документ».
            dkey = (kind, regnum, dts)
            # ТОННАЖ сторон — для правила «выпуск без тоннажа не наследует серию
            # у тоннажного сырья» (C.INHERIT_NEEDS_TONNAGE). Считает его
            # sales_report: справочник номенклатуры и единицы живут там.
            _tn = abs(tonnes_of(prihod or rashod, code, name, guid)) if tonnes_of else 0.0
            if prihod > 0:
                prod_out_w[dkey].add(sklad)
                prod_out_qty[dkey] += prihod
                if pnorm:
                    prod_out[dkey].add((pnorm, code, sklad))
                    prod_out_t[(dkey, pnorm, code, sklad)] += _tn
            if rashod > 0:
                # сырьё без лота тоже учитываем: своей серии оно не даёт, но
                # РАЗБАВЛЯЕТ смесь — доля считается от ВСЕГО сырья (§4.2)
                prod_in[dkey].append(((pnorm, code) if pnorm else None,
                                      rashod, sklad, ckey, _tn))
                prod_in_t[dkey] += _tn

        # куплено по серии (для тождества «1000 − (1+2+3+4) = 0»)
        if reg.startswith("Приобретение товаров и услуг") and prihod > 0:
            purchased_rows += 1
            purchased[ckey or "БЕЗ СЕРИИ"] += prihod

        # ---- жизненный цикл партии: классифицируем строку по потоку ----
        # Каждая строка может дать ДВЕ записи (перемещение: уехало И приехало),
        # поэтому копим список.
        flows_here = []
        flow = None
        if is_prod_doc:
            if rashod > 0: flows_here.append(("переработка_забрали", rashod))
            if prihod > 0: flows_here.append(("переработка_вернули", prihod))
        elif reg.startswith(C.FLOW_MOVE):
            if rashod > 0:
                flows_here.append(("уехало", rashod))
                # расходная сторона перемещения — источник серии для приходной
                if ckey:
                    move_pair[(regnum, dts, code)][ckey] += rashod
                    # тот же счётчик, но С УЧЁТОМ ПАРТИИ: у одного документа
                    # перемещения по одному коду бывает несколько партий с
                    # РАЗНЫМИ сериями, и общий счётчик отдавал все приходы
                    # доминирующей. Партия у обеих сторон перемещения одна.
                    move_pair[(regnum, dts, code, pnorm)][ckey] += rashod
            # ⚠️ ЗЕРКАЛЬНАЯ карта: приходная сторона с ЗАПИСЬЮ 1С — источник для
            # расходной, которая иначе останется БЕЗ СЕРИИ. Обычный порядок
            # обратный (приход берёт с расхода), но бывает и так: РУМЛ-010042 от
            # 25.05.2026 — приход записан серией 918, расход без серии и без лота.
            # Берём ТОЛЬКО инлайн-запись 1С: восстановленный приход — это уже
            # догадка, и строить на ней вторую догадку нельзя.
            if prihod > 0 and ckey and reg.startswith(C.FLOW_MOVE):
                move_in[(regnum, dts, code)][ckey] += prihod
                move_in[(regnum, dts, code, pnorm)][ckey] += prihod
            if prihod > 0: flows_here.append(("приехало", prihod))
        elif not any(reg.startswith(x) for x in C.FLOWS_IGNORE):
            # ВНИМАНИЕ: имя переменной цикла НЕ должно совпадать с `name`
            # (именем номенклатуры) — иначе оно затрёт его для строки реализации.
            for pref, fl_name, sign in C.FLOWS:
                if reg.startswith(pref):
                    # ⚠️ ±2 — НЕТТО, и его надо проверять ДО ±1: величина у
                    # таких документов бывает отрицательной и лежит то в
                    # приходе, то в расходе, а условия `prihod > 0` /
                    # `rashod > 0` её просто не видят (см. C.FLOWS, блок
                    # «документы, которые раньше просто терялись»).
                    if sign == 2:
                        if prihod or rashod: flow = (fl_name, prihod - rashod)
                    elif sign == -2:
                        if prihod or rashod: flow = (fl_name, rashod - prihod)
                    elif sign > 0 and prihod > 0:   flow = (fl_name, prihod)
                    elif sign < 0 and rashod > 0: flow = (fl_name, rashod)
                    elif sign == 0 and (prihod or rashod): flow = (fl_name, prihod - rashod)
                    break
        if flow:
            flows_here.append(flow)
        for fname, fqty in flows_here:
            # реализацию своей же организации в жизненный цикл не пускаем как продажу
            if fname == "продано" and kontr.strip().lower() in C.INTERNAL_BUYERS:
                fname = "внутренний_оборот"
            flow_rows.append(FlowRow(
                rid=n, flow=fname, qty=fqty, code=code, ckey=ckey,
                partia_norm=pnorm, sklad=sklad, ptype=ptype, series_raw=s_raw,
                regnum=regnum, name=name, podr=podr, guid=guid, dt=dts,
                ts=dt.strftime("%Y-%m-%d %H:%M:%S"),
                cp=kontr, so=sklad_ot, sp=sklad_pol, org=org, wk=wkind))

        # ---- строка реализации ----
        if reg.startswith(C.SALE_REGISTRAR) and rashod > 0:
            rec = {
                "rid": n,
                "dt": dt, "month": dt.strftime("%Y-%m"), "code": code, "name": name,
                "series_raw": s_raw, "ckey": ckey, "sklad": sklad, "podr": podr,
                "regnum": regnum, "partia": partia, "partia_norm": pnorm,
                "ptype": ptype, "qty": rashod, "guid": guid, "org": org,
                "buyer": kontr,
            }
            st, sbase, sana, ssrc, slabel = classify_site(podr, sklad, divisions or {},
                                                           sklad_top)
            rec["site"], rec["div_base"], rec["analytical"] = st, sbase, sana
            rec["site_src"], rec["div_label"] = ssrc, slabel
            if kontr.strip().lower() in C.INTERNAL_BUYERS:
                internal_sales.append(rec)      # внутренний оборот — НЕ продажа
            else:
                sales.append(rec)
                b["sale"] += rashod

    # карта партий: побеждает самая тяжёлая серия
    part_map = {k: cnt.most_common(1)[0][0] for k, cnt in part_map_votes.items()}
    part_variant = {k: cnt.most_common(1)[0][0] for k, cnt in part_var_votes.items()}
    part_variant_ser = {k: cnt.most_common(1)[0][0] for k, cnt in part_var_ser_votes.items()}
    # Состав лота по сериям (доли). В 566 лотах из 21 880 приход содержит НЕСКОЛЬКО
    # серий: напр. «прио:РУМЛ-000146 / 00000005139» = 0.756 серии 1578 + 0.008 серии
    # 1660. Победитель-забирает-всё приписывал 1578 и эти 8 кг, которых у неё в 1С
    # нет. Держим полный состав и делим продажу лота пропорционально.
    # Состав ограничиваем: компоненты с долей ниже MIX_MIN_SHARE и всё сверх
    # MIX_MAX_COMPONENTS сворачиваем в доминирующую серию. Иначе лоты на 23–28
    # серий дробят каждую продажу на десятки строк — это и шум, и рост файла.
    part_mix = {}
    for k, cnt in part_map_votes.items():
        if len(cnt) <= 1:
            continue
        tot_ = sum(cnt.values())
        if tot_ <= 0:
            continue
        shares = sorted(((ser, q / tot_) for ser, q in cnt.items()),
                        key=lambda kv: -kv[1])
        keep = [x for x in shares[:C.MIX_MAX_COMPONENTS] if x[1] >= C.MIX_MIN_SHARE]
        if len(keep) <= 1:
            continue                      # осталась одна серия — это не смесь
        rest = 1.0 - sum(f for _, f in keep)
        mix_ = {ser: f for ser, f in keep}
        mix_[keep[0][0]] += rest          # хвост — доминирующей
        part_mix[k] = mix_
    # Присутствие серии на складе через ЛОТ: если лот опознан по партии
    # (карта Партия+Код), то склады этого лота — тоже места присутствия серии.
    # Так учитываются перемещения и выпуск, где инлайн-серия не проставлена.
    for k, ser in part_map.items():
        for sk in lot_sklad.get(k, ()):
            series_at.add((ser, sk))

    # «единственная серия склада+кода» — только там, где вариант ровно один
    uniq_sklad_code = {k: next(iter(c)) for k, c in sklad_code_series.items() if len(c) == 1}
    # склад закреплён за единственной серией 1С (правило заказчика)
    sklad_series = {w: next(iter(c)) for w, c in sklad_series_votes.items() if len(c) == 1}

    # ---- ПРОСТОЙ ДОКУМЕНТ ПЕРЕРАБОТКИ (см. C.REWORK_SIMPLE_SAME_BASE) ----
    # Сырьё сняли на одном складе, выпуск оприходовали на соседнем складе ТОЙ ЖЕ
    # базы, количество сошлось — значит металл переехал внутри самого документа,
    # и требовать «эта серия уже бывала на складе выпуска» неправильно: она
    # попадает туда ровно этим документом. Правило узкое: три совпадения сразу.
    prod_simple = set()
    if C.REWORK_SIMPLE_SAME_BASE:
        for dkey, ins in prod_in.items():
            wout = prod_out_w.get(dkey)
            if not wout or len(wout) != 1:
                continue
            win = {x[2] for x in ins}
            if len(win) != 1:
                continue
            a, b = next(iter(win)), next(iter(wout))
            top = sklad_top or {}
            if (top.get(a) or a) != (top.get(b) or b):
                continue                       # разные базы — металл никуда не ехал
            # Сравниваем именно массу. В одном суточном документе встречаются
            # одновременно тонны, штуки и метры; складывать их количества нельзя.
            qi = prod_in_t.get(dkey, 0.0)
            qo = sum(prod_out_t.get((dkey,) + x, 0.0)
                     for x in prod_out.get(dkey, ()))
            if qi <= 0 or abs(qi - qo) > C.REWORK_SIMPLE_TOL * max(qi, qo):
                continue                       # количество не сходится — не тот случай
            prod_simple.add(dkey)
        _both = sum(1 for k in prod_out if k in prod_in)
        print("Простых документов переработки (склад -> склад одной базы, "
              "количество сходится): %d из %d с сырьём и выпуском"
              % (sum(1 for k in prod_simple if k in prod_out), _both))

    # Наследование при переработке — итеративно (несколько переделов).
    lot_dist = {}
    def input_dist(k):
        if k in lot_dist: return lot_dist[k]
        if k in part_map: return {part_map[k]: 1.0}
        return None
    def _sig(d): return tuple(sorted((s, round(f, 6)) for s, f in d.items()))
    changed, it = True, 0
    while changed and it < 60:
        changed, it = False, it + 1
        for dkey, outs in prod_out.items():
            ins = prod_in.get(dkey)
            if not ins: continue

            def mix(rows):
                """Смесь серий сырья по весам -> {серия: доля} или None.

                Доля считается от ВСЕГО веса сырья, а не только от опознанного.
                Иначе документ, где 90 % сырья без серии и 10 % — серия X, давал
                бы X долю 100 % и та забирала весь выпуск. Именно так «Лом 12АЦ»
                на СВК (Майский) попадал в БП 1578 (труба НКТ из ЗапСиба).
                Неопознанное сырьё разбавляет смесь — это и есть правда."""
                agg = defaultdict(float)
                tot = 0.0
                for ikey, raw_q, _sk, ick, tonnes in rows:
                    # Проектный баланс «в производство / из производства» ведётся в тоннах.
                    # Поэтому и долю смеси считаем по тоннажу, а не складываем
                    # несовместимые единицы 1С (тонны + штуки + метры).
                    w = tonnes
                    if w <= EPS:
                        continue
                    tot += w
                    # серия, записанная в самой строке сырья, важнее лота
                    di = {ick: 1.0} if ick else input_dist(ikey)
                    if di:
                        for s, f in di.items(): agg[s] += w * f
                return {s: v / tot for s, v in agg.items()} if tot > 0 else None

            dist_all = mix(ins)                       # весь документ — запасной вариант
            by_sklad = {}
            for okey_full in outs:
                pnorm_o, code_o, sklad_o = okey_full
                okey = (pnorm_o, code_o)
                if okey in part_map: continue         # инлайн-серия приоритетнее
                # ⚠️ СОХРАНЕНИЕ МЕТАЛЛА — единственное основание наследования.
                # Сырьё несёт тоннаж, а этот выпуск не несёт никакого — значит
                # металл в него не превращался, это отдельная позиция того же
                # суточного документа (см. C.INHERIT_NEEDS_TONNAGE: 20,660 т
                # «Лом 5А» и 34 шт «Противовес б/у» в РУМЛ-004381 у БП 1586).
                if (C.INHERIT_NEEDS_TONNAGE and prod_in_t.get(dkey, 0.0) > EPS
                        and prod_out_t.get((dkey,) + okey_full, 0.0) <= EPS):
                    continue
                # 1) сырьё ТОГО ЖЕ склада; 2) иначе — всё сырьё документа
                if sklad_o not in by_sklad:
                    same = [x for x in ins if x[2] == sklad_o]
                    by_sklad[sklad_o] = mix(same) if same else None
                dist = by_sklad[sklad_o] or dist_all
                if not dist: continue
                # Оставляем только серии, ФАКТИЧЕСКИ поступавшие на этот склад.
                # ⚠️ В ПРОСТОМ документе фильтр не применяем: серия попадает на
                # склад выпуска ровно этим документом, и проверять её «прошлое»
                # на этом складе нечем. Долю после фильтра НЕ нормализуем:
                # отфильтрованная и неопознанная часть обязана остаться
                # «БЕЗ СЕРИИ», иначе один известный БП забирает чужой металл.
                if dkey not in prod_simple:
                    dist = {ser: f for ser, f in dist.items() if (ser, sklad_o) in series_at}
                    tot_f = sum(dist.values())
                    if tot_f <= 0: continue
                elif not dist:
                    continue
                if okey not in lot_dist or _sig(lot_dist[okey]) != _sig(dist):
                    lot_dist[okey] = dist; changed = True

    # Раскладка смешанных лотов: точное количество -> ёмкость -> (крайний случай)
    # деление. Сюда входят ДВА вида смеси:
    #   1) серия прямо записана в приходах лота (part_mix);
    #   2) выпуск переработки унаследовал несколько БП из сырья (lot_dist).
    # Раньше второй вид для ПРОДАЖИ сворачивался в доминирующий БП. Например,
    # выпуск РУМЛ-003097 = 8,380 т БП 1491/1493 + 0,620 т БП 1523, а продажа
    # всех 9,000 т целиком попадала в 1491/1493 и рисовала остаток −0,620.
    mix_all = dict(part_mix)
    comp_all = dict(part_map_votes)
    prod_qty = Counter()
    for fr in flow_rows:
        if fr.flow == "переработка_вернули" and fr.partia_norm and fr.qty > EPS:
            prod_qty[(fr.partia_norm, fr.code)] += fr.qty
    inherited_mixed = 0
    inherited_keys = set()
    preferred = {}
    for key, dist in lot_dist.items():
        if key in mix_all or len(dist) <= 1:
            continue                         # прямая запись 1С важнее наследования
        total = prod_qty.get(key, 0.0)
        if total <= EPS:
            continue
        mix_ = {ser: frac for ser, frac in dist.items() if frac > 1e-12}
        known = sum(mix_.values())
        if known < 1.0 - 1e-12:
            mix_[None] = 1.0 - known         # неизвестная часть остаётся без БП
        norm = sum(mix_.values())
        if norm <= 0:
            continue
        mix_ = {ser: frac / norm for ser, frac in mix_.items()}
        if len(mix_) <= 1:
            continue
        mix_all[key] = mix_
        comp_all[key] = Counter({ser: total * frac for ser, frac in mix_.items()})
        inherited_keys.add(key)
        preferred[key] = max(dist.items(), key=lambda kv: kv[1])[0]
        inherited_mixed += 1
    print("Смешанных выпусков переработки с раскладкой продаж: %d" % inherited_mixed)
    # Считается ОДИН раз и применяется и к продажам, и к остальным расходам —
    # иначе «продано» в жизненном цикле разошлось бы с позициями.
    mix_alloc, mix_notes = build_mix_alloc(
        flow_rows, mix_all, comp_all,
        inherited_keys=inherited_keys, preferred=preferred)

    # ---- ОСТАТОК БЕЗ ПАРТИИ: сохраняем исходную серию -------------------
    # Пустая «Партия» не означает, что металл потерял БП. Реальный пример:
    # 14,900 т пришли на Склад МАТ Б.Ухта перемещением РУ00-004590 из серии
    # БП 164; в тот же день продали 8,228 т и списали 0,132 т. Оставшиеся ровно
    # 6,540 т позже уехали по РУ00-009617 уже без партии и серии. Старое правило
    # приписывало их «типичной серии склада» 1491/1493, создавая ложный минус.
    #
    # Ведём УЗКИЙ консервативный реестр только для строк без партии. Назначаем
    # серию расходу лишь когда весь известный остаток этого склада+кода относится
    # ровно к одной серии и его достаточно. Если есть неизвестный или смешанный
    # остаток — ничего не угадываем. Внутри дня сначала учитываем приходы: время
    # проведения документов в 1С техническое и может ставить продажу 00:00 до
    # фактического перемещения 14:00 того же операционного дня.
    inv_in = {"куплено", "приехало", "переработка_вернули",
              "излишки", "ввод_остатков"}
    inv_out = {"уехало", "переработка_забрали", "продано", "возврат",
               "списано", "списано_на_затраты", "внутренний_оборот"}
    known_moves = defaultdict(Counter)
    for k, cnt in move_pair.items():
        # Берём собственный ключ строки БЕЗ ПАРТИИ. Общий ключ документа
        # включает соседние строки с партиями: в РУ00-009617 рядом с 6,540 т
        # без партии ехали 2,158 и 7,462 т партий БП 1491/1493.
        if len(k) == 4 and not k[3]:
            known_moves[k].update(cnt)
    unpartied_alloc = {}
    # Несколько проходов нужны, чтобы найденная серия расхода перемещения дошла
    # до его приходной стороны, а затем могла продолжиться следующим рейсом.
    for _pass in range(6):
        stock = defaultdict(Counter)
        found = {}
        rows_ = [fr for fr in flow_rows if not fr.get("partia_norm") and fr["code"]]
        rows_.sort(key=lambda fr: (fr["dt"],
                                   0 if fr["flow"] in inv_in else 1,
                                   fr.get("ts") or "", fr["rid"]))
        discovered_moves = defaultdict(Counter)
        for fr in rows_:
            q = abs(fr["qty"])
            if q <= EPS:
                continue
            sk = (fr["sklad"], fr["code"])
            if fr["flow"] in inv_in:
                ser = fr["ckey"]
                if not ser and fr["flow"] == "приехало":
                    cnt = known_moves.get((fr["regnum"], fr["dt"][:10],
                                           fr["code"], ""))
                    if cnt and len(cnt) == 1:
                        ser = next(iter(cnt))
                stock[sk][ser] += q
                if ser and not fr["ckey"]:
                    found[fr["rid"]] = ser
                continue
            if fr["flow"] not in inv_out:
                continue
            if fr["ckey"]:
                ser = fr["ckey"]
                stock[sk][ser] = max(0.0, stock[sk][ser] - q)
            else:
                unknown = stock[sk].get(None, 0.0)
                known = [(s, v) for s, v in stock[sk].items()
                         if s is not None and v > EPS]
                if unknown <= EPS and len(known) == 1 and known[0][1] + EPS >= q:
                    ser = known[0][0]
                    stock[sk][ser] = max(0.0, stock[sk][ser] - q)
                    found[fr["rid"]] = ser
                elif unknown + EPS >= q:
                    stock[sk][None] = max(0.0, unknown - q)
                    ser = None
                else:
                    # После неоднозначного расхода состав остатка уже нельзя
                    # доказать — сбрасываем его, чтобы не раздать серию дальше.
                    stock.pop(sk, None)
                    ser = None
            if fr["flow"] == "уехало" and ser:
                discovered_moves[(fr["regnum"], fr["dt"][:10],
                                  fr["code"], "")][ser] += q
        for k, cnt in discovered_moves.items():
            known_moves[k].update(cnt)
        if found == unpartied_alloc:
            break
        unpartied_alloc = found
    print("Остаток без партии: серия продолжена для %d строк" % len(unpartied_alloc))

    return {
        "sales": sales, "internal_sales": internal_sales, "part_map": part_map,
        "lot_dist": lot_dist, "balance": balance, "purchased": dict(purchased),
        "uniq_sklad_code": uniq_sklad_code, "sklad_series": sklad_series,
        "move_in": dict(move_in),
        "lot_types": {k: c.most_common(1)[0][0] for k, c in lot_type_word.items()},
        "plat_proj": {k: v for k, v in plat_proj.items() if v},
        "sklad_series_votes": {w: len(c) for w, c in sklad_series_votes.items()},
        "part_variant": part_variant,
        "part_variant_ser": part_variant_ser, "move_pair": dict(move_pair),
        "part_mix": part_mix, "part_comp_qty": part_map_votes,
        "mix_alloc": mix_alloc, "mix_notes": mix_notes,
        "unpartied_alloc": unpartied_alloc,
        "purchased_rows": purchased_rows, "series_repr": series_repr,
        "series_variants": series_variants,
        "series_variants_own": series_variants_own, "flow_rows": flow_rows,
        "reg_kinds": reg_kinds, "rashod_by_kind": rashod_by_kind,
        "rows": n, "period_min": period_min, "period_max": period_max,
        "dump_dt": dump_dt,
        "prod_inherit_iters": it,
    }

# ---------------------------------------------------------------------------
# Ручные привязки
# ---------------------------------------------------------------------------
def load_overrides(path):
    if not path or not os.path.exists(path): return {}
    try:
        items = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for it in items or []:
        rn = (it.get("regnum") or "").strip()
        if not rn or not it.get("series"): continue
        # ключ включает ЛОТ: у одного документа по одному коду бывает несколько
        # партий с РАЗНЫМИ сериями — таких пар 2 839 из 34 211. Ключ «документ +
        # код» накрывал их все разом: правка одной строки портила три соседние.
        # Реальный случай — РУМЛ-005217, «Лом 5А»: четыре лота, четыре серии
        # (1124 без V, 1124 V, 197 Ферум тендер, 10/2025 УРАЛ).
        # Лот пишется так, как он показан в отчёте (номер документа-родителя).
        out[(rn, (it.get("lot") or "").strip(), (it.get("code") or "").strip())] = {
            "series": str(it["series"]).strip(),
            "author": it.get("author", ""), "note": it.get("note", "")}
    return out

def override_for(overrides, regnum, code, lot=""):
    """Ищем от САМОГО ТОЧНОГО ключа к самому общему: документ+лот+код ->
    документ+лот -> документ+код -> документ. Последние два — прежний формат
    без лота, поэтому старые overrides.json продолжают работать как раньше."""
    if not overrides: return None
    lot = (lot or "").strip()
    for k in ((regnum, lot, code), (regnum, lot, ""),
              (regnum, "", code), (regnum, "", "")):
        hit = overrides.get(k)
        if hit:
            return hit
    return None


def load_plan_dates(path):
    """Ручные корректировки срока вывоза: ключ проекта -> {date, note, at, by}.

    Файл пишет веб-приложение, когда экономист правит дату прямо в диаграмме.
    Дата принимается только правдоподобная (те же годы, что и для «ДатыВывоза»
    из 1С), иначе одна опечатка снова растянет отставание на 21 852 месяца.
    Пустая строка — осмысленное значение: «плана нет», снять срок, который 1С
    проставила ошибочно."""
    if not path or not os.path.exists(path):
        return {}
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for pk, rec in (raw or {}).items():
        if not isinstance(rec, dict):
            rec = {"date": rec}
        d = str(rec.get("date") or "").strip()[:10]
        if d and not ("2015-01-01" <= d <= "2040-12-31"):
            continue
        out[pk] = {"date": d, "note": (rec.get("note") or "").strip(),
                   "by": (rec.get("by") or "").strip(),
                   "at": (rec.get("at") or "").strip()}
    return out

# ---------------------------------------------------------------------------
# Каскад восстановления серии ДЛЯ СТРОКИ ПРОДАЖИ
# ---------------------------------------------------------------------------
def warehouse_of_row(s):
    """Склад, по которому определяется серия (правило заказчика §«серия по складу»):
    покупка и приход готовой продукции — склад-ПОЛУЧАТЕЛЬ (это склад самой строки),
    всё остальное — склад-ОТПРАВИТЕЛЬ. Отличие возникает у приходной стороны
    перемещения: там склад строки — получатель, а нужен отправитель."""
    fl = s.get("flow")
    if fl in C.WAREHOUSE_SERIES_BY_RECEIVER:
        return s.get("sklad") or ""
    if fl == "приехало":
        return (s.get("so") or "").strip() or s.get("sklad") or ""
    return s.get("sklad") or ""


def row_date10(s):
    """Дата строки в виде «ГГГГ-ММ-ДД». У строк реализации `dt` — datetime,
    у строк движения — уже строка: карты перемещения читаются и оттуда, и оттуда."""
    dt_ = s.get("dt")
    if dt_ is None:
        return ""
    return dt_.strftime("%Y-%m-%d") if hasattr(dt_, "strftime") else str(dt_)[:10]


def _mirror_in(m, s):
    """Приходная сторона ТОГО ЖЕ документа для строки «уехало» -> (серия, бейдж).

    Карта `m`: (документ, дата, код[, партия]) -> {серия: бейдж} либо Counter(серия).
    Отдаём только когда серия там ОДНА: две серии на приходе — это не зеркало,
    а выбор наугад. Общий ключ по коду применим лишь к строке БЕЗ ПАРТИИ —
    у соседней партии того же документа свой металл и своя серия.
    """
    d, p = row_date10(s), (s.get("partia_norm") or "")
    cnt = (m.get((s["regnum"], d, s["code"], p)) if p
           else m.get((s["regnum"], d, s["code"])))
    if not cnt or len(cnt) != 1:
        return None
    ser = next(iter(cnt))
    val = cnt[ser]
    return (ser, val if isinstance(val, str) else "док")


def recover_series_parts(s, part_map, lot_dist, uniq_sklad_code, overrides,
                         edits_log=None, mix_alloc=None, move_pair=None,
                         sklad_series=None, plat_proj=None, move_in=None,
                         move_rec=None, unpartied_alloc=None, stock_fix=None):
    """Каскад восстановления серии для ОДНОЙ строки (продажи или движения).

    -> [(серия|None, количество, бейдж)] — обычно одна часть; несколько только
    у строки из смешанного лота, которую иначе не разложить.
    Порядок ступеней — по ТЗ; ручная привязка применяется ПЕРВОЙ.
    """
    key = (s["partia_norm"], s["code"])
    qty = s["qty"]

    # 0. ручная привязка (overrides.json) — вне очереди
    ov = override_for(overrides, s["regnum"], s["code"],
                      lot_display(s.get("partia_norm") or ""))
    if ov:
        if edits_log is not None:
            dt_ = s["dt"]
            ts = dt_.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt_, "strftime") else str(dt_)
            edits_log.append({"regnum": s["regnum"], "code": s["code"],
                              "new": ov["series"], "old": s["ckey"] or "(восстановлено)",
                              "author": ov.get("author", ""), "note": ov.get("note", ""),
                              "qty": round(qty, 3), "ts": ts})
        return [(ov["series"], qty, "ручная")]

    # 0б. серия-донор по остатку склада (build_stock_fix): решение принято
    #     по ВСЕМ строкам ключа «склад + код» сразу, поэтому оно главнее и
    #     строки 1С, и всех эвристик ниже — иначе минус на складе не закрыть
    hit = (stock_fix or {}).get(s["rid"])
    if hit:
        return list(hit)

    # 1. инлайн-серия самой строки реализации
    if s["ckey"]:
        return [(s["ckey"], qty, "1С")]

    # 1б. Строка без партии продолжает доказанный остаток своего склада+кода.
    # Это важнее эвристики «типичная серия склада» и зеркала от получателя:
    # внутреннее перемещение не имеет права само назначать новый БП.
    hit = (unpartied_alloc or {}).get(s["rid"])
    if hit:
        return [(hit, qty, "остаток")]

    # 2. Склад закреплён за ЕДИНСТВЕННОЙ серией 1С — правило заказчика.
    #    Серия в строке главнее (ступень 1), но если её нет, а склад «свой»
    #    для одной серии, берём её: покупка и приход ГП — по складу-получателю,
    #    всё остальное — по складу-отправителю.
    if sklad_series and C.WAREHOUSE_SERIES:
        hit = sklad_series.get(warehouse_of_row(s))
        if hit:
            return [(hit, qty, "склад")]

    # 3. смешанный лот — готовая раскладка по компонентам (точное количество /
    #    ёмкость / деление). Считается один раз в build_mix_alloc.
    got = (mix_alloc or {}).get(s["rid"])
    if got:
        return [(ser, q, badge) for ser, q, badge in got]

    # 4. Перемещение, ПРИХОДНАЯ сторона: серию берём с расходной стороны ТОГО ЖЕ
    #    документа. «Уехало» и «приехало» — это две строки одного документа 1С
    #    «Перемещение товаров», и серия у них обязана быть одна. Без этого правила
    #    приход опознавался по общей карте лота, побеждала доминирующая серия, и
    #    по серии возникал ложный «недоезд»: у лота РУМЛ-000146 уезжало 0.008
    #    серии 1660, а приезжало «0.008 серии 1578».
    if move_pair and s.get("flow") == "приехало":
        # ⚠️ Сначала ищем расходную сторону ТОЙ ЖЕ ПАРТИИ и только потом общую по
        # коду. Документ РУ00-004626 двигал «Труба НКТ 73*5,5» сразу под пятью
        # сериями (1491/1493, 918, 170, 1199 и восстановленной по лоту), партии у
        # них разные. Общий счётчик по (документ, код) отдавал ВСЕ приходы
        # доминирующей серии 170: 18,172 т уходили из БП 1491/1493 в чужой план,
        # и у 1491/1493 остаток на Складе МАТ Б.Усинск становился −18,172.
        # ⚠️ И общий счётчик по коду применим ТОЛЬКО к строке БЕЗ ПАРТИИ. Если
        # партия у строки есть, а её расходная сторона серии не дала, — брать
        # серию СОСЕДНЕЙ партии того же документа нельзя, это ровно та ошибка,
        # против которой партию и добавили в ключ. Пример: РУ00-013768 от
        # 25.12.2024 двигал «00000035377» двумя лотами — РУ00-001385 (серия 1312)
        # и РУ00-008633 (серии нет); приход второго лота забирал 1312 у первого,
        # расход оставался БЕЗ СЕРИИ, и по 1312 возникал ложный «приехало больше,
        # чем уехало». Таких пар 328 на 447,132 т.
        _d = row_date10(s)
        _p = s.get("partia_norm") or ""
        cnt = (move_pair.get((s["regnum"], _d, s["code"], _p)) if _p
               else move_pair.get((s["regnum"], _d, s["code"])))
        if cnt:
            for ser, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
                if abs(v - qty) <= EPS:               # точное количество
                    return [(ser, qty, "док")]
            return [(max(cnt.items(), key=lambda kv: kv[1])[0], qty, "док")]

    # 4б. ПЕРЕМЕЩЕНИЕ, РАСХОДНАЯ сторона: серия с приходной стороны того же
    #     документа, ЗАПИСАННАЯ САМОЙ 1С. Зеркало ступени 4, и стоит оно ВЫШЕ
    #     карты лота: запись 1С в ЭТОМ документе локальнее и точнее, чем карта
    #     (Партия+Код), собранная по всей выгрузке.
    #     ⚠️ Пример, ради которого ступень поднята: РУ00-005218 от 21.06.2024 —
    #     карта лота отдавала расход проекту 579, а 1С в приходной строке того же
    #     документа написала серию 114; 83,859 т лежали в чужом плане, и по обоим
    #     проектам не сходилось «уехало ≠ приехало». Таких пар 33 на 149,010 т.
    if move_in and C.MOVE_MIRROR_1C and s.get("flow") == "уехало":
        got = _mirror_in(move_in, s)
        if got:
            return [(got[0], qty, "док")]

    # 5. карта (Партия+Код) -> серия по всей выгрузке
    hit = part_map.get(key)
    if hit:
        return [(hit, qty, "док")]

    # 6. Наследование при переработке — по ПОЛНОМУ составу тоннажного сырья.
    #    Суточный документ может содержать несколько БП. Победитель-забирает-всё
    #    создавал ложную потерю у миноритарных БП и ложный приход у доминирующего:
    #    хотя документ целиком сходился, «в производство / из производства» по БП расходились.
    #    Выпуск делим теми же долями; неопознанный остаток оставляем без серии.
    dist = lot_dist.get(key) if C.INHERIT_AFTER_REWORK else None
    if dist and s.get("flow") in ("переработка_забрали", "переработка_вернули"):
        parts = [(ser, qty * share, "переработка")
                 for ser, share in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
                 if share > 1e-12]
        known = sum(q for _ser, q, _badge in parts)
        if qty - known > EPS:
            parts.append((None, qty - known, "нет"))
        if parts:
            return parts
    if dist:
        # Для продажи и остальных движений сохраняем строгую атрибуцию:
        # слабая доля смеси не доказывает принадлежность конкретной позиции.
        # Пропорции нужны для баланса сторон резки, но не должны размазывать
        # БП по последующим продажам смешанного выпуска.
        best, share = max(dist.items(), key=lambda kv: (kv[1], kv[0]))
        if share >= C.INHERIT_MIN_SHARE:
            return [(best, qty, "переработка")]

    # 7. старый лот без партии: единственная серия, приходившая на этот склад
    #    под этим кодом (замена ступени «остатки на 01.03.2024» — файла нет)
    hit = uniq_sklad_code.get((s["sklad"], s["code"]))
    if hit:
        return [(hit, qty, "вероятно")]

    # 7б. ПЕРЕМЕЩЕНИЕ, РАСХОДНАЯ СТОРОНА: серия с приходной стороны того же
    #     документа, ВОССТАНОВЛЕННАЯ нами (бейдж «док» или «площадка»). Основание
    #     слабее записи 1С (ступень 4б), поэтому ступень стоит НИЖЕ лота и
    #     переработки. Бейдж наследуется от приходной стороны — по нему видно, на
    #     чём эта серия держится, и она не выглядит крепче, чем есть.
    #     ⚠️ Выше площадки эта ступень стоять обязана: у 96 из 102 пар приходная
    #     сторона и есть площадка, и без неё расход остался бы БЕЗ СЕРИИ.
    if move_rec and C.MOVE_MIRROR_RECOVERED and s.get("flow") == "уехало":
        got = _mirror_in(move_rec, s)
        if got:
            return [(got[0], qty, got[1])]

    # 8. договорная площадка: договор + номер БП из имени склада-отправителя.
    #    Если такой БП есть в справочнике серий (по паре «договор + номер»),
    #    это тот же бизнес-план — отдаём НАСТОЯЩИЙ проект, а не псевдо-серию:
    #    ранние движения по площадке «22ESP0397К БП 1001» и поздние по серии
    #    «1001 (22ESP0397К от 08.06.22 г. (ЭПУ))» — один БП.
    hit = (plat_proj or {}).get(s["sklad"])
    if hit:
        return [(hit, qty, "площадка")]
    pkey, _, _ = platform_series(s["sklad"])
    if pkey:
        return [(pkey, qty, "площадка")]

    # 9. служебные пометки ВВОД ОСТАТКОВ / ИЗЛИШКИ
    if marker_series(s["series_raw"]) or "ИзлишковТоваров" in s["ptype"]:
        return [(None, qty, "ввод")]

    # 10. не восстановлено
    return [(None, qty, "нет")]


def recover_one(s, part_map, lot_dist, uniq_sklad_code, overrides,
                edits_log=None, mix_alloc=None, move_pair=None, sklad_series=None,
                plat_proj=None, move_in=None):
    """То же, но одной серией: для движений, где дробить строку не нужно.
    Побеждает самая тяжёлая часть; бейдж — худший из частей."""
    parts = recover_series_parts(s, part_map, lot_dist, uniq_sklad_code,
                                 overrides, edits_log, mix_alloc, move_pair,
                                 sklad_series, plat_proj)
    top = max(parts, key=lambda p: p[1])
    badge = None
    for _s, _q, b in parts:
        badge = _best_badge(badge, b)
    return top[0], badge, parts

# ---------------------------------------------------------------------------
# Тоннаж / категории
# ---------------------------------------------------------------------------
def qty_to_tonnes(qty, unit, info):
    """unit — единица из факта (приоритет); info — запись номенклатуры."""
    u = (unit or "").lower()
    if not u and info: u = info.get("unit_base", "")
    if u in C.UNIT_TONNE:
        return qty * C.UNIT_TONNE[u]
    if info and info.get("unit_report") == "т" and u in ("м", "пог.м", "пм"):
        coef = info.get("coef") or 1.0
        return qty / coef if coef else 0.0
    if info and info.get("unit_report") == "т" and u not in C.UNIT_NON_WEIGHT:
        coef = info.get("coef") or 1.0
        return qty / coef if coef else 0.0
    return 0.0    # шт/компл/пар/л/… — не тонны

_MEASURE_OF = {u: key for key, _t, units in C.MEASURE_GROUPS for u in units}


def measure_class(unit):
    """Единица строки -> класс разреза («т», «м», «шт», «л», …).

    Вес сводится в «т», метры — в «м», всё прочее остаётся само собой: смешивать
    километры с метрами или комплекты со штуками нельзя, это разные величины."""
    u = (unit or "").strip().lower()
    if not u:
        return C.MEASURE_NONE
    return _MEASURE_OF.get(u, u)


def measure_title(key):
    for k, title, _u in C.MEASURE_GROUPS:
        if k == key:
            return title
    return C.MEASURE_TITLES.get(key, key)


def measure_pairs(qty, tonnes, unit):
    """Строка -> [(класс, величина)]: в каком разрезе она видна и каким числом.

    Строка попадает в «тонны», если у неё есть тоннаж, и в разрез своей единицы,
    если эта единица не весовая. Обычно это одно и то же — но у номенклатуры,
    где 1С заполнила и «единицу для отчётов = т», и коэффициент, строка честно
    видна в обоих: «Труба НКТ 60 мм б/у (м)» это и 32 метра, и 0,218 тонны.
    Поэтому суммы разных разрезов между собой НЕ складываются."""
    out = []
    if abs(tonnes) > 1e-12:
        out.append((C.MEASURE_MAIN, tonnes))
    cls = measure_class(unit)
    if cls != C.MEASURE_MAIN:
        out.append((cls, measure_qty(qty, unit)))
    return out


def measure_qty(qty, unit):
    """Количество В ЕДИНИЦЕ КЛАССА: метры -> километры, километры -> как есть.

    Вес сюда не попадает — он считается через qty_to_tonnes по коэффициентам
    номенклатуры. Здесь только пересчёт внутри группы длины (и любой другой,
    что появится в C.UNIT_SCALE)."""
    u = (unit or "").strip().lower()
    return qty * C.UNIT_SCALE.get(u, 1.0)


def move_ends(fr, site_kind=""):
    """-> (откуда, куда) для построчного показа движения (§5 ТЗ)."""
    ends = C.MOVE_ENDS.get(fr["flow"])
    if not ends:
        return fr.get("sklad", ""), ""
    def val(spec):
        if spec.startswith("="):
            return spec[1:]
        v = (fr.get(spec, "") or "").strip()
        if spec == "cp" and not v:
            return "не указан"
        return v or "—"
    a, b = val(ends[0]), val(ends[1])
    if fr["flow"] == "приехало" and site_kind in ("База", "Цех"):
        b += " (на %s)" % site_kind.lower()
    return a, b


def move_phrase_ft(flow, frm, to):
    """Однозначная формулировка движения из потока и концов «откуда/куда»:
    «уехало со склада X → Y». Один шаблон на Python (Excel) и на дашборд."""
    tpl = C.MOVE_PHRASE.get(flow)
    if not tpl:
        return flow
    return tpl.replace("{from}", frm or "—").replace("{to}", to or "—")


def move_phrase(fr, site_kind=""):
    """То же для строки движения."""
    frm, to = move_ends(fr, site_kind)
    return move_phrase_ft(fr["flow"], frm, to)


def category_of(name, report_group):
    text = (name or "") + " " + (report_group or "")
    for cat, pat in C.CATEGORIES:
        if pat.search(text): return cat
    return "Прочее"

def build_stock_fix(flow_rows, parts_by_rid, tonnes_of, min_t=None):
    """СЕРИЯ-ДОНОР ПО ОСТАТКУ СКЛАДА (правило заказчика 14.09.2026, см.
    C.STOCK_DONOR_MIN_T). -> (stock_fix, log).

    parts_by_rid — результат каскада для каждой строки: rid -> [(серия,
    количество, бейдж)]; tonnes_of(fr, q) — перевод количества строки в тонны.
    stock_fix: rid -> новый список частей той же строки (то же суммарное
    количество, часть переписана на донора с бейджем «донор»). log — список
    правок для отчёта: (склад, код, серия-минус, донор, тонн, строк).
    """
    min_t = C.STOCK_DONOR_MIN_T if min_t is None else min_t
    IN, OUT = set(C.BALANCE_1C_IN), set(C.BALANCE_1C_OUT)
    # ⚠️ ПЕРЕРАБОТКУ НЕ ПЕРЕПИСЫВАЕМ: сырьё и выпуск одного документа спарены
    # по серии (rework_ratio), и смена серии у сырья разорвала бы пару. Донору
    # отдаются только строки, которыми металл покинул склад: продажа, внутренний
    # оборот, перемещение, возврат, списания.
    MOVABLE = OUT - {"переработка_забрали", "переработка_расход_без_пары"}
    bal = defaultdict(float)          # (склад, код, серия) -> складской остаток, т
    arrived = defaultdict(float)      # (склад, код, серия) -> приехало перемещением, т
    outs = defaultdict(list)          # (склад, код, серия) -> [[дата, rid, часть, т]]
    work = {}                         # rid -> изменяемые части строки
    for fr in flow_rows:
        parts = parts_by_rid.get(fr["rid"])
        if not parts or not fr["code"]:
            continue
        fl = fr["flow"]
        if fl not in IN and fl not in OUT:
            continue
        for i, (ser, q, b) in enumerate(parts):
            if not ser:
                continue
            t = tonnes_of(fr, q)
            if abs(t) <= 1e-9:
                continue
            k = (fr["sklad"], fr["code"], ser)
            if fl in IN:
                bal[k] += t
                if fl == "приехало":
                    arrived[k] += t
            else:
                bal[k] -= t
                if fl not in MOVABLE:
                    continue
                if fr["rid"] not in work:
                    work[fr["rid"]] = [{"ser": s_, "q": q_, "b": b_} for s_, q_, b_ in parts]
                outs[k].append([fr["dt"], fr["rid"], work[fr["rid"]][i], t])
    by_key = defaultdict(dict)
    for (sk, code, ser), v in bal.items():
        by_key[(sk, code)][ser] = v
    touched, log = set(), []
    for (sk, code), d in by_key.items():
        negs = sorted(((ser, -v) for ser, v in d.items() if v < -min_t),
                      key=lambda x: -x[1])
        if not negs:
            continue
        donors = {}
        for ser, v in d.items():
            cap = min(v, arrived.get((sk, code, ser), 0.0))
            if v > min_t and cap > min_t:
                donors[ser] = cap
        if not donors:
            continue
        for neg_ser, need in negs:
            # расходные строки минусовой серии — самые поздние первыми: свой
            # металл уходил раньше, перебор — в конце
            rows = sorted(outs.get((sk, code, neg_ser), []), key=lambda r: r[0], reverse=True)
            for cap, donor in sorted(((c, d_) for d_, c in donors.items()
                                      if d_ != neg_ser and c > min_t), reverse=True):
                take_total = min(need, cap)
                if take_total <= min_t:
                    continue
                left, n_rows = take_total, 0
                for row in rows:
                    if left <= 1e-9:
                        break
                    dt_, rid, part, t_left = row
                    if t_left <= 1e-9 or part["q"] <= 1e-12:
                        continue
                    take = min(left, t_left)
                    q_move = part["q"] * (take / t_left)
                    part["q"] -= q_move
                    work[rid].append({"ser": donor, "q": q_move, "b": "донор"})
                    row[3] = t_left - take
                    touched.add(rid)
                    left -= take
                    n_rows += 1
                done = take_total - left
                if done > 1e-9:
                    donors[donor] -= done
                    need -= done
                    log.append((sk, code, neg_ser, donor, round(done, 3), n_rows))
                if need <= min_t:
                    break
    stock_fix = {rid: [(x["ser"], x["q"], x["b"]) for x in work[rid] if x["q"] > 1e-12]
                 for rid in touched}
    return stock_fix, log


def _best_badge(a, b):
    """Худшая метка побеждает при агрегации."""
    if a is None: return b
    if b is None: return a
    return a if C.BADGE_RANK.get(a, 9) >= C.BADGE_RANK.get(b, 9) else b

def series_display(skey):
    if skey == "БЕЗ СЕРИИ": return "БЕЗ СЕРИИ"
    if str(skey).startswith("площадка:"): return "Площадка " + str(skey).split(":", 1)[1]
    return str(skey)


# ---------------------------------------------------------------------------
# ОПЛАТЫ ЗА ЛОМ
# ---------------------------------------------------------------------------
_PAY_DT = re.compile(r"\bот\s+(\d{2})\.(\d{2})\.(\d{4})")

_PAY_DOG = re.compile(r"№\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9/._-]{3,})")


def contract_no(s):
    """Номер договора из строки «Договор №23Y0618 от 05.04.2023 (НДС)» -> 23Y0618.

    Нужен только чтобы ПОСЧИТАТЬ платежи без бизнес-плана по этому договору и
    честно сказать об этом на строке плана. Приписывать по нему суммы нельзя."""
    m = _PAY_DOG.search(str(s or ""))
    if not m:
        return ""
    return re.sub(r"\s+", "", m.group(1)).rstrip(".,").upper()


def load_payments(path):
    """Выгрузка «Оплата за лом» -> (платежи, статистика).

    Платёж привязывается к бизнес-плану по ГУИДу
    «ЗаявкаНаРасходованиеДенежныхСредствБизнесПланГуид» — это тот же ПроектГуид,
    которым в этом сервисе опознаётся бизнес-план, поэтому сводить по имени или
    по номеру договора не нужно (и нельзя: под одним договором лежат десятки
    проектов).

    Дата берётся из колонки «ДатаПлатежа» (формат «2025-07-30 23:59:59.000 +0500»,
    берём первые 10 символов). Запасной путь — разбор имени регистратора
    «Списание безналичных ДС ИВ00-П001460 от 21.05.2024 14:59:16».

    ⚠️ В выгрузке от 05.08.2026 «ДатаПлатежа» была ПУСТА во всех 2 110 строках, и
    дата бралась только из имени документа. В выгрузке от 06.08.2026 её заполнили
    во всех 3 873 строках — и оказалось, что у 72 платежей она НЕ СОВПАДАЕТ с
    датой в имени документа (обычно на день позже: банк провёл на следующий день),
    а у 11 из них расходится МЕСЯЦ. Для диаграммы Ганта это значит точку не в том
    делении, поэтому «ДатаПлатежа» приоритетнее и менять этот порядок нельзя.
    Сколько платежей взяли дату каждым способом — в статистике (from_field/from_name).

    ⚠️ Бизнес-план проставлен НЕ У ВСЕХ платежей (в выгрузке 06.08.2026 — 284
    строки из 3 873). Остальные привязать не к чему: ни в заявке, ни в назначении
    платежа номера БП нет. Их сумма считается отдельно и показана в автопроверке,
    выдумывать привязку по договору нельзя."""
    rows = list(csv.DictReader(read_text_lines(path)))
    B = "ЗаявкаНаРасходованиеДенежныхСредствБизнесПлан"
    out, no_bp, no_dt = [], 0, 0
    from_field = from_name = 0        # откуда взялась дата платежа
    total = 0.0
    # ⚠️ ПЛАТЕЖИ БЕЗ БП — ПО ДОГОВОРАМ. Привязать их к плану нельзя (под одним
    # договором десятки проектов), но и молчать о них неправильно: экономист
    # видит на Ганте одну точку и думает, что расчёт потерял оплаты. По договору
    # 23Y0618 из 46 платежей у 21 (208 309 364 ₽) бизнес-план в 1С не проставлен
    # вовсе, а остальные 25 разложены по ОДИННАДЦАТИ разным планам — вот почему
    # сводить по договору нельзя. Считаем их отдельно и показываем на строке
    # как «в 1С не проставлен БП», не приписывая планам ни рубля.
    gap = defaultdict(lambda: {"n": 0, "sum": 0.0, "dmin": "", "dmax": ""})
    for r in rows:
        try:
            summ = float((r.get("СуммаОплаты") or "0").replace(",", ".").strip() or 0)
        except ValueError:
            summ = 0.0
        total += summ
        pk = (r.get(B + "Гуид") or "").strip().upper()
        if not pk or pk == "00000000-0000-0000-0000-000000000000":
            no_bp += 1
            dg = contract_no(r.get("Договор") or "")
            if dg:
                g = gap[dg]
                g["n"] += 1
                g["sum"] += summ
                dd = (r.get("ДатаПлатежа") or "").strip()[:10]
                if len(dd) == 10 and dd[4] == ".":
                    dd = dd[6:10] + "-" + dd[3:5] + "-" + dd[0:2]
                if len(dd) == 10:
                    if not g["dmin"] or dd < g["dmin"]: g["dmin"] = dd
                    if not g["dmax"] or dd > g["dmax"]: g["dmax"] = dd
            continue
        d = (r.get("ДатаПлатежа") or "").strip()[:10]
        if len(d) == 10 and d[4] == "." :           # ДД.ММ.ГГГГ
            d = d[6:10] + "-" + d[3:5] + "-" + d[0:2]
        if len(d) == 10 and d[4] == "-":
            from_field += 1
        else:
            m = _PAY_DT.search(r.get("Регистратор") or "")
            d = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""
            if d:
                from_name += 1
        if not d:
            no_dt += 1
            continue
        reg = (r.get("Регистратор") or "").strip()
        out.append({
            "pk": pk,
            "bp": (r.get(B) or "").strip(),
            "d": d,
            "s": round(summ, 2),
            "ca": (r.get("Контрагент") or "").strip(),
            "dog": (r.get("Договор") or "").strip(),
            "org": (r.get("Организация") or "").strip(),
            # номер документа без хвоста «от <дата> <время>» — дата уже отдельно
            "doc": _PAY_DT.split(reg)[0].strip() or reg,
            "np": (r.get("НазначениеПлатежа") or "").strip()[:300],
        })
    out.sort(key=lambda p: (p["d"], p["pk"]))
    stats = {"rows": len(rows), "with_bp": len(out), "no_bp": no_bp,
             "no_date": no_dt, "from_field": from_field, "from_name": from_name,
             "sum_all": round(total, 2),
             "sum_bp": round(sum(p["s"] for p in out), 2),
             "date_min": out[0]["d"] if out else "", "date_max": out[-1]["d"] if out else "",
             "gap_by_contract": {k: {"n": v["n"], "sum": round(v["sum"], 2),
                                     "dmin": v["dmin"], "dmax": v["dmax"]}
                                 for k, v in gap.items()}}
    return out, stats


# ---------------------------------------------------------------------------
# НАШ ПЛАН ВЫВОЗА: сколько месяцев закладывали по каждому бизнес-плану
# ---------------------------------------------------------------------------
# Источник — выгрузка из Битрикса «Проверка_БП_*.xlsx», лист «Проверка месяцев».
# Одна строка = один блок «Расчет процентов», найденный в файле бизнес-плана:
#   «БП 476_22.11.2022 | v0 28.11.2022» · кол-во месяцев по строкам · статус.
#
# ⚠️ Берём колонку «Кол-во месяцев ПО СТРОКАМ» (решение заказчика), а не «по
# ячейке»: по строкам считаются фактические строки помесячного плана, по ячейке —
# формула, и у 238 блоков они расходятся.
#
# У одного БП блоков обычно несколько (версии плана, отправленный/ДСП, правки
# цены). Правило заказчика: берём тот, что за ПОСЛЕДНЮЮ ДАТУ. Если и на неё
# приходится несколько блоков с разным числом месяцев — предпочитаем статус «ОК»
# (у «Расхождения» парсер сам сомневается), а среди равных берём наибольшее и
# помечаем строку как спорную: в подсказке видно все варианты.
_BPM_NUM  = re.compile(r"^БП\s*(\d{1,4})[_\s]")
_BPM_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2,4})")


def _bpm_dates(s):
    """Даты из куска названия в виде ГГГГ-ММ-ДД."""
    out = []
    for d, m, y in _BPM_DATE.findall(s or ""):
        y = int(y)
        y = y + 2000 if y < 100 else y
        try:
            out.append("%04d-%02d-%02d" % (y, int(m), int(d)))
        except ValueError:
            pass
    return out


def bpm_version_date(name):
    """Дата ВЕРСИИ плана: «БП 1597_05.03.25 | v0 09.01.25_лук» -> 2025-01-09.

    ⚠️ Берём дату СПРАВА от «|», а не максимум по всей строке. Слева стоит дата
    самого бизнес-плана, у всех его версий она одна и та же, и по максимуму
    versions схлопывались: у БП 1597 версия от 09.01.25 (2 месяца) получала дату
    05.03.25 от номера БП и выигрывала у настоящей поздней версии от 05.03.25
    (1 месяц). Если справа даты нет («БП 14_26.05.21 | Коми») — берём левую.
    """
    parts = str(name or "").split("|", 1)
    right = _bpm_dates(parts[1]) if len(parts) > 1 else []
    if right:
        return max(right)
    left = _bpm_dates(parts[0])
    return max(left) if left else ""


def load_bp_months(path, choices=None):
    """-> (по номеру БП -> план, статистика).

    план: {"m": месяцев, "ver": дата версии, "rows": блоков, "alt": [другие
    значения на ту же дату], "status": статус блока, "name": название блока}

    choices — отметки экономистов с вкладки «Версии БП» (25.08.2026):
    версия с крестиком (bad) ИСКЛЮЧАЕТСЯ из кандидатов, версия с галочкой (ok)
    ПЕРЕКРЫВАЕТ правило «последняя по дате» — алгоритмы обязаны использовать
    то, что человек назвал верным, а не то, что новее.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Проверка месяцев"] if "Проверка месяцев" in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    hdr = [str(c or "").strip() for c in (next(it, None) or ())]
    # ⚠️ КОЛОНКИ — ПО ИМЕНИ ИЗ ШАПКИ, а не по номеру. Файл делает «Парсер БП»
    # (/opt/parser-bp на сервере), и его экспорт растёт: 21.08.2026 между
    # «Кол-во месяцев по ячейке» и «Статусом» вклинилась «Чистая прибыль» —
    # по старому индексу r[3] статусом стала бы прибыль, фильтр «ОК» молча
    # перестал бы работать, и при расхождениях выбиралось бы не то число.
    # Фолбэк на старые индексы — для файлов без шапки.
    def _col(name, dflt):
        return hdr.index(name) if name in hdr else dflt
    c_m, c_st = _col("Кол-во месяцев по строкам", 1), _col("Статус", 3)
    choices = choices or {}
    by_bp, rows_total, no_num = defaultdict(list), 0, 0
    rejected = forced = 0
    for r in it:
        if not r or not r[0]:
            continue
        rows_total += 1
        m = _BPM_NUM.match(str(r[0]))
        if not m:
            no_num += 1                              # «БП 06.07.2021_v0_…» — номера нет
            continue
        _ch = (choices.get(str(r[0]).strip()) or {}).get("verdict") or ""
        if _ch == "bad":
            rejected += 1                            # экономист: версия неверна
            continue
        by_bp[m.group(1)].append({
            "chosen": _ch == "ok",
            "m": int((r[c_m] if c_m < len(r) else 0) or 0),
            "d": bpm_version_date(r[0]),
            "status": str((r[c_st] if c_st < len(r) else "") or "").strip(),
            "name": str(r[0]),
        })
    out, ties = {}, 0
    for bp, lst in by_bp.items():
        # галочка экономиста перекрывает «последнюю дату»
        ok_ch = [x for x in lst if x.get("chosen") and x["m"] > 0]
        if ok_ch:
            forced += 1
            lst = ok_ch
        last = max(x["d"] for x in lst)
        cand = [x for x in lst if x["d"] == last and x["m"] > 0]
        if not cand:
            continue                                 # «Не заполнен» — плана нет
        ok = [x for x in cand if x["status"] == "ОК"] or cand
        vals = sorted({x["m"] for x in ok}, reverse=True)
        if len(vals) > 1:
            ties += 1
        best = next(x for x in ok if x["m"] == vals[0])
        out[bp] = {"m": vals[0], "ver": last, "rows": len(lst),
                   "alt": vals[1:], "status": best["status"], "name": best["name"]}
    stats = {"rows": rows_total, "no_num": no_num, "bps": len(by_bp),
             "with_plan": len(out), "ties": ties,
             "ver_rejected": rejected, "ver_forced": forced}
    return out, stats


# ---------------------------------------------------------------------------
# ПЛАН ПО ОБЪЁМАМ ПРОИЗВОДСТВА («Объемы на загрузку»)
# ---------------------------------------------------------------------------
# Заказан 14.08.2026: показать на Ганте рамками, что мы САМИ планируем сделать
# по бизнес-плану в ближайшие месяцы. Выгрузка — «Объемы на загрузку», 91
# колонка, и это важно понимать до чтения:
#
# ⚠️ ШИРОКИЕ КОЛОНКИ `_0_…_3_` (прогноз на текущий период и три следующих)
# ПОЧТИ ПУСТЫЕ — заполнены у 0,1–0,4 % строк. Это наследие прежней формы отчёта.
# Настоящие данные лежат в ДЛИННОЙ форме: одна строка — один период × операция ×
# серия, а величина в «Значение».
#
# ⚠️ ЗНАК ИГНОРИРУЕМ. Поле «Знак» строго повторяет смысл операции (расход −1,
# приход +1) — проверено по всей выгрузке, ни одной операции со смешанным знаком
# нет. А вот САМО «Значение» бывает отрицательным (475 строк у «Вывоз с цеха на
# базу», 68 у «Перемещение») — это снятие ранее заведённого плана, и его надо
# ВЫЧИТАТЬ. Возьми мы модуль — снятые объёмы удвоили бы план.
#
# ⚠️ ТОЛЬКО ТОННЫ. В выгрузке есть шт (11 991 строка), м, кг и компл; складывать
# их с тоннами нельзя, а рамка на Ганте показывает одну величину.
#
# ⚠️ СВЯЗЬ С БИЗНЕС-ПЛАНОМ — ПО НОМЕРУ В СТРОКЕ СЕРИИ, тем же приёмом, что и с
# Битриксом: «БП 1659», «п.Майский (АО Агро-Альянс) БП 1498 (Кательная)». Из 405
# номеров выгрузки 376 знакомы отчёту; остальное — серии «без БП».
PRODPLAN_OPS = {
    "выпуск":   ("Выпуск продукции",),
    "вывоз":    ("Вывоз с цеха на базу", "Вывоз с цеха на ответхранение"),
    "отгрузка": ("Отгрузка с баз на покупателя", "Отгрузка с цеха на покупателя",
                 "Отгрузка ответхранения на покупателя"),
    "приход":   ("Приход", "Закупка на базы"),
}
_PP_BP = re.compile(r"БП\s*(\d{3,4})")

# ---------------------------------------------------------------------------
# ПЛАН ПО БАЗАМ: ЧТО СЧИТАТЬ ВЫБЫТИЕМ, А ЧТО ПОСТУПЛЕНИЕМ (14.08.2026)
# ---------------------------------------------------------------------------
# Прогноз остатка строится ПО БАЗЕ, а не по бизнес-плану: строка плана привязана
# к подразделению («База Усинск»), а номер БП стоит только у трети строк.
# Поэтому операции разложены по роли в остатке БАЗЫ.
#
# ⚠️ ВЫБЫТИЕ И ПОСТУПЛЕНИЕ — РАЗНЫЕ ПО ПРИМЕНЕНИЮ, а не только по знаку.
# Выбытие гасит остатки конкретных бизнес-планов (ФИФО), поступление — нет:
# у ещё не приехавшего металла нет строки на диаграмме, приписать его чужому
# плану значило бы выдумать. Поэтому поступление входит только в прогноз БАЗЫ.
#
# ⚠️ «ВЫВОЗ С ЦЕХА НА БАЗУ» В ПРОГНОЗ БАЗЫ НЕ ВХОДИТ, хотя по смыслу это её
# приход. Строка стоит за подразделением-ОТПРАВИТЕЛЕМ (Пермь, Усинск, Когалым —
# заготовка и цеха), а получатель в выгрузке пуст: `_0_ПунктНазначения`
# заполнен у 14 строк из 614. Приписать такой план базе не на чем.
# Потери нет: встречный «Приход» у самой базы заведён отдельной строкой
# (22 202 т против 21 273 т «вывоза» в волне 13.04.2026) — это те же тонны с
# правильной стороны. Сложи мы обе — база получила бы двойной приход.
#
# ⚠️ «РАСХОД СЫРЬЯ» И «ВЫПУСК ПРОДУКЦИИ» — ПЕРЕРАБОТКА НА МЕСТЕ, а не выбытие.
# На базе с шредером сырьё уходит в переработку и возвращается продукцией на тот
# же склад; в движениях они точно так же гасят друг друга. Считать «расход
# сырья» вывозом (26 661 т в волне 13.04) — значит списать по ФИФО остатки
# планов, металл которых с базы никуда не уехал.
PRODPLAN_BASE_OUT = ("Отгрузка с баз на покупателя",
                     "Перемещение между базами/площадками")
PRODPLAN_BASE_IN = ("Приход", "Закупка на базы")
PRODPLAN_BASE_MILL = ("Расход сырья", "Выпуск продукции")
# Подразделение базы в выгрузке плана называется так же, как в справочнике
# подразделений 1С («База Усинск»), — тем и связываем. Проверено на волне
# 13.04.2026: все девять баз плана нашлись среди наших складов.
PRODPLAN_BASE_PREFIX = "База"


def _pp_num(s):
    """«26,843000» / «26.843000» -> float. Пустое и мусор -> None."""
    s = (s or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def load_prodplan(path, unit="т"):
    """-> (по номеру БП -> {месяц -> {вид -> тонны}}, по базам, статистика).

    Вид — ключ из PRODPLAN_OPS. Месяц — «ГГГГ-ММ» из поля «Период».
    По базам: {подразделение -> {месяц -> {"out": выбытие, "in": поступление,
    "mill": переработка на месте}}} — роли из PRODPLAN_BASE_*.
    """
    op_of = {}
    for key, names in PRODPLAN_OPS.items():
        for n in names:
            op_of[n] = key
    role_of = {}
    for names, role in ((PRODPLAN_BASE_OUT, "out"), (PRODPLAN_BASE_IN, "in"),
                        (PRODPLAN_BASE_MILL, "mill")):
        for n in names:
            role_of[n] = role
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    bases = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    # ВОЛНЫ ПЛАНИРОВАНИЯ: «АктуальнаяДатаПланирования» -> какие периоды заведены.
    # Из них берётся НУЛЕВОЙ МЕСЯЦ — первый период последней волны (см. main).
    waves = defaultdict(set)
    seen_ops, months, bps_all = Counter(), set(), set()
    base_skip = Counter()          # операции базы, не попавшие ни в одну роль
    off_base = Counter()           # роль есть, а подразделение не база
    rows_total = no_bp = no_unit = no_op = 0
    for r in csv.DictReader(read_text_lines(path)):
        rows_total += 1
        if (r.get("ЕдИзм") or "").strip() != unit:
            no_unit += 1
            continue
        v = _pp_num(r.get("Значение"))
        mon = (r.get("Период") or "")[:7]
        opn = (r.get("Операция") or "").strip()
        if v is not None and len(mon) == 7:
            wave = (r.get("АктуальнаяДатаПланирования") or "")[:10]
            if wave and "2015" <= wave[:4] <= "2040":
                waves[wave].add(mon)
            unit_ = (r.get("Подразделение") or "").strip()
            role = role_of.get(opn)
            if unit_.startswith(PRODPLAN_BASE_PREFIX):
                if role:
                    bases[unit_][mon][role] += v
                else:
                    base_skip[opn or "(пусто)"] += abs(v)
            elif role:
                off_base[opn or "(пусто)"] += abs(v)
        op = op_of.get(opn)
        if not op:
            no_op += 1
            seen_ops[opn or "(пусто)"] += 1
            continue
        m = _PP_BP.search(r.get("Серия") or "")
        if not m:
            no_bp += 1
            continue
        if v is None or len(mon) != 7:
            continue
        bp = m.group(1)
        out[bp][mon][op] += v
        months.add(mon)
        bps_all.add(bp)
    plan = {bp: {mon: {k: round(v, 3) for k, v in ops.items() if abs(v) > 0.0005}
                 for mon, ops in by_mon.items()}
            for bp, by_mon in out.items()}
    plan = {bp: {m: o for m, o in by.items() if o} for bp, by in plan.items()}
    plan = {bp: by for bp, by in plan.items() if by}
    base_plan = {u: {m: {k: round(x, 3) for k, x in roles.items() if abs(x) > 0.0005}
                     for m, roles in by_mon.items()}
                 for u, by_mon in bases.items()}
    base_plan = {u: {m: r for m, r in by.items() if r} for u, by in base_plan.items()}
    base_plan = {u: by for u, by in base_plan.items() if by}
    stats = {"rows": rows_total, "bps": len(plan), "months": sorted(months),
             "no_bp": no_bp, "no_unit": no_unit, "no_op": no_op,
             "other_ops": dict(seen_ops.most_common(8)),
             "waves": {w: sorted(ms) for w, ms in sorted(waves.items())},
             "base_skip": dict(base_skip.most_common(6)),
             "off_base": dict(off_base.most_common(6))}
    return plan, base_plan, stats


# ---------------------------------------------------------------------------
# КОММЕНТАРИИ ПО ЗАПАСАМ («Значения нефинансовых показателей»)
# ---------------------------------------------------------------------------
# Заказано 20.08.2026: у экономистов в 1С по каждому остатку ведётся живой
# комментарий — «не вывозим, деньги вернули», «заберём с новыми объёмами»,
# «демонтаж произведён, ждём реализации с места». На диаграмме этого не видно, и
# любой вопрос «а почему тут остаток второй год» упирается в звонок человеку.
#
# ⚠️ ЭТО ИСТОРИЯ, А НЕ СНИМОК. В выгрузке 40 срезов с 03.02.2025 по 12.08.2026,
# одна и та же позиция комментируется до восьми раз. Показывать надо ВСЮ ленту,
# от свежего к старому: смысл как раз в том, как менялась причина.
#
# ⚠️ СВЯЗЬ — ПО ГУИДУ СЕРИИ, А НЕ ПО СТРОКЕ. Строка серии в этой выгрузке
# написана по-своему, и совпадение по тексту даёт 337 из 453 серий; гуид через
# справочник серий даёт 7 173 строки из 7 179 (99,9 %), а на строки Ганты
# ложится 7 117 (99,1 %). Ключ отчёта — ПРОЕКТ (бизнес-план), поэтому гуид серии
# переводится в ключ проекта: у проекта серий бывает несколько, и комментарии по
# ним — про один и тот же план.
#
# ⚠️ СТРОКИ БЕЗ ТЕКСТА ОТБРАСЫВАЕМ. Из 7 179 строк комментарий заполнен у 4 877;
# остальные несут только величину остатка на дату — её отчёт и так считает сам,
# а пустые строчки в ленте выглядели бы как «комментарий потеряли».
def load_stock_notes(path, sg2p):
    """-> (ключ проекта -> [{d, w, n, q, t, u}, …], статистика).

    d — дата среза, w — склад, n — номенклатура, q — остаток в этот срез,
    t — сам комментарий, u — кто ведёт (см. ниже). Лента от свежего к старому.

    ⚠️ АВТОРА-ЧЕЛОВЕКА В ВЫГРУЗКЕ НЕТ (проверено 20.08.2026: все 16 колонок).
    Лучшее, что есть, — «Подразделение» (Пермь, Когалым…): чей это остаток, тот
    и пишет. ФИО иногда встречается прямо в тексте («Репетун Е.: …»), но это не
    колонка. Если 1С начнёт отдавать автора отдельным полем («Автор» /
    «Ответственный» / «Пользователь»), оно подхватится само и ПЕРЕКРОЕТ
    подразделение — искать правку кода не придётся.
    """
    out = defaultdict(list)
    rows = kept = no_ser = no_text = 0
    dates, dump = set(), ""
    for r in csv.DictReader(read_text_lines(path)):
        rows += 1
        txt = (r.get("Комментарий") or "").strip()
        if not txt:
            no_text += 1
            continue
        pk = sg2p.get((r.get("СерияГуид") or "").strip().upper())
        if not pk:
            no_ser += 1
            continue
        d = (r.get("Период") or "")[:10]
        q = _pp_num(r.get("ЗначениеПоказателя"))
        who = ((r.get("Автор") or "").strip()
               or (r.get("Ответственный") or "").strip()
               or (r.get("Пользователь") or "").strip()
               or (r.get("Подразделение") or "").strip())
        out[pk].append({"d": d, "w": (r.get("Склад") or "").strip(),
                        "n": (r.get("Номенклатура") or "").strip(),
                        "q": round(q, 3) if q is not None else None, "t": txt,
                        "u": who})
        kept += 1
        if d:
            dates.add(d)
        dump = max(dump, (r.get("ДатаВыгрузки") or "")[:10])
    for pk in out:
        # от свежего к старому; внутри одной даты — по складу и номенклатуре,
        # чтобы порядок не плясал от пересборки к пересборке
        out[pk].sort(key=lambda c: (c["d"], c["w"], c["n"]), reverse=True)
    stats = {"rows": rows, "kept": kept, "no_series": no_ser, "no_text": no_text,
             "bps": len(out), "dates": sorted(dates), "dump": dump,
             "uniq": len({c["t"] for cs in out.values() for c in cs})}
    return dict(out), stats


# ---------------------------------------------------------------------------
# ВЕРСИИ РАСЧЁТОВ БП («БП_версии_*.json» от парсера) + выбор экономистов
# ---------------------------------------------------------------------------
# Заказано 25.08.2026: вкладка «Версии БП» — экономисты отмечают, какая версия
# расчёта (лист в excel-файле БП) верна и какую алгоритмы должны использовать.
# JSON пишет bp_auto_export.py на сервере тем же прогоном, что и свод
# «Проверка месяцев»; выручку в parser_core добавили тем же правилом, что и
# прибыль («Выручка без НДС», столбец «Суммарно»).
# ---------------------------------------------------------------------------
# ПЛАНОВАЯ НОМЕНКЛАТУРА EXCEL  <->  НАША НМК
# ---------------------------------------------------------------------------
# Заказ (02.09.2026): «почему не заполнены план, т и план, ₽ — у тебя же есть
# суммы по каждой позиции из Excel и конкретный вес из 1С».
#
# ЧТО ЗДЕСЬ СЛОЖНОГО. В расчёте номенклатура написана словами продавца
# («Труба НКТ 73х5.5 б/у общ.назнач.», «Лом стальной марки 5А ГОСТ 2787-75»,
# «Металлолом (черный металл)»), а в 1С приходуют своей НМК («Труба НКТ 73*5,5
# б/у*», «Лом 5А»). Один в один тексты не совпадают почти никогда: разные
# разделители размеров (х / * / x), запятая против точки, приписки «б/у»,
# «(т)», ГОСТы и месторождения.
#
# ПРАВИЛО СОПОСТАВЛЕНИЯ — не «похожесть строк», а СМЫСЛ позиции:
#   1) ВИД: кабель / штанга / лом / труба / цветмет. Определяется по первому
#      встреченному слову-признаку, поэтому «Труба НКТ 73 (Лом)» — труба, а
#      «Металлолом 12А (отбраковка НКТ)» — лом;
#   2) ПРИЗНАК ВНУТРИ ВИДА: у лома — марка (5А, 12А, 3АН), у трубы и штанги —
#      размеры, у кабеля — марка кабеля и сечение, у цветмета — металл;
#   3) из подходящих кандидатов берётся один. Если их несколько с одинаковым
#      весом совпадения — выигрывает САМОЕ ПРОСТОЕ имя: плановая строка тоже
#      без уточнений, и «Лом 5А» ближе к ней, чем «Лом 5А (жд)».
#
# ⚠️ ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ: разнесения «общего» лома. Строка плана
# «Металлолом (черный металл)» без марки, когда в 1С по этому проекту пришли
# 3А, 5А и 12А, НЕ раскладывается по ним долями: доля была бы выдумкой, а
# экономист прочитал бы её как факт. Такие тонны собираются отдельной суммой
# «марка в плане не указана» и показываются под таблицей — вместе с теми, что
# не легли ни на одну НМК. Сумма колонки «план» всегда равна тому, что реально
# сопоставлено, и никогда не больше.
_NM_NUM = re.compile(r"\d+(?:\.\d+)?")
# марка лома: 5А, 12А, 3А1, 3АН, 5АТ, 12АЦ, 5АЭ …
_NM_GRADE = re.compile(r"(?<![0-9])(\d{1,2})\s*(а|б)(\d|т|н\d?|ц|э)?(?![0-9а-я])")
_NM_METALS = (
    ("медь", r"мед[ьин]|катанк"), ("алюминий", r"алюмин"), ("латунь", r"латун"),
    ("нержавейка", r"нержав|(?:^|\s)нж(?:\s|$)"), ("свинец", r"свинец|свинц"),
    ("бронза", r"бронз"), ("чугун", r"чугун"),
)
_NM_KINDS = (
    ("кабель", r"кабел"),
    # ⚠️ ПОЛУШТАНГА — ОТДЕЛЬНЫЙ ВИД. Она содержит слово «штанга» и при общем
    # виде спорила с ней на равных: «Штанга 22мм б/у (т)» из плана одинаково
    # подходила и к «Штанга 22 мм б/у (т)», и к «Полуштанга 22 мм (т)» — имена
    # той же длины, тай-брейк не помогал, и 25 т уходили в «не разнесено».
    ("полуштанга", r"полуштанг"),
    ("штанга", r"штанг"),
    ("лом", r"лом|отход|обрез|стружк|шлак|окалин|скрап"),
    ("труба", r"труб|(?:^|\s)нкт(?:\s|$)"),
)
# «ГОСТ 2787-75», годы и номера приложений размерами не являются
_NM_JUNK_NUM = (2787.0, 75.0, 2019.0, 1975.0)

# ДЮЙМЫ -> МИЛЛИМЕТРЫ (подтверждено заказчиком 02.09.2026: «да, всё так
# определяй»). В старых расчётах размер НКТ пишут по-промысловому, в дюймах
# («Труба НКТ 2,0 б/у», «_Труба 2 1/2 б/у», «нкт 2.5»), а наша НМК — в
# миллиметрах. Ряд стандартный: 2" = 60, 2 1/2" (оно же 2 7/8") = 73,
# 3" и 3 1/2" = 89, 4 1/2" = 114.
# ⚠️ АРИФМЕТИКОЙ ЭТО НЕ ДОКАЗЫВАЛОСЬ. Соблазн был: в БП 999 остаток по «Труба
# НКТ 60х5 б/у (т)» ровно равен дюймовой строке (166,037 − 114,206 = 51,831).
# Но проверка по всем 31 проекту с такими строками дала совпадение ровно в
# одном — то есть это была случайность. Правило стоит на слове заказчика, а не
# на подгонке чисел; если ряд когда-нибудь окажется другим — менять здесь.
_NM_INCH_MM = {1.25: 48.0, 1.5: 48.0, 1.9: 48.0,
               2.0: 60.0, 2.375: 60.0, 2.5: 73.0, 2.875: 73.0,
               3.0: 89.0, 3.5: 89.0, 4.5: 114.0}
# МЕЛКИЕ РАЗМЕРЫ (заказчик 02.09.2026: «да, добавь») — и здесь, в отличие от
# ряда выше, есть подтверждение В ДАННЫХ, по двум независимым проектам, тонна
# в тонну:
#   БП 839: план «Труба НКТ 1 1/4" б/у» 1,613 т — факт «Труба НКТ 48 мм б/у»
#           куплено 1,613 т, другой мелкой трубы в проекте нет;
#   БП 176: план «труба нкт 1 1/2» 4,000 т — факт «Труба 48 мм б/у» 4,000 т.
# Обе полуторки и дюйм с четвертью ложатся на НКТ 48. В трёх других проектах
# такие строки останутся в «не сошлось»: там мелкой трубы в факте просто нет,
# и это правильный ответ, а не промах правила.
_NM_FRAC = re.compile(r"(?<![0-9])(\d)\s+(\d)\s*/\s*(\d)(?![0-9])")


def _nm_norm(s):
    """Общий знаменатель для двух разных манер писать одно и то же."""
    s = (s or "").lower().replace(u"\xa0", " ").replace(u"ё", u"е")
    s = s.replace(",", ".")
    # размер склеиваем в один токен: «73*5,5», «73x5.5», «73 х 5.5» -> «73х5.5».
    # ⚠️ Звёздочку в букву «х» огульно не превращаем: у нашей НМК она стоит
    # хвостом («Труба НКТ 73*5,5 б/у*»), и «б/у*» становилось словом «ух» —
    # лишний токен, который портил сравнение имён.
    s = re.sub(u"(\\d)\\s*[*x\u00d7\u0445]\\s*(\\d)", u"\\1\u0445\\2", s)
    s = re.sub(r"[^0-9\u0430-\u044f./\s-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Слова, которые есть почти у всех и потому ничего не различают: единицы,
# «б/у», ссылки на ГОСТ и его годы.
_NM_STOP = set(u"""бу кг шт тн гост общ назнач наз исп гр из под для
2787 2019 2024 1975 75""".split())


def _nm_tokens(sn):
    """Значимые слова нормализованного имени — по ним сравниваются НАЗВАНИЯ.

    ⚠️ ЧИСЛА И РАЗМЕРЫ — ТОЖЕ СЛОВА, и выбрасывать их нельзя. Первая версия
    резала имя по точкам: «труба нкт 2.0 б/у» превращалась в {труба, нкт}, и
    от «труба нкт 60» она отличалась только одним словом — сходство 0.8, план
    по двухдюймовой трубе ложился на НКТ 60. Размер оставляем одним токеном
    («73х5.5», «2.0»), короткими выкидываем только буквенные обрывки.
    """
    out = []
    # ⚠️ БУКВЫ И ЦИФРЫ В ОДНОМ СЛОВЕ НЕ РАЗРЫВАЕМ: «б62» — это марка, а не «б»
    # и «62», и разорванная она не находила «12Б62» в нашей НМК.
    for t in re.split(u"[^\u0430-\u044f0-9.]+", sn or ""):
        t = t.strip(".")
        if not t or t in _NM_STOP:
            continue
        if t[0].isdigit() or len(t) >= 2:
            out.append(t)
    return out


def _nm_code_hit(pw, cw):
    """Совпал характерный код позиции: «б62» внутри «12б62», «120» и «120».

    Так находится марка, которую ни один разбор не опознал: в плане пишут
    «Лом черных металлов Б62 ГОСТ 2787-2019», у нас — «Лом 12Б62».
    """
    P = [t for t in pw if len(t) >= 3 and any(ch.isdigit() for ch in t)]
    C = [t for t in cw if len(t) >= 3 and any(ch.isdigit() for ch in t)]
    for a in P:
        for b in C:
            if a == b or a in b or b in a:
                return True
    return False


def _nm_sim(a, b):
    """Мера Дайса по значимым словам: 1.0 — те же слова, 0 — ни одного общего."""
    A, B = set(a), set(b)
    if not A or not B:
        return 0.0
    return 2.0 * len(A & B) / float(len(A) + len(B))


def nomen_sig(name):
    """Смысловая подпись позиции: вид, марка, размеры, металл."""
    s = _nm_norm(name)
    # «труба-633 НКТ 73х5.5» — 633 это артикул расчёта, а не размер трубы
    s_num = re.sub(u"труб[ауыое]?\\s*-\\s*\\d{2,4}", u"труба", s)
    # «2 1/2» -> 2.5: иначе размер рассыпается на три бессмысленных числа
    s_num = _NM_FRAC.sub(lambda m: "%g" % (int(m.group(1))
                                           + float(m.group(2)) / float(m.group(3))), s_num)
    nums = []
    for m in _NM_NUM.finditer(s_num):
        v = float(m.group(0))
        if v in _NM_JUNK_NUM or v > 1000:
            continue
        nums.append(v)
    sig = {"s": s, "n": nums, "g": [], "metal": None, "kind": u"прочее",
           "brand": "", "nkt": False, "sec": [], "w": _nm_tokens(s)}
    for m in _NM_GRADE.finditer(s):
        sig["g"].append(m.group(1) + m.group(2) + (m.group(3) or ""))
    if re.search(u"н/л|нелегиров", s):
        sig["g"].append(u"н/л")
    for mt, rx in _NM_METALS:
        if re.search(rx, s):
            sig["metal"] = mt
            break
    best = None
    for kind, rx in _NM_KINDS:
        m = re.search(rx, s)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), kind)
    if best:
        sig["kind"] = best[1]
    if sig["kind"] == u"кабель":
        m = re.search(u"кабел[ья]\\s*([\\u0430-\\u044f]{2,})", s)
        b = m.group(1) if m else ""
        # «кабель (по позициям на доп листе)» — «по» не марка
        sig["brand"] = "" if b in (u"по", u"на", u"из", u"для", u"без") else b
        # СЕЧЕНИЕ — жилы × мм² («3х16», «4х1.5», «4х3х1»): у кабеля это и есть
        # товар. Заказчик 14.09.2026: «Кабель 3х1,5» из плана и наш «Кабель КГ
        # 3х1,5 б/у» — одно и то же, марка в плане просто не написана.
        sig["sec"] = re.findall(u"(?<![0-9.])\\d+(?:\\.\\d+)?(?:\u0445\\d+(?:\\.\\d+)?)+(?![0-9.])", s)
    elif sig["kind"] == u"труба":
        sig["nkt"] = u"нкт" in s
        # РАЗМЕР В ДЮЙМАХ. Признак — все числа имени мельче десяти: труба в
        # миллиметрах меньше 48 не бывает, а дюймовый размер больше 4,5 в НКТ
        # не встречается, так что перепутать нечего.
        if sig["n"] and all(z < 10 for z in sig["n"]):
            mm = _NM_INCH_MM.get(round(sig["n"][0], 3))
            if mm:
                sig["n"] = [mm]
                sig["inch"] = True
    # металл сильнее вида: «Лом меди» и «Медь» — одно и то же, и с чёрным ломом
    # их путать нельзя
    if sig["metal"] and sig["kind"] in (u"лом", u"прочее"):
        sig["kind"] = u"цветмет"
    return sig


# Порог «это одно и то же название». 0.75 подобран по данным: «Штанга 19 мм»
# против «Штанга 22 мм» даёт 0.67 и порога не берёт, а «Лом электродвигателей»
# против «Лом электродвигателей (кг)» — 1.0.
_NM_SAME = 0.75


def _nm_score(p, c):
    """Насколько плановая позиция p похожа на нашу НМК c. None — не пара.

    Порядок сигналов — от сильного к слабому:
      300 — у лома сошлась МАРКА (5А, 12А, 3АН): для лома марка и есть товар;
      250 — совпал характерный код («Б62» ↔ «12Б62»);
      200 — НАЗВАНИЕ практически то же («Лом электродвигателей» ↔ «… (кг)»);
      100 — сошлись размеры трубы/штанги, марка кабеля, металл цветмета;
       30 — «металлолом» без марки: годится, только если кандидат один.
    ⚠️ РАЗМЕР И МАРКА — ВЕТО. Похожее название не может перебить разные
    размеры или разные марки: «Штанга 19 мм» и «Штанга 22 мм» написаны почти
    одинаково, но это разный товар.
    """
    if p["kind"] != c["kind"] or p["kind"] == u"прочее":
        return None
    sim = _nm_sim(p["w"], c["w"])
    bump = int(round(sim * 50))                 # ближе по названию — выше место
    same = 200 + bump if sim >= _NM_SAME else None
    code = 250 + bump if _nm_code_hit(p["w"], c["w"]) else None
    best = lambda *xs: max([x for x in xs if x is not None] or [0]) or None

    if p["kind"] == u"лом":
        pg, cg = p["g"], c["g"]
        if pg and cg:
            if set(pg) & set(cg):
                return 300 + bump               # марка сошлась
            if any(a[:2] == b[:2] for a in pg for b in cg):
                return 60                       # 5А против 5АТ — подвид
            return None                         # разные марки — разный товар
        if pg and not cg:
            # марка есть только в плане: годится, если имя почти то же
            return best(code, same)
        return best(code, same, 30)             # «металлолом» без марки

    if p["kind"] == u"цветмет":
        if not (p["metal"] and p["metal"] == c["metal"]):
            return None
        return best(code, same, 100)

    if p["kind"] in (u"штанга", u"полуштанга"):
        if p["n"] and c["n"]:
            if p["n"][0] != c["n"][0]:
                return None                     # разный диаметр
            return best(code, same, 100)
        if not p["n"] and not c["n"]:
            return best(same, 40)
        return None

    if p["kind"] == u"труба":
        bonus = 10 if p["nkt"] == c["nkt"] else 0
        if p["n"] and c["n"]:
            if p["n"][:2] == c["n"][:2]:
                return best(code, same, 100 + bonus)   # диаметр и стенка
            if p["n"][0] == c["n"][0]:
                return best(code, same, 70 + bonus)    # сошёлся диаметр
            return None                                # другой размер
        if not p["n"] and not c["n"]:
            return best(same, 40 + bonus)
        return best(same, 35 + bonus)

    if p["kind"] == u"кабель":
        ps, cs = set(p.get("sec") or ()), set(c.get("sec") or ())
        same_brand = bool(p["brand"]) and p["brand"] == c["brand"]
        if ps and cs and (ps & cs):
            return best(code, same, 100 + (10 if same_brand else 0))   # сечение сошлось
        if same_brand:
            return best(code, same, 70)                # марка та же, сечение не сошлось
        if not ps and not p["brand"]:
            return best(same, 30)                      # просто «кабель» — если он один
        return same
    return None


# ---------------------------------------------------------------------------
# МЕСТО ХРАНЕНИЯ ИЗ EXCEL  <->  НАШ СКЛАД (14.09.2026, по просьбе финдира:
# «факт пойдёт по разрезам наша НМК × склады, план в идеале тоже»)
# ---------------------------------------------------------------------------
# В плане место хранения — текст продавца («база ООО "ЛНПО-Сервис", г.Лангепас»,
# «Когалым, ООО "Когалым НПО Сервис"»), у нас склад — своё имя, часто с
# договором и сокращением города («22С0371 … Ланг.БП236,248,249», «Когалым
# КНПО», «Склад МАТ Б.Усинск»). Сравниваем ГОРОДА/ПЛОЩАДКИ: из обоих имён
# берутся значимые слова, сокращения раскрываются по словарю ниже, слово
# считается общим, если одно начинается с другого (лангепас ~ ланг). Слова
# «ооо», «база», «сервис», «нпо» есть у всех и ничего не различают.
_PLACE_STOP = set(u"""ооо оао ао зао ип база базa склад сервис нпо кнпо лнпо ул
месторождение месторожд под над от до для г км мат гп бп дс сп доп нал лом труба
штанга кабель цех цеховое подразделение головные сооружения основной""".split())
_PLACE_ABBR = {u"ланг": u"лангепас", u"пок": u"покачи", u"ког": u"когалым",
               u"сов": u"советский", u"ур": u"урай", u"усин": u"усинск",
               u"ухт": u"ухта", u"берез": u"березники", u"осенц": u"осенцы",
               u"нижн": u"нижневартовск", u"волг": u"волгоград", u"черн": u"чернушка",
               u"кстов": u"кстово", u"котов": u"котово", u"солик": u"соликамск",
               u"повх": u"повхнефтегаз", u"свк": u"свк"}


def _place_words(name):
    s = (name or "").lower().replace(u"ё", u"е")
    s = re.sub(u"[^\u0430-\u044f0-9\\s]+", " ", s)
    out = []
    for w in s.split():
        if len(w) < 3 or w in _PLACE_STOP or w[0].isdigit():
            continue
        out.append(_PLACE_ABBR.get(w, w))
    return out


def match_place(division, warehouses):
    """warehouses — [[имя склада, тонн], …] нашей позиции. -> имя склада или None.
    Побеждает склад с наибольшим числом общих слов; при равенстве — где факт
    больше. Ни одного общего слова — не угадываем."""
    pw = _place_words(division)
    if not pw or not warehouses:
        return None
    best, bs, bt = None, 0, 0.0
    for w in warehouses:
        name, t = (w[0], float(w[1] or 0)) if isinstance(w, (list, tuple)) else (str(w), 0.0)
        cw = _place_words(name)
        score = 0
        for a in pw:
            for b in cw:
                if a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a))):
                    score += 1
                    break
        if score > bs or (score == bs and score > 0 and t > bt):
            best, bs, bt = name, score, t
    return best if bs > 0 else None


def match_plan_items(items, nm_rows):
    """Разложить плановые позиции Excel по нашей НМК.

    items   — позиции версии расчёта ({nomenclature, volume, cost, …});
    nm_rows — строки факта 1С по этому же проекту ({c: код, n: имя, …}).

    -> (marks, buckets):
      marks   — по позиции: код нашей НМК или None (в том же порядке);
      buckets — {"mt": {код: [т, ₽]}, "ug": [т, ₽, шт], "un": [т, ₽, шт]},
                где ug — «марка в плане не указана» (кандидатов несколько),
                un — не легло ни на одну нашу НМК.
    """
    cands = [(n, nomen_sig(n.get("n") or "")) for n in (nm_rows or [])]
    marks, places = [], []
    mt, ug, un = {}, [0.0, 0.0, 0], [0.0, 0.0, 0]
    mw = {}                                   # код -> {наш склад: [т, ₽]}
    for it in (items or []):
        vol = float(it.get("volume") or 0)
        cost = float(it.get("cost") or 0)
        ps = nomen_sig(it.get("nomenclature") or "")
        best, bs = [], 0
        for n, cs in cands:
            sc = _nm_score(ps, cs)
            if sc is None:
                continue
            if sc > bs:
                best, bs = [n], sc
            elif sc == bs:
                best.append(n)
        if len(best) > 1 and bs >= 60:
            # ⚠️ ТАЙ-БРЕЙК ТОЛЬКО ПРИ НАСТОЯЩЕМ СИГНАЛЕ (bs >= 60: сошлись
            # марка, размер или марка кабеля). При ничьей на общем правиле
            # «лом без марки» (30) выбирать НЕЛЬЗЯ — там кандидаты равны по
            # смыслу, и любой выбор был бы выдумкой: такие тонны идут в
            # «марка в плане не указана».
            # Сначала — ближе по названию, потом самое простое имя: плановая
            # строка тоже без уточнений в скобках.
            # ⚠️ СНАЧАЛА — ТО, ЧТО РЕАЛЬНО КУПИЛИ. При ничьей между «Труба
            # НКТ 60 (т)» (куплено 568 т) и «Труба НКТ 60х5 б/у (т)»
            # (куплено 0) план должен лечь на первую: вешать план на позицию
            # с нулевой закупкой бессмысленно — сравнивать будет не с чем.
            bought = [x for x in best if abs(float(x.get("t") or 0)) > 0.0005]
            if bought and len(bought) < len(best):
                best = bought
            sims = [(_nm_sim(ps["w"], nomen_sig(x.get("n") or "")["w"]), x)
                    for x in best]
            top = max(v for v, _ in sims)
            near = [x for v, x in sims if v >= top - 1e-9]
            if len(near) == 1:
                best = near
            else:
                shortest = min(len(x.get("n") or "") for x in near)
                short = [x for x in near if len(x.get("n") or "") == shortest]
                best = short if len(short) == 1 else near
        if len(best) == 1:
            code = best[0].get("c") or ""
            marks.append(code)
            a = mt.setdefault(code, [0.0, 0.0])
            a[0] += vol
            a[1] += cost
            # место хранения плана -> наш склад оприходования этой позиции
            pl = match_place(it.get("division") or it.get("supplier") or "",
                             best[0].get("w") or [])
            places.append(pl)
            if pl:
                b = mw.setdefault(code, {}).setdefault(pl, [0.0, 0.0])
                b[0] += vol
                b[1] += cost
        else:
            marks.append(None)
            places.append(None)
            box = ug if best else un
            box[0] += vol
            box[1] += cost
            box[2] += 1
    buckets = {"mt": {k: [round(v[0], 3), round(v[1], 2)] for k, v in mt.items()},
               "mw": {k: {w: [round(v[0], 3), round(v[1], 2)] for w, v in d.items()}
                      for k, d in mw.items()},
               "mp": places,
               "ug": [round(ug[0], 3), round(ug[1], 2), ug[2]],
               "un": [round(un[0], 3), round(un[1], 2), un[2]]}
    return marks, buckets


# ⚠️ МУСОР В ПЛАНОВЫХ ПОЗИЦИЯХ. Парсер иногда прихватывает из листа строку
# итогов или уезжает на колонку правее: в «номенклатуре» оказывается число
# («877190.2625926045»), а в объёме — рубли (145 900 000 «тонн»). Таких строк
# 42 из 9 944, но они одни давали 297 млн тонн плана против реальных 1,4 млн.
# Отсекаем по имени: номенклатура не бывает голым числом и не начинается со
# слов «итого» / «сумма» / «всего».
def clean_plan_items(items):
    out = []
    for x in (items or []):
        n = (x.get("nomenclature") or "").strip()
        if not n or re.fullmatch(r"[\d\s.,%/-]*", n):
            continue
        if re.match(u"^(итого|сумма|всего|порядковый)", n.lower()):
            continue
        out.append(x)
    return out


# ---------------------------------------------------------------------------
# ФАКТ ВЫРУЧКИ: регистр «Выручка и себестоимость продаж» (14.09.2026)
# ---------------------------------------------------------------------------
# Выгрузка 1С: одна строка = документ реализации × номенклатура × операция;
# выручка и себестоимость лежат в РАЗНЫХ строках одного и того же ключа
# (строка с количеством и выручкой, рядом строка с нулевым количеством и
# себестоимостью). Кода номенклатуры в выгрузке нет — только имя («Лом 5А;
# Основной склад»), серия записана лишь у 14,9 % выручки. Поэтому рубли
# привязываются к НАШИМ строкам продаж по ключу «номер документа + имя
# номенклатуры», а серию получают уже от нашей раскладки — той же, что у
# тонн. Внутри ключа выручка делится между нашими строками ПРОПОРЦИОНАЛЬНО
# НАШЕМУ количеству: тогда в отчёт попадает ровно 100 % выручки ключа, даже
# если количества в двух регистрах разошлись (554 ключа из 33 245).
_REV_DOC = re.compile(r"^(.*?)\s+(\S+)\s+от\s+(\d{2}\.\d{2}\.\d{4})")


def load_revenue(path):
    """-> ({(номер документа, имя номенклатуры): [кол-во, выручка без НДС,
    себестоимость без НДС]}, статистика)."""
    out = defaultdict(lambda: [0.0, 0.0, 0.0])
    st = {"rows": 0, "no_doc": 0, "rev": 0.0, "cost": 0.0, "max_date": ""}
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        hdr = next(r)
        ix = {h: i for i, h in enumerate(hdr)}
        need = ("ЗаказКлиента", "АналитикаУчетаНоменклатуры", "Количество",
                "СуммаВыручкиБезНДС", "СтоимостьБезНДС")
        miss = [c for c in need if c not in ix]
        if miss:
            raise SystemExit("В выгрузке выручки нет колонок: %s" % ", ".join(miss))
        for row in r:
            st["rows"] += 1
            m = _REV_DOC.match(row[ix["ЗаказКлиента"]] or "")
            if not m:
                st["no_doc"] += 1
                continue
            num = m.group(2)
            nom = (row[ix["АналитикаУчетаНоменклатуры"]] or "").split(";")[0].strip()
            q = float(row[ix["Количество"]] or 0)
            rv = float(row[ix["СуммаВыручкиБезНДС"]] or 0)
            c = float(row[ix["СтоимостьБезНДС"]] or 0)
            a = out[(num, nom)]
            a[0] += q
            a[1] += rv
            a[2] += c
            st["rev"] += rv
            st["cost"] += c
            d = (row[ix["Период"]] or "")[:10] if "Период" in ix else ""
            if d > st["max_date"]:
                st["max_date"] = d
    return dict(out), st


def match_revenue(rev, sales_keys):
    """Подобрать ключ выручки к КАЖДОМУ нашему ключу продаж (номер, имя).

    sales_keys — {(номер, имя): наше количество}. Сначала точное совпадение
    имени; у остальных — внутри того же документа: если непарная строка с
    каждой стороны одна, они пара; иначе пара по равенству количества. Имена
    расходятся, когда справочник переименовали («Труба НКТ 73 (неликвид)» у
    нас против «Труба НКТ 73 (т) Z» в выручке) — таких ключей ~3 %.
    -> ({наш ключ: ключ выручки}, статистика)."""
    link, used = {}, set()
    for k in sales_keys:
        if k in rev:
            link[k] = k
            used.add(k)
    by_doc_rev = defaultdict(list)
    for k in rev:
        if k not in used:
            by_doc_rev[k[0]].append(k)
    by_doc_ours = defaultdict(list)
    for k in sales_keys:
        if k not in link:
            by_doc_ours[k[0]].append(k)
    st = {"exact": len(link), "by_doc": 0, "by_qty": 0, "unmatched": 0}
    for num, ours in by_doc_ours.items():
        cands = [k for k in by_doc_rev.get(num, []) if k not in used]
        if len(ours) == 1 and len(cands) == 1:
            link[ours[0]] = cands[0]
            used.add(cands[0])
            st["by_doc"] += 1
            continue
        for ok in ours:
            q = sales_keys[ok]
            hit = [k for k in cands if k not in used and abs(rev[k][0] - q) < 0.0015]
            if len(hit) == 1:
                link[ok] = hit[0]
                used.add(hit[0])
                st["by_qty"] += 1
            else:
                st["unmatched"] += 1
    return link, st


# ---------------------------------------------------------------------------
# СДЕЛКИ БИТРИКСА (DEAL_*.xlsx): доля лота и юрлицо-победитель (14.09.2026)
# ---------------------------------------------------------------------------
# Заказчик: «таблица показывает, в каком процентном соотношении был выигран
# тот или иной БП и на какое юрлицо». Лот часто делится между нами
# («Металлолом») и партнёром («УВМ»); план в Excel — на ВЕСЬ лот, поэтому
# сравнивать с нашим фактом надо план × нашу долю. Доли в выгрузке пишут
# как попало: 1 / 100 / 0.5 / 50 / «50%?» / «50%? Сов/Урай» — число ≤ 1 это
# доля, больше — проценты, вопросительный знак — доля под сомнением (флаг).
_DEAL_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def _deal_share(v):
    """-> (доля 0..1 или None, под сомнением?)"""
    if v is None or v == "":
        return None, False
    if isinstance(v, (int, float)):
        x = float(v)
        return (x / 100.0 if x > 1 else x), False
    t = str(v).strip()
    m = _DEAL_NUM.search(t)
    if not m:
        return None, "?" in t
    x = float(m.group(1).replace(",", "."))
    if "%" in t or x > 1:
        x = x / 100.0
    return x, ("?" in t)


def load_deals(path):
    """-> ({номер БП: {share, share_q, uvm, ul, stage, lot_t, contract_t}},
    статистика). Ключ — «№ в текущем реестре»."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h or "").strip() for h in next(rows)]

    def col(*keys):
        for i, h in enumerate(hdr):
            hl = h.lower()
            if all(k in hl for k in keys):
                return i
        return None

    c_no, c_share, c_uvm = col("№ в текущем"), col("доля металлолом"), col("доля увм")
    c_ul, c_stage, c_lot = col("юл выиграло"), col("стадия"), col("объем лота")
    c_contr, c_name = col("объем по договору"), col("название сделки")
    if c_no is None:
        raise SystemExit("В файле сделок нет колонки «№ в текущем реестре»")
    out, st = {}, {"rows": 0, "with_no": 0, "with_share": 0, "dup": 0}
    for r in rows:
        st["rows"] += 1
        no = r[c_no]
        if no is None or str(no).strip() == "":
            continue
        no = str(int(no)) if isinstance(no, (int, float)) else str(no).strip()
        st["with_no"] += 1
        share, q = _deal_share(r[c_share]) if c_share is not None else (None, False)
        uvm, _ = _deal_share(r[c_uvm]) if c_uvm is not None else (None, False)
        rec = {"share": share, "share_q": bool(q), "uvm": uvm,
               "ul": (str(r[c_ul] or "").strip() if c_ul is not None else ""),
               "stage": (str(r[c_stage] or "").strip() if c_stage is not None else ""),
               "lot_t": _num_or_none(r[c_lot]) if c_lot is not None else None,
               "contract_t": _num_or_none(r[c_contr]) if c_contr is not None else None,
               "deal": (str(r[c_name] or "").strip()[:80] if c_name is not None else "")}
        if share is not None:
            st["with_share"] += 1
        if no in out:
            st["dup"] += 1
            # два раза один номер — берём запись, где доля заполнена
            if out[no].get("share") is not None and share is None:
                continue
        out[no] = rec
    return out, st


def _num_or_none(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _DEAL_NUM.search(str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def load_bp_versions(path, choices=None):
    """-> (список версий для вкладки, статистика).

    Версия: {bp, name, sheet, file, ver (дата версии), months, status,
    lukoil, dsp, profit, revenue, choice ('ok'/'bad'/''), by, at, dflt}.
    ⚠️ dflt — «по умолчанию»: у БП без явного выбора помечается версия с
    ПОСЛЕДНЕЙ датой версии (слово заказчика: «по умолчанию выставляй ту,
    которая была изменена последней»). Это подсветка, не выбор экономиста.
    ⚠️ ТРЕКИ (правка заказчика 01.09.2026: «одна у лук и одна у дсп, и правило
    по умолчанию для всех»): верная версия выбирается НА ТРЕК — «luk», «dsp»,
    а у версий без признаков — «other». Версия лук+дсп занимает оба трека.
    dflt — строка вида "luk+dsp": в каких треках эта версия является
    умолчанием.
    """
    choices = choices or {}
    d = json.load(open(path, encoding="utf-8"))
    rows, seen = [], set()
    for order, r in enumerate(d.get("rows") or []):
        name = (r.get("name") or "").strip()
        if not name or name in seen:      # один лист = одна версия, без дублей
            continue
        seen.add(name)
        m = _BPM_NUM.match(name)
        ch = choices.get(name) or {}
        rows.append({
            "bp": m.group(1) if m else "",
            "name": name,
            "sheet": (r.get("sheet") or "")[:80],
            "file": (r.get("file") or "")[:120],
            "ver": bpm_version_date(name),
            "months": r.get("months"),
            "status": r.get("status") or "",
            "lukoil": r.get("lukoil") or "",
            "dsp": r.get("dsp") or "",
            "profit": r.get("profit"),
            "revenue": r.get("revenue"),
            # ПЛАНОВЫЕ ПОЗИЦИИ ВЕРСИИ (парсер 02.09.2026): номенклатура текстом
            # Лукойла, место хранения, объём и цена — вторая половина мостика
            # «план Excel <-> факт 1С». Извлекаются у 98 % версий.
            # ⚠️ ЧЕРЕЗ clean_plan_items: строки итогов и съехавшие колонки
            # (рубли в графе тонн) иначе дают 297 млн тонн плана вместо 1,4 млн
            "items": clean_plan_items(r.get("items")),
            "plan_t": round(sum(float(x.get("volume") or 0)
                                for x in clean_plan_items(r.get("items"))), 3),
            "plan_sum": round(sum(float(x.get("cost") or 0)
                                  for x in clean_plan_items(r.get("items"))), 2),
            # порядок листа в книге: парсер идёт по листам по порядку, и
            # позиция строки в выгрузке = позиция листа (см. умолчание ниже)
            "order": order,
            "choice": ch.get("verdict") or "",
            "by": ch.get("by") or "",
            "at": (ch.get("at") or "")[:16],
            "dflt": "",
        })
    # «по умолчанию» — ПО ТРЕКАМ: в каждом треке (лук / дсп / без признаков)
    # последняя дата среди неотклонённых; трек, где экономист уже выбрал (ok),
    # умолчания не получает
    # ⚠️ ТРЕК — ПО ИМЕНИ ЛИСТА, а не по признакам файла. Парсер ставит
    # лук/дсп на ФАЙЛ ЦЕЛИКОМ (по названиям всех листов книги), и в книге с
    # лук- и дсп-листами каждая версия выходила «лук+дсп» — треки сливались, и
    # выбор снова становился единственным (поймано заказчиком 01.09.2026 со
    # второй попытки). Имя самого листа («v0 05.08.25_дсп») говорит точнее;
    # файловый признак остаётся запасным для листов без слов в имени.
    def _tracks(r):
        sh = (r["sheet"] or r["name"]).lower()
        t = []
        if "лук" in sh: t.append("luk")
        if "дсп" in sh: t.append("dsp")
        if not t:
            if r["lukoil"] == "да": t.append("luk")
            if r["dsp"] == "да": t.append("dsp")
        return t or ["other"]
    by_bp = defaultdict(list)
    for r in rows:
        if r["bp"]:
            by_bp[r["bp"]].append(r)
    for bp, lst in by_bp.items():
        for tr in ("luk", "dsp", "other"):
            t_rows = [x for x in lst if tr in _tracks(x)]
            if not t_rows or any(x["choice"] == "ok" for x in t_rows):
                continue
            cand = [x for x in t_rows if x["choice"] != "bad"] or t_rows
            # ⚠️ УМОЛЧАНИЕ — ПОСЛЕДНИЙ ЛИСТ КНИГИ (заказчик 14.09.2026: «если не
            # стоит галочка, бери по последнему листу за версию»). Раньше брали
            # самую позднюю дату в имени версии; дата — запасной ключ, когда
            # порядок листов неизвестен (старые выгрузки без «order»).
            best = max(cand, key=lambda x: (x.get("order", -1), x["ver"], x["name"]))
            best["dflt"] = (best["dflt"] + "+" + tr).strip("+")
    stats = {"rows": len(rows), "bps": len(by_bp),
             "chosen": sum(1 for r in rows if r["choice"] == "ok"),
             "rejected": sum(1 for r in rows if r["choice"] == "bad"),
             "generated": (d.get("generated") or "")[:16],
             "with_rev": sum(1 for r in rows if r.get("revenue") is not None),
             "with_items": sum(1 for r in rows if r.get("items")),
             "items": sum(len(r.get("items") or []) for r in rows)}
    return rows, stats


def load_bp_version_choice(path):
    """data/bp_version_choice.json -> {имя версии -> {verdict, bp, by, at}}."""
    if not path or not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
