"""Ключевые слова: ловим то, чего не знает классификатор.

Классификатор семейств выучен на справочнике 1С — он узнаёт то, что уже
проходило через компанию. Ключевые слова закрывают остальное: новую марку,
редкую позицию, чужую формулировку «куплю». Без них объявление с незнакомым
словом попадало в ленту как «позиция не распознана» и терялось.

Слово по умолчанию ищется целиком: «НКТ» не должно срабатывать на «нктовый»,
а «ПЭД» — внутри «ПЭДАЛЬ». Фразы из нескольких слов сопоставляются с любым
количеством пробелов между ними.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy.orm import Session

from ..db.models import MonitorKeyword

# Стартовый набор — марки и группы, которыми компания реально торгует.
# Директор правит список в интерфейсе, это только чтобы лента не была пустой.
DEFAULT_KEYWORDS: list[tuple[str, bool, str]] = [
    ("ПЭД", True, "погружные электродвигатели"),
    ("ВДМ", True, "вентильные погружные двигатели"),
    ("ЭЦН", True, "секции насосов"),
    ("НКТ", True, "насосно-компрессорные трубы"),
    ("ТМПН", True, "трансформаторы погружных насосов"),
    ("ТНЖШ", True, "щелочные аккумуляторные батареи"),
    ("гидрозащита", False, ""),
    ("станция управления", False, ""),
    ("КПБК", True, "кабель погружной"),
    ("КПБП", True, "кабель погружной"),
    ("обсадная труба", False, ""),
    ("буровые трубы", False, ""),
    ("лом", True, "любые ломовые объявления"),
]

_cache: dict = {"key": None, "compiled": None}


def seed_defaults(session: Session, user: str = "") -> int:
    """Завести стартовый список, если своего ещё нет."""
    if session.query(MonitorKeyword).count():
        return 0
    for phrase, whole, note in DEFAULT_KEYWORDS:
        session.add(MonitorKeyword(phrase=phrase, whole_word=whole, note=note,
                                   added_by=user))
    session.commit()
    return len(DEFAULT_KEYWORDS)


_LETTER = "0-9A-Za-zА-Яа-яЁё"
_VOWELS = "аеёиоуыэюяaeiouy"


def _stem(word: str) -> str:
    """Отсечь окончание, чтобы ловить падежи: «гидрозащита» → «гидрозащит».

    Русский текст склоняется, а слово в списке пишут в именительном падеже.
    Без этого «гидрозащита» не находилась во фразе «куплю гидрозащиту».
    """
    return word[:-1] if len(word) > 4 and word[-1].lower() in _VOWELS else word


def _pattern(phrase: str, whole_word: bool) -> re.Pattern:
    words = phrase.split()
    if whole_word:
        # Аббревиатуры (НКТ, ПЭД, ВДМ) ищем строго: «ПЭД» не должен
        # срабатывать внутри «ПЭДАЛЬ». \b на стыке кириллицы и латиницы
        # ведёт себя неровно, поэтому границу задаём явно.
        body = r"[\s\-]+".join(re.escape(w) for w in words)
        body = f"(?<![{_LETTER}])" + body + f"(?![{_LETTER}])"
    else:
        # Со словами ищем по основе: слово начинается с основы, окончание любое
        body = r"[\s\-]+".join(re.escape(_stem(w)) + r"[а-яёa-z]*" for w in words)
        body = f"(?<![{_LETTER}])" + body
    return re.compile(body, re.I)


def _compiled(session: Session) -> list[tuple[MonitorKeyword, re.Pattern]]:
    marker = (session.query(MonitorKeyword).count(),
              session.query(MonitorKeyword)
              .filter(MonitorKeyword.is_active.is_(True)).count())
    if _cache["key"] != marker or _cache["compiled"] is None:
        rows = (session.query(MonitorKeyword)
                .filter(MonitorKeyword.is_active.is_(True)).all())
        _cache["key"] = marker
        _cache["compiled"] = [(k, _pattern(k.phrase, bool(k.whole_word)))
                              for k in rows]
    return _cache["compiled"]


def reset_cache() -> None:
    _cache["key"] = None
    _cache["compiled"] = None


def match(session: Session, text: str) -> list[MonitorKeyword]:
    """Какие ключевые слова сработали на тексте."""
    if not text:
        return []
    return [k for k, rx in _compiled(session) if rx.search(text)]


def note_hits(session: Session, keywords: list[MonitorKeyword],
              when: dt.datetime | None = None) -> None:
    when = when or dt.datetime.utcnow()
    for k in keywords:
        k.hits = (k.hits or 0) + 1
        if k.last_hit_at is None or when > k.last_hit_at:
            k.last_hit_at = when
