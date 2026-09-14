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


def normalize(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = s.replace("*", "х").replace("x", "х")          # 73*5,5 / 73x5.5 → 73х5,5
    s = s.replace(",", ".")
    s = re.sub(r"[_()\[\]«»\"']", " ", s)
    s = re.sub(r"(?<=\d)(?=[а-я])", " ", s)           # 73мм → 73 мм
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[\s\-/]+", normalize(s))
            if t and t not in _STOP and len(t) > 1}


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
    if not toks:
        return []
    seen: dict[str, sqlite3.Row] = {}
    for t in toks:
        for c in conn.execute(
                "SELECT name, guid, unit FROM ref_nomenclature_1c "
                "WHERE norm LIKE ? LIMIT 400", (f"%{t}%",)).fetchall():
            seen.setdefault(c["guid"] or c["name"], c)
    scored = []
    for c in seen.values():
        score = _similarity(seller_name, c["name"])
        if c["unit"] == "т":                      # металлолом ведём в тоннах
            score += 0.05
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
