"""Готовность разделов БП: чего не хватает и что из-за этого не посчитается.

Смысл не в том, чтобы отметить пустые поля, а в том, чтобы назвать
ПОСЛЕДСТВИЕ. «Не задан шаг аукциона» человеку ничего не говорит; «без шага
аукциона не построится лестница торгов» — говорит.

Формулировки взяты из фактического поведения расчёта: каждая проверка
соответствует месту в коде, где отсутствие данных меняет результат или
блокирует выгрузку.
"""
from __future__ import annotations

# Порядок вкладок = порядок работы: сначала заводим сделку, потом лот,
# потом логистику, потом считаем экономику и принимаем решение.
TABS = [
    ("deal", "Сделка", "Шапка, стороны и параметры лота — заводятся один раз "
                       "при импорте, дальше почти не меняются."),
    ("lot", "Лот", "Состав позиций, сопоставление с 1С, реквизиты перечня и фото."),
    ("logistics", "Логистика", "Пункты отгрузки, плечи, рейсы и машины."),
    ("economics", "Экономика", "Затраты, P&L, экономика по базам, аукцион и сценарии."),
    ("decision", "Решение", "Риски, итог, статус, версии и выгрузка документов."),
]


def _has(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def deal_gaps(bp) -> list[str]:
    gaps = []
    if not _has(bp["seller_name"]):
        gaps.append("Не указан продавец — он попадает в документы и в договор.")
    if not _has(bp["division"]):
        gaps.append("Не указан дивизион — без него экономика по базам "
                    "и ориентир цены считаются по всему предприятию.")
    if not _has(bp["lot_cost"]):
        gaps.append("Не задана стоимость лота — вся закупка в расчёте равна нулю, "
                    "прибыль завышена.")
    if not _has(bp["auction_step"]):
        gaps.append("Не задан шаг аукциона — не построится лестница торгов "
                    "(до какой цены можно торговаться).")
    if not _has(bp["start_month"]):
        gaps.append("Не задан месяц начала вывоза — план-факт считается "
                    "периодами «1–3 мес» вместо кварталов, сезонность зимников "
                    "не применяется.")
    if not _has(bp["removal_months"]):
        gaps.append("Не задан срок вывоза — не построится график вывоза.")
    if not _has(bp["manager"]):
        gaps.append("Не указан ответственный менеджер.")
    return gaps


def lot_gaps(bp, items) -> list[str]:
    gaps = []
    if not items:
        gaps.append("В лоте нет ни одной позиции — считать нечего.")
        return gaps
    if not any(_has(it["sale_price"]) for it in items):
        gaps.append("Ни у одной позиции нет цены реализации — выручка нулевая.")
    no_price = [it for it in items if not _has(it["sale_price"])]
    if no_price and len(no_price) < len(items):
        gaps.append(f"Без цены реализации {len(no_price)} из {len(items)} позиций — "
                    "их выручка в расчёт не попадёт.")
    no_group = [it for it in items if not _has(it["cargo_group"])]
    if no_group:
        gaps.append(f"Без группы аналитического учёта {len(no_group)} позиций — "
                    "для них не работает ориентир цены и прогноз.")
    no_match = [it for it in items if not _has(it["nomen_1c"])]
    if len(no_match) == len(items):
        gaps.append("Позиции не сопоставлены с номенклатурой 1С — выгрузка "
                    "не примется.")
    elif no_match:
        gaps.append(f"Не сопоставлены с 1С {len(no_match)} позиций — они выпадут "
                    "из выгрузки.")
    return gaps


def logistics_gaps(points, routes_count: int) -> list[str]:
    gaps = []
    if not points:
        gaps.append("Пункты отгрузки не заведены — не посчитать рейсы, машины "
                    "и транспортные затраты.")
        return gaps
    no_distance = [p for p in points if not _has(p["distance_km"])]
    if no_distance:
        gaps.append(f"Не задано плечо у {len(no_distance)} пунктов из "
                    f"{len(points)} — рейсы и стоимость доставки по ним "
                    "не рассчитываются.")
    no_warehouse = [p for p in points if not _has(p["warehouse_name"])]
    if no_warehouse:
        gaps.append(f"Не сопоставлены со складом 1С {len(no_warehouse)} пунктов "
                    "— выгрузка транспорта уйдёт без склада.")
    return gaps


def economics_gaps(bp, costs, pnl, variant: str = "bsp") -> list[str]:
    gaps = []
    if not any(_has(c["amount"]) and c["amount"] for c in costs):
        gaps.append("Все статьи затрат нулевые — прибыль равна марже, "
                    "это не расчёт сделки.")
    if pnl and pnl.get("sale_volume", 0) <= 0:
        gaps.append("Объём реализации нулевой — проверьте объёмы позиций и засор.")
    # Контроль берётся по активному варианту: у «ДСП» и «Лукойла» свои
    # контрольные суммы в книге, и сравнение крест-накрест всегда врёт.
    control = bp[f"control_net_profit_{variant}"]
    if control is not None and pnl:
        diff = abs(pnl["net_profit"] - control)
        if diff > max(1.0, abs(control) * 0.001):
            gaps.append(f"Расчёт расходится с книгой экономистов на "
                        f"{diff:,.0f} руб".replace(",", " ") +
                        " — проверьте цены, статьи затрат и параметры.")
    return gaps


def decision_gaps(bp, risks, has_route: bool, matched_any: bool) -> list[str]:
    gaps = []
    if not risks:
        gaps.append("Риски не заполнены — раздел уходит в документ пустым.")
    if not _has(bp["recommendation"]):
        gaps.append("Нет рекомендации — руководителю нечего согласовывать.")
    if not has_route:
        gaps.append("Граф маршрута не построен — таблицы «план_транспорт» "
                    "и «план_переработка» уйдут в 1С пустыми.")
    if not matched_any:
        gaps.append("Нет ни одной позиции, сопоставленной с 1С — выгрузка "
                    "не примется.")
    return gaps


# Пробелы, из-за которых выгрузка в 1С не примется или расчёт заведомо
# неверен: такие вкладки помечаются красным, остальные — жёлтым.
BLOCKING = ("выгрузка не примется", "считать нечего", "выручка нулевая",
            "уйдут в 1С пустыми", "закупка в расчёте равна нулю")


def is_blocking(gaps: list[str]) -> bool:
    return any(any(mark in g for mark in BLOCKING) for g in gaps)


def build(gaps_by_tab: dict[str, list[str]], active: str) -> dict:
    """Полоса вкладок с метками и список пробелов активной вкладки."""
    tabs = []
    for code, title, hint in TABS:
        gaps = gaps_by_tab.get(code, [])
        tabs.append({
            "code": code, "title": title, "hint": hint,
            "gaps": gaps, "blocking": is_blocking(gaps),
            "done": not gaps,
        })
    return {
        "tabs": tabs,
        "tab": active,
        "tab_gaps": gaps_by_tab.get(active, []),
        "tab_blocking": is_blocking(gaps_by_tab.get(active, [])),
        "total_gaps": sum(len(g) for g in gaps_by_tab.values()),
    }
