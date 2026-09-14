"""Доступ к рыночным данным в БД: последние значения, история, обзвон."""
from __future__ import annotations
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import MarketQuote, PhoneSurvey


def latest_quote(db: Session, metric: str, region: str | None = None,
                 basis: str | None = None) -> MarketQuote | None:
    q = db.query(MarketQuote).filter(MarketQuote.metric == metric)
    if region:
        q = q.filter(MarketQuote.region == region)
    if basis:
        q = q.filter(MarketQuote.basis == basis)
    return q.order_by(MarketQuote.collected_at.desc()).first()


def quote_view(quote: MarketQuote | None) -> dict | None:
    if not quote:
        return None
    return {
        "value": quote.value, "unit": quote.unit, "source": quote.source,
        "quality": quote.quality, "collected_at": quote.collected_at.isoformat(),
        "region": quote.region, "basis": quote.basis,
    }


def metric_history(db: Session, metric: str, days: int = 90,
                   region: str | None = None) -> list[dict]:
    """Дневная история метрики (последнее значение за день)."""
    since = datetime.utcnow() - timedelta(days=days)
    q = (db.query(MarketQuote)
         .filter(MarketQuote.metric == metric, MarketQuote.collected_at >= since))
    if region:
        q = q.filter(MarketQuote.region == region)
    rows = q.order_by(MarketQuote.collected_at.asc()).all()
    by_day: dict[str, MarketQuote] = {}
    for r in rows:
        by_day[r.collected_at.date().isoformat()] = r
    return [{"date": d, "value": r.value, "quality": r.quality, "source": r.source}
            for d, r in sorted(by_day.items())]


def latest_survey_prices(db: Session, max_age_days: int = 45) -> dict:
    """Свежие цены обзвона: {(region, basis): (price, date)}."""
    since = date.today() - timedelta(days=max_age_days)
    rows = (db.query(PhoneSurvey)
            .filter(PhoneSurvey.survey_date >= since, PhoneSurvey.metal == "A3_SCRAP")
            .order_by(PhoneSurvey.survey_date.asc()).all())
    out: dict = {}
    for r in rows:  # asc → последняя запись перезапишет старую
        out[(r.region, r.basis)] = (r.price, r.survey_date)
    return out


def survey_history(db: Session, region: str, basis: str, limit: int = 60) -> list[dict]:
    rows = (db.query(PhoneSurvey)
            .filter(PhoneSurvey.region == region, PhoneSurvey.basis == basis)
            .order_by(PhoneSurvey.survey_date.desc()).limit(limit).all())
    return [{"date": r.survey_date.isoformat(), "price": r.price,
             "comment": r.comment} for r in reversed(rows)]


def gather_live_bases(db: Session, max_age_days: int = 21) -> dict:
    """Живые базы для рядов прогноза: {series: (value, source, date)}.

    black_rf     — медиана свежих FCA-цен Транслома по городам (среднее РФ)
    perm_cpt     — Транслом CPT ж/д Урал (прокси цены на воротах для Урала)
    perm_fca / komi_fca / hmao — цены областей без ЖДТ из еженедельника MMI
    copper/alum  — Транслом (LME и лом РФ)
    """
    since = datetime.utcnow() - timedelta(days=max_age_days)

    def _q(metric, region=None, basis=None):
        q = db.query(MarketQuote).filter(MarketQuote.metric == metric,
                                         MarketQuote.collected_at >= since)
        if region:
            q = q.filter(MarketQuote.region == region)
        if basis:
            q = q.filter(MarketQuote.basis == basis)
        return q.order_by(MarketQuote.collected_at.desc()).first()

    out: dict = {}

    # Среднее РФ: медиана свежих FCA по городам Транслома
    rows = (db.query(MarketQuote)
            .filter(MarketQuote.metric == "lom3a", MarketQuote.basis == "FCA",
                    MarketQuote.collected_at >= since)
            .order_by(MarketQuote.collected_at.desc()).all())
    by_city: dict[str, float] = {}
    for r in rows:
        by_city.setdefault(r.city or r.region, r.value)
    if by_city:
        vals = sorted(by_city.values())
        med = vals[len(vals) // 2]
        out["black_rf"] = (round(med), f"Транслом, медиана FCA {len(vals)} городов",
                           rows[0].collected_at.date().isoformat())

    pairs = [
        ("perm_cpt", _q("lom3a", region="УРАЛ", basis="CPT_RD"),
         "Транслом CPT ж/д Урал (прокси)"),
        ("perm_fca", _q("lom3a_obl", region="PERM") or _q("lom3a_calc_fca", region="PERM"),
         None),
        ("komi_fca", _q("lom3a_obl", region="KOMI_NORTH") or _q("lom3a_calc_fca", region="KOMI_NORTH"),
         None),
        ("hmao", _q("lom3a_obl", region="HMAO"), "MMI, ХМАО без ЖДТ"),
        ("copper_lme", _q("copper_lme"), None),
        ("alum_lme", _q("alum_lme"), None),
        ("copper_m1", _q("copper_scrap_rf"), "Транслом, медь 3 сорт FCA"),
        ("alum_scrap", _q("alum_scrap_rf"), "Транслом, лом алюминия FCA"),
    ]
    for series, quote, label in pairs:
        if quote:
            out[series] = (round(quote.value),
                           label or f"{quote.source} (live)",
                           quote.collected_at.date().isoformat())
    return out


def save_quote(db: Session, *, metric: str, value: float, source: str,
               quality: str = "live", region: str = "", city: str = "",
               basis: str = "", unit: str = "RUB/т", source_url: str = "",
               extra: dict | None = None) -> None:
    db.add(MarketQuote(metric=metric, value=value, source=source, quality=quality,
                       region=region, city=city, basis=basis, unit=unit,
                       source_url=source_url, extra=extra))
