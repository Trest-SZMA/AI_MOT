"""Импорт CSV-выгрузок 1С (волна 1).

- «Движение ТМЦ Факт Доработка»: Реализация → price_quotes(sales_fact);
  «Сборка (разборка) товаров» → component_yields блока mot (наши факт. выхода).
- «Закупки»: Закупка у поставщика → price_quotes(purchase_fact).

Все файлы большие (до 0,5 ГБ) — читаем потоково через csv.DictReader.
"""
from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db.models import (
    ApprovedValue,
    ComponentYield,
    ExpertMetalProfile,
    ExpertYield,
    Item,
    ItemAlias,
    PriceQuote,
)
from ..normalize import classify_family, normalize_name

log = logging.getLogger(__name__)

csv.field_size_limit(10_000_000)

EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

# материал ломовой позиции по имени (для выходов разборки)
_SCRAP_MATERIAL_RE: list[tuple[str, re.Pattern]] = [
    # «Лом меди» падал в чермет: «меди» не совпадало ни с «медь», ни с «медн»
    ("медь", re.compile(r"мед[ьияню]|медн|\bм5\b|\bм1\b|катанк", re.I)),
    ("латунь", re.compile(r"латун|\bл14|\bл21|\bл19", re.I)),
    ("алюминий", re.compile(r"алюмин|\bа2\b|\bа18\b|\bа5-2\b", re.I)),
    ("свинец", re.compile(r"свин", re.I)),
    ("нержавейка", re.compile(r"нержав|нирезист|\bб26|легиров", re.I)),
    ("16АЦ", re.compile(r"16ац|оцинков", re.I)),
    ("масло", re.compile(r"масло", re.I)),
    # чермет — последним (широкий)
    ("чермет", re.compile(r"лом|чермет|\b3а\b|\b5а\b|\b12а\b|\b20а\b|\b10а\b"
                          r"|\b5ар\b|пакет|стальн|чугун", re.I)),
]


def scrap_material(name: str) -> str | None:
    for material, pat in _SCRAP_MATERIAL_RE:
        if pat.search(name):
            return material
    return None


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ".").replace("\xa0", "").strip() or 0)
    except ValueError:
        return 0.0


def _date_from_reg(reg: str) -> str | None:
    m = re.search(r"от (\d{2}\.\d{2}\.\d{4})", reg or "")
    return m.group(1) if m else None


def _guid_map(session: Session) -> dict[str, int]:
    """guid (lower) → item_id, включая aliases."""
    m: dict[str, int] = {}
    for guid, iid in session.query(Item.guid, Item.id).filter(Item.guid.isnot(None)):
        m[guid.lower()] = iid
    for guid, iid in session.query(ItemAlias.guid, ItemAlias.item_id).filter(
            ItemAlias.guid.isnot(None)):
        m.setdefault(guid.lower(), iid)
    return m


@dataclass
class OneCStats:
    rows: int = 0
    imported: int = 0
    skipped_no_item: int = 0
    notes: dict = field(default_factory=dict)


# ------------------------------------------------------------- продажи


def import_sales_facts(session: Session, csv_path: str,
                       min_price: float = 1.0) -> OneCStats:
    """«Движение ТМЦ Факт Доработка»: строки Реализации с ценой.

    Берём последнюю (по периоду) цену на номенклатуру; средневзвешенную
    сохраняем в notes для сверки.
    """
    stats = OneCStats()
    guid_map = _guid_map(session)
    # guid → (period, price, unit, контрагент, qty_sum, revenue_sum)
    latest: dict[str, tuple] = {}
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            if not row.get("ХозяйственнаяОперация", "").startswith("Реализация"):
                continue
            price = _num(row.get("Цена"))
            qty = _num(row.get("Количество"))
            revenue = _num(row.get("Выручка"))
            guid = (row.get("НоменклатураГуид") or "").strip().lower()
            if not guid or guid == EMPTY_GUID or price < min_price:
                continue
            period = row.get("Период", "")
            unit = row.get("ЕдиницаИзмерения") or ""
            contragent = row.get("Контрагент") or ""
            cur = latest.get(guid)
            if cur is None or period > cur[0]:
                latest[guid] = (period, price, unit, contragent)
            t = totals[guid]
            t[0] += qty
            t[1] += revenue

    session.query(PriceQuote).filter(
        PriceQuote.quote_type == "sales_fact").delete(synchronize_session=False)
    for guid, (period, price, unit, contragent) in latest.items():
        item_id = guid_map.get(guid)
        if item_id is None:
            stats.skipped_no_item += 1
            continue
        qty_sum, rev_sum = totals[guid]
        avg = rev_sum / qty_sum if qty_sum else None
        session.add(PriceQuote(
            item_id=item_id,
            quote_type="sales_fact",
            price=price,
            unit=unit,
            source=f"1С Реализация · {contragent}",
            confidence="high",
            ttl_days=365,
            notes=(f"последняя продажа {period[:10]}"
                   + (f"; средневзв. {avg:,.0f}" if avg else "")),
        ))
        stats.imported += 1
    session.commit()
    return stats


# ------------------------------------------------------------- закупки


def import_purchase_facts(session: Session, csv_path: str) -> OneCStats:
    """«Закупки»: Закупка у поставщика → последняя цена без НДС за единицу."""
    stats = OneCStats()
    guid_map = _guid_map(session)
    latest: dict[str, tuple] = {}

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            if row.get("ХозяйственнаяОперация") not in (
                    "Закупка у поставщика", "Закупка через подотчетное лицо"):
                continue
            qty = _num(row.get("КоличествоОборот"))
            total = _num(row.get("СуммаБезНДСОборот"))
            guid = (row.get("НоменклатураГуид") or "").strip().lower()
            if not guid or guid == EMPTY_GUID or qty <= 0 or total <= 0:
                continue
            date = _date_from_reg(row.get("Регистратор", "")) or ""
            key_date = ".".join(reversed(date.split("."))) if date else ""
            cur = latest.get(guid)
            if cur is None or key_date > cur[0]:
                latest[guid] = (key_date, total / qty, row.get("ЕдИзм") or "",
                                row.get("Контрагент") or "", date)

    session.query(PriceQuote).filter(
        PriceQuote.quote_type == "purchase_fact").delete(synchronize_session=False)
    for guid, (_, price, unit, contragent, date) in latest.items():
        item_id = guid_map.get(guid)
        if item_id is None:
            stats.skipped_no_item += 1
            continue
        session.add(PriceQuote(
            item_id=item_id,
            quote_type="purchase_fact",
            price=price,
            unit=unit,
            source=f"1С Закупка · {contragent}",
            confidence="high",
            ttl_days=365,
            notes=f"последняя закупка {date}",
        ))
        stats.imported += 1
    session.commit()
    return stats


# ------------------------------------------------------------- разборка


def import_disassembly(session: Session, csv_path: str) -> OneCStats:
    """«Сборка (разборка) товаров»: вход (исходная) → выходы (новая).

    Количество и стоимость одной пары (документ, номенклатура, операция)
    разнесены по разным строкам — агрегируем. Составы пишем в блок mot,
    только если вход — оборудование (семейство не «лом»), а выходы — лом.
    """
    stats = OneCStats()
    docs: dict[str, dict] = defaultdict(
        lambda: {"in": defaultdict(lambda: [0.0, 0.0, ""]),
                 "out": defaultdict(lambda: [0.0, 0.0, ""]),
                 "reg": "", "date": ""})

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            if row.get("ХозяйственнаяОперация") != "Разборка на комплектующие":
                continue
            op = row.get("Операция", "")
            side = ("in" if "исходная" in op
                    else "out" if "новая" in op else None)
            if side is None:
                continue
            reg = row.get("РегистраторГуид") or row.get("Регистратор", "")
            name = (row.get("Номенклатура") or "").strip()
            if not name:
                continue
            d = docs[reg]
            d["reg"] = row.get("Регистратор", "")
            d["date"] = _date_from_reg(row.get("Регистратор", "")) or ""
            slot = d[side][name]
            slot[0] += _num(row.get("Количество"))
            slot[1] += _num(row.get("Стоимость"))
            slot[2] = row.get("ЕдиницаИзмерения") or ""

    session.query(ComponentYield).filter(
        ComponentYield.block == "mot",
        ComponentYield.notes.like("1С разборка%")).delete(synchronize_session=False)

    made = 0
    for reg_guid, d in docs.items():
        ins = {n: v for n, v in d["in"].items() if v[0] > 0}
        outs = {n: v for n, v in d["out"].items() if v[0] > 0}
        if len(ins) != 1 or not outs:
            continue  # многовходовые документы пропускаем (неоднозначно)
        in_name, (in_qty, _, in_unit) = next(iter(ins.items()))
        family = classify_family(in_name)
        if family in (None, "лом") or re.search(r"\bлом\b", in_name, re.I):
            continue  # переклассификация лома, не разборка оборудования
        in_unit_l = in_unit.strip().lower()
        gross_kg = in_qty * 1000.0 if in_unit_l in ("т", "тн", "тонна") else None
        quantity = in_qty if in_unit_l.startswith("шт") else None
        for out_name, (out_qty, _, out_unit) in outs.items():
            material = scrap_material(out_name)
            if material is None or material == "масло":
                continue
            out_unit_l = out_unit.strip().lower()
            if out_unit_l in ("т", "тн", "тонна"):
                mass_kg = out_qty * 1000.0
            elif out_unit_l == "кг":
                mass_kg = out_qty
            else:
                continue
            session.add(ComponentYield(
                item_name=in_name,
                component_name=out_name,
                material=material,
                quantity=quantity or in_qty,
                unit=in_unit,
                gross_weight_kg=gross_kg,
                metal_mass_kg=mass_kg,
                block="mot",
                document_ref=f"1c-razborka:{reg_guid}",
                confidence="high",
                notes=f"1С разборка · {d['reg'][:60]}",
            ))
            made += 1
    stats.imported = made
    stats.notes["documents"] = len(docs)
    session.commit()
    return stats


# ------------------------------------------------------- логистика (Отвесная)

def import_logistics_rates(session: Session, csv_path: str) -> OneCStats:
    """Фактическая стоимость перевозки ₽/т из «Отвесной».

    Дискриминатор (проверен на данных): достоверная стоимость перевозки —
    только строки «Автотранспорт найм» с заполненным перевозчиком; в прочих
    строках поле «Стоимость» содержит стоимость груза (≈ цена лома).
    """
    import statistics

    stats = OneCStats()
    by_place: dict[str, list[float]] = defaultdict(list)
    per_tkm: list[float] = []

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            if (row.get("ВидДоставки") or "").strip() != "Автотранспорт найм":
                continue
            if not (row.get("Перевозчик") or "").strip():
                continue
            cost = _num(row.get("Стоимость"))
            weight = _num(row.get("ВесПоПСА")) or _num(row.get("ВесПоТТН"))
            if cost <= 0 or weight <= 0.05:
                continue
            rub_t = cost / weight
            if not (100 < rub_t < 100_000):  # отсечь единичный мусор
                continue
            by_place[(row.get("МестоПогрузки") or "не указано").strip()[:80]].append(rub_t)
            km = _num(row.get("Километраж"))
            if km > 1:
                per_tkm.append(rub_t / km)

    allv = [v for vals in by_place.values() for v in vals]
    if not allv:
        stats.notes["error"] = "не найдено строк наёмного транспорта со стоимостью"
        return stats

    median_all = statistics.median(allv)
    session.query(ApprovedValue).filter(
        ApprovedValue.key == "logistics_cost_per_tonne",
        ApprovedValue.is_current.is_(True)).update({"is_current": False})
    session.add(ApprovedValue(
        key="logistics_cost_per_tonne", value=round(median_all, 2), unit="₽/т",
        approved_by="1С (Отвесная)", is_current=True,
        notes=f"факт наёмного транспорта: {len(allv)} перевозок, медиана"))
    # ставка ₽/(т·км) — справочно
    if per_tkm:
        session.query(ApprovedValue).filter(
            ApprovedValue.key == "logistics_rub_per_tonne_km",
            ApprovedValue.is_current.is_(True)).update({"is_current": False})
        session.add(ApprovedValue(
            key="logistics_rub_per_tonne_km",
            value=round(statistics.median(per_tkm), 3), unit="₽/(т·км)",
            approved_by="1С (Отвесная)", is_current=True,
            notes=f"факт: {len(per_tkm)} перевозок с километражом"))
    # по местам погрузки (для будущей привязки КП к региону продавца)
    session.query(ApprovedValue).filter(
        ApprovedValue.key == "logistics_cost_per_tonne",
        ApprovedValue.scope.isnot(None)).delete(synchronize_session=False)
    places = skipped = 0
    for place, vals in by_place.items():
        if len(vals) < 20:
            continue
        med = statistics.median(vals)
        # правдоподобный коридор ставки перевозки; вне него в поле «Стоимость»
        # почти всегда стоимость груза, а не транспорта
        if not (200 <= med <= 10_000):
            skipped += 1
            continue
        session.add(ApprovedValue(
            key="logistics_cost_per_tonne", scope=place,
            value=round(med, 2), unit="₽/т",
            approved_by="1С (Отвесная)", is_current=True,
            notes=f"факт: {len(vals)} перевозок"))
        places += 1
    stats.notes["places_skipped_out_of_range"] = skipped
    session.commit()
    stats.imported = places + 1
    stats.notes.update({"median_rub_t": round(median_all),
                        "deliveries": len(allv), "places": places})
    return stats


# ------------------------------------------------- фактический деловой выход

# Семейства, ценность которых определяется металлом: деловой выход не применим
# (кабель считается кабельным контуром, обмотки/статоры — лом по определению).
_METAL_ONLY_FAMILIES = {"кабель", "статор", "обмотка", "лом"}


def import_fact_business_yields(session: Session, csv_path: str,
                                threshold: float = 1.25,
                                min_observations: int = 30) -> OneCStats:
    """Фактический деловой выход (блок mot) из движений 1С.

    Определение: доля массы семейства, проданной ДОРОЖЕ стоимости собственного
    металлосодержания (порог `threshold` × ₽/т лома по составу семейства и году),
    против массы, проданной по цене лома либо ушедшей в разборку.
    Цены лома по материалам и годам берутся из этого же файла (продажи ломовой
    номенклатуры) — расчёт самодостаточен и воспроизводим.
    """
    import statistics

    stats = OneCStats()
    unit_mass: dict[str, float] = defaultdict(float)
    shares: dict[str, dict[str, float]] = defaultdict(dict)
    for p in session.query(ExpertMetalProfile).all():
        if p.kg_per_unit:
            unit_mass[p.family] += p.kg_per_unit
        if p.percent_of_mass:
            shares[p.family][p.material] = p.percent_of_mass

    mat_year: dict[tuple[str, str], list[float]] = defaultdict(list)
    movements: list[tuple] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            hop = row.get("ХозяйственнаяОперация") or ""
            if not (hop.startswith("Реализация")
                    or hop == "Разборка на комплектующие"):
                continue
            name = row.get("Номенклатура") or ""
            unit = (row.get("ЕдиницаИзмерения") or "").strip().lower()
            qty = _num(row.get("Количество"))
            price = _num(row.get("Цена"))
            year = (row.get("Период") or "")[:4]
            family = classify_family(name)
            if (hop.startswith("Реализация") and family == "лом"
                    and unit in ("т", "тн", "тонна") and price > 1000):
                material = scrap_material(name)
                if material:
                    mat_year[(material, year)].append(price)
            movements.append((hop, row.get("Операция") or "", name, unit,
                              qty, price, year, family))

    prices = {k: statistics.median(v) for k, v in mat_year.items() if len(v) >= 8}
    if not prices:
        stats.notes["error"] = "не удалось определить цены лома по годам"
        return stats
    fallback_year = max(y for _, y in prices)

    def family_scrap_rub_t(family: str, year: str) -> float | None:
        """₽/т металлосодержания семейства в ценах года."""
        comp = shares.get(family)
        if comp:
            total = base = 0.0
            for material, share in comp.items():
                pr = prices.get((material, year)) or prices.get(
                    (material, fallback_year))
                if pr:
                    total += share * pr
                    base += share
            if base > 0.3:
                return total / base
        return prices.get(("чермет", year)) or prices.get(("чермет", fallback_year))

    good: dict[str, float] = defaultdict(float)
    scrap: dict[str, float] = defaultdict(float)
    obs: dict[str, int] = defaultdict(int)
    for hop, op, name, unit, qty, price, year, family in movements:
        if not family or family in _METAL_ONLY_FAMILIES:
            continue
        if unit in ("т", "тн", "тонна"):
            mass_kg = qty * 1000.0
        elif unit.startswith("шт") and unit_mass.get(family):
            mass_kg = qty * unit_mass[family]
        else:
            continue
        if mass_kg <= 0:
            continue
        if hop.startswith("Реализация"):
            ref = family_scrap_rub_t(family, year)
            if not ref or price <= 0:
                continue
            rub_t = (price * 1000.0 / unit_mass[family]
                     if unit.startswith("шт") and unit_mass.get(family) else price)
            obs[family] += 1
            if rub_t > ref * threshold:
                good[family] += mass_kg
            else:
                scrap[family] += mass_kg
        elif "исходная" in op:
            scrap[family] += mass_kg
            obs[family] += 1

    session.query(ExpertYield).filter(
        ExpertYield.block == "mot",
        ExpertYield.notes.like("факт 1С%")).delete(synchronize_session=False)
    detail = {}
    for family in set(good) | set(scrap):
        total = good[family] + scrap[family]
        if total <= 0 or obs[family] < min_observations:
            continue
        pct = good[family] / total * 100.0
        conf = "high" if obs[family] >= 300 else "medium"
        # позиционные/размерные нормативы приоритетнее, поэтому пишем категорию
        session.query(ExpertYield).filter(
            ExpertYield.block == "mot",
            ExpertYield.category == family,
            ExpertYield.size_key.is_(None)).delete(synchronize_session=False)
        session.add(ExpertYield(
            block="mot", category=family, good_percent=round(pct, 1),
            scrap_percent=round(100 - pct, 1), confidence=conf,
            expert_name="1С (факт)",
            approved_at=None,
            notes=(f"факт 1С: деловая {good[family]/1000:,.0f} т против "
                   f"ломовой {scrap[family]/1000:,.0f} т, "
                   f"{obs[family]} наблюдений, порог {threshold}× металлосодержания")))
        detail[family] = (round(pct, 1), obs[family])
        stats.imported += 1
    session.commit()
    stats.notes["families"] = detail
    return stats


# ------------------------------------------------- норматив переработки

# В «Производственной себестоимости» Количество измеряется в тоннах: у разборки
# станций управления медиана 0,358 — долей штуки не бывает. Затраты в колонках
# СуммаГСМ/ПРР/Амортизации/ФОТ.
_COST_COLUMNS = ("СуммаГСМ", "СуммаПРР", "СуммаАмортизации", "СуммаФОТ")


def import_processing_costs(session: Session, cost_csv: str, movements_csv: str,
                            years: tuple[str, ...] = ("2024", "2025")) -> OneCStats:
    """Фактическая стоимость переработки ₽/т вместо норматива «с потолка».

    Берём только завершённые годы: в текущем году выгрузка движений обрывается
    и тоннаж занижен, из-за чего ставка взлетает до абсурда.
    """
    stats = OneCStats()
    by_type: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "qty": 0.0})
    total_cost = 0.0
    with open(cost_csv, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            if (row.get("Период") or "")[:4] not in years:
                continue
            amount = sum(_num(row.get(c)) for c in _COST_COLUMNS)
            work = (row.get("ВидРабот") or "не указан").strip()
            by_type[work]["cost"] += amount
            by_type[work]["qty"] += _num(row.get("Количество"))
            total_cost += amount

    scrap_sold_t = 0.0
    with open(movements_csv, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("Период") or "")[:4] not in years:
                continue
            if not (row.get("ХозяйственнаяОперация") or "").startswith("Реализация"):
                continue
            unit = (row.get("ЕдиницаИзмерения") or "").strip().lower()
            qty = _num(row.get("Количество"))
            if unit in ("т", "тн", "тонна") and qty > 0 and classify_family(
                    row.get("Номенклатура") or "") == "лом":
                scrap_sold_t += qty

    if scrap_sold_t <= 0:
        stats.notes["error"] = "не удалось определить тоннаж проданного лома"
        return stats

    rate = total_cost / scrap_sold_t
    session.query(ApprovedValue).filter(
        ApprovedValue.key == "dismantling_cost_per_tonne",
        ApprovedValue.is_current.is_(True)).update({"is_current": False})
    session.add(ApprovedValue(
        key="dismantling_cost_per_tonne", value=round(rate, 2), unit="₽/т",
        approved_by="1С (производственная себестоимость)", is_current=True,
        notes=(f"факт {'–'.join(years)}: {total_cost:,.0f} ₽ переработки на "
               f"{scrap_sold_t:,.0f} т проданного лома")))

    # ставки по видам работ — чтобы директор видел, из чего сложилось
    session.query(ApprovedValue).filter(
        ApprovedValue.key == "processing_cost_per_tonne",
        ApprovedValue.scope.isnot(None)).delete(synchronize_session=False)
    for work, agg in by_type.items():
        if agg["qty"] < 100 or agg["cost"] <= 0:  # мелкие виды работ не показываем
            continue
        # У части работ «Количество» ведётся не в тоннах (у разделки кабеля —
        # 978 тыс. «тонн» за два года, чего быть не может). Такие ставки не
        # публикуем: лучше пробел, чем неверная цифра.
        if agg["qty"] > scrap_sold_t:
            stats.notes.setdefault("unit_unclear", []).append(work)
            continue
        session.add(ApprovedValue(
            key="processing_cost_per_tonne", scope=work,
            value=round(agg["cost"] / agg["qty"], 2), unit="₽/т",
            approved_by="1С (производственная себестоимость)", is_current=True,
            notes=f"факт: {agg['cost']:,.0f} ₽ на {agg['qty']:,.0f} т"))
        stats.imported += 1
    session.commit()
    stats.notes.update({"rate_per_tonne": round(rate),
                        "total_cost": round(total_cost),
                        "scrap_sold_t": round(scrap_sold_t)})
    return stats
