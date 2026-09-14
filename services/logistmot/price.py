"""Прайс транспортных ставок: загрузка выгрузки 1С, действующие ставки, поиск.

Источник — документ 1С «Установка транспортных ставок (учет металлолома)».
Попадает к нам двумя путями:

  • выгрузка CSV, залитая через панель (работает всегда, ни от чего не зависит);
  • напрямую из MS SQL «Extractor», куда 1С складывает таблицы, — по образцу
    /opt/metoptorg-realizaciya/src/sqlsrc.py (драйвер python-tds, см. sync_sql).

Зачем: служебная записка согласуется исходя из того, насколько ставка рейса
отклоняется от подписанного прайса. Значит, прайс должен лежать рядом с
заявками — в bot.db, а не открываться руками в 1С.

⚠️ Ставку НЕ определяет один только маршрут. У «Когалым - Первоуральск» в
прайсе три цены: 2300 без НДС, 2400 вкл. 5%, 2800 вкл. 22%. Полный ключ —
маршрут + модель ТС + единица + признак НДС + ставка НДС + доп. условие
(«до 20 тонн», «10 часов»). Сравнивать по одному маршруту — врать в отчёте.

⚠️ В документе «Повышение текущих ставок» строки идут парами «Было» / «Стало».
Действующая — только «Стало»; «Было» хранится ради истории и в расчёт не идёт.
"""

import csv
import io
import re
import time

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_docs (
    doc_guid    TEXT PRIMARY KEY,      -- СсылкаГуид: документ 1С
    number      TEXT,                  -- СсылкаНомер («000000006»)
    doc_date    INTEGER,               -- СсылкаДата, unix
    kind        TEXT,                  -- «Ввод согласованного прайса» и т.п.
    org         TEXT,                  -- СсылкаОрганизация
    responsible TEXT,                  -- СсылкаОтветственный
    status      TEXT,                  -- «На согласовании» / «Утверждён» …
    loaded_at   INTEGER NOT NULL,
    source      TEXT                   -- 'csv' | 'sql'
);

CREATE TABLE IF NOT EXISTS price_rates (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_guid  TEXT NOT NULL REFERENCES price_docs(doc_guid),
    line_no   INTEGER,
    route     TEXT,      -- как в 1С: «Когалым - Полевской»
    route_key TEXT,      -- нормализованный ключ: «когалым|полевской»
    vehicle   TEXT,      -- МодельТС: «тент/борт», «Кран 25 т»
    unit      TEXT,      -- т | рейс | ч | смена
    price     REAL,      -- как в документе
    vat_incl  INTEGER,   -- 1 = цена уже включает НДС
    vat_rate  TEXT,      -- «22%» | «5%» | «20%» | ''
    price_net REAL,      -- цена без НДС — по ней и сравниваем
    cond      TEXT,      -- доп. условие без «Было»/«Стало»
    phase     TEXT       -- '' | 'was' | 'now'
);

CREATE INDEX IF NOT EXISTS ix_price_rates_key ON price_rates (route_key);
CREATE INDEX IF NOT EXISTS ix_price_rates_doc ON price_rates (doc_guid);
"""


def init():
    with db.connect() as conn:
        conn.executescript(SCHEMA)


# --- нормализация ---------------------------------------------------------

# Разделитель маршрута: стрелка либо тире, у которого есть пробел ХОТЯ БЫ С
# ОДНОЙ стороны.
#
# ⚠️ Пробел с обеих сторон требовать нельзя: в самом прайсе адрес записан как
# «Астрахань, ул.Советской Гвардии 52- г.Абинск» — пробела перед тире нет, а
# логист напишет «52 - г.Абинск», и маршруты перестанут сходиться.
# Обойтись совсем без пробела тоже нельзя: «Пыть-Ях» и «Ханты-Мансийск»
# развалятся на два города. Тире внутри слова (буква-тире-буква) не трогаем.
ROUTE_SPLIT = re.compile(r"\s*(?:→|->|=>)\s*|\s+[-–—]+\s*|\s*[-–—]+\s+")

# «г.Абинск», «г. Пермь» — приставка города в ключ не идёт: в прайсе она
# стоит не везде, и из-за неё один и тот же город даёт два разных ключа.
CITY_PREFIX = re.compile(r"^(?:г|гор|пос|п|с|ст|д)\.\s*")


def route_key(route: str | None) -> str:
    """«Когалым - Полевской» и «Когалым → Полевской» → «когалым|полевской».

    Уточнения в скобках («Майский (СВК)») и адрес после запятой в ключ не
    идут: в прайсе они есть не всегда, а в заявке логиста бывают.
    """
    s = (route or "").strip().lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)", " ", s)
    parts = []
    for chunk in ROUTE_SPLIT.split(s):
        chunk = chunk.split(",")[0]
        chunk = re.sub(r"\s+", " ", chunk).strip(" .;:")
        chunk = CITY_PREFIX.sub("", chunk).strip()
        if chunk:
            parts.append(chunk)
    return "|".join(parts)


PHASE_RE = re.compile(r"^\s*(было|стало)\b\s*", re.IGNORECASE)
PHASE_CODE = {"было": "was", "стало": "now"}


def split_phase(cond: str | None) -> tuple[str, str]:
    """«Было 10 часов» → ('was', '10 часов'); «до 20 тонн» → ('', 'до 20 тонн')."""
    raw = (cond or "").strip()
    m = PHASE_RE.match(raw)
    if not m:
        return "", raw
    return PHASE_CODE[m.group(1).lower()], raw[m.end():].strip()


VAT_RATES = {"22%": 0.22, "20%": 0.20, "5%": 0.05, "10%": 0.10, "7%": 0.07}


def net_price(price: float, vat_incl: bool, vat_rate: str | None) -> float:
    """Цена без НДС — общий знаменатель для сравнения со ставкой записки."""
    k = VAT_RATES.get((vat_rate or "").strip())
    if vat_incl and k:
        return round(price / (1 + k), 2)
    return round(price, 2)


def _num(raw: str | None) -> float | None:
    s = re.sub(r"[^\d,.\-]", "", (raw or "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _ts(raw: str | None) -> int | None:
    """«2026-06-17 09:30:20.000 +0500» → unix. Смещение игнорируем: все даты
    в выгрузке в одном поясе, а нам нужен только порядок документов."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", (raw or "").strip())
    if not m:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", (raw or "").strip())
        if not m:
            return None
        y, mo, d = (int(x) for x in m.groups())
        h = mi = s = 0
    else:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
    import datetime as dt
    try:
        return int(dt.datetime(y, mo, d, h, mi, s).timestamp())
    except ValueError:
        return None


# --- загрузка выгрузки ----------------------------------------------------

REQUIRED_COLUMNS = ("СсылкаГуид", "Маршрут", "Цена", "ЕдиницаИзмерения")


def parse_csv(text: str) -> tuple[dict, list]:
    """Разобрать выгрузку 1С. Возвращает ({guid: документ}, [строки ставок]).

    Выгрузка приходит с BOM и иногда со склеенными кусками (шапка повторяется
    в середине файла) — повторную шапку молча пропускаем.
    """
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("пустой файл")
    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise ValueError("это не выгрузка ставок: нет колонок " + ", ".join(missing))

    docs: dict = {}
    rates: list = []
    for row in reader:
        guid = (row.get("СсылкаГуид") or "").strip().strip('"')
        if not guid or guid == "СсылкаГуид":   # повторная шапка внутри файла
            continue
        if guid not in docs:
            docs[guid] = {
                "doc_guid": guid,
                "number": (row.get("СсылкаНомер") or "").strip(),
                "doc_date": _ts(row.get("СсылкаДата")),
                "kind": (row.get("СсылкаВидОперации") or "").strip(),
                "org": (row.get("СсылкаОрганизация") or "").strip(),
                "responsible": (row.get("СсылкаОтветственный") or "").strip(),
                "status": (row.get("СсылкаСтатус") or "").strip(),
            }
        price = _num(row.get("Цена"))
        if price is None:
            continue
        vat_incl = (row.get("ЦенаВключаетНДС") or "").strip() in ("1", "true", "Да")
        vat_rate = (row.get("СтавкаНДС") or "").strip()
        phase, cond = split_phase(row.get("ДополнительныеУсловия"))
        route = (row.get("Маршрут") or "").strip()
        try:
            line_no = int((row.get("НомерСтроки") or "0").strip() or 0)
        except ValueError:
            line_no = 0
        rates.append({
            "doc_guid": guid,
            "line_no": line_no,
            "route": route,
            "route_key": route_key(route),
            "vehicle": (row.get("МодельТС") or "").strip(),
            "unit": (row.get("ЕдиницаИзмерения") or "").strip(),
            "price": price,
            "vat_incl": int(vat_incl),
            "vat_rate": vat_rate,
            "price_net": net_price(price, vat_incl, vat_rate),
            "cond": cond,
            "phase": phase,
        })
    if not rates:
        raise ValueError("в файле нет ни одной строки со ставкой")
    return docs, rates


def import_rows(docs: dict, rates: list, source: str = "csv") -> dict:
    """Записать документы и ставки. Идемпотентно: документ перезаливается
    целиком, чтобы повторная выгрузка не плодила дубли строк."""
    init()
    now = int(time.time())
    with db.connect() as conn:
        for guid, d in docs.items():
            conn.execute(
                """INSERT INTO price_docs (doc_guid, number, doc_date, kind, org,
                                           responsible, status, loaded_at, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(doc_guid) DO UPDATE SET
                       number = excluded.number, doc_date = excluded.doc_date,
                       kind = excluded.kind, org = excluded.org,
                       responsible = excluded.responsible,
                       status = excluded.status, loaded_at = excluded.loaded_at,
                       source = excluded.source""",
                (guid, d["number"], d["doc_date"], d["kind"], d["org"],
                 d["responsible"], d["status"], now, source),
            )
            conn.execute("DELETE FROM price_rates WHERE doc_guid = ?", (guid,))
        conn.executemany(
            """INSERT INTO price_rates (doc_guid, line_no, route, route_key, vehicle,
                                        unit, price, vat_incl, vat_rate, price_net,
                                        cond, phase)
               VALUES (:doc_guid, :line_no, :route, :route_key, :vehicle, :unit,
                       :price, :vat_incl, :vat_rate, :price_net, :cond, :phase)""",
            rates,
        )
    return {"docs": len(docs), "rates": len(rates),
            "routes": len({r["route_key"] for r in rates})}


def import_csv(text: str, source: str = "csv") -> dict:
    docs, rates = parse_csv(text)
    return import_rows(docs, rates, source)


# --- действующий прайс ----------------------------------------------------

RATE_KEY = ("route_key", "vehicle", "unit", "vat_incl", "vat_rate", "cond")


def effective_rates() -> list[dict]:
    """Действующие ставки: по каждому ключу — строка из самого свежего
    документа. Строки «Было» отбрасываются: они история, а не прайс."""
    init()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT r.*, d.number, d.doc_date, d.kind, d.status, d.org
               FROM price_rates r JOIN price_docs d ON d.doc_guid = r.doc_guid
               WHERE r.phase != 'was'
               ORDER BY d.doc_date, d.number, r.line_no"""
        ).fetchall()
    best: dict = {}
    for r in rows:                      # порядок по возрастанию даты —
        best[tuple(r[k] for k in RATE_KEY)] = dict(r)   # свежий затирает старый
    out = list(best.values())
    out.sort(key=lambda x: (x["route"] or "", x["unit"] or "", x["price"]))
    return out


def docs_summary() -> list[dict]:
    """Документы прайса: что и когда загружено, в каком статусе."""
    init()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT d.*,
                      (SELECT COUNT(*) FROM price_rates r
                        WHERE r.doc_guid = d.doc_guid)                 AS lines,
                      (SELECT COUNT(*) FROM price_rates r
                        WHERE r.doc_guid = d.doc_guid AND r.phase != 'was') AS active
               FROM price_docs d ORDER BY d.doc_date DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


APPROVED_STATUSES = ("утвержд", "согласован прайс", "действует")


def price_status() -> dict:
    """Состояние прайса для плашки в панели.

    Сейчас в 1С все документы приходят со статусом «На согласовании»: считаем
    по ним, но честно помечаем, что утверждённого прайса в базе нет.
    """
    docs = docs_summary()
    if not docs:
        return {"loaded": False, "approved": False,
                "note": "прайс не загружен"}
    top = docs[0]
    st = (top["status"] or "").lower()
    approved = any(w in st for w in APPROVED_STATUSES)
    return {
        "loaded": True,
        "approved": approved,
        "status": top["status"],
        "number": top["number"],
        "kind": top["kind"],
        "date": time.strftime("%d.%m.%Y", time.localtime(top["doc_date"]))
                if top["doc_date"] else None,
        "loaded_at": time.strftime("%d.%m.%Y %H:%M", time.localtime(top["loaded_at"])),
        "source": top["source"],
        "docs": len(docs),
        "note": ("" if approved else
                 f"документ в статусе «{top['status']}» — утверждённого прайса "
                 "в базе нет, расчёт ведём по нему"),
    }


# --- поиск ставки под конкретный рейс -------------------------------------

def candidates(route: str, rates: list | None = None) -> list[dict]:
    """Все действующие ставки по этому маршруту."""
    key = route_key(route)
    if not key:
        return []
    return [r for r in (rates if rates is not None else effective_rates())
            if r["route_key"] == key]


def lookup(route: str, unit: str | None = None, vehicle: str | None = None,
           cond: str | None = None, rates: list | None = None) -> dict | None:
    """Ставка прайса под рейс. Возвращает строку прайса или None.

    ⚠️ ЕДИНИЦА — ЖЁСТКОЕ УСЛОВИЕ, в отличие от модели ТС и доп. условия.
    Ставку за рейс нельзя сравнивать со ставкой за тонну: 35 000 ₽/рейс
    против 2 000 ₽/т давали «+1650%», и такая цифра уходила в отчёт
    директору. Нет строки в нужной единице — значит, сравнивать не с чем.
    Модель ТС и условие сужают выбор мягко: если точного совпадения нет,
    берём самый дешёвый вариант, а в ответе видно, по какой строке считали.
    """
    pool = candidates(route, rates)
    if not pool:
        return None
    if unit:
        want = str(unit).strip().lower()
        pool = [r for r in pool if (r["unit"] or "").strip().lower() == want]
        if not pool:
            return None

    def narrow(items, field, value):
        if not value:
            return items
        want = str(value).strip().lower()
        hit = [r for r in items if (r[field] or "").strip().lower() == want]
        return hit or items

    pool = narrow(pool, "vehicle", vehicle)
    pool = narrow(pool, "cond", cond)
    return min(pool, key=lambda r: r["price_net"])


def compare(route: str, rate_net: float, unit: str | None = None,
            vehicle: str | None = None, cond: str | None = None,
            rates: list | None = None) -> dict:
    """Насколько ставка рейса отклоняется от прайса.

    rate_net — ставка БЕЗ НДС (приводить к ней должен вызывающий: в записке
    ставка бывает записана и с НДС).
    """
    row = lookup(route, unit, vehicle, cond, rates)
    if row is None:
        # почему не сошлось — логисту это важнее, чем сухое «не найдено»
        pool = candidates(route, rates)
        if not pool:
            reason = "маршрута нет в прайсе"
        else:
            units = sorted({r["unit"] for r in pool if r["unit"]})
            reason = (f"по этому маршруту в прайсе есть ставка только за "
                      f"«{'», «'.join(units)}» — со ставкой за «{unit}» "
                      f"её сравнивать нельзя")
        return {"found": False, "route_key": route_key(route), "reason": reason}
    base = row["price_net"]
    diff = round(rate_net - base, 2)
    pct = round(diff * 100 / base, 1) if base else None
    return {
        "found": True,
        "price": base,
        "unit": row["unit"],
        "vehicle": row["vehicle"],
        "cond": row["cond"],
        "route": row["route"],
        "doc": row["number"],
        "doc_kind": row["kind"],
        "doc_status": row["status"],
        "doc_date": time.strftime("%d.%m.%Y", time.localtime(row["doc_date"]))
                    if row["doc_date"] else None,
        "diff": diff,
        "pct": pct,
        "over": diff > 0,
    }


if __name__ == "__main__":   # ручная загрузка: ./.venv/bin/python price.py файл.csv
    import sys
    if len(sys.argv) < 2:
        print("Использование: python price.py <выгрузка.csv>")
        raise SystemExit(2)
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        res = import_csv(f.read())
    print(f"Загружено: документов {res['docs']}, строк {res['rates']}, "
          f"маршрутов {res['routes']}")
    st = price_status()
    print(f"Прайс: {st['kind']} № {st['number']} от {st['date']} "
          f"({st['status']})")
    if st["note"]:
        print("⚠️ " + st["note"])
    print(f"Действующих ставок: {len(effective_rates())}")
