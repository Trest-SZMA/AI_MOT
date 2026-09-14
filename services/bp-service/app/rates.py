"""Транспортные ставки перевозчика из приказа 1С.

Источник — выгрузка регистра «Установка транспортных ставок (учёт
металлолома)»: документы со ставками по маршрутам («Урай - Пермь»),
модели ТС и единице (руб/т, руб/рейс, руб/ч, руб/смена).

Решение от 18.08.2026 (по совещанию): документ вида «Ввод согласованного
прайса» — РАБОЧИЕ ставки; документ «Повышение текущих ставок» — только
ПОМЕТКА «на согласовании» рядом с рабочей ставкой (директор повышение не
подписывал). Ставки берутся из приказа, не из истории перевозок — это
прямое указание директора.

Сопоставление маршрута с плечом графа БП — по вхождению слов: сторона
маршрута «Урай» находится в узле «ХМАО, г. Урай, трубная база». Полное
совпадение имён невозможно: в приказе города, в графе — адреса площадок.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

# Обязательные колонки выгрузки. Имена фиксированы 1С; если формат сменится,
# parse_file назовёт, чего не хватает, вместо молчаливых нулей.
REQUIRED = ["Ссылка", "Маршрут", "МодельТС", "ЕдиницаИзмерения", "Цена"]
OPTIONAL = ["ДополнительныеУсловия", "СсылкаВидОперации", "СсылкаДата",
            "СсылкаНомер", "СсылкаОрганизация", "СсылкаОтветственный",
            "СсылкаСтатус", "СсылкаГуид"]

# Слова, которые не помогают узнать место (встречаются везде).
_STOPWORDS = {"обл", "область", "район", "гор", "город", "пос", "село",
              "ул", "улица", "база", "трубная", "площадка", "цех", "склад"}


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


def split_route(route: str) -> tuple[str, str]:
    """«Урай - Пермь» → («Урай», «Пермь»). Сначала « - » (имена мест сами
    содержат дефисы), потом одиночный дефис, иначе весь маршрут — откуда."""
    for sep in (" - ", " – ", "-"):
        if sep in route:
            a, _, b = route.partition(sep)
            return a.strip(), b.strip()
    return route.strip(), ""


def classify(doc_kind: str, conditions: str) -> str:
    """current — рабочая ставка (согласованный прайс);
    proposed — из документа повышения, ждёт подписи;
    was — строка «Было» документа повышения (дубль рабочей, для сверки)."""
    kind = (doc_kind or "").lower()
    if "прайс" in kind:
        return "current"
    if (conditions or "").strip().lower().startswith("было"):
        return "was"
    return "proposed"


def parse_file(path: Path) -> dict:
    """Разбор CSV выгрузки. Возвращает rows (все строки с классификацией)
    и docs (сводка по документам). Кидает ValueError с понятным текстом."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        missing = [c for c in REQUIRED if c not in headers]
        if missing:
            raise ValueError("в файле нет колонок: " + ", ".join(missing)
                             + ". Есть: " + ", ".join(headers[:12]))
        rows, skipped = [], 0
        for r in reader:
            route = (r.get("Маршрут") or "").strip()
            price = _f(r.get("Цена"))
            if not route or price <= 0:
                skipped += 1
                continue
            doc_kind = (r.get("СсылкаВидОперации") or "").strip()
            conditions = (r.get("ДополнительныеУсловия") or "").strip()
            frm, to = split_route(route)
            rows.append({
                "doc_number": (r.get("СсылкаНомер") or "").strip(),
                "doc_date": (r.get("СсылкаДата") or "").strip()[:10],
                "doc_kind": doc_kind,
                "doc_status": (r.get("СсылкаСтатус") or "").strip(),
                "organization": (r.get("СсылкаОрганизация") or "").strip(),
                "responsible": (r.get("СсылкаОтветственный") or "").strip(),
                "route": route, "route_from": frm, "route_to": to,
                "vehicle": (r.get("МодельТС") or "").strip(),
                "unit": (r.get("ЕдиницаИзмерения") or "").strip(),
                "price": price,
                "conditions": conditions,
                "kind": classify(doc_kind, conditions),
                "row_guid": (r.get("СсылкаГуид") or "").strip(),
            })
    docs: dict[str, dict] = {}
    for r in rows:
        d = docs.setdefault(r["doc_number"] or r["doc_kind"], {
            "doc_number": r["doc_number"], "doc_date": r["doc_date"],
            "doc_kind": r["doc_kind"], "doc_status": r["doc_status"],
            "organization": r["organization"], "rows": 0})
        d["rows"] += 1
    if not rows:
        raise ValueError("не найдено ни одной строки со ставкой")
    return {"rows": rows, "docs": sorted(docs.values(),
                                         key=lambda d: d["doc_date"]),
            "skipped": skipped}


def replace_all(conn: sqlite3.Connection, rows: list[dict],
                source_file: str) -> int:
    """Полная замена справочника: выгрузка каждый раз содержит все
    действующие документы, догружать её к прежней — плодить дубли."""
    conn.execute("DELETE FROM transport_rates")
    conn.executemany(
        "INSERT INTO transport_rates (doc_number, doc_date, doc_kind, "
        "doc_status, organization, responsible, route, route_from, route_to, "
        "vehicle, unit, price, conditions, kind, row_guid, source_file) "
        "VALUES (:doc_number, :doc_date, :doc_kind, :doc_status, "
        ":organization, :responsible, :route, :route_from, :route_to, "
        ":vehicle, :unit, :price, :conditions, :kind, :row_guid, "
        ":source_file)",
        [{**r, "source_file": source_file} for r in rows])
    return len(rows)


def summary(conn: sqlite3.Connection) -> dict:
    """Сводка для карточки справочника: документы, строки, дата загрузки."""
    docs = conn.execute(
        "SELECT doc_number, doc_date, doc_kind, doc_status, organization, "
        "COUNT(*) AS rows_n, MAX(loaded_at) AS loaded_at "
        "FROM transport_rates GROUP BY doc_number ORDER BY doc_date").fetchall()
    total = conn.execute("SELECT COUNT(*) AS c, MAX(loaded_at) AS t, "
                         "MAX(source_file) AS f FROM transport_rates").fetchone()
    return {"docs": docs, "total": total["c"], "loaded_at": total["t"],
            "source_file": total["f"]}


def current_rates(conn: sqlite3.Connection) -> list[dict]:
    """Рабочие ставки; предложение повышения — диапазоном на маршрут.

    Построчные пары «Было → Стало» намеренно НЕ строятся: у 1С маршрут
    «Когалым - Полевской» имеет две ставки (2300 и 2400) с одним гуидом
    маршрута, и любое сопоставление пар было бы выдумкой. Диапазон
    «Стало»-цен маршрута — то, что известно достоверно."""
    proposed: dict[tuple, dict] = {}
    for p in conn.execute(
            "SELECT route, vehicle, unit, MIN(price) AS lo, MAX(price) AS hi, "
            "MAX(doc_status) AS status, MAX(doc_number) AS doc_number, "
            "MAX(doc_date) AS doc_date FROM transport_rates "
            "WHERE kind = 'proposed' GROUP BY route, vehicle, unit"):
        proposed[(p["route"], p["vehicle"], p["unit"])] = dict(p)
    rows = []
    for c in conn.execute(
            "SELECT * FROM transport_rates WHERE kind = 'current' "
            "ORDER BY route, vehicle, unit, price"):
        p = proposed.get((c["route"], c["vehicle"], c["unit"]))
        rows.append({**dict(c),
                     "proposed_lo": p["lo"] if p else None,
                     "proposed_hi": p["hi"] if p else None,
                     "proposed_status": p["status"] if p else None,
                     "proposed_doc": p["doc_number"] if p else None,
                     "proposed_date": p["doc_date"] if p else None})
    return rows


# Заводы-покупатели → города их площадок: узел графа называют заводом
# («Чермет Волжский», «ВТЗ»), а маршрут приказа — городом («Котово-
# Волгоград»). Волжский — город-спутник Волгограда, экономисты применяют
# волгоградскую ставку. Подсказка показывает имя маршрута приказа рядом,
# так что подмена видна человеку.
_PLANT_CITIES = {
    "чермет": {"волжский", "волгоград"},
    "втз": {"волжский", "волгоград"},
    "красный октябрь": {"волгоград"},
    "вмпз": {"волгоград"},
}


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^\wёЁ]+", (text or "").lower())
    out = {w for w in words if len(w) >= 3 and w not in _STOPWORDS
           and not w.isdigit()}
    low = (text or "").lower()
    for plant, cities in _PLANT_CITIES.items():
        if plant in low:
            out |= cities
    return out


def side_matches(side: str, label: str) -> bool:
    """Сторона маршрута приказа находится в имени узла графа: хотя бы одно
    содержательное слово стороны есть среди слов узла."""
    return bool(_tokens(side) & _tokens(label))


def hints_for_edges(conn: sqlite3.Connection, edges: list) -> list[dict]:
    """Подсказки к плечам маршрута БП: какая ставка приказа подходит.

    Ничего не пишет — только показывает. Ставка руб/т сразу умножается на
    тоннаж плеча; руб/рейс переводится и на тонну через норматив загрузки
    машины (16 тн/рейс), чтобы цифры можно было сравнить."""
    rates = current_rates(conn)
    if not rates:
        return []
    truck = conn.execute(
        "SELECT value FROM cost_norms WHERE key = 'truck_capacity_t' "
        "AND base IS NULL").fetchone()
    truck_t = (truck["value"] if truck else 0) or 16.0
    out = []
    for e in edges:
        frm, to = e["from_label"], e["to_label"]
        found = [r for r in rates
                 if side_matches(r["route_from"], frm)
                 and side_matches(r["route_to"], to)]
        if not found:
            continue
        vol = e["volume_t"] or 0.0
        hints = []
        for r in found:
            per_t = None
            if r["unit"] == "т":
                per_t = r["price"]
            elif r["unit"] == "рейс" and truck_t:
                per_t = r["price"] / truck_t
            hints.append({
                "route": r["route"], "vehicle": r["vehicle"],
                "unit": r["unit"], "price": r["price"],
                "conditions": r["conditions"],
                "doc_number": r["doc_number"], "doc_date": r["doc_date"],
                "per_t": per_t,
                "total": per_t * vol if per_t and vol else None,
                "proposed_lo": r["proposed_lo"],
                "proposed_hi": r["proposed_hi"],
                "proposed_status": r["proposed_status"],
            })
        out.append({"edge_id": e["id"], "from_label": frm, "to_label": to,
                    "transport": e["transport"], "volume_t": vol,
                    "rates": hints})
    return out
