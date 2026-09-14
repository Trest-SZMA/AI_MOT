"""Все API-эндпоинты MetalLomPro 2.0."""
from __future__ import annotations

import threading
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import check_login, clear_session, require_user, set_session, current_user
from app.config import INBOX_1C, ARCHIVE_1C
from app.db import get_db
from app.models import AiOutput, ImportLog, MarketQuote, NewsItem, PhoneSurvey
from app.services import knowledge as kb
from app.services.calculator import best_destination, margin_today
from app.services.company import (actual_auto_rate, data_freshness, expensive_trips,
                                  route_rates, sales_by_buyer, sales_monthly,
                                  stock_by_warehouse)
from app.services.forecast import ForecastModel, SERIES_NAMES, make_model
from app.services.market import (latest_quote, latest_survey_prices, metric_history,
                                 quote_view, survey_history)
from app.services.radar import run_radar

router = APIRouter(prefix="/api")


# ── Авторизация ──────────────────────────────────────────────────
class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginBody):
    if not check_login(body.username, body.password):
        raise HTTPException(401, "Неверный логин или пароль")
    resp = JSONResponse({"ok": True, "user": body.username})
    set_session(resp, body.username)
    return resp


@router.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    clear_session(resp)
    return resp


@router.get("/me")
def me(request_user: str | None = Depends(current_user)):
    return {"user": request_user}


# ── Сводка ───────────────────────────────────────────────────────
# Репрезентативный рублёвый ряд для «перевода» сигнала в ₽/т
_OUTLOOK_SERIES = {"black": "black_rf", "copper": "copper_m1", "alum": "alum_scrap"}


def _outlook(fm: ForecastModel) -> dict:
    """Сигнал модели → ожидаемое изменение цены за 6 мес (в % и ₽/т)."""
    out = {}
    for metal, series in _OUTLOOK_SERIES.items():
        try:
            fc = fm.forecast(series, 6)
            base, last = fc["base"], fc["points"][-1]["price"]
            out[metal] = {
                "series_name": fc["series_name"],
                "base": base, "m6": last,
                "abs_change": last - base,
                "pct_change": round((last - base) / base * 100, 1) if base else 0,
                "base_source": fc["base_source"],
            }
        except Exception:
            out[metal] = None
    return out


@router.get("/summary")
def summary(user: str = Depends(require_user), db: Session = Depends(get_db)):
    fm = make_model(db)
    s = fm.summary()
    s["outlook"] = _outlook(fm)
    key_metrics = {}
    for metric in ("translom_index", "usd_rub", "key_rate", "hms_turkey",
                   "copper_lme", "alum_lme", "plant_MMK"):
        key_metrics[metric] = quote_view(latest_quote(db, metric))
    key_metrics["lom3a_ural"] = quote_view(
        latest_quote(db, "lom3a", region="УРАЛ", basis="CPT_RD"))
    key_metrics["lom3a_ural_fca"] = quote_view(
        latest_quote(db, "lom3a", region="УРАЛ", basis="FCA"))
    surveys = {f"{r}_{b}": {"price": p, "date": str(d)}
               for (r, b), (p, d) in latest_survey_prices(db, max_age_days=3650).items()}
    # Три источника индексов: RusLOM (ручной), MMI (еженедельный отчёт), MetallPlace (ручной)
    from app.services.matrix_svc import latest_week
    snap = latest_week(db)
    mmi_val = None
    if snap:
        perm = snap.payload.get("prices_obl", {}).get("rows", {}).get("Пермский кр.", {})
        if perm:
            last_d = max(perm)
            mmi_val = {"value": perm[last_d], "unit": "RUB/т",
                       "source": f"MMI, Пермский кр. без ЖДТ",
                       "quality": "live", "collected_at": last_d + "T00:00:00",
                       "region": "PERM", "basis": "FCA_NO_RAIL"}
    indices3 = {
        "ruslom": quote_view(latest_quote(db, "ruslom_index")),
        "mmi": mmi_val,
        "metallplace": quote_view(latest_quote(db, "metallplace_index")),
        "translom": quote_view(latest_quote(db, "lom3a", region="УРАЛ", basis="CPT_RD")),
    }
    our_index = {reg: quote_view(latest_quote(db, "lom3a_calc_fca", region=reg))
                 for reg in ("PERM", "KOMI_NORTH", "HMAO", "SOUTH")}
    return {
        "signals": s["signals"], "strategy": s["strategy"], "two_phase": s["two_phase"],
        "outlook": s["outlook"], "indices3": indices3, "our_index": our_index,
        "model_date": s["model_date"], "key_metrics": key_metrics,
        "survey_prices": surveys, "company_freshness": data_freshness(db),
        "regions": kb.TARGET_REGIONS,
    }


# ── Радар ────────────────────────────────────────────────────────
@router.get("/radar")
def radar(user: str = Depends(require_user), db: Session = Depends(get_db)):
    return {"checked_at": datetime.utcnow().isoformat(), "checks": run_radar(db)}


# ── Калькулятор и маржа ──────────────────────────────────────────
@router.get("/calculator")
def calculator(from_region: str = "PERM", metal_code: str = "A3_SCRAP",
               weight_ton: float = 20.0, transport: str = "AUTO",
               user: str = Depends(require_user), db: Session = Depends(get_db)):
    return best_destination(db, from_region, metal_code, weight_ton, transport)


@router.get("/margin_today")
def api_margin(perm: float = 0, komi: float = 0, komi_north: float = 0,
               hmao: float = 0, south: float = 0, cost: float = 0,
               user: str = Depends(require_user), db: Session = Depends(get_db)):
    tons = {"PERM": perm, "KOMI_PERM": komi, "KOMI_NORTH": komi_north,
            "HMAO": hmao, "SOUTH": south}
    return margin_today(db, tons, cost_override=cost or None)


# ── Прогноз ──────────────────────────────────────────────────────
@router.get("/forecast/series")
def forecast_series(user: str = Depends(require_user), db: Session = Depends(get_db)):
    fm = make_model(db)
    return {"series": [{"id": s, "name": SERIES_NAMES.get(s, s)} for s in fm.all_series()],
            "factors": {m: fm.factors(m) for m in ("black", "copper", "alum")},
            "signals": {m: fm.signal(m) for m in ("black", "copper", "alum")}}


@router.get("/forecast")
def forecast(series: str = "perm_cpt", months: int = 6,
             user: str = Depends(require_user), db: Session = Depends(get_db)):
    fm = make_model(db)
    try:
        return fm.forecast(series, months)
    except KeyError as e:
        raise HTTPException(404, str(e))


# ── Рынок ────────────────────────────────────────────────────────
@router.get("/market/history")
def market_history(metric: str, days: int = 120, region: str | None = None,
                   user: str = Depends(require_user), db: Session = Depends(get_db)):
    return {"metric": metric, "points": metric_history(db, metric, days, region)}


@router.get("/market/plants")
def market_plants(user: str = Depends(require_user), db: Session = Depends(get_db)):
    out = []
    for p in kb.STEEL_PLANTS:
        live = quote_view(latest_quote(db, f"plant_{p['id']}"))
        out.append({**p, "live": live})
    return out


@router.get("/news")
def news(limit: int = 40, user: str = Depends(require_user), db: Session = Depends(get_db)):
    rows = (db.query(NewsItem).order_by(NewsItem.collected_at.desc()).limit(limit).all())
    return [{"source": n.source, "title": n.title, "url": n.url,
             "collected_at": n.collected_at.isoformat(),
             "ai_impact": n.ai_impact, "ai_direction": n.ai_direction,
             "ai_region": n.ai_region} for n in rows]


# ── Данные компании ──────────────────────────────────────────────
@router.get("/sales/monthly")
def api_sales_monthly(item_prefix: str = "Лом 3А",
                      user: str = Depends(require_user), db: Session = Depends(get_db)):
    return sales_monthly(db, item_prefix)


@router.get("/sales/buyers")
def api_sales_buyers(group: str = "chermet", months: int = 12,
                     user: str = Depends(require_user), db: Session = Depends(get_db)):
    return sales_by_buyer(db, group, months)


@router.get("/sales/vs_market")
def sales_vs_market(user: str = Depends(require_user), db: Session = Depends(get_db)):
    """Наша средняя цена продажи 3А против индекса Транслом (CPT ж/д Урал)."""
    ours = sales_monthly(db, "Лом 3А")
    index = latest_quote(db, "lom3a", region="УРАЛ", basis="CPT_RD")
    return {"our_sales": ours, "market_index": quote_view(index),
            "note": ("Базисы различаются (наш микс FCA/CPT против CPT ж/д Урал) — "
                     "сравнивать как ориентир, не в лоб")}


@router.get("/trips/rates")
def api_trip_rates(user: str = Depends(require_user), db: Session = Depends(get_db)):
    return {"summary": actual_auto_rate(db), "routes": route_rates(db)[:40]}


@router.get("/trips/expensive")
def api_trips_expensive(days: int = 90, user: str = Depends(require_user),
                        db: Session = Depends(get_db)):
    return expensive_trips(db, days)


@router.get("/stock")
def api_stock(group: str = "chermet", user: str = Depends(require_user),
              db: Session = Depends(get_db)):
    return stock_by_warehouse(db, group)


@router.get("/stock_neighbor")
def api_stock_neighbor(user: str = Depends(require_user)):
    """Сверенные остатки лома из соседнего сервиса «Остатки» (:8090)."""
    from app.services.neighbors import ostatki_stock
    return ostatki_stock() or {"error": "Сервис «Остатки» недоступен"}


# ── ЖД и вагоны ──────────────────────────────────────────────────
@router.get("/rail/status")
def rail_status(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.rail import data_period
    return data_period(db) or {"shipments": 0}


@router.get("/rail/routes")
def rail_routes(from_region: str | None = None,
                user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.rail import route_tariffs
    return route_tariffs(db, from_region)[:60]


@router.get("/rail/wagon_calc")
def rail_wagon_calc(plant_id: str = "SEVERSTAL_TZ", from_region: str = "PERM",
                    cpt_price: float = 0, plant_wagon_price: float = 0,
                    private_rate: float = 3250, own_wagon_cost: float = 0,
                    user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.rail import wagon_economics
    return wagon_economics(db, plant_id, from_region,
                           cpt_price or None, plant_wagon_price or None,
                           private_rate, own_wagon_cost)


@router.get("/rail/competitors")
def rail_competitors(from_region: str = "PERM",
                     user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.rail import competitors
    return competitors(db, from_region)[:30]


@router.get("/rail/operators")
def rail_operators(from_region: str | None = None,
                   user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.rail import wagon_operators
    return wagon_operators(db, from_region)


# ── Матрица связей ───────────────────────────────────────────────
@router.get("/matrix")
def api_matrix(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.matrix_svc import matrix_view
    return matrix_view(db) or {"error": "Загрузите еженедельный отчёт MMI (xlsx) на экране «Данные»"}


@router.get("/matrix/grid")
def api_matrix_grid(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.matrix_svc import matrix_grid
    return matrix_grid(db) or {"error": "Загрузите еженедельный отчёт MMI (xlsx) на экране «Данные»"}


@router.get("/matrix/plant")
def api_matrix_plant(name: str, user: str = Depends(require_user),
                     db: Session = Depends(get_db)):
    from app.services.matrix_svc import plant_dossier
    return plant_dossier(db, name) or {"error": "Нет данных по заводу"}


@router.get("/matrix/multi")
def api_matrix_multi(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.matrix_svc import matrix_multi
    return matrix_multi(db) or {"error": "Загрузите еженедельный отчёт MMI (xlsx)"}


@router.get("/market/factors")
def api_market_factors(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.matrix_svc import market_factors
    return market_factors(db)


@router.get("/forecast/scenarios")
def api_forecast_scenarios(series: str = "black_rf", months: int = 6,
                           user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.forecast import scenarios
    try:
        return scenarios(db, series, months)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.get("/ai/influence_web")
def ai_web_latest(user: str = Depends(require_user), db: Session = Depends(get_db)):
    import json as _json
    row = (db.query(AiOutput).filter(AiOutput.kind == "web")
           .order_by(AiOutput.created_at.desc()).first())
    if not row:
        return {"web": None}
    return {"web": _json.loads(row.content), "created_at": row.created_at.isoformat()}


@router.post("/ai/influence_web")
def ai_web_generate(user: str = Depends(require_user)):
    from app.services.ai import build_influence_web, llm_available
    if not llm_available():
        raise HTTPException(503, "ИИ не подключён")
    data = build_influence_web()
    if not data:
        raise HTTPException(502, "ИИ не вернул валидную паутину — попробуйте ещё раз")
    return {"web": data}


@router.get("/matrix/plants_registry")
def api_plants_registry(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.matrix_svc import plants_registry
    return plants_registry(db)


@router.get("/matrix/suppliers")
def api_suppliers(from_region: str | None = None,
                  user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.matrix_svc import suppliers_registry
    return suppliers_registry(db, from_region)[:60]


@router.get("/matrix/demand_trend")
def api_demand_trend(plant: str, user: str = Depends(require_user),
                     db: Session = Depends(get_db)):
    from app.services.matrix_svc import demand_trend
    return demand_trend(db, plant)


# ── Продажи vs заводы ────────────────────────────────────────────
@router.get("/sales/vs_plants")
def api_sales_vs_plants(months: int = 12, user: str = Depends(require_user),
                        db: Session = Depends(get_db)):
    from app.services.company import sales_vs_plants
    return sales_vs_plants(db, months)


# ── ИИ-ревизия модели ────────────────────────────────────────────
@router.post("/ai/factors_review")
def ai_factors_review(user: str = Depends(require_user)):
    from app.services.ai import review_factors, llm_available
    if not llm_available():
        raise HTTPException(503, "ИИ не подключён")
    res = review_factors()
    if not res:
        raise HTTPException(502, "ИИ не ответил")
    return res


@router.get("/ai/factors_review")
def ai_factors_latest(user: str = Depends(require_user), db: Session = Depends(get_db)):
    row = (db.query(AiOutput).filter(AiOutput.kind == "factors")
           .order_by(AiOutput.created_at.desc()).first())
    return {"content": row.content if row else None,
            "created_at": row.created_at.isoformat() if row else None}


# ── Краулер ломоприёмок ──────────────────────────────────────────
_crawl_lock = threading.Lock()


@router.get("/crawler/status")
def crawler_status_api(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.crawler import crawler_status, seed_sources
    seed_sources(db)
    return crawler_status(db)


@router.get("/crawler/sources")
def crawler_sources(user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.models import ScrapeSource
    rows = (db.query(ScrapeSource).order_by(ScrapeSource.last_ok.desc().nullslast())
            .limit(300).all())
    return [{"name": s.name, "url": s.url, "region": s.region, "city": s.city,
             "kind": s.kind, "added_by": s.added_by, "enabled": s.enabled,
             "last_ok": s.last_ok.isoformat() if s.last_ok else None,
             "last_price": s.last_price, "last_method": s.last_method,
             "fail_count": s.fail_count, "note": s.note} for s in rows]


@router.post("/crawler/run")
def crawler_run(user: str = Depends(require_user)):
    from app.services.crawler import crawl_all
    from app.db import session_scope
    if not _crawl_lock.acquire(blocking=False):
        return {"ok": False, "message": "Обход уже идёт"}

    def _run():
        try:
            with session_scope() as db:
                crawl_all(db)
        finally:
            _crawl_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Обход запущен в фоне (до 5-10 мин)"}


class DiscoverBody(BaseModel):
    region: str = "PERM"


@router.post("/crawler/discover")
def crawler_discover(body: DiscoverBody, user: str = Depends(require_user),
                     db: Session = Depends(get_db)):
    from app.services.crawler import discover_sources
    return discover_sources(db, body.region)


class AddSourceBody(BaseModel):
    url: str
    name: str = ""
    region: str = ""
    city: str = ""


@router.post("/crawler/add")
def crawler_add(body: AddSourceBody, user: str = Depends(require_user),
                db: Session = Depends(get_db)):
    from app.models import ScrapeSource
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Нужен полный URL с http(s)://")
    if db.query(ScrapeSource).filter(ScrapeSource.url == url).first():
        return {"ok": False, "message": "Уже в реестре"}
    db.add(ScrapeSource(name=body.name or url, url=url, region=body.region,
                        city=body.city, added_by="manual"))
    db.commit()
    return {"ok": True}


@router.get("/market/local_prices")
def local_prices(user: str = Depends(require_user), db: Session = Depends(get_db)):
    """Свежие цены приёмок по регионам (из обхода)."""
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=14)
    rows = (db.query(MarketQuote)
            .filter(MarketQuote.metric.in_(["lom3a_local", "copper_local", "alum_local"]),
                    MarketQuote.collected_at >= since)
            .order_by(MarketQuote.collected_at.desc()).all())
    seen, out = set(), []
    for r in rows:
        key = (r.source, r.metric)
        if key in seen:
            continue
        seen.add(key)
        out.append({"metric": r.metric, "value": r.value, "source": r.source,
                    "region": r.region, "city": r.city,
                    "date": r.collected_at.date().isoformat(),
                    "via": (r.extra or {}).get("via", "")})
    return out


# ── Обзвон ───────────────────────────────────────────────────────
class SurveyEntry(BaseModel):
    region: str
    basis: str
    price: float
    comment: str = ""


class SurveyBody(BaseModel):
    survey_date: date
    entries: list[SurveyEntry]


@router.post("/survey")
def save_survey(body: SurveyBody, user: str = Depends(require_user),
                db: Session = Depends(get_db)):
    for e in body.entries:
        if e.price <= 0:
            continue
        db.add(PhoneSurvey(survey_date=body.survey_date, region=e.region, basis=e.basis,
                           price=e.price, comment=e.comment, author=user))
    db.commit()
    return {"ok": True, "saved": len(body.entries)}


class ManualQuote(BaseModel):
    metric: str          # plant_MMK | wagon_rate | armatura_a500
    value: float
    comment: str = ""


ALLOWED_MANUAL = {"plant_MMK": ("ММК ВторМет (ручной ввод)", "CPT_AUTO", "RUB/т"),
                  "wagon_rate": ("ИПЕМ (ручной ввод)", "", "RUB/сут"),
                  "armatura_a500": ("Ручной ввод", "", "RUB/т"),
                  "ruslom_index": ("RusLOM / rusmet.ru (ручной ввод)", "INDEX", "RUB/т"),
                  "metallplace_index": ("MetallPlace (ручной ввод)", "INDEX", "RUB/т")}


@router.post("/manual_quote")
def manual_quote(body: ManualQuote, user: str = Depends(require_user),
                 db: Session = Depends(get_db)):
    if body.metric not in ALLOWED_MANUAL or body.value <= 0:
        raise HTTPException(400, "Недопустимая метрика или значение")
    from app.services.market import save_quote
    src, basis, unit = ALLOWED_MANUAL[body.metric]
    save_quote(db, metric=body.metric, value=body.value, source=src, quality="manual",
               basis=basis, unit=unit)
    db.commit()
    return {"ok": True}


@router.get("/survey/history")
def api_survey_history(region: str = "PERM", basis: str = "CPT_AUTO",
                       user: str = Depends(require_user), db: Session = Depends(get_db)):
    return survey_history(db, region, basis)


# ── Загрузка 1С ──────────────────────────────────────────────────
@router.post("/upload_1c")
async def upload_1c(files: list[UploadFile] = File(...),
                    user: str = Depends(require_user), db: Session = Depends(get_db)):
    from app.importers.csv_1c import import_any
    results = []
    for f in files:
        dest = INBOX_1C / f.filename
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        entry = import_any(db, dest)
        results.append({"file": f.filename, "kind": entry.kind, "status": entry.status,
                        "loaded": entry.rows_loaded, "skipped": entry.rows_skipped,
                        "message": entry.message})
        try:
            dest.rename(ARCHIVE_1C / f"{datetime.now():%Y%m%d_%H%M%S}_{f.filename}")
        except OSError:
            pass
    return {"results": results}


@router.get("/imports")
def imports_log(user: str = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.query(ImportLog).order_by(ImportLog.created_at.desc()).limit(50).all()
    return [{"file": r.filename, "kind": r.kind, "status": r.status,
             "loaded": r.rows_loaded, "skipped": r.rows_skipped,
             "message": r.message, "at": r.created_at.isoformat()} for r in rows]


# ── Сбор данных вручную ──────────────────────────────────────────
_collect_lock = threading.Lock()


@router.post("/collect")
def trigger_collect(user: str = Depends(require_user)):
    from app.scrapers.runner import collect_market
    if not _collect_lock.acquire(blocking=False):
        return {"ok": False, "message": "Сбор уже идёт"}

    def _run():
        try:
            collect_market()
        finally:
            _collect_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Сбор запущен в фоне (~1-2 мин)"}


# ── ИИ ───────────────────────────────────────────────────────────
class ChatBody(BaseModel):
    question: str


@router.get("/ai/status")
def ai_status(user: str = Depends(require_user)):
    from app.services.ai import llm_available, llm_name
    return {"available": llm_available(), "provider": llm_name()}


@router.get("/ai/briefing")
def ai_briefing_latest(user: str = Depends(require_user), db: Session = Depends(get_db)):
    row = (db.query(AiOutput).filter(AiOutput.kind == "briefing")
           .order_by(AiOutput.created_at.desc()).first())
    if not row:
        return {"content": None}
    return {"content": row.content, "model": row.model,
            "created_at": row.created_at.isoformat()}


@router.post("/ai/briefing/generate")
def ai_briefing_generate(user: str = Depends(require_user)):
    from app.services.ai import generate_briefing, llm_available
    if not llm_available():
        raise HTTPException(503, "ИИ не подключён: нет ключа в .env")
    content = generate_briefing()
    if not content:
        raise HTTPException(502, "ИИ не ответил, попробуйте позже")
    return {"content": content}


@router.post("/ai/chat")
def ai_chat(body: ChatBody, user: str = Depends(require_user)):
    from app.services.ai import answer_question, llm_available
    if not llm_available():
        raise HTTPException(503, "ИИ не подключён: нет ключа в .env")
    answer = answer_question(body.question.strip()[:2000])
    if not answer:
        raise HTTPException(502, "ИИ не ответил, попробуйте позже")
    return {"answer": answer}


@router.get("/ai/history")
def ai_history(user: str = Depends(require_user), db: Session = Depends(get_db)):
    rows = (db.query(AiOutput).filter(AiOutput.kind.in_(["chat", "briefing"]))
            .order_by(AiOutput.created_at.desc()).limit(20).all())
    return [{"kind": r.kind, "question": r.question, "content": r.content,
             "at": r.created_at.isoformat()} for r in rows]


# ── Статус ───────────────────────────────────────────────────────
@router.get("/status")
def status(db: Session = Depends(get_db)):
    from app.models import MarketQuote
    from app.services.ai import llm_available, llm_name
    n = db.query(MarketQuote).count()
    last = db.query(MarketQuote).order_by(MarketQuote.collected_at.desc()).first()
    return {"ok": True, "quotes": n,
            "last_collect": last.collected_at.isoformat() if last else None,
            "llm": {"available": llm_available(), "provider": llm_name()}}
