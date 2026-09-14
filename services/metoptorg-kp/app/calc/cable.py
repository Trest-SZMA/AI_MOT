"""Кабельный модуль: парсер маркировки и аналитический расчёт металлов.

Методика из файла «Кабель нормы выхода и веса»:
  медь     = Σ(жилы × сечение, мм²) × 8,9 кг/км
  алюминий = Σ(жилы × сечение, мм²) × 2,7 кг/км
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

COPPER_KG_PER_KM_MM2 = 8.9
ALUMINUM_KG_PER_KM_MM2 = 2.7

# группа жил: «3х16», «4х2х0,52» (пары), «1х70»
_CORE_GROUP_RE = re.compile(
    r"(\d+)\s*х\s*(?:(\d+)\s*х\s*)?(\d+(?:\.\d+)?)", re.I)
_NUM_NORM = re.compile(r"(?<=\d),(?=\d)")
_SEP_NORM = re.compile(r"[x×*]", re.I)

# алюминиевые префиксы марки: АВВГ, АВБШв, АСБ, АС-50, СИП, АКВВГ, КНАПпБП…
_ALUM_PREFIX_RE = re.compile(r"^(а[а-я]|ас\b|ас-|сип)", re.I)
# исключения: АПв… бывает и медь, но по файлам компании А-префикс = алюминий.
# КНАПпБП — нефтепогружной с алюминиевыми жилами (подтверждено файлом: 3х16 → 129,6)
_ALUM_SPECIAL_RE = re.compile(r"кнап", re.I)
# лужёная медь: МКЭШ… и подобные монтажные экранированные
_TINNED_RE = re.compile(r"мкэ[кш]?ш", re.I)
# свинцовая оболочка: КЭСБП, КБТ, КЕСБП, КЭСБкП, СБГ, АСБ (С = свинец)
_LEAD_RE = re.compile(r"к[еэи]?[фс]?сб|кбт|\bсбг|асбг?\b", re.I)
# броня: буква Б в типе (ВБбШв, АВБШв, КВБбШнг…)
_ARMOR_RE = re.compile(r"б[бш]?[шв]|бп\b|пбп", re.I)


@dataclass
class CableMarking:
    brand: str
    core_groups: list[tuple[int, float]] = field(default_factory=list)  # (жил, мм²)
    conductor: str = "медь"  # медь | медь луженая | алюминий
    has_lead_sheath: bool = False
    has_armor: bool = False

    @property
    def total_section_mm2(self) -> float:
        return sum(n * s for n, s in self.core_groups)

    @property
    def conductor_kg_per_km(self) -> float:
        k = ALUMINUM_KG_PER_KM_MM2 if self.conductor == "алюминий" else COPPER_KG_PER_KM_MM2
        return self.total_section_mm2 * k


def parse_cable_marking(name: str) -> CableMarking | None:
    """«Кабель ВВГнг(А)-LS 3х150+1х70» → группы жил, материал, признаки."""
    s = str(name).strip()
    s_norm = _SEP_NORM.sub("х", _NUM_NORM.sub(".", s))
    # отрезаем слово «кабель»/«отходы кабеля»
    body = re.sub(r"^\s*(отходы\s+)?кабел[ья]\s+", "", s_norm, flags=re.I).strip()
    if not body:
        return None

    groups: list[tuple[int, float]] = []
    for m in _CORE_GROUP_RE.finditer(body):
        n1, n2, sec = int(m.group(1)), m.group(2), float(m.group(3))
        # «4х2х0,52» → 4 пары × 2 жилы сечением 0,52
        cores = n1 * int(n2) if n2 else n1
        # отсечь явные вольтажи/даты в хвосте («-0,66», «230») не попадающие
        # под шаблон группы: сечение жилы реального кабеля 0.2–1000 мм²
        if 0.1 <= sec <= 1000 and 1 <= cores <= 100:
            groups.append((cores, sec))
    if not groups:
        return None

    first_word = body.split()[0] if body.split() else body
    conductor = "медь"
    if _ALUM_PREFIX_RE.search(first_word) or _ALUM_SPECIAL_RE.search(first_word):
        conductor = "алюминий"
    elif _TINNED_RE.search(first_word):
        conductor = "медь луженая"

    return CableMarking(
        brand=body,
        core_groups=groups,
        conductor=conductor,
        has_lead_sheath=bool(_LEAD_RE.search(first_word)),
        has_armor=bool(_ARMOR_RE.search(first_word)),
    )


@dataclass
class CableEstimate:
    """Оценка металлосодержания кабельной позиции."""

    metals_kg_per_km: dict[str, float]
    cable_kg_per_km: float | None
    source: str  # 'brand_ref' | 'fact' | 'section_calc' | 'family_avg'
    confidence: str
    notes: str = ""

    def metal_percent(self, material: str) -> float | None:
        if not self.cable_kg_per_km:
            return None
        return self.metals_kg_per_km.get(material, 0.0) / self.cable_kg_per_km

    def value_per_tonne(self, scrap_prices: dict[str, float]) -> float | None:
        """₽ за тонну кабеля по прайсу лома (цены ₽/т металла)."""
        if not self.cable_kg_per_km:
            return None
        total = sum(
            kg * scrap_prices.get(mat, 0.0) / 1000.0
            for mat, kg in self.metals_kg_per_km.items()
        )
        return total / self.cable_kg_per_km * 1000.0


def estimate_by_section(name: str,
                        cable_kg_per_km: float | None = None) -> CableEstimate | None:
    """Аналитический расчёт по маркировке (fallback, confidence low)."""
    mk = parse_cable_marking(name)
    if mk is None:
        return None
    notes = []
    if mk.has_armor:
        notes.append("броня не оценена")
    if mk.has_lead_sheath:
        notes.append("свинцовая оболочка не оценена (нет массы оболочки)")
    return CableEstimate(
        metals_kg_per_km={mk.conductor: round(mk.conductor_kg_per_km, 3)},
        cable_kg_per_km=cable_kg_per_km,
        source="section_calc",
        confidence="low",
        notes="расчёт по сечению; " + "; ".join(notes) if notes else "расчёт по сечению",
    )
