"""«Матрица связей»: потоки лома завод×область из еженедельника MMI +
спрос по заводам + позиция наших регионов."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import WeeklySnapshot

OUR_OBL = {
    "Пермский кр.": "PERM", "респ. Коми": "KOMI_NORTH",
    "Ханты-Мансийский авт. окр.": "HMAO", "Волгоградская обл.": "SOUTH",
}


def latest_week(db: Session) -> WeeklySnapshot | None:
    return (db.query(WeeklySnapshot).filter(WeeklySnapshot.kind == "mmi_week")
            .order_by(WeeklySnapshot.week_start.desc()).first())


def weeks_list(db: Session) -> list[str]:
    rows = (db.query(WeeklySnapshot.week_start)
            .filter(WeeklySnapshot.kind == "mmi_week")
            .order_by(WeeklySnapshot.week_start.desc()).all())
    return [r[0].isoformat() for r in rows]


def matrix_view(db: Session) -> dict | None:
    snap = latest_week(db)
    if not snap:
        return None
    matrix = snap.payload.get("matrix", {})
    ship = snap.payload.get("shipments_total", {"rows": {}})
    ship_free = snap.payload.get("shipments_free", {"rows": {}})

    # Топ-потоки завод←область
    flows = []
    for plant, regions in matrix.items():
        for reg, tons in regions.items():
            flows.append({"plant": plant, "region": reg, "tons": tons})
    flows.sort(key=lambda x: -x["tons"])

    # Наши регионы: куда уходит лом
    ours = {}
    for obl, code in OUR_OBL.items():
        lst = [f for f in flows if f["region"] == obl]
        total = sum(f["tons"] for f in lst)
        ours[code] = {"obl": obl, "total_t": round(total),
                      "flows": lst[:8]}

    # Спрос по заводам: суммарные отгрузки за неделю + доля свободного рынка
    demand = []
    for plant, series in ship.get("rows", {}).items():
        if plant in ("УФО", "ЦФО", "СФО", "ЮФО", "Итого", "Прочие"):
            continue
        total = sum(series.values())
        free = sum(ship_free.get("rows", {}).get(plant, {}).values())
        if total > 0:
            demand.append({"plant": plant, "week_tons": round(total),
                           "free_market_tons": round(free),
                           "free_share_pct": round(free / total * 100) if total else 0})
    demand.sort(key=lambda x: -x["week_tons"])

    return {
        "week_start": snap.week_start.isoformat(),
        "source": snap.source,
        "top_flows": flows[:40],
        "our_regions": ours,
        "plant_demand": demand,
        "weeks_available": weeks_list(db),
    }


def matrix_grid(db: Session, max_plants: int = 16, max_regions: int = 12) -> dict | None:
    """Матрица завод × область (тонны за неделю) для heatmap.

    Наши области всегда включены; остальные — топ по объёму.
    Строка завода дополняется CPT-ценой (методика MMI: средневзвешенная с ЖДТ)
    и недельным спросом с долей свободного рынка.
    """
    snap = latest_week(db)
    if not snap:
        return None
    matrix = snap.payload.get("matrix", {})
    prices = snap.payload.get("prices_plant", {"rows": {}})["rows"]
    ship = snap.payload.get("shipments_total", {"rows": {}})["rows"]
    ship_free = snap.payload.get("shipments_free", {"rows": {}})["rows"]

    # Область → суммарный объём (для выбора колонок)
    reg_tons: dict[str, float] = {}
    for plant, regions in matrix.items():
        for reg, tons in regions.items():
            reg_tons[reg] = reg_tons.get(reg, 0) + tons
    our = list(OUR_OBL.keys())
    top_regions = [r for r, _ in sorted(reg_tons.items(), key=lambda x: -x[1])
                   if r not in our][:max_regions - len(our)]
    columns = our + top_regions

    # Заводы — топ по объёму
    plant_tons = {p: sum(r.values()) for p, r in matrix.items()}
    plants = [p for p, _ in sorted(plant_tons.items(), key=lambda x: -x[1])][:max_plants]

    rows = []
    max_cell = 1.0
    for p in plants:
        series = prices.get(p, {})
        cpt = series[max(series)] if series else None
        total = sum(ship.get(p, {}).values()) or plant_tons.get(p, 0)
        free = sum(ship_free.get(p, {}).values())
        cells = []
        for reg in columns:
            v = matrix.get(p, {}).get(reg, 0)
            max_cell = max(max_cell, v)
            cells.append(round(v))
        rows.append({"plant": p, "cpt": round(cpt) if cpt else None,
                     "week_tons": round(total),
                     "free_share_pct": round(free / total * 100) if total else None,
                     "cells": cells})
    return {
        "week_start": snap.week_start.isoformat(),
        "columns": columns, "our_columns": our,
        "rows": rows, "max_cell": round(max_cell),
        "method": ("Ячейка — тонны ЖД-отгрузок за неделю (MMI). CPT — средневзвешенная "
                   "цена с ЖДТ по методике MMI. СР — доля свободного рынка."),
    }


# Нормализация названий областей ЖД-базы → формат MMI («Пермский край»→«Пермский кр.»)
def _norm_obl(name: str) -> str:
    n = (name or "").strip()
    n = n.replace("автономный округ - Югра", "авт. окр.").replace("автономный округ", "авт. окр.")
    n = n.replace("Республика ", "респ. ")
    n = n.replace(" область", " обл.").replace(" край", " кр.")
    return n


def matrix_multi(db: Session, max_plants: int = 16, max_regions: int = 12) -> dict | None:
    """Мультиисточниковая матрица: в каждой ячейке — три слоя данных.

      mmi  — тонны ЖД-отгрузок за неделю (еженедельник MMI)
      rail — тонны из НАШЕЙ повагонной базы (весь загруженный период)
      ours — тонны наших продаж этому заводу из 1С (12 мес, по регионам компании)
    Разные источники → разная картина: MMI показывает рынок, повагонка —
    проверяемый факт, наши продажи — наше место в этих потоках.
    """
    from sqlalchemy import func
    from app.importers.mmi_weekly import MMI_PLANT_MAP
    from app.importers.rail_db import map_plant
    from app.models import CompanySale, RailShipment

    base = matrix_grid(db, max_plants, max_regions)
    if not base:
        return None
    columns = base["columns"]
    pid_by_plant = {p: MMI_PLANT_MAP.get(p, "") for p in [r["plant"] for r in base["rows"]]}
    plant_by_pid = {pid: p for p, pid in pid_by_plant.items() if pid}

    # Слой ЖД-базы: обл (норм.) → plant_id → тонны
    rail_layer: dict[tuple, float] = {}
    rail_period = None
    rows = (db.query(RailShipment.from_region, RailShipment.plant_id,
                     func.sum(RailShipment.tons),
                     func.min(RailShipment.ship_date), func.max(RailShipment.ship_date))
            .filter(RailShipment.plant_id != "")
            .group_by(RailShipment.from_region, RailShipment.plant_id).all())
    for fr, pid, tons, lo, hi in rows:
        obl = _norm_obl(fr)
        if obl in columns and pid in plant_by_pid:
            rail_layer[(plant_by_pid[pid], obl)] = round(tons)
            rail_period = (lo.isoformat(), hi.isoformat())

    # Слой наших продаж: регион компании → обл-колонка
    OUR_COL = {v: k for k, v in OUR_OBL.items()}   # obl→code
    code_to_obl = {code: obl for obl, code in OUR_OBL.items()}
    ours_layer: dict[tuple, float] = {}
    from app.services.company import place_to_region
    sales = (db.query(CompanySale.buyer, CompanySale.division, CompanySale.warehouse,
                      func.sum(CompanySale.qty_t))
             .filter(CompanySale.item_group == "chermet", CompanySale.qty_t > 0)
             .group_by(CompanySale.buyer, CompanySale.division, CompanySale.warehouse).all())
    for buyer, division, wh, qty in sales:
        pid = map_plant(buyer or "", "")
        if not pid or pid not in plant_by_pid:
            continue
        reg = place_to_region(division or "") or place_to_region(wh or "")
        obl = code_to_obl.get(reg or "")
        if obl and obl in columns:
            key = (plant_by_pid[pid], obl)
            ours_layer[key] = ours_layer.get(key, 0) + qty

    for r in base["rows"]:
        r["rail"] = [rail_layer.get((r["plant"], c)) for c in columns]
        r["ours"] = [round(v) if (v := ours_layer.get((r["plant"], c))) else None
                     for c in columns]
    base["rail_period"] = rail_period
    base["legend"] = {
        "mmi": "фон ячейки — MMI, отгрузки за неделю",
        "rail": "зелёным — наша повагонная база (факт, весь период)",
        "ours": "оранжевым — наши продажи из 1С (12 мес)",
    }
    return base


# ── Факторы рынка лома РФ (автосбор) ─────────────────────────────
def market_factors(db: Session) -> list[dict]:
    """Сводка факторов, влияющих на рынок лома РФ: макро + внешние + новости ИИ.

    direction: −1…+1 — влияние на цену лома (минус давит вниз).
    Каждый фактор — с живым значением и источником, без выдумок.
    """
    from datetime import datetime, timedelta
    from app.models import NewsItem
    from app.services.market import latest_quote

    out = []

    def add(name, quote, direction, comment, fmt_="{:,.0f}"):
        out.append({
            "name": name,
            "value": fmt_.format(quote.value).replace(",", " ") if quote else None,
            "unit": quote.unit if quote else "",
            "direction": direction, "comment": comment,
            "source": quote.source if quote else "нет данных",
            "quality": quote.quality if quote else "na",
            "date": quote.collected_at.date().isoformat() if quote else None,
        })

    q = latest_quote(db, "key_rate")
    if q:
        add("Ключевая ставка ЦБ", q, -0.4 if q.value > 12 else 0.3,
            "Высокая ставка душит стройку и оборотку ломозаготовителей"
            if q.value > 12 else "Снижение ставки оживляет стройку — бычий сигнал",
            "{:.1f}")
    q = latest_quote(db, "usd_rub")
    if q:
        add("Курс USD/RUB", q, -0.4 if q.value < 90 else 0.3,
            "Крепкий рубль запирает лом внутри РФ" if q.value < 90
            else "Слабый рубль открывает экспортное окно", "{:.1f}")
    q = latest_quote(db, "hms_turkey")
    if q:
        add("HMS 80/20 CFR Турция", q, -0.3 if q.value < 380 else 0.3,
            "Экспортный якорь снижается — давит на внутренние цены"
            if q.value < 380 else "Экспортный паритет поддерживает цену")
    fob = latest_quote(db, "fob_black_sea_usd")
    hms = latest_quote(db, "hms_turkey")
    if fob and hms:
        spread = hms.value - fob.value
        out.append({"name": "Спред CFR Турция − FOB ЧМ", "value": f"{spread:,.0f}".replace(",", " "),
                    "unit": "USD/т", "direction": 0.3 if spread < 50 else -0.1,
                    "comment": "Спред узкий — экспорт оживает" if spread < 50
                               else "Экспорт заперт, внутренний рынок сам по себе",
                    "source": "расчёт по Транслом", "quality": "calc",
                    "date": hms.collected_at.date().isoformat()})
    q = latest_quote(db, "copper_lme")
    if q:
        add("Медь LME", q, 0.3 if q.value > 12000 else 0.0,
            "Высокая медь тянет вверх весь цветмет")
    month = datetime.now().month
    out.append({"name": "Сезонность (стройсезон)", "value": None, "unit": "",
                "direction": 0.2 if 4 <= month <= 9 else -0.3,
                "comment": "Стройсезон поддерживает спрос на арматуру и лом"
                           if 4 <= month <= 9 else "Конец стройсезона: спрос падает до весны",
                "source": "календарь", "quality": "calc", "date": None})

    # Топ ИИ-новостей за 10 дней по силе влияния
    since = datetime.utcnow() - timedelta(days=10)
    news = (db.query(NewsItem)
            .filter(NewsItem.collected_at >= since, NewsItem.ai_impact != "")
            .all())
    news.sort(key=lambda n: -abs(n.ai_direction or 0))
    for n in news[:5]:
        if abs(n.ai_direction or 0) < 0.2:
            continue
        out.append({"name": "📰 " + n.title[:90], "value": None, "unit": "",
                    "direction": n.ai_direction, "comment": n.ai_impact[:160],
                    "source": f"{n.source} + ИИ-оценка", "quality": "live",
                    "date": n.collected_at.date().isoformat()})
    return out


def plants_registry(db: Session) -> list[dict]:
    """Реестр заводов: закуп лома (спрос), цены и динамика, наши продажи, netback.

    «Закуп» = ЖД-отгрузки лома НА завод (MMI). «Их продажи» (прокат) — данных нет:
    появятся, если подключим еженедельники MMI по прокату.
    """
    from sqlalchemy import func
    from app.importers.mmi_weekly import MMI_PLANT_MAP, FO_ROWS
    from app.importers.rail_db import map_plant
    from app.models import CompanySale
    from app.services.rail import rail_tariff_for

    snap = latest_week(db)
    if not snap:
        return []
    payload = snap.payload
    prices = payload.get("prices_plant", {"rows": {}})["rows"]
    ship = payload.get("shipments_total", {"rows": {}})["rows"]
    ship_free = payload.get("shipments_free", {"rows": {}})["rows"]
    matrix = payload.get("matrix", {})

    # Наши продажи по заводам одним проходом
    our: dict[str, list] = {}
    for r in (db.query(CompanySale.buyer, func.sum(CompanySale.qty_t),
                       func.sum(CompanySale.revenue_rub), func.max(CompanySale.period))
              .filter(CompanySale.item_group == "chermet", CompanySale.qty_t > 0,
                      CompanySale.revenue_rub > 0)
              .group_by(CompanySale.buyer).all()):
        pid = map_plant(r[0] or "", "")
        if pid:
            a = our.setdefault(pid, [0.0, 0.0, None])
            a[0] += r[1]
            a[1] += r[2]
            a[2] = max(a[2], r[3]) if a[2] else r[3]

    out = []
    for plant, series in prices.items():
        if plant in FO_ROWS:
            continue
        days = sorted(series)
        cpt_last = series[days[-1]] if days else None
        cpt_first = series[days[0]] if days else None
        total = sum(ship.get(plant, {}).values())
        free = sum(ship_free.get(plant, {}).values())
        pid = MMI_PLANT_MAP.get(plant, "")
        o = our.get(pid)
        supply = sorted(matrix.get(plant, {}).items(), key=lambda x: -x[1])
        nb = rail_tariff_for(db, "PERM", pid) if pid else None
        out.append({
            "plant": plant, "plant_id": pid or None,
            "cpt": round(cpt_last) if cpt_last else None,
            "cpt_change_wk": round(cpt_last - cpt_first) if cpt_last and cpt_first else None,
            "buy_week_tons": round(total),
            "free_share_pct": round(free / total * 100) if total else None,
            "top_supplier_region": supply[0][0] if supply else None,
            "our_qty_t": round(o[0]) if o else None,
            "our_price": round(o[1] / o[0]) if o else None,
            "our_last": o[2].isoformat() if o and o[2] else None,
            "netback_perm": (round(cpt_last - nb["tariff_per_t"])
                             if cpt_last and nb else None),
        })
    out.sort(key=lambda x: -(x["buy_week_tons"] or 0))
    return out


def suppliers_registry(db: Session, from_region: str | None = None,
                       min_tons: float = 100) -> list[dict]:
    """Реестр ломоприёмщиков/заготовителей — грузоотправители повагонной базы:
    кто, из каких областей, куда возит, объёмы, средний тариф, кэптив ли."""
    from sqlalchemy import func
    from app.models import RailShipment
    from app.services.rail import REGION_RAIL, _is_captive

    q = (db.query(RailShipment.consignor, RailShipment.from_region,
                  RailShipment.consignee,
                  func.sum(RailShipment.tons), func.sum(RailShipment.tariff_rub),
                  func.count(), func.sum(RailShipment.wagons))
         .filter(RailShipment.consignor != ""))
    if from_region:
        q = q.filter(RailShipment.from_region.in_(
            REGION_RAIL.get(from_region, [from_region])))
    rows = q.group_by(RailShipment.consignor, RailShipment.from_region,
                      RailShipment.consignee).all()

    agg: dict[str, dict] = {}
    for consignor, fr, consignee, tons, tariff, n, wagons in rows:
        a = agg.setdefault(consignor, {
            "consignor": consignor, "tons": 0.0, "tariff": 0.0, "shipments": 0,
            "wagons": 0, "regions": {}, "plants": {},
            "captive": _is_captive(consignor)})
        a["tons"] += tons
        a["tariff"] += tariff
        a["shipments"] += n
        a["wagons"] += int(wagons or 0)
        a["regions"][fr] = a["regions"].get(fr, 0) + tons
        a["plants"][consignee or "?"] = a["plants"].get(consignee or "?", 0) + tons
    out = []
    for a in agg.values():
        if a["tons"] < min_tons:
            continue
        top_reg = sorted(a["regions"].items(), key=lambda x: -x[1])
        top_pl = sorted(a["plants"].items(), key=lambda x: -x[1])
        out.append({
            "consignor": a["consignor"], "captive": a["captive"],
            "tons": round(a["tons"]), "shipments": a["shipments"],
            "wagons": a["wagons"],
            "tariff_per_t": round(a["tariff"] / a["tons"]) if a["tons"] else 0,
            "top_regions": [f"{r} ({round(t):,} т)".replace(",", " ")
                            for r, t in top_reg[:2]],
            "top_plants": [f"{p[:34]} ({round(t):,} т)".replace(",", " ")
                           for p, t in top_pl[:3]],
        })
    out.sort(key=lambda x: -x["tons"])
    return out


def plant_dossier(db: Session, plant: str) -> dict | None:
    """Досье завода — «мозговая» карточка: цены, спрос, поставщики, наши продажи,
    наш netback по методике MMI, документы."""
    from sqlalchemy import func
    from app.importers.mmi_weekly import MMI_PLANT_MAP
    from app.importers.rail_db import map_plant
    from app.models import CompanySale, RailShipment
    from app.services import knowledge as kb
    from app.services.rail import REGION_RAIL, rail_tariff_for, _is_captive

    snap = latest_week(db)
    payload = snap.payload if snap else {}
    prices = payload.get("prices_plant", {"rows": {}})["rows"].get(plant, {})
    ship = payload.get("shipments_total", {"rows": {}})["rows"].get(plant, {})
    ship_free = payload.get("shipments_free", {"rows": {}})["rows"].get(plant, {})
    matrix_row = payload.get("matrix", {}).get(plant, {})

    pid = MMI_PLANT_MAP.get(plant, "")
    kb_plant = next((p for p in kb.STEEL_PLANTS if p["id"] == pid), None)

    # Поставщики завода из повагонной базы (свободный рынок vs кэптив)
    suppliers = []
    if pid:
        rows = (db.query(RailShipment.consignor, RailShipment.from_region,
                         func.sum(RailShipment.tons), func.sum(RailShipment.tariff_rub))
                .filter(RailShipment.plant_id == pid)
                .group_by(RailShipment.consignor, RailShipment.from_region)
                .having(func.sum(RailShipment.tons) > 30).all())
        for consignor, fr, tons, tariff in sorted(rows, key=lambda x: -x[2])[:12]:
            suppliers.append({"consignor": consignor or "(не указан)", "from_region": fr,
                              "tons": round(tons),
                              "tariff_per_t": round(tariff / tons) if tons else 0,
                              "captive": _is_captive(consignor)})

    # Наши продажи этому заводу (по маппингу контрагентов 1С)
    our_sales = None
    if pid:
        rows = (db.query(CompanySale)
                .filter(CompanySale.item_group == "chermet",
                        CompanySale.qty_t > 0, CompanySale.revenue_rub > 0).all())
        qty = rev = 0.0
        last = None
        by_month: dict[str, list] = {}
        for r in rows:
            if map_plant(r.buyer or "", "") != pid:
                continue
            qty += r.qty_t
            rev += r.revenue_rub
            last = max(last, r.period) if last else r.period
            m = by_month.setdefault(r.period.strftime("%Y-%m"), [0.0, 0.0])
            m[0] += r.qty_t
            m[1] += r.revenue_rub
        if qty > 0:
            months = [{"month": m, "qty_t": round(q), "avg_price": round(v / q)}
                      for m, (q, v) in sorted(by_month.items())][-6:]
            our_sales = {"qty_t": round(qty), "avg_price": round(rev / qty),
                         "last_date": last.isoformat() if last else None,
                         "months": months}

    # Netback из наших регионов по методике MMI: CPT − факт-тариф
    cpt_last = prices[max(prices)] if prices else (kb_plant["price_cpt_rd"] if kb_plant else None)
    netbacks = []
    if pid and cpt_last:
        for reg in ("PERM", "KOMI_NORTH", "HMAO", "SOUTH"):
            t = rail_tariff_for(db, reg, pid)
            if t:
                netbacks.append({"region": reg,
                                 "region_name": kb.TARGET_REGIONS[reg]["name"],
                                 "tariff_per_t": t["tariff_per_t"],
                                 "tariff_quality": t["quality"],
                                 "netback": round(cpt_last - t["tariff_per_t"])})

    total = sum(ship.values())
    free = sum(ship_free.values())
    return {
        "plant": plant, "plant_id": pid or None,
        "kb": ({"city": kb_plant["city"], "region": kb_plant["region"],
                "transport": kb_plant["transport"], "notes": kb_plant["notes"],
                "price_page": kb_plant["price_page"]} if kb_plant else None),
        "price_series": [{"date": d, "price": round(v)} for d, v in sorted(prices.items())],
        "cpt_last": round(cpt_last) if cpt_last else None,
        "week_tons": round(total), "free_tons": round(free),
        "free_share_pct": round(free / total * 100) if total else None,
        "supply_regions": sorted(({"region": r, "tons": round(t)}
                                  for r, t in matrix_row.items()),
                                 key=lambda x: -x["tons"])[:10],
        "suppliers": suppliers,
        "our_sales": our_sales,
        "netbacks": netbacks,
        "docs_note": ("Договоры и спецификации подключим из 1С (справочник «Серии/Договоры» "
                      "через SQL-экстрактор) — сейчас показываются только фактические продажи."),
    }


def demand_trend(db: Session, plant: str) -> list[dict]:
    """Динамика недельных отгрузок на завод по всем загруженным неделям."""
    rows = (db.query(WeeklySnapshot).filter(WeeklySnapshot.kind == "mmi_week")
            .order_by(WeeklySnapshot.week_start.asc()).all())
    out = []
    for snap in rows:
        series = snap.payload.get("shipments_total", {}).get("rows", {}).get(plant, {})
        if series:
            out.append({"week": snap.week_start.isoformat(),
                        "tons": round(sum(series.values()))})
    return out
