"""Модели БД MetalLomPro 2.0.

Принцип честности: каждая рыночная цифра хранит источник, момент сбора и
качество (live / cache / manual / calc) — фронт показывает бейдж на каждом значении.
"""
from __future__ import annotations
from datetime import datetime, date

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, Integer,
                        String, Text, UniqueConstraint, Index, JSON)

from app.db import Base


# ── Рынок ────────────────────────────────────────────────────────
class MarketQuote(Base):
    """Единица рыночных данных: индекс, курс, прайс завода, биржа."""
    __tablename__ = "market_quotes"
    id = Column(Integer, primary_key=True)
    metric = Column(String(64), nullable=False)      # lom3a / hms_turkey / usd_rub / copper_lme ...
    source = Column(String(128), nullable=False)     # Транслом / ЦБ РФ / mmk-vtormet.ru ...
    source_url = Column(String(512), default="")
    region = Column(String(64), default="")          # УРАЛ / ЮГ / РФ / ''
    city = Column(String(64), default="")
    basis = Column(String(16), default="")           # FCA / CPT_AUTO / CPT_RD / INDEX / EXCH / ''
    value = Column(Float, nullable=False)
    unit = Column(String(16), default="RUB/т")
    quality = Column(String(8), default="live")      # live | cache | manual | calc
    collected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    extra = Column(JSON, default=None)
    __table_args__ = (Index("ix_quote_metric_time", "metric", "collected_at"),)


class PhoneSurvey(Base):
    """Еженедельный обзвон Павла — главный live-источник закупочных цен заводов."""
    __tablename__ = "phone_survey"
    id = Column(Integer, primary_key=True)
    survey_date = Column(Date, nullable=False)
    region = Column(String(32), nullable=False)      # PERM / KOMI_PERM / HMAO / SOUTH
    basis = Column(String(16), nullable=False)       # FCA / CPT_AUTO / CPT_RD
    metal = Column(String(32), default="A3_SCRAP")
    price = Column(Float, nullable=False)
    comment = Column(String(512), default="")
    author = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class NewsItem(Base):
    __tablename__ = "news_items"
    id = Column(Integer, primary_key=True)
    source = Column(String(128))
    title = Column(String(512), nullable=False)
    url = Column(String(512), default="")
    collected_at = Column(DateTime, default=datetime.utcnow)
    # ИИ-разметка
    ai_impact = Column(Text, default="")             # влияние на рынок, кратко
    ai_direction = Column(Float, default=0.0)        # -1..+1 для цены лома
    ai_region = Column(String(64), default="")
    ai_done = Column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("title", name="uq_news_title"),)


class RadarCheck(Base):
    """Результат проверки одного индикатора раннего предупреждения."""
    __tablename__ = "radar_checks"
    id = Column(Integer, primary_key=True)
    checked_at = Column(DateTime, default=datetime.utcnow)
    indicator_n = Column(Integer, nullable=False)    # 1..7 рынок, 8+ внутренние
    name = Column(String(128))
    value_text = Column(String(128), default="")
    level = Column(String(8), default="ok")          # ok | warn | alarm | na
    message = Column(String(512), default="")


class AiOutput(Base):
    """Брифинги, сводки и ответы ИИ."""
    __tablename__ = "ai_outputs"
    id = Column(Integer, primary_key=True)
    kind = Column(String(16), nullable=False)        # briefing | weekly | chat | news
    content = Column(Text, nullable=False)
    model = Column(String(64), default="")
    question = Column(Text, default="")              # для chat
    created_at = Column(DateTime, default=datetime.utcnow)


class ForecastSnapshot(Base):
    """Слепок прогноза факторной модели (для истории точности)."""
    __tablename__ = "forecast_snapshots"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    series = Column(String(32))
    payload = Column(JSON)


# ── Данные компании (1С) ─────────────────────────────────────────
class CompanySale(Base):
    """Выручка на загрузку: фактические продажи."""
    __tablename__ = "co_sales"
    id = Column(Integer, primary_key=True)
    period = Column(Date, nullable=False)
    buyer = Column(String(256), default="")
    item = Column(String(256), default="")
    item_group = Column(String(32), default="")      # chermet / pipes / cvetmet / cable / other
    warehouse = Column(String(256), default="")
    division = Column(String(128), default="")
    operation = Column(String(128), default="")
    qty_t = Column(Float, default=0)
    revenue_rub = Column(Float, default=0)
    cost_rub = Column(Float, default=0)
    row_hash = Column(String(32), unique=True)


class CompanyTrip(Base):
    """Отвесная: рейс с весами, километражем и стоимостью доставки."""
    __tablename__ = "co_trips"
    id = Column(Integer, primary_key=True)
    load_date = Column(Date)
    from_place = Column(String(256), default="")
    recipient = Column(String(256), default="")
    km = Column(Float, default=0)
    item = Column(String(256), default="")
    item_group = Column(String(32), default="")
    weight_ttn = Column(Float, default=0)
    weight_net = Column(Float, default=0)
    cost_rub = Column(Float, default=0)              # стоимость перевозки
    delivery_kind = Column(String(64), default="")
    carrier = Column(String(256), default="")
    vehicle = Column(String(128), default="")
    warehouse = Column(String(256), default="")
    division = Column(String(128), default="")
    guid = Column(String(40), default="")
    row_hash = Column(String(32), unique=True)
    __table_args__ = (Index("ix_trip_date", "load_date"),)


class CompanyStock(Base):
    """Движение ТМЦ: приход/расход для расчёта остатков по складам."""
    __tablename__ = "co_stock_moves"
    id = Column(Integer, primary_key=True)
    period = Column(Date)
    warehouse = Column(String(256), default="")
    item = Column(String(256), default="")
    item_group = Column(String(32), default="")
    analytics_group = Column(String(256), default="")
    operation = Column(String(128), default="")
    qty = Column(Float, default=0)                   # + приход / - расход
    value_rub = Column(Float, default=0)
    division = Column(String(128), default="")
    row_hash = Column(String(32), unique=True)
    __table_args__ = (Index("ix_stock_wh_item", "warehouse", "item"),)


class RailShipment(Base):
    """Повагонная ЖД-отправка лома (выгрузка ИВМ, код груза 31607)."""
    __tablename__ = "rail_shipments"
    id = Column(Integer, primary_key=True)
    ship_date = Column(Date, nullable=False)
    transport_kind = Column(String(32), default="")     # внутренняя/экспорт
    from_region = Column(String(128), default="")       # область отправления
    from_station = Column(String(128), default="")
    to_region = Column(String(128), default="")
    to_station = Column(String(128), default="")
    consignor = Column(String(256), default="")         # грузоотправитель
    consignee = Column(String(256), default="")         # грузополучатель (завод)
    plant_id = Column(String(32), default="")           # маппинг на наш справочник
    wagon_kind = Column(String(64), default="")
    wagon_owner = Column(String(128), default="")
    wagon_operator = Column(String(128), default="")
    tons = Column(Float, default=0)
    tariff_rub = Column(Float, default=0)               # тариф РЖД за отправку
    wagons = Column(Integer, default=0)
    row_hash = Column(String(32), unique=True)
    __table_args__ = (Index("ix_rail_route", "from_region", "consignee"),
                      Index("ix_rail_date", "ship_date"),)


class WeeklySnapshot(Base):
    """Слепок еженедельного отчёта (MMI): матрицы, отгрузки, цены — JSON."""
    __tablename__ = "weekly_snapshots"
    id = Column(Integer, primary_key=True)
    kind = Column(String(32), nullable=False)           # mmi_week
    week_start = Column(Date, nullable=False)
    source = Column(String(128), default="MMI")
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("kind", "week_start", name="uq_week_kind"),)


class ScrapeSource(Base):
    """Реестр сайтов для обхода: заводы, ломоприёмки, локальные заготовители.

    Пополняется вручную и ИИ-разведкой (discover). Обходится краулером:
    эвристика → LLM-извлечение. Статистика успехов/провалов — прямо здесь.
    """
    __tablename__ = "scrape_sources"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False)
    url = Column(String(512), nullable=False, unique=True)
    region = Column(String(32), default="")          # PERM/KOMI_NORTH/HMAO/SOUTH/'' (РФ)
    city = Column(String(64), default="")
    kind = Column(String(16), default="local")       # plant | local
    enabled = Column(Boolean, default=True)
    added_by = Column(String(32), default="manual")  # manual | seed | ai_discover
    last_ok = Column(DateTime, default=None)
    last_price = Column(Float, default=None)
    last_method = Column(String(16), default="")     # heuristic | llm
    fail_count = Column(Integer, default=0)
    note = Column(String(256), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class ImportLog(Base):
    __tablename__ = "import_log"
    id = Column(Integer, primary_key=True)
    filename = Column(String(256))
    kind = Column(String(32))                        # sales / trips / stock / unknown
    rows_total = Column(Integer, default=0)
    rows_loaded = Column(Integer, default=0)
    rows_skipped = Column(Integer, default=0)
    status = Column(String(16), default="ok")        # ok | error | skipped
    message = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
