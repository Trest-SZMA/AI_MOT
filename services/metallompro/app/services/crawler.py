"""Масштабный обход сайтов ломоприёмок и заводов (паттерн ScrapeGraphAI на нашем стеке).

Конвейер на источник:
  1. httpx-fetch страницы (быстро, бесплатно);
  2. эвристика цен (таблицы/текст) — как раньше;
  3. если эвристика пуста → LLM-извлечение (Perplexity sonar, без веб-поиска —
     дёшево): текст страницы → строгий JSON цен;
  4. котировки → market_quotes (metric lom3a_local / copper_local / alum_local,
     город/регион/источник, quality=live, метод в extra).

Разведка (discover): sonar-pro ищет новые сайты приёмок с ценами по региону →
кандидаты добавляются в реестр (added_by=ai_discover) и проверяются обходом.
"""
from __future__ import annotations
import json
import logging
import re
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import MarketQuote, ScrapeSource
from app.scrapers.base import make_client, parse_price, detect_metal
from app.services.market import save_quote

log = logging.getLogger("crawler")

# Границы разумных цен, ₽/т (защита от мусора LLM и парсера)
BOUNDS = {"lom3a_local": (8_000, 45_000), "copper_local": (300_000, 1_300_000),
          "alum_local": (60_000, 400_000)}
METAL_METRIC = {"A3_SCRAP": "lom3a_local", "A5_SCRAP": "lom3a_local",
                "A12_SCRAP": "lom3a_local", "COPPER": "copper_local",
                "ALUM": "alum_local"}

# Сид: проверенные источники из закладок Павла (v1)
SEED_SOURCES = [
    # (name, url, region, city, kind)
    ("ММК ВторМет", "https://mmk-vtormet.ru/prices/", "", "Магнитогорск", "plant"),
    ("Вторчермет НЛМК — Пермь", "https://www.uvchm.ru/prices/p-369/", "PERM", "Пермь", "plant"),
    ("Вторчермет НЛМК — Краснокамск", "https://www.uvchm.ru/price/krasnokamsk/", "PERM", "Краснокамск", "plant"),
    ("Акрон Скрап Урал", "https://ural.akron-scrap.ru/prajs/", "", "Екатеринбург", "local"),
    ("МЕТА-Пермь (ТМК)", "https://tmk-meta.tmk-group.ru/price-perm", "PERM", "Пермь", "plant"),
    ("Надеждинский МЗ (УМК-Сталь)", "https://mmc-steel.ru/factory/umk-stal/zakupki_tmc/zakupki-loma/", "", "Серов", "plant"),
    ("Абинский ЭМЗ — опт", "https://snab-service.com/priyem_metalloloma/tseny/optovyye_tseny_abinsk", "SOUTH", "Абинск", "plant"),
    ("УГМЕТ — чермет", "http://lom.ugmet.ru/ferrousmetals", "", "Екатеринбург", "local"),
    ("ЛомСпецПром Пермь", "https://lsp59.ru/", "PERM", "Пермь", "local"),
    ("МеталлПермь", "https://metalperm.ru/about_us.html", "PERM", "Пермь", "local"),
    ("Прием лома 59", "https://priemloma59.tilda.ws/#price", "PERM", "Пермь", "local"),
    ("Тройка-Мет Лысьва", "http://troyka-met.ru/zakupochnye-tseny", "PERM", "Лысьва", "local"),
    ("Рико-Металл", "https://riko-metall.ru/quotes/", "PERM", "Пермь", "local"),
    ("Сфера Усинск", "https://sferalom.ru/usinsk#rec592686914", "KOMI_NORTH", "Усинск", "local"),
    ("Вторчермет ХМАО Нижневартовск", "https://vchm86.ru/service/punkty-priema-metalla-v-nizhnevartovske/", "HMAO", "Нижневартовск", "local"),
    ("ИМПЕКС Волгоград", "https://vtormet-vlg.ru/price", "SOUTH", "Волгоград", "local"),
    ("ВолгоЛомСнаб", "http://volgalomsnab.ru/ceny-na-lom.html", "SOUTH", "Волгоград", "local"),
    ("ВСК-Мет Волгоград", "https://vsk-met.ru/price/", "SOUTH", "Волгоград", "local"),
    ("Транслом-Втормет Волгоград", "https://vgr.translom-vtormet.ru/prices/", "SOUTH", "Волгоград", "local"),
    ("ОРИОН Волгоград", "https://vtormet-orion.ru/price/", "SOUTH", "Волгоград", "local"),
    ("ВЦМ Волга Саратов", "https://vcmvolga.akron-holding.ru/price/", "SOUTH", "Саратов", "local"),
    ("Ферратек чермет", "https://www.ferratek.com/price/lom-chernyh-metallov", "SOUTH", "Волгоград", "local"),
]


def seed_sources(db: Session) -> int:
    added = 0
    existing = {u for (u,) in db.query(ScrapeSource.url).all()}
    for name, url, region, city, kind in SEED_SOURCES:
        if url in existing:
            continue
        db.add(ScrapeSource(name=name, url=url, region=region, city=city,
                            kind=kind, added_by="seed"))
        added += 1
    db.commit()
    return added


# ── Извлечение цен ───────────────────────────────────────────────
def _heuristic_prices(soup) -> list[dict]:
    """Быстрый бесплатный проход: таблицы, затем текст."""
    out = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            metal = detect_metal(" ".join(cells))
            if not metal:
                continue
            for cell in cells[1:]:
                p = parse_price(cell)
                if p and 500 < p < 2_000_000:
                    out.append({"metal": metal, "price": p})
                    break
    if not out:
        for el in soup.find_all(["div", "p", "li", "span"]):
            t = el.get_text(" ", strip=True)
            if not (5 < len(t) < 300):
                continue
            metal = detect_metal(t)
            if metal:
                p = parse_price(t)
                if p and 500 < p < 2_000_000:
                    out.append({"metal": metal, "price": p})
    return out


def _llm_prices(page_text: str, source_name: str) -> list[dict]:
    """LLM-извлечение (паттерн ScrapeGraphAI): текст → строгий JSON цен."""
    from app.services.ai import complete, llm_available, _parse_json_answer
    if not llm_available():
        return []
    text = re.sub(r"\s+", " ", page_text)[:7000]
    answer = complete(
        "Ты извлекаешь закупочные цены лома с сайтов приёма металлолома. "
        "Отвечай СТРОГО одним JSON без текста вокруг.",
        f"Сайт «{source_name}». Извлеки закупочные цены ЗА ТОННУ (если на сайте за кг — "
        "умножь на 1000). Верни JSON:\n"
        '{"prices":[{"metal":"A3_SCRAP|A5_SCRAP|A12_SCRAP|COPPER|ALUM","price":число_руб_за_тонну}]}\n'
        "Только реально указанные на странице цены, без выдумок. Если цен нет — "
        '{"prices":[]}.\n\nТЕКСТ СТРАНИЦЫ:\n' + text,
        max_tokens=600, web_search=False)
    data = _parse_json_answer(answer) or {}
    out = []
    for p in data.get("prices", []):
        try:
            out.append({"metal": str(p["metal"]), "price": float(p["price"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _best_by_metric(prices: list[dict]) -> dict[str, float]:
    """Лучшая (максимальная валидная) цена по каждой метрике."""
    best: dict[str, float] = {}
    for p in prices:
        metric = METAL_METRIC.get(p["metal"])
        if not metric:
            continue
        lo, hi = BOUNDS[metric]
        v = p["price"]
        if lo <= v <= hi and v > best.get(metric, 0):
            best[metric] = v
    return best


def crawl_source(db: Session, src: ScrapeSource, client) -> dict:
    """Обойти один источник. → {'ok': bool, 'method': ..., 'prices': {...}}"""
    from bs4 import BeautifulSoup
    import httpx
    try:
        try:
            r = client.get(src.url, timeout=20)
            r.raise_for_status()
        except httpx.ConnectError as e:
            if "SSL" not in str(e) and "certificate" not in str(e):
                raise
            # у мелких приёмок часто самоподписанные сертификаты — публичный
            # прайс читаем и так (данные не секретные, мы ничего не отправляем)
            with httpx.Client(headers=dict(client.headers), timeout=20,
                              follow_redirects=True, verify=False) as insecure:
                r = insecure.get(src.url)
                r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        src.fail_count += 1
        src.note = f"fetch: {str(e)[:120]}"
        return {"ok": False, "method": "fetch", "error": str(e)[:100]}

    prices = _heuristic_prices(soup)
    method = "heuristic"
    best = _best_by_metric(prices)
    if not best:
        page_text = soup.get_text(" ", strip=True)
        if len(page_text) > 200:
            prices = _llm_prices(page_text, src.name)
            method = "llm"
            best = _best_by_metric(prices)
    if not best:
        src.fail_count += 1
        src.note = "цены не найдены"
        return {"ok": False, "method": method, "error": "no prices"}

    for metric, value in best.items():
        dup = (db.query(MarketQuote)
               .filter(MarketQuote.metric == metric, MarketQuote.value == value,
                       MarketQuote.source == src.name)
               .order_by(MarketQuote.collected_at.desc()).first())
        if dup and (datetime.utcnow() - dup.collected_at).days < 1:
            continue
        save_quote(db, metric=metric, value=value, source=src.name, quality="live",
                   region=src.region or "", city=src.city or "", basis="FCA",
                   source_url=src.url,
                   extra={"via": method, "kind": src.kind})
    src.last_ok = datetime.utcnow()
    src.last_price = best.get("lom3a_local") or list(best.values())[0]
    src.last_method = method
    src.fail_count = 0
    src.note = ""
    return {"ok": True, "method": method, "prices": best}


def crawl_all(db: Session, limit: int = 60) -> dict:
    """Обход пачки источников (старейшие last_ok первыми). Вызывается планировщиком."""
    seed_sources(db)
    srcs = (db.query(ScrapeSource).filter(ScrapeSource.enabled == True)  # noqa: E712
            .order_by(ScrapeSource.last_ok.asc().nullsfirst()).limit(limit).all())
    stats = {"total": len(srcs), "ok": 0, "llm": 0, "fail": 0}
    with make_client() as client:
        for src in srcs:
            res = crawl_source(db, src, client)
            if res["ok"]:
                stats["ok"] += 1
                if res["method"] == "llm":
                    stats["llm"] += 1
            else:
                stats["fail"] += 1
            db.commit()
            time.sleep(0.7)
    log.info("Обход: %s", stats)
    return stats


# ── ИИ-разведка новых источников ─────────────────────────────────
DISCOVER_QUERIES = {
    "PERM": "Пермский край (Пермь, Березники, Соликамск, Чусовой, Лысьва)",
    "KOMI_NORTH": "Республика Коми (Усинск, Ухта, Сыктывкар, Печора)",
    "HMAO": "ХМАО-Югра (Сургут, Нижневартовск, Когалым, Нягань)",
    "SOUTH": "Волгоградская область и юг (Волгоград, Волжский, Камышин, Саратов)",
    "": "крупные города Урала и Поволжья (Екатеринбург, Челябинск, Уфа, Казань, Тюмень)",
}


def discover_sources(db: Session, region: str = "PERM", limit: int = 12) -> dict:
    """sonar-pro ищет сайты приёмок с опубликованными ценами → в реестр."""
    from app.services.ai import complete, llm_available, _parse_json_answer
    if not llm_available():
        return {"error": "ИИ не подключён"}
    area = DISCOVER_QUERIES.get(region, region)
    answer = complete(
        "Ты ищешь сайты компаний по приёму металлолома в России. Отвечай СТРОГО JSON.",
        f"Найди до {limit} САЙТОВ пунктов приёма металлолома (ломоприёмки, вторчермет, "
        f"вторцветмет) в регионе: {area}. Обязательное условие: на сайте опубликован "
        "прайс/цены приёма лома. Верни прямые ссылки на страницы с ценами.\n"
        'JSON: {"sites":[{"name":"...","url":"https://...","city":"..."}]}\n'
        "Только реально существующие сайты из результатов поиска, никаких выдуманных доменов.",
        max_tokens=1500, web_search=True)
    data = _parse_json_answer(answer) or {}
    existing = {u for (u,) in db.query(ScrapeSource.url).all()}
    domains = {re.sub(r"^www\.", "", (re.findall(r"https?://([^/]+)", u) or [""])[0])
               for u in existing}
    added, skipped = 0, 0
    for s in data.get("sites", []):
        url = str(s.get("url", "")).strip()
        if not url.startswith("http"):
            continue
        dom = re.sub(r"^www\.", "", (re.findall(r"https?://([^/]+)", url) or [""])[0])
        if url in existing or dom in domains:
            skipped += 1
            continue
        db.add(ScrapeSource(name=str(s.get("name", dom))[:250], url=url[:500],
                            region=region, city=str(s.get("city", ""))[:60],
                            kind="local", added_by="ai_discover"))
        existing.add(url)
        domains.add(dom)
        added += 1
    db.commit()
    return {"added": added, "skipped_dup": skipped, "region": region}


def crawler_status(db: Session) -> dict:
    from sqlalchemy import func
    total = db.query(func.count(ScrapeSource.id)).scalar()
    ok = db.query(func.count(ScrapeSource.id)).filter(ScrapeSource.last_ok != None).scalar()  # noqa: E711
    llm = (db.query(func.count(ScrapeSource.id))
           .filter(ScrapeSource.last_method == "llm").scalar())
    disc = (db.query(func.count(ScrapeSource.id))
            .filter(ScrapeSource.added_by == "ai_discover").scalar())
    return {"total": total, "with_price": ok, "via_llm": llm, "ai_discovered": disc}
