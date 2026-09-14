"""Тесты импортёров на реальных файлах со сверкой контрольных итогов."""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CableBrand, ComponentYield
from app.calc.cable import parse_cable_marking, CableEstimate
from app.importers.cable_files import (
    import_brand_table,
    import_valuation_sheet,
    import_vnpz_sheet,
)
from app.importers.metal_yields import (
    XlsxScanError,
    detect_xlsx_scan,
    import_yields_file,
    rebuild_profiles,
)

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


# ---------------------------------------------------------------- кабель


def test_marking_parser():
    mk = parse_cable_marking("Кабель ВВГнг(А)-LS 3х16")
    assert mk.core_groups == [(3, 16.0)]
    mk = parse_cable_marking("Кабель ВВГнг(А)-LS 3х150+1х70")
    assert mk.total_section_mm2 == 3 * 150 + 70
    mk = parse_cable_marking("Кабель МКЭШВнг-LS 4х2х0,52")
    assert mk.core_groups == [(8, 0.52)]
    assert mk.conductor == "медь луженая"


def test_analytic_copper_ppu():
    """ППИ-У жила 2 мм → 3,14 мм² → ≈27,9 кг/км (проверено файлом)."""
    mk = parse_cable_marking("Кабель ППИ-У 1х3,14")
    assert mk.conductor_kg_per_km == pytest.approx(27.9, rel=0.01)


def test_analytic_aluminum():
    mk = parse_cable_marking("Кабель АВВГ 1х16")
    assert mk.conductor == "алюминий"
    assert mk.conductor_kg_per_km == pytest.approx(43.2)


def test_value_per_tonne():
    est = CableEstimate(metals_kg_per_km={"медь": 44.5},
                        cable_kg_per_km=190.0, source="brand_ref",
                        confidence="medium")
    # = учетная цена «КГ 2*2,5»: 44,5 кг меди / 190 кг кабеля × 735 ₽/кг
    v = est.value_per_tonne({"медь": 735_000})
    assert v == pytest.approx(172_144.7, rel=1e-3)


def test_ocenka_metalla_totals(session):
    """Контрольный итог файла: 1,2176 т металла (допуск 0,5%)."""
    st = import_valuation_sheet(session, str(SAMPLES / "ocenka_metalla.xlsx"),
                                "Оценка металла", "Оценка_металла")
    assert st.checks["metal_kg_sum"] == pytest.approx(1217.6, rel=0.005)
    assert st.imported == 77


def test_nnos_totals(session):
    """ННОС 29.12.25: медь 1102,4 + луженая 24,76 + алюминий 90,4 кг."""
    st = import_valuation_sheet(session, str(SAMPLES / "kabel_normy.xlsx"),
                                "ННОС 29.12.25", "ННОС")
    assert st.checks["metal_kg_sum"] == pytest.approx(
        1102.44 + 24.76 + 90.4, rel=0.005)
    by_mat = {}
    for y in session.query(ComponentYield):
        by_mat[y.material] = by_mat.get(y.material, 0) + y.metal_mass_kg
    assert by_mat["медь"] == pytest.approx(1102.44, rel=0.005)
    assert by_mat["медь луженая"] == pytest.approx(24.76, rel=0.005)
    assert by_mat["алюминий"] == pytest.approx(90.4, rel=0.005)


def test_brand_table_bad_rows_skipped(session):
    st = import_brand_table(session, str(SAMPLES / "kabel_normy.xlsx"),
                            "Таблица по маркам", "Таблица по маркам")
    assert st.imported > 150
    assert any("АВБШв4х90" in x for x in st.skipped)  # «нет такого кабеля»


def test_vnpz_skips_div0(session):
    st = import_vnpz_sheet(session, str(SAMPLES / "kabel_normy.xlsx"),
                           "ВНПЗ медь", "медь", "ВНПЗ медь")
    assert st.imported > 0
    assert len(st.skipped) > 0  # #DIV/0! и «недостаточно информации»
    # URL сохраняются
    with_url = session.query(CableBrand).filter(
        CableBrand.source_url.isnot(None)).count()
    assert with_url > 0


# ---------------------------------------------------------------- выходы


def test_scan_detected():
    assert detect_xlsx_scan(str(SAMPLES / "vyhody_16_18_scan.xlsx"))
    assert not detect_xlsx_scan(str(SAMPLES / "vyhody_39_40.xlsx"))


def test_scan_raises(session):
    with pytest.raises(XlsxScanError):
        import_yields_file(session, str(SAMPLES / "vyhody_16_18_scan.xlsx"),
                           "16-18")


def test_39_40_two_lists_and_kg_per_unit(session):
    st = import_yields_file(session, str(SAMPLES / "vyhody_39_40.xlsx"),
                            "выходы 39-40")
    assert set(st.lists) == {"39", "40"}
    # Гидрозащита ПР92Д перечня 40: 3,976 т / 71 шт ≈ 56 кг/шт
    row = (session.query(ComponentYield)
           .filter(ComponentYield.item_name.like("Гидрозащита ПР92Д%"),
                   ComponentYield.notes == "перечень 40",
                   ComponentYield.material == "чермет")
           .order_by(ComponentYield.id).first())
    assert row is not None
    assert row.metal_mass_kg / row.quantity == pytest.approx(56.0, rel=0.01)


def test_sectional_41_51(session):
    st = import_yields_file(session, str(SAMPLES / "vyhody_41_51.xlsx"),
                            "выходы 41-51")
    assert "42" in st.lists and "44" in st.lists
    assert st.batches > 40


def test_profiles_rebuilt(session):
    import_yields_file(session, str(SAMPLES / "vyhody_39_40.xlsx"), "39-40")
    n = rebuild_profiles(session)
    assert n > 0
    from app.db.models import ExpertMetalProfile
    p = session.query(ExpertMetalProfile).filter_by(
        family="гидрозащита", material="чермет").first()
    assert p is not None and p.kg_per_unit and 40 < p.kg_per_unit < 90
