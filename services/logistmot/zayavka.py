"""Заявка на перевозку: формат, парсинг шаблона логиста, отправка.

Два способа отправить заявку:

1) Логист пишет боту в личку (основной способ, см. bot.py):
       заявка
       маршрут: Покачи → Полевской
       даты: 23.07–24.07
       груз: НКТ 73
       ставка: 2500 без НДС / 3050 с НДС
       тс: тент/открытые, верхняя загрузка
       условия: подача данных за день до 14:00
       контакты: Светлана 8 912 345-67-89
       важно: Подняли ставку!
   Обязательная строка — только «маршрут». Бот покажет превью и ждёт «отправить».

2) Из терминала:
       ./.venv/bin/python zayavka.py --route "Покачи → Полевской" --cargo "НКТ 73" ...
       ./.venv/bin/python zayavka.py --list-chats   # узнать chat_id группы
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from maxapi import Bot
from maxapi.enums.parse_mode import ParseMode
from maxapi.types import CallbackButton, LinkButton, RequestContactButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import db

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.environ["MAX_BOT_TOKEN"]
DELAY = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.1"))
DEFAULT_CHAT_ID = os.getenv("MAX_CHAT_ID")
REPLY_WINDOW_SECONDS = int(float(os.getenv("REPLY_WINDOW_HOURS", "48")) * 3600)

BOT_USERNAME = "@id590773639042_bot"
BOT_LINK = "https://max.ru/id590773639042_bot"
BRAND = "ЛогистАрмада/МОТ"

# --- формат ---------------------------------------------------------------

FIELDS = ("important", "route", "dates", "cargo", "rate", "ts",
          "load", "unload", "note", "contacts")

ALIASES = {
    "важно": "important", "срочно": "important",
    "маршрут": "route", "направление": "route",
    "даты": "dates", "дата": "dates", "когда": "dates",
    "груз": "cargo",
    "ставка": "rate", "цена": "rate",
    "тс": "ts", "машины": "ts", "транспорт": "ts",
    "загрузка": "load", "погрузка": "load",
    "выгрузка": "unload",
    "условия": "note", "примечание": "note",
    "контакты": "contacts", "тел": "contacts", "телефоны": "contacts",
}


# --- типы заявок: разное оформление в чате ------------------------------
# Баннер: эмодзи + жирный заголовок с выгодой, тонкая линия, детали.
# Разные типы различаются эмодзи, текстом и цветом кнопки под сообщением.

RULE = "━━━━━━━━━━━━━━━━━━━━"

KINDS = {
    "normal": {
        "emoji": None, "title": None,
        "button": "🚛 Готов взять",
        "intent": None,      # обычная кнопка
        "notify": False,     # без звука: рядовая заявка
    },
    "urgent": {
        "emoji": "🚨", "title": "СРОЧНО", "tail": "машины нужны сегодня",
        "button": "🚨 БЕРУ СРОЧНО",
        "intent": "negative",  # красная кнопка
        "notify": True,        # со звуком — это исключение, а не норма
    },
    "new_route": {
        "emoji": "🗺", "title": "НОВОЕ НАПРАВЛЕНИЕ", "tail": "раньше не возили",
        "button": "🚛 Готов взять",
        "intent": "positive",
        "notify": True,
    },
    "rate_up": {
        # не ⬆️: этим значком помечена «Загрузка» в теле заявки, и при
        # пересылке карточки боту баннер попадал в поле загрузки
        "emoji": "↗️", "title": "СТАВКА ВЫШЕ", "tail": None,
        "button": "🚛 Готов взять",
        "intent": "positive",  # зелёная
        "notify": True,
    },
}

# Жирный заголовок: markdown поддерживается MAX. Если разметка вдруг
# не отрисуется, заголовок останется читаемым (заглавные буквы).
USE_MARKDOWN = os.getenv("USE_MARKDOWN", "1") != "0"


def rate_gain(old: int | None, new: int | None) -> str | None:
    """Выгода цифрой: «+300 ₽ (+12%)» — мотивирует сильнее слова «повышена»."""
    if not old or not new or new <= old:
        return None
    diff = new - old
    pct = round(diff * 100 / old)
    return f"+{diff:,} ₽".replace(",", " ") + (f" (+{pct}%)" if pct else "")

URGENT_WORDS = ("сроч", "горит", "горящ", "срочно", "сегодня же", "asap",
                "нужны машины сегодня", "очень нужн")
RATE_UP_WORDS = ("подня", "поднима", "повыси", "повыша", "увеличи", "ставка выше")


def detect_kind(f: dict, explicit: str | None = None) -> tuple[str, str | None]:
    """Определить тип заявки. Возвращает (kind, подпись «было → стало»).

    Явный выбор логиста в панели важнее автоопределения.
    """
    if explicit in KINDS and explicit != "auto":
        kind = explicit
    else:
        kind = "normal"
        hay = " ".join(str(f.get(k) or "") for k in
                       ("important", "note", "freeform", "dates", "ts")).lower()
        if any(w in hay for w in URGENT_WORDS):
            kind = "urgent"
        elif any(w in hay for w in RATE_UP_WORDS):
            kind = "rate_up"

    # сравнение со ставкой по этому же маршруту: и подпись, и повод для rate_up
    note = None
    prev = db.route_history(f.get("route"))
    new_rate = db.rate_number(f.get("rate"))
    if not prev and f.get("route") and kind == "normal":
        kind = "new_route"
    elif prev and new_rate:
        old_rate = next((db.rate_number(p["rate"]) for p in prev
                         if db.rate_number(p["rate"])), None)
        gain = rate_gain(old_rate, new_rate)
        if gain:
            note = f"было {old_rate:,} → стало {new_rate:,} ₽".replace(",", " ")
            note = f"{note}|{gain}"      # хвост после | уходит в заголовок
            if kind == "normal":
                kind = "rate_up"
        elif kind == "rate_up" and old_rate and new_rate <= old_rate:
            kind = "normal"       # «подняли» на словах, а по цифрам нет
    return kind, note


FOOTER = ["", "💬 Готовы взять рейс? Ответьте одним сообщением по образцу:",
          "«2 машины, Иванов, 8 912 345-67-89»",
          "(сколько ТС · как вас зовут · телефон — логист свяжется для уточнения)",
          f"Надёжнее — боту в личку: {BOT_USERNAME} (нажмите «Начать»).",
          "", f"— {BRAND}"]


def kind_header(kind: str, note: str | None = None,
                gain: str | None = None) -> list[str]:
    """Баннер: эмодзи + жирный заголовок (+ выгода), линия, детали.

    ⬆️ **СТАВКА ВЫШЕ** · +300 ₽ (+12%)
    ━━━━━━━━━━━━━━━━━━━━
    было 2 500 → стало 2 800 ₽
    """
    cfg = KINDS.get(kind or "normal", KINDS["normal"])
    if not cfg["emoji"]:
        return []
    title = f"**{cfg['title']}**" if USE_MARKDOWN else cfg["title"]
    head = f"{cfg['emoji']} {title}"
    tail = gain or cfg.get("tail")
    if tail:
        head += f" · {tail}"
    lines = [head, RULE]
    if note:
        lines.append(note)
    lines.append("")
    return lines


def split_note(note: str | None) -> tuple[str | None, str | None]:
    """«было … → стало …|+300 ₽ (+12%)» → (детали, выгода для заголовка)."""
    if not note:
        return None, None
    if "|" in note:
        detail, gain = note.split("|", 1)
        return detail.strip() or None, gain.strip() or None
    return note, None


def format_zayavka(f: dict, kind: str = "normal", note: str | None = None) -> str:
    """f: словарь с ключами из FIELDS (все опциональны, кроме route),
    либо {'freeform': текст} — свободная форма."""
    if "freeform" in f:
        return format_freeform(f["freeform"], kind, note)
    detail, gain = split_note(note)
    lines = list(kind_header(kind, detail, gain))
    if f.get("important"):
        lines += [f"‼️ {f['important'].upper()}", ""]
    lines += ["🚛 ЗАЯВКА НА ПЕРЕВОЗКУ", ""]
    lines.append(f"📍 Маршрут: {f['route']}")
    if f.get("dates"):
        lines.append(f"📅 Даты: {f['dates']}")
    if f.get("cargo"):
        lines.append(f"📦 Груз: {f['cargo']}")
    if f.get("rate"):
        lines.append(f"💰 Ставка: {f['rate']}")
    if f.get("ts"):
        lines.append(f"🚚 ТС: {f['ts']}")
    if f.get("load"):
        lines.append(f"⬆️ Загрузка: {f['load']}")
    if f.get("unload"):
        lines.append(f"⬇️ Выгрузка: {f['unload']}")
    if f.get("note"):
        lines += ["", f"⏰ {f['note']}"]
    if f.get("contacts"):
        lines += ["", f"📞 {f['contacts']}"]
    return "\n".join(lines + FOOTER)


def format_freeform(text: str, kind: str = "normal", note: str | None = None) -> str:
    """Свободный текст логиста: шапка + текст как есть + призыв и подпись."""
    detail, gain = split_note(note)
    return "\n".join(kind_header(kind, detail, gain)
                     + ["🚛 ЗАЯВКА НА ПЕРЕВОЗКУ", "", text.strip()] + FOOTER)


import re as _re


def _num_clean(s: str) -> str:
    """«2 800,00» → «2800»; «45 000» → «45000»."""
    s = _re.sub(r"\s", "", s)
    return _re.sub(r"[.,]00$", "", s)


def parse_freeform(text: str) -> dict:
    """Разбор привычного текста логиста в структурные поля.

    Принцип: распознанные куски вырезаются из текста, всё осмысленное,
    что осталось, попадает в «условия» — информация не теряется.
    Не нашли маршрут — вернём {'freeform': текст} (заявка уйдёт как есть).
    """
    f: dict = {}
    work = " " + text.strip() + " "

    def grab(pattern, flags=_re.IGNORECASE):
        """Вырезать все совпадения из work, вернуть список match-объектов."""
        nonlocal work
        found: list = []
        def _cb(m):
            found.append(m)
            return " "
        work = _re.sub(pattern, _cb, work, flags=flags)
        return found

    # --- срочность и декоративный мусор ---
    if _re.search(r"подня\w+\s+ставк|поднимаем\s+ставк", work, _re.I):
        f["important"] = "Подняли ставку!"
    elif _re.search(r"важно", work, _re.I):
        f["important"] = "Важно!"
    work = _re.sub(r"[‼🔥⚠❗️]+", " ", work)
    grab(r"важно[!\s]*")
    grab(r"подня\w+\s+ставк\w+|поднимаем\s+ставк\w+")
    grab(r"\bот\b(?=\s*\d)")  # «ОТ 2500» — служебное слово перед ставкой

    # --- даты (до маршрута, чтобы тире дат не путалось с тире маршрута) ---
    # точка в конце даты — обычное дело в заявках: «10.08. - 14.08.»
    d = r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?"
    m = grab(rf"\b({d})\.?\s*(?:[-–—]+|по)\s*({d})\.?(?!\d)")
    if m:
        f["dates"] = f"{m[0].group(1)}–{m[0].group(2)}"
    else:
        m = grab(rf"\b({d})\.?(?!\d)(?!\s*[-–—])")
        if m:
            f["dates"] = m[0].group(1)
    if grab(r"\bежедневно\b"):
        f["dates"] = " · ".join(x for x in (f.get("dates"), "ежедневно") if x)

    # --- загрузка / выгрузка ---
    if grab(r"(?:за|по)грузка\s*/\s*выгрузка\s+верхн\w+"):
        f["load"] = f["unload"] = "верхняя"
    m = grab(r"(?:утром\s+погрузка|погрузка\s+утром)")
    if m:
        f["load"] = " · ".join(x for x in (f.get("load"), "утром") if x)
    if grab(r"выгрузка\s+круглосуточно(?:\s+ежедневно)?"):
        f["unload"] = " · ".join(x for x in (f.get("unload"), "круглосуточно, ежедневно") if x)
    extra_load = []
    if grab(r"подача\s+данных\s+на\s+(?:за|по)грузку\s+за\s+день\s+до\s+[\d.:]+"):
        extra_load.append("подача данных за день до 14:00")
    if grab(r"в\s+выходные(?:\s+и\s+праздничные\s+дни)?\s+(?:по|за)грузки\s+нет"):
        extra_load.append("в выходные и праздники погрузки нет")
    if extra_load:
        f["load"] = " · ".join(x for x in ([f.get("load")] + extra_load) if x)

    # --- ставки ---
    num = r"\d[\d\s]{0,7}(?:[.,]\d{2})?"
    per_tonne = bool(_re.search(r"/\s*тонн|за\s+тонну|цена\s+за\s+тонну|ндс\s*/\s*тонн", text, _re.I))
    grab(r"цена\s+за\s+тонну")
    rate_parts = []
    m = grab(rf"({num})\s*(?:руб\S*|₽)?\s*без\s+ндс(?:\s*/\s*тонн\S*)?")
    if m:
        rate_parts.append(f"{_num_clean(m[0].group(1))} ₽ без НДС")
    m = grab(rf"(?:без\s+)?({num})\s*(?:руб\S*|₽)?\s*с\s+ндс")
    if m:
        rate_parts.append(f"{_num_clean(m[0].group(1))} ₽ с НДС")
    if rate_parts:
        f["rate"] = " · ".join(rate_parts) + (" (за тонну)" if per_tonne else "")
        grab(r"\bставк\w+\b")  # само слово «ставка» больше не нужно

    # --- контакты: «к/л Имя», телефоны с именами ---
    contacts = []
    m = grab(r"к/л\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)")
    if m:
        contacts.append(m[0].group(1))
    phone = r"(?:\+7|8)?[\s(-]*9\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"
    grab(r"предложени\w*\s+прошу\s+по\s+тел[.:]*|по\s+тел[.:]+")
    for m0 in grab(rf"((?:[А-ЯЁ][а-яё]+\s+){{0,2}})({phone})((?:\s*/\s*{phone})*)"
                   rf"(\s+[А-ЯЁ][а-яё]+)?"):
        nums = [m0.group(2)] + _re.findall(phone, m0.group(3) or "")
        nums = ["+7" + _re.sub(r"\D", "", n)[-10:] for n in nums]
        before = (m0.group(1) or "").strip()
        after = (m0.group(4) or "").strip()
        name = before or after
        contacts.append((f"{name} " if name else "") + " / ".join(nums))
    if contacts:
        f["contacts"] = " · ".join(contacts)

    # --- груз и тоннаж ---
    cargo, tons = [], []
    m = grab(r"(?:груз[а-я]*\s+)?(?:труба\s+)?(нкт\s*-?\s*\d+)")
    if m:
        cargo.append("НКТ " + _re.sub(r"\D", "", m[0].group(1)))
    m = grab(r"лом\s+[а-яё]+(?:\s+цветмет\w*|\s+чермет\w*)?")
    if m:
        cargo.append(m[0].group(0).strip())
    m = grab(r"кабель(?:\s+в\s+катушках)?")
    if m:
        cargo.append(m[0].group(0).strip())
    # прокат и трубное с типоразмером: «штанга 22 мм», «труба 325, 273».
    # список закрытый, без \w*-хвостов: «труб\w*» съедало бы город Трубчевск.
    # Размер не путаем с тоннажем — в «труба 1020, 10 т» это груз и вес.
    goods = (r"штанга|штанги|труба|трубы|трубу|лист|листы|балка|балки|"
             r"швеллер|швеллеры|уголок|уголки|арматура|катанка|проволока|"
             r"рельс|рельсы|электродвигатель|электродвигатели")
    size1 = r"\d+(?:[.,]\d+)?\b(?:\s*мм)?(?!\s*(?:т\b|тонн|тн\b))"
    for m0 in grab(rf"\b({goods})\b((?:\s*{size1})(?:\s*,\s*{size1})*)?"):
        cargo.append(_re.sub(r"\s+", " ",
                             (m0.group(1) + (m0.group(2) or "")).strip()))
    for m0 in grab(r"(до\s+)?(\d+)\s*(?:тонн\w*|тн)\b"):
        tons.append(("до " if m0.group(1) else "") + m0.group(2) + " т")
    for m0 in grab(r"\b(\d+)\s*т\.?(?![\wа-яёА-ЯЁ])"):
        tons.append(m0.group(1) + " т")
    if cargo or tons:
        f["cargo"] = ", ".join(cargo + tons)

    # --- ТС ---
    ts = []
    if grab(r"количество\s+тс\s+без\s+ограничений"):
        ts.append("количество без ограничений")
    m = grab(r"(\d+)\s*(?:тс|ТС)\b(?:\s*машин\w*)?")
    if m:
        ts.append(m[0].group(1) + " ТС")
    if grab(r"тент\s*/\s*откр\w*"):
        ts.append("тент/открытые")
    elif grab(r"нужен\s+тент|\bтент\b"):
        ts.append("тент")
    if grab(r"\bборт\b"):
        ts.append("борт")
    if grab(r"площадк\w*"):
        ts.append("площадка")
    # «3 пары коников обязательно» — требование к ТС, а не примечание
    m = grab(r"(\d+)\s*пар\w*\s+коник\w*(?:\s+обязательн\w*)?")
    if m:
        ts.append(f"{m[0].group(1)} пары коников обязательно")
    elif grab(r"коник\w+(?:\s+обязательн\w*)?"):
        ts.append("коники обязательно")
    grab(r"требуются\s+тс|требуется\s+тс|\bтс\b\s*:?|\bтс\b(?=\s+[А-ЯЁ])")
    if ts:
        f["ts"] = ", ".join(ts)

    # --- маршрут (после вычистки дат и ставок) ---
    # адрес в скобках («(629830, Ямало-Ненецкий АО, ул. …)») содержит дефисы
    # и запятые — при поиске маршрута он только мешает, убираем его из работы
    addr_in_parens = _re.findall(r"\([^)]{10,}\)", work)
    for chunk in addr_in_parens:
        work = work.replace(chunk, " ")

    # «ЭПУ» в «Покачи ЭПУ» — часть города, а «ТС»/«НКТ» с новой строки — уже нет
    STOP = r"(?!ТС|НКТ|ГК|ООО|ИП)"
    city = rf"[А-ЯЁ][А-Яа-яЁё-]+(?:[ \t]+{STOP}[А-ЯЁ]{{2,}})?"
    addr = r"(?:\s*,\s*(?:г\.?\s*)?(?:ул|пр|пер|туп|ш)\.?\s+[^,\n]+)?(?:\s*,\s*д\.?\s*[\d/]+(?:\s*,?\s*к[./]?\s*\d+)?)?"
    m = grab(rf"({city})[ \t]*[-–—]+[ \t]*(?:г\.?[ \t]+)?({city}{addr})")
    if m:
        f["route"] = f"{m[0].group(1).strip()} → {m[0].group(2).strip()}"
        # адрес из скобок не теряем — он нужен водителю (допишем в конце)
    else:
        return {"freeform": text}  # маршрут не нашли — шлём как есть

    # --- всё осмысленное, что осталось → условия ---
    # режем на фразы и сохраняем целиком те, где есть слово: иначе теряются
    # числа рядом со словами («3 пары коников обязательно»)
    phrases = []
    for chunk in _re.split(r"[.;!?\n]+|\s{3,}", work):
        chunk = _re.sub(r"\s+", " ", chunk)
        chunk = _re.sub(r"[,:/]\s*(?=[,:/])", "", chunk)       # «: , ,» от вырезанных слов
        chunk = _re.sub(r"^\s*[,:/]+|[,:/]+\s*$", "", chunk).strip(" ,:-–—/")
        # служебные хвосты вроде «предложения по телефону» уже отражены в контактах
        chunk = _re.sub(r"предложени\w*(\s+прошу)?(\s+по)?(\s+тел\w*)?", "", chunk,
                        flags=_re.IGNORECASE).strip(" ,:-–—/")
        if not chunk or not _re.search(r"[а-яА-ЯёЁ]{3,}", chunk) or len(chunk) < 4:
            continue
        phrases.append(chunk)
    if phrases:
        f["note"] = ". ".join(phrases)
    if addr_in_parens:                       # адрес из скобок — в конец условий
        addr = addr_in_parens[0].strip("() ").strip()
        if addr and addr not in (f.get("note") or ""):
            f["note"] = ((f["note"] + ". ") if f.get("note") else "") + addr
    return f


def extract_meta(text: str) -> dict:
    """Мета для панели из свободного текста (когда parse_freeform не справился)."""
    parsed = parse_freeform(text)
    return {} if "freeform" in parsed else parsed


# --- автоисправление опечаток ---------------------------------------------

import difflib as _difflib

# Эталонные написания городов. Пополняется из истории заявок: новый город,
# не похожий ни на один эталон, запоминается как есть.
SEED_CITIES = (
    "Когалым", "Полевской", "Покачи", "Советский", "Тюмень", "Ижевск",
    "Пермь", "Краснокамск", "Курган", "Нягань", "Сургут", "Екатеринбург",
    "Нижневартовск", "Нефтеюганск", "Лангепас", "Мегион", "Радужный",
    "Урай", "Пыть-Ях", "Ханты-Мансийск", "Челябинск", "Уфа", "Казань",
    "Ноябрьск", "Новый Уренгой", "Салехард", "Березники", "Первоуральск",
)

# Частые опечатки в тексте заявок: (шаблон, замена, что показать логисту)
TEXT_FIXES = (
    (r"транформатор", "трансформатор", "транформатор → трансформатор"),
    (r"трансфарматор", "трансформатор", "трансфарматор → трансформатор"),
    (r"\bНДМ\b", "НДС", "НДМ → НДС"),
    (r"\bНДТ\b", "НДС", "НДТ → НДС"),
    (r"(\d)\s*тон\b", r"\1 тонн", "тон → тонн"),
    (r"Нефтянников", "Нефтяников", "Нефтянников → Нефтяников"),
    (r"\bкатушкахх\b", "катушках", "катушкахх → катушках"),
)

_CITY_CACHE: dict = {"ts": 0, "list": []}


def known_cities() -> list[str]:
    """Эталоны + города из истории (кешируется на 5 минут)."""
    now = int(_time.time())
    if _CITY_CACHE["list"] and now - _CITY_CACHE["ts"] < 300:
        return _CITY_CACHE["list"]
    cities = list(SEED_CITIES)
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT route FROM broadcasts WHERE route IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        for part in (r["route"] or "").split("→"):
            name = part.strip().split(",")[0].strip()
            name = _re.sub(r"\s*\(.*?\)", "", name).strip()
            if len(name) < 4:
                continue
            head = name.split()[0]
            # запоминаем, только если не похоже на уже известный (иначе
            # опечатка из истории закрепится как «правильная»)
            if not _difflib.get_close_matches(head.lower(),
                                              [c.lower() for c in cities],
                                              n=1, cutoff=0.75):
                cities.append(head)
    _CITY_CACHE.update({"ts": now, "list": cities})
    return cities


def fix_city(name: str) -> tuple[str, str | None]:
    """«Когалы» → «Когалым». Возвращает (имя, что_исправлено|None)."""
    head = name.strip()
    if len(head) < 4:
        return name, None
    cities = known_cities()
    lower = {c.lower(): c for c in cities}
    if head.lower() in lower:
        canon = lower[head.lower()]
        return (canon, f"{head} → {canon}") if canon != head else (name, None)
    match = _difflib.get_close_matches(head.lower(), list(lower), n=1, cutoff=0.8)
    if match:
        canon = lower[match[0]]
        return canon, f"{head} → {canon}"
    return name, None


def fix_route(route: str) -> tuple[str, list[str]]:
    """Исправить города в маршруте, сохранив адреса и уточнения."""
    fixes: list[str] = []
    parts = _re.split(r"\s*(?:→|->|—|–|-)\s*", route.strip(), maxsplit=1)
    out = []
    for part in parts:
        chunks = part.split(",")
        words = chunks[0].strip().split()
        if words:
            fixed, note = fix_city(words[0])
            if note:
                fixes.append(note)
                words[0] = fixed
            chunks[0] = " ".join(words)
        out.append(", ".join(c.strip() for c in chunks if c.strip()))
    return " → ".join(x for x in out if x), fixes


def fix_typos(f: dict) -> tuple[dict, list[str]]:
    """Причесать поля заявки: города в маршруте + частые опечатки в тексте."""
    fixes: list[str] = []
    out = dict(f)
    if out.get("route"):
        route, rf = fix_route(out["route"])
        out["route"] = route
        fixes += rf
    for key in ("route", "cargo", "dates", "rate", "ts", "note", "load",
                "unload", "contacts", "important", "freeform"):
        val = out.get(key)
        if not val:
            continue
        new = val
        for pattern, repl, label in TEXT_FIXES:
            fixed = _re.sub(pattern, repl, new, flags=_re.IGNORECASE)
            if fixed != new:
                new = fixed
                if label not in fixes:
                    fixes.append(label)
        if new != val:
            out[key] = new
    return out, fixes


CARD_MARKERS = {
    "📍": "route", "📅": "dates", "📦": "cargo", "💰": "rate",
    "🚚": "ts", "⬆": "load", "⬇": "unload", "📞": "contacts",
}


def parse_card(text: str) -> dict | None:
    """Разобрать скопированную/пересланную карточку бота обратно в поля.

    Позволяет логисту переслать боту старую заявку — и получить новую
    (обычно поменяв только даты), не набирая текст заново.
    """
    if "ЗАЯВКА НА ПЕРЕВОЗКУ" not in text:
        return None
    banner_titles = tuple(c["title"] for c in KINDS.values() if c.get("title"))
    f: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # строки баннера («СТАВКА ВЫШЕ», «СРОЧНО», линия) — это оформление
        # прошлой заявки, а не данные: иначе баннер попадает в поля
        clean = line.replace("*", "")
        if clean.strip("━ ") == "" or any(t in clean.upper() for t in banner_titles):
            continue
        matched = False
        for emoji, key in CARD_MARKERS.items():
            if line.startswith(emoji):
                val = line.split(":", 1)[1] if ":" in line else line[len(emoji):]
                val = val.strip().replace("*", "")
                # «Ставка: Ставка 4000…» → «4000…»: логист иногда пишет
                # название поля внутри значения, в карточке оно дублируется
                val = _re.sub(r"^(ставка|груз|маршрут|даты|тс|загрузка|выгрузка|"
                              r"контакты)[\s:—-]+", "", val, flags=_re.IGNORECASE)
                if val and key not in f:
                    f[key] = val
                matched = True
                break
        if matched:
            continue
        if line.startswith("‼") and "ЗАЯВКА" not in line:
            imp = line.strip("‼️ ").strip()
            if imp and "important" not in f:
                f["important"] = imp.capitalize()
    return f if f.get("route") else None


CARD_LABELS = (("route", "📍 Маршрут:"), ("dates", "📅 Даты:"),
               ("cargo", "📦 Груз:"), ("rate", "💰 Ставка:"))


def retext_card(text: str, **fields) -> str:
    """Обновить поля в уже отправленной карточке, не трогая остальное.

    Правка с панели меняет только маршрут/даты/груз/ставку. Пересобирать
    карточку целиком нельзя: в ней есть ТС, загрузка, условия и контакты,
    которых нет в колонках базы, а у баннера — подпись «было → стало».
    Поэтому строку поля заменяем на месте: значение — новое, если задано;
    пустое — строка убирается; появилось впервые — вставляется по порядку.
    """
    lines = text.split("\n")
    order = [k for k, _ in CARD_LABELS]
    for key, label in CARD_LABELS:
        if key not in fields:
            continue
        val = (fields[key] or "").strip()
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(label)), None)
        if idx is not None:
            if val:
                lines[idx] = f"{label} {val}"
            else:
                del lines[idx]
        elif val:
            # вставляем после последнего присутствующего поля выше по списку
            before = order[:order.index(key)]
            prev = max((i for i, ln in enumerate(lines)
                        if any(ln.startswith(l) for k, l in CARD_LABELS if k in before)),
                       default=None)
            if prev is None:
                prev = next((i for i, ln in enumerate(lines)
                             if "ЗАЯВКА НА ПЕРЕВОЗКУ" in ln), -1) + 1
            lines.insert(prev + 1, f"{label} {val}")
    return "\n".join(lines)


def parse_zayavka_message(text: str) -> dict | None:
    """Разбор сообщения логиста. Возвращает dict или None, если это не заявка.

    Первая строка должна начинаться со слова «заявка». Дальше:
      • строки «ключ: значение» (см. ALIASES) → структурная заявка;
      • любой другой текст → свободная форма ({'freeform': текст}).
    """
    raw = (text or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines or not lines[0].lower().startswith("заявка"):
        return None
    body = raw.split("\n", 1)[1].strip() if "\n" in raw else ""
    if not body:
        return None  # одно слово «заявка» без текста → показать помощь
    f: dict = {}
    for ln in lines[1:]:
        if ":" not in ln:
            continue
        key, val = ln.split(":", 1)
        field = ALIASES.get(key.strip().lower())
        if field and val.strip():
            f[field] = val.strip()
    if f.get("route"):
        return f          # структурная форма (ключ: значение)
    return parse_freeform(body)  # свободная форма → авторазбор на поля


HELP_TEXT = (
    "Заявки отправляются только через бота (в чат ничего не постите вручную — "
    "такие сообщения не учитываются). Три способа:\n"
    "1. Напишите просто «заявка» — бот задаст 6 коротких вопросов "
    "(маршрут, даты, груз, ставка, ТС, контакты) и соберёт заявку сам.\n"
    "2. «заявка» + ваш текст → бот разберёт и покажет превью → «отправить».\n"
    "3. «заявка!» + текст → отправка сразу, без превью.\n\n"
    "Бот сам разложит текст по полям (маршрут, даты, груз, ставка, ТС, "
    "загрузка/выгрузка, контакты) и уберёт лишнее.\n\n"
    "заявка\n"
    "23.07-24.07 требуются тс Покачи - Полевской нкт73, загрузка/выгрузка верхняя, "
    "2500 руб. без НДС/тонну, 3050 с НДС. Тел. 8 912 345-67-89 Светлана\n\n"
    "Бот покажет, как будет выглядеть заявка. Проверьте поля и напишите "
    "«отправить» (или «отмена»).\n\n"
    "Чтобы бот понял всё точно:\n"
    "• один рейс — одна заявка (не два маршрута в одном сообщении);\n"
    "• маршрут через тире: Когалым - Полевской;\n"
    "• даты: 23.07 или 23.07-25.07;\n"
    "• ставка со словами «без НДС» / «с НДС».\n\n"
    "Можно и по шаблону «ключ: значение» (маршрут:, даты:, груз:, ставка:, тс:, "
    "загрузка:, выгрузка:, контакты:, важно:) — как удобнее.\n\n"
    "Управление: «заявки» — список активных; «закрыть 9» — закрыть заявку и "
    "убрать из чата; «повторить 9 30.07-31.07» — отправить заявку заново с "
    "новыми датами; «дайджест» — запустить утренний цикл вручную.\n"
    "Каждый день в 10:00 бот сам: убирает из чата неактуальные заявки, постит "
    "дайджест актуальных рейсов с кнопками и напоминает об истёкших без откликов."
)


# --- отправка -------------------------------------------------------------

async def dispatch_zayavka(bot: Bot, f: dict, to_chat: bool = True,
                           chat_already: bool = False, to_dm: bool = True,
                           author_id: int | None = None,
                           author_name: str | None = None,
                           kind: str | None = None) -> str:
    """Разослать заявку: групповой чат (если задан MAX_CHAT_ID) + личка.

    to_chat=False — не публиковать в чат (логист уже сам запостил её туда);
    to_dm=False  — не рассылать в личку (только регистрация для учёта откликов).
    Возвращает текстовую сводку для логиста.
    """
    db.init_db()
    chat_id = int(DEFAULT_CHAT_ID) if DEFAULT_CHAT_ID else None

    # для свободной формы поля для панели достаём из текста, как получится
    meta = extract_meta(f["freeform"]) if "freeform" in f else f

    # тип заявки: явный выбор логиста или автоопределение по тексту и ставке
    kind_key, rate_note = detect_kind({**meta, **f}, kind)
    cfg = KINDS[kind_key]
    text = format_zayavka(f, kind_key, rate_note)
    pmode = ParseMode.MARKDOWN if (USE_MARKDOWN and cfg["emoji"]) else None

    # авторство: аккаунт MAX важнее, но если его нет (заявка с панели под
    # общим логином) — определяем логиста по имени в контактах заявки
    if author_id is None:
        env_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
        accounts = db.logist_accounts(env_ids)
        hint = " ".join(str(f.get(k) or "") for k in ("contacts", "freeform", "note"))
        found = db.match_logist(hint, accounts)
        if found:
            author_id, author_name = found["user_id"], found["name"]
    bid = db.create_broadcast(
        text, route=meta.get("route"), dates=meta.get("dates"),
        cargo=meta.get("cargo"), rate=meta.get("rate"),
        chat_posted=chat_already or bool(chat_id and to_chat),
        author_id=author_id, author_name=author_name, kind=kind_key,
    )

    # кнопка отклика: цвет и текст зависят от типа (горящая — красная)
    btn = CallbackButton(text=cfg["button"], payload=f"take:{bid}")
    if cfg["intent"]:
        btn.intent = cfg["intent"]
    kb = InlineKeyboardBuilder().add(btn).as_markup()

    parts = []
    if to_chat and chat_id:
        try:
            # В канал нельзя отправлять «тихо»: MAX отвечает channel-notify.
            # Поэтому в чат уходит без управления звуком, в личку — по типу.
            try:
                sent = await bot.send_message(chat_id=chat_id, text=text,
                                              attachments=[kb], parse_mode=pmode)
            except Exception:  # noqa: BLE001 — вдруг дело в разметке
                sent = await bot.send_message(chat_id=chat_id, text=text,
                                              attachments=[kb])
            mid = getattr(getattr(getattr(sent, "message", None), "body", None), "mid", None)
            if mid:
                with db.connect() as conn:
                    conn.execute("UPDATE broadcasts SET chat_mid = ? WHERE id = ?",
                                 (mid, bid))
            parts.append("в чат: ✓")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"в чат: ✗ ({exc})")
    elif chat_already:
        parts.append("в чате: уже опубликована вами")
    else:
        parts.append("в чат: пропущено")

    if to_dm:
        # логистам заявку в личку не дублируем — они её и так составляли/видят
        logists = ({int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
                   | db.db_admins())
        users = [row for row in db.active_users() if row["user_id"] not in logists]
        ok = 0
        for row in users:
            try:
                try:
                    await bot.send_message(user_id=row["user_id"], text=text,
                                           attachments=[kb], notify=cfg["notify"],
                                           parse_mode=pmode)
                except Exception:  # noqa: BLE001 — без звука/разметки, но доставить
                    await bot.send_message(user_id=row["user_id"], text=text,
                                           attachments=[kb])
                sent = True
            except Exception:  # noqa: BLE001
                sent = False
                db.deactivate_user(row["user_id"])
            db.record_delivery(bid, row["user_id"], sent)
            ok += int(sent)
            await asyncio.sleep(DELAY)
        parts.append(f"в личку: {ok} из {len(users)}")
    try:  # новая заявка сразу появляется в закреплённом дайджесте
        await refresh_digest(bot)
    except Exception:  # noqa: BLE001
        pass
    return f"Заявка #{bid} отправлена. " + " · ".join(parts)


# --- дайджест актуальных рейсов (закреп в чате) ---------------------------

import time as _time


def build_digest():
    """Текст и клавиатура закреплённого дайджеста. (None, None, 0) — пусто."""
    acts = db.active_broadcasts(REPLY_WINDOW_SECONDS, limit=8)
    if not acts:
        return None, None, 0
    lines = [f"🚛 Актуальные рейсы на {_time.strftime('%d.%m')}:", ""]
    kb = InlineKeyboardBuilder()
    for b in acts:
        lines.append(f"📍 #{b['id']} {b['route'] or 'маршрут уточняется'}")
        meta = " · ".join(x for x in (b["dates"], b["cargo"]) if x)
        if meta:
            lines.append(f"🗓 {meta}")
        if b["rate"]:
            lines.append(f"💰 {b['rate']}")
        lines.append("")
        kb.row(CallbackButton(
            text=f"🚛 Готов взять #{b['id']} {b['route'] or ''}"[:48],
            payload=f"take:{b['id']}"))
    kb.row(LinkButton(text="✅ Подписаться на заявки", url=BOT_LINK))
    lines += [
        "Жмите кнопку нужного рейса — или пишите боту в личку.",
        "",
        "⚠️ Важно: чтобы логист смог перезвонить, подпишитесь на бота — "
        "кнопка «Подписаться на заявки» внизу (один раз, 5 секунд). "
        "Без подписки мы не получим ваш контакт.",
    ]
    return "\n".join(lines), kb, len(acts)


async def digest_alive(bot: Bot) -> bool:
    """Жив ли закреплённый дайджест (мог быть удалён вручную)."""
    mid = db.get_kv("digest_mid")
    if not mid:
        return False
    try:
        await bot.get_message(mid)
        return True
    except Exception:  # noqa: BLE001 — 404: сообщение удалили
        return False


async def refresh_digest(bot: Bot, repost: bool = False) -> int:
    """Актуализировать закреплённый дайджест.

    repost=True — удалить и опубликовать заново (утренний цикл, «поднимает»
    сообщение). Иначе — тихо отредактировать закреп на месте: без нового
    сообщения и уведомлений. Пустой список активных — закреп удаляется.
    """
    chat_id = int(DEFAULT_CHAT_ID) if DEFAULT_CHAT_ID else None
    if not chat_id:
        return 0
    text, kb, n = build_digest()
    mid = db.get_kv("digest_mid")
    if n == 0:
        if mid:
            try:
                await bot.delete_message(mid)
            except Exception:  # noqa: BLE001
                pass
            db.set_kv("digest_mid", "")
        return 0
    if not repost and mid:
        try:
            await bot.edit_message(mid, text=text, attachments=[kb.as_markup()])
            return n
        except Exception:  # noqa: BLE001 — закреп удалили руками, публикуем заново
            pass
    if mid:
        try:
            await bot.delete_message(mid)
        except Exception:  # noqa: BLE001
            pass
    sent = await bot.send_message(chat_id=chat_id, text=text,
                                  attachments=[kb.as_markup()])
    new_mid = getattr(getattr(getattr(sent, "message", None), "body", None), "mid", None)
    db.set_kv("digest_mid", new_mid or "")
    if new_mid:
        try:
            await bot.pin_message(chat_id=chat_id, message_id=new_mid, notify=False)
        except Exception:  # noqa: BLE001
            pass
    return n


# --- CLI ------------------------------------------------------------------

async def list_chats():
    bot = Bot(TOKEN)
    chats = await bot.get_chats()
    for c in getattr(chats, "chats", []) or []:
        print(f"chat_id={c.chat_id}  type={getattr(c, 'type', '?')}  "
              f"title={getattr(c, 'title', None)!r}  "
              f"участников={getattr(c, 'participants_count', '?')}")


def parse_args():
    p = argparse.ArgumentParser(description="Заявка на перевозку ЛогистМОТ")
    p.add_argument("--list-chats", action="store_true")
    p.add_argument("--route")
    p.add_argument("--dates")
    p.add_argument("--cargo")
    p.add_argument("--rate", help="«2500 без НДС / 3050 с НДС»")
    p.add_argument("--ts")
    p.add_argument("--note")
    p.add_argument("--contacts")
    p.add_argument("--important")
    a = p.parse_args()
    if not a.list_chats and not a.route:
        p.error("укажите --route (или --list-chats)")
    return a


if __name__ == "__main__":
    args = parse_args()
    if args.list_chats:
        asyncio.run(list_chats())
    else:
        fields = {k: getattr(args, k) for k in FIELDS if getattr(args, k, None)}
        async def _run():
            print(format_zayavka(fields), "\n" + "=" * 40)
            print(await dispatch_zayavka(Bot(TOKEN), fields))
        asyncio.run(_run())
