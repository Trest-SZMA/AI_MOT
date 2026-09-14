"""Кампания подбора клиентов: собрать лиды, обосновать цену, сохранить.

Порядок стадий выбран так, чтобы результат был полезен даже когда внешние
источники недоступны: сначала считается всё, что можно посчитать по своим
данным (покупатели из истории сделок и ценовой коридор), и только потом
подключаются интернет-источники. Ключа провайдера нет — кампания всё равно
завершится с внутренними лидами и пометкой, почему внешних нет.

Сервис не заменяет отдел продаж: он не звонит, не пишет и ничего не отправляет.
Он отвечает на два вопроса, на которые у менеджера нет времени — «кому это
нужно» и «сколько за это платили» — и кладёт под каждый ответ доказательство.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy.orm import Session

from ..db.models import (Counterparty, Item, Lead, LeadCampaign, LeadEvidence,
                         MarketListing)
from ..normalize import (classify_family, company_key, extract_size_key,
                         web_url)
from . import internal, pricing, web

log = logging.getLogger(__name__)

# Базовый вес внешнего канала: подтверждённая закупка ценнее, чем упоминание
# компании в отраслевом справочнике.
CHANNEL_BASE = {"объявление «куплю»": 52.0, "госзакупки": 46.0,
                "отраслевой поиск": 28.0}
# Объявление «куплю» стоит выше отраслевого поиска: это не догадка о профиле,
# а человек, который прямо сейчас ищет такую позицию, и у него есть контакт.
NEAR_REGIONS = ("перм", "свердлов", "екатеринбург", "тюмен", "ханты", "югр",
                "ямал", "башкорт", "татарстан", "самар", "удмурт", "оренбург",
                "челябин", "коми", "томск", "нижневартовск", "когалым")


def create(session: Session, *, item_id: int | None, query_name: str,
           unit: str | None, quantity: float | None,
           user: str = "") -> LeadCampaign:
    item = session.get(Item, item_id) if item_id else None
    name = query_name or (item.name if item else "")
    camp = LeadCampaign(
        item_id=item.id if item else None,
        query_name=name,
        unit=(unit or (item.unit if item else None)),
        quantity=quantity,
        family=(item.family if item else None) or classify_family(name),
        size_key=(item.size_key if item else None) or extract_size_key(name),
        created_by=user,
        status="работает",
        stage="запуск",
    )
    session.add(camp)
    session.commit()
    return camp


def run(session: Session, campaign_id: int) -> LeadCampaign:
    """Выполнить кампанию целиком (синхронно). Исключения не пробрасываются."""
    camp = session.get(LeadCampaign, campaign_id)
    if camp is None:
        raise ValueError("кампания не найдена")
    item = session.get(Item, camp.item_id) if camp.item_id else None
    notes: list[str] = []
    # Повторный запуск кампании не должен удваивать список: чистим прошлый
    # результат, а не дописываем к нему.
    old_ids = [i for (i,) in session.query(Lead.id)
               .filter(Lead.campaign_id == camp.id)]
    if old_ids:
        session.query(LeadEvidence).filter(
            LeadEvidence.lead_id.in_(old_ids)).delete(synchronize_session=False)
        session.query(Lead).filter(
            Lead.campaign_id == camp.id).delete(synchronize_session=False)
        session.commit()
    try:
        # --- 1. ценовой коридор по своим фактам
        camp.stage = "ценовой коридор"
        session.commit()
        cor = pricing.corridor(session, camp.query_name, camp.unit, item)
        camp.price_unit = cor.unit
        camp.price_floor, camp.price_target = cor.floor, cor.target
        camp.price_ceiling = cor.ceiling
        camp.price_basis = "; ".join(
            x for x in (cor.target_basis,
                        f"пол: {cor.floor_basis}" if cor.floor_basis else None,
                        f"потолок: {cor.ceiling_basis}" if cor.ceiling_basis else None)
            if x)
        session.commit()

        # --- 2. покупатели из своей истории
        camp.stage = "покупатели из истории сделок"
        session.commit()
        by_key: dict[str, Lead] = {}
        for il in internal.find(session, camp.query_name, camp.unit, item):
            lead = Lead(
                campaign_id=camp.id,
                counterparty_id=il.counterparty.id,
                name=il.counterparty.name,
                inn=il.counterparty.inn,
                channel=il.channel,
                tier=il.tier,
                region=il.counterparty.region,
                why=il.why,
                expected_price=il.price,
                price_unit=il.price_unit,
                price_basis=("медиана цен, которые он платил нам"
                             if il.price else None),
                score=il.score,
                confidence="high" if il.tier in ("ровно эта позиция",
                                                 "тот же типоразмер") else "medium",
                last_deal_at=il.last_deal_at,
                manager=il.manager,
                is_existing=True,   # это наш действующий клиент, не новый лид
            )
            session.add(lead)
            session.flush()
            for d in il.deals:
                session.add(LeadEvidence(
                    lead_id=lead.id, kind="deal",
                    title=f"{d.item_name} — {d.quantity:g} {d.unit or ''}"
                          if d.quantity else (d.item_name or ""),
                    happened_at=d.period, amount=d.price, unit=d.unit,
                    snippet=(f"продано {d.period:%d.%m.%Y}" if d.period else "")
                            + (f", менеджер {d.manager}" if d.manager else "")))
            by_key[company_key(il.counterparty.name)] = lead
            if il.counterparty.inn:
                by_key[il.counterparty.inn] = lead
        session.commit()


        # --- 2б. объявления «куплю» из мониторинга каналов: это новые
        # покупатели с контактом, а не наши старые клиенты
        camp.stage = "объявления «куплю»"
        session.commit()
        for listing in _buy_listings(session, camp, item):
            _add_listing_lead(session, camp, by_key, cor, listing)
        session.commit()

        # --- 3. внешние источники (не обязательны)
        if not web.is_enabled():
            notes.append("внешние источники пропущены: ключ ИИ-провайдера "
                         "не задан в окружении службы")
        else:
            camp.stage = "профиль товара"
            session.commit()
            profile, err = web._safe(web.product_profile, camp.query_name, camp.unit)
            if err:
                notes.append(f"профиль товара не получен: {err}")
            profile = profile or {}
            if profile:
                import json
                camp.profile = json.dumps(profile, ensure_ascii=False)
                session.commit()

            camp.stage = "закупки в ЕИС"
            session.commit()
            tenders, err = web._safe(web.find_tenders, camp.query_name, profile,
                                     camp.family)
            if err:
                notes.append(f"поиск закупок не удался: {err}")
            for t in tenders or []:
                _add_external(session, camp, by_key, cor,
                              name=t["customer"], inn=t["inn"], region=t["region"],
                              site="", channel="госзакупки",
                              why=(f"закупал: {t['subject']}"
                                   + (f"; {t['quantity']}" if t["quantity"] else "")
                                   + (f"; {t['date']}" if t["date"] else "")),
                              url=t["url"], amount=t["price"],
                              evidence_kind="tender", evidence_title=t["subject"])

            camp.stage = "отраслевые покупатели"
            session.commit()
            buyers, err = web._safe(web.find_buyers, camp.query_name, profile,
                                    camp.family)
            if err:
                notes.append(f"отраслевой поиск не удался: {err}")
            for b in buyers or []:
                _add_external(session, camp, by_key, cor,
                              name=b["name"], inn=b["inn"], region=b["region"],
                              site=b["site"], channel="отраслевой поиск",
                              why=(b["why"] or b["activity"]), url=b["url"],
                              amount=None, evidence_kind="source",
                              evidence_title=b["activity"] or b["name"])
            session.commit()

        camp.leads_found = session.query(Lead).filter(
            Lead.campaign_id == camp.id).count()
        camp.status = "готово"
        camp.stage = "готово"
        camp.error = "; ".join(notes) or None
    except Exception as e:  # noqa: BLE001 — кампания не должна валить сервис
        log.exception("кампания %s упала", campaign_id)
        camp.status = "ошибка"
        camp.error = f"{type(e).__name__}: {e}"
    session.commit()
    return camp


def _add_external(session: Session, camp: LeadCampaign, by_key: dict, cor,
                  *, name: str, inn: str, region: str, site: str, channel: str,
                  why: str, url: str, amount: float | None,
                  evidence_kind: str, evidence_title: str) -> None:
    """Внешняя находка: либо усиливает уже найденного своего клиента, либо
    заводит нового лида."""
    key_name = company_key(name)
    existing = by_key.get(inn) if inn else None
    existing = existing or by_key.get(key_name)
    if existing is not None:
        # компания уже есть среди наших покупателей — это сильный сигнал
        existing.score = round((existing.score or 0) + 12.0, 2)
        existing.why = (existing.why or "") + f" · подтверждено внешне: {why}"[:600]
        existing.site = existing.site or site or None
        existing.region = existing.region or region or None
        session.add(LeadEvidence(lead_id=existing.id, kind=evidence_kind,
                                 title=evidence_title[:400], url=url or None,
                                 amount=amount))
        return

    # свой контрагент, но по этой позиции сделок не было — тоже ценно
    cp = None
    if inn:
        cp = session.query(Counterparty).filter(Counterparty.inn == inn).first()
    if cp is None:
        cp = _counterparty_by_name(session, key_name)

    score = CHANNEL_BASE.get(channel, 20.0)
    if inn:
        score += 8.0
    if region and any(r in region.lower() for r in NEAR_REGIONS):
        score += 10.0
    if cp is not None:
        # не новый клиент: он уже в базе, просто по этой позиции не покупал
        why = f"уже наш контрагент (сделок: {cp.sales_count}). " + why

    lead = Lead(
        campaign_id=camp.id,
        counterparty_id=cp.id if cp else None,
        name=name, inn=inn or None, channel=channel,
        tier="внешний источник", region=region or None, site=site or None,
        why=why[:600], expected_price=cor.target, price_unit=cor.unit,
        price_basis=("ориентир кампании: " + (cor.target_basis or ""))[:400]
                    if cor.target else None,
        score=round(score, 2), confidence="low",
        is_existing=cp is not None,
    )
    session.add(lead)
    session.flush()
    session.add(LeadEvidence(lead_id=lead.id, kind=evidence_kind,
                             title=evidence_title[:400], url=url or None,
                             amount=amount))
    by_key[key_name] = lead
    if inn:
        by_key[inn] = lead


_cp_index: dict[str, dict] = {}


def _counterparty_by_name(session: Session, key: str) -> Counterparty | None:
    """Свой контрагент по ключу организации; индекс строится один раз."""
    marker = session.query(Counterparty).count()
    idx = _cp_index.get("data")
    if _cp_index.get("key") != marker or idx is None:
        idx = {}
        for cp_id, cp_name in session.query(Counterparty.id, Counterparty.name):
            idx.setdefault(company_key(cp_name), cp_id)
        _cp_index.update(key=marker, data=idx)
    cp_id = idx.get(key)
    return session.get(Counterparty, cp_id) if cp_id else None


def _buy_listings(session: Session, camp: LeadCampaign,
                  item: Item | None) -> list[MarketListing]:
    """Объявления «куплю» по нашей позиции из отслеживаемых каналов."""
    from sqlalchemy import or_

    mark = None
    from ..normalize import series_key
    sk = series_key(camp.query_name or "")
    if sk:
        mark = sk[0]
    filters = []
    if item is not None:
        filters.append(MarketListing.item_id == item.id)
    if camp.size_key:
        filters.append(MarketListing.size_key == camp.size_key)
    if mark:
        filters.append(MarketListing.series_mark == mark)
    if camp.family:
        filters.append(MarketListing.family == camp.family)
    if not filters:
        return []
    return (session.query(MarketListing)
            .filter(MarketListing.direction == "buy",
                    MarketListing.is_hidden.is_(False),
                    or_(*filters))
            .order_by(MarketListing.posted_at.desc().nullslast())
            .limit(20).all())


def _add_listing_lead(session: Session, camp: LeadCampaign, by_key: dict, cor,
                      listing: MarketListing) -> None:
    """Лид из объявления «куплю». Имени компании в объявлении обычно нет —
    опознаём по контакту, он же и есть способ связаться."""
    key = (listing.contacts or listing.url or "").lower()
    if not key or key in by_key:
        return
    name = listing.contacts or f"Покупатель из {listing.author or 'Telegram'}"
    age_days = 999
    if listing.posted_at:
        age_days = max(0, (dt.datetime.utcnow() - listing.posted_at).days)
    score = CHANNEL_BASE["объявление «куплю»"] * (0.5 ** (age_days / 45.0))
    lead = Lead(
        campaign_id=camp.id, name=name[:200],
        channel="объявление «куплю»", tier="ищет это прямо сейчас",
        region=listing.region, site=listing.url, contacts=listing.contacts,
        why=(f"объявление от {listing.posted_at:%d.%m.%Y}: " if listing.posted_at
             else "объявление: ") + (listing.text or "")[:400],
        expected_price=listing.price or cor.target,
        price_unit=listing.price_unit or cor.unit,
        price_basis=("цена из объявления" if listing.price
                     else ("ориентир кампании: " + (cor.target_basis or ""))[:400]),
        score=round(score, 2),
        confidence="medium" if listing.item_id else "low",
        is_existing=False,
    )
    session.add(lead)
    session.flush()
    session.add(LeadEvidence(
        lead_id=lead.id, kind="listing", title=(listing.text or "")[:400],
        url=listing.url, happened_at=listing.posted_at, amount=listing.price,
        unit=listing.price_unit))
    by_key[key] = lead


# --------------------------------------------------------------- фоновый запуск

_running: set[int] = set()


def release_orphans(session: Session) -> int:
    """Отметить кампании, которые оборвал перезапуск службы.

    Кампания идёт в фоновом потоке, и рестарт службы убивает его вместе с
    процессом. Без этой уборки такая кампания навсегда остаётся в статусе
    «работает», и оператор ждёт результата, которого уже никто не считает.
    """
    stale = (session.query(LeadCampaign)
             .filter(LeadCampaign.status == "работает").all())
    for camp in stale:
        camp.status = "ошибка"
        camp.error = ("Кампания прервана перезапуском службы на стадии "
                      f"«{camp.stage or 'запуск'}». Запустите подбор заново.")
        camp.stage = "прервана"
    if stale:
        session.commit()
    return len(stale)


def start(session_factory, campaign_id: int) -> None:
    """Запустить кампанию в фоне: внешние источники отвечают секундами."""
    if campaign_id in _running:
        return
    _running.add(campaign_id)

    def worker():
        session = session_factory()
        try:
            run(session, campaign_id)
        finally:
            session.close()
            _running.discard(campaign_id)

    threading.Thread(target=worker, daemon=True,
                     name=f"leads-{campaign_id}").start()


def outreach_draft(session: Session, campaign_id: int, lead_id: int) -> dict:
    """Черновик письма покупателю. Ничего не отправляет — только текст."""
    camp = session.get(LeadCampaign, campaign_id)
    lead = session.get(Lead, lead_id)
    if camp is None or lead is None:
        raise ValueError("кампания или лид не найдены")
    price = lead.expected_price or camp.price_target
    qty = f"{camp.quantity:g} {camp.unit}" if camp.quantity else "по запросу"
    hist = ""
    if lead.last_deal_at:
        hist = (f"\nМы уже работали с вами — последняя отгрузка "
                f"{lead.last_deal_at:%d.%m.%Y}.")
    body = (
        f"Здравствуйте!\n\n"
        f"ООО «МетОптТорг» предлагает {camp.query_name}.\n"
        f"Количество: {qty}."
        + (f"\nЦена: {price:,.0f} ₽".replace(",", " ")
           + f" за {camp.price_unit or camp.unit} без НДС "
             "(обсуждается от объёма)." if price else "")
        + hist +
        "\n\nОборудование б/у, отгрузка с наших площадок, документы по "
        "форме 1С. Готовы прислать фотографии и согласовать осмотр.\n\n"
        "С уважением,\nотдел продаж ООО «МетОптТорг»")
    return {"subject": f"{camp.query_name} — предложение от МетОптТорг",
            "body": body,
            "warning": "Черновик. Сервис ничего не отправляет — "
                       "письмо отправляет менеджер сам."}


def campaign_dict(session: Session, camp: LeadCampaign,
                  with_leads: bool = True) -> dict:
    out = {
        "id": camp.id, "item_id": camp.item_id, "query_name": camp.query_name,
        "unit": camp.unit, "quantity": camp.quantity, "family": camp.family,
        "status": camp.status, "stage": camp.stage, "error": camp.error,
        "created_at": camp.created_at.isoformat() if camp.created_at else None,
        "created_by": camp.created_by,
        "price": {"unit": camp.price_unit, "floor": camp.price_floor,
                  "target": camp.price_target, "ceiling": camp.price_ceiling,
                  "basis": camp.price_basis},
        "leads_found": camp.leads_found,
    }
    if camp.profile:
        import json
        try:
            out["profile"] = json.loads(camp.profile)
        except ValueError:
            out["profile"] = None
    if not with_leads:
        return out
    leads = (session.query(Lead).filter(Lead.campaign_id == camp.id)
             .order_by(Lead.score.desc()).all())
    ev_by_lead: dict[int, list] = {}
    if leads:
        for e in (session.query(LeadEvidence)
                  .filter(LeadEvidence.lead_id.in_([x.id for x in leads])).all()):
            ev_by_lead.setdefault(e.lead_id, []).append(e)
    def row(l):
        return {
        "id": l.id, "name": l.name, "inn": l.inn, "channel": l.channel,
        "tier": l.tier, "region": l.region, "site": web_url(l.site),
        "contacts": l.contacts, "why": l.why,
        "expected_price": l.expected_price, "price_unit": l.price_unit,
        "price_basis": l.price_basis, "score": l.score,
        "confidence": l.confidence, "manager": l.manager,
        "last_deal_at": l.last_deal_at.isoformat() if l.last_deal_at else None,
        "status": l.status, "owner": l.owner, "comment": l.comment,
        "evidence": [{"kind": e.kind, "title": e.title,
                      "url": web_url(e.url),
                      "at": e.happened_at.isoformat() if e.happened_at else None,
                      "amount": e.amount, "unit": e.unit, "snippet": e.snippet}
                     for e in sorted(ev_by_lead.get(l.id, []),
                                     key=lambda x: x.happened_at or dt.datetime.min,
                                     reverse=True)[:8]],
        "is_existing": bool(l.is_existing),
    }

    # Цель вкладки — новые покупатели, поэтому они идут основным списком.
    # Действующие клиенты остаются отдельно: они нужны как ценовой ориентир и
    # как напоминание, что позиция уже кому-то уходила, но искать их не надо —
    # их ведёт отдел продаж.
    out["leads"] = [row(l) for l in leads if not l.is_existing]
    out["existing_customers"] = [row(l) for l in leads if l.is_existing]
    return out
