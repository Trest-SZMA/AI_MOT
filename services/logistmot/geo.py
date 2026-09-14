"""Подсказка города с регионом для маршрута заявки.

Источник — Nominatim (OpenStreetMap): бесплатно, без ключа и регистрации,
условия — не чаще запроса в секунду и честный User-Agent. Для двух логистов
этого с запасом; на всякий случай ответы кэшируются в bot.db (таблица kv),
и второй раз «Чернушка» отдаётся без похода в интернет.

Зачем регион: «Чернушка» есть в Пермском крае и в Марий Эл, «Советский» —
в ХМАО и в Кировской области. Перевозчику нужно знать, куда ехать.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request

import db

URL = "https://nominatim.openstreetmap.org/search"
URL_REV = "https://nominatim.openstreetmap.org/reverse"
UA = "LogistMOT/1.0 (metoptorg logistics panel; contact: logistics@metoptorg)"
CACHE_TTL = 30 * 86400
_lock = threading.Lock()
_last = 0.0

# что считаем населённым пунктом (типы OSM place=*)
PLACE_TYPES = {"city", "town", "village", "hamlet", "municipality", "locality",
               "suburb", "isolated_dwelling"}
ADMIN_PREFIX = re.compile(
    r"^(?:городской округ|городское поселение|сельское поселение|"
    r"муниципальный округ|муниципальный район|город|посёлок|поселок|село|"
    r"деревня|пгт)\s+", re.IGNORECASE)
TYPE_RU = {"city": "город", "town": "город", "village": "село", "hamlet": "деревня",
           "district": "район",
           "municipality": "муниципалитет", "locality": "местность",
           "suburb": "район", "isolated_dwelling": "хутор"}


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _fetch(q: str) -> list[dict]:
    global _last
    params = {"q": q, "format": "jsonv2", "countrycodes": "ru", "limit": 10,
              "accept-language": "ru", "addressdetails": 1}
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": UA})
    with _lock:                       # правило Nominatim: ≤ 1 запрос/с
        wait = 1.05 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = json.loads(r.read().decode("utf-8"))
    out, seen = [], set()
    for it in raw:
        # «Пыть-Ях» OSM отдаёт не как place, а как границу «городской округ
        # Пыть-Ях» — ориентируемся на addresstype и берём имя из адреса
        kind = it.get("addresstype") or it.get("type")
        if it.get("category") not in ("place", "boundary") or kind not in PLACE_TYPES:
            continue
        addr = it.get("address") or {}
        name = addr.get(kind) or it.get("name") or ""
        name = ADMIN_PREFIX.sub("", name).strip()
        region = addr.get("state") or addr.get("region") or ""
        if not name or (name, region) in seen:
            continue
        seen.add((name, region))
        try:
            lat, lon = round(float(it["lat"]), 5), round(float(it["lon"]), 5)
        except (KeyError, ValueError, TypeError):
            lat = lon = None
        out.append({"name": name, "region": region,
                    "kind": TYPE_RU.get(kind, kind), "lat": lat, "lon": lon,
                    "district": addr.get("county") or addr.get("municipality") or ""})
    # точное совпадение имени — первым: на «Советский» OSM ставит выше
    # Вилючинск (бывший Советский), а нужен обычно тот, что так и называется
    out.sort(key=lambda x: 0 if x["name"].lower() == q.lower() else 1)
    return out[:8]


def suggest(q: str) -> dict:
    """{"items": [...], "source": "cache"|"osm"|"error"}."""
    key = _norm(q)
    if len(key) < 3:
        return {"items": [], "source": "short"}
    cache_key = "geo2:" + key   # geo2: с координатами
    cached = db.get_kv(cache_key)
    if cached:
        try:
            c = json.loads(cached)
            if time.time() - c.get("at", 0) < CACHE_TTL:
                return {"items": c["items"], "source": "cache"}
        except (ValueError, KeyError):
            pass
    try:
        items = _fetch(key)
    except Exception as exc:  # noqa: BLE001 — интернет/OSM недоступны
        return {"items": [], "source": "error", "error": str(exc)[:120]}
    db.set_kv(cache_key, json.dumps({"at": int(time.time()), "items": items},
                                    ensure_ascii=False))
    return {"items": items, "source": "osm"}


SHORT_REGION = (
    (re.compile(r"^Ханты-Мансийский автономный округ.*$"), "ХМАО"),
    (re.compile(r"^Ямало-Ненецкий автономный округ$"), "ЯНАО"),
    (re.compile(r"^Республика\s+"), ""),
    (re.compile(r"\sобласть$"), " обл."),
)


def short_region(region: str) -> str:
    """«Ханты-Мансийский автономный округ — Югра» → «ХМАО», «Свердловская
    область» → «Свердловская обл.»: в карточке маршрут должен читаться."""
    for rx, repl in SHORT_REGION:
        region = rx.sub(repl, region)
    return region.strip()


def label(item: dict) -> str:
    """Как город пишется в маршруте: «Чернушка (Пермский край)».
    «Пермь (Пермский край)» не пишем — регион и так в названии."""
    name, region = item.get("name") or "", item.get("region") or ""
    if not region or region == name or region[:4].lower() == name[:4].lower():
        return name
    return f"{name} ({short_region(region)})"


# --- точка на карте → город --------------------------------------------------

def _pick_place(addr: dict) -> tuple[str, str]:
    """Из адреса OSM — населённый пункт и его тип; для точки в поле берём
    ближайший к городу уровень, что есть в адресе."""
    for kind in ("city", "town", "village", "hamlet", "municipality", "locality"):
        if addr.get(kind):
            return addr[kind], kind
    return "", ""


def reverse(lat: float, lon: float) -> dict:
    """Клик по карте → {"name", "region", "kind", "label", "lat", "lon"}.
    Пустой name — точка вне населённого пункта (в поле, на трассе)."""
    key = f"georev2:{lat:.3f},{lon:.3f}"
    cached = db.get_kv(key)
    if cached:
        try:
            c = json.loads(cached)
            if time.time() - c.get("at", 0) < CACHE_TTL:
                return c["item"]
        except (ValueError, KeyError):
            pass
    # zoom 10 — уровень города: точка внутри границ пункта. Если кликнули в
    # поле рядом (на мелком масштабе иначе не бывает) — zoom 14 отдаёт
    # ближайший адрес, а в нём деревню/посёлок/город, к которому он относится
    addr, name, kind = {}, "", ""
    for zoom in (10, 16):
        params = {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2",
                  "zoom": zoom, "accept-language": "ru", "addressdetails": 1}
        req = urllib.request.Request(URL_REV + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": UA})
        global _last
        with _lock:
            wait = 1.05 - (time.time() - _last)
            if wait > 0:
                time.sleep(wait)
            _last = time.time()
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = json.loads(r.read().decode("utf-8"))
        addr = raw.get("address") or {}
        name, kind = _pick_place(addr)
        if name:
            break
    if not name and addr.get("county"):
        # точка в лесу или на трассе: отдаём округ/район — «Верхняя Пышма»
        # из «городской округ Верхняя Пышма», «Чернушинский район» как есть
        county = addr["county"]
        if ADMIN_PREFIX.match(county):
            name, kind = county, "city"
        else:
            name = re.sub(r"муниципальный (?:округ|район)", "район", county)
            kind = "district"
    name = ADMIN_PREFIX.sub("", name).strip()
    item = {"name": name, "region": addr.get("state") or addr.get("region") or "",
            "kind": TYPE_RU.get(kind, kind), "lat": round(lat, 5), "lon": round(lon, 5),
            "district": addr.get("county") or addr.get("municipality") or ""}
    item["label"] = label(item) if name else ""
    db.set_kv(key, json.dumps({"at": int(time.time()), "item": item}, ensure_ascii=False))
    return item
