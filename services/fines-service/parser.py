# -*- coding: utf-8 -*-
"""Извлечение данных из PDF постановлений: штрафы ГИБДД/МУГАДН и документы ФССП."""
import re
import subprocess
import shutil
from datetime import datetime


def extract_text(pdf_path: str) -> str:
    """Текст из PDF: pdftotext (если есть) c fallback на pdfplumber."""
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(
                ["pdftotext", "-layout", pdf_path, "-"],
                capture_output=True, timeout=60,
            )
            text = out.stdout.decode("utf-8", errors="replace")
            if len(text.strip()) > 200:
                return text
        except Exception:
            pass
    import pdfplumber
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def _norm(text: str) -> str:
    # склеиваем переносы строк внутри предложений, убираем маркеры "MMM"
    text = text.replace("­", "")
    text = re.sub(r"MMM", " ", text)
    return re.sub(r"\s+", " ", text)


def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%d.%m.%Y").date().isoformat()
    except Exception:
        return None


def _amount(s: str):
    s = s.replace(" ", " ").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def classify(text: str) -> str:
    """'fine' — постановление о штрафе, 'fssp' — документ судебных приставов.

    Упоминание пристава в шаблонном тексте штрафа («…может быть направлено судебному
    приставу…») не делает документ ФССПшным: смотрим на номер ИП и шапку документа.
    """
    t = _norm(text)
    has_ip_number = bool(re.search(r"\d{4,7}/\d{2}/\d{4,6}-ИП\b", t))
    fssp_header = bool(re.search(r"ФССП|службы судебных приставов|судебный пристав-исполнитель", t[:900], re.I))
    if has_ip_number or fssp_header:
        return "fssp"
    if re.search(r"ПОСТАНОВЛЕНИЕ\s*№?\s*\d{15,25}", t) or "об административном правонарушении" in t:
        return "fine"
    if re.search(r"судебн\w+ пристав|исполнительн\w+ производств", t, re.I):
        return "fssp"
    return "unknown"


# Вид нарушения по статье КоАП — главный источник; ключевые слова только уточняют
ARTICLE_MAP = {
    "12.1": "Управление незарегистрированным ТС",
    "12.2": "Нарушение правил установки гос. номеров",
    "12.5": "Управление ТС с неисправностями",
    "12.6": "Непристегнутый ремень безопасности",
    "12.9": "Превышение скорости",
    "12.12": "Проезд на запрещающий сигнал светофора",
    "12.13": "Нарушение правил проезда перекрестка",
    "12.14": "Нарушение правил маневрирования",
    "12.15": "Нарушение правил расположения ТС на дороге",
    "12.16": "Несоблюдение требований знаков или разметки",
    "12.17": "Движение по полосе для маршрутных ТС",
    "12.18": "Непредоставление преимущества пешеходу",
    "12.19": "Нарушение правил остановки/стоянки",
    "12.20": "Нарушение правил пользования световыми приборами",
    "12.21.1": "Перегруз (движение тяжеловесного ТС)",
    "12.21.2": "Нарушение правил перевозки опасных грузов",
    "12.21.3": "Неоплата проезда большегруза («Платон»)",
    "12.21.4": "Неоплата проезда по платной дороге",
    "12.23": "Нарушение правил перевозки людей",
    "12.36.1": "Пользование телефоном за рулем",
    "12.37": "Отсутствие ОСАГО",
}

VIOLATION_MAP = [
    (r"пассажир", "Непристегнутый пассажир"),
    (r"не пристегнут|ремн[её]м безопасности", "Непристегнутый ремень безопасности"),
    (r"превы\w+ .{0,40}скорост", None),  # обрабатывается отдельно (нужно кол-во км/ч)
    (r"телефон", "Пользование телефоном за рулем"),
    (r"светов\w+ приборами", "Нарушение правил пользования световыми приборами"),
    (r"платн\w+ (автомобильн\w+ )?дорог", "Неоплата проезда по платной дороге"),
    (r"стоянк|парковк", "Нарушение правил остановки/стоянки"),
    (r"полос\w+ .{0,30}маршрутн", "Движение по полосе для маршрутных ТС"),
    (r"разметк", "Несоблюдение требований разметки"),
    (r"светофор|запрещающ\w+ сигнал", "Проезд на запрещающий сигнал светофора"),
    (r"пешеход", "Непредоставление преимущества пешеходу"),
    (r"регистрац\w+ .{0,20}ТС|не зарегистрирован", "Управление незарегистрированным ТС"),
]


def _short_violation(sentence: str, full_text: str) -> str:
    s = sentence.lower()
    m = re.search(r"скорост\w*[^,]{0,80}?на\s+(\d+)\s*км/ч", s) or \
        re.search(r"на\s+(\d+)\s*км/ч", s)
    if re.search(r"скорост", s) and m:
        return f"Превышение скорости на {m.group(1)} км/ч"
    for pattern, label in VIOLATION_MAP:
        if label and re.search(pattern, s):
            return label
    # запасной вариант — очищенный фрагмент из текста
    frag = sentence.strip().rstrip(".")
    frag = re.sub(r"^водитель,?\s*", "", frag, flags=re.I)
    return (frag[:180] + "…") if len(frag) > 180 else (frag or "См. постановление")


def _norm_company(raw: str) -> str:
    raw = raw.strip().strip('",« »')
    raw = re.sub(r'^["«]*\s*(ООО|АО|ПАО|ЗАО|ИП)\s*[«"]*', r"\1 «", raw)
    raw = re.sub(r'["»]*$', "»", raw)
    if "«" not in raw:
        return raw.rstrip("»")
    return raw


def parse_fine(text: str) -> dict:
    t = _norm(text)
    d = {}

    m = re.search(r"ПОСТАНОВЛЕНИЕ\s*№?\s*(\d{15,25})", t) or re.search(r"УИН[:\s]*(\d{15,25})", t)
    d["number"] = m.group(1) if m else None

    # дата постановления: "Дело об АПН №... от ДД.ММ.ГГГГ" или первая дата после номера
    m = re.search(r"Дело об АПН [^ ]+ от (\d{2}\.\d{2}\.\d{4})", t)
    if not m:
        m = re.search(r"правонарушении\s+(\d{2}\.\d{2}\.\d{4})", t)
    if not m:
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", t)
    d["date"] = _parse_date(m.group(1)) if m else None

    m = re.search(
        r"транспортн\w+ средств\w+\s*\(далее\s*[-–]\s*ТС\)\s*(.+?),\s*государственный регистрационный знак\s*([А-ЯA-Z0-9]{6,12})",
        t, re.I)
    if not m:
        m = re.search(
            r"марка/модель\s+(.+?)\s+тип.*?государственный регистрационный знак\s+([А-ЯA-Z0-9]{6,12})",
            t, re.I)
    if not m:
        m = re.search(
            r"транспортн\w+ средств\w+ марки\s+(.+?)\s*,\s*государственный регистрационный знак\s*([А-ЯA-Z0-9]{6,12})",
            t, re.I)
    if not m:
        m = re.search(r"ТС[:\s]+(.+?),?\s*гос(?:ударственный)?\.?\s*(?:рег)?[^А-Я0-9]{0,20}знак\s*([А-ЯA-Z0-9]{6,12})", t, re.I)
    if m:
        vehicle = re.sub(r"\s+", " ", m.group(1)).strip()
        # "МАЗ 643028 643028" -> "МАЗ 643028"
        parts = vehicle.split()
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            vehicle = " ".join(parts[:-1])
        d["vehicle"] = vehicle
        d["plate"] = m.group(2)
    else:
        d["vehicle"] = d["plate"] = None

    m = re.search(r"явля\w+\s+[\"«]*(.+?)[\"»]*\s*,\s*дата регистрации", t)
    d["company"] = _norm_company(m.group(1)) if m else None
    # ИНН берём ближайший ПОСЛЕ упоминания собственника (в начале может быть ИНН получателя платежа)
    inn_from = m.end() if m else 0
    m = re.search(r"ИНН[:\s]*(\d{10,12})", t[inn_from:]) or re.search(r"ИНН[:\s]*(\d{10,12})", t)
    d["inn"] = m.group(1) if m else None

    # статья: только сам номер (ч. X ст. Y), без хвоста из названия закона
    m = re.search(
        r"предусмотренн\w+\s+(?:часть[юи]?\s*(\d+)\s*стать[еий]\w*|ч\.?\s*(\d+)\s*ст\.?|стать[еий]\w*|ст\.?)\s*([\d.]+\d)",
        t, re.I)
    if m:
        part = m.group(1) or m.group(2)
        art = f"ч. {part} ст. {m.group(3)}" if part else f"ст. {m.group(3)}"
        law = re.search(r"Закона\s+(\S+\s+(?:края|области|округа))", t[m.end():m.end() + 60])
        if law:
            art += f" Закона {law.group(1)}"
        d["article"] = art
    else:
        d["article"] = None

    m = re.search(r"в размере\s*([\d\s ]+(?:[.,]\d{2})?)\s*(?:\([^)]*\))?\s*руб", t)
    d["amount"] = _amount(m.group(1)) if m else None
    m = re.search(r"75 процентов[^.]*?([\d\s ]+)\s*руб", t)
    d["discount_amount"] = _amount(m.group(1)) if m else None

    # дата/время/место нарушения
    m = re.search(r"УСТАНОВИЛ:\s*(\d{2}\.\d{2}\.\d{4}),?\s+(?:в\s+)?(\d{2}:\d{2}(?::\d{2})?)\s+по адресу\s+(.+?)(?:,\s*водитель|\s+водитель|\s+собственник|,\s*в нарушение)", t)
    if not m:
        # формат административных комиссий: "10.07.2026 г. в период с 10:36:51 до 11:59:48 ... по адресу: ..."
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*г?\.?\s*в период с\s*(\d{2}:\d{2}(?::\d{2})?)[^.]{0,120}?по адресу:?\s*(.+?)(?:\s*\(координаты|,?\s*было|\s+в нарушение)", t)
    if m:
        d["violation_date"] = _parse_date(m.group(1))
        d["violation_time"] = m.group(2)
        d["violation_place"] = m.group(3).strip().rstrip(",")
    else:
        # платные дороги (МУГАДН): "УСТАНОВИЛ: 16.07.2026 г. в 10:34:53 лицо ... по участку платной автомобильной дороги: X"
        m = re.search(r"УСТАНОВИЛ:\s*(\d{2}\.\d{2}\.\d{4})\s*г?\.?\s*в\s*(\d{2}:\d{2}(?::\d{2})?)", t)
        d["violation_date"] = _parse_date(m.group(1)) if m else None
        d["violation_time"] = m.group(2) if m else None
        pm = re.search(r"платн\w+ автомобильн\w+ дорог\w*:\s*(.+?)(?:,\s*ш\.|\.\s|$)", t)
        d["violation_place"] = ("платная дорога " + pm.group(1).strip()) if pm else None

    # описательная часть постановления (между УСТАНОВИЛ и ПОСТАНОВИЛ) — только в ней ищем ключевые слова
    seg_m = re.search(r"У\s?С\s?Т\s?А\s?Н\s?О\s?В\s?И\s?Л\s?А?\s*:(.+?)П\s?О\s?С\s?Т\s?А\s?Н\s?О\s?В\s?И\s?Л", t, re.S)
    segment = seg_m.group(1) if seg_m else None

    m = re.search(r"в нарушение\s+(?:п\.?[\d.]+\s*(?:\([^)]*\))?\s*ПДД(?:\s*РФ)?|требовани\w+[^,]{0,80}ПДД(?:\s*РФ)?)\s*,?\s*(.+?)[.,]\s*(?:Собственником|Руководствуясь|чем)", t)
    if m:
        d["violation"] = _short_violation(m.group(1), t)
    else:
        m2 = re.search(r"водитель[^.]*?\s(.+?)\.\s*Собственником", t)
        d["violation"] = _short_violation(m2.group(1), t) if m2 else None
    # превышение скорости: точное значение из фразы "превысив ... на NN км/ч"
    m = re.search(r"превыси\w+[^.]{0,120}?на\s+(\d+)\s*км/ч", t, re.I)
    if m:
        d["violation"] = f"Превышение скорости на {m.group(1)} км/ч"

    # статья КоАП — главный источник вида нарушения (ключевые слова из всего текста
    # ненадёжны: «телефон» есть в каждом постановлении в контактах)
    art_num = re.search(r"ст\.\s*([\d.]+\d)", d["article"] or "")
    art_label = ARTICLE_MAP.get(art_num.group(1)) if art_num else None
    if art_label:
        keep_speed = art_num.group(1) == "12.9" and (d["violation"] or "").startswith("Превышение скорости на")
        if not keep_speed:
            d["violation"] = art_label
        if art_num.group(1) == "12.6" and re.search(r"пассажир", segment or "", re.I):
            d["violation"] = "Непристегнутый пассажир"
        if art_num.group(1) == "12.21.1":
            mass = re.search(r"Превышение общей массы\s*\(%\)\s*([\d,.]+)\s*%", t)
            axle = re.search(r"Превышение нагрузки на ось\s*\(%\)\s*([\d,.]+)\s*%", t)
            details = [f"масса +{mass.group(1).split(',')[0].split('.')[0]} %" if mass and mass.group(1)[0] != "0" else None,
                       f"ось +{axle.group(1).split(',')[0].split('.')[0]} %" if axle and axle.group(1)[0] != "0" else None]
            details = [x for x in details if x]
            if details:
                d["violation"] = f"Перегруз ({', '.join(details)})"
    elif not d["violation"] or len(d["violation"]) > 120:
        # нестандартная формулировка без известной статьи — ключевые слова, но только в описательной части
        seg = segment or t
        if re.search(r"газон|объект\w* озеленения|цветник", seg, re.I):
            d["violation"] = "Размещение ТС на газоне (объекте озеленения)"
        elif re.search(r"платн\w+ (?:городск\w+ )?парковк", seg, re.I):
            d["violation"] = "Неоплата платной парковки"
        else:
            for pattern, label in VIOLATION_MAP:
                if label and re.search(pattern, seg, re.I):
                    d["violation"] = label
                    break

    m = re.search(r"^\s*(?:КОПИЯ\s*)?(.{5,120}?)\s*(?:КОПИЯ)?\s*ПОСТАНОВЛЕНИЕ", _norm(text[:600]))
    if m:
        d["authority"] = m.group(1).strip()
    else:
        m = (re.search(r"(Административная комиссия .{5,90}?округ\w*)", t)
             or re.search(r"(ЦАФАП[^,.;]{3,90}?(?:МУГАДН|Ространснадзор\w*|ГИБДД[^,.;]{0,60}))", t)
             or re.search(r"((?:МТУ|Межрегиональное территориальное управление) Ространснадзора[^,.;]{0,80})", t))
        d["authority"] = m.group(1).strip() if m else None
    return d


FSSP_TYPES = [
    (r"о возбуждении исполнительного производства", "Возбуждение ИП"),
    (r"о наложении ареста", "Арест денежных средств"),
    (r"о снятии ареста и обращении взыскания", "Снятие ареста, обращение взыскания"),
    (r"о снятии ареста", "Снятие ареста"),
    (r"об обращении взыскания", "Обращение взыскания на ДС"),
    (r"об окончании исполнительного производства", "Окончание ИП"),
    (r"о взыскании исполнительского сбора", "Исполнительский сбор"),
    (r"об отмене", "Отмена постановления"),
]


def parse_fssp(text: str) -> dict:
    t = _norm(text)
    d = {"doc_type": "Документ ФССП", "title": None}

    m = re.search(r"Постановление (о[бн]?\s+.{5,120}?)(?:\s+\d{2}\.\d{2}\.\d{4}|\s+г\.\s|$)", t)
    if m:
        d["title"] = "Постановление " + m.group(1).strip()
    # тип определяем сначала по заголовку, затем по всему тексту
    for source in ([d["title"]] if d["title"] else []) + [t]:
        matched = False
        for pattern, label in FSSP_TYPES:
            if re.search(pattern, source, re.I):
                d["doc_type"] = label
                matched = True
                break
        if matched:
            break

    m = re.search(r"исполнительного производства\s*(?:от\s*(\d{2}\.\d{2}\.\d{4})\s*)?№\s*([\d/]+-ИП)", t)
    if m:
        d["ip_date"] = _parse_date(m.group(1)) if m.group(1) else None
        d["ip_number"] = m.group(2)
    else:
        d["ip_date"] = d["ip_number"] = None

    # номер постановления о штрафе, на основании которого возбуждено ИП
    m = re.search(r"по делу об административном правонарушении[^№]{0,80}№\s*(\d{15,25})", t)
    if not m:
        m = re.search(r"по делу\s*№\s*(\d{15,25})", t)
    if not m:
        m = re.search(r"исполнительного документа[^№]{0,120}№\s*(\d{15,25})", t)
    d["fine_ref"] = re.sub(r"СП$", "", m.group(1)) if m else None

    m = re.search(r"должника\s*\(тип должника[^)]*\)\s*:\s*(.+?),\s*ИНН\s*(\d{10,12})", t)
    if m:
        d["debtor"] = _norm_company(m.group(1))
        d["debtor_inn"] = m.group(2)
    else:
        m = re.search(r"Получатель:\s*([^,]{1,80}?)(?:\s{2,}|,|Постановление|$)", t)
        d["debtor"] = _norm_company(m.group(1)) if m else None
        d["debtor_inn"] = None

    m = re.search(r"государственный регистрационный знак\s*([А-ЯA-Z0-9]{6,12})", t)
    d["plate"] = m.group(1) if m else None
    m = re.search(r"марка/модель\s+(.+?)\s+тип", t)
    d["vehicle"] = m.group(1).strip() if m else None

    m = re.search(r"в размере:?\s*([\d\s ]+(?:[.,]\d{2})?)\s*(?:\([^)]*\))?\s*руб", t)
    d["amount"] = _amount(m.group(1)) if m else None

    # реквизиты самого документа ФССП: "от 03.07.2026 № 77047/26/3308282"
    m = re.search(r"от\s*(\d{2}\.\d{2}\.\d{4})\s*№\s*(\d{4,6}/\d{2}/\d{5,12})(?!\d*-ИП)", t)
    if m:
        d["doc_date"] = _parse_date(m.group(1))
        d["doc_number"] = m.group(2)
    else:
        d["doc_date"] = d["ip_date"]
        d["doc_number"] = None
    return d


def fssp_fine_details(text: str, fine_ref: str) -> dict:
    """Данные исходного штрафа из текста постановления ФССП (для создания записи штрафа)."""
    t = _norm(text)
    d = {"date": None, "article": None, "violation": None}
    if fine_ref:
        m = re.search(re.escape(fine_ref) + r"(?:СП)?\s+от\s+(\d{2}\.\d{2}\.\d{4})", t)
        if m:
            d["date"] = _parse_date(m.group(1))
    m = re.search(r"предусмотренного\s+(.+?)\s*КоАП", t)
    if m:
        art = m.group(1).strip().rstrip(",")
        art = re.sub(r"часть[юи]?\s*(\d+)\s*стать[еий]\w*\s*", r"ч. \1 ст. ", art, flags=re.I)
        art = re.sub(r"^стать[еий]\w*\s*", "ст. ", art, flags=re.I)
        d["article"] = re.sub(r"\s+", " ", art).strip()
        if "12.21.4" in d["article"]:
            d["violation"] = "Неоплата проезда по платной дороге"
    return d


def parse_pdf(pdf_path: str) -> dict:
    """Главная точка входа: {'kind': 'fine'|'fssp'|'unknown', 'data': {...}, 'text': str}."""
    text = extract_text(pdf_path)
    kind = classify(text)
    if kind == "fine":
        data = parse_fine(text)
    elif kind == "fssp":
        data = parse_fssp(text)
    else:
        data = {}
    return {"kind": kind, "data": data, "text": text}
