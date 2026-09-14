"""Ценовой коридор позиции: пол по металлу — ориентир — потолок рынка.

Менеджеру нужна не одна цифра, а рамка торга: ниже пола продавать нельзя
(металл стоит дороже), ориентир — то, за что мы сами продавали такое, потолок —
максимум, который рынок платил. Все три числа приходят с основанием, чтобы в
переговорах на них можно было сослаться.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..calc import price_ladder
from ..db.models import DealFact, Item, ScrapPrice
from ..normalize import classify_family, extract_size_key, series_key

DEFAULT_FERROUS = "чермет"


@dataclass
class Corridor:
    unit: str | None = None
    floor: float | None = None
    floor_basis: str | None = None
    target: float | None = None
    target_basis: str | None = None
    ceiling: float | None = None
    ceiling_basis: str | None = None
    candidates: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"unit": self.unit,
                "floor": self.floor, "floor_basis": self.floor_basis,
                "target": self.target, "target_basis": self.target_basis,
                "ceiling": self.ceiling, "ceiling_basis": self.ceiling_basis,
                "candidates": self.candidates}


def _scrap_floor(session: Session, item: Item | None,
                 unit: str | None) -> tuple[float | None, str | None]:
    """Пол цены: сколько единица стоит как металл.

    Считается только когда известна масса единицы — гадать здесь опаснее, чем
    промолчать: заниженный пол развяжет руки на торге в минус.
    """
    prices = {sp.material: sp.price_per_tonne for sp in
              session.query(ScrapPrice).filter(ScrapPrice.is_current.is_(True))}
    ferrous = prices.get(DEFAULT_FERROUS)
    if not ferrous:
        return None, None
    u = (unit or "").strip().lower()
    if u in ("т", "тн", "тонна"):
        return ferrous, f"чермет {ferrous:,.0f} ₽/т (прайс лома)"
    if u == "кг":
        return ferrous / 1000.0, f"чермет {ferrous:,.0f} ₽/т (прайс лома)"
    if item is not None and item.unit_mass_kg:
        return (item.unit_mass_kg * ferrous / 1000.0,
                f"{item.unit_mass_kg:,.0f} кг × чермет {ferrous:,.0f} ₽/т")
    return None, ("масса единицы неизвестна — задайте её в карточке, "
                  "иначе пол цены по металлу не посчитать")


def _market_ceiling(session: Session, name: str, unit: str | None,
                    item: Item | None) -> tuple[float | None, str | None]:
    """Максимум, который за такое реально платили (по нашим же сделкам)."""
    u = (unit or "").strip().lower()
    sk = series_key(name or "")
    size_key = (item.size_key if item else None) or extract_size_key(name or "")
    family = (item.family if item else None) or classify_family(name or "")

    q = session.query(DealFact).filter(DealFact.direction == "sale",
                                       DealFact.is_scrap.is_(False),
                                       DealFact.price.isnot(None))
    if u:
        q = q.filter(DealFact.unit == u)
    rows = []
    scope = None
    if size_key:
        rows = q.filter(DealFact.size_key == size_key).all()
        scope = f"типоразмер «{size_key}»"
    if not rows and sk:
        rows = q.filter(DealFact.series_mark == sk[0]).all()
        scope = f"ряд {sk[0].upper()}"
    if not rows and family:
        rows = q.filter(DealFact.family == family).all()
        scope = f"семейство «{family}»"
    prices = sorted(r.price for r in rows if r.price and r.price > 0)
    if not prices:
        return None, None
    # 90-й процентиль, а не абсолютный максимум: единичная нетипичная сделка
    # не должна становиться целью торга
    idx = min(len(prices) - 1, int(round(0.9 * (len(prices) - 1))))
    top = prices[idx]
    return top, (f"90-й процентиль наших продаж, {scope}: "
                 f"{len(prices)} сдел., медиана {statistics.median(prices):,.0f}")


def corridor(session: Session, name: str, unit: str | None,
             item: Item | None) -> Corridor:
    u = (unit or (item.unit if item else None) or "").strip().lower()
    c = Corridor(unit=u or None)

    cands = price_ladder.candidates(session, name, u, item)
    c.candidates = [x.as_dict() for x in cands]
    if cands:
        c.target, c.target_basis = cands[0].price, cands[0].label

    c.floor, c.floor_basis = _scrap_floor(session, item, u)
    c.ceiling, c.ceiling_basis = _market_ceiling(session, name, u, item)

    # согласование: ориентир не может быть ниже пола и выше потолка
    if c.target is not None and c.floor is not None and c.target < c.floor:
        c.target, c.target_basis = c.floor, (
            f"пол по металлу выше рыночного ориентира — продавать дешевле "
            f"металла нет смысла ({c.floor_basis})")
    if c.ceiling is not None and c.target is not None and c.ceiling < c.target:
        c.ceiling, c.ceiling_basis = c.target, c.target_basis
    return c
