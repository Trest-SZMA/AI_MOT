"""MetalLomPro 2.0 — приложение FastAPI + фоновый планировщик."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import (ARCHIVE_1C, BRIEFING_HOUR, INBOX_1C, ROOT,
                        SCRAPE_INTERVAL_H)
from app.db import init_db, session_scope
from app.routers.api import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("main")

app = FastAPI(title="MetalLomPro 2.0", docs_url="/api/docs", redoc_url=None)
app.include_router(router)

FRONTEND = ROOT / "frontend"


class NoCacheStatic(StaticFiles):
    """Статика с обязательной ревалидацией: браузер всегда проверяет свежесть
    (иначе после деплоя у пользователей остаются старые app.js и вкладки «пустеют»)."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if (FRONTEND / "static").is_dir():
    app.mount("/static", NoCacheStatic(directory=str(FRONTEND / "static")), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    fp = FRONTEND / "index.html"
    if fp.exists():
        return FileResponse(str(fp), headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h2>MetalLomPro 2.0 — <a href='/api/docs'>API</a></h2>")


# ── Первичное наполнение из seed (однократно) ────────────────────
def seed_initial_data():
    from app.models import MarketQuote, PhoneSurvey
    from app.services.market import save_quote
    seed_dir = ROOT / "seed"
    with session_scope() as db:
        if db.query(MarketQuote).count() > 0:
            return
        cp = json.loads((seed_dir / "current_prices.json").read_text(encoding="utf-8"))
        seeded_at = datetime(2026, 7, 22, 12, 0)
        src = "Обзвон/Транслом 22.07.2026 (сид)"
        idx = cp.get("indices", {})
        pairs = [
            ("translom_index", idx.get("translom_index"), "РФ", "INDEX", "RUB/т"),
            ("lom3a", idx.get("fca_ekb"), "УРАЛ", "FCA", "RUB/т"),
            ("lom3a", idx.get("cpt_rd_ural"), "УРАЛ", "CPT_RD", "RUB/т"),
            ("lom3a", idx.get("cpt_rd_south"), "ЮГ", "CPT_RD", "RUB/т"),
            ("hms_turkey", idx.get("hms_cfr_turkey_usd"), "", "INDEX", "USD/т"),
            ("fob_black_sea_usd", idx.get("fob_black_sea_usd"), "", "INDEX", "USD/т"),
            ("copper_lme", cp.get("lme", {}).get("copper_usd"), "", "EXCH", "USD/т"),
            ("alum_lme", cp.get("lme", {}).get("alum_usd"), "", "EXCH", "USD/т"),
            ("usd_rub", cp.get("macro", {}).get("usd_rub"), "", "", "RUB"),
            ("key_rate", cp.get("macro", {}).get("key_rate_pct"), "", "", "%"),
            ("copper_m1_rf", cp.get("colored_scrap_rf", {}).get("copper_m1"), "РФ", "INDEX", "RUB/т"),
        ]
        for metric, value, region, basis, unit in pairs:
            if value:
                q_kwargs = dict(metric=metric, value=float(value), source=src,
                                quality="cache", region=region, basis=basis, unit=unit)
                save_quote(db, **q_kwargs)
        # Двигаем дату сид-котировок на реальную дату обзвона
        db.flush()
        for q in db.query(MarketQuote).all():
            q.collected_at = seeded_at
        # Обзвон 22.07.2026 → phone_survey
        for region, prices in cp.get("user_prices_3a", {}).items():
            for basis_key, basis in [("fca", "FCA"), ("cpt_auto", "CPT_AUTO")]:
                v = prices.get(basis_key)
                if v:
                    db.add(PhoneSurvey(survey_date=date(2026, 7, 22), region=region,
                                       basis=basis, price=float(v),
                                       comment=prices.get("note", "")[:500],
                                       author="seed"))
        log.info("Сид-данные загружены (обзвон и индексы от 22.07.2026)")


# ── Планировщик ──────────────────────────────────────────────────
def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.scrapers.runner import collect_market
    from app.importers.csv_1c import scan_inbox

    sched = BackgroundScheduler(timezone="Asia/Yekaterinburg")

    sched.add_job(collect_market, "interval", hours=SCRAPE_INTERVAL_H,
                  next_run_time=datetime.now(), id="collect",
                  max_instances=1, coalesce=True)

    def _inbox_job():
        # Сначала подхватываем свежие CSV соседа-«Реализации» (MS SQL Extractor → её data/)
        try:
            from app.services.neighbors import pull_neighbor_csv
            pull_neighbor_csv(INBOX_1C)
        except Exception as e:
            log.info("Сосед «Реализация» не подхвачен: %s", e)
        with session_scope() as db:
            res = scan_inbox(db, INBOX_1C, ARCHIVE_1C)
            if res:
                log.info("Watch-папка 1С: %s", res)

    sched.add_job(_inbox_job, "interval", minutes=10, id="inbox_1c",
                  max_instances=1, coalesce=True)

    def _briefing_job():
        from app.services.ai import generate_briefing, llm_available
        if llm_available():
            generate_briefing()

    sched.add_job(_briefing_job, "cron", hour=BRIEFING_HOUR, minute=30, id="briefing")

    def _crawl_job():
        from app.services.crawler import crawl_all
        with session_scope() as db:
            crawl_all(db)

    # Обход сайтов приёмок: каждые 6 ч со сдвигом 30 мин от сбора рынка
    sched.add_job(_crawl_job, "interval", hours=SCRAPE_INTERVAL_H,
                  id="crawler", max_instances=1, coalesce=True,
                  next_run_time=datetime.now() + timedelta(minutes=30))

    def _cleanup_job():
        from app.importers.csv_1c import cleanup_archive
        cleanup_archive(ARCHIVE_1C)

    # Чистка архива выгрузок — каждую ночь
    sched.add_job(_cleanup_job, "cron", hour=3, minute=15, id="cleanup")
    sched.start()
    log.info("Планировщик запущен: сбор каждые %sч, watch-папка 10 мин, брифинг в %d:30",
             SCRAPE_INTERVAL_H, BRIEFING_HOUR)
    return sched


@app.on_event("startup")
def on_startup():
    init_db()
    seed_initial_data()
    start_scheduler()
