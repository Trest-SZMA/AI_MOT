"""Печатная форма БП в PDF — лист «БП_дата» книги экономиста.

Повторяет утверждённый вид (образец — «БП_06.08.26 лук» книги 1935):
шапка сделки, таблица «Показатели / Тоннаж / Суммарно / на 1 тонну»
и блок комментариев. Все цифры берутся из calc.pnl — того же расчёта,
что показывает карточка, поэтому форма всегда сходится с сервисом.

Шрифты DejaVu лежат рядом в fonts/ — на сервере системных шрифтов с
кириллицей может не быть, а без них PDF молча выйдет с «□□□».
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from fpdf import FPDF

from .. import calc

FONTS = Path(__file__).parent / "fonts"

NAVY = (31, 56, 100)        # шапка таблицы
YELLOW = (255, 242, 204)    # итоговые строки разделов
ORANGE = (250, 192, 144)    # чистая прибыль
RED = (192, 0, 0)           # акценты шапки и НДС
GREY = (176, 176, 176)      # рамки
BLACK = (0, 0, 0)

# Ширины колонок, мм (A4 портрет, поля по 10)
W_LABEL, W_TON, W_SUM, W_PER_T = 92, 26, 40, 32
ROW_H = 5.2

# Строки «Выручка без НДС» по типам продажи: подпись формы — тип сервиса.
TYPE_LABELS = [("черный лом", "лом"), ("цветной лом", "цветмет"),
               ("труба", "труба"), ("ДХНО", "ДХНО"), ("кабель", "кабель")]

# Порядок статей в форме — как в книге; имена совпадают со статьями bp_costs.
VARIABLE_ITEMS = ["Погрузочно-разгрузочные расходы",
                  "Заправка газом и кислородом",
                  "Транспортные расходы — собственная техника",
                  "Транспортные расходы на отгрузку",
                  "Транспортные расходы на перемещение"]
PERSONNEL_ITEMS = ["Зарплата", "Аренда квартир (проживание)",
                   "Командировочные расходы", "Прочие расходы на персонал"]
FIXED_ITEMS = ["Оборудование и инструменты",
               "Аренда баз, коммунальные расходы, охрана",
               "Прочие производственные расходы",
               "Амортизационные отчисления (транспорт, оборудование)"]


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _money(v) -> str:
    """1 234 567 — суммы без копеек, минус обычный."""
    return f"{v:,.0f}".replace(",", " ")


def _tons(v) -> str:
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def _contamination(bp, items) -> str:
    """Засор чёрного лома: одно число или диапазон «5-10%», как в книге."""
    vals = sorted({_f(it["contamination_pct"]) for it in items
                   if it["contamination_pct"] is not None} or
                  {_f(bp["contamination_pct"])})
    vals = [v for v in vals if v] or [0.0]
    def pct(v):
        return f"{v:g}"
    if len(vals) == 1:
        return f"{pct(vals[0])}%"
    return f"{pct(vals[0])}-{pct(vals[-1])}%"


def _cost_amount(costs, item: str) -> float:
    return sum(_f(c["amount"]) for c in costs if c["item"] == item)


class _Form(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_margins(10, 10, 10)
        self.set_auto_page_break(True, margin=10)
        for style, fname in [("", "DejaVuSans.ttf"), ("B", "DejaVuSans-Bold.ttf"),
                             ("I", "DejaVuSans-Oblique.ttf"),
                             ("BI", "DejaVuSans-BoldOblique.ttf")]:
            self.add_font("DejaVu", style, FONTS / fname)
        self.set_font("DejaVu", "", 8)
        self.set_draw_color(*GREY)


def build_bp_pdf(bp, items: list, costs: list, pnl: dict, path: Path,
                 variant_label: str | None = None,
                 version_label: str | None = None) -> Path:
    pdf = _Form()
    pdf.add_page()
    version_label = (version_label
                     or (bp["scenario"] or "").strip()
                     or f"версия {bp['version']} {date.today():%d.%m.%y}")

    # ─────────────────────────────── Шапка ───────────────────────────
    left_w, right_w = 120, 70

    def header_line(label: str, value: str = "", *, label_style="B",
                    label_color=RED, right: str = "", right_color=BLACK,
                    right_style="", fill=False, indent=0.0):
        pdf.set_font("DejaVu", label_style, 8.5)
        pdf.set_text_color(*label_color)
        if indent:
            pdf.cell(indent, 4.6, "")
        pdf.cell(left_w - indent - 40, 4.6, label)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.set_text_color(*BLACK)
        if fill:
            pdf.set_fill_color(255, 255, 153)
        pdf.cell(40, 4.6, value, align="R", fill=fill)
        pdf.set_font("DejaVu", right_style, 8.5)
        pdf.set_text_color(*right_color)
        pdf.cell(right_w, 4.6, right, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*BLACK)

    purchase_by_type: dict[str, float] = {}
    for r in pnl["rows"]:
        purchase_by_type[r["purchase_type"]] = (
            purchase_by_type.get(r["purchase_type"], 0.0) + r["volume_t"])

    ref = (bp["tender_ref"] or "").strip()
    header_line(bp["seller_name"] or "Продавец не указан",
                right=(f"На запрос {ref}" if ref else ""),
                right_color=RED, right_style="B")
    header_line(f"Дивизион: {bp['division'] or '—'}",
                _tons(pnl["purchase_volume"]) + " тн")
    lom_sale = pnl["by_type"].get("лом", {}).get("volume", 0.0)
    header_line("Лом", _tons(lom_sale) + " тн", label_style="B",
                label_color=BLACK)
    header_line("черный лом", _tons(purchase_by_type.get("лом", 0.0)) + " тн",
                label_style="", indent=6)
    header_line("цветной лом", _tons(purchase_by_type.get("цветмет", 0.0)) + " тн",
                label_style="", indent=6)
    header_line("Засор черный лом", _contamination(bp, items), fill=True,
                label_style="", indent=6)
    header_line("Засор цветной лом", "0%", label_style="", indent=6)
    src = (bp["source_name"] or "").strip()
    months = _f(bp["removal_months"]) or pnl["removal_months"]
    header_line(f"Срок вывоза: {months:g} мес", "")
    header_line(f"Месяц начала вывоза: {bp['start_month'] or '—'}", "",
                right=src)
    header_line(f"Вид отгрузки: {bp['shipment_type'] or '—'}", "",
                right=bp["bp_number"]
                + (f" ({variant_label})" if variant_label else ""))
    header_line(f"Перемещение: {bp['relocation'] or 'нет'}", "")
    pdf.ln(1.5)

    # ─────────────────────────────── Таблица ─────────────────────────
    def row(label: str, ton="", total="", per_t="", *, bold=False,
            italic=False, fill=None, color=BLACK, indent=0.0, size=8.0):
        style = ("B" if bold else "") + ("I" if italic else "")
        pdf.set_font("DejaVu", style, size)
        pdf.set_text_color(*color)
        if fill:
            pdf.set_fill_color(*fill)
        f = fill is not None
        pdf.cell(W_LABEL, ROW_H, " " * int(indent) + label, border=1, fill=f)
        pdf.cell(W_TON, ROW_H, ton, border=1, align="R", fill=f)
        pdf.cell(W_SUM, ROW_H, total, border=1, align="R", fill=f)
        pdf.cell(W_PER_T, ROW_H, per_t, border=1, align="R", fill=f,
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*BLACK)

    # Шапка таблицы: названия колонок + строка версии расчёта.
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("DejaVu", "B", 8)
    head_h = 8
    x0, y0 = pdf.get_x(), pdf.get_y()
    for w, title in [(W_LABEL, "Показатели"), (W_TON, "Тоннаж с учетом засора"),
                     (W_SUM, "Суммарно"), (W_PER_T, "на 1 тонну")]:
        pdf.set_xy(x0, y0)
        pdf.multi_cell(w, head_h / 2 if title.count(" ") > 2 else head_h,
                       title, border=1, fill=True, align="C",
                       max_line_height=head_h / 2)
        x0 += w
    # multi_cell с одной строкой рисует ячейку высотой h — выровнять рамки.
    pdf.set_xy(pdf.l_margin, y0)
    for w in (W_LABEL, W_TON, W_SUM, W_PER_T):
        pdf.cell(w, head_h, "", border=1)
    pdf.set_xy(pdf.l_margin, y0 + head_h)
    pdf.set_text_color(*BLACK)
    # Версия расчёта — одной ячейкой на обе числовые колонки: длинный
    # сценарий («ДСП по указаниям Сергея…») в двух копиях накладывался.
    pdf.set_font("DejaVu", "I", 7.5)
    pdf.cell(W_LABEL + W_TON, ROW_H, "", border=1)
    pdf.cell(W_SUM + W_PER_T, ROW_H, version_label, border=1, align="R",
             new_x="LMARGIN", new_y="NEXT")

    sale_volume = pnl["sale_volume"]

    def per_t(v):
        return _money(v / sale_volume) if sale_volume else "0"

    row("Объем металлолома", "", _tons(sale_volume), "ХХХ", bold=True)
    row("Выручка без НДС", _tons(sale_volume), _money(pnl["revenue"]),
        _money(pnl["revenue_per_t"]), bold=True)
    for label, t in TYPE_LABELS:
        bt = pnl["by_type"].get(t) or {}
        if not bt.get("volume") and not bt.get("revenue"):
            continue
        vol = bt.get("volume", 0.0)
        rev = bt.get("revenue", 0.0)
        row(label, _tons(vol), _money(rev),
            _money(rev / vol if vol else 0.0), indent=4)
    row("Себестоимость без НДС", "", _money(pnl["lot_cost"]),
        per_t(pnl["lot_cost"]), bold=True)
    row("Маржинальная прибыль", "", _money(pnl["gross_purchase"]),
        per_t(pnl["gross_purchase"]), bold=True, fill=YELLOW)
    row("% наценки", "", f"{pnl['markup_pct']:.0f}%", f"{pnl['markup_pct']:.0f}%",
        italic=True, fill=YELLOW)
    row("Прямые (производственные) затраты, в т.ч.", "",
        _money(pnl["direct_costs"]), per_t(pnl["direct_costs"]),
        bold=True, fill=YELLOW)
    row("Переменные затраты", "", _money(pnl["variable_costs"]),
        per_t(pnl["variable_costs"]), bold=True)
    for item in VARIABLE_ITEMS:
        amount = _cost_amount(costs, item)
        row(item, "", _money(amount), per_t(amount), indent=2)
    row("Постоянные затраты", "", _money(pnl["fixed_costs_wo_vat"]),
        per_t(pnl["fixed_costs_wo_vat"]), bold=True)
    row("Расходы на персонал", "", _money(pnl["personnel_total"]),
        per_t(pnl["personnel_total"]), indent=2)
    for item in PERSONNEL_ITEMS:
        amount = _cost_amount(costs, item)
        row(item, "", _money(amount), per_t(amount), indent=6, italic=True)
    row("Налоги с ФОТ", "", _money(pnl["payroll_tax"]),
        per_t(pnl["payroll_tax"]), indent=6, italic=True)
    for item in FIXED_ITEMS:
        amount = _cost_amount(costs, item)
        row(item, "", _money(amount), per_t(amount), indent=2)
    row(f"НДС, не возмещенный {pnl['vat_unrecovered_pct']:g}%", "",
        _money(pnl["vat_unrecovered"]), per_t(pnl["vat_unrecovered"]),
        bold=True, color=RED, fill=YELLOW)
    row("Операционная прибыль", "", _money(pnl["operating_profit"]),
        per_t(pnl["operating_profit"]), bold=True, fill=YELLOW)
    if pnl["admin_costs"]:
        row("Административные расходы", "", _money(pnl["admin_costs"]),
            per_t(pnl["admin_costs"]))
    if pnl["other_costs"]:
        row("Прочие расходы", "", _money(pnl["other_costs"]),
            per_t(pnl["other_costs"]))
    row("Стоимость привлеченного капитала", "", _money(pnl["capital_cost"]),
        per_t(pnl["capital_cost"]))
    row("Прибыль до налогообложения", "", _money(pnl["profit_before_tax"]),
        per_t(pnl["profit_before_tax"]), bold=True, fill=YELLOW)
    row("Налоги", "", _money(pnl["income_tax"]), per_t(pnl["income_tax"]))
    row("Чистая прибыль", "", _money(pnl["net_profit"]),
        per_t(pnl["net_profit"]), bold=True, fill=ORANGE)
    ros = f"{pnl['ros_pct']:.2f}%".replace(".", ",")
    row("Рентабельность продаж", "", ros, ros, italic=True, fill=ORANGE)

    # ─────────────────────────────── Комментарии ─────────────────────
    auto = [f"Способ отгрузки — {bp['shipment_type'] or '—'}",
            f"Срок вывоза {months:g} мес",
            f"ЧП составит {_money(pnl['net_profit'])} руб."]
    text = "Комментарии:\n" + "\n".join(auto)
    econ = (bp["economist_comment"] or "").strip()
    if econ:
        text += "\n" + econ
    pdf.set_font("DejaVu", "", 8)
    pdf.multi_cell(W_LABEL + W_TON + W_SUM + W_PER_T, 4.4, text, border=1)

    pdf.output(str(path))
    return path
