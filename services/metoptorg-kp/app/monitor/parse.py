"""Разбор торгового объявления: продают или покупают, что и почём.

Объявления в отраслевых каналах пишут списком: «Продам: НКТ 73 б/у — 15 000 р/т,
ПЭД 45-117 — 30 000 р/шт». Поэтому разбираем построчно и возвращаем позицию на
каждую распознанную строку, а не одну запись на сообщение — иначе цена первой
позиции приписалась бы всему списку.

Направление важнее цены: «продам» даёт ориентир рынка, а «куплю» — это уже
готовый покупатель, то есть лид.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..normalize import classify_family, extract_size_key, series_key

# --- направление сделки

_BUY_RE = re.compile(
    r"\bкупл[юе]м?\b|\bкупим\b|\bзакуп\w*|\bинтересует\b|\bищ[уеё]м?\b|"
    r"\bтребуетс[яи]\b|\bнужн[ыоа]?\b|\bприм[уе]м?\b|\bскупа\w*", re.I)
_SELL_RE = re.compile(
    r"\bпрода[мю]\b|\bпрода[ёе]м\b|\bпродаётся\b|\bпродается\b|\bв наличии\b|"
    r"\bреализуе м?\w*|\bреализу\w*|\bотгруз\w*|\bпредлага\w*|\bотдам\b|"
    r"\bраспродаж\w*", re.I)

# --- цена

_MULT = {"млн": 1_000_000.0, "миллион": 1_000_000.0,
         "тыс": 1_000.0, "т.р": 1_000.0, "тр": 1_000.0, "к": 1_000.0}
_UNIT_WORD = {
    "т": "т", "тн": "т", "тонн": "т", "тонну": "т", "тонны": "т",
    "кг": "кг", "килограмм": "кг",
    "шт": "шт", "штук": "шт", "штуку": "шт", "ед": "шт", "единиц": "шт",
    "м": "м", "метр": "м", "пм": "м", "пог.м": "м", "км": "км",
}
_PRICE_RE = re.compile(
    r"(?P<num>\d[\d\s .,]{0,12}\d|\d)"
    r"\s*(?P<mult>млн|тыс\.?|т\.\s?р\.?|тр\b|к\b)?"
    # «30000 р/шт» пишут без точки после «р» — с шаблоном «р\.» цена терялась
    r"\s*(?P<cur>руб\w*|₽|rub|р(?=\s*[/\s.]|$))?"
    r"(?P<sep>\s*/\s*|\s*за\s+)?\s*"
    r"(?P<unit>тонн\w*|тн\b|\bт\b|кг\b|килограмм\w*|шт\w*|ед\b|единиц\w*|"
    r"пог\.?\s?м\b|\bм\b|км\b)?",
    re.I)
# Слова, при которых число рядом с единицей — это цена, а не количество
_PRICE_CUE_RE = re.compile(r"цен[аыу]|стоимость|прайс|по цене|отдам за", re.I)
# Числа, которые ценой не являются: марки, диаметры, годы, телефоны
_NOT_PRICE_CONTEXT = re.compile(r"[хx*×]\s*$|№\s*$|\bгод\w*\s*$", re.I)
MIN_PRICE = 100.0
MAX_PRICE = 500_000_000.0

# --- контакты и регион

_PHONE_RE = re.compile(
    r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b")
_NICK_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
# Регион ищем по основе слова: в объявлениях пишут «по Пермскому краю»,
# «из Тюмени», «Ханты-Мансийск». Границы слова обязательны — иначе «коми»
# находится внутри «комиссии», а «омск» внутри «Томска».
REGION_STEMS = (
    "перм", "екатеринбург", "свердлов", "тюмен", "сургут", "нижневартовск",
    "когалым", "нефтеюганск", "ханты", "югр", "ямал", "уренгой", "ноябрьск",
    "муравленко", "уфа", "башкир", "башкорт", "казан", "татарстан", "самар",
    "челябин", "ижевск", "удмурт", "оренбург", "томск", "омск", "москв",
    "петербург", "нижний новгород", "альметьевск", "бугульма", "усинск",
    "ухта", "\\bкоми\\b", "новосибирск", "красноярск", "иркутск",
)
_REGION_RE = re.compile(
    r"(?<![а-яё])(" + "|".join(REGION_STEMS) + r")", re.I)


@dataclass
class ParsedListing:
    line_no: int
    text: str
    direction: str | None = None
    price: float | None = None
    price_unit: str | None = None
    family: str | None = None
    size_key: str | None = None
    series_mark: str | None = None
    contacts: str | None = None
    region: str | None = None
    evidence: list[str] = field(default_factory=list)


def detect_direction(text: str) -> str | None:
    """Первое встреченное намерение и есть намерение объявления."""
    buy = _BUY_RE.search(text)
    sell = _SELL_RE.search(text)
    if buy and sell:
        return "buy" if buy.start() < sell.start() else "sell"
    if buy:
        return "buy"
    if sell:
        return "sell"
    return None


def _to_number(raw: str, mult: str | None) -> float | None:
    s = raw.replace(" ", "").replace(" ", "")
    # «15.000» и «15,000» в объявлениях — это разделитель тысяч, а не дробь;
    # дробью считаем только один-два знака после запятой («1,2 млн»)
    if re.fullmatch(r"\d+[.,]\d{3}", s):
        s = s.replace(".", "").replace(",", "")
    else:
        s = s.replace(".", ",")
        parts = s.split(",")
        s = parts[0] + ("." + parts[1] if len(parts) > 1 else "")
        s = s.replace(",", "")
    try:
        value = float(s)
    except ValueError:
        return None
    if mult:
        key = mult.lower().rstrip(".").replace(" ", "").replace(".", "")
        value *= _MULT.get(key, _MULT.get(key[:3], 1.0))
    return value


def _inside_designation(text: str, start: int, end: int) -> bool:
    """Число — часть обозначения изделия, а не цена.

    «ВДМ 80-2400-3.0-117В5»: и 80, и 2400, и 117 сцеплены дефисами с соседними
    числами. Без этой проверки строка со словом «цена» отдавала 2 400 ₽ —
    кусок марки вместо цены.
    """
    prev = text[start - 1] if start else ""
    nxt = text[end] if end < len(text) else ""
    if prev in "-–—/":
        return True
    if nxt in "-–—" and end + 1 < len(text) and text[end + 1].isdigit():
        return True
    if prev.lower() in "хx*×" or nxt.lower() in "хx*×":
        return True
    return bool(nxt.isalpha() and nxt.lower() not in "тшкмер")


def extract_price(text: str) -> tuple[float | None, str | None]:
    """Цена и единица из строки. Берём первую правдоподобную."""
    for m in _PRICE_RE.finditer(text):
        before = text[max(0, m.start() - 2):m.start()]
        if _NOT_PRICE_CONTEXT.search(before):
            continue
        if _inside_designation(text, m.start("num"), m.end("num")):
            continue
        value = _to_number(m.group("num"), m.group("mult"))
        if value is None or not (MIN_PRICE <= value <= MAX_PRICE):
            continue
        unit_raw = (m.group("unit") or "").lower().rstrip(".")
        unit = None
        for key, norm in _UNIT_WORD.items():
            if unit_raw.startswith(key):
                unit = norm
                break
        # «60 шт» — это количество, а не цена. Числу верим, только когда рядом
        # валюта, множитель, дробная черта («15000/т») или слово «цена».
        has_currency = bool(m.group("cur"))
        has_slash = "/" in (m.group("sep") or "")
        if not (has_currency or m.group("mult") or has_slash
                or _PRICE_CUE_RE.search(text)):
            continue
        return value, unit
    return None, None


def extract_contacts(text: str) -> str | None:
    found = _PHONE_RE.findall(text) + [f"@{n}" for n in _NICK_RE.findall(text)]
    seen, out = set(), []
    for c in found:
        c = re.sub(r"\s+", " ", c).strip()
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return "; ".join(out[:4]) or None


def extract_region(text: str) -> str | None:
    m = _REGION_RE.search(text)
    return m.group(1).capitalize() if m else None


def _position_of(line: str) -> tuple[str | None, str | None, str | None]:
    """→ (семейство, типоразмер, марка ряда) для строки объявления."""
    sk = series_key(line)
    family = classify_family(line)
    size_key = extract_size_key(line)
    return family, size_key, (sk[0] if sk else None)


def parse_message(text: str) -> list[ParsedListing]:
    """Разобрать сообщение в одну или несколько позиций."""
    direction = detect_direction(text)
    contacts = extract_contacts(text)
    region = extract_region(text)
    msg_price, msg_unit = extract_price(text)

    out: list[ParsedListing] = []
    for i, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip(" -—•*·\t")
        if len(line) < 4:
            continue
        family, size_key, mark = _position_of(line)
        if not (family or mark):
            continue
        price, unit = extract_price(line)
        out.append(ParsedListing(
            line_no=i, text=line[:500],
            direction=detect_direction(line) or direction,
            price=price, price_unit=unit,
            family=family, size_key=size_key, series_mark=mark,
            contacts=contacts, region=region))

    if not out:
        # позиций не распознали — сообщение всё равно кладём в ленту целиком,
        # менеджер увидит его глазами
        family, size_key, mark = _position_of(text)
        out.append(ParsedListing(
            line_no=0, text=text[:2000], direction=direction,
            price=msg_price, price_unit=msg_unit, family=family,
            size_key=size_key, series_mark=mark, contacts=contacts,
            region=region))
    elif len(out) == 1 and out[0].price is None:
        # единственная позиция в сообщении — цена из шапки относится к ней
        out[0].price, out[0].price_unit = msg_price, msg_unit
    return out
