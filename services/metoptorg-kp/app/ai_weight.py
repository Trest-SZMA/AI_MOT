"""ИИ-оценка массы единицы оборудования по открытым данным.

В выгрузке 1С реквизита «Вес» нет, а профиль разборки даёт грубую оценку
(ПЭД 310 кг/шт против паспортных ~700–900). Масса единицы напрямую задаёт
ломовую часть позиции, поэтому берём её из открытых источников.

Провайдер и транспорт общие с поиском цен (см. ai_price).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from .ai_price import (
    AiUnavailable,
    DEFAULT_MODEL,
    TIMEOUT,
    _extract_json,
    _provider,
    _ssl_context,
    is_enabled,
)
from .db.models import ExpertMetalProfile, Item
from .normalize import unit_info

log = logging.getLogger(__name__)

PROMPT = """Ты определяешь массу нефтепромыслового и электротехнического
оборудования по открытым техническим данным (каталоги заводов, паспорта, ГОСТ,
объявления с характеристиками).

Позиция: {name}
Учётная единица: {unit}

Найди МАССУ ОДНОЙ ЕДИНИЦЫ в килограммах (сухая масса изделия без упаковки и
без масла). Если модель точно не найдена — оцени по ближайшему типоразмеру
того же типа и укажи это в комментарии.

Верни СТРОГО JSON без пояснений:
{{"mass_kg": <число, кг за 1 {unit}>,
  "confidence": "low"|"medium"|"high",
  "sources": ["<url>", ...],
  "comment": "<кратко: чья модель, откуда масса>"}}

Если данных нет — верни {{"mass_kg": null, "sources": [], "comment": "нет данных"}}.
Не выдумывай."""

# Разумный коридор для нефтепромыслового оборудования: вне его почти всегда
# ошибка размерности (граммы вместо килограммов или масса всей партии).
MIN_KG, MAX_KG = 0.5, 25_000
# Во сколько раз ответ может отличаться от нашего профиля разборки. Проверено
# на реальном случае: «Секция 117ЭЦН(НГ) 5-25 (4м)» модель приняла за кабель
# ВВГнг 5х25 и выдала 2 кг вместо ~270 — причём с доверием «medium».
PROFILE_TOLERANCE = 3.0


def _family_profile_mass(session: Session, family: str | None) -> float | None:
    """Масса единицы по нашим разборкам — ориентир для проверки ответа ИИ."""
    if not family:
        return None
    rows = (session.query(ExpertMetalProfile)
            .filter(ExpertMetalProfile.family == family,
                    ExpertMetalProfile.kg_per_unit.isnot(None)).all())
    total = sum(r.kg_per_unit for r in rows)
    return total or None


def ask_weight(name: str, unit: str) -> dict:
    url, model, headers = _provider()
    payload = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(name=name, unit=unit or "шт")}],
        "temperature": 0,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=_ssl_context()) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise AiUnavailable(f"провайдер вернул {e.code}: {detail}") from e
    except OSError as e:
        raise AiUnavailable(f"сеть недоступна: {e}") from e

    data = _extract_json(body["choices"][0]["message"]["content"])
    for key in ("citations", "search_results"):
        extra = body.get(key)
        if extra:
            urls = [c if isinstance(c, str) else c.get("url") for c in extra]
            data["sources"] = (data.get("sources") or []) + [u for u in urls if u]
    return data


def ai_weight_for_item(session: Session, item_id: int) -> dict:
    """Оценить и сохранить массу единицы для карточки справочника."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError("позиция не найдена")
    info = unit_info(item.unit)
    if info and info[0] != "count":
        raise ValueError(
            f"У позиции учётная единица «{item.unit}» — масса единицы для неё "
            "определяется самой единицей (1 т = 1000 кг), поиск не нужен.")

    data = ask_weight(item.name, item.unit or "шт")
    mass = data.get("mass_kg")
    if mass is None:
        return {"saved": False, "comment": data.get("comment") or "нет данных"}
    mass = float(mass)
    if not (MIN_KG <= mass <= MAX_KG):
        return {"saved": False,
                "comment": f"масса {mass:,.0f} кг вне разумного коридора "
                           f"{MIN_KG}–{MAX_KG:,.0f} кг — похоже на ошибку "
                           f"размерности, не сохраняю"}
    # Сверка с собственными разборками: расхождение в разы означает, что модель
    # опознала не то изделие (секцию ЭЦН приняла за кабель).
    profile = _family_profile_mass(session, item.family)
    if profile and not (1 / PROFILE_TOLERANCE <= mass / profile <= PROFILE_TOLERANCE):
        return {"saved": False,
                "comment": (f"ответ {mass:,.0f} кг расходится с нашими разборками "
                            f"({profile:,.0f} кг/шт по семейству «{item.family}») "
                            f"в {max(mass / profile, profile / mass):.0f} раз — "
                            f"вероятно, опознано другое изделие. Не сохраняю, "
                            f"проверьте вручную."),
                "profile_kg": round(profile, 1), "ai_kg": round(mass, 1)}
    sources = [s for s in (data.get("sources") or []) if isinstance(s, str)]
    item.unit_mass_kg = round(mass, 1)
    item.unit_mass_source = ("ИИ-поиск ("
                             + os.environ.get("AI_PRICE_MODEL", DEFAULT_MODEL) + ")")
    item.unit_mass_url = sources[0] if sources else None
    item.unit_mass_confidence = data.get("confidence") or "low"
    item.unit_mass_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    return {"saved": True, "mass_kg": item.unit_mass_kg, "sources": sources,
            "comment": data.get("comment"),
            "confidence": item.unit_mass_confidence}


__all__ = ["ai_weight_for_item", "ask_weight", "AiUnavailable", "is_enabled"]
