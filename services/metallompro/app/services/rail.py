"""Аналитика повагонной ЖД-базы: тарифы по маршрутам, вагонная экономика, конкуренты.

Логика вагонного калькулятора (постановка директора):
  Завод даёт цену CPT (вагонная) — например 28 339 ₽/т, если вагоны организуем мы.
  Варианты:
    1. «Свои вагоны»    — получаем полную CPT, минус своя вагонная себестоимость;
    2. «Вагоны завода»  — завод даёт вагоны, но цена ниже (например 25 000);
    3. «Наёмные вагоны» — частник/оператор даёт вагоны за ставку (3000–3500 ₽/т),
                          получаем полную CPT минус ставка.
  Во всех вариантах ЖД-тариф РЖД (инфраструктура+локомотив) платит грузоотправитель —
  берём ФАКТ из повагонной базы по этому маршруту.
"""
from __future__ import annotations
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import RailShipment
from app.services import knowledge as kb
from app.services.market import latest_quote

# Наш регион → как область называется в ЖД-базе
REGION_RAIL = {
    "PERM": ["Пермский край"],
    "KOMI_NORTH": ["Республика Коми"],
    "KOMI_PERM": ["Пермский край"],
    "HMAO": ["Ханты-Мансийский автономный округ - Югра",
             "Ханты-Мансийский автономный округ"],
    "SOUTH": ["Волгоградская область"],
}


def data_period(db: Session) -> dict | None:
    lo, hi = db.query(func.min(RailShipment.ship_date),
                      func.max(RailShipment.ship_date)).first()
    if not lo:
        return None
    n = db.query(func.count(RailShipment.id)).scalar()
    return {"from": lo.isoformat(), "to": hi.isoformat(), "shipments": n}


def route_tariffs(db: Session, from_region: str | None = None,
                  min_tons: float = 60) -> list[dict]:
    """Средний фактический тариф ₽/т по маршрутам (область → грузополучатель)."""
    q = (db.query(RailShipment.from_region, RailShipment.consignee,
                  RailShipment.plant_id,
                  func.sum(RailShipment.tons).label("tons"),
                  func.sum(RailShipment.tariff_rub).label("tariff"),
                  func.count().label("n"),
                  func.sum(RailShipment.wagons).label("wagons"))
         .filter(RailShipment.consignee != ""))
    if from_region:
        q = q.filter(RailShipment.from_region.in_(REGION_RAIL.get(from_region, [from_region])))
    rows = (q.group_by(RailShipment.from_region, RailShipment.consignee,
                       RailShipment.plant_id)
            .having(func.sum(RailShipment.tons) >= min_tons).all())
    out = []
    for fr, cons, pid, tons, tariff, n, wagons in rows:
        out.append({
            "from_region": fr, "consignee": cons, "plant_id": pid or None,
            "tons": round(tons), "shipments": n, "wagons": int(wagons or 0),
            "tariff_per_t": round(tariff / tons) if tons else 0,
            "avg_wagon_t": round(tons / wagons, 1) if wagons else None,
        })
    out.sort(key=lambda x: -x["tons"])
    return out


def rail_tariff_for(db: Session, from_region: str, plant_id: str) -> dict | None:
    """Фактический ЖД-тариф ₽/т для маршрута наш регион → завод."""
    regions = REGION_RAIL.get(from_region, [from_region])
    row = (db.query(func.sum(RailShipment.tons), func.sum(RailShipment.tariff_rub),
                    func.count())
           .filter(RailShipment.from_region.in_(regions),
                   RailShipment.plant_id == plant_id).first())
    tons, tariff, n = row or (0, 0, 0)
    if tons and tons > 30:
        return {"tariff_per_t": round(tariff / tons), "tons": round(tons),
                "shipments": n, "quality": "live",
                "source": f"повагонная база, {n} отпр., {round(tons):,} т".replace(",", " ")}
    # фолбэк — справочное плечо
    dist = kb.DISTANCES.get(from_region, {}).get(plant_id)
    if dist:
        est = max(kb.LOGISTICS["RD_MIN"], dist / 100 * kb.LOGISTICS["RD_PER_100KM"])
        return {"tariff_per_t": round(est), "tons": None, "shipments": 0,
                "quality": "calc", "source": f"оценка по плечу {dist} км"}
    return None


def wagon_economics(db: Session, plant_id: str, from_region: str,
                    cpt_price: float | None = None,
                    plant_wagon_price: float | None = None,
                    private_rate: float = 3250,
                    own_wagon_cost: float = 0) -> dict:
    """Сравнение трёх вагонных схем для завода и региона отправления."""
    plant = next((p for p in kb.STEEL_PLANTS if p["id"] == plant_id), None)
    plant_name = plant["name"] if plant else plant_id

    # Цена CPT (вагонная): приоритет — введённая, затем MMI/скрейп, затем справочник
    price_src = "введена вручную"
    if not cpt_price:
        q = latest_quote(db, f"plant_{plant_id}", basis="CPT_RD")
        if q:
            cpt_price, price_src = q.value, f"{q.source} ({q.quality})"
        elif plant:
            cpt_price, price_src = plant["price_cpt_rd"], "справочник"
        else:
            cpt_price, price_src = 0, "нет данных"

    rail = rail_tariff_for(db, from_region, plant_id)
    rail_t = rail["tariff_per_t"] if rail else 0

    if plant_wagon_price is None and cpt_price:
        # по умолчанию: вагонная составляющая ≈ ставке частника
        plant_wagon_price = round(cpt_price - private_rate)

    variants = [
        {"id": "own", "name": "Свои вагоны",
         "price": round(cpt_price), "wagon_cost": round(own_wagon_cost),
         "note": "полная CPT-цена; вагонная себестоимость — своя"},
        {"id": "plant", "name": "Вагоны завода",
         "price": round(plant_wagon_price or 0), "wagon_cost": 0,
         "note": "завод даёт вагоны, цена ниже на вагонную составляющую"},
        {"id": "private", "name": "Наёмные вагоны",
         "price": round(cpt_price), "wagon_cost": round(private_rate),
         "note": f"оператор/частник, ставка {round(private_rate):,} ₽/т".replace(",", " ")},
    ]
    for v in variants:
        v["rail_tariff"] = rail_t
        v["net_per_ton"] = v["price"] - rail_t - v["wagon_cost"]
    best = max(variants, key=lambda v: v["net_per_ton"])
    for v in variants:
        v["is_best"] = v["id"] == best["id"]
        v["vs_best"] = v["net_per_ton"] - best["net_per_ton"]
    return {
        "plant_id": plant_id, "plant_name": plant_name,
        "from_region": from_region,
        "from_region_name": kb.TARGET_REGIONS.get(from_region, {}).get("name", from_region),
        "cpt_price": round(cpt_price), "price_source": price_src,
        "rail": rail, "variants": variants,
        "best": best["name"],
        "summary": (f"{plant_name}: выгоднее «{best['name']}» — чистыми "
                    f"{best['net_per_ton']:,} ₽/т".replace(",", " ")),
    }


# ── Собственный индекс по методике MMI ───────────────────────────
# Кэптивные грузоотправители (дочерние структуры заводов) исключаются из
# расчёта свободного рынка — как в методике MMI («исключаются поставки от
# дочерних структур, стоп-цены, спец-поставщики»)
CAPTIVE_KEYWORDS = ["ВТОРЧЕРМЕТ НЛМК", "ВТОРМЕТ", "ПРОМСОРТ", "УГМК-ВТОРЦВЕТМЕТ",
                    "ТМК ЧЕРМЕТ", "СЕВЕРСТАЛЬ ВТОРЧЕРМЕТ", "ЕВРАЗ МЕТАЛЛ"]


def _is_captive(consignor: str) -> bool:
    c = (consignor or "").upper()
    return any(kw in c for kw in CAPTIVE_KEYWORDS)


def mmi_style_index(db: Session, region: str) -> dict | None:
    """FCA-эквивалент региона по методике MMI (раздел 2, обратный расчёт):

        FCA_региона = Σ((CPT_завода − Тариф_маршрута) × V_маршрута) / ΣV

    где CPT_завода — свежая котировка (MMI weekly), Тариф — ФАКТ из повагонной
    базы (у MMI — прейскурант 10-01 × коэффициент операторов; наш факт точнее),
    V — тонны свободного рынка (кэптивные поставки исключены).
    """
    from app.services.market import latest_quote
    regions = REGION_RAIL.get(region, [region])
    rows = (db.query(RailShipment.plant_id, RailShipment.consignor,
                     func.sum(RailShipment.tons), func.sum(RailShipment.tariff_rub))
            .filter(RailShipment.from_region.in_(regions),
                    RailShipment.plant_id != "")
            .group_by(RailShipment.plant_id, RailShipment.consignor).all())
    num = den = captive_t = 0.0
    used_plants = set()
    for pid, consignor, tons, tariff in rows:
        if not tons:
            continue
        if _is_captive(consignor):
            captive_t += tons
            continue
        q = latest_quote(db, f"plant_{pid}", basis="CPT_RD")
        if not q:
            continue
        tariff_t = tariff / tons if tons else 0
        num += (q.value - tariff_t) * tons
        den += tons
        used_plants.add(pid)
    if den < 50:
        return None
    return {
        "region": region, "fca_calc": round(num / den),
        "free_tons": round(den), "captive_tons": round(captive_t),
        "plants_used": sorted(used_plants),
        "method": "Σ((CPT завода − факт-тариф) × V) / ΣV, без кэптивных",
    }


def recompute_indices(db: Session) -> int:
    """Пересчитать наш индекс по всем регионам → market_quotes (quality=calc).
    Вызывается после каждого сбора рынка и импорта ЖД/MMI."""
    from app.services.market import save_quote
    from app.models import MarketQuote
    saved = 0
    for region in ("PERM", "KOMI_NORTH", "HMAO", "SOUTH"):
        idx = mmi_style_index(db, region)
        if not idx:
            continue
        dup = (db.query(MarketQuote)
               .filter(MarketQuote.metric == "lom3a_calc_fca",
                       MarketQuote.region == region,
                       MarketQuote.value == idx["fca_calc"]).first())
        if dup:
            continue
        save_quote(db, metric="lom3a_calc_fca", value=idx["fca_calc"],
                   source=f"Наш расчёт по методике MMI ({idx['free_tons']:,} т СР)".replace(",", " "),
                   quality="calc", region=region, basis="FCA_NO_RAIL",
                   extra=idx)
        saved += 1
    db.commit()
    return saved


def competitors(db: Session, from_region: str) -> list[dict]:
    """Кто и куда везёт лом из нашего региона (рыночная разведка)."""
    regions = REGION_RAIL.get(from_region, [from_region])
    rows = (db.query(RailShipment.consignor, RailShipment.consignee,
                     func.sum(RailShipment.tons), func.sum(RailShipment.tariff_rub),
                     func.count())
            .filter(RailShipment.from_region.in_(regions))
            .group_by(RailShipment.consignor, RailShipment.consignee)
            .having(func.sum(RailShipment.tons) > 30).all())
    out = [{"consignor": a or "(не указан)", "consignee": b or "(не указан)",
            "tons": round(t), "shipments": n,
            "tariff_per_t": round(tr / t) if t else 0}
           for a, b, t, tr, n in rows]
    out.sort(key=lambda x: -x["tons"])
    return out


def wagon_operators(db: Session, from_region: str | None = None) -> list[dict]:
    """У кого в наших регионах есть вагоны (кандидаты в «наёмные вагоны»)."""
    q = db.query(RailShipment.wagon_operator,
                 func.sum(RailShipment.tons), func.sum(RailShipment.wagons),
                 func.count())
    if from_region:
        q = q.filter(RailShipment.from_region.in_(REGION_RAIL.get(from_region, [from_region])))
    rows = (q.filter(RailShipment.wagon_operator != "")
            .group_by(RailShipment.wagon_operator)
            .having(func.sum(RailShipment.wagons) >= 5).all())
    out = [{"operator": op, "tons": round(t), "wagons": int(w or 0), "shipments": n}
           for op, t, w, n in rows]
    out.sort(key=lambda x: -x["wagons"])
    return out[:25]
