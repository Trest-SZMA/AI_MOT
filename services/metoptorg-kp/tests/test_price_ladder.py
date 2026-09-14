"""Каскад цены и подбор клиентов — на данных, повторяющих реальные ловушки 1С.

Числа взяты из настоящей выгрузки: «10ВДМ100-2400-3,0-117» продан 01.09.2025
в «СОЮЗ-ТЕХНО ООО» за 35 625 ₽/шт, а КП приходит с именем
«ВДМ 100-2400-3.0-117В5 б/у», у которого есть только закупка.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.calc import price_ladder
from app.db.models import (
    Base,
    Counterparty,
    DealFact,
    Item,
    PriceQuote,
    ScrapPrice,
)
from app.leads import internal, pricing
from app.normalize import classify_family, extract_size_key, is_scrap, model_key, series_key

# (габарит, цена продажи) — фактический ряд ВДМ из 1С
VDM_SALES = [(80, 32_250), (100, 35_625), (128, 44_250), (130, 60_000),
             (150, 48_000), (180, 58_500), (230, 72_375), (250, 81_000)]
# закупки покрывают ряд шире продаж
VDM_PURCHASES = [(20, 2_871), (30, 3_450), (40, 4_400), (50, 5_330),
                 (60, 6_270), (80, 6_940), (100, 13_170), (130, 14_554),
                 (150, 14_120), (200, 23_862)]


def add_item(s, name, unit="шт"):
    it = Item(name=name, name_normalized=name.lower(), unit=unit, is_trade=True,
              family=classify_family(name), size_key=extract_size_key(name))
    s.add(it)
    s.commit()
    return it


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ScrapPrice(material="чермет", price_per_tonne=18_399))
    for size, price in VDM_SALES:
        it = add_item(s, f"Погружной электродвигатель 10ВДМ{size}-2400-3,0-117 (шт)")
        s.add(PriceQuote(item_id=it.id, quote_type="sales_fact", price=price,
                         unit="шт", ttl_days=9999))
    for size, price in VDM_PURCHASES:
        it = add_item(s, f"ВДМ {size}-2400-3.0-117В5 б/у")
        s.add(PriceQuote(item_id=it.id, quote_type="purchase_fact", price=price,
                         unit="шт", ttl_days=9999))
    s.commit()
    price_ladder.reset_cache()
    yield s
    s.close()


# ------------------------------------------------------------- нормализация


def test_gabarit_prefix_and_housing_tail_give_one_model_key():
    """«10ВДМ100-2400-3,0-117» и «ВДМ 100-2400-3.0-117В5 б/у» — одна машина.

    Пока ключи расходились, цена продажи лежала на одной карточке, закупка на
    другой, и позиция КП не видела ни одной.
    """
    a = model_key("Погружной электродвигатель 10ВДМ100-2400-3,0-117 (шт)")
    b = model_key("Погружной электродвигатель ВДМ 100-2400-3.0-117В5 б/у")
    c = model_key("ВДМ 100-2400-3.0-117В5 б/у")
    assert a == b == c


def test_series_key_and_family_for_bare_vdm():
    """Карточка без слова «электродвигатель» тоже должна попадать в семейство."""
    assert classify_family("ВДМ 80-2400-3.0-117В5 б/у") == "ПЭД"
    assert series_key("ВДМ 80-2400-3.0-117В5 б/у") == ("вдм", 80.0)
    assert extract_size_key("ВДМ 80-2400-3.0-117В5 б/у") == "вдм-80"


def test_battery_is_not_scrap_and_not_a_motor():
    """ТНЖШ — щелочная батарея: не лом (иначе цена изделия = цена лома)."""
    assert classify_family("аккумуляторных батарей ТНЖШ-350ВМ-У5") == "аккумуляторы"
    assert not is_scrap("аккумуляторных батарей ТНЖШ-350ВМ-У5")
    assert is_scrap("Лом аккумуляторных батарей ТНЖШ-350ВМ-У5")
    # у ломовой карточки типоразмера ряда быть не должно
    assert extract_size_key("Лом аккумуляторных батарей ТНЖШ-350ВМ-У5") is None


# ------------------------------------------------------------------ каскад


def test_twin_card_sale_beats_family_median(session):
    """Цена берётся с карточки-двойника, а не как медиана по всему ПЭД."""
    item = session.query(Item).filter(
        Item.name == "ВДМ 100-2400-3.0-117В5 б/у").one()
    best = price_ladder.best(session, item.name, "шт", item)
    assert best is not None
    assert best.price == pytest.approx(35_625)
    assert best.confidence == "high"


def test_series_regression_scales_with_gabarit(session):
    """Внутри ряда цена растёт с габаритом: 56 ≠ 170, а не «56 250 у всех»."""
    small = price_ladder.series_estimate(
        session, "Погружной электродвигатель ВДМ 56 б/у (шт)", "шт")
    big = price_ladder.series_estimate(
        session, "Погружной электродвигатель ВДМ 170 (шт) б/у", "шт")
    assert small and big
    assert small.price < big.price
    # обе оценки лежат внутри наблюдавшегося диапазона ряда
    assert 20_000 < small.price < 40_000
    assert 45_000 < big.price < 75_000


def test_far_extrapolation_is_refused(session):
    """За краем ряда вдвое оценка не выдаётся: это была бы случайная цифра."""
    assert price_ladder.series_estimate(session, "ВДМ 5-100-3.0-117В5", "шт") is None
    assert price_ladder.series_estimate(session, "ВДМ 900-100-3.0-117В5", "шт") is None


def test_purchase_with_markup_covers_small_gabarits(session):
    """Габарит ниже ряда продаж закрывается закупкой × наценка ряда."""
    item = session.query(Item).filter(
        Item.name == "ВДМ 20-2400-3.0-117В5 б/у").one()
    cands = price_ladder.candidates(session, item.name, "шт", item)
    markup = next((c for c in cands if c.source == "markup"), None)
    assert markup is not None
    # наценка по ряду ~3,5–4× закупки: 2 871 ₽ → примерно 10 тыс. ₽
    assert 7_000 < markup.price < 16_000
    assert "наценка" in markup.label


def test_confidence_outranks_cascade_order(session):
    """Экстраполяция ряда (low) не должна обходить нашу закупку (medium)."""
    item = session.query(Item).filter(
        Item.name == "ВДМ 20-2400-3.0-117В5 б/у").one()
    best = price_ladder.best(session, item.name, "шт", item)
    assert best.confidence in ("high", "medium")


# ---------------------------------------------------------- подбор клиентов


def _sell(s, cp, name, price, when, unit="шт", scrap=False):
    sk = series_key(name)
    s.add(DealFact(counterparty_id=cp.id, item_name=name,
                   family=classify_family(name), size_key=extract_size_key(name),
                   series_mark=sk[0] if sk else None, direction="sale",
                   period=when, quantity=1, unit=unit, price=price,
                   revenue=price, is_scrap=scrap))


@pytest.fixture()
def deals(session):
    soyuz = Counterparty(name="СОЮЗ-ТЕХНО ООО", name_normalized="союз-техно ооо")
    scrapper = Counterparty(name="СУМЗ-ВЦМ ООО", name_normalized="сумз-вцм ооо")
    old = Counterparty(name="АЛМЕТ ЗАО", name_normalized="алмет зао")
    session.add_all([soyuz, scrapper, old])
    session.commit()
    now = dt.datetime.utcnow()
    for size, price in VDM_SALES:
        _sell(session, soyuz, f"Погружной электродвигатель 10ВДМ{size}-2400-3,0-117 (шт)",
              price, now - dt.timedelta(days=200))
    _sell(session, old, "Погружной электродвигатель ВДМ 100-117В5 б/у",
          81_944, now - dt.timedelta(days=1200), unit="т")
    _sell(session, scrapper, "Лом аккумуляторных батарей (кг)", 44,
          now - dt.timedelta(days=30), unit="кг", scrap=True)
    session.commit()
    return session


def test_recent_buyer_of_same_series_ranks_first(deals):
    leads = internal.find(deals, "Погружной электродвигатель ВДМ 60-117В5 б/у",
                          "шт", None)
    assert leads, "покупатель ряда ВДМ должен находиться"
    assert leads[0].counterparty.name == "СОЮЗ-ТЕХНО ООО"
    assert leads[0].tier == "тот же модельный ряд"
    # давняя сделка АЛМЕТа тоже видна, но ниже
    names = [l.counterparty.name for l in leads]
    assert names.index("СОЮЗ-ТЕХНО ООО") < names.index("АЛМЕТ ЗАО")


def test_scrap_channel_found_for_item_never_sold(deals):
    """Батареи ТНЖШ как изделие не продавались ни разу — но ломовой канал есть."""
    leads = internal.find(deals, "аккумуляторных батарей ТНЖШ-350ВМ-У5", "шт", None)
    assert [l.counterparty.name for l in leads] == ["СУМЗ-ВЦМ ООО"]
    assert leads[0].tier == "ломовой канал"
    assert leads[0].channel == "ломовой канал"


def test_distant_tier_does_not_borrow_price(deals):
    """Цена чужого габарита не выдаётся за ожидаемую по нашей позиции."""
    leads = internal.find(deals, "Погружной электродвигатель ВДМ 60-117В5 б/у",
                          "шт", None)
    almet = next(l for l in leads if l.counterparty.name == "АЛМЕТ ЗАО")
    # АЛМЕТ покупал в тоннах — цена в ₽/т не должна утечь в ₽/шт
    assert almet.price is None


def test_corridor_orders_floor_target_ceiling(deals):
    item = deals.query(Item).filter(
        Item.name == "ВДМ 100-2400-3.0-117В5 б/у").one()
    cor = pricing.corridor(deals, item.name, "шт", item)
    assert cor.target == pytest.approx(35_625)
    assert cor.ceiling is not None and cor.ceiling >= cor.target
    assert cor.candidates and cor.candidates[0]["label"]


# ------------------------------------------- сведение внешних находок со своими


def test_company_key_matches_the_same_firm_written_differently():
    """«ООО «Союз-Техно»» из интернета и «СОЮЗ-ТЕХНО ООО» из 1С — одна фирма.

    Пока ключи расходились, внешний поиск заводил второго лида на покупателя,
    который в списке уже стоял первым.
    """
    from app.normalize import company_key

    assert company_key("ООО «Союз-Техно»") == company_key("СОЮЗ-ТЕХНО ООО")
    assert company_key('ФЛАГМАН ООО (ИНН 6319240833)') == company_key('ООО "Флагман"')
    assert company_key("АЛМЕТ ЗАО") != company_key("АЛМАЗ ООО")


def test_existing_customer_is_kept_out_of_new_leads(deals, monkeypatch):
    """Действующий клиент — не лид: цель вкладки в том, чтобы найти новых.

    При этом внешняя находка, оказавшаяся нашим же контрагентом, не должна
    заводить второго лида — она усиливает уже имеющуюся карточку.
    """
    from app.db.models import Lead, LeadCampaign
    from app.leads import service, web

    monkeypatch.setattr(web, "is_enabled", lambda: True)
    monkeypatch.setattr(web, "product_profile", lambda *a, **k: {"what": "ПЭД"})
    monkeypatch.setattr(web, "find_tenders", lambda *a, **k: [])
    monkeypatch.setattr(web, "find_buyers", lambda *a, **k: [
        {"name": "ООО «АЛМАЗ-Нефтесервис»", "inn": "", "region": "Пермский край",
         "site": "almaz.ru", "activity": "ремонт УЭЦН",
         "why": "профильный ремонтник", "url": "https://example.org/almaz"},
        # та же фирма, что наш покупатель, но написана иначе
        {"name": "ООО «Союз-Техно»", "inn": "", "region": "Пермь", "site": "",
         "activity": "погружное оборудование", "why": "работает с ПЭД",
         "url": "https://example.org/soyuz"},
    ])

    camp = service.create(deals, item_id=None,
                          query_name="Погружной электродвигатель ВДМ 60-117В5 б/у",
                          unit="шт", quantity=1, user="тест")
    service.run(deals, camp.id)
    leads = (deals.query(Lead).filter(Lead.campaign_id == camp.id)
             .order_by(Lead.score.desc()).all())

    soyuz = next(l for l in leads if "оюз" in l.name.lower())
    almaz = next(l for l in leads if "АЛМАЗ" in l.name)
    # дубля быть не должно: внешняя находка усилила существующего лида
    assert sum(1 for l in leads if "оюз" in l.name.lower()) == 1
    assert "подтверждено внешне" in soyuz.why
    # наш действующий клиент помечен как таковой и в список новых не идёт,
    # а найденная снаружи компания — новый лид
    assert soyuz.is_existing is True
    assert almaz.is_existing is False
    assert deals.get(LeadCampaign, camp.id).status == "готово"


# ------------------------------------------------------------- мониторинг


def test_ad_parser_reads_multi_position_message():
    """Объявление списком даёт позицию на строку, а не одну цену на всё."""
    from app.monitor.parse import parse_message

    ad = ("Продам с базы в Когалыме:\n"
          "НКТ 73 б/у - 15 000 руб/т\n"
          "ПЭД 45-117 б/у — 30000 р/шт\n"
          "Тел: +7 912 345-67-89, @lom_ural")
    rows = parse_message(ad)
    assert len(rows) == 2
    nkt, ped = rows
    assert (nkt.family, nkt.size_key, nkt.price, nkt.price_unit) == \
           ("НКТ", "нкт-73", 15_000, "т")
    assert (ped.family, ped.price, ped.price_unit) == ("ПЭД", 30_000, "шт")
    assert all(r.direction == "sell" for r in rows)
    assert "+7 912 345-67-89" in nkt.contacts and "@lom_ural" in nkt.contacts
    assert nkt.region == "Когалым"


def test_ad_parser_does_not_read_designation_as_price():
    """«ВДМ 80-2400» — марка, а не 2 400 ₽, даже когда в строке есть «цена»."""
    from app.monitor.parse import parse_message

    (row,) = parse_message(
        "Погружной электродвигатель ВДМ 80-2400-3.0-117В5 б/у, 4 шт. "
        "в наличии, цена 35 т.р. за штуку, Тюмень")
    assert row.price == 35_000 and row.price_unit == "шт"
    assert row.size_key == "вдм-80"


def test_ad_parser_quantity_is_not_a_price():
    """«60 шт» — количество. Числу без валюты и без «цены» не верим."""
    from app.monitor.parse import parse_message

    (row,) = parse_message(
        "Аккумуляторные батареи ТНЖШ-350 б/у, 60 шт, куплю дорого, Челябинск")
    assert row.direction == "buy"
    assert row.price is None
    assert row.family == "аккумуляторы" and row.size_key == "тнжш-350"


def test_ad_parser_ignores_chatter():
    """Не всякое сообщение канала — объявление."""
    from app.monitor.parse import parse_message

    (row,) = parse_message("Всем привет! Напоминаем про вебинар в четверг")
    assert row.family is None and row.price is None and row.direction is None


def test_telegram_handle_normalization():
    from app.monitor.telegram import normalize_handle

    for raw in ("@lom_ural", "https://t.me/lom_ural", "t.me/s/lom_ural",
                "lom_ural/"):
        assert normalize_handle(raw) == "lom_ural"


def test_market_price_from_ad_is_refused_when_it_contradicts_our_facts(session):
    """Цена из чужого объявления не двигает КП, если расходится с фактами.

    Ошибка разбора текста дороже отсутствия внешней цены: по ней выставят
    предложение продавцу.
    """
    from app.db.models import MarketListing, PriceQuote
    from app.monitor.service import _save_market_price

    item = session.query(Item).filter(
        Item.name == "Погружной электродвигатель 10ВДМ100-2400-3,0-117 (шт)").one()
    listing = MarketListing(url="https://t.me/x/1", text="объявление")
    assert _save_market_price(session, item, 40_000, "шт", listing) is True
    # в двенадцать раз дороже нашего факта 35 625 ₽ — не принимаем
    assert _save_market_price(session, item, 450_000, "шт", listing) is False
    # и единицу не путаем: ₽/т на штучную карточку не ложится
    assert _save_market_price(session, item, 40_000, "т", listing) is False
    saved = session.query(PriceQuote).filter(
        PriceQuote.quote_type == "used_market_offer").all()
    assert len(saved) == 1 and saved[0].price == 40_000


def test_keywords_catch_what_classifier_does_not_know(session):
    """Ключевое слово ловит позицию, которой нет в справочнике 1С."""
    from app.db.models import MonitorKeyword
    from app.monitor import keywords as kw

    session.add(MonitorKeyword(phrase="ЭДБ", whole_word=True))
    session.add(MonitorKeyword(phrase="гидрозащита", whole_word=False))
    session.commit()
    kw.reset_cache()

    # без «целого слова» ищем по основе — русский текст склоняется
    hit = kw.match(session, "Куплю ЭДБ 45-117 и гидрозащиту, самовывоз")
    assert {k.phrase for k in hit} == {"ЭДБ", "гидрозащита"}
    assert {k.phrase for k in kw.match(session, "гидрозащиты в наличии")} == \
           {"гидрозащита"}
    # целым словом: аббревиатура не срабатывает внутри другого слова
    assert kw.match(session, "поставка ЭДБШНЫХ узлов") == []
    # неактивное слово не ищется
    for k in session.query(MonitorKeyword):
        k.is_active = False
    session.commit()
    kw.reset_cache()
    assert kw.match(session, "Куплю ЭДБ 45-117") == []


def test_keyword_seed_is_idempotent(session):
    from app.db.models import MonitorKeyword
    from app.monitor import keywords as kw

    assert kw.seed_defaults(session) > 0
    before = session.query(MonitorKeyword).count()
    assert kw.seed_defaults(session) == 0     # второй раз не дублирует
    assert session.query(MonitorKeyword).count() == before


# ------------------------------------------------- источники ScrapeGraphAI


def test_web_source_needs_a_key(session):
    """Без ключа страницу добавить нельзя — и сказано, чего не хватает."""
    from app.monitor import scrapegraph
    from app.monitor import service as mon

    with pytest.raises(scrapegraph.ScrapeGraphUnavailable) as e:
        mon.add_source(session, "https://example.org/list", "web")
    assert "SGAI_API_KEY" in str(e.value)


def test_web_source_is_polled_and_parsed(session, monkeypatch):
    """Разобранная страница проходит тот же путь, что и Telegram-объявление."""
    from app.db.models import MarketListing, MonitorSource
    from app.monitor import scrapegraph
    from app.monitor import service as mon

    monkeypatch.setattr(scrapegraph, "is_enabled", lambda: True)
    monkeypatch.setattr(scrapegraph, "extract_listings", lambda url, **k: [
        {"title": "Куплю ПЭД 45-117 б/у", "text": "самовывоз, Пермь",
         "direction": "buy", "price": 30_000, "price_unit": "шт",
         "seller": "ООО Ремонт", "region": "Пермский край",
         "contacts": "+7 900 000-00-00", "date": "", "url": "https://x/1"},
        {"title": "Реклама вебинара", "text": "приходите в четверг",
         "direction": None, "price": None, "price_unit": None,
         "seller": None, "region": None, "contacts": None, "date": "",
         "url": "https://x/2"},
    ])
    src = mon.add_source(session, "https://example.org/board", "web")
    assert src.kind == "web" and src.target == "https://example.org/board"

    res = mon.poll_all(session)
    assert res.sources == 1 and res.new_listings == 2
    buy = (session.query(MarketListing)
           .filter(MarketListing.direction == "buy").one())
    assert buy.family == "ПЭД" and buy.size_key == "пэд-45"
    assert buy.price == 30_000 and buy.price_unit == "шт"
    assert "+7 900 000-00-00" in buy.contacts

    # повторный опрос не задваивает: отпечаток ссылки и заголовка тот же
    res2 = mon.poll_all(session)
    assert res2.new_listings == 0
    assert session.query(MonitorSource).count() == 1


def test_search_source_uses_search_endpoint(session, monkeypatch):
    from app.monitor import scrapegraph
    from app.monitor import service as mon

    calls = []
    monkeypatch.setattr(scrapegraph, "is_enabled", lambda: True)
    monkeypatch.setattr(scrapegraph, "search_listings",
                        lambda q, **k: calls.append(q) or [])
    mon.add_source(session, "куплю ПЭД 117 б/у", "search")
    mon.poll_all(session)
    assert calls == ["куплю ПЭД 117 б/у"]


def test_bulk_add_reports_each_source_separately(session, monkeypatch):
    """Список из папки Telegram приходит россыпью — по каждому нужен вердикт."""
    from app.monitor import service as mon
    from app.monitor import telegram

    def fake_fetch(handle):
        if handle == "good_channel":
            return "Трубы б/у", [object()]
        raise telegram.TelegramUnavailable(
            f"У канала @{handle} не видно сообщений — вероятно, это группа")

    monkeypatch.setattr(telegram, "fetch", fake_fetch)
    rows = mon.add_many(session, """
        @good_channel
        https://t.me/private_group
        good_channel
    """)
    assert [r["value"] for r in rows] == ["good_channel", "private_group"]
    assert rows[0]["ok"] is True and rows[0]["title"] == "Трубы б/у"
    assert rows[1]["ok"] is False and "группа" in rows[1]["error"]


def test_search_result_is_one_listing_not_many(session, monkeypatch):
    """Страница из поиска — одно объявление, а не по строке на каждую строку."""
    from app.db.models import MarketListing
    from app.monitor import scrapegraph
    from app.monitor import service as mon

    monkeypatch.setattr(scrapegraph, "is_enabled", lambda: True)
    monkeypatch.setattr(scrapegraph, "search_listings", lambda q, **k: [{
        "title": "Куплю ПЭД 45-117 б/у",
        "text": "Куплю ПЭД 45-117 б/у\nцена договорная\nтруба НКТ 73 тоже нужна",
        "direction": None, "price": None, "price_unit": None, "seller": None,
        "region": None, "contacts": None, "date": None, "url": "https://x/1"}])
    mon.add_source(session, "куплю ПЭД", "search")
    res = mon.poll_all(session)
    assert res.new_listings == 1
    assert session.query(MarketListing).one().family == "ПЭД"


def test_useful_lines_prefers_price_and_contacts_over_menu():
    """Из страницы берём строки с ценой и телефоном, а не шапку сайта."""
    from app.monitor.scrapegraph import useful_lines

    page = ("Главная Каталог Контакты О компании Доставка Оплата Новости\n"
            "Разделы сайта: трубы, метизы, прокат, услуги, вакансии\n"
            "Труба НКТ 73 б/у — цена 15 000 руб/т\n"
            "Телефон: +7 912 345-67-89\n")
    picked = useful_lines(page, 500)
    assert "15 000 руб/т" in picked and "+7 912 345-67-89" in picked
    assert "Главная Каталог" not in picked


# ------------------------------------------------ частота запросов к ИИ


def _http_error(code, body=b'{"error":{"code":429}}', headers=None):
    import io as _io
    import urllib.error

    return urllib.error.HTTPError("https://api", code, "Too Many Requests",
                                  headers or {}, _io.BytesIO(body))


def test_provider_429_is_retried_not_surfaced(monkeypatch):
    """429 — это «подожди», а не «не смог»: кампания не должна падать из-за него.

    Подбор клиентов бьёт три запроса подряд, и провайдер резал их по частоте.
    """
    import json as _json
    import io as _io
    from app import ai_client

    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    monkeypatch.setattr(ai_client.time, "sleep", lambda s: None)
    ai_client._last_call_at = 0.0

    calls = {"n": 0}
    ok = _json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]})

    def fake_urlopen(req, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429)
        class _Resp:
            def read(self_inner):
                return ok.encode()
            def __enter__(self_inner):
                return _io.BytesIO(ok.encode())
            def __exit__(self_inner, *a):
                return False
        return _Resp()

    monkeypatch.setattr(ai_client.urllib.request, "urlopen", fake_urlopen)
    content, _ = ai_client.chat("проверка")
    assert calls["n"] == 3           # два отказа и успех с третьей попытки
    assert "ok" in content


def test_provider_429_after_all_retries_explains_what_to_do(monkeypatch):
    from app import ai_client

    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    monkeypatch.setenv("AI_MAX_RETRIES", "2")
    monkeypatch.setattr(ai_client.time, "sleep", lambda s: None)
    ai_client._last_call_at = 0.0
    monkeypatch.setattr(ai_client.urllib.request, "urlopen",
                        lambda req, **kw: (_ for _ in ()).throw(_http_error(429)))

    with pytest.raises(ai_client.AiUnavailable) as e:
        ai_client.chat("проверка")
    text = str(e.value)
    assert "AI_MIN_INTERVAL_SEC" in text and "429" in text


def test_retry_after_header_is_respected(monkeypatch):
    """Если провайдер сказал, сколько ждать, — ждём именно столько."""
    from app import ai_client

    assert ai_client._retry_after({"Retry-After": "7"}, 0) == 7.0
    assert ai_client._retry_after({"Retry-After": "мусор"}, 0) > 0
    # без заголовка пауза растёт с номером попытки
    assert ai_client._retry_after(None, 2) > ai_client._retry_after(None, 0)


def test_calls_are_spaced_apart(monkeypatch):
    """Запросы к провайдеру сериализованы и разнесены во времени."""
    from app import ai_client

    slept = []
    monkeypatch.setenv("AI_MIN_INTERVAL_SEC", "2")
    monkeypatch.setattr(ai_client.time, "sleep", lambda s: slept.append(s))
    ai_client._last_call_at = ai_client.time.monotonic()
    ai_client._throttle()
    assert slept and 0 < slept[0] <= 2


def test_empty_profile_does_not_kill_external_search(monkeypatch):
    """Пустой профиль от модели не должен обнулять отраслевой поиск.

    Провайдер иногда отвечает скелетом JSON с пустыми полями. Раньше в
    следующий запрос уходило «Чем является: —», и он возвращал ноль компаний.
    """
    from app import ai_client
    from app.leads import web

    sent = {}

    def fake_ask(prompt, **kwargs):
        sent["prompt"] = prompt
        return [], []

    monkeypatch.setattr(ai_client, "ask_json", fake_ask)
    web.find_buyers("Погружной электродвигатель ВДМ 60-117В5 б/у", {}, "ПЭД")
    # в полях запроса не должно остаться прочерков вместо описания
    assert "Чем является: —" not in sent["prompt"]
    assert "Отрасли-потребители: —" not in sent["prompt"]
    assert "ВДМ 60-117В5" in sent["prompt"]
    assert "нефтесервис" in sent["prompt"]

    # для батарей подставляется своя отрасль, а не общая «промышленность»
    web.find_buyers("аккумуляторные батареи ТНЖШ-350", {}, "аккумуляторы")
    assert "шахтный транспорт" in sent["prompt"]


def test_restart_does_not_leave_campaigns_hanging(session):
    """Рестарт службы убивает фоновый поток — кампания не должна «висеть»."""
    from app.db.models import LeadCampaign
    from app.leads import service

    session.add(LeadCampaign(query_name="ВДМ 60", status="работает",
                             stage="отраслевые покупатели"))
    session.add(LeadCampaign(query_name="НКТ 73", status="готово",
                             stage="готово"))
    session.commit()

    assert service.release_orphans(session) == 1
    rows = {c.query_name: c for c in session.query(LeadCampaign)}
    assert rows["ВДМ 60"].status == "ошибка"
    assert "перезапуском службы" in rows["ВДМ 60"].error
    assert rows["НКТ 73"].status == "готово"      # завершённую не трогаем
    assert service.release_orphans(session) == 0  # второй раз убирать нечего


# ------------------------------------------------------------------ ссылки


def test_bare_domain_becomes_a_real_link():
    """«almaz-neft.ru» без схемы браузер считает путём на нашем сервере.

    Из-за этого ссылка «сайт» в карточке лида открывала 404 нашего же API.
    """
    from app.normalize import web_url

    assert web_url("almaz-neft.ru") == "https://almaz-neft.ru"
    assert web_url("www.kraftpump.ru") == "https://www.kraftpump.ru"
    assert web_url("rn-npo.ru/services/remont") == "https://rn-npo.ru/services/remont"
    assert web_url("//cdn.example.ru/a") == "https://cdn.example.ru/a"
    # уже абсолютные не трогаем
    assert web_url("https://borets.ru/about") == "https://borets.ru/about"
    # мусор ссылкой не притворяется
    assert web_url("нет данных") is None
    assert web_url("+7 912 000-11-22") is None
    assert web_url("") is None


def test_stored_lead_link_is_fixed_on_read(deals):
    """Уже сохранённые лиды с «голым» доменом должны открываться без миграции."""
    from app.db.models import Lead, LeadCampaign, LeadEvidence
    from app.leads import service

    camp = LeadCampaign(query_name="ВДМ 60", status="готово")
    deals.add(camp)
    deals.commit()
    lead = Lead(campaign_id=camp.id, name="ООО «АЛМАЗ-Нефтесервис»",
                site="almaz-neft.ru", score=38.0, is_existing=False)
    deals.add(lead)
    deals.flush()
    deals.add(LeadEvidence(lead_id=lead.id, kind="source", title="услуги",
                           url="almaz-neft.ru/services"))
    deals.commit()

    d = service.campaign_dict(deals, camp)
    row = d["leads"][0]
    assert row["site"] == "https://almaz-neft.ru"
    assert row["evidence"][0]["url"] == "https://almaz-neft.ru/services"
