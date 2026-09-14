"""Цена реализации по аналогам из фактов 1С.

Закрывает главный пробел: у позиции нет своей цены продажи, но компания
продавала похожие. Аналог ищется по (семейство, типоразмер, единица) —
средневзвешенно по фактам, с разбросом min–max и числом наблюдений.

Приоритет в движке: ручная → sales_fact (своя) → АНАЛОГ → resale/market →
ИИ-оценка → floor по лому.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db.models import Item, PriceQuote

# кэш индекса: перестраивается при изменении числа фактов продаж
_cache: dict = {"key": None, "index": None}


@dataclass
class AnalogPrice:
    price: float
    unit: str
    samples: int
    low: float
    high: float
    basis: str  # 'size' | 'family'

    @property
    def label(self) -> str:
        scope = "типоразмер" if self.basis == "size" else "семейство"
        return (f"аналог по {scope}: {self.samples} факт. продаж, "
                f"{self.low:,.0f}–{self.high:,.0f} ₽/{self.unit}")


def reset_cache() -> None:
    _cache["key"] = None
    _cache["index"] = None


def _build_index(session: Session) -> dict:
    """(уровень, ключ, единица) → список цен из фактов продаж торговых позиций."""
    rows = (session.query(PriceQuote.price, PriceQuote.unit, Item.family,
                          Item.size_key)
            .join(Item, PriceQuote.item_id == Item.id)
            .filter(PriceQuote.quote_type == "sales_fact",
                    Item.is_trade.is_(True),
                    PriceQuote.price > 0).all())
    index: dict[tuple, list[float]] = {}
    for price, unit, family, size_key in rows:
        u = (unit or "").strip().lower()
        if not u:
            continue
        if size_key:
            index.setdefault(("size", size_key, u), []).append(price)
        if family:
            index.setdefault(("family", family, u), []).append(price)
    return index


def _index(session: Session) -> dict:
    key = session.query(PriceQuote).filter(
        PriceQuote.quote_type == "sales_fact").count()
    if _cache["key"] != key or _cache["index"] is None:
        _cache["key"] = key
        _cache["index"] = _build_index(session)
    return _cache["index"]


def find_analog(session: Session, family: str | None, size_key: str | None,
                unit: str | None, min_samples: int = 3) -> AnalogPrice | None:
    """Цена аналога: сначала по типоразмеру, затем по семейству."""
    u = (unit or "").strip().lower()
    if not u:
        return None
    idx = _index(session)
    for basis, key in (("size", size_key), ("family", family)):
        if not key:
            continue
        vals = idx.get((basis, key, u))
        if vals and len(vals) >= min_samples:
            return AnalogPrice(
                price=statistics.median(vals),  # медиана устойчива к выбросам
                unit=u,
                samples=len(vals),
                low=min(vals),
                high=max(vals),
                basis=basis,
            )
    return None


def family_medians(session: Session) -> dict[tuple[str, str], float]:
    """(семейство, единица) → медианная цена продаж — база для поиска выбросов."""
    idx = _index(session)
    return {(key, unit): statistics.median(vals)
            for (basis, key, unit), vals in idx.items()
            if basis == "family" and len(vals) >= 5}
