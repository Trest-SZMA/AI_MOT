"""Фоновый сбор: скраперы → БД, затем радар и ИИ-разметка новостей.

Дедупликация котировок: не пишем повтор, если за последние 12 часов уже есть
запись той же метрики с тем же значением и качеством.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_

from app.db import session_scope
from app.models import MarketQuote, NewsItem
from app.services.market import save_quote
from app.scrapers import sources

log = logging.getLogger("runner")


def _save_quotes(db, quotes: list[dict]) -> int:
    saved = 0
    cutoff = datetime.utcnow() - timedelta(hours=12)
    for q in quotes:
        dup = (db.query(MarketQuote)
               .filter(and_(MarketQuote.metric == q["metric"],
                            MarketQuote.value == q["value"],
                            MarketQuote.quality == q.get("quality", "live"),
                            MarketQuote.collected_at >= cutoff))
               .first())
        if dup:
            continue
        save_quote(db, metric=q["metric"], value=q["value"], source=q["source"],
                   quality=q.get("quality", "live"), region=q.get("region", ""),
                   city=q.get("city", ""), basis=q.get("basis", ""),
                   unit=q.get("unit", "RUB/т"), source_url=q.get("source_url", ""))
        saved += 1
    return saved


def _save_news(db, items: list[dict]) -> int:
    saved = 0
    for n in items:
        if db.query(NewsItem).filter(NewsItem.title == n["title"]).first():
            continue
        db.add(NewsItem(source=n["source"], title=n["title"], url=n.get("url", "")))
        saved += 1
    return saved


def collect_market() -> dict:
    """Полный цикл сбора. Вызывается планировщиком и вручную из API."""
    stats = {}
    with session_scope() as db:
        for name, fn in [("cbr", sources.scrape_cbr),
                         ("translom", sources.scrape_translom),
                         ("plants", sources.scrape_plants),
                         ("lme", sources.scrape_lme)]:
            try:
                stats[name] = _save_quotes(db, fn())
            except Exception as e:
                log.warning("Сбор %s упал: %s", name, e)
                stats[name] = -1
        try:
            stats["news"] = _save_news(db, sources.scrape_news())
        except Exception as e:
            log.warning("Новости: %s", e)
            stats["news"] = -1

        # Наш индекс по методике MMI (пересчёт после сбора)
        try:
            from app.services.rail import recompute_indices
            recompute_indices(db)
        except Exception as e:
            log.info("Индекс MMI-style: %s", e)

        # Радар после каждого сбора
        try:
            from app.services.radar import run_radar, save_radar
            save_radar(db, run_radar(db))
            stats["radar"] = "ok"
        except Exception as e:
            log.warning("Радар: %s", e)

    # ИИ-разметка свежих новостей — отдельной сессией, не валит сбор
    try:
        from app.services.ai import annotate_fresh_news
        annotate_fresh_news(limit=15)
        stats["ai_news"] = "ok"
    except Exception as e:
        log.info("ИИ-разметка пропущена: %s", e)
    log.info("Сбор завершён: %s", stats)
    return stats
