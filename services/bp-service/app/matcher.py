"""Автосопоставление номенклатуры продавца (из БП) с номенклатурой 1С.

Порядок подбора:
1. История подтверждённых сопоставлений (nomen_match_history) — то самое
   «предложено на основе ИИ»: чем чаще мастера подтверждали пару, тем выше.
2. Точное совпадение нормализованных имён с ref_nomenclature_1c.
3. Нечёткое совпадение: кандидаты по подстроке, оценка по пересечению
   токенов и посимвольной близости; предпочтение позициям в тоннах.

Финальное слово всегда за человеком: подбор заполняет позиции со статусом
«авто»/«ИИ (история)», мастер подтверждает или правит вручную.
"""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher

# Слова, не несущие смысла при сравнении номенклатур.
_STOP = {"б/у", "бу", "т", "тн", "шт", "м", "мм", "кг", "общ", "назнач",
         "назначения", "общего", "категория", "вид", "и"}


# Сокращения продавцов → полные слова справочника 1С («нерж. сталей» →
# «нержавеющих сталей», «черн. мет.» → «черных металлов»).
_ABBR = [
    (re.compile(r"\bнерж\.?\s*(стал\w*)"), r"нержавеющих сталей"),
    (re.compile(r"\bнерж\.(?=\s|$)"), "нержавеющ"),
    (re.compile(r"\bчерн\.\s*метал\w*"), "черных металлов"),
    (re.compile(r"\bцвет\.\s*метал\w*"), "цветных металлов"),
    (re.compile(r"\bотх\b\.?"), "отходы"),
    (re.compile(r"\bзагр\.\s*н/п"), "загрязненные нефтепродуктами"),
    (re.compile(r"\bалюм\.(?=\s|$)"), "алюминия"),
]


def normalize(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    for rx, rep in _ABBR:
        s = rx.sub(rep, s)
    s = s.replace("*", "х").replace("x", "х")          # 73*5,5 / 73x5.5 → 73х5,5
    s = s.replace(",", ".")
    s = re.sub(r"[_()\[\]«»\"']", " ", s)
    s = re.sub(r"(?<=\d)(?=[а-я])", " ", s)           # 73мм → 73 мм
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[\s\-/]+", normalize(s))
            if t and t not in _STOP and len(t) > 1}


# Металл в названии решает: «Лом алюминия ГОСТ Р 54564» не должен сопоставляться
# с «Лом меди A-I-3 ГОСТ Р 54564» только потому, что совпали «лом», «гост» и
# номер стандарта (КП 1956, 16.09.2026). Группы взаимоисключающие.
_METALS = {
    "алюмин": "al", "алюм": "al", "медь": "cu", "меди": "cu", "медн": "cu",
    "латун": "brass", "бронз": "bronze", "свинц": "pb", "свинец": "pb",
    "нерж": "ss", "нержав": "ss", "чугун": "cast", "титан": "ti", "цинк": "zn",
    "никел": "ni", "магни": "mg",
}


# После normalize между цифрой и буквой стоит пробел («20 а»), а «10%» и
# «№ 1» категорией не являются.
_CATEGORY = re.compile(r"(?<![\wа-я%.])(\d{1,2}) ?([абв])(?![\wа-я/])|(?<![\wа-я])б ?(\d{2})(?![\wа-я])", re.IGNORECASE)


def category_code(name: str) -> str | None:
    """Категория лома в имени: «20А», «5А», «Б26» — самый точный признак."""
    m = _CATEGORY.search(normalize(name))
    if not m:
        return None
    return (m.group(1) + m.group(2)).lower() if m.group(1) else "б" + m.group(3)


def metal_of(name: str) -> str | None:
    n = normalize(name)
    for word, code in _METALS.items():
        if word in n:
            return code
    return None


def _similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    return 0.6 * overlap + 0.4 * ratio


def suggest(conn: sqlite3.Connection, seller_name: str,
            limit: int = 3, seller_code: str | None = None) -> list[dict]:
    """Список предложений: [{name, guid, score, source}], лучшее — первым."""
    norm = normalize(seller_name)
    out: list[dict] = []

    # 0. Код продавца (КССС / номенклатурный номер) — самый надёжный ключ:
    # наименование в перечнях пишут по-разному («Труба НКТ 73х5.5 б/у» и
    # «_Труба НКТ 73мм б/у»), а код у позиции один и тот же из лота в лот.
    code = (seller_code or "").strip()
    if code:
        for h in conn.execute(
                "SELECT nomen_1c, nomen_1c_guid, uses FROM nomen_match_history "
                "WHERE seller_code = ? ORDER BY uses DESC, updated_at DESC "
                "LIMIT ?", (code, limit)).fetchall():
            out.append({"name": h["nomen_1c"], "guid": h["nomen_1c_guid"],
                        "score": 1.0,
                        "source": f"по коду продавца {code} "
                                  f"(подтверждений: {h['uses']})"})
        if out:
            return out[:limit]

    # 1. История подтверждений («ИИ»)
    for h in conn.execute(
            "SELECT nomen_1c, nomen_1c_guid, uses FROM nomen_match_history "
            "WHERE seller_norm = ? ORDER BY uses DESC, updated_at DESC LIMIT ?",
            (norm, limit)).fetchall():
        out.append({"name": h["nomen_1c"], "guid": h["nomen_1c_guid"],
                    "score": 1.0,
                    "source": f"ИИ (история, подтверждений: {h['uses']})"})
    if out:
        return out[:limit]

    # 2. Точное совпадение нормализованных имён
    exact = conn.execute("SELECT name, guid FROM ref_nomenclature_1c WHERE norm = ? "
                         "ORDER BY (unit = 'т') DESC LIMIT ?", (norm, limit)).fetchall()
    for e in exact:
        out.append({"name": e["name"], "guid": e["guid"], "score": 0.99,
                    "source": "авто (точное совпадение)"})
    if out:
        return out[:limit]

    # 3. Нечёткий подбор: кандидаты по самым длинным токенам
    toks = sorted(tokens(seller_name), key=len, reverse=True)[:3]
    # Категория лома («20 а») в кандидаты — токен короткий, по длине не попадает.
    cat = category_code(seller_name)
    if cat:
        toks.append(cat[:-1] + " " + cat[-1] if cat[0].isdigit() else cat)
    if not toks:
        return []
    seen: dict[str, sqlite3.Row] = {}
    for t in toks:
        for c in conn.execute(
                "SELECT name, guid, unit FROM ref_nomenclature_1c "
                "WHERE norm LIKE ? LIMIT 400", (f"%{t}%",)).fetchall():
            seen.setdefault(c["guid"] or c["name"], c)
    scored = []
    want_metal = metal_of(seller_name)
    # Из двух похожих имён справочника предпочитаем то, которым реально
    # торгуют: «Лом алюминия» (409 т продаж) против «Лом алюминия ГОСТ 1639-93»
    # (0 т) — у первого есть факт цены, у второго нет.
    try:
        traded = {r["nomen_norm"]: float(r["q"] or 0) for r in conn.execute(
            "SELECT nomen_norm, SUM(total_qty_t) AS q FROM stat_sale_price_nomen GROUP BY nomen_norm")}
    except sqlite3.Error:
        traded = {}
    for c in seen.values():
        have_metal = metal_of(c["name"])
        if want_metal and have_metal and want_metal != have_metal:
            continue                              # другой металл — не кандидат
        if want_metal and not have_metal and want_metal in ("al", "cu", "ss"):
            continue                              # цветмет/нерж без металла в имени — не то
            # (чугун в 1С часто идёт по категории «20А» без слова — не режем)
        score = _similarity(seller_name, c["name"])
        if c["unit"] == "т":                      # металлолом ведём в тоннах
            score += 0.05
        q = traded.get(normalize(c["name"]), 0.0)
        if q >= 5:                                 # есть факт продаж — надёжнее
            score += 0.08 if q >= 50 else 0.04
        # Категория лома («20А», «Б26») совпала — это точнее любого слова;
        # не совпала при заданной у продавца — кандидат не тот.
        want_cat, have_cat = category_code(seller_name), category_code(c["name"])
        if want_cat and have_cat:
            if want_cat == have_cat:
                score += 0.15
            else:
                continue
        if score >= 0.45:
            scored.append({"name": c["name"], "guid": c["guid"], "_raw": score,
                           "score": round(min(score, 0.98), 2),
                           "source": "авто (похожее наименование)"})
    scored.sort(key=lambda x: -x["_raw"])
    return [{k: v for k, v in s.items() if k != "_raw"} for s in scored[:limit]]


def confirm(conn: sqlite3.Connection, seller_name: str, nomen_1c: str,
            guid: str | None, seller_code: str | None = None) -> None:
    """Фиксация подтверждённой пары в истории (обучение «ИИ»).

    Код продавца сохраняется вместе с парой: в следующем лоте позиция с тем
    же кодом сопоставится сразу, даже если наименование записали иначе."""
    conn.execute(
        "INSERT INTO nomen_match_history (seller_norm, seller_name, nomen_1c, "
        "nomen_1c_guid, seller_code) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(seller_norm, nomen_1c) DO UPDATE SET "
        "uses = uses + 1, updated_at = datetime('now'), "
        "seller_code = COALESCE(excluded.seller_code, seller_code)",
        (normalize(seller_name), seller_name, nomen_1c, guid,
         (seller_code or "").strip() or None))
