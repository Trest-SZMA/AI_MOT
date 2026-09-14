"""Сравнение версий расчёта БП.

Каждая правка карточки пишет снимок в `bp_versions.snapshot_json` (сделка,
позиции, статьи затрат, риски). Здесь снимки сравниваются построчно, чтобы
на вопрос «что изменилось между v3 и v7» отвечал сервис, а не память
экономиста.

Сравнение идёт по идентификаторам строк, а НЕ по их порядку: позиции
добавляют, делят и удаляют, и сопоставление по номеру строки показало бы
разницу там, где просто сдвинулся список.
"""
from __future__ import annotations

# Понятные названия полей. Чего нет в словаре — показывается именем колонки:
# лучше техническое имя, чем пропущенное изменение.
BP_LABELS = {
    "status": "Статус", "scenario": "Сценарий / версия расчёта",
    "manager": "Ответственный менеджер", "source_type": "Форма работы",
    "tender_ref": "№ запроса / перечня", "division": "Дивизион",
    "seller_name": "Продавец", "buyer_name": "Покупатель",
    "lot_cost": "Стоимость лота, руб", "auction_step": "Шаг аукциона, руб",
    "contamination_pct": "Засор, %", "removal_months": "Срок вывоза, мес",
    "start_month": "Месяц начала", "shipment_type": "Вид отгрузки",
    "relocation": "Перемещение", "payment_terms": "Условия оплаты",
    "purchase_special": "Особенности закупки", "vat_rate": "Ставка НДС, %",
    "capital_rate": "Ставка капитала, %", "tax_rate": "Налог на прибыль, %",
    "payroll_tax_rate": "Налоги с ФОТ, %",
    "risk_expert": "Экспертиза рисков", "recommendation": "Рекомендация",
    "economist_comment": "Комментарий экономиста",
    "clarify_notes": "Что уточнить", "shipment_loss_pct": "Потери при отгрузке, %",
    "scen_price_delta": "Шаг сценария цены, руб",
    "approved_date": "Дата согласования", "payment_date": "Дата оплаты",
    "work_start_date": "Дата начала работ",
    "vat_unrecovered_pct": "Невозмещённый НДС, %",
    "active_variant": "Активный вариант", "has_luk": "Вариант «Лукойл» заведён",
    "capital_base": "База привлечённого капитала, руб",
    "payment_delay_months": "Отсрочка оплаты, мес",
    "bitrix_task_id": "Задача в Битрикс24", "source_name": "Исходное название",
    "luk_overrides": "Переопределения варианта «Лукойл»",
    "control_op_profit_bsp": "Контроль: опер. прибыль ДСП",
    "control_net_profit_bsp": "Контроль: ЧП ДСП",
    "control_op_profit_luk": "Контроль: опер. прибыль Лукойл",
    "control_net_profit_luk": "Контроль: ЧП Лукойл",
}

ITEM_LABELS = {
    "supplier": "Поставщик", "division": "Дивизион", "warehouse": "Склад",
    "nomenclature": "Наименование", "category": "Категория",
    "purchase_type": "Тип закупки", "unit": "Ед. изм.",
    "volume_t": "Объём, тн", "sale_type": "Вид реализации",
    "sale_price": "Цена реализации ДСП, руб/тн",
    "sale_price_luk": "Цена реализации Лукойл, руб/тн",
    "note": "Примечание", "own_transport_pct": "Свой транспорт, %",
    "workshop_cut_pct": "Подрезка в цехе, %", "nomen_1c": "Номенклатура 1С",
    "match_source": "Источник сопоставления",
    "match_confirmed": "Сопоставление подтверждено", "buyer": "Покупатель",
    "shipment": "Отгрузка", "price_owner": "Кто дал цену",
    "distance_km": "Плечо, км", "contamination_pct": "Засор ДСП, %",
    "contamination_pct_luk": "Засор Лукойл, %", "batch_size_t": "Кратность партии, тн",
    "balance_price": "Балансовая цена", "balance_cost": "Балансовая стоимость",
    "tech_doc": "Документы", "storage_conditions": "Условия хранения",
    "condition_note": "Состояние", "extra_works": "Доп. работы",
    "seller_code": "Код продавца", "cargo_group": "Группа учёта",
    "sale_group": "План продажи (группа)", "liquidity": "Ликвидность",
    "expert_note": "Заключение коммерсанта", "price_set_by": "Цену дал",
    "price_set_at": "Дата цены", "sale_period": "Период реализации",
    "origin_reason": "Причина появления",
    "sale_instruction": "Указание по реализации (ДСП)",
    "sale_instruction_luk": "Указание по реализации (Лукойл)",
}

COST_LABELS = {
    "section": "Раздел", "item": "Статья", "amount": "Сумма ДСП, руб",
    "amount_luk": "Сумма Лукойл, руб", "base": "База распределения",
    "comment": "Комментарий",
}

RISK_LABELS = {
    "risk_type": "Тип риска", "description": "Описание",
    "probability": "Вероятность", "impact": "Влияние",
    "mitigation": "Меры",
}

# Служебные поля: меняются при каждой записи и о сути правки не говорят.
SKIP_FIELDS = {"id", "bp_id", "version", "created_at", "updated_at",
               "nomen_1c_guid", "edge_id", "snapshot_hash"}


def _same(a, b) -> bool:
    """Равенство значений с поправкой на хранение чисел.

    В снимке одно и то же значение может лежать как 5 и как 5.0, а пустота —
    как None и как «»; без этого diff показывал бы правки, которых не было."""
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if a is None or b is None:
        return str(a or "").strip() == str(b or "").strip()
    return str(a).strip() == str(b).strip()


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return f"{int(round(value)):,}".replace(",", " ")
        return f"{value:,.2f}".replace(",", " ")
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "…"


def _field_diffs(old: dict, new: dict, labels: dict) -> list[dict]:
    fields = [k for k in (list(old) + [k for k in new if k not in old])
              if k not in SKIP_FIELDS]
    out = []
    for key in fields:
        before, after = old.get(key), new.get(key)
        if _same(before, after):
            continue
        out.append({"field": key, "label": labels.get(key, key),
                    "before": _fmt(before), "after": _fmt(after)})
    return out


def _row_title(row: dict, kind: str) -> str:
    if kind == "items":
        name = row.get("nomenclature") or "без наименования"
        code = row.get("seller_code")
        return f"{name} ({code})" if code else name
    if kind == "costs":
        return row.get("item") or "статья без названия"
    if kind == "risks":
        return row.get("risk_type") or "риск"
    return str(row.get("id"))


def _rows_diff(old_rows: list, new_rows: list, labels: dict,
               kind: str) -> dict:
    """Сравнение списков строк по id: изменённые, добавленные, удалённые."""
    old_by_id = {r.get("id"): r for r in old_rows or []}
    new_by_id = {r.get("id"): r for r in new_rows or []}
    changed, added, removed = [], [], []
    for rid, new_row in new_by_id.items():
        old_row = old_by_id.get(rid)
        if old_row is None:
            added.append({"title": _row_title(new_row, kind), "id": rid})
            continue
        diffs = _field_diffs(old_row, new_row, labels)
        if diffs:
            changed.append({"title": _row_title(new_row, kind), "id": rid,
                            "fields": diffs})
    for rid, old_row in old_by_id.items():
        if rid not in new_by_id:
            removed.append({"title": _row_title(old_row, kind), "id": rid})
    changed.sort(key=lambda r: r["title"])
    return {"changed": changed, "added": added, "removed": removed,
            "total": len(changed) + len(added) + len(removed)}


def diff(snap_old: dict, snap_new: dict) -> dict:
    """Разница двух снимков расчёта.

    Возвращает разделы «сделка / позиции / затраты / риски» и сводку по
    чистой прибыли обоих вариантов."""
    profits = {}
    for code in ("bsp", "luk"):
        key = f"net_profit_{code}"
        before = (snap_old.get("pnl") or {}).get(key)
        after = (snap_new.get("pnl") or {}).get(key)
        if before is None and after is None:
            continue
        delta = None
        if before is not None and after is not None:
            delta = after - before
        profits[code] = {"before": before, "after": after, "delta": delta}

    deal = _field_diffs(snap_old.get("bp") or {}, snap_new.get("bp") or {},
                        BP_LABELS)
    items = _rows_diff(snap_old.get("items"), snap_new.get("items"),
                       ITEM_LABELS, "items")
    costs = _rows_diff(snap_old.get("costs"), snap_new.get("costs"),
                       COST_LABELS, "costs")
    risks = _rows_diff(snap_old.get("risks"), snap_new.get("risks"),
                       RISK_LABELS, "risks")
    return {
        "profits": profits,
        "deal": deal,
        "items": items,
        "costs": costs,
        "risks": risks,
        "total": len(deal) + items["total"] + costs["total"] + risks["total"],
    }
