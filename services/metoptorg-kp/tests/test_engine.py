"""Юнит-тесты сценарного расчёта (список из промпта)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    ComponentYield,
    ExpertYield,
    Item,
    KpDocument,
    KpPosition,
    PriceQuote,
    ScrapPrice,
)
from app.calc.engine import calc_position
from app.normalize import convert_quantity


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    # прайс лома
    s.add(ScrapPrice(material="чермет", price_per_tonne=25_000))
    s.add(ScrapPrice(material="медь", price_per_tonne=700_000))
    doc = KpDocument(title="тест")
    s.add(doc)
    s.commit()
    yield s
    s.close()


def make_pos(s, name="Труба стальная б/у", qty=10, unit="шт",
             weight_kg=5000.0, item=None, **manual):
    pos = KpPosition(document_id=1, raw_name=name, quantity=qty, unit=unit,
                     weight_kg=weight_kg,
                     item_id=item.id if item else None, **manual)
    s.add(pos)
    s.commit()
    return pos


def test_base_no_data_all_scrap(session):
    """База без данных → всё в лом по весу × цена чермета."""
    pos = make_pos(session, name="Задвижка чугунная б/у")
    calc = calc_position(session, pos)
    assert calc.base.good_percent == 0
    assert calc.base.scrap_value == pytest.approx(5.0 * 25_000)  # 5 т чермета
    assert "чермет" in calc.base.scrap_basis


def test_expert_beats_ai_in_bp(session):
    session.add(ExpertYield(block="ai", category="труба", good_percent=80))
    session.add(ExpertYield(block="expert", category="труба", good_percent=50))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у")
    calc = calc_position(session, pos)
    assert calc.bp.good_percent == 50
    assert "экспертная" in calc.bp.good_source


def test_manual_beats_everything(session):
    session.add(ExpertYield(block="expert", category="труба", good_percent=50))
    session.add(ExpertYield(block="mot", category="труба", good_percent=35))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у", manual_good_percent=77.0)
    calc = calc_position(session, pos)
    assert calc.base.good_percent == 77
    assert calc.bp.good_percent == 77
    assert calc.base.good_source == "ручная корректировка"


def test_base_only_mot(session):
    """База игнорирует expert/ai."""
    session.add(ExpertYield(block="expert", category="труба", good_percent=50))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у")
    calc = calc_position(session, pos)
    assert calc.base.good_percent == 0
    assert calc.bp.good_percent == 50


def test_unit_conversion():
    assert convert_quantity(250, "м", "км") == pytest.approx(0.25)
    assert convert_quantity(500, "кг", "т") == pytest.approx(0.5)
    assert convert_quantity(5, "кг", "м") is None  # несовместимы


def test_good_floor_by_scrap(session):
    """Годная часть без цены реализации → floor по лому (не дешевле полного лома)."""
    session.add(ExpertYield(block="mot", category="труба", good_percent=40))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у")
    calc = calc_position(session, pos)
    assert calc.base.good_floor_applied
    # реализация = лом 60% + годное 40% по цене лома = как полный лом
    assert calc.base.resale_total == pytest.approx(5.0 * 25_000)


def test_weight_from_kp_defines_unit_mass(session):
    pos = make_pos(session, qty=10, weight_kg=5000)
    calc = calc_position(session, pos)
    assert calc.base.unit_mass_kg == pytest.approx(500.0)
    assert calc.base.mass_source == "вес из файла КП"


def test_buyout_formula(session):
    """выкуп = реализация × (1−маржа) − разбор×доля_лома − логистика."""
    item = Item(name="Труба 89 б/у", name_normalized="труба 89 б/у",
                unit="шт", is_trade=True, family="труба")
    session.add(item)
    session.commit()
    session.add(PriceQuote(item_id=item.id, quote_type="sales_fact",
                           price=100_000, ttl_days=9999))
    session.add(ExpertYield(block="mot", category="труба", good_percent=40))
    from app.db.models import ApprovedValue
    session.add(ApprovedValue(key="target_margin_percent", value=25))
    session.add(ApprovedValue(key="dismantling_cost_per_tonne", value=3000))
    session.add(ApprovedValue(key="logistics_cost_per_tonne", value=2000))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у", qty=10, weight_kg=5000, item=item)
    calc = calc_position(session, pos)
    r = calc.base
    scrap_t = 5.0 * 0.6
    expected_resale = scrap_t * 25_000 + 4 * 100_000  # лом + 4 годных шт
    assert r.resale_total == pytest.approx(expected_resale)
    assert not r.good_floor_applied
    expected_buyout = expected_resale * 0.75 - 3000 * scrap_t - 2000 * scrap_t
    assert r.buyout_total == pytest.approx(expected_buyout)


def test_expert_composition_only_in_bp(session):
    """Экспертные составы применяются только в БП."""
    session.add(ComponentYield(item_name="Двигатель ПЭД-45 б/у",
                               material="медь", metal_mass_kg=100,
                               gross_weight_kg=1000, quantity=2, unit="шт",
                               block="expert", document_ref="x1"))
    session.add(ComponentYield(item_name="Двигатель ПЭД-45 б/у",
                               material="чермет", metal_mass_kg=800,
                               gross_weight_kg=1000, quantity=2, unit="шт",
                               block="expert", document_ref="x1"))
    session.commit()
    pos = make_pos(session, name="Двигатель ПЭД-45 б/у", qty=2, weight_kg=1000)
    calc = calc_position(session, pos)
    # база: состава mot нет → грубо чермет
    assert "грубо" in calc.base.scrap_basis
    assert calc.base.scrap_value == pytest.approx(1.0 * 25_000)
    # БП: экспертный состав — медь 10% + чермет 80%
    assert "состав" in calc.bp.scrap_basis
    expected = 1000 * 0.1 * 700 + 1000 * 0.8 * 25  # кг × ₽/кг
    assert calc.bp.scrap_value == pytest.approx(expected)


def test_sales_fact_beats_ai(session):
    item = Item(name="Труба 89 б/у", name_normalized="труба 89 б/у",
                unit="шт", is_trade=True, family="труба")
    session.add(item)
    session.commit()
    session.add(PriceQuote(item_id=item.id, quote_type="ai_estimate",
                           price=99_999, ttl_days=9999))
    session.add(PriceQuote(item_id=item.id, quote_type="sales_fact",
                           price=10_000, ttl_days=9999))
    session.add(ExpertYield(block="mot", category="труба", good_percent=50))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у", item=item)
    calc = calc_position(session, pos)
    assert "факт продаж" in calc.base.good_price_source


def test_size_norm_binding(session):
    """Типоразмер из названия: нкт-73."""
    session.add(ExpertYield(block="expert", size_key="нкт-73", good_percent=52))
    session.add(ExpertYield(block="expert", category="НКТ", good_percent=45))
    session.commit()
    pos = make_pos(session, name="Труба НКТ-73х5,5 б/у")
    calc = calc_position(session, pos)
    assert calc.bp.good_percent == 52
    assert "нкт-73" in calc.bp.good_source


def test_cable_by_section_with_length(session):
    """Кабель без справочника: расчёт по сечению, длина в метрах."""
    pos = make_pos(session, name="Кабель ВВГнг 3х4", qty=1000, unit="м",
                   weight_kg=None)
    calc = calc_position(session, pos)
    # 3×4×8,9 = 106,8 кг меди на км × 1 км × 700 ₽/кг
    assert calc.base.scrap_value == pytest.approx(106.8 * 700, rel=1e-3)
    assert "сечению" in calc.base.scrap_basis


def test_floor_applies_when_price_below_scrap(session):
    """Цена реализации ниже стоимости лома → берём лом (годное не дешевле лома)."""
    item = Item(name="Труба 89 б/у", name_normalized="труба 89 б/у",
                unit="шт", is_trade=True, family="труба")
    session.add(item)
    session.commit()
    # 1 000 ₽/шт при массе 500 кг/шт = 2 000 ₽/т — вдвое ниже чермета
    session.add(PriceQuote(item_id=item.id, quote_type="sales_fact",
                           price=1_000, ttl_days=9999))
    session.add(ExpertYield(block="mot", category="труба", good_percent=40))
    session.commit()
    pos = make_pos(session, name="Труба 89 б/у", qty=10, weight_kg=5000, item=item)
    calc = calc_position(session, pos)
    assert calc.base.good_floor_applied
    # годная часть 2 т × 25 000 ₽/т = 50 000 ₽ (а не 10 × 40% × 1 000 = 4 000)
    assert calc.base.good_value == pytest.approx(2.0 * 25_000)
    assert "floor" in calc.base.good_price_source


def test_analog_price_from_family_facts(session):
    """Своей цены нет — берётся медиана фактов продаж семейства (аналог)."""
    from app.calc.analog_price import reset_cache
    sold = []
    for i, price in enumerate((30_000, 40_000, 50_000, 60_000, 1_000_000)):
        it = Item(name=f"Двигатель ПЭД-{i} б/у", name_normalized=f"двигатель пэд-{i} б/у",
                  unit="шт", is_trade=True, family="ПЭД")
        session.add(it)
        session.commit()
        session.add(PriceQuote(item_id=it.id, quote_type="sales_fact",
                               price=price, unit="шт", ttl_days=9999))
        sold.append(it)
    target = Item(name="Двигатель ПЭД-99 б/у", name_normalized="двигатель пэд-99 б/у",
                  unit="шт", is_trade=True, family="ПЭД")
    session.add(target)
    session.add(ExpertYield(block="mot", category="ПЭД", good_percent=50))
    session.commit()
    reset_cache()
    pos = make_pos(session, name="Двигатель ПЭД-99 б/у", qty=10,
                   weight_kg=3000, item=target)
    calc = calc_position(session, pos)
    # медиана (50 000), а не среднее (236 000) — выброс 1 млн не тянет оценку
    assert calc.base.good_value == pytest.approx(10 * 0.5 * 50_000)
    assert "аналог" in calc.base.good_price_source


def test_family_classifier_scrap_wins_over_equipment():
    """«Лом ЭЦН» — это лом, а не насосная секция: иначе он попадёт в расчёт
    делового выхода оборудования и завысит его."""
    from app.normalize import classify_family
    assert classify_family("Лом ЭЦН (т)") == "лом"
    assert classify_family("Лом ПЭД (т)") == "лом"
    assert classify_family("Металлолом 5А") == "лом"      # одно слово
    assert classify_family("Стружка чёрная") == "лом"
    # само оборудование классифицируется по-прежнему
    assert classify_family("Секция ЭЦН5-50 б/у") == "секция ЭЦН"
    assert classify_family("Двигатель ПЭД-45 б/у") == "ПЭД"


def test_family_classifier_pipeline_fittings():
    """Крупнейшая группа ДХНО — арматура и фитинги — теперь распознаётся."""
    from app.normalize import classify_family
    assert classify_family("Клапан обратный Ду100") == "арматура трубопроводная"
    assert classify_family("Задвижка 30с41нж Ду200") == "арматура трубопроводная"
    assert classify_family("Отвод 90 Ду200") == "фитинги"
    assert classify_family("Тройник равнопроходной 159") == "фитинги"
    assert classify_family("Фланец плоский Ду150") == "фитинги"
    assert classify_family("Поковка ст.45") == "металлопрокат"
    assert classify_family("Подшипник 180309") == "уплотнения и подшипники"
    assert classify_family("Долото буровое 215.9") == "буровой инструмент"
    # труба не должна перехватываться фитингами
    assert classify_family("Труба 159х6 б/у") == "труба"


def test_non_trade_names_filtered():
    """Оргтехника и спецодежда не торговые, даже если лежат в группе ДХНО."""
    from app.importers.nomenclature import _is_trade
    for name in ("Картридж HP 85A", "Тонер Kyocera", "Костюм зимний",
                 "Мышь компьютерная", "Чай Greenfield"):
        assert _is_trade(name, "ДХНО") == (False, False), name
    assert _is_trade("Труба НКТ 73 б/у", "ДХНО")[0] is True


def test_seed_restores_missing_norms_individually(session):
    """Импорт фактов удаляет норматив семейства; если факта больше нет,
    посев обязан вернуть стартовое значение, а не пропустить его."""
    from app.seed import seed, START_NOTE
    seed(session)
    nkt = (session.query(ExpertYield)
           .filter(ExpertYield.block == "mot", ExpertYield.category == "НКТ")
           .first())
    assert nkt is not None and nkt.good_percent == 35
    # имитируем удаление норматива импортом фактов
    session.delete(nkt)
    session.commit()
    seed(session)
    restored = (session.query(ExpertYield)
                .filter(ExpertYield.block == "mot",
                        ExpertYield.category == "НКТ").first())
    assert restored is not None and restored.good_percent == 35
    assert restored.notes == START_NOTE
    # дубликатов не наплодили
    assert session.query(ExpertYield).filter(
        ExpertYield.block == "mot", ExpertYield.category == "НКТ").count() == 1


def test_regional_logistics_beats_global(session):
    """Ставка по месту погрузки применяется вместо общей медианы."""
    from app.db.models import ApprovedValue, KpDocument

    session.add(ApprovedValue(key="target_margin_percent", value=0))
    session.add(ApprovedValue(key="dismantling_cost_per_tonne", value=0))
    session.add(ApprovedValue(key="logistics_cost_per_tonne", value=2279))
    session.add(ApprovedValue(key="logistics_cost_per_tonne", scope="База Оса",
                              value=700))
    session.commit()

    pos = make_pos(session, name="Задвижка чугунная б/у", qty=1, weight_kg=1000)
    doc = session.get(KpDocument, pos.document_id)

    # без места погрузки — общая медиана: 1 т × 2 279 ₽
    calc = calc_position(session, pos)
    assert calc.base.buyout_total == pytest.approx(1.0 * 25_000 - 2279)

    doc.seller_region = "База Оса"
    session.commit()
    calc = calc_position(session, pos)
    assert calc.base.buyout_total == pytest.approx(1.0 * 25_000 - 700)

    # неизвестное место — снова общая медиана, а не ноль
    doc.seller_region = "Марс"
    session.commit()
    calc = calc_position(session, pos)
    assert calc.base.buyout_total == pytest.approx(1.0 * 25_000 - 2279)


def test_processing_cost_norm_from_facts(tmp_path):
    """Норматив разборки считается как затраты переработки / тоннаж лома,
    а виды работ с явно не-тоннами в количестве не публикуются."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.models import ApprovedValue, Base
    from app.importers.one_c import import_processing_costs

    cost = tmp_path / "cost.csv"
    cost.write_text(
        "Период,ВидРабот,Количество,СуммаГСМ,СуммаПРР,СуммаАмортизации,СуммаФОТ\n"
        "2024-01-01,Резка ручная,1000,0,0,0,600000\n"      # 600 ₽/т
        "2024-01-01,Разделка кабеля,900000,0,0,0,900000\n"  # единица не тонны
        "2023-01-01,Резка ручная,5000,0,0,0,9999999\n",     # другой год — не берём
        encoding="utf-8")
    mov = tmp_path / "mov.csv"
    mov.write_text(
        "Период,ХозяйственнаяОперация,Номенклатура,ЕдиницаИзмерения,Количество\n"
        "2024-02-01,Реализация,Лом 5А,т,3000\n"
        "2023-02-01,Реализация,Лом 5А,т,9000\n",
        encoding="utf-8")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    st = import_processing_costs(s, str(cost), str(mov), years=("2024",))
    # (600 000 + 900 000) / 3 000 т = 500 ₽/т
    assert st.notes["rate_per_tonne"] == 500
    assert st.notes["unit_unclear"] == ["Разделка кабеля"]
    norm = (s.query(ApprovedValue)
            .filter(ApprovedValue.key == "dismantling_cost_per_tonne",
                    ApprovedValue.is_current.is_(True)).one())
    assert norm.value == pytest.approx(500) and "факт" in norm.notes
    # ставка по кабелю не опубликована, по резке — да
    scopes = {a.scope for a in s.query(ApprovedValue).filter(
        ApprovedValue.key == "processing_cost_per_tonne")}
    assert scopes == {"Резка ручная"}
    s.close()


def test_card_weight_beats_family_profile(session):
    """Масса из карточки точнее профиля семейства и должна побеждать его."""
    from app.db.models import ExpertMetalProfile

    session.add(ExpertMetalProfile(family="ПЭД", material="чермет",
                                   kg_per_unit=310))
    item = Item(name="Двигатель ПЭД-45-117 б/у", unit="шт", is_trade=True,
                family="ПЭД", name_normalized="двигатель пэд-45-117 б/у")
    session.add(item)
    session.commit()

    # без веса карточки берётся профиль
    pos = make_pos(session, name="Двигатель ПЭД-45-117 б/у", qty=2,
                   weight_kg=None, item=item)
    calc = calc_position(session, pos)
    assert calc.base.unit_mass_kg == pytest.approx(310)
    assert "профиль семейства" in calc.base.mass_source

    # вес карточки перебивает профиль и виден в источнике
    item.unit_mass_kg = 780
    item.unit_mass_source = "ИИ-поиск (sonar)"
    item.unit_mass_confidence = "medium"
    session.commit()
    calc = calc_position(session, pos)
    assert calc.base.unit_mass_kg == pytest.approx(780)
    assert "вес карточки" in calc.base.mass_source

    # но вес из файла КП остаётся первичным
    pos.weight_kg = 2000
    session.commit()
    calc = calc_position(session, pos)
    assert calc.base.unit_mass_kg == pytest.approx(1000)
    assert calc.base.mass_source == "вес из файла КП"
