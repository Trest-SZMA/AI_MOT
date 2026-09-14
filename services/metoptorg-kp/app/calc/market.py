"""Связка «рынок → наш коэффициент → рабочая цена лома».

Индекс обзора — внешний ориентир, а не наша выручка: по фактам 1С компания
продаёт чермет примерно на треть дешевле индекса (марка, засор, условия
приёмки). Поэтому рабочая цена = индекс региона × исторический коэффициент.
Коэффициент считается из наших же продаж и хранится нормативом.
"""
from __future__ import annotations

import statistics

from sqlalchemy.orm import Session

from ..db.models import ApprovedValue, MarketPrice

RATIO_KEY = "market_ratio"
# Обзор индексирует ТОЛЬКО чёрный лом (марка 3А). Привязывать к нему медь или
# алюминий нельзя: это независимые рынки, коэффициент получился бы 5× и увёл
# бы цену цветмета за индексом чермета.
INDEXED_MATERIALS = {"чермет"}
HOME_REGION_KEY = "market_home_region"
DEFAULT_HOME_REGION = "Пермский кр."


def latest_issue(session: Session) -> str | None:
    row = (session.query(MarketPrice)
           .order_by(MarketPrice.imported_at.desc()).first())
    return row.issue if row else None


def market_index(session: Session, region: str,
                 issue: str | None = None) -> MarketPrice | None:
    """Индекс FCA по региону за выпуск (по умолчанию — последний)."""
    issue = issue or latest_issue(session)
    if not issue:
        return None
    return (session.query(MarketPrice)
            .filter(MarketPrice.issue == issue,
                    MarketPrice.scope_kind == "region",
                    MarketPrice.scope == region).first())


def get_ratio(session: Session, material: str) -> tuple[float | None, str | None]:
    """Коэффициент «наша цена / индекс» и пояснение, откуда он взят."""
    row = (session.query(ApprovedValue)
           .filter(ApprovedValue.key == RATIO_KEY,
                   ApprovedValue.scope == material,
                   ApprovedValue.is_current.is_(True))
           .order_by(ApprovedValue.approved_at.desc()).first())
    return (row.value, row.notes) if row else (None, None)


def set_ratio(session: Session, material: str, ratio: float, notes: str,
              user: str = "") -> None:
    if material not in INDEXED_MATERIALS:
        raise ValueError(
            f"«{material}» не индексируется обзором: он про чёрный лом (3А). "
            f"Цену цветного металла ведём по нашим фактам продаж, "
            f"а не через коэффициент к индексу чермета.")
    session.query(ApprovedValue).filter(
        ApprovedValue.key == RATIO_KEY, ApprovedValue.scope == material,
        ApprovedValue.is_current.is_(True)).update({"is_current": False})
    session.add(ApprovedValue(key=RATIO_KEY, scope=material, value=round(ratio, 4),
                              unit="доля индекса", approved_by=user, notes=notes))
    session.commit()


def home_region(session: Session) -> str:
    row = (session.query(ApprovedValue)
           .filter(ApprovedValue.key == HOME_REGION_KEY,
                   ApprovedValue.is_current.is_(True)).first())
    return (row.notes or DEFAULT_HOME_REGION) if row else DEFAULT_HOME_REGION


def fact_price(session: Session, material: str) -> dict | None:
    """Цена по нашим фактам продаж — для металлов, которых нет в индексе."""
    from ..db.models import Item, PriceQuote
    from ..importers.one_c import scrap_material

    # часть лома продаётся в килограммах — приводим к ₽/т, иначе выборка
    # получается втрое меньше и цена цветмета считается по случайным сделкам
    rows = (session.query(PriceQuote.price, PriceQuote.unit, Item.name)
            .join(Item, PriceQuote.item_id == Item.id)
            .filter(PriceQuote.quote_type == "sales_fact",
                    Item.family == "лом").all())
    vals = []
    for price, unit, name in rows:
        u = (unit or "").strip().lower()
        factor = 1.0 if u in ("т", "тн", "тонна") else 1000.0 if u == "кг" else None
        if factor is None or price <= 0 or scrap_material(name) != material:
            continue
        vals.append(price * factor)
    if len(vals) < 3:
        return None
    return {"recommended": round(statistics.median(vals), 2),
            "samples": len(vals), "index": None, "ratio": None,
            "explanation": (f"медиана наших продаж: "
                            f"{statistics.median(vals):,.0f} ₽/т по {len(vals)} "
                            f"позициям · индекса рынка для «{material}» нет "
                            f"(обзор про чёрный лом)")}


def recommended_price(session: Session, material: str,
                      region: str | None = None) -> dict | None:
    """Рабочая цена = индекс × коэффициент, со всей прослеживаемостью."""
    if material not in INDEXED_MATERIALS:
        return fact_price(session, material)
    region = region or home_region(session)
    idx = market_index(session, region)
    if idx is None:
        return None
    ratio, ratio_notes = get_ratio(session, material)
    if ratio is None:
        return {"region": region, "index": idx.price_per_tonne, "issue": idx.issue,
                "ratio": None, "recommended": None,
                "explanation": (f"индекс {idx.price_per_tonne:,.0f} ₽/т "
                                f"({region}, вып. {idx.issue}, базис {idx.basis}); "
                                f"коэффициент по «{material}» не рассчитан — "
                                f"пересчитайте калибровку")}
    price = idx.price_per_tonne * ratio
    return {"region": region, "index": idx.price_per_tonne, "issue": idx.issue,
            "basis": idx.basis, "ratio": ratio, "recommended": round(price, 2),
            "ratio_notes": ratio_notes,
            "explanation": (f"{idx.price_per_tonne:,.0f} ₽/т × {ratio:.3f} = "
                            f"{price:,.0f} ₽/т · индекс {region}, вып. {idx.issue}, "
                            f"базис {idx.basis} · коэффициент: {ratio_notes or '—'}")}


def calibrate_from_sales(prices: list[float], index_price: float) -> float:
    """Коэффициент по медиане наших продаж (устойчива к разовым сделкам)."""
    if not prices or index_price <= 0:
        raise ValueError("нужны наши продажи и индекс рынка")
    return statistics.median(prices) / index_price
