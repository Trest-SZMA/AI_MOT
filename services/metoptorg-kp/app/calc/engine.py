"""Расчётный движок: два сценария (база / БП) и предельная цена выкупа.

Требования директора (дословно):
- База: только наши данные (mot). Нет делового выхода → всё в лом.
  Нет состава → лом грубо: масса × цена чермета.
- БП: экспертные превалируют (expert → ai → mot).
- Ручные корректировки — высший приоритет в обоих сценариях.
- выкуп = реализация × (1 − маржа) − разбор × доля_лома − логистика.
- Годная часть без цены реализации — floor по цене лома (с пометкой).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db.models import (
    ApprovedValue,
    KpDocument,
    ComponentYield,
    ExpertMetalProfile,
    ExpertYield,
    Item,
    KpPosition,
    PriceQuote,
    ScrapPrice,
)
from ..normalize import classify_family, extract_size_key, normalize_name
from . import price_ladder
from .analog_price import find_analog
from .cable import estimate_by_section
from ..importers.cable_files import find_brand

DEFAULT_MARGIN = 0.25  # стартовая маржа 25%
DEFAULT_FERROUS = "чермет"


@dataclass
class ScenarioResult:
    scenario: str  # 'base' | 'bp'
    good_percent: float
    good_source: str
    unit_mass_kg: float | None
    mass_source: str
    scrap_value: float  # ₽ за всю ломовую часть позиции
    scrap_basis: str
    good_value: float  # ₽ за деловую часть
    good_price_source: str
    good_floor_applied: bool = False
    resale_total: float = 0.0  # реализация всего
    buyout_total: float = 0.0  # предельная цена выкупа всего
    buyout_per_unit: float | None = None
    warnings: list[str] = field(default_factory=list)

    def finalize(self, quantity: float | None, margin: float,
                 dismantling_per_tonne: float, logistics_per_tonne: float,
                 scrap_share_mass_t: float):
        self.resale_total = self.scrap_value + self.good_value
        dismantle_cost = dismantling_per_tonne * scrap_share_mass_t
        logistics_cost = logistics_per_tonne * scrap_share_mass_t
        self.buyout_total = max(
            0.0, self.resale_total * (1 - margin) - dismantle_cost - logistics_cost)
        if quantity:
            self.buyout_per_unit = self.buyout_total / quantity


def _current_scrap_prices(session: Session,
                          as_of: dt.datetime | None = None) -> dict[str, float]:
    """Прайс лома: текущий либо действовавший на дату as_of (воспроизводимость)."""
    if as_of is None:
        rows = session.query(ScrapPrice).filter(ScrapPrice.is_current.is_(True)).all()
        return {sp.material: sp.price_per_tonne for sp in rows}
    prices: dict[str, tuple[dt.datetime, float]] = {}
    for sp in session.query(ScrapPrice).all():
        vf = sp.valid_from
        if vf is None:
            continue
        if vf.tzinfo is not None:
            vf = vf.replace(tzinfo=None)
        if vf > as_of:
            continue  # цена установлена позже даты оценки
        cur = prices.get(sp.material)
        if cur is None or vf > cur[0]:
            prices[sp.material] = (vf, sp.price_per_tonne)
    return {m: p for m, (_, p) in prices.items()}


def _norm_value(session: Session, key: str, default: float,
                scope: str | None = None) -> float:
    """Норматив: сначала привязанный к месту (scope), затем глобальный."""
    if scope:
        row = (session.query(ApprovedValue)
               .filter(ApprovedValue.key == key,
                       ApprovedValue.is_current.is_(True),
                       ApprovedValue.scope == scope)
               .order_by(ApprovedValue.approved_at.desc()).first())
        if row:
            return row.value
    row = (session.query(ApprovedValue)
           .filter(ApprovedValue.key == key,
                   ApprovedValue.is_current.is_(True),
                   ApprovedValue.scope.is_(None))
           .order_by(ApprovedValue.approved_at.desc()).first())
    return row.value if row else default


def _good_percent(session: Session, pos: KpPosition, item: Item | None,
                  scenario: str) -> tuple[float, str]:
    """Приоритет: ручная → [БП: expert→ai→mot; база: mot] → норматив категории → 0."""
    if pos.manual_good_percent is not None:
        return pos.manual_good_percent, "ручная корректировка"
    blocks = ["expert", "ai", "mot"] if scenario == "bp" else ["mot"]
    name = item.name if item else pos.raw_name
    size = (item.size_key if item else None) or extract_size_key(pos.raw_name)
    family = (item.family if item else None) or classify_family(pos.raw_name)
    norm = normalize_name(name)

    for block in blocks:
        q = session.query(ExpertYield).filter(ExpertYield.block == block)
        # 1) позиционная привязка (бьёт размерную независимо от блока)
        if item is not None:
            r = q.filter(ExpertYield.item_id == item.id).first()
            if r:
                return r.good_percent, f"{_block_label(block)} · позиция"
        row = q.filter(ExpertYield.item_name_normalized == norm).first()
        if row:
            return row.good_percent, f"{_block_label(block)} · позиция"
        # 2) типоразмер
        if size:
            row = q.filter(ExpertYield.size_key == size).first()
            if row:
                return row.good_percent, f"{_block_label(block)} · типоразмер {size}"
        # 3) категория/семейство
        if family:
            row = q.filter(ExpertYield.category == family).first()
            if row:
                return row.good_percent, f"{_block_label(block)} · категория {family}"
    return 0.0, "нет данных — всё в лом"


def _block_label(block: str) -> str:
    return {"mot": "наш норматив", "expert": "экспертная оценка",
            "ai": "ИИ-оценка"}[block]


def _unit_mass(session: Session, pos: KpPosition,
               item: Item | None) -> tuple[float | None, str]:
    """Масса единицы: вес КП / кол-во (первичен) → состав → весовая единица."""
    weight = pos.manual_weight_kg if pos.manual_weight_kg is not None else pos.weight_kg
    qty = pos.manual_quantity if pos.manual_quantity is not None else pos.quantity
    if weight and qty:
        return weight / qty, "вес из файла КП"
    unit = (pos.unit or "").strip().lower()
    if unit in ("т", "тн", "тонна") and qty:
        return 1000.0, "весовая учётная единица (1 т)"
    if unit == "кг" and qty:
        return 1.0, "весовая учётная единица (1 кг)"
    # состав справочника: gross-масса штучной партии
    name = item.name if item else pos.raw_name
    rows = _find_yields(session, item, name)
    for r in rows:
        if r.quantity and (r.unit or "").lower().startswith("шт") and r.gross_weight_kg:
            return r.gross_weight_kg / r.quantity, "состав справочника (gross)"
    # масса из карточки (ручная или найденная в открытых источниках) — точнее
    # профиля семейства, который усредняет разные модели одного типа
    if item is not None and item.unit_mass_kg:
        src = item.unit_mass_source or "карточка"
        conf = item.unit_mass_confidence
        return item.unit_mass_kg, f"вес карточки · {src}" + (
            f" ({conf})" if conf else "")
    # профиль семейства: сумма кг/шт
    family = (item.family if item else None) or classify_family(name)
    if family:
        profs = session.query(ExpertMetalProfile).filter(
            ExpertMetalProfile.family == family,
            ExpertMetalProfile.kg_per_unit.isnot(None)).all()
        if profs:
            return sum(p.kg_per_unit for p in profs), f"профиль семейства «{family}»"
    return None, "масса не определена"


def _find_yields(session: Session, item: Item | None,
                 raw_name: str) -> list[ComponentYield]:
    """Составы: по карточке и по имени позиции КП (нормализованно)."""
    rows: list[ComponentYield] = []
    if item is not None:
        rows += session.query(ComponentYield).filter(
            ComponentYield.item_id == item.id).all()
    norm = normalize_name(raw_name)
    seen = {r.id for r in rows}
    rows += [r for r in session.query(ComponentYield).filter(
        ComponentYield.item_name_normalized == norm).all() if r.id not in seen]
    return rows


def _scrap_composition(session: Session, pos: KpPosition, item: Item | None,
                       scenario: str,
                       scrap_prices: dict[str, float]) -> tuple[dict[str, float], str] | None:
    """Доли металлов ломовой части (material → доля массы).

    База: только наши составы (mot). БП: экспертные → наши → профиль семейства.
    """
    name = item.name if item else pos.raw_name
    rows = _find_yields(session, item, name)
    blocks = ["expert", "mot", "ai"] if scenario == "bp" else ["mot"]
    for block in blocks:
        brows = [r for r in rows if r.block == block]
        if brows:
            total = sum(r.metal_mass_kg for r in brows)
            gross = next((r.gross_weight_kg for r in brows if r.gross_weight_kg), None)
            base = gross or total
            if base:
                comp = {}
                for r in brows:
                    comp[r.material] = comp.get(r.material, 0.0) + r.metal_mass_kg / base
                return comp, f"состав ({_block_label(block)})"
    if scenario == "bp":
        family = (item.family if item else None) or classify_family(name)
        if family:
            profs = session.query(ExpertMetalProfile).filter(
                ExpertMetalProfile.family == family).all()
            if profs:
                comp = {p.material: (p.percent_of_mass or 0.0) for p in profs
                        if p.percent_of_mass}
                if comp:
                    return comp, f"профиль семейства «{family}»"
    return None


def _scrap_value_of_mass(mass_kg: float, session: Session, pos: KpPosition,
                         item: Item | None, scenario: str,
                         scrap_prices: dict[str, float]) -> float:
    """Стоимость массы как лома — по составу позиции, иначе по чермету."""
    if mass_kg <= 0:
        return 0.0
    if pos.manual_scrap_price is not None:
        return mass_kg * pos.manual_scrap_price / 1000.0
    comp = _scrap_composition(session, pos, item, scenario, scrap_prices)
    if comp is not None:
        shares, _ = comp
        return sum(mass_kg * share * scrap_prices.get(mat, 0.0) / 1000.0
                   for mat, share in shares.items())
    return mass_kg * scrap_prices.get(DEFAULT_FERROUS, 0.0) / 1000.0


def _resale_price(session: Session, pos: KpPosition,
                  item: Item | None) -> tuple[float | None, str]:
    """Цена реализации за единицу — лучшая ступень каскада (price_ladder).

    ручная → своя продажа → карточка-двойник → тот же габарит → модельный ряд →
    закупка × наценка → медиана семейства → внешние источники.
    """
    if pos.manual_resale_price is not None:
        return pos.manual_resale_price, "ручная корректировка"
    name = item.name if item is not None else pos.raw_name
    unit = (item.unit if item is not None else None) or pos.unit
    cands = price_ladder.candidates(session, name, unit, item)
    if cands:
        return cands[0].price, cands[0].label
    # последняя попытка — старый аналог по семейству без привязки к единице ряда
    family = (item.family if item else None) or classify_family(pos.raw_name)
    size = (item.size_key if item else None) or extract_size_key(pos.raw_name)
    an = find_analog(session, family, size, unit)
    if an:
        return an.price, an.label
    return None, ("цена реализации не найдена" if item is not None
                  else "нет карточки")


@dataclass
class PositionCalc:
    base: ScenarioResult
    bp: ScenarioResult
    quantity: float | None
    total_weight_kg: float | None
    cable_value_per_tonne: float | None = None
    cable_source: str | None = None


def calc_position(session: Session, pos: KpPosition,
                  as_of: dt.datetime | None = None) -> PositionCalc:
    """Расчёт позиции. as_of — дата оценки (цены лома на эту дату)."""
    item = session.get(Item, pos.item_id) if pos.item_id else None
    scrap_prices = _current_scrap_prices(session, as_of)
    margin = _norm_value(session, "target_margin_percent", DEFAULT_MARGIN * 100) / 100.0
    dism = _norm_value(session, "dismantling_cost_per_tonne", 0.0)
    # место погрузки КП задаёт ставку перевозки; без него — общая медиана
    region = None
    if pos.document_id:
        doc = session.get(KpDocument, pos.document_id)
        region = doc.seller_region if doc else None
    logi = _norm_value(session, "logistics_cost_per_tonne", 0.0, scope=region)

    qty = pos.manual_quantity if pos.manual_quantity is not None else pos.quantity
    results = {}
    cable_vpt, cable_src = None, None

    # общие для сценариев данные считаем один раз
    unit_mass, mass_src = _unit_mass(session, pos, item)
    shared_family = (item.family if item else None) or classify_family(pos.raw_name)
    shared_cable_est = (_cable_estimate(session, pos, item)
                        if shared_family == "кабель" else None)

    for scenario in ("base", "bp"):
        good_pct, good_src = _good_percent(session, pos, item, scenario)
        weight = (pos.manual_weight_kg if pos.manual_weight_kg is not None
                  else pos.weight_kg)
        total_mass_kg = weight or (unit_mass * qty if unit_mass and qty else None)

        res = ScenarioResult(scenario=scenario, good_percent=good_pct,
                             good_source=good_src, unit_mass_kg=unit_mass,
                             mass_source=mass_src, scrap_value=0.0,
                             scrap_basis="", good_value=0.0,
                             good_price_source="")

        # --- кабельная ветка: ценность = металлы на км/тонну
        is_cable = shared_family == "кабель"
        cable_est = None
        if is_cable:
            cable_est = shared_cable_est
            unit = (pos.unit or "").strip().lower()
            length_km = None
            if qty:
                if unit in ("м", "метр", "пог.м"):
                    length_km = qty / 1000.0
                elif unit in ("км",):
                    length_km = qty
            if cable_est is not None:
                if length_km is not None:
                    # длина известна → металл напрямую: кг/км × км
                    value = sum(
                        kg * length_km * scrap_prices.get(mat, 0.0) / 1000.0
                        for mat, kg in cable_est.metals_kg_per_km.items())
                    res.scrap_value = value
                    res.scrap_basis = f"кабель: {cable_est.source} ({cable_est.notes})"
                    if cable_est.cable_kg_per_km and total_mass_kg is None:
                        total_mass_kg = cable_est.cable_kg_per_km * length_km
                    vpt = cable_est.value_per_tonne(scrap_prices)
                    cable_vpt, cable_src = vpt, cable_est.source
                elif total_mass_kg:
                    vpt = cable_est.value_per_tonne(scrap_prices)
                    if vpt is not None:
                        res.scrap_value = vpt * total_mass_kg / 1000.0
                        res.scrap_basis = (f"кабель: {cable_est.source} "
                                           f"({cable_est.notes})")
                        cable_vpt, cable_src = vpt, cable_est.source
                    else:
                        cable_est = None
                        res.warnings.append(
                            "кабель: нет массы кг/км — ₽/т не рассчитан")
                else:
                    cable_est = None
        # --- обычная ветка
        if not is_cable or cable_est is None:
            scrap_mass_kg = (total_mass_kg or 0.0) * (1 - good_pct / 100.0)
            comp = _scrap_composition(session, pos, item, scenario, scrap_prices)
            manual_scrap = pos.manual_scrap_price
            if comp is not None:
                shares, basis = comp
                value = 0.0
                for material, share in shares.items():
                    price = (manual_scrap if manual_scrap is not None
                             else scrap_prices.get(material, scrap_prices.get(
                                 DEFAULT_FERROUS, 0.0)))
                    value += scrap_mass_kg * share * price / 1000.0
                res.scrap_value = value
                res.scrap_basis = basis
            else:
                price = (manual_scrap if manual_scrap is not None
                         else scrap_prices.get(DEFAULT_FERROUS, 0.0))
                res.scrap_value = scrap_mass_kg * price / 1000.0
                res.scrap_basis = "грубо: масса × цена чермета"
                if not scrap_prices.get(DEFAULT_FERROUS) and manual_scrap is None:
                    res.warnings.append("нет цены чермета в прайсе")

        # --- деловая часть
        good_mass_kg = (total_mass_kg or 0.0) * good_pct / 100.0
        if good_pct > 0:
            # нижняя граница: годная часть стоит не меньше своего лома
            floor_value = _scrap_value_of_mass(
                good_mass_kg, session, pos, item, scenario, scrap_prices)
            price, src = (_resale_price(session, pos, item) if qty
                          else (None, "нет количества"))
            if price is not None and qty:
                priced = price * qty * good_pct / 100.0
                if priced >= floor_value:
                    res.good_value = priced
                    res.good_price_source = src
                else:
                    res.good_value = floor_value
                    res.good_price_source = (
                        f"floor по лому (цена реализации ниже лома: {src})")
                    res.good_floor_applied = True
            else:
                res.good_value = floor_value
                res.good_price_source = "floor по лому (нет цены реализации)"
                res.good_floor_applied = True

        scrap_share_t = ((total_mass_kg or 0.0) * (1 - good_pct / 100.0)) / 1000.0
        res.finalize(qty, margin, dism, logi, scrap_share_t)
        if total_mass_kg is None:
            res.warnings.append("масса позиции не определена")
        results[scenario] = res

    weight = pos.manual_weight_kg if pos.manual_weight_kg is not None else pos.weight_kg
    total_w = weight or (unit_mass * qty if unit_mass and qty else None)
    return PositionCalc(base=results["base"], bp=results["bp"], quantity=qty,
                        total_weight_kg=total_w,
                        cable_value_per_tonne=cable_vpt, cable_source=cable_src)


def _cable_estimate(session: Session, pos: KpPosition, item: Item | None):
    """Оценка кабеля: справочник марок → расчёт по сечению."""
    name = item.name if item else pos.raw_name
    brands = find_brand(session, name)
    if brands:
        # средние по маркам (вес зависит от завода)
        metals: dict[str, list[float]] = {}
        cables: list[float] = []
        for b in brands:
            for mat, v in (("медь", b.copper_kg_per_km),
                           ("медь луженая", b.tinned_copper_kg_per_km),
                           ("алюминий", b.aluminum_kg_per_km),
                           ("свинец", b.lead_kg_per_km),
                           ("16АЦ", b.armor_kg_per_km)):
                if v:
                    metals.setdefault(mat, []).append(v)
            if b.cable_kg_per_km:
                cables.append(b.cable_kg_per_km)
        if metals and cables:
            from .cable import CableEstimate
            return CableEstimate(
                metals_kg_per_km={m: sum(v) / len(v) for m, v in metals.items()},
                cable_kg_per_km=sum(cables) / len(cables),
                source="brand_ref",
                confidence="medium",
                notes=f"справочник марок ({len(brands)} зап.)",
            )
    return estimate_by_section(name)
