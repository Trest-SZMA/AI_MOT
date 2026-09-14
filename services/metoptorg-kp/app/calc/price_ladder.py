"""Каскад цены реализации: от точного факта к обоснованной оценке.

Зачем: пакетный поиск цен «находил 0 из 50». Разбор на реальных данных показал,
что дело не в интернете, а в том, что цена лежала рядом и её никто не искал:

* одна и та же машина заведена в 1С несколькими карточками — продажа попала на
  «Погружной электродвигатель 10ВДМ100-2400-3,0-117 (шт)» (35 625 ₽), а КП
  приходит с «ВДМ 100-2400-3.0-117В5 б/у», где есть только закупка;
* карточки без слова «электродвигатель» вообще не получали семейства, значит и
  аналога;
* там, где аналог находился, он был медианой по всему семейству ПЭД — одна и та
  же цифра 56 250 ₽ и для ВДМ 28, и для ВДМ 250, различающихся втрое.

Каскад (в порядке убывания доверия):

1. своя продажа этой карточки;
2. продажа карточки-двойника (тот же модельный ключ);
3. продажа того же габарита ряда («вдм-100»);
4. модельный ряд — робастная регрессия цены по габариту;
5. своя закупка × наценка ряда/семейства;
6. медиана семейства (грубо, только чтобы не остаться без числа);
7. внешние источники (рыночное предложение, ИИ-поиск);
8. floor по лому — считает движок, он же знает массу и состав.

Возвращаем не одно число, а все сработавшие ступени: оценщик видит вилку и
основание, а не «цифру от программы».
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db.models import Item, PriceQuote
from ..normalize import model_key, series_key, unit_info

# Минимум наблюдений, при котором ряд считается пригодным для регрессии.
MIN_SERIES_POINTS = 4
# Дальше этих границ от крайних наблюдений ряда экстраполяция не считается
# обоснованной: за пределами — только ближайший сосед с пометкой.
EXTRAPOLATION_SPAN = 2.0

_cache: dict = {"key": None, "data": None}


@dataclass
class PriceCandidate:
    """Одна сработавшая ступень каскада."""

    price: float
    unit: str
    source: str            # машинный код ступени
    label: str             # человеческое объяснение
    confidence: str        # high | medium | low
    samples: int = 0
    low: float | None = None
    high: float | None = None
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"price": self.price, "unit": self.unit, "source": self.source,
                "label": self.label, "confidence": self.confidence,
                "samples": self.samples, "low": self.low, "high": self.high,
                "evidence": self.evidence[:5]}


def reset_cache() -> None:
    _cache["key"] = None
    _cache["data"] = None


# ------------------------------------------------------------------ индексы


def _unit_norm(unit: str | None) -> str:
    u = (unit or "").strip().lower().rstrip(".")
    info = unit_info(u)
    # приводим тонны/кг к одному ключу нельзя (цена за тонну ≠ за кг), но
    # синонимы «т»/«тн» — это одна единица
    if info and info[0] == "mass" and info[1] == 1000.0:
        return "т"
    return u


def _build(session: Session) -> dict:
    """Индексы фактов 1С: по модельному ключу, габариту ряда, ряду и семейству."""
    rows = (session.query(PriceQuote.price, PriceQuote.unit, PriceQuote.quote_type,
                          PriceQuote.source, Item.id, Item.name, Item.family,
                          Item.size_key)
            .join(Item, PriceQuote.item_id == Item.id)
            .filter(PriceQuote.quote_type.in_(("sales_fact", "purchase_fact")),
                    Item.is_trade.is_(True),
                    PriceQuote.price > 0).all())

    sales_by_model: dict[tuple, list[tuple[float, str]]] = {}
    sales_by_size: dict[tuple, list[tuple[float, str]]] = {}
    sales_by_series: dict[tuple, list[tuple[float, float, str]]] = {}
    sales_by_family: dict[tuple, list[float]] = {}
    purch_by_size: dict[tuple, list[float]] = {}
    purch_by_model: dict[tuple, list[float]] = {}
    purch_by_series: dict[tuple, list[tuple[float, float, str]]] = {}
    size_family: dict[str, str] = {}

    for price, unit, qtype, src, item_id, name, family, size_key in rows:
        u = _unit_norm(unit)
        if not u:
            continue
        if size_key and family:
            size_family.setdefault(size_key, family)
        mk = model_key(name or "")
        sk = series_key(name or "")
        if qtype == "sales_fact":
            if len(mk) >= 5:
                sales_by_model.setdefault((mk, u), []).append((price, name))
            if size_key:
                sales_by_size.setdefault((size_key, u), []).append((price, name))
            if sk:
                sales_by_series.setdefault((sk[0], u), []).append((sk[1], price, name))
            if family:
                sales_by_family.setdefault((family, u), []).append(price)
        else:
            if size_key:
                purch_by_size.setdefault((size_key, u), []).append(price)
            if len(mk) >= 5:
                purch_by_model.setdefault((mk, u), []).append(price)
            if sk:
                purch_by_series.setdefault((sk[0], u), []).append((sk[1], price, name))

    return {"sales_model": sales_by_model, "sales_size": sales_by_size,
            "sales_series": sales_by_series, "sales_family": sales_by_family,
            "purch_size": purch_by_size, "purch_model": purch_by_model,
            "purch_series": purch_by_series, "size_family": size_family}


def _data(session: Session) -> dict:
    key = session.query(PriceQuote).filter(
        PriceQuote.quote_type.in_(("sales_fact", "purchase_fact"))).count()
    if _cache["key"] != key or _cache["data"] is None:
        _cache["key"] = key
        _cache["data"] = _build(session)
    return _cache["data"]


# ------------------------------------------------------- регрессия по ряду


def _theil_sen(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Робастная прямая по медиане попарных наклонов (Тейл — Сен).

    Обычный МНК ломается на единственном выбросе, а в ряду ВДМ такой есть:
    «10ВДМ230-390-0,5-117» продан за 26 250 ₽ при том, что «10ВДМ230-2800» —
    за 72 375 ₽ (другое исполнение при том же габарите). Медиана наклонов это
    переживает, МНК — нет.
    """
    slopes = []
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            dx = points[j][0] - points[i][0]
            if abs(dx) < 1e-9:
                continue
            slopes.append((points[j][1] - points[i][1]) / dx)
    if not slopes:
        return None
    slope = statistics.median(slopes)
    intercept = statistics.median(y - slope * x for x, y in points)
    return slope, intercept


@dataclass
class SeriesFit:
    price: float
    points: list[tuple[float, float]]
    lo_size: float
    hi_size: float
    extrapolated: bool


def series_fit(session: Session, name: str, unit: str | None,
               kind: str = "sales") -> SeriesFit | None:
    """Оценка цены по модельному ряду: регрессия log(цена) ~ log(габарит).

    За пределами ряда более чем в EXTRAPOLATION_SPAN раз оценка не выдаётся:
    «крайнее наблюдение» в такой ситуации — не оценка, а случайная цифра.
    """
    import math

    sk = series_key(name or "")
    if not sk:
        return None
    mark, size = sk
    u = _unit_norm(unit)
    idx = "sales_series" if kind == "sales" else "purch_series"
    obs = _data(session)[idx].get((mark, u)) or []
    # свой габарит из выборки убирать не нужно: точное совпадение отработала
    # предыдущая ступень каскада, сюда мы попадаем только когда его не было
    pts = [(s, p) for s, p, _ in obs if s > 0 and p > 0]
    if len(pts) < MIN_SERIES_POINTS:
        return None
    lo_size, hi_size = min(s for s, _ in pts), max(s for s, _ in pts)
    if size < lo_size / EXTRAPOLATION_SPAN or size > hi_size * EXTRAPOLATION_SPAN:
        return None
    line = _theil_sen([(math.log(s), math.log(p)) for s, p in pts])
    if line is None:
        return None
    slope, intercept = line
    price = math.exp(intercept + slope * math.log(size))
    return SeriesFit(price=price, points=sorted(pts), lo_size=lo_size,
                     hi_size=hi_size, extrapolated=not lo_size <= size <= hi_size)


def series_estimate(session: Session, name: str,
                    unit: str | None) -> PriceCandidate | None:
    """Цена реализации по ряду продаж."""
    sk = series_key(name or "")
    fit = series_fit(session, name, unit, "sales")
    if fit is None or sk is None:
        return None
    mark, size = sk
    u = _unit_norm(unit)
    prices = sorted(p for _, p in fit.points)
    note = (f"; экстраполяция за край ряда {fit.lo_size:g}–{fit.hi_size:g}"
            if fit.extrapolated else "")
    return PriceCandidate(
        price=round(fit.price, 2), unit=u, source="series",
        label=(f"модельный ряд {mark.upper()}: {len(fit.points)} факт. продаж "
               f"габаритов {fit.lo_size:g}–{fit.hi_size:g}, "
               f"цена по габариту {size:g}{note}"),
        confidence="low" if fit.extrapolated else "medium", samples=len(fit.points),
        low=prices[0], high=prices[-1],
        evidence=[f"{s:g} → {p:,.0f} ₽/{u}" for s, p in fit.points])


# ------------------------------------------------------- наценка к закупке


def markup_ratio(session: Session, unit: str | None,
                 size_key: str | None, family: str | None) -> tuple[float, int, str] | None:
    """Медианное отношение «цена продажи / цена закупки» по ряду, затем семейству.

    Считается по габаритам, где есть и продажа, и закупка: продажа и закупка
    одной и той же машины в 1С часто лежат на РАЗНЫХ карточках, поэтому
    сопоставлять их поштучно бесполезно — сходятся они только по габариту.
    """
    d = _data(session)
    u = _unit_norm(unit)

    def ratios(keys) -> list[float]:
        out = []
        for k in keys:
            s = d["sales_size"].get((k, u))
            p = d["purch_size"].get((k, u))
            if s and p:
                sm, pm = statistics.median([x[0] for x in s]), statistics.median(p)
                if pm > 0 and 1.0 <= sm / pm <= 20.0:  # вне этого — ошибка данных
                    out.append(sm / pm)
        return out

    if size_key:
        mark = size_key.rsplit("-", 1)[0]
        same_series = [k for (k, uu) in d["sales_size"]
                       if uu == u and k.rsplit("-", 1)[0] == mark]
        vals = ratios(same_series)
        if len(vals) >= 2:
            return statistics.median(vals), len(vals), f"ряд {mark.upper()}"
    if family:
        fam_keys = [k for (k, uu) in d["sales_size"]
                    if uu == u and d["size_family"].get(k) == family]
        vals = ratios(fam_keys)
        if len(vals) >= 3:
            return statistics.median(vals), len(vals), f"семейство {family}"
    return None


# ------------------------------------------------------------------ каскад


_QTYPE_LABEL = {
    "sales_fact": "факт продаж 1С",
    "resale_price_without_vat": "цена реализации без НДС",
    "used_market_offer": "рыночное предложение б/у",
    "ai_estimate": "ИИ-оценка",
}


def _fresh(row: PriceQuote) -> bool:
    if not row.ttl_days or not row.quoted_at:
        return True
    quoted = row.quoted_at
    if quoted.tzinfo is None:
        quoted = quoted.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - quoted).days <= row.ttl_days


def candidates(session: Session, name: str, unit: str | None,
               item: Item | None) -> list[PriceCandidate]:
    """Все обоснованные оценки цены реализации за единицу, лучшая — первой."""
    out: list[PriceCandidate] = []
    u = _unit_norm(unit or (item.unit if item else None))
    d = _data(session)
    family = item.family if item else None
    size_key = item.size_key if item else None
    if not size_key:
        from ..normalize import extract_size_key
        size_key = extract_size_key(name or "")
    if not family:
        from ..normalize import classify_family
        family = classify_family(name or "")

    # 1. своя продажа
    if item is not None:
        row = (session.query(PriceQuote)
               .filter(PriceQuote.item_id == item.id,
                       PriceQuote.quote_type == "sales_fact")
               .order_by(PriceQuote.quoted_at.desc()).first())
        if row and row.price > 0:
            out.append(PriceCandidate(
                price=row.price, unit=_unit_norm(row.unit) or u,
                source="sales_fact", label="факт продаж 1С по этой карточке",
                confidence="high", samples=1,
                evidence=[row.source or "1С"]))

    # 2. продажа карточки-двойника (тот же модельный ключ)
    mk = model_key(name or "")
    if len(mk) >= 5:
        rows = d["sales_model"].get((mk, u)) or []
        rows = [r for r in rows if not (item and r[1] == item.name)]
        if rows:
            vals = [p for p, _ in rows]
            out.append(PriceCandidate(
                price=statistics.median(vals), unit=u, source="model_twin",
                label=(f"факт продаж карточки-двойника ({len(rows)} шт.): "
                       "та же машина заведена в 1С под другим именем"),
                confidence="high", samples=len(rows),
                low=min(vals), high=max(vals),
                evidence=[n for _, n in rows]))

    # 3. тот же габарит ряда
    if size_key:
        rows = d["sales_size"].get((size_key, u)) or []
        rows = [r for r in rows if not (item and r[1] == item.name)]
        if rows:
            vals = [p for p, _ in rows]
            out.append(PriceCandidate(
                price=statistics.median(vals), unit=u, source="size",
                label=f"продажи того же типоразмера «{size_key}»: {len(rows)} факт.",
                confidence="high" if len(rows) >= 2 else "medium",
                samples=len(rows), low=min(vals), high=max(vals),
                evidence=[n for _, n in rows]))

    # 4. модельный ряд
    ser = series_estimate(session, name or "", u)
    if ser:
        out.append(ser)

    # 5. своя закупка × наценка
    purchase = None
    if item is not None:
        row = (session.query(PriceQuote)
               .filter(PriceQuote.item_id == item.id,
                       PriceQuote.quote_type == "purchase_fact")
               .order_by(PriceQuote.quoted_at.desc()).first())
        if row and row.price > 0:
            purchase = row.price
    if purchase is None and len(mk) >= 5:
        vals = d["purch_model"].get((mk, u))
        if vals:
            purchase = statistics.median(vals)
    purchase_note = "наша закупка"
    if purchase is None:
        # Закупки покрывают ряд плотнее продаж (ВДМ: закупались габариты
        # 20…200, продавались 80…250), поэтому по ряду закупок оценка
        # существует там, где по ряду продаж её нет.
        pfit = series_fit(session, name or "", u, "purchase")
        if pfit is not None and not pfit.extrapolated:
            purchase = pfit.price
            purchase_note = f"закупка по ряду ({len(pfit.points)} факт.)"
    if purchase:
        mr = markup_ratio(session, u, size_key, family)
        if mr:
            ratio, n, scope = mr
            out.append(PriceCandidate(
                price=round(purchase * ratio, 2), unit=u, source="markup",
                label=(f"{purchase_note} {purchase:,.0f} ₽ × наценка {ratio:.2f} "
                       f"({scope}, {n} габаритов с продажей и закупкой)"),
                confidence="medium" if purchase_note == "наша закупка" else "low",
                samples=n))

    # 6. медиана семейства — грубая, но лучше пустоты
    if family:
        vals = d["sales_family"].get((family, u)) or []
        if len(vals) >= 5:
            out.append(PriceCandidate(
                price=statistics.median(vals), unit=u, source="family",
                label=(f"аналог по семейству «{family}»: медиана "
                       f"{len(vals)} факт. продаж — грубо, внутри семейства "
                       "цена меняется в разы"),
                confidence="low", samples=len(vals),
                low=min(vals), high=max(vals)))

    # 7. внешние источники
    if item is not None:
        for qt in ("resale_price_without_vat", "used_market_offer", "ai_estimate"):
            row = (session.query(PriceQuote)
                   .filter(PriceQuote.item_id == item.id,
                           PriceQuote.quote_type == qt)
                   .order_by(PriceQuote.quoted_at.desc()).first())
            if row and row.price > 0:
                label = _QTYPE_LABEL[qt]
                if not _fresh(row):
                    label += " (устарела)"
                out.append(PriceCandidate(
                    price=row.price, unit=_unit_norm(row.unit) or u,
                    source=qt, label=label,
                    confidence=row.confidence or "low", samples=1,
                    evidence=[row.source_url] if row.source_url else []))

    # Ступень каскада задаёт порядок, но доверие важнее: экстраполяция ряда за
    # его край (low) не должна обходить нашу же закупку с наценкой (medium).
    # Сортировка устойчивая — внутри одного уровня доверия порядок каскада цел.
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda c: rank.get(c.confidence, 2))
    return out


def best(session: Session, name: str, unit: str | None,
         item: Item | None) -> PriceCandidate | None:
    cands = candidates(session, name, unit, item)
    return cands[0] if cands else None
