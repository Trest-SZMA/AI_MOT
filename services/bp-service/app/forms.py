"""Разбор значений из форм: одно место, где текст поля становится числом.

До 17.08.2026 каждый роут разбирал число сам, и опечатка проходила молча:
`abc` записывался как пусто (закупка обнулялась, ЧП завышалась на 5,7 млн),
`1e999` давал `inf` и превращал чистую прибыль в `nan`, `-5` принималось как
отрицательная цена. Excel в таком случае показывает #ЗНАЧ! и считать
отказывается — сервис, который его заменяет, обязан вести себя не хуже.

Здесь три вещи:

* `number()` — строгий разбор: число, знак, диапазон. Экспонента не
  принимается намеренно: `1e999` человек в поле цены не пишет, а `inf`
  разрушает весь расчёт;
* `RULES` — границы по именам полей, чтобы «доля 150%» и «отрицательная
  цена» отсекались одинаково во всех формах;
* `Form` — чтение отправки с накоплением ошибок: роут либо записывает всё,
  либо не пишет ничего и возвращает человеку, что именно не так. Отдельно
  различаются «поле не пришло» (не трогать прежнее значение) и «поле пришло
  пустым» (осознанная очистка) — их смешение обнуляло статьи затрат при
  неполной отправке формы.
"""
from __future__ import annotations

import math
import re

# Только обычная десятичная запись. Экспоненты и «1_000» тут не бывает, а
# `1e999` — это `inf`, после которого расчёт даёт nan во всех строках.
NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:[.]\d*)?|[.]\d+)$")

# Потолок на любое число из формы: суммы сделок — миллиарды, всё, что больше
# триллиона, это промах по клавише, а не деньги.
LIMIT = 1e12

_MISSING = object()

PCT = (0.0, 100.0)
MONEY = (0.0, LIMIT)
QTY = (0.0, 1e9)


class Invalid(ValueError):
    """Значение не прошло проверку. Текст исключения готов к показу человеку."""


# Границы и человеческое название по имени поля. Ключ — имя поля в форме без
# идентификатора строки (`sale_price_17` → `sale_price`).
RULES: dict[str, tuple[str, float | None, float | None]] = {
    # Параметры лота и сделки
    "lot_cost": ("Стоимость лота", *MONEY),
    "auction_step": ("Шаг аукциона", *MONEY),
    "capital_base": ("База капитала", *MONEY),
    "contamination_pct": ("Засор", *PCT),
    "contamination_pct_luk": ("Засор (Лукойл)", *PCT),
    "vat_rate": ("Ставка НДС", *PCT),
    "vat_unrecovered_pct": ("Невозмещённый НДС на затраты", *PCT),
    "tax_rate": ("Налог на прибыль", *PCT),
    "payroll_tax_rate": ("Налоги с ФОТ", *PCT),
    "shipment_loss_pct": ("Потери при отгрузке", *PCT),
    "capital_rate": ("Ставка капитала", 0.0, 1000.0),
    "removal_months": ("Срок вывоза", 0.0, 120.0),
    "payment_delay_months": ("Отсрочка оплаты", 0.0, 120.0),
    # Сдвиг цены в сценариях — единственная величина, которая по смыслу
    # бывает отрицательной («минус 1000 руб/тн» — рабочий сценарий книг).
    "scen_price_delta": ("Сдвиг цены в сценариях", -1e9, 1e9),
    "seller_id": ("Продавец", 0.0, 1e12),
    "buyer_id": ("Покупатель", 0.0, 1e12),
    # Позиции лота
    "volume_t": ("Объём", *QTY),
    "volume": ("Объём", *QTY),
    "sale_price": ("Цена реализации", 0.0, 1e9),
    "sale_price_luk": ("Цена реализации (Лукойл)", 0.0, 1e9),
    "price": ("Цена", 0.0, 1e9),
    "purchase_price": ("Цена закупки", 0.0, 1e9),
    "balance_cost": ("Балансовая стоимость", *MONEY),
    "batch_size_t": ("Кратность партии", *QTY),
    "own_transport_pct": ("Доля своего транспорта", *PCT),
    "workshop_cut_pct": ("Доля порезки в цехе", *PCT),
    "distance_km": ("Расстояние", 0.0, 20000.0),
    "hours": ("Время в пути", 0.0, 1000.0),
    # Затраты
    "amount": ("Сумма статьи", *MONEY),
    "amount_luk": ("Сумма статьи (Лукойл)", *MONEY),
    "per_t": ("Сумма на тонну", 0.0, 1e9),
    # План-факт и график вывоза
    "plan": ("План", *QTY),
    "fact": ("Факт", *QTY),
    "revenue": ("Выручка", *MONEY),
    "cost": ("Себестоимость", *MONEY),
    # Справочники
    "value": ("Значение норматива", 0.0, LIMIT),
    "factor": ("Коэффициент", 0.0, 100.0),
    "rate_per_trip": ("Ставка за рейс", *MONEY),
    "part_load_pct": ("Догруз", *PCT),
}

# Короткие имена полей форм, у которых имя в базе другое.
ALIASES = {
    "own": "own_transport_pct",
    "cut": "workshop_cut_pct",
    "batch": "batch_size_t",
    "contamination": "contamination_pct",
    "contamination_luk": "contamination_pct_luk",
    "lot_price_per_t": "purchase_price",
    "km": "distance_km",
}


def rule(field: str) -> tuple[str, float | None, float | None]:
    """Название и границы поля. Незнакомое поле — без границ, но с потолком."""
    field = ALIASES.get(field, field)
    return RULES.get(field, (field, None, None))


def _show(value: float) -> str:
    """Число в сообщении: 100 вместо 100.0, запятая как в форме."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:g}".replace(".", ",")


def _cut(raw: str, limit: int = 24) -> str:
    raw = raw.strip()
    return raw if len(raw) <= limit else raw[:limit] + "…"


def number(raw, field: str = "", *, label: str | None = None,
           lo: float | None = _MISSING, hi: float | None = _MISSING,
           required: bool = False) -> float | None:
    """Число из поля формы. Пусто → None, мусор → Invalid с объяснением.

    Границы берутся из RULES по имени поля; переданные lo/hi их перекрывают.
    """
    name, rule_lo, rule_hi = rule(field or "")
    label = label or name
    lo = rule_lo if lo is _MISSING else lo
    hi = rule_hi if hi is _MISSING else hi

    # Пробелы всех видов (в том числе неразрывный из Excel) убираем:
    # «19 900» человек копирует из книги вместе с ними.
    text = re.sub(r"\s+", "", "" if raw is None else str(raw)).replace(",", ".")
    if not text:
        if required:
            raise Invalid(f"{label}: значение обязательно")
        return None
    if not NUMBER_RE.match(text):
        raise Invalid(f"{label}: «{_cut(str(raw))}» — это не число. "
                      f"Введите, например, 1 234,56")
    try:
        value = float(text)
    except ValueError:                        # длиннее, чем float умеет
        raise Invalid(f"{label}: «{_cut(str(raw))}» — это не число")
    if not math.isfinite(value):              # 400 цифр подряд дают inf
        raise Invalid(f"{label}: слишком большое число")
    if abs(value) > LIMIT:
        raise Invalid(f"{label}: {_show(value)} — слишком большое число")
    if lo is not None and value < lo:
        if lo == 0:
            raise Invalid(f"{label}: {_show(value)} — "
                          f"значение не может быть отрицательным")
        raise Invalid(f"{label}: {_show(value)} — допустимо от {_show(lo)}"
                      + (f" до {_show(hi)}" if hi is not None else ""))
    if hi is not None and value > hi:
        raise Invalid(f"{label}: {_show(value)} — допустимо "
                      f"от {_show(lo or 0)} до {_show(hi)}")
    return value


class Form:
    """Отправка формы с накоплением ошибок.

    Роут читает все нужные поля, потом один раз смотрит `ok`: человек видит
    все ошибки формы сразу, а не по одной за отправку. Ни одно значение при
    этом ещё не записано — запись идёт только после проверки.
    """

    def __init__(self, data):
        self.data = data
        self.errors: list[str] = []

    # ── наличие поля ──────────────────────────────────────────────────
    def has(self, name: str) -> bool:
        """Поле реально пришло в отправке.

        Отсутствующее поле и пустое поле — разные вещи: первое значит «эта
        часть формы не отправлялась», второе — «человек стёр значение».
        Смешение этих случаев обнуляло все 13 статей затрат при неполной
        отправке.
        """
        return name in self.data

    def raw(self, name: str, default: str = "") -> str:
        value = self.data.get(name)
        return default if value is None else str(value)

    # ── значения ──────────────────────────────────────────────────────
    def num(self, name: str, keep=_MISSING, *, field: str | None = None,
            label: str | None = None, lo: float | None = _MISSING,
            hi: float | None = _MISSING, required: bool = False):
        """Число поля `name`.

        Поля нет в отправке → `keep` (прежнее значение остаётся нетронутым).
        Ошибка → запоминается в `errors`, возвращается `keep`.
        """
        if name not in self.data:
            return None if keep is _MISSING else keep
        try:
            return number(self.data.get(name), field or _strip_id(name),
                          label=label, lo=lo, hi=hi, required=required)
        except Invalid as exc:
            self.errors.append(str(exc))
            return None if keep is _MISSING else keep

    def text(self, name: str, keep=_MISSING, *, limit: int = 2000):
        """Строка поля: пусто → None (очистка), поля нет → `keep`."""
        if name not in self.data:
            return None if keep is _MISSING else keep
        value = str(self.data.get(name) or "").strip()
        return value[:limit] or None

    def fail(self, text: str) -> None:
        """Добавить свою ошибку (проверка, которую не выразить границами)."""
        self.errors.append(text)

    # ── итог ──────────────────────────────────────────────────────────
    @property
    def ok(self) -> bool:
        return not self.errors

    def message(self, limit: int = 3) -> str:
        """Что показать человеку. Первые ошибки целиком, остальные счётом:
        сообщение уезжает в адресную строку, и длинный список туда не влезет."""
        if not self.errors:
            return ""
        shown = "; ".join(self.errors[:limit])
        if len(self.errors) > limit:
            shown += f"; и ещё ошибок: {len(self.errors) - limit}"
        return f"Не сохранено. {shown}."


_ID_TAIL = re.compile(r"_\d+$")


def _strip_id(name: str) -> str:
    """`sale_price_17` → `sale_price`: границы задаются на поле, не на строку."""
    return _ID_TAIL.sub("", name)
