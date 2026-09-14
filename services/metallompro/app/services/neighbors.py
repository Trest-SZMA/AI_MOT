"""Интеграция с соседними сервисами МетОптТорг на этом же сервере.

1. «Остатки» (:8090) — отдаёт сверенные фактические остатки (data.json).
   Берём тонны лома по базам → регионы калькулятора. Это лучше нашего
   регистра оборотов: их цифры прошли ручную сверку с пересчётом на складах.

2. «Реализация» (:8092) — каждую ночь тянет свежие таблицы из MS SQL
   «Extractor» (1С) и складывает CSV в свою папку data/. Мы подхватываем
   новые файлы себе в импортёр — дедупликация по строкам не даст дублей.
"""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("neighbors")

OSTATKI_URL = os.getenv("NEIGHBOR_OSTATKI_URL", "http://127.0.0.1:8090/data.json")
REALIZACIYA_DIR = Path(os.getenv("NEIGHBOR_1C_DIR", "/opt/metoptorg-realizaciya/data"))
# какие файлы соседа забираем себе (маска → наш импортёр их понимает)
NEIGHBOR_PATTERNS = ["СебестоимостьТоваровОбороты_*.csv"]

# База «Остатков» → регион калькулятора
BASE_REGION = [
    ("усинск", "KOMI_NORTH"), ("ухта", "KOMI_NORTH"),
    ("осенцы", "PERM"), ("березники", "PERM"), ("оса", "PERM"),
    ("свк", "PERM"), ("майский", "PERM"), ("мгм", "PERM"), ("камасталь", "PERM"),
    ("советский", "HMAO"), ("когалым", "HMAO"), ("лангепас", "HMAO"),
    ("волгоград", "SOUTH"),
    # Коломна — Центр, маршрутов в калькуляторе нет: попадёт в unmapped
]

_cache: dict = {"at": 0.0, "data": None}
CACHE_TTL = 900  # 15 минут


def ostatki_stock() -> dict | None:
    """Остатки лома по регионам из сервиса «Остатки».

    → {"regions": {"PERM": t, ...}, "unmapped": {"база": t},
       "total_t": t, "actual_date": "17.07.2026", "source": "..."}
    None — если сервис недоступен (честно покажем, что данных нет).
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    try:
        r = httpx.get(OSTATKI_URL, timeout=15)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.warning("Остатки (:8090) недоступны: %s", e)
        return _cache["data"]  # отдадим устаревший кеш, если был

    regions: dict[str, float] = {}
    unmapped: dict[str, float] = {}
    bases: dict[str, dict] = {}
    total = 0.0
    for row in d.get("rows", []):
        if (row.get("category") or "") != "Лом":
            continue
        tonnes = float(row.get("tonnes") or 0)
        if tonnes <= 0:
            continue
        base_name = (row.get("base") or "?").strip()
        region = next((reg for kw, reg in BASE_REGION if kw in base_name.lower()), None)
        if region:
            regions[region] = regions.get(region, 0) + tonnes
        else:
            unmapped[base_name] = unmapped.get(base_name, 0) + tonnes
        b = bases.setdefault(base_name, {"base": base_name, "tonnes": 0.0,
                                         "region": region})
        b["tonnes"] += tonnes
        total += tonnes
    bases_list = sorted(bases.values(), key=lambda x: -x["tonnes"])
    for b in bases_list:
        b["tonnes"] = round(b["tonnes"], 1)
    out = {
        "regions": {k: round(v, 1) for k, v in regions.items()},
        "unmapped": {k: round(v, 1) for k, v in unmapped.items()},
        "bases": bases_list,
        "total_t": round(total, 1),
        "actual_date": d.get("meta", {}).get("actual_date", "?"),
        "source": "сервис «Остатки» (:8090), сверенные данные",
    }
    _cache.update(at=now, data=out)
    return out


def pull_neighbor_csv(inbox: Path) -> list[str]:
    """Скопировать в наш inbox новые CSV соседа-«Реализации» (если папка доступна).

    ВАЖНО: сосед может выгружать регистр хоть каждые 30 минут (файл ~315 МБ с новым
    timestamp в имени). Данные меняются раз в сутки, поэтому каждый тип файла
    (префикс до «_») подхватываем НЕ ЧАЩЕ раза в 20 часов — иначе диск и память
    сервера уходят на бесполезные копии-дубли.
    """
    copied = []
    if not REALIZACIYA_DIR.is_dir():
        return copied
    from datetime import datetime, timedelta
    from app.db import session_scope
    from app.models import ImportLog

    def _prefix(name: str) -> str:
        return name.rsplit("_", 1)[0]     # СебестоимостьТоваровОбороты_2026...csv → префикс

    cutoff = datetime.utcnow() - timedelta(hours=20)
    with session_scope() as db:
        seen = {f for (f,) in db.query(ImportLog.filename).all()}
        recent_prefixes = {_prefix(f) for (f,) in
                           db.query(ImportLog.filename)
                           .filter(ImportLog.created_at >= cutoff,
                                   ImportLog.status == "ok").all()}
    for pattern in NEIGHBOR_PATTERNS:
        candidates = sorted(REALIZACIYA_DIR.glob(pattern))
        if not candidates:
            continue
        src = candidates[-1]              # только самый свежий файл типа
        if (src.name in seen or (inbox / src.name).exists()
                or _prefix(src.name) in recent_prefixes):
            continue
        try:
            (inbox / src.name).write_bytes(src.read_bytes())
            copied.append(src.name)
            log.info("Забрал у «Реализации»: %s", src.name)
        except OSError as e:
            log.warning("Не смог скопировать %s: %s", src.name, e)
    return copied
