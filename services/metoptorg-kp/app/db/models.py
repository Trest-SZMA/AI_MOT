"""Модель данных сервиса оценки КП «МетОптТорг».

СУБД-независимая схема (SQLite для разработки, PostgreSQL в проде).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- справочник


class Item(Base):
    """Карточка номенклатуры (очищенный справочник 1С)."""

    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    guid = Column(String(64), unique=True, index=True)  # guid канона из 1С
    code = Column(String(32), index=True)  # код «Материал» из 1С, если известен
    name = Column(Text, nullable=False)
    name_normalized = Column(Text, index=True, nullable=False)
    unit = Column(String(32))  # учётная единица карточки
    category = Column(String(128))  # номенклатурная группа
    family = Column(String(64), index=True)  # семейство (ПЭД, НКТ, кабель…)
    size_key = Column(String(64), index=True)  # типоразмер («нкт-73»)
    is_trade = Column(Boolean, default=False, index=True)
    is_hidden = Column(Boolean, default=False, index=True)
    source = Column(String(16), default="1c")  # '1c' | 'manual'
    # Масса единицы: в выгрузке 1С реквизита «Вес» нет, поэтому берём из
    # открытых источников (ИИ) или вводим руками. Профиль семейства — грубее.
    unit_mass_kg = Column(Float)
    unit_mass_source = Column(String(255))
    unit_mass_url = Column(Text)
    unit_mass_confidence = Column(String(16))
    unit_mass_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)

    aliases = relationship("ItemAlias", back_populates="item")


class ItemAlias(Base):
    """Guid'ы и имена объединённых дублей — связь с 1С не теряется."""

    __tablename__ = "item_aliases"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True, nullable=False)
    guid = Column(String(64), index=True)
    name = Column(Text)
    name_normalized = Column(Text, index=True)

    item = relationship("Item", back_populates="aliases")


class MergeCandidate(Base):
    """Очередь нечётких дублей на ручное объединение."""

    __tablename__ = "merge_candidates"

    id = Column(Integer, primary_key=True)
    item_id_a = Column(Integer, ForeignKey("items.id"), nullable=False)
    item_id_b = Column(Integer, ForeignKey("items.id"), nullable=False)
    score = Column(Float)
    status = Column(String(16), default="pending")  # pending|merged|rejected
    decided_by = Column(String(64))
    decided_at = Column(DateTime)


# ---------------------------------------------------------------- цены


class PriceQuote(Base):
    __tablename__ = "price_quotes"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    item_name = Column(Text)  # если карточки нет
    quote_type = Column(String(40), nullable=False)
    # resale_price_without_vat | used_market_offer | scrap_price_per_tonne |
    # supplier_offer | sales_fact | purchase_fact | ai_estimate
    metal = Column(String(32))  # для scrap_price_per_tonne
    price = Column(Float, nullable=False)
    unit = Column(String(32))
    currency = Column(String(8), default="RUB")
    source = Column(String(255))
    source_url = Column(Text)
    confidence = Column(String(16), default="medium")  # low|medium|high
    quoted_at = Column(DateTime, default=utcnow)
    ttl_days = Column(Integer, default=180)
    notes = Column(Text)


class ScrapPrice(Base):
    """Прайс лома по материалам/маркам с историей (запись на каждую смену цены)."""

    __tablename__ = "scrap_prices"

    id = Column(Integer, primary_key=True)
    material = Column(String(32), nullable=False, index=True)
    # медь | медь луженая | чермет | латунь | алюминий | свинец |
    # нержавейка | 16АЦ (броня/оцинкованная полоса)
    grade = Column(String(32))  # марка: 12А, М5, Л14…
    price_per_tonne = Column(Float, nullable=False)
    source = Column(String(255))
    valid_from = Column(DateTime, default=utcnow)
    created_by = Column(String(64))
    is_current = Column(Boolean, default=True, index=True)


class MarketPrice(Base):
    """Индекс рынка лома из еженедельного обзора (внешний ориентир).

    Не подменяет наш прайс: мы продаём дешевле индекса (грейд, засор, условия),
    поэтому рабочая цена = индекс × наш исторический коэффициент.
    """

    __tablename__ = "market_prices"

    id = Column(Integer, primary_key=True)
    scope_kind = Column(String(16), nullable=False)   # region | consumer
    scope = Column(String(120), nullable=False, index=True)
    material = Column(String(32), default="чермет")
    grade = Column(String(32))                        # 3А и т.п.
    basis = Column(String(8))                         # FCA (без ж/д) | CPT (с ж/д)
    price_per_tonne = Column(Float, nullable=False)
    price_min = Column(Float)
    price_max = Column(Float)
    observations = Column(Integer)                    # сколько дней в среднем
    period_start = Column(DateTime)
    period_end = Column(DateTime)
    issue = Column(String(32))                        # «34-2026»
    source_file_id = Column(Integer, ForeignKey("source_files.id"))
    imported_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------- составы/выхода


class ComponentYield(Base):
    """Состав партии: сколько какого металла вышло."""

    __tablename__ = "component_yields"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    item_name = Column(Text, index=True)
    item_name_normalized = Column(Text, index=True)
    component_name = Column(Text)
    material = Column(String(32), nullable=False)
    quantity = Column(Float)  # размер партии
    unit = Column(String(32))
    gross_weight_kg = Column(Float)  # полная масса партии
    metal_mass_kg = Column(Float, nullable=False)
    block = Column(String(16), default="expert")  # mot | expert | ai
    source_file_id = Column(Integer, ForeignKey("source_files.id"))
    document_ref = Column(String(255), index=True)  # уникален на строку файла
    confidence = Column(String(16), default="medium")
    notes = Column(Text)


class ExpertYield(Base):
    """Процент делового выхода."""

    __tablename__ = "expert_yields"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    item_name = Column(Text)
    item_name_normalized = Column(Text, index=True)
    category = Column(String(128), index=True)
    size_key = Column(String(64), index=True)
    block = Column(String(16), nullable=False)  # mot | expert | ai
    good_percent = Column(Float, nullable=False)
    scrap_percent = Column(Float)
    expert_name = Column(String(128))
    confidence = Column(String(16), default="medium")
    source_url = Column(Text)
    approved_by = Column(String(64))
    approved_at = Column(DateTime)
    notes = Column(Text)


class ExpertMetalProfile(Base):
    """Статистический профиль семейства (пересобирается после импорта)."""

    __tablename__ = "expert_metal_profiles"

    id = Column(Integer, primary_key=True)
    family = Column(String(64), index=True, nullable=False)
    material = Column(String(32), nullable=False)
    kg_per_unit = Column(Float)
    kg_per_unit_min = Column(Float)
    kg_per_unit_max = Column(Float)
    percent_of_mass = Column(Float)
    batches = Column(Integer)
    units = Column(Float)
    mass_t = Column(Float)
    rebuilt_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("family", "material"),)


class CableBrand(Base):
    """Помарочный справочник кабеля."""

    __tablename__ = "cable_brands"

    id = Column(Integer, primary_key=True)
    brand = Column(Text, nullable=False)
    brand_normalized = Column(Text, index=True, nullable=False)
    cores_spec = Column(String(64))  # «3х16», «3х150+1х70»
    cable_kg_per_km = Column(Float)
    copper_kg_per_km = Column(Float)
    tinned_copper_kg_per_km = Column(Float)
    aluminum_kg_per_km = Column(Float)
    lead_kg_per_km = Column(Float)
    armor_kg_per_km = Column(Float)  # броня/оцинкованная полоса (16АЦ)
    manufacturer = Column(String(128))  # завод (вес зависит от завода)
    source = Column(String(255))
    source_url = Column(Text)
    confidence = Column(String(16), default="medium")
    source_file_id = Column(Integer, ForeignKey("source_files.id"))


# ---------------------------------------------------------------- КП


class SourceFile(Base):
    __tablename__ = "source_files"

    id = Column(Integer, primary_key=True)
    filename = Column(Text, nullable=False)
    sha256 = Column(String(64), unique=True, index=True, nullable=False)
    kind = Column(String(32))  # kp | yields | cable | nomenclature | other
    stored_path = Column(Text)
    uploaded_by = Column(String(64))
    uploaded_at = Column(DateTime, default=utcnow)
    parse_status = Column(String(32), default="new")
    # new | parsed | needs_review (OCR) | error | duplicate
    parse_log = Column(Text)


class KpDocument(Base):
    __tablename__ = "kp_documents"

    id = Column(Integer, primary_key=True)
    source_file_id = Column(Integer, ForeignKey("source_files.id"))
    title = Column(Text)
    seller = Column(String(255))
    created_at = Column(DateTime, default=utcnow)
    # дата оценки: цены лома и нормативы берутся действовавшие на эту дату,
    # чтобы отправленное продавцу предложение воспроизводилось дословно
    priced_at = Column(DateTime, default=utcnow)
    # итог по весу, заявленный в самом файле КП, — для сверки разбора
    file_total_weight_kg = Column(Float)
    # место погрузки: по нему берётся фактическая ставка перевозки вместо
    # общей медианы — разброс по базам 295…10 000 ₽/т решает исход торга
    seller_region = Column(String(120))
    # ---- работа с предложением: статус, отправленная сумма, решение
    status = Column(String(16), default="черновик", index=True)
    # черновик | отправлено | выиграно | проиграно
    offer_amount = Column(Float)        # сколько реально предложили продавцу
    offer_scenario = Column(String(8))  # по какому сценарию считали: base|bp
    sent_at = Column(DateTime)
    decided_at = Column(DateTime)
    comment = Column(Text)
    # ---- кэш итогов: список КП не должен пересчитывать сотни позиций
    calc_base_buyout = Column(Float)
    calc_bp_buyout = Column(Float)
    calc_weight_t = Column(Float)
    calc_at = Column(DateTime)

    positions = relationship("KpPosition", back_populates="document")


class KpPosition(Base):
    __tablename__ = "kp_positions"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("kp_documents.id"), index=True)
    raw_name = Column(Text, nullable=False)
    quantity = Column(Float)
    unit = Column(String(32))
    weight_kg = Column(Float)
    seller_price = Column(Float)
    item_id = Column(Integer, ForeignKey("items.id"))
    match_kind = Column(String(32))  # exact|alias|contains|tokens|reverse|manual|none
    match_score = Column(Float)
    lots_merged = Column(Integer, default=1)
    # ручные корректировки — высший приоритет
    manual_good_percent = Column(Float)
    manual_resale_price = Column(Float)
    manual_scrap_price = Column(Float)
    manual_quantity = Column(Float)
    manual_weight_kg = Column(Float)
    comment = Column(Text)
    updated_by = Column(String(64))
    updated_at = Column(DateTime)

    document = relationship("KpDocument", back_populates="positions")


class ApprovedValue(Base):
    """Нормативы: target_margin_percent, dismantling_cost_per_tonne, logistics…"""

    __tablename__ = "approved_values"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), nullable=False, index=True)
    value = Column(Float, nullable=False)
    unit = Column(String(32))
    scope = Column(String(128))  # категория/размер, если норматив не глобальный
    approved_by = Column(String(64))
    approved_at = Column(DateTime, default=utcnow)
    is_current = Column(Boolean, default=True)
    notes = Column(Text)


from sqlalchemy import event  # noqa: E402


@event.listens_for(ComponentYield, "before_insert")
@event.listens_for(ComponentYield, "before_update")
def _cy_normalize(mapper, connection, target):
    if target.item_name and not target.item_name_normalized:
        from ..normalize import normalize_name
        target.item_name_normalized = normalize_name(target.item_name)


@event.listens_for(ExpertYield, "before_insert")
@event.listens_for(ExpertYield, "before_update")
def _ey_normalize(mapper, connection, target):
    if target.item_name and not target.item_name_normalized:
        from ..normalize import normalize_name
        target.item_name_normalized = normalize_name(target.item_name)


class User(Base):
    """Пользователь сервиса. Пароль хранится только как pbkdf2-хеш."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    login = Column(String(64), unique=True, index=True, nullable=False)
    full_name = Column(String(128))
    password_hash = Column(String(256), nullable=False)
    salt = Column(String(64), nullable=False)
    role = Column(String(16), nullable=False, default="оценщик")  # оценщик|директор
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    last_login_at = Column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=utcnow)
    user = Column(String(64))
    action = Column(String(64), nullable=False)
    entity = Column(String(64))
    entity_id = Column(Integer)
    details = Column(Text)


# ------------------------------------------------- контрагенты и сделки

# Выгрузка «Движение ТМЦ» хранит контрагента в каждой строке, но импортёр цен
# брал из неё только последнюю цену и выбрасывал остальное. Между тем это
# готовый ответ на вопрос «кому продать»: 698 покупателей, 44 тыс. строк
# реализации, с датами, объёмами, ценами и менеджером сделки.


class Counterparty(Base):
    """Покупатель или поставщик из движений 1С."""

    __tablename__ = "counterparties"

    id = Column(Integer, primary_key=True)
    guid = Column(String(64), unique=True, index=True)
    name = Column(Text, nullable=False)
    name_normalized = Column(Text, index=True)
    inn = Column(String(16), index=True)  # если ИНН указан прямо в наименовании
    is_person = Column(Boolean, default=False)  # физлицо/розница/«нал»
    is_internal = Column(Boolean, default=False)  # «проект Базы» и подобные
    sales_count = Column(Integer, default=0)
    sales_revenue = Column(Float, default=0.0)
    purchase_count = Column(Integer, default=0)
    first_deal_at = Column(DateTime)
    last_deal_at = Column(DateTime, index=True)
    last_manager = Column(String(128))
    region = Column(String(120))
    site = Column(Text)
    contacts = Column(Text)
    comment = Column(Text)


class DealFact(Base):
    """Строка реализации или закупки: кто, что, когда, сколько и почём."""

    __tablename__ = "deal_facts"

    id = Column(Integer, primary_key=True)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), index=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    item_name = Column(Text)
    item_guid = Column(String(64), index=True)
    family = Column(String(64), index=True)
    size_key = Column(String(64), index=True)
    series_mark = Column(String(32), index=True)  # «вдм», «пэд», «тнжш»
    direction = Column(String(10), index=True)    # sale | purchase
    period = Column(DateTime, index=True)
    quantity = Column(Float)
    unit = Column(String(32))
    price = Column(Float)      # ₽ за единицу
    revenue = Column(Float)
    manager = Column(String(128))
    warehouse = Column(String(120))
    is_scrap = Column(Boolean, default=False, index=True)


# ------------------------------------------------------------- лидогенерация


class LeadCampaign(Base):
    """Поиск покупателей под конкретную позицию (или свободный запрос)."""

    __tablename__ = "lead_campaigns"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    query_name = Column(Text, nullable=False)
    unit = Column(String(32))
    quantity = Column(Float)
    family = Column(String(64))
    size_key = Column(String(64))
    created_at = Column(DateTime, default=utcnow)
    created_by = Column(String(64))
    status = Column(String(24), default="работает")  # работает|готово|ошибка
    stage = Column(String(120))
    error = Column(Text)
    # ценовой коридор: пол (металл), ориентир (наши продажи/ряд), потолок (рынок)
    price_floor = Column(Float)
    price_target = Column(Float)
    price_ceiling = Column(Float)
    price_unit = Column(String(32))
    price_basis = Column(Text)
    # профиль товара от ИИ: применение, отрасли-потребители, ключевые слова
    profile = Column(Text)
    leads_found = Column(Integer, default=0)


class Lead(Base):
    """Потенциальный покупатель с обоснованием и рекомендованной ценой."""

    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("lead_campaigns.id"), index=True)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), index=True)
    name = Column(Text, nullable=False)
    inn = Column(String(16))
    channel = Column(String(32), index=True)
    # свой покупатель | госзакупки | объявления | отраслевой поиск
    tier = Column(String(40))       # «покупал ровно это», «покупал ряд ВДМ»…
    region = Column(String(120))
    site = Column(Text)
    contacts = Column(Text)
    why = Column(Text)              # обоснование: почему этому можно продать
    expected_price = Column(Float)  # ₽ за единицу, которую он платил/платит
    price_unit = Column(String(32))
    price_basis = Column(Text)
    score = Column(Float, index=True)
    confidence = Column(String(16), default="medium")
    last_deal_at = Column(DateTime)
    manager = Column(String(128))   # наш менеджер, который его ведёт
    status = Column(String(16), default="новый", index=True)
    # новый | в работе | отказ | сделка
    # Цель вкладки — НОВЫЕ покупатели. Наши действующие контрагенты нужны как
    # ценовой ориентир и как напоминание, но в список лидов не идут: их и так
    # ведёт отдел продаж.
    is_existing = Column(Boolean, default=False, index=True)
    owner = Column(String(64))
    comment = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime)


class LeadEvidence(Base):
    """Доказательство под лидом: сделка, закупка, объявление, страница сайта."""

    __tablename__ = "lead_evidence"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), index=True, nullable=False)
    kind = Column(String(24))  # deal | tender | listing | source
    title = Column(Text)
    url = Column(Text)
    happened_at = Column(DateTime)
    amount = Column(Float)
    unit = Column(String(32))
    snippet = Column(Text)


# --------------------------------------------------- мониторинг объявлений

# Avito официального поиска по чужим объявлениям не даёт, а прямое чтение
# отдаёт 429 и запрещено robots.txt — поэтому единственный источник, который
# опрашивается по расписанию, это публичные Telegram-каналы: их веб-просмотр
# (t.me/s/<канал>) отдаёт сообщения без ключей и без входа в аккаунт.


class MonitorSource(Base):
    """Источник объявлений, опрашиваемый по расписанию."""

    __tablename__ = "monitor_sources"

    id = Column(Integer, primary_key=True)
    kind = Column(String(16), nullable=False, default="telegram")
    # telegram — имя канала без @; web — короткая метка (домен);
    # search — короткая метка запроса
    handle = Column(String(120), nullable=False)
    # что именно опрашиваем: URL страницы или текст поискового запроса
    target = Column(Text)
    title = Column(Text)
    is_active = Column(Boolean, default=True, index=True)
    added_by = Column(String(64))
    added_at = Column(DateTime, default=utcnow)
    last_polled_at = Column(DateTime)
    last_error = Column(Text)
    messages_seen = Column(Integer, default=0)
    listings_found = Column(Integer, default=0)
    notes = Column(Text)

    __table_args__ = (UniqueConstraint("kind", "handle",
                                       name="uq_monitor_source"),)


class MarketListing(Base):
    """Разобранное объявление: что, почём, продают или покупают."""

    __tablename__ = "market_listings"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("monitor_sources.id"), index=True)
    kind = Column(String(16), default="telegram")
    external_id = Column(String(64), index=True)  # id сообщения в канале
    posted_at = Column(DateTime, index=True)
    seen_at = Column(DateTime, default=utcnow)
    url = Column(Text)
    author = Column(String(160))
    text = Column(Text)
    # «продам» и «куплю» — это разные сущности: первое даёт ориентир цены,
    # второе само по себе является лидом
    direction = Column(String(8), index=True)  # sell | buy | None
    price = Column(Float)
    price_unit = Column(String(16))
    family = Column(String(64), index=True)
    size_key = Column(String(64), index=True)
    series_mark = Column(String(32), index=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    match_kind = Column(String(24))
    contacts = Column(Text)
    region = Column(String(120))
    # Сработавшие ключевые слова через «;». Нужны для позиций, которых
    # классификатор не знает: без них такое объявление не видно в ленте.
    matched_keywords = Column(Text, index=True)
    is_hidden = Column(Boolean, default=False, index=True)

    __table_args__ = (UniqueConstraint("source_id", "external_id",
                                       name="uq_listing_external"),)


class MonitorKeyword(Base):
    """Слово или фраза, за которой следим в лентах каналов.

    Классификатор знает только то, что уже встречалось в 1С. Ключевые слова —
    способ поймать остальное: новую марку, редкую позицию, чужой запрос
    «куплю» с формулировкой, которой у нас в справочнике нет.
    """

    __tablename__ = "monitor_keywords"

    id = Column(Integer, primary_key=True)
    phrase = Column(String(120), nullable=False, unique=True)
    whole_word = Column(Boolean, default=True)  # «НКТ» ≠ «нктовый»
    is_active = Column(Boolean, default=True, index=True)
    note = Column(Text)
    added_by = Column(String(64))
    added_at = Column(DateTime, default=utcnow)
    hits = Column(Integer, default=0)
    last_hit_at = Column(DateTime)
