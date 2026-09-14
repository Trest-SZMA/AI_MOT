"""ИИ-поиск рыночных цен б/у оборудования — внешний, последний источник.

Почему переписан. Прежняя версия задавала один вопрос «сколько стоит <полное
имя из 1С>» и требовала в ответ одно число. По нишевым позициям («Погружной
электродвигатель ВДМ 60-117В5 б/у») такого запроса в интернете нет ни у кого,
модель честно отвечала «нет данных», и пакетный поиск давал ноль находок.

Что изменилось:

* спрашиваем не число, а СПИСОК найденных предложений с состоянием (новое /
  б/у / восстановленное), ценой, единицей, продавцом и ссылкой — агрегируем
  сами, в питоне, а не доверяем усреднение модели;
* задаём лестницу запросов: полное имя → марка с габаритом → родовое название
  ряда. Первый же запрос с находками останавливает лестницу;
* если нашлись только новые — берём цену нового и применяем коэффициент
  «б/у от нового» (норматив, правится в разделе «Нормативы»);
* результат сверяется с внутренней оценкой по фактам 1С: расхождение более чем
  в пять раз отклоняется. Модель путает изделия, и завышенная внешняя цена
  опаснее отсутствия цены — по ней выставят предложение продавцу.

Внутренняя оценка (app/calc/price_ladder) от этого модуля НЕ зависит: цена
позиции считается по фактам 1С и без ключа провайдера.
"""
from __future__ import annotations

import logging
import os
import re
import statistics

from sqlalchemy.orm import Session

from . import ai_client
from .ai_client import AiUnavailable, is_enabled
from .db.models import ApprovedValue, Item, PriceQuote
from .normalize import series_key, unit_info

log = logging.getLogger(__name__)

TTL_DAYS = 90
# Расхождение с оценкой по нашим фактам, после которого находка отклоняется.
SANITY_FACTOR = 5.0
# Доля цены нового, если найдены только предложения новой техники.
DEFAULT_USED_OF_NEW = 0.35

# Совместимость с модулями ai_weight/ai_yield, которые брали транспорт отсюда.
DEFAULT_MODEL = ai_client.DEFAULT_MODEL
TIMEOUT = ai_client.TIMEOUT
_provider = ai_client.provider
_ssl_context = ai_client._ssl_context


def _extract_json(text: str) -> dict:
    return ai_client.extract_json(text)


PROMPT = """Найди в интернете РЕАЛЬНЫЕ предложения о продаже по позиции для
российского рынка. Нужны именно объявления, прайсы и коммерческие предложения,
а не оценки «на глаз».

Позиция: {query}
Единица учёта: {unit}

Верни СТРОГО JSON-массив найденных предложений, без пояснений вокруг:
[{{"price": <число, рублей за {unit} без НДС>,
   "condition": "used" | "new" | "refurbished",
   "seller": "<кто продаёт>",
   "url": "<ссылка на предложение>",
   "date": "<когда опубликовано, если видно>",
   "note": "<важные оговорки: комплектность, состояние, партия>"}}]

Правила:
- если цена указана за партию, пересчитай на одну {unit} и напиши это в note;
- не выдумывай ссылки: предложение без ссылки не включай;
- если ничего достоверного нет — верни пустой массив [].
"""


def _used_of_new(session: Session) -> float:
    row = (session.query(ApprovedValue)
           .filter(ApprovedValue.key == "used_of_new_ratio",
                   ApprovedValue.is_current.is_(True))
           .order_by(ApprovedValue.approved_at.desc()).first())
    return row.value if row and row.value else DEFAULT_USED_OF_NEW


def query_ladder(name: str) -> list[str]:
    """Лестница поисковых запросов от точного к родовому.

    «Погружной электродвигатель ВДМ 60-117В5 б/у (шт)» →
    «Погружной электродвигатель ВДМ 60-117В5» → «ВДМ 60-117В5» → «ВДМ 60».
    Полное имя из 1С почти никогда не встречается в объявлениях дословно.
    """
    base = re.sub(r"\s*\((?:шт|т|тн|кг|км|м)\.?\)\s*", " ", name or "", flags=re.I)
    base = re.sub(r"\bб[\s./\\]*у\b\.?", " ", base, flags=re.I)
    base = re.sub(r"\s{2,}", " ", base).strip(" ,.;")
    out = [base] if base else []

    sk = series_key(name or "")
    if sk:
        mark, size = sk
        # марка с полным обозначением из исходного имени
        m = re.search(rf"\d{{0,2}}{mark}[\s\-]*[\d\-.,]+[a-zа-я\d]*", base, re.I)
        if m and m.group(0).strip() not in out:
            out.append(m.group(0).strip())
        short = f"{mark.upper()} {size:g}"
        if short not in out:
            out.append(short)
    # убираем дубликаты, сохраняя порядок
    seen, res = set(), []
    for q in out:
        k = q.lower()
        if k not in seen and len(k) >= 4:
            seen.add(k)
            res.append(q)
    return res[:3]


def ask_offers(query: str, unit: str) -> tuple[list[dict], list[str]]:
    """Список найденных предложений по одному запросу."""
    data, urls = ai_client.ask_json(
        PROMPT.format(query=query, unit=unit or "шт"),
        want_list=True, search_domains=ai_client.TRADE_DOMAINS)
    if isinstance(data, dict):
        data = data.get("offers") or data.get("items") or []
    offers = []
    for o in data if isinstance(data, list) else []:
        if not isinstance(o, dict):
            continue
        price = o.get("price")
        try:
            price = float(str(price).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        offers.append({
            "price": price,
            "condition": (o.get("condition") or "used").lower(),
            "seller": str(o.get("seller") or "")[:120],
            "url": str(o.get("url") or "")[:500],
            "date": str(o.get("date") or "")[:32],
            "note": str(o.get("note") or "")[:300],
        })
    return offers, urls


def ask_price(name: str, unit: str) -> dict:
    """Обратная совместимость: одна агрегированная цена по имени позиции."""
    offers, urls = [], []
    for q in query_ladder(name) or [name]:
        got, u = ask_offers(q, unit)
        offers += got
        urls += u
        if len(offers) >= 3:
            break
    agg = aggregate(offers, DEFAULT_USED_OF_NEW)
    return {"price": agg["price"], "confidence": agg["confidence"],
            "sources": [o["url"] for o in offers if o["url"]] or urls,
            "comment": agg["comment"], "offers": offers}


def aggregate(offers: list[dict], used_of_new: float) -> dict:
    """Медиана по б/у; если б/у нет — цена нового с коэффициентом."""
    used = [o["price"] for o in offers
            if o["condition"] in ("used", "refurbished", "б/у")]
    new = [o["price"] for o in offers if o["condition"] == "new"]
    if used:
        price = statistics.median(used)
        return {"price": price, "confidence": "medium" if len(used) >= 3 else "low",
                "comment": (f"медиана {len(used)} предложений б/у "
                            f"({min(used):,.0f}–{max(used):,.0f} ₽)")}
    if new:
        price = statistics.median(new) * used_of_new
        return {"price": price, "confidence": "low",
                "comment": (f"б/у предложений нет; цена нового "
                            f"{statistics.median(new):,.0f} ₽ × {used_of_new:.2f} "
                            "(норматив «б/у от нового»)")}
    return {"price": None, "confidence": "low", "comment": "предложений не найдено"}


def _internal_reference(session: Session, item: Item) -> tuple[float | None, str]:
    """Оценка по нашим фактам — эталон для проверки правдоподобия находки."""
    from .calc import price_ladder

    cands = [c for c in price_ladder.candidates(session, item.name, item.unit, item)
             if c.source not in ("ai_estimate", "used_market_offer")]
    if not cands:
        return None, ""
    return cands[0].price, cands[0].label


def ai_price_for_item(session: Session, item_id: int) -> dict:
    """Найти и сохранить ИИ-цену для карточки справочника."""
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError("позиция не найдена")
    # Для лома ИИ-поиск вреден: он находит цену Б/У ИЗДЕЛИЯ вместо цены лома
    # (проверено: «Лом 5А (Труба 159)» → 31 500 ₽/т как за трубу, тогда как
    # чермет стоит ~20 000). Цены лома ведутся централизованно в прайсе.
    if item.family == "лом" or re.match(r"^\s*лом\b", item.name or "", re.I):
        raise ValueError(
            "Это ломовая позиция — цену берём из прайса лома, а не из поиска: "
            "ИИ находит цену б/у изделия и завышает оценку. "
            "Обновите цену материала в разделе «Цены лома».")

    unit = item.unit or "шт"
    offers: list[dict] = []
    urls: list[str] = []
    used_queries: list[str] = []
    for q in query_ladder(item.name):
        used_queries.append(q)
        got, u = ask_offers(q, unit)
        offers += got
        urls += u
        if len([o for o in got if o["condition"] != "new"]) >= 2:
            break   # достаточно б/у предложений, дальше не расширяем запрос

    agg = aggregate(offers, _used_of_new(session))
    price = agg["price"]
    if not price:
        return {"saved": False, "queries": used_queries,
                "comment": agg["comment"] or "нет данных"}

    ref, ref_label = _internal_reference(session, item)
    if ref and (price > ref * SANITY_FACTOR or price < ref / SANITY_FACTOR):
        return {"saved": False, "queries": used_queries, "price": price,
                "comment": (f"находка {price:,.0f} ₽/{unit} отклонена: расходится "
                            f"с оценкой по нашим фактам ({ref:,.0f} ₽/{unit}, "
                            f"{ref_label}) более чем в {SANITY_FACTOR:g} раз — "
                            "скорее всего найдено другое изделие")}

    sources = [o["url"] for o in offers if o.get("url")] or urls
    session.query(PriceQuote).filter(
        PriceQuote.item_id == item_id,
        PriceQuote.quote_type == "ai_estimate").delete(synchronize_session=False)
    session.add(PriceQuote(
        item_id=item_id,
        quote_type="ai_estimate",
        price=float(price),
        unit=item.unit,
        source="ИИ-поиск (" + os.environ.get("AI_PRICE_MODEL", DEFAULT_MODEL) + ")",
        source_url=sources[0] if sources else None,
        confidence=agg["confidence"],
        ttl_days=TTL_DAYS,
        notes=(f"запрос: {used_queries[-1]}; {agg['comment']}"
               + (f" · источники: {', '.join(sources[:3])}" if sources else ""))[:900],
    ))
    session.commit()
    return {"saved": True, "price": float(price), "sources": sources,
            "queries": used_queries, "offers": offers[:10],
            "comment": agg["comment"]}


# ---------------------------------------------------------------- пакетный поиск

import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

# Каждый запрос к провайдеру занимает 2–4 с, поэтому пачку гоняем в фоне и
# показываем прогресс. Реестр задач живёт в памяти процесса: история не нужна,
# найденные цены и так сохраняются в price_quotes.
_jobs: dict[str, dict] = {}
PAUSE_BETWEEN = 0.4  # щадим провайдера, чтобы не поймать ограничение частоты
MAX_BATCH = 300


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def start_batch(session_factory, item_ids: list[int], user: str = "",
                worker=None) -> dict:
    """Запустить фоновый поиск по списку карточек.

    worker(session, item_id) -> dict с ключом 'saved'; по умолчанию — поиск
    цены. Тем же механизмом гоняется поиск масс (ai_weight).
    """
    worker = worker or ai_price_for_item
    if not is_enabled():
        raise AiUnavailable(
            "ИИ-поиск выключен: не задан ключ провайдера в окружении службы.")
    item_ids = item_ids[:MAX_BATCH]
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "total": len(item_ids), "done": 0, "found": 0,
           "no_data": 0, "skipped": 0, "errors": 0, "status": "работает",
           "started_by": user, "log": []}
    _jobs[job_id] = job

    def run():
        session = session_factory()
        try:
            for item_id in item_ids:
                try:
                    result = worker(session, item_id)
                    if result.get("saved"):
                        job["found"] += 1
                        value = result.get("price") or result.get("mass_kg")
                        job["log"].append(f"{value:,.0f} — карточка #{item_id}")
                    else:
                        job["no_data"] += 1
                        note = (result.get("comment") or "")[:70]
                        if note:
                            job["log"].append(f"#{item_id}: {note}")
                except ValueError as e:  # лом и подобное — осознанный пропуск
                    job["skipped"] += 1
                    job["log"].append(f"пропуск #{item_id}: {str(e)[:60]}")
                except AiUnavailable as e:
                    job["errors"] += 1
                    job["log"].append(f"провайдер недоступен: {str(e)[:80]}")
                    job["status"] = "остановлена: провайдер недоступен"
                    return
                except Exception as e:  # noqa: BLE001 — одна позиция не рушит пачку
                    job["errors"] += 1
                    job["log"].append(f"ошибка #{item_id}: {type(e).__name__}")
                finally:
                    job["done"] += 1
                    job["log"] = job["log"][-10:]
                time.sleep(PAUSE_BETWEEN)
            job["status"] = "готово"
        finally:
            session.close()

    threading.Thread(target=run, daemon=True, name=f"ai-batch-{job_id}").start()
    return job
