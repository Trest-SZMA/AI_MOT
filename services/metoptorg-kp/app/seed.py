"""Стартовые нормативы и прайс лома («стартовый норматив — уточните по факту»)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .db.models import ApprovedValue, ExpertYield, ScrapPrice

START_NOTE = "стартовый норматив — уточните по факту разборов"

START_EXPERT_YIELDS = [
    # (block, category, size_key, good_percent, confidence)
    ("mot", "НКТ", None, 35.0, "medium"),        # НКТ «как есть», все диаметры
    ("mot", None, "труба-159", 70.0, "medium"),  # труба 159 — не НКТ!
    ("expert", None, "нкт-48", 45.0, "medium"),
    ("expert", None, "нкт-60", 48.0, "medium"),
    ("expert", None, "нкт-73", 52.0, "medium"),
    ("expert", None, "нкт-89", 55.0, "medium"),
    ("expert", None, "нкт-102", 57.0, "medium"),
    ("expert", None, "нкт-114", 58.0, "medium"),
    ("expert", None, "труба-159", 70.0, "medium"),
    ("expert", "труба", None, 50.0, "medium"),
]

# стартовый прайс лома, ₽/т (из реальных файлов компании, датируются файлами)
START_SCRAP_PRICES = [
    ("медь", "М5", 732_000.0),
    ("медь луженая", None, 732_000.0),
    ("алюминий", "А2/А18", 180_000.0),
    ("свинец", None, 120_000.0),
    ("чермет", "12А/20А/3А/5А", 25_000.0),
    ("латунь", "Л14/Л21", 450_000.0),
    ("нержавейка", "Б26", 80_000.0),
    ("16АЦ", "16АЦ", 5_000.0),
]

# ИИ-оценки делового выхода по семействам (блок ai — работает только в БП).
# Основа: Иванов В.П. и др., Полоцкий гос. ун-т — «около четверти деталей
# ремонтного фонда не изношены… могут быть использованы повторно, а около
# половины — после восстановления при себестоимости 15–30% цены новых»
# (https://elib.psu.by/bitstream/123456789/46512/1/74-79.pdf), плюс рыночные
# наблюдения (б/у ТМПН и СУ продаются целыми изделиями, в т.ч. metopt-torg.ru).
AI_NOTE = "ИИ-оценка по отраслевым данным — уточните по факту разборов"
AI_SRC_PSU = "https://elib.psu.by/bitstream/123456789/46512/1/74-79.pdf"
AI_YIELDS = [
    # (category/family, good_percent, confidence, source_url, пояснение)
    ("ПЭД", 25.0, "low", AI_SRC_PSU,
     "~25% ремфонда агрегатов пригодно повторно без ремонта (ПГУ)"),
    ("секция ЭЦН", 20.0, "low", AI_SRC_PSU,
     "высокий износ рабочих органов; слом вала — типовой отказ УЭЦН"),
    ("гидрозащита", 20.0, "low", AI_SRC_PSU,
     "уплотнения/диафрагмы — расходники; корпуса восстановимы"),
    ("станция управления", 40.0, "low",
     "https://metopt-torg.ru/services/prodazha-metalla/transformatory-tmpn-b-u/",
     "наземная электроника, активный вторичный рынок б/у СУ"),
    ("трансформатор", 50.0, "medium",
     "https://metopt-torg.ru/services/prodazha-metalla/transformatory-tmpn-b-u/",
     "ТМПН б/у продаются целыми изделиями (в т.ч. МетОптТорг)"),
    ("блок ТМС", 30.0, "low", AI_SRC_PSU,
     "электронный блок; оценка между СУ и погружным оборудованием"),
    ("статор", 0.0, "medium", AI_SRC_PSU,
     "статоры/обмотки — практически всегда лом (медь)"),
    ("обмотка", 0.0, "medium", AI_SRC_PSU,
     "обмоточная медь — лом по определению"),
]

START_NORMS = [
    ("target_margin_percent", 25.0, "%"),
    ("dismantling_cost_per_tonne", 3_000.0, "₽/т"),
    ("logistics_cost_per_tonne", 2_000.0, "₽/т"),
]


def seed(session: Session) -> None:
    # Проверяем КАЖДУЮ запись отдельно: импорт фактов из 1С удаляет норматив
    # семейства, а если факта по нему больше нет (например, весь НКТ ушёл в
    # «лом» после уточнения классификатора) — база осталась бы без выхода.
    for block, cat, size, pct, conf in START_EXPERT_YIELDS:
        exists = (session.query(ExpertYield)
                  .filter(ExpertYield.block == block,
                          ExpertYield.category == cat,
                          ExpertYield.size_key == size).first())
        if exists is None:
            session.add(ExpertYield(block=block, category=cat, size_key=size,
                                    good_percent=pct, scrap_percent=100 - pct,
                                    confidence=conf, notes=START_NOTE))
    if session.query(ExpertYield).filter(
            ExpertYield.notes.like("%" + AI_NOTE[:20] + "%")).first() is None:
        for cat, pct, conf, url, why in AI_YIELDS:
            session.add(ExpertYield(
                block="ai", category=cat, good_percent=pct,
                scrap_percent=100 - pct, confidence=conf, source_url=url,
                expert_name="ИИ-исследование",
                notes=f"{AI_NOTE}: {why}"))
    if session.query(ScrapPrice).first() is None:
        for material, grade, price in START_SCRAP_PRICES:
            session.add(ScrapPrice(material=material, grade=grade,
                                   price_per_tonne=price, source=START_NOTE))
    for key, value, unit in START_NORMS:
        if session.query(ApprovedValue).filter(
                ApprovedValue.key == key,
                ApprovedValue.is_current.is_(True)).first() is None:
            session.add(ApprovedValue(key=key, value=value, unit=unit,
                                      notes=START_NOTE))
    session.commit()
