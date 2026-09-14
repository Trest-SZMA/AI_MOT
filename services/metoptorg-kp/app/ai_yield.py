"""ИИ-оценка делового выхода: какая доля б/у позиции продаётся изделием.

Дополняет два блока данных там, где их нет: собственные факты разборки (mot)
и экспертные оценки. Результат пишется в блок `ai`, поэтому влияет только на
сценарий БП и всегда уступает экспертной оценке — по требованию директора.

Провайдер и транспорт общие с поиском цен (см. ai_price).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
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
from .db.models import ExpertYield, Item
from .normalize import classify_family, normalize_name

log = logging.getLogger(__name__)

# Семейства, где деловой выход неприменим: ценность в металле, не в изделии.
METAL_ONLY = {"лом", "кабель", "статор", "обмотка"}

PROMPT = """Ты оцениваешь Б/У нефтепромысловое оборудование для российского
рынка вторичных ресурсов.

Позиция: {name}
Категория: {family}

Оцени ДЕЛОВОЙ ВЫХОД — какая доля партии такого б/у оборудования пригодна к
продаже как изделие (после дефектовки и, при необходимости, ремонта), а не
идёт в металлолом. Учитывай типичный износ, наличие вторичного рынка и
ремонтопригодность.

Верни СТРОГО JSON без пояснений:
{{"good_percent": <число 0..100>,
  "confidence": "low"|"medium",
  "sources": ["<url>", ...],
  "comment": "<кратко: на чём основана оценка>"}}

Если достоверных оснований нет — верни {{"good_percent": null, "sources": [],
"comment": "нет данных"}}. Не выдумывай."""


def ask_yield(name: str, family: str | None) -> dict:
    url, model, headers = _provider()
    payload = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(name=name,
                                               family=family or "не определена")}],
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


def ai_yield_for_item(session: Session, item_id: int) -> dict:
    """Оценить и сохранить деловой выход для карточки справочника."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError("позиция не найдена")
    family = item.family or classify_family(item.name)
    if family in METAL_ONLY:
        raise ValueError(
            f"Для семейства «{family}» деловой выход не применяется: ценность "
            "в металлосодержании, а не в изделии. Смотрите составы и прайс лома.")

    data = ask_yield(item.name, family)
    percent = data.get("good_percent")
    if percent is None:
        return {"saved": False, "comment": data.get("comment") or "нет данных"}
    percent = max(0.0, min(100.0, float(percent)))
    sources = [s for s in (data.get("sources") or []) if isinstance(s, str)]

    # заменяем прежнюю ИИ-оценку этой же позиции, экспертные не трогаем
    session.query(ExpertYield).filter(
        ExpertYield.block == "ai",
        ExpertYield.item_id == item_id).delete(synchronize_session=False)
    session.add(ExpertYield(
        item_id=item_id,
        item_name=item.name,
        item_name_normalized=normalize_name(item.name),
        category=family,
        block="ai",
        good_percent=round(percent, 1),
        scrap_percent=round(100 - percent, 1),
        expert_name="ИИ-поиск (" + os.environ.get("AI_PRICE_MODEL", DEFAULT_MODEL) + ")",
        confidence="low",  # ИИ всегда low: уступает и факту, и эксперту
        source_url=sources[0] if sources else None,
        approved_at=dt.datetime.now(dt.timezone.utc),
        notes=("ИИ-оценка: " + (data.get("comment") or ""))[:500]
        + (f" · источники: {', '.join(sources[:3])}" if sources else ""),
    ))
    session.commit()
    return {"saved": True, "good_percent": round(percent, 1),
            "sources": sources, "comment": data.get("comment")}


__all__ = ["ai_yield_for_item", "ask_yield", "AiUnavailable", "is_enabled",
           "METAL_ONLY"]
