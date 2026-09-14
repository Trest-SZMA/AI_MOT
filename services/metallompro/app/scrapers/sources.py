"""Скраперы источников. Каждый возвращает список котировок-словарей для save_quote.

Формат: {metric, value, source, quality, region, city, basis, unit, source_url}
Если сайт недоступен — источник просто ничего не возвращает (не выдумываем).
"""
from __future__ import annotations
import logging
import re
import time
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.config import METALS_API_KEY
from app.scrapers.base import make_client, parse_price, detect_metal

log = logging.getLogger("scrapers")


# ── ЦБ РФ: курсы + ключевая ставка ───────────────────────────────
def scrape_cbr() -> list[dict]:
    out = []
    with make_client() as client:
        try:
            r = client.get("https://www.cbr.ru/scripts/XML_daily.asp")
            r.raise_for_status()
            tree = ElementTree.fromstring(r.content)
            for v in tree.iter("Valute"):
                code = v.findtext("CharCode")
                if code in ("USD", "EUR", "CNY"):
                    val = float(v.findtext("Value").replace(",", "."))
                    nominal = float(v.findtext("Nominal").replace(",", "."))
                    out.append({"metric": f"{code.lower()}_rub", "value": round(val / nominal, 4),
                                "source": "ЦБ РФ", "quality": "live", "unit": "RUB",
                                "source_url": "https://www.cbr.ru"})
        except Exception as e:
            log.warning("ЦБ РФ курсы: %s", e)
        try:
            r = client.get("https://www.cbr.ru/hd_base/KeyRate/")
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            table = soup.find("table")
            if table:
                first = table.find_all("tr")[1]
                cells = [c.get_text(strip=True) for c in first.find_all("td")]
                if len(cells) >= 2:
                    rate = parse_price(cells[1])
                    if rate and 3 < rate < 30:
                        out.append({"metric": "key_rate", "value": rate, "source": "ЦБ РФ",
                                    "quality": "live", "unit": "%",
                                    "source_url": "https://www.cbr.ru/hd_base/KeyRate/"})
        except Exception as e:
            log.warning("ЦБ РФ ставка: %s", e)
    return out


# ── Транслом: главный живой индекс ───────────────────────────────
TRANSLOM_CACHE = [  # значения от 22.07.2026 — честно помечаются quality=cache
    {"metric": "translom_index", "value": 17810, "region": "РФ", "basis": "INDEX"},
    {"metric": "lom3a", "value": 20500, "region": "УРАЛ", "city": "Екатеринбург", "basis": "FCA"},
    {"metric": "lom3a", "value": 21990, "region": "УРАЛ", "basis": "CPT_RD"},
    {"metric": "lom3a", "value": 22099, "region": "ЮГ", "basis": "CPT_RD"},
]


_TL_CITY_REGION = {"Екатеринбург": "УРАЛ", "Татарстан": "ПОВОЛЖЬЕ", "Новосибирск": "СИБИРЬ",
                   "Москва и МО": "ЦЕНТР"}
_TL_CPT_REGION = {"Урал": "УРАЛ", "Юг": "ЮГ", "Центр": "ЦЕНТР", "Сибирь": "СИБИРЬ"}


def scrape_translom() -> list[dict]:
    """translom.ru/graph — полный срез рынка: 3А по регионам, HMS, FOB, медь, алюминий."""
    url = "https://translom.ru/graph/"
    out, dedup = [], set()

    def add(metric, value, lo, hi, *, region="", city="", basis="", unit="RUB/т"):
        key = (metric, region, city, basis)
        if value and lo < value < hi and key not in dedup:
            dedup.add(key)
            out.append({"metric": metric, "value": value, "region": region, "city": city,
                        "basis": basis, "unit": unit, "source": "Транслом",
                        "quality": "live", "source_url": url})

    with make_client() as client:
        try:
            r = client.get(url)
            r.raise_for_status()
            text = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True)
            # 3А: FCA по городам и CPT ж/д по макрорегионам
            for m in re.finditer(r"3А, (FCA|CPT ж/д) ([А-Яа-яёЁ\-\s]+?) ([\d\s]{4,10}) ₽", text):
                kind, place, price = m.group(1), m.group(2).strip(), parse_price(m.group(3))
                if kind == "FCA":
                    add("lom3a", price, 10_000, 45_000,
                        region=_TL_CITY_REGION.get(place, place), city=place, basis="FCA")
                else:
                    add("lom3a", price, 10_000, 45_000,
                        region=_TL_CPT_REGION.get(place, place), basis="CPT_RD")
            # Внешний рынок
            for pat, metric in [(r"HMS 1/2 80:20, CFR Турция (\d+) \$", "hms_turkey"),
                                (r"3А, FOB Балтийское море (\d+) \$", "fob_baltic_usd"),
                                (r"3А, FOB Черное море (\d+) \$", "fob_black_sea_usd")]:
                m = re.search(pat, text)
                if m:
                    add(metric, parse_price(m.group(1)), 100, 900, basis="INDEX", unit="USD/т")
            # Цветмет
            m = re.search(r"Медь 3 сорт, FCA ([\d\s]{5,12}) ₽", text)
            if m:
                add("copper_scrap_rf", parse_price(m.group(1)), 300_000, 2_000_000,
                    region="РФ", basis="FCA")
            m = re.search(r"Медь LME ([\d\s]{4,9}) \$", text)
            if m:
                add("copper_lme", parse_price(m.group(1)), 4_000, 30_000,
                    basis="EXCH", unit="USD/т")
            m = re.search(r"Алюминий смешанный, FCA ([\d\s]{5,12}) ₽", text)
            if m:
                add("alum_scrap_rf", parse_price(m.group(1)), 50_000, 500_000,
                    region="РФ", basis="FCA")
            m = re.search(r"Алюминий LME ([\d\s]{4,9}) \$", text)
            if m:
                add("alum_lme", parse_price(m.group(1)), 1_000, 8_000,
                    basis="EXCH", unit="USD/т")
        except Exception as e:
            log.warning("Транслом: %s", e)
    if not out:  # сайт недоступен → кеш с честной пометкой
        for c in TRANSLOM_CACHE:
            out.append({**c, "source": "Транслом (кеш 22.07.2026)", "quality": "cache",
                        "source_url": url})
    return out


# ── Прайсы заводов и приёмок из закладок Павла ───────────────────
PLANT_SOURCES = [
    # (metric_id, имя, url, регион, город, basis)
    ("plant_MMK",     "ММК ВторМет",                  "https://mmk-vtormet.ru/prices/",  "УРАЛ", "Магнитогорск", "CPT_AUTO"),
    ("uvchm_perm",    "Вторчермет НЛМК — Пермь",      "https://www.uvchm.ru/prices/p-369/", "УРАЛ", "Пермь", "FCA"),
    ("akron_ural",    "Акрон Скрап Урал",             "https://ural.akron-scrap.ru/prajs/", "УРАЛ", "Екатеринбург", "FCA"),
    ("tmk_meta_perm", "МЕТА-Пермь (ТМК)",             "https://tmk-meta.tmk-group.ru/price-perm", "УРАЛ", "Пермь", "FCA"),
    ("abinsk_optom",  "Абинский ЭМЗ (опт)",           "https://snab-service.com/priyem_metalloloma/tseny/optovyye_tseny_abinsk", "ЮГ", "Абинск", "CPT_AUTO"),
    ("lsp59",         "ЛомСпецПром Пермь",            "https://lsp59.ru/", "УРАЛ", "Пермь", "FCA"),
    ("vtormet_vlg",   "ИМПЕКС Волгоград",             "https://vtormet-vlg.ru/price", "ЮГ", "Волгоград", "FCA"),
    ("vchm86",        "Вторчермет Нижневартовск",     "https://vchm86.ru/service/punkty-priema-metalla-v-nizhnevartovske/", "УРАЛ", "Нижневартовск", "FCA"),
]


def _find_price_3a(soup: BeautifulSoup) -> float | None:
    """Ищем цену чермета (3А приоритетно) в таблицах, потом в тексте."""
    candidates: list[float] = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            metal = detect_metal(" ".join(cells))
            if metal not in ("A3_SCRAP", "A5_SCRAP", "A12_SCRAP"):
                continue
            for cell in cells[1:]:
                p = parse_price(cell)
                if p and 8_000 < p < 40_000:
                    candidates.append(p)
    if not candidates:
        for el in soup.find_all(["div", "p", "li", "span"]):
            t = el.get_text(separator=" ", strip=True)
            if not (5 < len(t) < 300) or detect_metal(t) != "A3_SCRAP":
                continue
            p = parse_price(t)
            if p and 8_000 < p < 40_000:
                candidates.append(p)
    return max(candidates) if candidates else None


def scrape_plants() -> list[dict]:
    out = []
    with make_client() as client:
        for metric, name, url, region, city, basis in PLANT_SOURCES:
            try:
                time.sleep(1.0)
                r = client.get(url, timeout=20)
                r.raise_for_status()
                price = _find_price_3a(BeautifulSoup(r.text, "lxml"))
                if price:
                    out.append({"metric": metric, "value": price, "source": name,
                                "quality": "live", "region": region, "city": city,
                                "basis": basis, "source_url": url})
                    log.info("✓ %s: %s ₽/т", name, price)
            except Exception as e:
                log.warning("✗ %s: %s", name, e)
    return out


# ── LME через metals-api ─────────────────────────────────────────
TROY_OZ_PER_TONNE = 32_150.7
LME_SYMBOLS = {"copper_lme": "LME-XCU", "alum_lme": "USDALU", "nickel_lme": "USDNI",
               "steel_lme": "USDSTEEL-RE"}


def scrape_lme() -> list[dict]:
    if not METALS_API_KEY:
        return []
    out = []
    with make_client() as client:
        try:
            r = client.get("https://metals-api.com/api/latest",
                           params={"access_key": METALS_API_KEY, "base": "USD",
                                   "symbols": ",".join(LME_SYMBOLS.values())})
            data = r.json()
            if not data.get("success"):
                log.warning("metals-api: %s", data.get("error"))
                return []
            for metric, sym in LME_SYMBOLS.items():
                rate = data.get("rates", {}).get(sym)
                if rate and rate > 0:
                    price = round((1.0 / rate) * TROY_OZ_PER_TONNE, 2)
                    out.append({"metric": metric, "value": price, "source": "metals-api (LME)",
                                "quality": "live", "unit": "USD/т", "basis": "EXCH",
                                "source_url": "https://metals-api.com"})
        except Exception as e:
            log.warning("metals-api: %s", e)
    return out


# ── Новости ──────────────────────────────────────────────────────
NEWS_SOURCES = [
    ("Русмет", "https://rusmet.ru/"),
    ("МеталлБюллетень", "https://www.metalbulletin.ru/news/scrap/"),
    ("MetalTorg", "https://www.metaltorg.ru/news/"),
]
NEWS_KEYWORDS = ["лом", "металл", "сталь", "прокат", "цен", "завод", "ммк", "нлмк",
                 "северсталь", "арматур", "scrap"]


def scrape_news() -> list[dict]:
    out = []
    with make_client() as client:
        for source, url in NEWS_SOURCES:
            try:
                r = client.get(url, timeout=20)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "lxml")
                seen = set()
                for el in soup.find_all(["h1", "h2", "h3", "h4", "a"], limit=80):
                    title = el.get_text(strip=True)
                    if not (20 < len(title) < 300) or title in seen:
                        continue
                    if not any(kw in title.lower() for kw in NEWS_KEYWORDS):
                        continue
                    href = el.get("href", "")
                    out.append({"source": source, "title": title,
                                "url": href if href.startswith("http") else url})
                    seen.add(title)
            except Exception as e:
                log.warning("Новости %s: %s", source, e)
    return out
