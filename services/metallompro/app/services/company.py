"""Аналитика по данным компании (1С): факт-логистика, продажи, остатки, маржа.

Все расчёты помечаются как «оценка по данным 1С» — точность зависит от полноты выгрузок.
"""
from __future__ import annotations
from datetime import date, timedelta
from statistics import median

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CompanySale, CompanyTrip, CompanyStock

# Маппинг подразделений/складов на целевые регионы калькулятора
DIVISION_REGION = [
    ("волгоград", "SOUTH"), ("юг", "SOUTH"),
    ("усинск", "KOMI_PERM"), ("ухта", "KOMI_PERM"), ("коми", "KOMI_PERM"),
    ("когалым", "HMAO"), ("лангепас", "HMAO"), ("советский", "HMAO"),
    ("запад", "HMAO"), ("сибир", "HMAO"),
    ("пермь", "PERM"), ("осенцы", "PERM"), ("березники", "PERM"),
    ("оса", "PERM"), ("пермск", "PERM"), ("мгм", "PERM"), ("свк", "PERM"),
]


def place_to_region(text: str) -> str | None:
    t = (text or "").lower()
    for kw, reg in DIVISION_REGION:
        if kw in t:
            return reg
    return None


# ── Факт-логистика ───────────────────────────────────────────────
def actual_auto_rate(db: Session, days: int = 540) -> dict | None:
    """Медианный фактический тариф ₽/т/100км по собственному+наёмному авто."""
    since = date.today() - timedelta(days=days)
    rows = (db.query(CompanyTrip.km, CompanyTrip.weight_ttn, CompanyTrip.cost_rub)
            .filter(CompanyTrip.load_date >= since,
                    CompanyTrip.km > 50, CompanyTrip.weight_ttn > 5,
                    CompanyTrip.cost_rub > 1000,
                    CompanyTrip.delivery_kind.like("Автотранспорт%")).all())
    rates = [(c / w) / (km / 100) for km, w, c in rows if w and km]
    if len(rates) < 20:
        return None
    rates.sort()
    n = len(rates)
    return {"median": round(rates[n // 2]), "p25": round(rates[n // 4]),
            "p75": round(rates[3 * n // 4]), "trips": n,
            "period_days": days, "source": "1С «Отвесная»"}


def route_rates(db: Session, min_trips: int = 5) -> list[dict]:
    """Фактический тариф по направлениям (место погрузки → грузополучатель)."""
    rows = (db.query(CompanyTrip.from_place, CompanyTrip.recipient,
                     CompanyTrip.km, CompanyTrip.weight_ttn, CompanyTrip.cost_rub)
            .filter(CompanyTrip.km > 50, CompanyTrip.weight_ttn > 5,
                    CompanyTrip.cost_rub > 1000).all())
    routes: dict[tuple, list] = {}
    for fp, rc, km, w, c in rows:
        routes.setdefault((fp, rc), []).append(((c / w) / (km / 100), km))
    out = []
    for (fp, rc), vals in routes.items():
        if len(vals) < min_trips:
            continue
        rs = sorted(v[0] for v in vals)
        out.append({"from": fp, "to": rc, "trips": len(vals),
                    "rate_per_100km": round(rs[len(rs) // 2]),
                    "km": round(median(v[1] for v in vals))})
    out.sort(key=lambda x: -x["trips"])
    return out


def expensive_trips(db: Session, days: int = 90, limit: int = 30) -> list[dict]:
    """Рейсы с тарифом выше 75-го перцентиля — кандидаты на разбор."""
    since = date.today() - timedelta(days=days)
    rows = (db.query(CompanyTrip)
            .filter(CompanyTrip.load_date >= since, CompanyTrip.km > 50,
                    CompanyTrip.weight_ttn > 5, CompanyTrip.cost_rub > 1000).all())
    scored = []
    for t in rows:
        rate = (t.cost_rub / t.weight_ttn) / (t.km / 100)
        scored.append((rate, t))
    if len(scored) < 8:
        return []
    scored.sort(key=lambda x: x[0])
    p75 = scored[int(len(scored) * 0.75)][0]
    out = []
    for rate, t in reversed(scored):
        if rate <= p75 or len(out) >= limit:
            break
        out.append({"date": t.load_date.isoformat() if t.load_date else "",
                    "from": t.from_place, "to": t.recipient, "km": t.km,
                    "weight_t": t.weight_ttn, "cost_rub": t.cost_rub,
                    "rate_per_100km": round(rate), "p75": round(p75),
                    "delivery": t.delivery_kind, "vehicle": t.vehicle})
    return out


# ── Продажи ──────────────────────────────────────────────────────
def sales_monthly(db: Session, item_prefix: str = "Лом 3А", months: int = 18) -> list[dict]:
    rows = (db.query(CompanySale)
            .filter(CompanySale.item.like(f"{item_prefix}%"),
                    CompanySale.qty_t > 0, CompanySale.revenue_rub > 0).all())
    agg: dict[str, list] = {}
    for r in rows:
        key = r.period.strftime("%Y-%m")
        a = agg.setdefault(key, [0.0, 0.0, 0.0])
        a[0] += r.qty_t
        a[1] += r.revenue_rub
        a[2] += r.cost_rub or 0
    out = [{"month": m, "qty_t": round(q), "revenue_rub": round(v),
            "avg_price": round(v / q) if q else 0, "cost_rub": round(c)}
           for m, (q, v, c) in sorted(agg.items())]
    return out[-months:]


def sales_by_buyer(db: Session, group: str = "chermet", months: int = 12) -> list[dict]:
    since = date.today().replace(day=1) - timedelta(days=31 * months)
    rows = (db.query(CompanySale)
            .filter(CompanySale.item_group == group,
                    CompanySale.period >= since,
                    CompanySale.qty_t > 0, CompanySale.revenue_rub > 0).all())
    agg: dict[str, list] = {}
    for r in rows:
        buyer = (r.buyer or "").strip() or "(не указан)"
        a = agg.setdefault(buyer, [0.0, 0.0])
        a[0] += r.qty_t
        a[1] += r.revenue_rub
    out = [{"buyer": b, "qty_t": round(q), "revenue_rub": round(v),
            "avg_price": round(v / q) if q else 0}
           for b, (q, v) in agg.items()]
    out.sort(key=lambda x: -x["revenue_rub"])
    return out


# ── Себестоимость из факта продаж ────────────────────────────────
def avg_cost_per_ton(db: Session, group: str = "chermet", months: int = 6) -> dict | None:
    """Медианная фактическая себестоимость ₽/т по недавним продажам группы.

    В выгрузке встречаются отрицательные/нулевые и аномальные значения
    (перераспределения) — берём медиану по очищенным строкам, а не среднее.
    """
    since = date.today().replace(day=1) - timedelta(days=31 * months)
    lo, hi = (1000, 35000) if group == "chermet" else (1000, 2_000_000)
    rows = (db.query(CompanySale.qty_t, CompanySale.cost_rub)
            .filter(CompanySale.item_group == group, CompanySale.period >= since,
                    CompanySale.qty_t > 1).all())
    costs = sorted(c / q for q, c in rows if q and lo < (c / q) < hi)
    if len(costs) < 10:
        return None
    total_q = sum(q for q, c in rows if q)
    return {"cost_per_t": round(costs[len(costs) // 2]), "qty_t": round(total_q),
            "months": months,
            "source": "1С, медиана себестоимости фактических продаж (очищенная)"}


def sales_vs_plants(db: Session, months: int = 12) -> list[dict]:
    """Продажи чермета по контрагентам: регион, наша цена vs цена завода (MMI/рынок).

    Контрагент маппится на завод справочника (та же логика, что в ЖД-базе),
    рыночная цена — свежая котировка plant_<ID> (MMI CPT с ЖДТ).
    """
    from app.importers.rail_db import map_plant
    from app.services.market import latest_quote
    since = date.today().replace(day=1) - timedelta(days=31 * months)
    rows = (db.query(CompanySale)
            .filter(CompanySale.item_group == "chermet", CompanySale.period >= since,
                    CompanySale.qty_t > 0, CompanySale.revenue_rub > 0).all())
    recent_cut = date.today() - timedelta(days=75)
    agg: dict[tuple, list] = {}
    for r in rows:
        buyer = (r.buyer or "").strip() or "(не указан)"
        region = place_to_region(r.division or "") or place_to_region(r.warehouse or "")
        a = agg.setdefault((buyer, region), [0.0, 0.0, 0.0, 0.0])
        a[0] += r.qty_t
        a[1] += r.revenue_rub
        if r.period >= recent_cut:
            a[2] += r.qty_t
            a[3] += r.revenue_rub
    out = []
    for (buyer, region), (qty, rev, rq, rrev) in agg.items():
        if qty < 20:
            continue
        pid = map_plant(buyer, "")
        market = latest_quote(db, f"plant_{pid}", basis="CPT_RD") if pid else None
        our_price = round(rev / qty)
        our_recent = round(rrev / rq) if rq > 5 else None
        cmp_price = our_recent or our_price
        row = {"buyer": buyer, "region": region, "qty_t": round(qty),
               "our_price": our_price, "our_recent_price": our_recent,
               "plant_id": pid or None,
               "market_price": round(market.value) if market else None,
               "market_source": market.source if market else None,
               "delta": round(cmp_price - market.value) if market else None,
               "delta_note": "к свежей нашей цене" if our_recent else "к средней за период"}
        out.append(row)
    out.sort(key=lambda x: -x["qty_t"])
    return out


# ── Остатки и маржа ──────────────────────────────────────────────
def stock_by_warehouse(db: Session, group: str = "chermet") -> list[dict]:
    """СЫРАЯ оценка: сумма приход−расход по регистру оборотов. Требует сверки —
    регистр не содержит начальных остатков, цифры могут быть завышены."""
    rows = (db.query(CompanyStock.warehouse,
                     func.sum(CompanyStock.qty).label("qty"),
                     func.sum(CompanyStock.value_rub).label("val"))
            .filter(CompanyStock.item_group == group)
            .group_by(CompanyStock.warehouse).all())
    out = []
    for wh, qty, val in rows:
        if qty is None or qty < 1:      # отрицательные/нулевые — артефакты выгрузки
            continue
        out.append({"warehouse": wh, "qty_t": round(qty, 1),
                    "value_rub": round(val or 0),
                    "cost_per_t": round((val or 0) / qty) if qty else 0,
                    "region": place_to_region(wh)})
    out.sort(key=lambda x: -x["qty_t"])
    return out


def data_freshness(db: Session) -> dict:
    """Даты последних записей 1С — для честных пометок на дашборде."""
    def _max(col, model, datecol):
        v = db.query(func.max(datecol)).scalar()
        return v.isoformat() if v else None
    return {
        "sales": _max(None, CompanySale, CompanySale.period),
        "trips": _max(None, CompanyTrip, CompanyTrip.load_date),
        "stock": _max(None, CompanyStock, CompanyStock.period),
    }
