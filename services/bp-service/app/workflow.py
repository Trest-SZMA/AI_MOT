"""Статусы, роли и права доступа к разделам БП."""
from __future__ import annotations

ROLES = {
    "manager": "Менеджер",
    "logist": "Логист",
    "economist": "Экономист",
    "director": "Руководитель",
    "admin": "Системный администратор",
}

# Разделы карточки БП и роли, которым разрешено их редактировать.
# Руководитель имеет доступ ко всем блокам БП; системный администратор — ко всему.
SECTION_ROLES = {
    "header": {"manager", "director", "admin"},     # шапка: дивизион, сценарий, запрос
    "parties": {"manager", "director", "admin"},    # продавец / покупатель
    "lot": {"manager", "economist", "director", "admin"},  # параметры лота и ставки
    "items": {"manager", "economist", "logist", "director", "admin"},
                                                    # позиции (цены — экономист,
                                                    # доли транспорта/подрезки — логист)
    "costs": {"economist", "logist", "director", "admin"},  # затраты по статьям P&L
    "route": {"logist", "manager", "director", "admin"},    # граф маршрута
    "risks": {"economist", "director", "admin"},
    "summary": {"economist", "director", "admin"},
}

# Переходы статусов: (из, действие) -> (в, роли).
TRANSITIONS = {
    ("Черновик", "to_review"): ("На проверке", {"manager", "admin"}),
    ("Доработка", "to_review"): ("На проверке", {"manager", "admin"}),
    ("На проверке", "to_approval"): ("На согласовании", {"economist", "admin"}),
    ("На проверке", "to_rework"): ("Доработка", {"economist", "admin"}),
    ("На согласовании", "approve"): ("Согласован", {"director", "admin"}),
    ("На согласовании", "reject"): ("Отклонён", {"director", "admin"}),
    ("На согласовании", "to_rework"): ("Доработка", {"director", "admin"}),
}

ACTION_LABELS = {
    "to_review": "Отправить на проверку",
    "to_approval": "На согласование",
    "to_rework": "Вернуть на доработку",
    "approve": "Согласовать",
    "reject": "Отклонить",
}

# Статусы черновой работы: в них БП идёт по маршруту согласования. Расчёт
# редактируется в ЛЮБОМ статусе (см. can_edit_section) — экономист правит
# вариант «Лукойл» уже после того, как «ДСП» закрыли и согласовали (БП 1935).
# Набор оставлен как признак «БП ещё в работе» для подписей в интерфейсе.
DRAFT_STATUSES = {"Черновик", "На проверке", "Доработка"}

# Виды узлов графа маршрута.
NODE_KINDS = {
    "seller": "Продавец",
    "base": "База",
    "workshop": "Цех",
    "production": "Производство",
    "custody": "Ответхранение",
    "buyer": "Покупатель",
}


def can_edit_section(section: str, role: str, status: str) -> bool:
    """Право роли править раздел. Статус НЕ блокирует: оба варианта расчёта
    («ДСП» и «Лукойл») корректируются в любой момент, в том числе после
    согласования — по БП 1935 «ДСП» закрыт, а «Лукойл» продолжают считать.
    История правок сохраняется версиями расчёта и журналом (audit_log)."""
    return role in SECTION_ROLES.get(section, set())


def is_final_status(status: str) -> bool:
    """Итоговый статус: правки в нём допустимы, но помечаются в карточке
    предупреждением — БП уже прошёл согласование."""
    return status not in DRAFT_STATUSES


def available_actions(status: str, role: str) -> list[tuple[str, str]]:
    return [(action, ACTION_LABELS[action])
            for (from_st, action), (_, roles) in TRANSITIONS.items()
            if from_st == status and role in roles]
