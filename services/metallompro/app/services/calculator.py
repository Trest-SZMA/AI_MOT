"""Калькулятор «куда везти выгоднее»: чистая выручка = цена завода − логистика.

Цена завода: приоритет — свежий прайс из скрапера (market_quotes), затем обзвон
(поправка на регион), затем справочник (quality=cache, честно помечается).
Логистика: фактический тариф компании из 1С (медиана ₽/т/100км), иначе справочный.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import knowledge as kb
from app.services.market import latest_quote, latest_survey_prices
from app.services.company import actual_auto_rate


def _plant_price(db: Session, plant: dict, transport: str) -> tuple[float, str, str]:
    """→ (цена, источник, качество)"""
    basis = "CPT_AUTO" if transport == "AUTO" else "CPT_RD"
    q = latest_quote(db, f"plant_{plant['id']}", basis=basis)
    if q:
        return q.value, q.source, q.quality
    key = "price_cpt_auto" if transport == "AUTO" else "price_cpt_rd"
    return float(plant[key]), "справочник Rusmet 06/2025", "cache"


def best_destination(db: Session, from_region: str = "PERM",
                     metal_code: str = "A3_SCRAP", weight_ton: float = 20.0,
                     transport: str = "AUTO") -> dict:
    mult = kb.METAL_MULTIPLIER.get(metal_code, 1.0)
    dists = kb.DISTANCES.get(from_region, {})

    # Фактический тариф авто из 1С, иначе справочный
    fact = actual_auto_rate(db)
    if transport == "AUTO" and fact:
        auto_rate, logi_src = fact["median"], f"факт 1С (медиана {fact['trips']} рейсов)"
    else:
        auto_rate, logi_src = kb.LOGISTICS["AUTO_PER_100KM"], "справочный тариф"

    results = []
    for plant in kb.STEEL_PLANTS:
        pid = plant["id"]
        if pid not in dists:
            continue
        dist_km = dists[pid]
        price, price_src, price_q = _plant_price(db, plant, transport)
        price = round(price * mult)
        if transport == "AUTO":
            logistics = dist_km / 100 * auto_rate
        else:
            logistics = (max(kb.LOGISTICS["RD_MIN"],
                             dist_km / 100 * kb.LOGISTICS["RD_PER_100KM"])
                         + kb.LOGISTICS["RD_WAGON_SURCHARGE"])
            logi_src = "справочный ж/д тариф"
        net = price - logistics
        results.append({
            "plant_id": pid, "plant_name": plant["name"], "plant_city": plant["city"],
            "distance_km": dist_km,
            "plant_price_ton": price, "price_source": price_src, "price_quality": price_q,
            "logistics_per_ton": round(logistics), "logistics_source": logi_src,
            "net_per_ton": round(net),
            "net_total_rub": round(net * weight_ton),
        })
    results.sort(key=lambda x: -x["net_per_ton"])
    best = results[0] if results else None
    return {
        "from_region": from_region,
        "from_region_name": kb.TARGET_REGIONS.get(from_region, {}).get("name", from_region),
        "metal_code": metal_code, "metal_name": kb.METAL_NAMES.get(metal_code, metal_code),
        "weight_ton": weight_ton, "transport": transport,
        "logistics_rate_note": f"Авто: {round(auto_rate)} ₽/т/100км ({logi_src})"
                               if transport == "AUTO" else "Ж/д: справочный тариф",
        "best_option": best, "all_options": results,
        "summary": (f"Из {kb.TARGET_REGIONS.get(from_region, {}).get('name', from_region)} "
                    f"выгоднее всего: {best['plant_name']} ({best['plant_city']}) — "
                    f"чистыми {best['net_per_ton']:,} ₽/т".replace(",", " "))
                   if best else "Нет заводов для маршрута",
    }


def margin_today(db: Session, stock_tons: dict[str, float] | None = None,
                 cost_override: float | None = None) -> dict:
    """«Маржа сегодня»: тоннаж по регионам × (лучшая чистая цена − себестоимость).

    stock_tons: {"PERM": 340, "HMAO": 120, ...} — вводится вручную, пока
    достоверные остатки не подключены из SQL-экстрактора 1С.
    Себестоимость — фактическая из недавних продаж 1С (avg_cost_per_ton).
    Прогнозная часть: та же маржа через 3 мес по факторной модели.
    """
    from app.services.company import avg_cost_per_ton
    from app.services.forecast import make_model

    cost = avg_cost_per_ton(db)
    if cost_override and cost_override > 0:
        cost_t = cost_override
        cost = {"cost_per_t": cost_override, "source": "введено вручную",
                "qty_t": None, "months": None}
    else:
        cost_t = cost["cost_per_t"] if cost else None

    # Тоннаж: ручной ввод приоритетнее; иначе — сверенные остатки
    # из соседнего сервиса «Остатки» (:8090)
    stock_tons = {k: v for k, v in (stock_tons or {}).items() if v and v > 0}
    stock_source = "введено вручную"
    if not stock_tons:
        from app.services.neighbors import ostatki_stock
        neighbor = ostatki_stock()
        if neighbor and neighbor["regions"]:
            stock_tons = neighbor["regions"]
            stock_source = f"{neighbor['source']}, на {neighbor['actual_date']}"

    fm = make_model(db)
    # Изменение цены чёрного за 3 мес по модели (доля от базы)
    fc = fm.forecast("black_rf", 3)
    drop_share = (fc["points"][-1]["price"] - fc["base"]) / fc["base"] if fc["base"] else 0

    rows, total_margin, total_margin_3m, total_qty = [], 0.0, 0.0, 0.0
    for reg, tons in stock_tons.items():
        calc = best_destination(db, from_region=reg)
        best = calc["best_option"]
        if not best:
            continue
        net = best["net_per_ton"]
        margin = (net - cost_t) * tons if cost_t else None
        net_3m = net * (1 + drop_share)
        margin_3m = (net_3m - cost_t) * tons if cost_t else None
        rows.append({
            "region": reg, "region_name": calc["from_region_name"], "qty_t": tons,
            "best_plant": best["plant_name"], "net_per_ton": net,
            "cost_per_t": cost_t,
            "margin_rub": round(margin) if margin is not None else None,
            "net_per_ton_3m": round(net_3m),
            "margin_rub_3m": round(margin_3m) if margin_3m is not None else None,
        })
        total_qty += tons
        if margin:
            total_margin += margin
        if margin_3m:
            total_margin_3m += margin_3m
    return {
        "rows": rows, "total_qty_t": round(total_qty),
        "total_margin_rub": round(total_margin),
        "total_margin_3m_rub": round(total_margin_3m),
        "wait_cost_rub": round(total_margin - total_margin_3m),
        "cost_source": cost,
        "stock_source": stock_source,
        "price_change_3m_pct": round(drop_share * 100, 1),
        "note": ("Тоннаж: " + stock_source + ". "
                 "Себестоимость — факт продаж 1С за последние месяцы. "
                 "Прогноз чистой цены через 3 мес — факторная модель (двухфазный сценарий)."),
    }
