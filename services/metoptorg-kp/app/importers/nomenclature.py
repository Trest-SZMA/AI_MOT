"""Импорт справочника номенклатуры из выгрузки 1С («Справочники.xlsx»).

Конвейер: отбор торговых позиций → нормализация → авто-слияние точных дублей
(guid'ы в item_aliases) → очередь нечётких кандидатов. Режим «только новые».
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

import openpyxl
from sqlalchemy.orm import Session

from ..db.models import Item, ItemAlias, MergeCandidate
from ..normalize import classify_family, extract_size_key, normalize_name, tokens

log = logging.getLogger(__name__)

# белый список групп (управляется в UI; стартовый — из реальной выгрузки)
TRADE_GROUPS = {
    "дхно",
    "лом цветных металлов",
    "лом черных металлов",
    "лом чёрных металлов",
    "лом легированной стали",
    "кабель",
    "труба",
    "труба больших диаметров (более 159 мм)",
    "труба малых диаметров (до 159 мм)",
    "труба и штанга",
    "комплектующие с разбора",
    "давальческое сырье",
    "давальческое сырьё",
    "металлоизделия",
    "оборудование",
}

BLACK_GROUPS_RE = re.compile(
    r"спецодежда|средства защиты|мебель|хоз|автозапчаст|гсм|запчаст|инструмент"
    r"|сетевое оборудование|it оборудование|сальник|манжет|услуг|работ",
    re.I,
)

# Явно непрофильные позиции: попадают в справочник через группу ДХНО, но
# металлотрейдер их не продаёт. Проверять ДО белого списка групп.
NON_TRADE_NAME_RE = re.compile(
    r"картридж|тонер|фотобарабан|драм-?картридж|\bмышь\b|клавиатур|монитор\b"
    r"|ноутбук|принтер|\bмфу\b|сканер\b|бумага|канцеляр|ручк[аи] шариков"
    r"|костюм|куртк|сапог|ботинк|перчатк|рукавиц|каск[аи]\b|респиратор"
    r"|очки защит|спецодежд|халат|жилет сигнальн"
    r"|чай\b|кофе\b|сахар|салфетк|полотенц|мыло|моющее"
    r"|лампа (энергосб|люминесц|светодиод)|светильник",
    re.I)

TRADE_NAME_RE = re.compile(
    r"нкт|труба|пэд|кабел|эцн|станци[яи] упр|су-|трансформатор|тмпн|лом\b"
    r"|гидрозащит|статор|обмотк|двигател.*погружн|насос|секци|провод|катанк"
    r"|медь|латун|алюмин|нержав|нирезист",
    re.I,
)
BU_RE = re.compile(r"б/у", re.I)


@dataclass
class ImportStats:
    rows: int = 0
    created: int = 0
    skipped_existing: int = 0
    merged_duplicates: int = 0
    trade: int = 0
    hidden: int = 0
    fuzzy_candidates: int = 0
    errors: list[str] = field(default_factory=list)


def _is_trade(name: str, group: str | None) -> tuple[bool, bool]:
    """→ (is_trade, is_hidden)."""
    g = (group or "").strip().lower()
    if g.startswith("_") or re.match(r"\d{2}/\d{4}", g) or "не использовать" in g:
        return False, True
    # непрофильное (оргтехника, спецодежда, бытовое) отсекаем даже внутри
    # белых групп: в ДХНО попадают картриджи и костюмы, которые мы не продаём
    if NON_TRADE_NAME_RE.search(name):
        return False, False
    if g in TRADE_GROUPS:
        return True, False
    if g and BLACK_GROUPS_RE.search(g):
        return False, False
    # позиции без группы (или с неизвестной) — классификация по имени
    if TRADE_NAME_RE.search(name) and (BU_RE.search(normalize_name(name)) or "лом" in name.lower()
                                       or g in ("", "основная номенклатурная группа")):
        return True, False
    if TRADE_NAME_RE.search(name) and not g:
        return True, False
    return False, False


def import_nomenclature(session: Session, xlsx_path: str,
                        sheet: str = "Номенклатура") -> ImportStats:
    stats = ImportStats()
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}

    def col(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    existing_norms: dict[str, int] = {
        n: iid for n, iid in session.query(Item.name_normalized, Item.id)
    }
    existing_guids: set[str] = {
        g for (g,) in session.query(Item.guid).filter(Item.guid.isnot(None))
    } | {g for (g,) in session.query(ItemAlias.guid).filter(ItemAlias.guid.isnot(None))}

    for row in rows:
        name = col(row, "Номенклатура")
        if not name or not str(name).strip():
            continue
        stats.rows += 1
        name = str(name).strip()
        guid = str(col(row, "НоменклатураГуид") or "").strip() or None
        if guid and guid in existing_guids:
            stats.skipped_existing += 1  # режим «только новые»
            continue
        unit = str(col(row, "ЕдиницаИзмерения") or "").strip() or None
        group = str(col(row, "НоменклатурнаяГруппаГрузов") or "").strip() or None
        norm = normalize_name(name)
        is_trade, is_hidden = _is_trade(name, group)

        canon_id = existing_norms.get(norm)
        if canon_id is not None:
            # точный дубль по нормализованному имени → alias
            session.add(ItemAlias(item_id=canon_id, guid=guid, name=name,
                                  name_normalized=norm))
            stats.merged_duplicates += 1
            if guid:
                existing_guids.add(guid)
            continue

        item = Item(
            guid=guid,
            name=name,
            name_normalized=norm,
            unit=unit,
            category=group,
            family=classify_family(name) if is_trade else None,
            size_key=extract_size_key(name) if is_trade else None,
            is_trade=is_trade,
            is_hidden=is_hidden,
            source="1c",
        )
        session.add(item)
        session.flush()
        existing_norms[norm] = item.id
        if guid:
            existing_guids.add(guid)
        stats.created += 1
        stats.trade += int(is_trade)
        stats.hidden += int(is_hidden)

    wb.close()
    session.commit()
    stats.fuzzy_candidates = _queue_fuzzy_candidates(session)
    return stats


def _queue_fuzzy_candidates(session: Session, threshold: float = 0.9,
                            limit: int = 2000) -> int:
    """Нечёткие дубли среди торговых позиций → очередь на ручное объединение."""
    items = session.query(Item.id, Item.name_normalized).filter(
        Item.is_trade.is_(True)).all()
    by_tokens: dict[frozenset, list[int]] = defaultdict(list)
    for iid, norm in items:
        key = frozenset(tokens(norm))
        if key:
            by_tokens[key].append(iid)

    existing = {
        tuple(sorted(p)) for p in session.query(
            MergeCandidate.item_id_a, MergeCandidate.item_id_b)
    }
    added = 0
    for ids in by_tokens.values():
        if len(ids) < 2 or added >= limit:
            continue
        # одинаковое мультимножество токенов, но разные нормализованные имена
        a = ids[0]
        for b in ids[1:]:
            pair = tuple(sorted((a, b)))
            if pair in existing:
                continue
            session.add(MergeCandidate(item_id_a=pair[0], item_id_b=pair[1],
                                       score=1.0))
            existing.add(pair)
            added += 1
    session.commit()
    return added


# ---------------------------------------------------------------- слияние

_ALNUM_ONLY = re.compile(r"[^a-zа-я0-9]")


def strict_key(name: str) -> str:
    """Имя без пунктуации и пробелов; «б/у» СОХРАНЯЕТСЯ.

    В отличие от model_key (он для сопоставления перечней) здесь «б/у» значим:
    новая и б/у карточки — разный товар с разной ценой.
    """
    return _ALNUM_ONLY.sub("", normalize_name(name))


def merge_tier(a, b) -> str | None:
    """Насколько очевидно, что это дубли: 'safe' | 'bu' | None."""
    from ..normalize import unit_info

    ua, ub = unit_info(a.unit), unit_info(b.unit)
    if ua and ub and ua[0] != ub[0]:
        return None  # штуки против тонн — разные учётные сущности
    ka, kb = strict_key(a.name), strict_key(b.name)
    if ka == kb:
        return "safe"  # различие только в пунктуации/пробелах
    if ka.replace("бу", "") == kb.replace("бу", ""):
        return "bu"    # различие только в признаке «б/у»
    return None


def pick_canonical(session: Session, a, b):
    """Какая карточка останется: с большей историей, затем с чистым именем."""
    from ..db.models import ComponentYield, ItemAlias, KpPosition, PriceQuote

    def weight(item):
        links = (session.query(PriceQuote).filter(
                     PriceQuote.item_id == item.id).count()
                 + session.query(KpPosition).filter(
                     KpPosition.item_id == item.id).count()
                 + session.query(ItemAlias).filter(
                     ItemAlias.item_id == item.id).count()
                 + session.query(ComponentYield).filter(
                     ComponentYield.item_id == item.id).count())
        # имя без служебного мусора в начале («*Металлолом», «/Кабель») лучше
        clean = 0 if re.match(r"^[\s*/\\-]", item.name or "") else 1
        return (links, clean, -len(item.name or ""))

    return (a, b) if weight(a) >= weight(b) else (b, a)


def merge_items(session: Session, keep, drop, user: str = "") -> None:
    """Слить карточку drop в keep: guid и все связи переезжают."""
    from ..db.models import ComponentYield, ItemAlias, KpPosition, PriceQuote
    from ..normalize import unit_info

    if keep.id == drop.id:
        raise ValueError("нельзя слить карточку саму с собой")
    # Цепочки запрещены: если карточка уже поглощена, повторное слияние
    # создаёт два alias на одну и ту же запись у разных владельцев.
    for it, role in ((keep, "каноническая"), (drop, "поглощаемая")):
        if not it.is_trade or it.is_hidden:
            raise ValueError(
                f"{role} карточка «{it.name}» уже объединена или не торговая")
    uk, ud = unit_info(keep.unit), unit_info(drop.unit)
    if uk and ud and uk[0] != ud[0]:
        raise ValueError(
            f"нельзя слить «{keep.name}» ({keep.unit}) и «{drop.name}» "
            f"({drop.unit}): разные семейства единиц")
    session.add(ItemAlias(item_id=keep.id, guid=drop.guid, name=drop.name,
                          name_normalized=drop.name_normalized))
    for table in (ItemAlias, PriceQuote, KpPosition, ComponentYield):
        column = table.item_id
        session.query(table).filter(column == drop.id).update(
            {"item_id": keep.id}, synchronize_session=False)
    drop.is_trade = False
    drop.is_hidden = True
    drop.guid = None  # guid переехал в alias — уникальность не нарушаем
