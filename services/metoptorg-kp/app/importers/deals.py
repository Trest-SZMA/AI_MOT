"""Импорт сделок по контрагентам из выгрузки «Движение ТМЦ Факт Доработка».

Импортёр цен (`one_c.import_sales_facts`) брал из этого файла только последнюю
цену на номенклатуру и выбрасывал остальное — вместе с контрагентом. При этом
именно здесь лежит ответ на вопрос «кому продать»: 44 тыс. строк реализации,
698 покупателей, с датами, объёмами, ценами и фамилией менеджера сделки.

Ловушки файла, проверенные на данных:

* каждая позиция документа идёт ДВУМЯ строками — количественной и суммовой;
  строка с нулём количества и нулём цены не является сделкой и отбрасывается;
* ИНН часто записан прямо в наименовании: «ФЛАГМАН ООО (ИНН 6319240833)»;
* «проект Базы», «Проект Кабель» и подобное — внутренние обороты, а не клиенты;
  такие контрагенты помечаются is_internal и в лиды не попадают;
* физлица и «(нал)» — разовые покупатели, отдельная пометка is_person.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db.models import Counterparty, DealFact, Item, ItemAlias
from ..normalize import classify_family, extract_size_key, is_scrap, normalize_name, series_key

log = logging.getLogger(__name__)

SALE_OPS = ("Реализация",)
PURCHASE_OPS = ("Закупка у поставщика", "Закупка через подотчетное лицо")

_INN_RE = re.compile(r"\bИНН\s*(\d{10}(?:\d{2})?)")
_PERSON_RE = re.compile(r"физ[\s.]*лицо|\(нал\)|розниц|^[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.",
                        re.I)
_INTERNAL_RE = re.compile(r"^\s*проект\b|^\s*склад\b|^\s*без\s+контрагента", re.I)


@dataclass
class DealsStats:
    rows: int = 0
    sales: int = 0
    purchases: int = 0
    counterparties: int = 0
    skipped_zero: int = 0
    skipped_no_name: int = 0


def _num(v) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _period(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    s = str(raw).strip()[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _guid_map(session: Session) -> dict[str, int]:
    """guid номенклатуры → id карточки, включая guid'ы объединённых дублей."""
    out = {g.lower(): i for i, g in session.query(Item.id, Item.guid)
           if g}
    for item_id, guid in session.query(ItemAlias.item_id, ItemAlias.guid):
        if guid:
            out.setdefault(guid.lower(), item_id)
    return out


def _upsert_counterparty(session: Session, cache: dict, guid: str,
                         name: str) -> Counterparty | None:
    name = (name or "").strip()
    if not name:
        return None
    key = guid.lower() if guid else normalize_name(name)
    cp = cache.get(key)
    if cp is not None:
        return cp
    cp = None
    if guid:
        cp = session.query(Counterparty).filter(
            Counterparty.guid == guid.lower()).first()
    if cp is None:
        cp = session.query(Counterparty).filter(
            Counterparty.name_normalized == normalize_name(name)).first()
    if cp is None:
        m = _INN_RE.search(name)
        cp = Counterparty(
            guid=guid.lower() if guid else None,
            name=name,
            name_normalized=normalize_name(name),
            inn=m.group(1) if m else None,
            is_person=bool(_PERSON_RE.search(name)),
            is_internal=bool(_INTERNAL_RE.search(name)),
        )
        session.add(cp)
        session.flush()
    cache[key] = cp
    return cp


def import_deals(session: Session, csv_path: str,
                 replace: bool = True) -> DealsStats:
    """Загрузить историю сделок по контрагентам."""
    stats = DealsStats()
    guid_map = _guid_map(session)
    if replace:
        session.query(DealFact).delete(synchronize_session=False)
        session.query(Counterparty).delete(synchronize_session=False)
        session.commit()

    cache: dict[str, Counterparty] = {}
    agg: dict[int, dict] = {}   # counterparty_id → накопительные итоги
    batch = 0

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            op = (row.get("ХозяйственнаяОперация") or "").strip()
            if op.startswith(SALE_OPS):
                direction = "sale"
            elif op in PURCHASE_OPS:
                direction = "purchase"
            else:
                continue
            qty = _num(row.get("Количество"))
            price = _num(row.get("Цена"))
            revenue = _num(row.get("Выручка"))
            if qty <= 0 and revenue <= 0:
                stats.skipped_zero += 1   # парная строка документа
                continue
            name = (row.get("Контрагент") or "").strip()
            if not name:
                stats.skipped_no_name += 1
                continue
            cp = _upsert_counterparty(
                session, cache, (row.get("КонтрагентГуид") or "").strip(), name)
            if cp is None:
                continue
            item_name = (row.get("Номенклатура") or "").strip()
            item_guid = (row.get("НоменклатураГуид") or "").strip().lower()
            sk = series_key(item_name)
            when = _period(row.get("Период"))
            session.add(DealFact(
                counterparty_id=cp.id,
                item_id=guid_map.get(item_guid),
                item_name=item_name,
                item_guid=item_guid or None,
                family=classify_family(item_name),
                size_key=extract_size_key(item_name),
                series_mark=sk[0] if sk else None,
                direction=direction,
                period=when,
                quantity=qty or None,
                unit=(row.get("ЕдиницаИзмерения") or "").strip() or None,
                price=price or (revenue / qty if qty else None),
                revenue=revenue or None,
                manager=(row.get("Менеджер") or "").strip() or None,
                warehouse=(row.get("Склад") or "").strip() or None,
                is_scrap=is_scrap(item_name),
            ))
            a = agg.setdefault(cp.id, {"sales": 0, "revenue": 0.0, "purch": 0,
                                       "first": None, "last": None, "mgr": None})
            if direction == "sale":
                a["sales"] += 1
                a["revenue"] += revenue
                stats.sales += 1
            else:
                a["purch"] += 1
                stats.purchases += 1
            if when:
                a["first"] = when if a["first"] is None else min(a["first"], when)
                if a["last"] is None or when >= a["last"]:
                    a["last"] = when
                    a["mgr"] = (row.get("Менеджер") or "").strip() or a["mgr"]
            batch += 1
            if batch % 5000 == 0:
                session.flush()

    session.flush()
    for cp_id, a in agg.items():
        cp = session.get(Counterparty, cp_id)
        cp.sales_count = a["sales"]
        cp.sales_revenue = a["revenue"]
        cp.purchase_count = a["purch"]
        cp.first_deal_at = a["first"]
        cp.last_deal_at = a["last"]
        cp.last_manager = a["mgr"]
    stats.counterparties = len(agg)
    session.commit()
    log.info("сделки загружены: %s", stats)
    return stats
