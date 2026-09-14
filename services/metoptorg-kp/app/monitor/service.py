"""Опрос источников, сохранение объявлений и их польза для дела.

Из ленты объявлений сервис извлекает ровно две вещи:

* «куплю» — это готовый покупатель. Такое объявление попадает в ленту с
  пометкой и контактами, менеджер звонит.
* «продам» с ценой и распознанной позицией — это рыночный ориентир. Он
  ложится в price_quotes как `used_market_offer`, то есть в самый низ каскада
  цены: ниже наших фактов, но выше пустоты.

Внешнюю цену пускаем в каскад с оглядкой: только когда объявление удалось
посадить на конкретную карточку и когда цена не расходится с нашей оценкой
более чем впятеро. Ошибка разбора чужого текста не должна двигать наши КП.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import threading
import time

from sqlalchemy.orm import Session

from ..db.models import Item, MarketListing, MonitorSource, PriceQuote
from ..importers.kp import match_item
from . import keywords as kw
from . import parse, scrapegraph, telegram

log = logging.getLogger(__name__)

# Расхождение с нашей оценкой, после которого цена из объявления не пишется
SANITY_FACTOR = 5.0
MARKET_TTL_DAYS = 45
# Периодичность фонового опроса; 0 — выключить
DEFAULT_INTERVAL_MIN = 30


class PollResult:
    def __init__(self) -> None:
        self.sources = 0
        self.messages = 0
        self.new_listings = 0
        self.buy_leads = 0
        self.prices_saved = 0
        self.by_keyword = 0
        self.errors: list[str] = []

    def as_dict(self) -> dict:
        return {"sources": self.sources, "messages": self.messages,
                "new_listings": self.new_listings, "buy_leads": self.buy_leads,
                "prices_saved": self.prices_saved, "by_keyword": self.by_keyword,
                "errors": self.errors[:10]}


KINDS = ("telegram", "web", "search")


def _label(kind: str, value: str) -> tuple[str, str]:
    """→ (короткая метка для таблицы, что опрашиваем на самом деле)."""
    value = (value or "").strip()
    if kind == "telegram":
        h = telegram.normalize_handle(value)
        return h, h
    if kind == "web":
        host = re.sub(r"^https?://", "", value, flags=re.I).split("/")[0]
        return host[:120], value
    return value[:120], value


def add_source(session: Session, value: str, kind: str = "telegram",
               user: str = "") -> MonitorSource:
    """Добавить источник. Telegram проверяем сразу — читается он или нет.

    Страницу и поисковый запрос не проверяем: каждая проверка тратит кредиты
    ScrapeGraphAI, а результат всё равно виден после первого опроса.
    """
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид источника: {kind}")
    if kind in ("web", "search") and not scrapegraph.is_enabled():
        raise scrapegraph.ScrapeGraphUnavailable(
            "Страницы и поисковые запросы разбирает ScrapeGraphAI, а его ключ "
            "не задан: добавьте SGAI_API_KEY в /etc/metoptorg-kp.env. "
            "Telegram-каналы читаются и без него.")
    handle, target = _label(kind, value)
    if not handle:
        raise ValueError("пустой источник")
    existing = (session.query(MonitorSource)
                .filter(MonitorSource.kind == kind,
                        MonitorSource.target == target).first())
    if existing is not None:
        existing.is_active = True
        session.commit()
        return existing
    title = None
    if kind == "telegram":
        title, _ = telegram.fetch(handle)   # бросит TelegramUnavailable
    elif kind == "search":
        title = f"поиск: {target[:80]}"
    else:
        title = target[:200]
    src = MonitorSource(kind=kind, handle=handle, target=target, title=title,
                        added_by=user)
    session.add(src)
    session.commit()
    return src


def _link_item(session: Session, listing_text: str, size_key: str | None,
               unit: str | None) -> tuple[int | None, str | None]:
    """Посадить объявление на карточку справочника — только уверенно."""
    if not size_key:
        return None, None
    item_id, kind, _ = match_item(session, listing_text, unit)
    if item_id and kind in ("exact", "alias", "model"):
        return item_id, kind
    # запасной ход: карточка того же типоразмера с той же единицей
    q = session.query(Item).filter(Item.size_key == size_key,
                                   Item.is_trade.is_(True),
                                   Item.is_hidden.is_(False))
    if unit:
        q = q.filter(Item.unit == unit)
    rows = q.all()
    if len(rows) == 1:
        return rows[0].id, "size"
    return None, None


def _save_market_price(session: Session, item: Item, price: float,
                       unit: str, listing: MarketListing) -> bool:
    """Записать рыночное предложение в каскад цены, если оно правдоподобно."""
    from ..calc import price_ladder

    if (item.unit or "").strip().lower() != (unit or "").strip().lower():
        return False   # ₽/т на штучную карточку класть нельзя
    ref = next((c for c in price_ladder.candidates(session, item.name,
                                                   item.unit, item)
                if c.source not in ("used_market_offer", "ai_estimate")), None)
    if ref and (price > ref.price * SANITY_FACTOR
                or price < ref.price / SANITY_FACTOR):
        return False
    session.query(PriceQuote).filter(
        PriceQuote.item_id == item.id,
        PriceQuote.quote_type == "used_market_offer",
        PriceQuote.source_url == listing.url).delete(synchronize_session=False)
    session.add(PriceQuote(
        item_id=item.id, quote_type="used_market_offer", price=price,
        unit=unit, source=f"объявление · {listing.author or 'Telegram'}",
        source_url=listing.url, confidence="low", ttl_days=MARKET_TTL_DAYS,
        notes=listing.text[:400]))
    return True


def _store(session: Session, src: MonitorSource, result: PollResult,
           known: set, *, external_id: str, parsed, posted_at, url: str,
           author: str | None, contacts: str | None = None,
           region: str | None = None) -> None:
    """Сохранить разобранное объявление и снять с него всю пользу."""
    if external_id in known:
        return
    known.add(external_id)
    listing = MarketListing(
        source_id=src.id, kind=src.kind, external_id=external_id,
        posted_at=posted_at, url=url, author=author, text=parsed.text,
        direction=parsed.direction, price=parsed.price,
        price_unit=parsed.price_unit, family=parsed.family,
        size_key=parsed.size_key, series_mark=parsed.series_mark,
        contacts=parsed.contacts or contacts, region=parsed.region or region)
    hits = kw.match(session, parsed.text)
    if hits:
        listing.matched_keywords = "; ".join(k.phrase for k in hits)
        kw.note_hits(session, hits, posted_at)
        result.by_keyword += 1
    item_id, kind = _link_item(session, parsed.text, parsed.size_key,
                               parsed.price_unit)
    listing.item_id, listing.match_kind = item_id, kind
    session.add(listing)
    session.flush()
    result.new_listings += 1
    src.listings_found = (src.listings_found or 0) + 1
    if parsed.direction == "buy" and (parsed.family or parsed.series_mark or hits):
        result.buy_leads += 1
    if parsed.direction == "sell" and parsed.price and item_id and parsed.price_unit:
        item = session.get(Item, item_id)
        if item is not None and _save_market_price(
                session, item, parsed.price, parsed.price_unit, listing):
            result.prices_saved += 1


def poll_web(session: Session, src: MonitorSource, result: PollResult) -> None:
    """Страница или поисковый запрос через ScrapeGraphAI."""
    try:
        rows = (scrapegraph.search_listings(src.target)
                if src.kind == "search"
                else scrapegraph.extract_listings(src.target))
    except scrapegraph.ScrapeGraphUnavailable as e:
        src.last_error = str(e)[:500]
        src.last_polled_at = dt.datetime.utcnow()
        session.commit()
        result.errors.append(f"{src.handle}: {e}")
        return

    src.last_error = None
    result.messages += len(rows)
    src.messages_seen = (src.messages_seen or 0) + len(rows)
    known = {x for (x,) in session.query(MarketListing.external_id)
             .filter(MarketListing.source_id == src.id)}
    for row in rows:
        # у объявления на сайте нет своего id — берём отпечаток ссылки и текста
        digest = hashlib.sha256(
            f"{row['url']}|{row['title']}".encode()).hexdigest()[:24]
        # Позиция обычно названа в заголовке, а не в теле: разбирать нужно всё
        # вместе, иначе «Куплю ПЭД 45-117» из заголовка потеряется.
        title, body = (row.get("title") or ""), (row.get("text") or "")
        text = body if title and title in body else f"{title}. {body}".strip(". ")
        rows_parsed = parse.parse_message(text)
        if src.kind == "search":
            # результат поиска — это одна страница про одну позицию;
            # разбивать её на строки значит плодить дубли одного объявления
            rows_parsed = rows_parsed[:1]
        for parsed in rows_parsed:
            # то, что уже определил сам сервис, важнее нашей регулярки
            parsed.direction = row["direction"] or parsed.direction
            parsed.price = row["price"] or parsed.price
            parsed.price_unit = row["price_unit"] or parsed.price_unit
            eid = digest if parsed.line_no == 0 else f"{digest}#{parsed.line_no}"
            _store(session, src, result, known, external_id=eid, parsed=parsed,
                   posted_at=None, url=row["url"] or src.target,
                   author=row["seller"] or src.title,
                   contacts=row["contacts"], region=row["region"])
    src.last_polled_at = dt.datetime.utcnow()
    session.commit()


def poll_source(session: Session, src: MonitorSource,
                result: PollResult) -> None:
    if src.kind in ("web", "search"):
        poll_web(session, src, result)
        return
    try:
        title, messages = telegram.fetch(src.handle)
    except telegram.TelegramUnavailable as e:
        src.last_error = str(e)[:500]
        src.last_polled_at = dt.datetime.utcnow()
        session.commit()
        result.errors.append(f"@{src.handle}: {e}")
        return

    src.title = title or src.title
    src.last_error = None
    known = {x for (x,) in session.query(MarketListing.external_id)
             .filter(MarketListing.source_id == src.id)}
    result.messages += len(messages)
    src.messages_seen = (src.messages_seen or 0) + len(messages)

    for msg in messages:
        for parsed in parse.parse_message(msg.text):
            external_id = (msg.external_id if parsed.line_no == 0
                           else f"{msg.external_id}#{parsed.line_no}")
            _store(session, src, result, known, external_id=external_id,
                   parsed=parsed, posted_at=msg.posted_at, url=msg.url,
                   author=title)

    src.last_polled_at = dt.datetime.utcnow()
    session.commit()


def poll_all(session: Session, source_id: int | None = None) -> PollResult:
    result = PollResult()
    q = session.query(MonitorSource).filter(MonitorSource.is_active.is_(True))
    if source_id is not None:
        q = q.filter(MonitorSource.id == source_id)
    for src in q.all():
        result.sources += 1
        poll_source(session, src, result)
    if result.prices_saved:
        from ..calc import price_ladder
        price_ladder.reset_cache()
    return result


# ------------------------------------------------------------- расписание

_scheduler_started = False


def interval_minutes() -> int:
    try:
        return int(os.environ.get("MONITOR_INTERVAL_MIN", DEFAULT_INTERVAL_MIN))
    except ValueError:
        return DEFAULT_INTERVAL_MIN


def start_scheduler(session_factory) -> bool:
    """Фоновый опрос по расписанию. Возвращает False, если он выключен."""
    global _scheduler_started
    minutes = interval_minutes()
    if _scheduler_started or minutes <= 0:
        return False
    _scheduler_started = True

    def loop():
        # первый проход с задержкой: запуск службы не должен ждать сеть
        time.sleep(20)
        while True:
            session = session_factory()
            try:
                res = poll_all(session)
                if res.sources:
                    log.info("мониторинг: источников %s, новых объявлений %s",
                             res.sources, res.new_listings)
            except Exception:  # noqa: BLE001 — расписание не должно умирать
                log.exception("мониторинг: проход не удался")
            finally:
                session.close()
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True, name="monitor").start()
    return True


# ------------------------------------------------------- массовое добавление

_HANDLE_LINE_RE = re.compile(r"[@a-zA-Z0-9_/:.\-]+")


def add_many(session: Session, text: str, kind: str = "telegram",
             user: str = "") -> list[dict]:
    """Добавить пачку источников и сказать по каждому, читается он или нет.

    Список каналов приходит из папки Telegram россыпью, и проверять их по
    одному — это семь кругов через интерфейс. Здесь каждый проверяется сразу,
    а причина отказа возвращается словами: закрытый канал, группа или опечатка.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        for token in _HANDLE_LINE_RE.findall(raw_line):
            value = token.strip().strip(",;")
            if kind == "telegram":
                value = telegram.normalize_handle(value)
                if len(value) < 4:
                    continue
            if not value or value.lower() in seen:
                continue
            seen.add(value.lower())
            try:
                src = add_source(session, value, kind, user)
                out.append({"value": value, "ok": True, "id": src.id,
                            "title": src.title})
            except telegram.TelegramUnavailable as e:
                out.append({"value": value, "ok": False, "error": str(e)})
            except scrapegraph.ScrapeGraphUnavailable as e:
                out.append({"value": value, "ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001 — одна строка не рушит пачку
                out.append({"value": value, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    return out
