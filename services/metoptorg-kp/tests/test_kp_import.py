"""Тесты импорта КП: коварности реальных файлов (синтетический файл)."""
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Item
from app.importers.kp import import_kp, match_item

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    for name in ["Труба НКТ 73 б/у", "Труба НКТ 89 б/у (тн)", "Труба",
                 "Двигатель ПЭД-45-117 б/у"]:
        from app.normalize import normalize_name
        s.add(Item(name=name, name_normalized=normalize_name(name),
                   unit="т", is_trade=True))
    s.commit()
    yield s
    s.close()


def make_tricky_xlsx(path: Path):
    """Титул, ВСЕГО перед данными, данные на втором листе, лоты, кол-во=вес."""
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Сводная"
    ws1.append(["Сводная информация"])
    ws2 = wb.create_sheet("Перечень")
    ws2.append(["Приложение № 1 к письму от 01.01.2026"])
    ws2.append([])
    ws2.append(["ВСЕГО", None, None, 100])
    ws2.append(["№", "Наименование ТМЦ (категория и вид металлолома, "
                     "прочие сведения)", "Ед. изм.", "Кол-во", "Вес, т"])
    ws2.append([1, "Труба НКТ 73 б/у", "шт", 50, 10.5])
    ws2.append([2, "Труба НКТ 73 б/у", "шт", 30, 6.3])
    ws2.append([3, "Труба НКТ 89 б/у", "Т", 0.829, None])  # кол-во = вес
    ws2.append(["ИТОГО", None, None, None, 17.629])
    wb.save(path)


def test_tricky_kp(session, tmp_path):
    f = tmp_path / "kp.xlsx"
    make_tricky_xlsx(f)
    doc, res = import_kp(session, str(f), "kp.xlsx")
    assert res.sheet == "Перечень"          # данные не на первом листе
    assert res.positions == 2               # лоты агрегированы
    by_name = {p.raw_name: p for p in doc.positions}
    nkt73 = by_name["Труба НКТ 73 б/у"]
    assert nkt73.quantity == 80             # 50 + 30
    assert nkt73.weight_kg == pytest.approx(16800)  # 10,5 + 6,3 т
    assert nkt73.lots_merged == 2
    assert "объединено лотов: 2" in nkt73.comment
    nkt89 = by_name["Труба НКТ 89 б/у"]
    assert nkt89.weight_kg == pytest.approx(829)    # кол-во в тоннах = вес
    # служебные строки отфильтрованы
    assert all("итого" not in p.raw_name.lower() for p in doc.positions)


def test_match_cascade(session):
    iid, kind, _ = match_item(session, "Труба НКТ 73 б/у")
    assert kind == "exact"
    iid, kind, _ = match_item(session, "труба нкт 89 б/у")
    assert kind in ("exact", "contains")    # суффикс (тн) нормализован
    iid, kind, _ = match_item(session, "НКТ-73 демонтированная лот 5")
    assert kind in ("tokens", "contains")
    # короткий генерик не матчится обратным вхождением
    iid, kind, _ = match_item(session, "Задвижка")
    assert kind == "none"


def test_model_key_matching(session):
    """Разный порядок слов и пунктуация внутри марки — одна и та же позиция."""
    from app.normalize import normalize_name
    from app.importers.kp import match_item, reset_match_cache

    for name in ("1ЭЦНД5-30 б/у (3 м.)", "Секция 115ЭЦН(НГА)5-80-4м б/у (шт)"):
        session.add(Item(name=name, name_normalized=normalize_name(name),
                         unit="шт", is_trade=True))
    session.commit()
    reset_match_cache()
    iid, kind, score = match_item(session, "Секция насосная 1ЭЦНД5-30, 3м б/у")
    assert kind == "model" and session.get(Item, iid).name == "1ЭЦНД5-30 б/у (3 м.)"
    iid, kind, _ = match_item(session, "Секция насосная 115ЭЦН(НГА) 5-80, 4м б/у")
    assert kind == "model"


def test_size_tokens_are_mandatory(session):
    """«Задвижка Ду100» не должна садиться на «Задвижку 50» — это другая цена."""
    from app.normalize import normalize_name
    from app.importers.kp import match_item, reset_match_cache

    name = "Задвижка 30с41нж ХЛ 50х1.6"
    session.add(Item(name=name, name_normalized=normalize_name(name),
                     unit="шт", is_trade=True))
    session.commit()
    reset_match_cache()
    iid, kind, _ = match_item(session, "Задвижка 30с41нж Ду100")
    assert iid is None and kind == "none"


def test_reverse_match_rejects_generic(session):
    """Специфичный ПЭД не должен садиться на генерик «Электродвигатель»."""
    from app.normalize import normalize_name
    from app.importers.kp import match_item, reset_match_cache

    session.add(Item(name="Электродвигатель", name_normalized="электродвигатель",
                     unit="т", is_trade=True))
    session.commit()
    reset_match_cache()
    iid, kind, _ = match_item(session, "Электродвигатель погружной ПЭД-117 б/у")
    assert iid is None, "генерик без типоразмера не должен подхватываться"


def test_unit_preference_picks_compatible_card(session):
    """При равных кандидатах выбираем карточку с той же единицей."""
    from app.normalize import normalize_name
    from app.importers.kp import match_item, reset_match_cache

    # обе карточки нормализуются в одно имя — выбор идёт только по единице
    for name, unit in (("Секция ЭЦН5-50 б/у (т)", "т"),
                       ("Секция ЭЦН5-50 б/у (шт)", "шт")):
        session.add(Item(name=name, name_normalized=normalize_name(name),
                         unit=unit, is_trade=True))
    session.commit()
    reset_match_cache()
    iid, _, _ = match_item(session, "Секция ЭЦН5-50 б/у", unit="Штука")
    assert session.get(Item, iid).unit == "шт"
    iid, _, _ = match_item(session, "Секция ЭЦН5-50 б/у", unit="т")
    assert session.get(Item, iid).unit == "т"
