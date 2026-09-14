"""Лиды из своей истории сделок — самый дешёвый и самый точный источник.

Отдел продаж помнит своих клиентов, но не помнит 698 покупателей за пять лет и
81 тысячу строк реализации. Между тем на вопрос «кому продать ВДМ» ответ уже
есть в 1С: 01.09.2025 четырнадцать таких машин ушли в «СОЮЗ-ТЕХНО ООО» по
26–81 тыс. ₽/шт. Здесь это находится за доли секунды и с доказательствами.

Лид тем ценнее, чем ближе он покупал к нашей позиции (ровно эту → типоразмер →
модельный ряд → семейство), чем свежее покупка и чем больше объём. Ломовые
покупатели показываются отдельным каналом: они дают пол цены, а не цену
изделия, и путать их с покупателями оборудования нельзя.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db.models import Counterparty, DealFact, Item
from ..normalize import (classify_family, extract_size_key, is_scrap, model_key,
                         series_key, tokens)

# Вес уровня родства позиции: «покупал ровно это» на порядок ценнее, чем
# «покупал что-то из этого семейства».
TIER_WEIGHT = {
    "ровно эта позиция": 100.0,
    "тот же типоразмер": 72.0,
    "тот же модельный ряд": 55.0,
    "это семейство": 32.0,
    "ломовой канал": 14.0,
}
# Уровни, на которых цена контрагента переносится на нашу позицию.
CLOSE_TIERS = ("ровно эта позиция", "тот же типоразмер", "тот же модельный ряд")
# За полтора года без сделок ценность лида падает вдвое.
HALF_LIFE_MONTHS = 18.0
PERSON_PENALTY = 0.55   # физлицо/розница — разовая продажа, не канал сбыта


@dataclass
class InternalLead:
    counterparty: Counterparty
    tier: str
    score: float
    deals: list[DealFact] = field(default_factory=list)
    price: float | None = None
    price_unit: str | None = None
    price_low: float | None = None
    price_high: float | None = None
    last_deal_at: dt.datetime | None = None
    manager: str | None = None
    channel: str = "свой покупатель"

    @property
    def why(self) -> str:
        n = len(self.deals)
        when = self.last_deal_at.strftime("%d.%m.%Y") if self.last_deal_at else "—"
        what = ", ".join(sorted({(d.item_name or "")[:48] for d in self.deals})[:3])
        base = (f"{self.tier}: {n} сдел. у нас, последняя {when}. Брал: {what}")
        if self.price:
            base += f". Платил {self.price:,.0f} ₽/{self.price_unit}"
        return base


def _months_ago(when: dt.datetime | None, now: dt.datetime) -> float:
    if when is None:
        return 60.0
    if when.tzinfo is not None:
        when = when.replace(tzinfo=None)
    return max(0.0, (now - when).days / 30.44)


def _tier_of(deal: DealFact, target: dict) -> str | None:
    """Насколько сделка близка к нашей позиции."""
    if target["scrap"] != bool(deal.is_scrap):
        # Деловое изделие и лом — разные товары и разные покупатели, но
        # ломовой покупатель это пол цены и запасной канал сбыта, поэтому он
        # показывается отдельно. Связь ищем по марке ряда: «Лом ПЭД (т)» и
        # «Лом аккумуляторных батарей ТНЖШ-350» относятся к семейству «лом»,
        # и по семейству их с изделием не свести.
        if deal.is_scrap and (
                (target["mark"] and deal.series_mark == target["mark"])
                or (target["family"] and deal.family == target["family"])):
            return "ломовой канал"
        return None
    if target["item_id"] and deal.item_id == target["item_id"]:
        return "ровно эта позиция"
    if target["model_key"] and len(target["model_key"]) >= 5 \
            and model_key(deal.item_name or "") == target["model_key"]:
        return "ровно эта позиция"
    if target["size_key"] and deal.size_key == target["size_key"]:
        return "тот же типоразмер"
    if target["mark"] and deal.series_mark == target["mark"]:
        return "тот же модельный ряд"
    if target["family"] and deal.family == target["family"]:
        return "это семейство"
    return None


def target_profile(session: Session, name: str, unit: str | None,
                   item: Item | None) -> dict:
    sk = series_key(name or "")
    return {
        "name": name,
        "unit": (unit or (item.unit if item else None) or "").strip().lower(),
        "item_id": item.id if item else None,
        "model_key": model_key(name or ""),
        "size_key": (item.size_key if item else None) or extract_size_key(name or ""),
        "mark": sk[0] if sk else None,
        "family": (item.family if item else None) or classify_family(name or ""),
        "scrap": is_scrap(name or ""),
    }


def find(session: Session, name: str, unit: str | None, item: Item | None,
         limit: int = 40) -> list[InternalLead]:
    """Покупатели из своей истории, отсортированные по убыванию пригодности."""
    target = target_profile(session, name, unit, item)
    now = dt.datetime.utcnow()

    q = session.query(DealFact).filter(DealFact.direction == "sale")
    # Отбор широкий, но не по всей базе: без этого пришлось бы читать 81 тыс.
    # строк на каждый запрос.
    filters = []
    if target["item_id"]:
        filters.append(DealFact.item_id == target["item_id"])
    if target["size_key"]:
        filters.append(DealFact.size_key == target["size_key"])
    if target["mark"]:
        filters.append(DealFact.series_mark == target["mark"])
    if target["family"]:
        filters.append(DealFact.family == target["family"])
    if not filters:
        return []
    from sqlalchemy import or_
    deals = q.filter(or_(*filters)).all()

    by_cp: dict[int, list[tuple[str, DealFact]]] = {}
    for d in deals:
        tier = _tier_of(d, target)
        if tier is None or not d.counterparty_id:
            continue
        by_cp.setdefault(d.counterparty_id, []).append((tier, d))

    # Ломовой канал по словам названия. Марка и семейство его не находят:
    # «Лом аккумуляторных батарей (кг)» относится к семейству «лом» и марки
    # ряда не имеет, но именно его покупатель (СУМЗ-ВЦМ) — запасной сбыт для
    # батарей ТНЖШ, которые как изделие у нас никогда не продавались.
    if not target["scrap"]:
        words = [t for t in tokens(name or "")
                 if len(t) >= 5 and not any(ch.isdigit() for ch in t)]
        if words:
            scrap_rows = (session.query(DealFact)
                          .filter(DealFact.direction == "sale",
                                  DealFact.is_scrap.is_(True),
                                  or_(*[DealFact.item_name.ilike(f"%{w}%")
                                        for w in words[:2]]))
                          .all())
            for d in scrap_rows:
                if d.counterparty_id:
                    by_cp.setdefault(d.counterparty_id, []).append(
                        ("ломовой канал", d))

    out: list[InternalLead] = []
    for cp_id, rows in by_cp.items():
        cp = session.get(Counterparty, cp_id)
        if cp is None or cp.is_internal:
            continue
        best_tier = min((t for t, _ in rows), key=lambda t: -TIER_WEIGHT[t])
        same = [d for t, d in rows if t == best_tier]
        last = max((d.period for d in same if d.period), default=None)
        revenue = sum(d.revenue or 0.0 for d in same)

        recency = 0.5 ** (_months_ago(last, now) / HALF_LIFE_MONTHS)
        volume = math.log1p(max(revenue, 0.0)) / math.log1p(50_000_000.0)
        score = TIER_WEIGHT[best_tier] * recency * (0.6 + 0.4 * min(volume, 1.0))
        if cp.is_person:
            score *= PERSON_PENALTY

        # Цена: что этот контрагент платил в нашей единице учёта. Берём её как
        # ожидаемую ТОЛЬКО при близком родстве позиции. Иначе получается
        # обман: «КАМЭЛЕКТРОПРИВОД платил 400 000 ₽/шт» — но за СТДМ-1250,
        # а не за ВДМ 60, и к нашей позиции эта цифра отношения не имеет.
        prices = [d.price for d in same
                  if d.price and d.unit and target["unit"]
                  and d.unit.strip().lower() == target["unit"]]
        if best_tier not in CLOSE_TIERS:
            prices = []
        price = statistics.median(prices) if prices else None
        out.append(InternalLead(
            counterparty=cp,
            tier=best_tier,
            score=round(score, 2),
            deals=sorted(same, key=lambda d: d.period or dt.datetime.min,
                         reverse=True)[:8],
            price=price,
            price_unit=target["unit"] or None,
            price_low=min(prices) if prices else None,
            price_high=max(prices) if prices else None,
            last_deal_at=last,
            manager=next((d.manager for d in same if d.manager), None),
            channel="ломовой канал" if best_tier == "ломовой канал"
                    else "свой покупатель",
        ))

    out.sort(key=lambda l: -l.score)
    return out[:limit]
