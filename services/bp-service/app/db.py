"""Подключение к SQLite и инициализация схемы."""
from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "bp.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
OUTPUT_DIR = BASE_DIR / "output"
ATTACH_DIR = BASE_DIR / "attachments"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # lower() в SQLite не работает для кириллицы — регистрируем свою функцию.
    conn.create_function("lower_ru", 1, lambda s: s.lower() if s else s)
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Дополнение схемы существующих баз (CREATE IF NOT EXISTS не меняет
    колонки). Каждый шаг идемпотентен."""
    # Авторизация: пароль и признаки учётной записи (роль была и раньше,
    # но входа не было — роль переключалась селектором в шапке).
    user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col, ddl in (
        ("password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
        ("is_active", "ALTER TABLE users ADD COLUMN is_active INTEGER "
                      "NOT NULL DEFAULT 1"),
        ("must_change_password", "ALTER TABLE users ADD COLUMN "
                                 "must_change_password INTEGER NOT NULL DEFAULT 0"),
        ("created_at", "ALTER TABLE users ADD COLUMN created_at TEXT"),
        ("last_login_at", "ALTER TABLE users ADD COLUMN last_login_at TEXT"),
    ):
        if col not in user_cols:
            conn.execute(ddl)
    # PIN-коды прежнего селектора ролей: вход теперь по паролю, а значения
    # лежали в настройках открытым текстом.
    conn.execute("DELETE FROM settings WHERE key IN ('pin_admin', 'pin_director')")
    # По какой строке адреса нашлись координаты пункта: «трубной базы» в
    # OpenStreetMap нет, и точка встаёт в центр посёлка — расхождение с
    # перечнем надо объяснять, а не прятать.
    point_cols = {r["name"] for r in
                  conn.execute("PRAGMA table_info(shipping_points)")}
    if "coords_note" not in point_cols:
        conn.execute("ALTER TABLE shipping_points ADD COLUMN coords_note TEXT")
    # Роль места в модели затрат: база (переработка и отгрузка) или цех
    # (только вывоз на базу base_name). Без роли модель считает по-старому.
    if "kind" not in point_cols:
        conn.execute("ALTER TABLE shipping_points ADD COLUMN kind TEXT")
    if "base_name" not in point_cols:
        conn.execute("ALTER TABLE shipping_points ADD COLUMN base_name TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(bp_costs)")}
    if "edge_id" not in cols:
        # Привязка статьи затрат к стрелке графа маршрута: каждая стрелка
        # перемещения связана с конкретными строками затрат.
        conn.execute("ALTER TABLE bp_costs ADD COLUMN edge_id INTEGER "
                     "REFERENCES bp_edges(id) ON DELETE SET NULL")
    bp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(business_plans)")}
    if "vat_unrecovered_pct" not in bp_cols:
        # Доля невозмещённого НДС, относимая на затраты, — параметр сценария
        # (БП 1674: базовый вариант 50%, вариант «минус 1000» — 30%).
        conn.execute("ALTER TABLE business_plans ADD COLUMN vat_unrecovered_pct REAL")
    item_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bp_items)")}
    if "contamination_pct" not in item_cols:
        # Засор на уровне позиции (БП 1738: 5,8–5,9% по строкам при 5% в
        # шапке). NULL = берётся общий засор БП.
        conn.execute("ALTER TABLE bp_items ADD COLUMN contamination_pct REAL")
    # ── Варианты расчёта «ДСП» (базовый, листы «_дсп») и «Лукойл» («_лук»).
    # У варианта «Лукойл» свои цены реализации и суммы статей затрат;
    # NULL = значение не задано, берётся базовое (ДСП).
    if "sale_price_luk" not in item_cols:
        conn.execute("ALTER TABLE bp_items ADD COLUMN sale_price_luk REAL")
    # Засор позиции в варианте «Лукойл» (БП 1935: труба ТТ 114х73 — 10% в
    # «Лукойл» против 5% в «ДСП»). NULL = как в базовом варианте.
    if "contamination_pct_luk" not in item_cols:
        conn.execute("ALTER TABLE bp_items ADD COLUMN contamination_pct_luk REAL")
    # Код номенклатуры у продавца (КССС / номенклатурный номер): точный ключ
    # для повторного сопоставления с 1С — имена в перечнях пишут по-разному.
    if "seller_code" not in item_cols:
        conn.execute("ALTER TABLE bp_items ADD COLUMN seller_code TEXT")
    # Группа аналитического учёта — единая классификация позиции вместо пары
    # «категория + тип закупки»: по ней берутся цена и план продажи.
    if "cargo_group" not in item_cols:
        conn.execute("ALTER TABLE bp_items ADD COLUMN cargo_group TEXT")
    # План продажи в терминах групп аналитического учёта: труба может
    # продаваться ломом, и цена тогда берётся по группе лома.
    if "sale_group" not in item_cols:
        conn.execute("ALTER TABLE bp_items ADD COLUMN sale_group TEXT")
    # Экспертиза коммерсанта по позиции и реквизиты перечня, влияющие на план.
    for col in ("liquidity", "expert_note", "price_set_by", "price_set_at",
                "sale_period", "origin_reason",
                # Указания продавца по реализации из перечня («БП ДСП» /
                # «БП Лукойл» / «ШУМЕЙКО») — раньше терялись, переносили руками.
                "sale_instruction", "sale_instruction_luk"):
        if col not in item_cols:
            conn.execute(f"ALTER TABLE bp_items ADD COLUMN {col} TEXT")
    hist_cols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(nomen_match_history)")}
    if "seller_code" not in hist_cols:
        conn.execute("ALTER TABLE nomen_match_history ADD COLUMN seller_code TEXT")
    # Кратность партии покупателя, тн (машина 20 тн): остаток сверх целых
    # партий — «хвост», который идёт на переработку или другому покупателю.
    if "batch_size_t" not in item_cols:
        conn.execute("ALTER TABLE bp_items ADD COLUMN batch_size_t REAL")
    # Поля перечня продавца (лист «база данных» книги 1785): балансовая
    # стоимость лома, документы и состояние — обоснование цены и работ.
    for col in ("balance_price", "balance_cost"):
        if col not in item_cols:
            conn.execute(f"ALTER TABLE bp_items ADD COLUMN {col} REAL")
    for col in ("tech_doc", "storage_conditions", "condition_note",
                "extra_works"):
        if col not in item_cols:
            conn.execute(f"ALTER TABLE bp_items ADD COLUMN {col} TEXT")
    cost_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bp_costs)")}
    if "amount_luk" not in cost_cols:
        conn.execute("ALTER TABLE bp_costs ADD COLUMN amount_luk REAL")
    # Версии расчёта: автоснимок при каждом изменении. Вариант и сценарий
    # показываются в списке версий под БП, snapshot_hash отсекает повторы.
    ver_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bp_versions)")}
    for col, ddl_type in (("variant", "TEXT"), ("scenario", "TEXT"),
                          ("net_profit_bsp", "REAL"), ("net_profit_luk", "REAL"),
                          ("change_note", "TEXT"), ("snapshot_hash", "TEXT"),
                          # Утверждённая версия (одна на БП): печатная форма
                          # формируется по её снимку, а не по текущим данным.
                          ("approved_at", "TEXT"), ("approved_by", "TEXT")):
        if col not in ver_cols:
            conn.execute(f"ALTER TABLE bp_versions ADD COLUMN {col} {ddl_type}")
    for col, ddl in [
        # Активный вариант карточки и флаг «вариант Лукойл заведён».
        ("active_variant", "ALTER TABLE business_plans ADD COLUMN active_variant "
                           "TEXT NOT NULL DEFAULT 'bsp'"),
        ("has_luk", "ALTER TABLE business_plans ADD COLUMN has_luk "
                    "INTEGER NOT NULL DEFAULT 0"),
        # Контрольные суммы из файла экономистов для сверки (по вариантам).
        ("control_op_profit_bsp", "ALTER TABLE business_plans ADD COLUMN "
                                  "control_op_profit_bsp REAL"),
        ("control_net_profit_bsp", "ALTER TABLE business_plans ADD COLUMN "
                                   "control_net_profit_bsp REAL"),
        ("control_op_profit_luk", "ALTER TABLE business_plans ADD COLUMN "
                                  "control_op_profit_luk REAL"),
        ("control_net_profit_luk", "ALTER TABLE business_plans ADD COLUMN "
                                   "control_net_profit_luk REAL"),
        # Переопределения параметров сделки в варианте «Лукойл» (JSON:
        # {removal_months: 5, ...}; в БП 1929 срок вывоза дсп=4, лук=5).
        ("luk_overrides", "ALTER TABLE business_plans ADD COLUMN "
                          "luk_overrides TEXT"),
        # Старое название БП — имя исходной книги экономистов
        # («1785_Лукойл-Астраханьэнерго, ...»), показывается в скобках.
        ("source_name", "ALTER TABLE business_plans ADD COLUMN "
                        "source_name TEXT"),
        # База привлечённого капитала, руб (NULL = закупка с НДС): в БП 1785
        # капитал считается от «стоимости закупа» без меди.
        ("capital_base", "ALTER TABLE business_plans ADD COLUMN "
                         "capital_base REAL"),
        # Отсрочка оплаты покупателем, мес после отгрузки («месяц на вывоз,
        # месяц на дебиторку») — удлиняет удержание капитала.
        ("payment_delay_months", "ALTER TABLE business_plans ADD COLUMN "
                                 "payment_delay_months REAL"),
        # Номер задачи в Битрикс24 — БП ведётся параллельно задаче.
        ("bitrix_task_id", "ALTER TABLE business_plans ADD COLUMN "
                           "bitrix_task_id TEXT"),
        # Тип сделки (труба / лом / кабель / …) и как он получен: тип задаёт
        # логику расчёта, поэтому хранится у сделки, а не выводится каждый раз.
        ("bp_type", "ALTER TABLE business_plans ADD COLUMN bp_type TEXT"),
        ("bp_type_source", "ALTER TABLE business_plans ADD COLUMN "
                           "bp_type_source TEXT"),
        # Доля лота (15.09.2026): сделка может делиться с партнёром.
        ("lot_share_pct", "ALTER TABLE business_plans ADD COLUMN lot_share_pct REAL"),
        ("lot_share_source", "ALTER TABLE business_plans ADD COLUMN lot_share_source TEXT"),
        ("deal_no", "ALTER TABLE business_plans ADD COLUMN deal_no TEXT"),
        ("deal_partner_pct", "ALTER TABLE business_plans ADD COLUMN deal_partner_pct REAL"),
        ("deal_winner", "ALTER TABLE business_plans ADD COLUMN deal_winner TEXT"),
        ("deal_stage", "ALTER TABLE business_plans ADD COLUMN deal_stage TEXT"),
        ("site", "ALTER TABLE business_plans ADD COLUMN site TEXT"),
        ("site_source", "ALTER TABLE business_plans ADD COLUMN site_source TEXT"),
    ]:
        if col not in bp_cols:
            conn.execute(ddl)
    # Архив реестра: пробы и черновики не должны теряться среди рабочих БП.
    # Удаления нет намеренно — БП с расчётом и версиями стирать нельзя, даже
    # если он выглядит тестовым: признак снимается одним нажатием.
    if "archived" not in bp_cols:
        conn.execute("ALTER TABLE business_plans ADD COLUMN archived INTEGER "
                     "NOT NULL DEFAULT 0")
    # Единица выпуска в статистике переработки: ставка руб/шт и руб/т —
    # разные величины, складывать их в один норматив нельзя.
    # Загрузка рейсов: сколько рейсов отброшено как невозможные по весу.
    load_cols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(stat_vehicle_load)")}
    if load_cols and "dropped" not in load_cols:
        conn.execute("ALTER TABLE stat_vehicle_load ADD COLUMN dropped "
                     "INTEGER NOT NULL DEFAULT 0")
    # Матрица затрат: сколько наблюдений отброшено как выбросы.
    matrix_cols = {r["name"] for r in
                   conn.execute("PRAGMA table_info(stat_cost_matrix)")}
    if matrix_cols and "outliers" not in matrix_cols:
        conn.execute("ALTER TABLE stat_cost_matrix ADD COLUMN outliers "
                     "INTEGER NOT NULL DEFAULT 0")
    proc_ddl = (conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'stat_processing'").fetchone() or {"sql": ""})["sql"] or ""
    if "cargo_group, unit)" not in proc_ddl.replace("\n", " "):
        # Уникальный ключ таблицы тоже меняется (в него входит единица), а
        # ALTER TABLE ограничения не правит — пересоздаём с переносом строк.
        conn.execute("ALTER TABLE stat_processing RENAME TO stat_processing_old")
        conn.execute("""
            CREATE TABLE stat_processing (
                id          INTEGER PRIMARY KEY,
                work_type   TEXT NOT NULL,
                division    TEXT,
                cargo_group TEXT,
                samples     INTEGER NOT NULL,
                total_qty   REAL,
                rate_fot    REAL,
                rate_prr    REAL,
                rate_gsm    REAL,
                rate_amort  REAL,
                labor_per_t REAL,
                unit        TEXT,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (work_type, division, cargo_group, unit)
            )""")
        conn.execute(
            "INSERT INTO stat_processing (work_type, division, cargo_group, "
            "samples, total_qty, rate_fot, rate_prr, rate_gsm, rate_amort, "
            "labor_per_t, updated_at) SELECT work_type, division, cargo_group, "
            "samples, total_qty, rate_fot, rate_prr, rate_gsm, rate_amort, "
            "labor_per_t, updated_at FROM stat_processing_old")
        conn.execute("DROP TABLE stat_processing_old")
    # Фотографии лота: дата съёмки (EXIF или вручную) и подпись.
    att_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bp_attachments)")}
    if "taken_at" not in att_cols:
        conn.execute("ALTER TABLE bp_attachments ADD COLUMN taken_at TEXT")
    if "note" not in att_cols:
        conn.execute("ALTER TABLE bp_attachments ADD COLUMN note TEXT")
    # Старое название для ранее импортированных БП — из журнала импорта.
    if "source_name" not in bp_cols:
        for r in conn.execute(
                "SELECT bp_id, details FROM audit_log WHERE action = 'import_bp' "
                "ORDER BY id").fetchall():
            name = (r["details"] or "").split(":", 1)[0].strip()
            name = name.rsplit(".xls", 1)[0]
            if name:
                conn.execute(
                    "UPDATE business_plans SET source_name = ? "
                    "WHERE id = ? AND source_name IS NULL", (name, r["bp_id"]))
    conn.execute(
        "INSERT INTO settings (key, value, comment) "
        "SELECT 'vat_unrecovered_pct', '50', 'Доля невозмещённого НДС, относимая "
        "на затраты, % (методика БП 1865)' "
        "WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = 'vat_unrecovered_pct')")
    conn.execute(
        "INSERT INTO settings (key, value, comment) "
        "SELECT 'cost_matrix_from', '2025-01-01', 'С какой даты берутся "
        "наблюдения в матрицу фактических затрат' "
        "WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = 'cost_matrix_from')")
    conn.execute(
        "INSERT INTO settings (key, value, comment) "
        "SELECT 'bp_type_threshold_pct', '80', 'Порог доминирования типа сделки: "
        "сколько процентов тоннажа лота должен занимать один тип, чтобы БП "
        "считался этого типа, а не смешанным' "
        "WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = 'bp_type_threshold_pct')")
    # Справочник типов сделки: наполняется типовыми значениями один раз,
    # дальше живёт своей жизнью (экономист правит названия и состав).
    # Группы аналитического учёта 1С по типам сделок — для фактической
    # рентабельности из «Реализации» (15.09.2026).
    if "fact_groups" not in {r[1] for r in conn.execute("PRAGMA table_info(ref_bp_types)")}:
        conn.execute("ALTER TABLE ref_bp_types ADD COLUMN fact_groups TEXT")
    conn.execute(
        "INSERT INTO settings (key, value, comment) "
        "SELECT 'type_margin_closed_pct', '80', 'Порог закрытости сделки для факта "
        "рентабельности: продано не меньше N % купленного, %' "
        "WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = 'type_margin_closed_pct')")
    if "rate_mat" not in {r[1] for r in conn.execute("PRAGMA table_info(stat_processing)")}:
        conn.execute("ALTER TABLE stat_processing ADD COLUMN rate_mat REAL")
    from . import bp_types, fact_costs, loading, type_margin
    type_margin.seed_groups(conn)
    fact_costs.seed_map(conn)
    from . import fact_model
    fact_model.seed_sites(conn)
    bp_types.seed(conn)
    loading.seed_categories(conn)
    # Уже заведённым сделкам тип проставляется один раз по составу лота:
    # без него реестр и логика затрат не отличают кабельный лот от трубного.
    # Пустой тип у БП без позиций — не ошибка, определится при загрузке КП.
    for row in conn.execute(
            "SELECT id FROM business_plans "
            "WHERE bp_type IS NULL AND bp_type_source IS NULL").fetchall():
        items = conn.execute(
            "SELECT purchase_type, volume_t FROM bp_items WHERE bp_id = ?",
            (row["id"],)).fetchall()
        if items:
            bp_types.apply_auto(conn, row["id"], items)


def get_setting(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return float(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def log(conn: sqlite3.Connection, user: str, bp_id: int | None, action: str,
        details: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (user_name, bp_id, action, details) VALUES (?, ?, ?, ?)",
        (user, bp_id, action, details),
    )
