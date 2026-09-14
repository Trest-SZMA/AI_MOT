"""Нормативный расчёт затрат по образцу расчёта экономистов (Данные_Тест, AD-CQ).

Статьи затрат считаются от норм (цены ГСМ, оклады, нормы выработки,
вместимость машин/вагонов, мощности баз), а не вводятся суммами.
Нормы лежат в справочнике cost_norms: общие + переопределения по базам
(база позиции определяется по полю «Поставщик»: Усинск, Ухта...).

Результат распределяется по статьям P&L; экономист может поправить суммы
вручную после расчёта.
"""
from __future__ import annotations

import math

import sqlite3
from collections import defaultdict

# (key, value, label, unit) — общие нормативы по умолчанию (из Данные_Тест).
DEFAULT_NORMS = [
    ("fuel_price", 76.52, "Цена ДТ, руб/л (с НДС)", "руб/л"),
    ("fuel_rate_l_km", 0.55, "Расход ГСМ на перемещение, л/км", "л/км"),
    ("truck_capacity_t", 16, "Загрузка машины, тн/рейс", "тн"),
    ("trips_per_shift", 3, "Рейсов на смену (перемещение)", "рейс"),
    ("prr_fuel_l_trip", 12, "ГСМ на ПРР, л/рейс (2×6)", "л"),
    ("driver_salary_month", 152500, "ФОТ водителя, руб/мес (средний)", "руб"),
    ("daily_allowance", 700, "Суточные, руб/день", "руб"),
    ("truck_depreciation_month", 155000, "Амортизация машины, руб/мес", "руб"),
    ("hired_transport_rate_t", 2255, "Наёмный транспорт (перемещение), руб/тн", "руб/тн"),
    ("shifts_per_month", 25, "Смен в месяце", "смен"),
    ("wagon_capacity_t", 62, "Загрузка вагона, тн", "тн"),
    ("net_m2_per_wagon", 38, "Сетка на вагон, м2", "м2"),
    ("net_price_m2", 220.83, "Цена сетки, руб/м2", "руб/м2"),
    ("wire_kg_per_wagon", 20, "Проволока на вагон, кг", "кг"),
    ("wire_price_kg", 85, "Цена проволоки, руб/кг", "руб/кг"),
    ("rail_services_per_wagon", 22450, "Ж/д услуги за вагон (подача-уборка+укрытие)", "руб"),
    ("loader_fuel_l_h", 11, "ГСМ перегружателя, л/час", "л/ч"),
    ("loader_h_per_truck", 1.5, "Часы перегружателя на машину (16 тн)", "ч"),
    ("loader_h_per_wagon", 4, "Часы перегружателя на вагон (62 тн)", "ч"),
    ("loader_salary_month", 140000, "ФОТ водителя перегружателя, руб/мес", "руб"),
    ("loader_shift_h", 11, "Смена перегружателя, часов", "ч"),
    ("manual_cut_share_pct", 35, "Доля ручной резки, % (остальное пресс)", "%"),
    ("oxygen_rate_per_t", 9, "Кислород на ручную резку, ед/тн (1.5×6)", "ед/тн"),
    ("oxygen_price", 107, "Цена кислорода, руб/ед", "руб"),
    ("petrol_l_per_t", 3, "Бензин на резку, л/тн", "л/тн"),
    ("cutter_salary_month", 90000, "ФОТ резчика, руб/мес", "руб"),
    ("cut_rate_t_shift", 4, "Норма ручной резки, тн/смену", "тн"),
    ("press_cost_per_t", 233, "ГСМ/электро пресса, руб/тн", "руб/тн"),
    ("press_salary_month", 110000, "ФОТ оператора пресса, руб/мес", "руб"),
    ("press_rate_t_shift", 30, "Норма пресса, тн/смену", "тн"),
    ("press_depreciation_month", 253503.32, "Амортизация пресса, руб/мес", "руб"),
    ("base_overhead_per_t", 756, "Распределяемые расходы базы, руб/тн", "руб/тн"),
]

# Переопределения по базам (из Данные_Тест: Усинск vs Ухта).
BASE_NORMS = {
    "Усинск": [
        ("oxygen_price", 107, "Цена кислорода, руб/ед", "руб"),
        ("base_overhead_per_t", 756, "Распределяемые: (оклады×1.49+аренда)/1500 тн", "руб/тн"),
        ("rail_services_per_wagon", 22450, "Ж/д: (6500+19000)/1.2+1200", "руб"),
    ],
    "Ухта": [
        ("oxygen_price", 75.8, "Цена кислорода, руб/ед", "руб"),
        ("base_overhead_per_t", 1962, "Распределяемые: (оклады×1.49+аренда)/500 тн", "руб/тн"),
        ("rail_services_per_wagon", 19867, "Ж/д: 22400/1.2+1200", "руб"),
        ("net_price_m2", 160.42, "Цена сетки, руб/м2", "руб/м2"),
        ("wire_price_kg", 71.5, "Цена проволоки, руб/кг", "руб/кг"),
        ("press_cost_per_t", 328, "Электроэнергия пресса, руб/тн", "руб/тн"),
        ("press_salary_month", 80000, "ФОТ оператора пресса, руб/мес", "руб"),
        ("press_depreciation_month", 134715, "Амортизация пресса, руб/мес", "руб"),
        ("press_rate_t_shift", 15, "Норма пресса, тн/смену", "тн"),
    ],
}


# Коэффициенты расхода по номенклатуре (со слов экономиста 10.08.2026):
# штангу и НКТ режут дольше — газа уходит в 1,5–2 раза больше нормы.
# cut_factor умножает расход кислорода/бензина и смены резчиков.
DEFAULT_NOMEN_FACTORS = [
    ("штанг", "cut_factor", 1.8, "резка штанги: газа в 1,5–2 раза больше нормы"),
    ("нкт", "cut_factor", 1.4, "резка НКТ тяжелее обычного лома"),
    ("труба", "cut_factor", 1.2, "трубу режут дольше габаритного лома"),
]

# Сезонный доступ: зимники открыты только зимой (декабрь-апрель).
DEFAULT_SEASONAL = [
    ("зимник", "12,1,2,3,4", "зимник: техника проходит только по морозу"),
]

# Тарифы погрузки по местам вывоза (образец: у части поставщиков свой кран).
DEFAULT_LOADING_TARIFFS = [
    ("Советский", 1, None, 50, None, "поставщик грузит своим краном"),
    ("Когалым", 1, None, 50, None, "поставщик грузит своим краном"),
]


def seed_norms(conn: sqlite3.Connection) -> None:
    for key, value, label, unit in DEFAULT_NORMS:
        # UNIQUE(base, key) не ловит дубли при base IS NULL — проверяем явно.
        conn.execute(
            "INSERT INTO cost_norms (base, key, value, label, unit) "
            "SELECT NULL, ?, ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM cost_norms WHERE base IS NULL AND key = ?)",
            (key, value, label, unit, key))
    for base, norms in BASE_NORMS.items():
        for key, value, label, unit in norms:
            conn.execute(
                "INSERT INTO cost_norms (base, key, value, label, unit) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(base, key) DO NOTHING", (base, key, value, label, unit))
    for pattern, key, factor, comment in DEFAULT_NOMEN_FACTORS:
        conn.execute(
            "INSERT INTO norm_nomen_factors (pattern, key, factor, comment) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(pattern, key) DO NOTHING",
            (pattern, key, factor, comment))
    for pattern, months, comment in DEFAULT_SEASONAL:
        conn.execute(
            "INSERT INTO seasonal_access (pattern, months, comment) "
            "VALUES (?, ?, ?) ON CONFLICT(pattern) DO NOTHING",
            (pattern, months, comment))
    for place, own, rate, part, cargo, comment in DEFAULT_LOADING_TARIFFS:
        conn.execute(
            "INSERT INTO loading_tariffs (place, own_crane, rate_per_trip, "
            "part_load_pct, cargo_type, comment) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(place) DO NOTHING",
            (place, own, rate, part, cargo, comment))


def _load_norms(conn: sqlite3.Connection) -> tuple[dict, dict]:
    common, by_base = {}, defaultdict(dict)
    for r in conn.execute("SELECT * FROM cost_norms").fetchall():
        if r["base"]:
            by_base[r["base"]][r["key"]] = r["value"]
        else:
            common[r["key"]] = r["value"]
    return common, dict(by_base)


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ── Единая модель расчёта: «статья = драйвер × норматив» ────────────
# Каждый компонент — строка конфигурации: формула (выражение над драйверами
# и нормативами) + перечень параметров, которые видит пользователь.
# Новая статья добавляется строкой здесь, без изменения вычислителя.
#
# Драйверы (считаются из позиций БП): v — объём базы, тн; v_own / v_hired —
# доли собственного/наёмного транспорта; dist — ср. км перемещения;
# trips — рейсы; shifts — смены водителей; wagons — вагоны; loader_h — часы
# перегружателя; w_base — объём переработки на базе; w_manual / w_mech —
# ручная / механизированная часть; cut_shifts — смены резчиков.
COST_MODEL = [
    {"section": "Переменные", "article": "Транспортные расходы на перемещение",
     "label": "ГСМ собственного транспорта", "stage": "Вывоз с цеха на базу",
     "formula": "trips * dist * 2 * fuel_rate_l_km * fuel_price / 1.2",
     "params": ["fuel_rate_l_km", "fuel_price"],
     "hint": "рейсы × км × 2 × расход × цена ДТ (без НДС)"},
    {"section": "Переменные", "article": "Транспортные расходы на перемещение",
     "label": "Наёмный транспорт", "stage": "Вывоз с цеха на базу",
     "formula": "v_hired * hired_transport_rate_t",
     "params": ["hired_transport_rate_t"],
     "hint": "наёмная доля объёма × тариф"},
    {"section": "Переменные", "article": "Транспортные расходы — собственная техника",
     "label": "ГСМ на ПРР", "stage": "Вывоз с цеха на базу",
     "formula": "trips * prr_fuel_l_trip * fuel_price / 1.2",
     "params": ["prr_fuel_l_trip"], "hint": "рейсы × л/рейс × цена ДТ"},
    {"section": "Переменные", "article": "Погрузочно-разгрузочные расходы",
     "label": "Наём крана на погрузку", "stage": "Вывоз с цеха на базу",
     "formula": "crane_trips * crane_rate_avg",
     "params": ["truck_capacity_t"],
     "hint": "рейсы × тариф места погрузки (у поставщиков со своим краном — 0); "
             "тарифы — в справочнике «Тарифы погрузки»"},
    {"section": "Переменные", "article": "Транспортные расходы — собственная техника",
     "label": "ГСМ перегружателя", "stage": "Переработка на базе",
     "formula": "loader_h * loader_fuel_l_h * fuel_price / 1.2",
     "params": ["loader_fuel_l_h"], "hint": "часы × л/час × цена ДТ"},
    {"section": "Переменные", "article": "Транспортные расходы — собственная техника",
     "label": "Пресс (ГСМ / электроэнергия)", "stage": "Переработка на базе",
     "formula": "w_mech * press_cost_per_t",
     "params": ["press_cost_per_t"], "hint": "механизированный объём × руб/тн"},
    {"section": "Переменные", "article": "Заправка газом и кислородом",
     "label": "Кислород (ручная резка)", "stage": "Переработка на базе",
     "formula": "w_manual * oxygen_rate_per_t * oxygen_price * cut_factor",
     "params": ["oxygen_rate_per_t", "oxygen_price", "manual_cut_share_pct"],
     "hint": "ручной объём × расход × цена × коэффициент номенклатуры "
             "(штанга/НКТ режутся дольше)"},
    {"section": "Переменные", "article": "Заправка газом и кислородом",
     "label": "Бензин на резку", "stage": "Переработка на базе",
     "formula": "w_manual * petrol_l_per_t * fuel_price / 1.2 * cut_factor",
     "params": ["petrol_l_per_t"],
     "hint": "ручной объём × л/тн × цена × коэффициент номенклатуры"},
    {"section": "Переменные", "article": "Транспортные расходы на отгрузку",
     "label": "Сетка и проволока для вагонов", "stage": "Отгрузка с базы",
     "formula": "wagons * (net_m2_per_wagon * net_price_m2 "
                "+ wire_kg_per_wagon * wire_price_kg)",
     "params": ["net_m2_per_wagon", "net_price_m2", "wire_kg_per_wagon",
                "wire_price_kg"],
     "hint": "вагоны × (м² × цена + кг × цена)"},
    {"section": "Переменные", "article": "Транспортные расходы на отгрузку",
     "label": "Ж/д услуги", "stage": "Отгрузка с базы",
     "formula": "wagons * rail_services_per_wagon",
     "params": ["rail_services_per_wagon", "wagon_capacity_t"],
     "hint": "вагоны × ставка за вагон"},
    {"section": "Персонал", "article": "Зарплата", "label": "Водители", "stage": "Вывоз с цеха на базу",
     "formula": "driver_salary_month / shifts_per_month * shifts",
     "params": ["driver_salary_month", "shifts_per_month", "truck_capacity_t",
                "trips_per_shift"],
     "hint": "оклад / смен в мес × отработанные смены"},
    {"section": "Персонал", "article": "Зарплата", "label": "Перегружатель", "stage": "Переработка на базе",
     "formula": "loader_salary_month / shifts_per_month / loader_shift_h * loader_h",
     "params": ["loader_salary_month", "loader_shift_h"],
     "hint": "оклад / смен / часов в смене × часы"},
    {"section": "Персонал", "article": "Зарплата", "label": "Резчики", "stage": "Переработка на базе",
     "formula": "cutter_salary_month / shifts_per_month * cut_shifts",
     "params": ["cutter_salary_month", "cut_rate_t_shift"],
     "hint": "оклад / смен × смены резки"},
    {"section": "Персонал", "article": "Зарплата", "label": "Оператор пресса", "stage": "Переработка на базе",
     "formula": "press_salary_month / shifts_per_month / press_rate_t_shift * w_mech",
     "params": ["press_salary_month", "press_rate_t_shift"],
     "hint": "оклад / смен / норма тн-смену × объём"},
    {"section": "Персонал", "article": "Командировочные расходы", "label": "Суточные", "stage": "Вывоз с цеха на базу",
     "formula": "daily_allowance * (2 * shifts + cut_shifts)",
     "params": ["daily_allowance"], "hint": "ставка × человеко-дни"},
    {"section": "Постоянные",
     "article": "Амортизационные отчисления (транспорт, оборудование)",
     "label": "Машины (КМУ, ломовозы)", "stage": "Вывоз с цеха на базу",
     "formula": "truck_depreciation_month / shifts_per_month * shifts",
     "params": ["truck_depreciation_month"], "hint": "руб/мес / смен × смены"},
    {"section": "Постоянные",
     "article": "Амортизационные отчисления (транспорт, оборудование)",
     "label": "Пресс", "stage": "Переработка на базе",
     "formula": "press_depreciation_month / shifts_per_month / press_rate_t_shift "
                "* w_mech",
     "params": ["press_depreciation_month"], "hint": "руб/мес / смен / норма × объём"},
    {"section": "Постоянные", "article": "Прочие производственные расходы",
     "label": "Распределяемые расходы баз", "stage": "Переработка на базе",
     "formula": "v * base_overhead_per_t",
     "params": ["base_overhead_per_t"], "hint": "объём × ставка руб/тн"},
]

# Статьи, полностью покрытые моделью (итог не вводится руками).
MODEL_ARTICLES = {c["article"] for c in COST_MODEL}

# Этапы сделки в порядке движения металла (требование экономиста: у цеха и
# базы разные штат, управление и нормативы, поэтому затраты должны быть
# видны раздельно, а итог — «цех плюс база»). Для каждого этапа указан
# драйвер, на который делится сумма, чтобы получить руб/тн этапа.
STAGES = [
    {"name": "Вывоз с цеха на базу", "driver": "v_move",
     "unit": "руб/тн вывезенного",
     "hint": "Затраты подразделения до базы: транспорт (свой и наёмный), "
             "погрузка, ФОТ водителей, суточные, амортизация машин."},
    {"name": "Переработка на базе", "driver": "w_base",
     "unit": "руб/тн переработки",
     "hint": "Затраты базы: резка (кислород, бензин, резчики), пресс, "
             "перегружатель, амортизация оборудования, распределяемые базы."},
    {"name": "Отгрузка с базы", "driver": "v",
     "unit": "руб/тн отгрузки",
     "hint": "Подготовка и отправка покупателю: сетка и проволока на вагоны, "
             "подача-уборка вагонов и укрытие."},
]


def load_nomen_factors(conn: sqlite3.Connection) -> list[dict]:
    """Коэффициенты нормативов по маске номенклатуры (штанга, НКТ)."""
    try:
        return [dict(r) for r in conn.execute(
            "SELECT pattern, key, factor FROM norm_nomen_factors").fetchall()]
    except sqlite3.Error:
        return []


def nomen_factor(name: str, key: str, factors: list[dict]) -> float:
    """Множитель норматива для номенклатуры: наибольший из подошедших масок
    (штанга режется дольше НКТ — берём худший случай)."""
    low = (name or "").lower().replace("ё", "е")
    hits = [_f(f["factor"]) for f in factors
            if f["key"] == key and f["pattern"].lower() in low]
    return max(hits) if hits else 1.0


def load_loading_tariffs(conn: sqlite3.Connection) -> list[dict]:
    """Тарифы погрузки по местам вывоза."""
    try:
        return [dict(r) for r in conn.execute(
            "SELECT place, own_crane, rate_per_trip, part_load_pct "
            "FROM loading_tariffs").fetchall()]
    except sqlite3.Error:
        return []


def _tariff_for(place_text: str, tariffs: list[dict]) -> dict | None:
    low = (place_text or "").lower().replace("ё", "е")
    for t in tariffs:
        if t["place"] and t["place"].lower() in low:
            return t
    return None


def point_roles(conn: sqlite3.Connection | None) -> dict[str, dict]:
    """Роли мест из реестра пунктов отгрузки: name_norm → {kind, base_name}.

    Роль задаёт, какие этапы затрат несёт место: база — переработку и
    отгрузку, цех — только вывоз на свою базу. Места без роли модель
    считает по-старому (все этапы на месте)."""
    if conn is None:
        return {}
    from .logistics import norm_name
    try:
        return {r["name_norm"]: {"kind": r["kind"],
                                 "base_name": (r["base_name"] or "").strip()}
                for r in conn.execute(
                    "SELECT name_norm, kind, base_name FROM shipping_points "
                    "WHERE kind IS NOT NULL")}
    except sqlite3.Error:
        return {}


def _base_groups(items: list, factors: list[dict] | None = None,
                 tariffs: list[dict] | None = None,
                 roles: dict[str, dict] | None = None) -> dict[str, dict]:
    """Позиции БП, сгруппированные по базам (Поставщик, при его отсутствии —
    подразделение/место хранения), со средневзвешенными долями
    транспорта/подрезки, км, коэффициентом резки по номенклатуре и
    тарифом погрузки места.

    roles (из point_roles) меняют группировку: позиции цеха попадают в
    группу его БАЗЫ, и только их объём считается вывозным (move_volume);
    позиции самой базы вывоза не требуют — металл уже на месте
    переработки. Без ролей всё как раньше: группа = место, весь объём
    вывозной."""
    from .calc import base_of
    from .logistics import norm_name
    factors = factors or []
    tariffs = tariffs or []
    roles = roles or {}

    def role_of(it) -> dict | None:
        for key in ("division", "warehouse", "supplier"):
            try:
                v = it[key]
            except (KeyError, IndexError):
                v = None
            if v and str(v).strip():
                r = roles.get(norm_name(str(v)))
                if r:
                    return r
        return None

    groups: dict[str, dict] = {}
    for it in items:
        role = role_of(it)
        if role and role["base_name"]:
            base = role["base_name"]
        elif role and role["kind"] == "база":
            base = base_of(it)
        else:
            base = base_of(it)
        needs_move = not (role and role["kind"] == "база")
        g = groups.setdefault(base, {"volume": 0.0, "move_volume": 0.0,
                                     "own_w": 0.0, "cut_w": 0.0,
                                     "dist_w": 0.0, "cut_factor_w": 0.0,
                                     "crane_w": 0.0, "crane_free_w": 0.0})
        vol = _f(it["volume_t"])
        g["volume"] += vol
        # Доли транспорта и километры — характеристики ВЫВОЗА: взвешиваются
        # только по вывозному объёму, иначе база с большим лежалым объёмом
        # разбавляла бы плечо цехов до нуля.
        if needs_move:
            g["move_volume"] += vol
            g["own_w"] += vol * (_f(it["own_transport_pct"])
                                 if it["own_transport_pct"] is not None else 100.0)
            g["dist_w"] += vol * _f(it["distance_km"])
        g["cut_w"] += vol * (_f(it["workshop_cut_pct"]) if it["workshop_cut_pct"]
                             is not None else 0.0)
        g["cut_factor_w"] += vol * nomen_factor(it["nomenclature"], "cut_factor",
                                                factors)
        place = " ".join(str(x or "") for x in (it["warehouse"], it["division"],
                                                it["supplier"]))
        t = _tariff_for(place, tariffs)
        if t and t["own_crane"]:
            g["crane_free_w"] += vol          # грузит поставщик — тариф 0
        elif t and t["rate_per_trip"]:
            g["crane_w"] += vol * _f(t["rate_per_trip"])
    return groups


def _drivers(g: dict, n) -> dict[str, float]:
    """Драйверы объёма для одной базы (n — доступ к нормативу).

    M — вывозной объём (с цехов на базу): у групп без ролей он равен всему
    объёму V, у базы с ролями — только тоннам цехов. Вывоз (рейсы, ГСМ,
    наёмный транспорт) считается от M, переработка и отгрузка — от V."""
    V = g["volume"]
    M = g.get("move_volume", V)
    own_share = g["own_w"] / M / 100.0 if M else 0.0
    base_cut = 1 - (g["cut_w"] / V / 100.0 if V else 0.0)
    dist = g["dist_w"] / M if M else 0.0
    v_own = M * own_share
    trips = v_own / (n("truck_capacity_t") or 16)
    shifts = trips / (n("trips_per_shift") or 3)
    wagons = V / (n("wagon_capacity_t") or 62)
    loader_h = (V / (n("truck_capacity_t") or 16) * n("loader_h_per_truck")
                + wagons * n("loader_h_per_wagon"))
    w_base = V * base_cut
    manual = n("manual_cut_share_pct") / 100.0
    w_manual = w_base * manual
    # Коэффициент резки по номенклатуре: средневзвешенный по объёму базы
    # (штанга 1,8 — газа больше нормы; обычный лом 1,0).
    cut_factor = (g.get("cut_factor_w", 0.0) / V) if V else 1.0
    cut_factor = cut_factor or 1.0
    # Погрузка краном: сумма тарифов по местам этой базы; объём, который
    # поставщик грузит своим краном, тариф не набирает.
    crane_paid_t = V - g.get("crane_free_w", 0.0)
    crane_rate_avg = (g.get("crane_w", 0.0) / crane_paid_t
                      if crane_paid_t > 0 else 0.0)
    crane_trips = (crane_paid_t / (n("truck_capacity_t") or 16)
                   if crane_rate_avg else 0.0)
    return {"v": V, "v_move": M, "v_own": v_own, "v_hired": M - v_own,
            "dist": dist,
            "trips": trips, "shifts": shifts, "wagons": wagons,
            "loader_h": loader_h, "w_base": w_base, "w_manual": w_manual,
            "w_mech": w_base - w_manual, "cut_factor": cut_factor,
            "crane_trips": crane_trips, "crane_rate_avg": crane_rate_avg,
            "cut_shifts": w_manual * cut_factor / (n("cut_rate_t_shift") or 4)}


def overhead_rate(conn: sqlite3.Connection, division: str | None = None,
                  volume_t: float | None = None) -> dict:
    """Обоснование ставки распределяемых расходов (лист «распределяемые»).

    Фактические косвенные расходы из 1С (stat_overheads) по подразделениям
    и статьям делятся на объём операций периода — получается ставка руб/тн
    вместо «магического» норматива. Объём берётся из фактических продаж 1С
    (stat_sale_price) или задаётся вручную аргументом volume_t.
    """
    try:
        where, params = ("WHERE amount > 0", [])
        if division:
            where += " AND lower_ru(division) LIKE lower_ru(?)"
            params.append(f"%{division}%")
        rows = conn.execute(
            f"SELECT division, item, account, SUM(amount) AS amount, "
            f"COUNT(DISTINCT period) AS months FROM stat_overheads {where} "
            "GROUP BY division, item ORDER BY amount DESC", params).fetchall()
        months = conn.execute(
            f"SELECT COUNT(DISTINCT period) AS m FROM stat_overheads {where}",
            params).fetchone()["m"] or 0
    except sqlite3.Error:
        return {"items": [], "total": 0.0, "months": 0, "volume_t": 0.0,
                "rate_per_t": 0.0, "source": "нет данных 1С"}

    total = sum(_f(r["amount"]) for r in rows)
    volume = _f(volume_t)
    source = "объём задан вручную"
    if not volume:
        try:
            row = conn.execute(
                "SELECT SUM(total_qty_t) AS q FROM stat_sale_price").fetchone()
            volume = _f(row["q"]) if row else 0.0
            source = "фактические продажи 1С"
        except sqlite3.Error:
            volume = 0.0
    return {
        "items": [{"division": r["division"], "item": r["item"],
                   "account": r["account"], "amount": round(_f(r["amount"]), 2),
                   "months": r["months"]} for r in rows],
        "total": round(total, 2), "months": months,
        "volume_t": round(volume, 1), "source": source,
        "rate_per_t": round(total / volume, 2) if volume else 0.0,
    }


def base_capacity(conn: sqlite3.Connection, base: str) -> dict:
    """Мощность переработки базы, тн/мес: ручная резка + пресс по нормативам
    этой базы (у Ухты пресс 15 тн/смену против 30 у Усинска). Нужна, чтобы
    видеть, успевает ли база переработать приходящий объём."""
    common, by_base = _load_norms(conn)

    def n(key: str) -> float:
        for bname, ov in by_base.items():
            if bname.lower() in (base or "").lower() and key in ov:
                return _f(ov[key])
        return _f(common.get(key, 0.0))

    shifts = n("shifts_per_month") or 25
    manual = (n("cut_rate_t_shift") or 0) * shifts
    press = (n("press_rate_t_shift") or 0) * shifts
    return {"manual_t_month": round(manual, 2), "press_t_month": round(press, 2),
            "total_t_month": round(manual + press, 2), "shifts": shifts}


def load_param_meta(conn: sqlite3.Connection) -> dict[str, dict]:
    """Подписи и единицы параметров (из общих нормативов)."""
    return {r["key"]: {"label": r["label"] or r["key"], "unit": r["unit"] or ""}
            for r in conn.execute(
                "SELECT key, label, unit FROM cost_norms WHERE base IS NULL").fetchall()}


def evaluate_model(items: list, conn: sqlite3.Connection,
                   overrides: dict[str, float] | None = None) -> dict:
    """Универсальный вычислитель модели: параметры (норматив или значение,
    введённое в БП) → драйверы → суммы компонентов, статей и секций."""
    overrides = overrides or {}
    common, by_base = _load_norms(conn)
    meta = load_param_meta(conn)
    groups = _base_groups(items, load_nomen_factors(conn),
                          load_loading_tariffs(conn), point_roles(conn))

    def norm_for(base: str):
        def n(key: str) -> float:
            if key in overrides:                       # значение этой сделки
                return overrides[key]
            for bname, ov in by_base.items():          # норматив базы
                if bname.lower() in (base or "").lower() and key in ov:
                    return ov[key]
            return common.get(key, 0.0)
        return n

    components = []
    for i, comp in enumerate(COST_MODEL):
        components.append({
            "idx": i, "section": comp["section"], "article": comp["article"],
            "label": comp["label"], "hint": comp.get("hint", ""),
            "stage": comp.get("stage", ""),
            "amount": 0.0, "bases": [],
            "params": [{"key": k,
                        "label": meta.get(k, {}).get("label", k),
                        "unit": meta.get(k, {}).get("unit", ""),
                        "value": overrides.get(k, common.get(k, 0.0)),
                        "overridden": k in overrides}
                       for k in comp["params"]],
        })

    # Драйверы этапов по всем базам: объём вывоза и объём переработки —
    # база для расчёта руб/тн каждого этапа.
    stage_drivers: dict[str, float] = defaultdict(float)
    stage_by_base: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for base, g in groups.items():
        if not g["volume"]:
            continue
        n = norm_for(base)
        ns = dict(_drivers(g, n))
        for key in common:
            ns[key] = n(key)
        for st in STAGES:
            stage_drivers[st["name"]] += ns.get(st["driver"], 0.0)
        for c in components:
            try:
                amount = float(eval(COST_MODEL[c["idx"]]["formula"],
                                    {"__builtins__": {}}, ns))
            except ZeroDivisionError:
                amount = 0.0
            if amount > 0.5:
                c["amount"] += amount
                c["bases"].append(f"{base}: {amount:,.0f}".replace(",", " "))
                if c["stage"]:
                    stage_by_base[c["stage"]][base] += amount

    articles: dict[str, float] = defaultdict(float)
    sections: dict[str, float] = defaultdict(float)
    stage_totals: dict[str, float] = defaultdict(float)
    for c in components:
        c["amount"] = round(c["amount"], 2)
        articles[c["article"]] += c["amount"]
        sections[c["section"]] += c["amount"]
        if c["stage"]:
            stage_totals[c["stage"]] += c["amount"]

    stages = []
    for st in STAGES:
        amount = round(stage_totals.get(st["name"], 0.0), 2)
        driver = stage_drivers.get(st["name"], 0.0)
        stages.append({
            "name": st["name"], "hint": st["hint"], "unit": st["unit"],
            "amount": amount, "driver_volume": round(driver, 3),
            "per_t": round(amount / driver, 2) if driver else 0.0,
            "bases": [{"base": b, "amount": round(a, 2)}
                      for b, a in sorted(stage_by_base.get(st["name"], {}).items(),
                                         key=lambda kv: -kv[1])],
        })
    return {"components": components,
            "articles": {k: round(v, 2) for k, v in articles.items()},
            "sections": {k: round(v, 2) for k, v in sections.items()},
            "stages": stages,
            "total": round(sum(sections.values()), 2)}


def compute_norm_costs(items: list, conn: sqlite3.Connection,
                       overrides: dict[str, float] | None = None
                       ) -> dict[str, tuple[float, str]]:
    """Суммы по статьям P&L (для записи в bp_costs): {статья: (руб, пояснение)}."""
    model = evaluate_model(items, conn, overrides)
    out: dict[str, tuple[float, str]] = {}
    for article, amount in model["articles"].items():
        parts = [f"{c['label']} {c['amount']:,.0f}".replace(",", " ")
                 for c in model["components"]
                 if c["article"] == article and c["amount"]]
        out[article] = (amount, "; ".join(parts))
    return out


def trips_plan(items: list, conn: sqlite3.Connection,
               overrides: dict | None = None) -> list[dict]:
    """Рейсы, машины и дни ПО РОЛЯМ точек — то, что логист считал вручную
    на листе «расчет» книги экономистов.

    Наши рейсы существуют только там, где везём мы: цех → база (вывоз,
    всегда) и база → покупатель (только при «доставке»). При самовывозе с
    базы возит покупатель — наших рейсов и дней ноль, о чём строка и
    говорит. Доля своего транспорта по умолчанию 100% — ровно как в модели
    затрат (_base_groups): раньше таблица молча считала 0% своих и писала
    «наём» там, где модель начисляла ГСМ своих машин. Вагоны считаются
    только при вагонной отгрузке.
    """
    common, _ = _load_norms(conn)
    over = overrides or {}

    def n(key: str, default: float = 0.0) -> float:
        if key in over:
            return _f(over[key])
        return _f(common.get(key)) or default

    capacity = n("truck_capacity_t", 16) or 16
    per_shift = n("trips_per_shift", 3) or 3
    wagon = n("wagon_capacity_t", 62) or 62

    groups: dict[str, dict] = {}
    for it in items:
        place = (it["division"] or it["supplier"] or it["warehouse"]
                 or "Без места").strip()
        g = groups.setdefault(place, {
            "place": place, "volume": 0.0, "distance_km": None,
            "shipment": set(), "own_pct": [], "positions": 0})
        vol = _f(it["volume_t"])
        g["volume"] += vol
        g["positions"] += 1
        km = _f(it["distance_km"])
        if km:
            g["distance_km"] = max(g["distance_km"] or 0, km)
        if it["shipment"]:
            g["shipment"].add(str(it["shipment"]).strip())
        if it["own_transport_pct"] is not None:
            g["own_pct"].append(_f(it["own_transport_pct"]))

    # Разложить места по ролям: цеховые тонны стекаются на базу, объём
    # отгрузки базы = свой лежак + притоки цехов.
    from .logistics import norm_name
    roles = point_roles(conn)
    bases: dict[str, dict] = {}
    workshops: list[dict] = []
    for g in groups.values():
        r = roles.get(norm_name(g["place"]))
        if r and r["kind"] == "цех" and r["base_name"]:
            workshops.append(g)
            b = bases.setdefault(r["base_name"], {
                "place": r["base_name"], "volume": 0.0, "inflow": 0.0,
                "distance_km": None, "shipment": set(), "own_pct": [],
                "positions": 0})
            b["inflow"] += g["volume"]
            b["shipment"] |= g["shipment"]
        else:
            key = (r["base_name"] if r and r["base_name"] else g["place"])
            b = bases.setdefault(key, {
                "place": key, "volume": 0.0, "inflow": 0.0,
                "distance_km": None, "shipment": set(), "own_pct": [],
                "positions": 0})
            b["volume"] += g["volume"]
            b["positions"] += g["positions"]
            b["shipment"] |= g["shipment"]
            b["own_pct"] += g["own_pct"]
            if g["distance_km"]:
                b["distance_km"] = max(b["distance_km"] or 0, g["distance_km"])

    def row(place, stage, vol, km, shipments, own_pct, positions,
            our_trips: bool, wagons_ok: bool) -> dict:
        trips = math.ceil(vol / capacity) if (capacity and our_trips) else 0
        # Доля своего транспорта — как в модели затрат: не задана = 100% свои.
        own_share = (sum(own_pct) / len(own_pct) / 100.0 if own_pct else 1.0)
        own_trips = round(trips * own_share)
        return {
            "place": place, "stage": stage, "positions": positions,
            "volume": vol, "distance_km": km,
            "trips": trips, "own_trips": own_trips,
            "hired_trips": trips - own_trips,
            "days": round(trips / per_shift, 1) if per_shift else 0.0,
            "wagons": round(vol / wagon, 2) if (wagon and wagons_ok) else 0.0,
            "shipment": ", ".join(sorted(shipments)) or "—",
            "capacity": capacity, "per_shift": per_shift,
        }

    def is_wagon(shipments) -> bool:
        text = " ".join(shipments).lower()
        return "вагон" in text or "жд" in text or "ж/д" in text

    out = []
    for g in sorted(workshops, key=lambda x: -x["volume"]):
        out.append(row(g["place"], "вывоз на базу", g["volume"],
                       g["distance_km"], g["shipment"], g["own_pct"],
                       g["positions"], our_trips=True, wagons_ok=False))
    for b in sorted(bases.values(), key=lambda x: -(x["volume"] + x["inflow"])):
        total = b["volume"] + b["inflow"]
        if not total:
            continue
        delivery = any("доставк" in s.lower() for s in b["shipment"])
        stage = ("доставка покупателю" if delivery
                 else "самовывоз покупателя — рейсы не наши")
        out.append(row(b["place"], stage, total, b["distance_km"],
                       b["shipment"], b["own_pct"], b["positions"],
                       our_trips=delivery, wagons_ok=is_wagon(b["shipment"])))
    return out
