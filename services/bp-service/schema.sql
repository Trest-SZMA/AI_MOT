-- Схема базы данных сервиса подготовки бизнес-планов МетОптТорг. Версия 2.
-- Диалект: SQLite (типы совместимы с PostgreSQL).
-- Модель построена по реальному БП (лот №1759 Лукойл-Пермь/Коми) и P&L-отчёту.

PRAGMA foreign_keys = ON;

-- ── Пользователи и настройки ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    login      TEXT NOT NULL UNIQUE,
    full_name  TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN
               ('manager', 'logist', 'economist', 'director', 'admin')),
    -- Пароль: PBKDF2-HMAC-SHA256, формат pbkdf2_sha256$итерации$соль$хеш.
    -- Пустой хеш = вход невозможен, пока администратор не задаст пароль.
    password_hash        TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at        TEXT
);

-- Сессии входа: токен из cookie → пользователь. Смена пароля и отключение
-- учётной записи удаляют все её сессии.
CREATE TABLE IF NOT EXISTS user_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    ip         TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    comment    TEXT
);

-- ── Справочники из 1С (выгрузка Справочники.xlsx) ───────────────────

CREATE TABLE IF NOT EXISTS ref_counterparties (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    guid  TEXT UNIQUE,
    ctype TEXT                          -- Завод / Трейдер / '-'
);

CREATE TABLE IF NOT EXISTS ref_warehouses (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    guid        TEXT UNIQUE,
    parent_name TEXT,
    parent_guid TEXT,
    top_parent  TEXT,                   -- дивизион/проект: 4.Коми, 6.Ответхранение...
    is_group    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ref_divisions (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE,
    name        TEXT NOT NULL,
    parent_code TEXT,
    parent_name TEXT
);

CREATE TABLE IF NOT EXISTS ref_nomen_groups (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    guid        TEXT UNIQUE,
    parent_name TEXT,
    parent_guid TEXT
);

-- Номенклатура 1С (позиции, а не группы) — цель автосопоставления.
CREATE TABLE IF NOT EXISTS ref_nomenclature_1c (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    guid        TEXT UNIQUE,
    unit        TEXT,                   -- единица для отчётов (т, шт, м)
    gost        TEXT,
    cargo_group TEXT,                   -- номенклатурная группа грузов
    norm        TEXT                    -- нормализованное имя для поиска
);

-- История подтверждённых сопоставлений: номенклатура продавца → наша 1С.
-- Основа «ИИ-предложений» для новых БП.
CREATE TABLE IF NOT EXISTS nomen_match_history (
    id            INTEGER PRIMARY KEY,
    seller_norm   TEXT NOT NULL,        -- нормализованное имя продавца
    seller_name   TEXT NOT NULL,
    seller_code   TEXT,                 -- код продавца: точный ключ повторного
                                        -- сопоставления (имя может отличаться)
    nomen_1c      TEXT NOT NULL,
    nomen_1c_guid TEXT,
    uses          INTEGER NOT NULL DEFAULT 1,   -- сколько раз подтверждено
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (seller_norm, nomen_1c)
);

CREATE TABLE IF NOT EXISTS ref_work_types (       -- процессы переработки (1С)
    id   INTEGER PRIMARY KEY,
    guid TEXT UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ref_expense_items (    -- статьи расходов (1С)
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    guid    TEXT UNIQUE,
    parent  TEXT,
    account TEXT
);

-- ── Номенклатура БП (синергия категорий БП с группами 1С) ───────────

CREATE TABLE IF NOT EXISTS nomenclature (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,  -- наименование в БП/спецификации
    category      TEXT,                  -- 12А/13А, 5А, медь, НКТ 73х5.5 ...
    purchase_type TEXT NOT NULL,         -- лом / труба / цветмет / кабель / ДХНО
    group_1c      TEXT,                  -- группа аналитического учёта 1С
    group_guid    TEXT,
    gost          TEXT,
    aliases       TEXT                   -- синонимы через ';'
);

-- Коэффициенты нормативов по номенклатуре: резка штанги и НКТ съедает
-- газа/кислорода в 1,5–2 раза больше общей нормы (со слов экономиста).
-- Маска ищется по вхождению в наименование позиции, без учёта регистра.
CREATE TABLE IF NOT EXISTS norm_nomen_factors (
    id      INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,               -- маска номенклатуры: «штанг», «нкт»
    key     TEXT NOT NULL,               -- норматив/драйвер: cut_factor ...
    factor  REAL NOT NULL DEFAULT 1,     -- множитель к нормативу
    comment TEXT,
    UNIQUE (pattern, key)
);

-- Тарифы погрузки по местам вывоза: у части поставщиков грузят своим краном
-- (Советский, Когалым — бесплатно), в остальных кран нанимается; неполная
-- машина считается по доле тарифа.
CREATE TABLE IF NOT EXISTS loading_tariffs (
    id            INTEGER PRIMARY KEY,
    place         TEXT NOT NULL UNIQUE,  -- место погрузки (маска по вхождению)
    own_crane     INTEGER NOT NULL DEFAULT 0,  -- 1 = грузят своим краном (0 руб)
    rate_per_trip REAL,                  -- тариф за полную машину, руб
    part_load_pct REAL DEFAULT 50,       -- доля тарифа при неполной машине, %
    cargo_type    TEXT,                  -- навалом / штуками / прочее
    comment       TEXT
);

-- Транспортные ставки перевозчика из приказа 1С (регистр «Установка
-- транспортных ставок»). Загружаются файлом выгрузки целиком (замена всего
-- справочника). kind: current — рабочая ставка (документ «Ввод согласованного
-- прайса»), proposed — повышение в статусе «На согласовании» (только пометка,
-- в расчёты не идёт), was — строка «Было» документа повышения (для сверки).
CREATE TABLE IF NOT EXISTS transport_rates (
    id           INTEGER PRIMARY KEY,
    doc_number   TEXT,                  -- номер документа 1С
    doc_date     TEXT,                  -- дата документа
    doc_kind     TEXT,                  -- вид операции (прайс / повышение)
    doc_status   TEXT,                  -- статус документа в 1С
    organization TEXT,                  -- перевозчик
    responsible  TEXT,
    route        TEXT NOT NULL,         -- «Урай - Пермь»
    route_from   TEXT,
    route_to     TEXT,
    vehicle      TEXT,                  -- тент/борт, кран 25 т, КМУ...
    unit         TEXT,                  -- т / рейс / ч / смена
    price        REAL NOT NULL,
    conditions   TEXT,                  -- «до 20 тонн», «8 ч», «Было/Стало»
    kind         TEXT NOT NULL,         -- current / proposed / was
    row_guid     TEXT,
    loaded_at    TEXT NOT NULL DEFAULT (datetime('now')),
    source_file  TEXT
);
CREATE INDEX IF NOT EXISTS idx_transport_rates_route
    ON transport_rates(route);

-- Производственные подразделения 1С: официальная структура «цех → база»
-- (ветка БАЗЫ и ветка ПРОИЗВОДСТВО/ЗАГОТОВКА, связь через аналитическое
-- подразделение). Загружается из выгрузки 1С скриптом
-- scripts/import_prod_units.py.
CREATE TABLE IF NOT EXISTS ref_prod_units (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    code          TEXT,
    parent_name   TEXT,
    functional_base TEXT,               -- функциональное подразделение (база)
    analytic_base TEXT,                 -- аналитическое подразделение (база)
    guid          TEXT UNIQUE,
    parent_guid   TEXT
);

-- Сезонность доступа к местам вывоза: на зимники технику пускают только
-- зимой, поэтому равномерное деление объёма по месяцам неверно.
-- Маска ищется по вхождению в базу/склад/подразделение/примечание позиции.
CREATE TABLE IF NOT EXISTS seasonal_access (
    id      INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL UNIQUE,        -- «зимник», имя базы...
    months  TEXT NOT NULL,               -- доступные месяцы: «1,2,3,11,12»
    comment TEXT
);

-- ── Бизнес-план ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS business_plans (
    id                INTEGER PRIMARY KEY,
    bp_number         TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'Черновик' CHECK (status IN
                      ('Черновик', 'На проверке', 'На согласовании',
                       'Согласован', 'Отклонён', 'Доработка')),
    version           INTEGER NOT NULL DEFAULT 1,
    scenario          TEXT,               -- v0 02.12.25 и т.п.
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    manager           TEXT,
    source_type       TEXT,               -- Тендер / Прямая закупка / Аукцион
    tender_ref        TEXT,               -- № запроса/перечня
    bitrix_task_id    TEXT,               -- номер задачи в Битрикс24: карточка
                                          -- ведётся параллельно задаче, ссылка
                                          -- открывает её напрямую
    division          TEXT,               -- дивизион: Коми, Западная Сибирь...
    archived          INTEGER NOT NULL DEFAULT 0,
                                          -- убран из рабочего реестра (проба,
                                          -- черновик): не удалён, находится
                                          -- фильтром и возвращается обратно

    -- Контрагенты сделки
    seller_id         INTEGER REFERENCES ref_counterparties(id),
    seller_name       TEXT,               -- продавец (напр. ООО "ЛУКОЙЛ-ПЕРМЬ")
    buyer_id          INTEGER REFERENCES ref_counterparties(id),
    buyer_name        TEXT,               -- покупатель (завод/трейдер)

    -- Параметры лота (закупка)
    lot_cost          REAL,               -- стоимость лота БЕЗ НДС (порог закупки)
    auction_step      REAL,               -- шаг аукциона, руб
    contamination_pct REAL,               -- засор чёрного лома, %
    removal_months    REAL,               -- срок вывоза / расчёта капитала, мес
    start_month       TEXT,               -- месяц начала вывоза
    shipment_type     TEXT,               -- самовывоз / доставка / вагоны...
    relocation        TEXT,               -- перемещение: да/нет/описание
    payment_terms     TEXT,
    purchase_special  TEXT,

    -- Ставки (по умолчанию из settings, экономист может уточнить)
    vat_rate          REAL,               -- НДС, %
    vat_unrecovered_pct REAL,             -- доля невозмещённого НДС на затраты, %
    capital_rate      REAL,               -- ставка привлечённого капитала, % годовых
    tax_rate          REAL,               -- налог на прибыль, %
    payroll_tax_rate  REAL,               -- налоги с ФОТ, %
    shipment_loss_pct REAL,               -- потери при отгрузке, % (реализация = 97%)
    scen_price_delta  REAL,               -- сценарии: сдвиг цены реализации, руб/тн

    -- Варианты расчёта: «ДСП» (базовый, листы «_дсп») и «Лукойл» (листы «_лук»)
    active_variant    TEXT NOT NULL DEFAULT 'bsp',  -- вариант, открытый в карточке
    has_luk           INTEGER NOT NULL DEFAULT 0,   -- вариант «Лукойл» заведён
    control_op_profit_bsp  REAL,          -- контрольные суммы из файла экономистов
    control_net_profit_bsp REAL,          -- (сверка пересчёта с исходной книгой)
    control_op_profit_luk  REAL,
    control_net_profit_luk REAL,
    luk_overrides     TEXT,               -- JSON: параметры, отличающиеся в «Лукойл»
    source_name       TEXT,               -- старое название БП (имя книги экономистов)
    capital_base      REAL,               -- база капитала, руб (NULL = закупка с НДС)
    payment_delay_months REAL,            -- отсрочка оплаты покупателем, мес

    -- Ключевые даты (ISO-формат — сортировка и фильтрация штатными средствами SQL)
    approved_date     TEXT,               -- дата согласования БП
    payment_date      TEXT,               -- дата оплаты (закупки номенклатуры)
    work_start_date   TEXT,               -- дата начала выполнения работ

    -- Риски (итог) и рекомендация
    -- Тип сделки (ref_bp_types.code) и как он получен: auto — по составу
    -- лота при загрузке КП, manual — выбран человеком и не перетирается.
    bp_type           TEXT,
    bp_type_source    TEXT,

    risk_expert       TEXT,
    recommendation    TEXT,
    economist_comment TEXT,
    clarify_notes     TEXT
    -- Доля лота (шаг 2 разбора «Реализации», 15.09.2026): половина сделок
    -- делится с партнёром (УВМ). План выручки, закупка и капитал считаются
    -- на нашу долю. NULL = 100 %. Источник: 'deal' (реестр сделок Битрикса)
    -- или 'manual' (задано в шапке и не перетирается импортом).
    lot_share_pct     REAL,
    lot_share_source  TEXT,
    deal_no           TEXT,      -- «№ в текущем реестре» сделки Битрикса
    deal_partner_pct  REAL,      -- доля УВМ
    deal_winner       TEXT,      -- юрлицо, выигравшее КП (МОТ/УВМ/ИВЦ)
    deal_stage        TEXT,
    -- Площадка компании (аналитическая база из «Цеха и базы» 1С:
    -- «Усинск (База + Цех)», «Юг»…): по ней берётся фактическая ставка
    -- распределяемых расходов. 'auto' — определить по подразделению серии.
    site              TEXT,
    site_source       TEXT
);

-- Позиции лота: привязка к поставщику/подразделению/месту хранения.
CREATE TABLE IF NOT EXISTS bp_items (
    id             INTEGER PRIMARY KEY,
    bp_id          INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    supplier       TEXT,                  -- дивизион/город поставщика (Усинск, Ухта)
    division       TEXT,                  -- подразделение / месторождение / база
    warehouse      TEXT,                  -- место хранения (склад 1С)
    nomenclature   TEXT NOT NULL,         -- наименование (состав лота по спецификации)
    seller_code    TEXT,                  -- код номенклатуры у продавца (КССС,
                                          -- номенклатурный номер) — по нему
                                          -- сопоставление переиспользуется точнее,
                                          -- чем по названию: имена в перечнях пишут
                                          -- по-разному, код у позиции один
    cargo_group    TEXT,                  -- группа аналитического учёта (справочник
                                          -- 1С): единая классификация позиции —
                                          -- по ней берётся цена, план продажи и
                                          -- выводится тип для НДС и засора
    category       TEXT,                  -- категория лома: 12А/13А, 5А, медь...
    purchase_type  TEXT NOT NULL DEFAULT 'лом',  -- лом/труба/цветмет/кабель/ДХНО
    unit           TEXT DEFAULT 'тн',
    volume_t       REAL NOT NULL DEFAULT 0,      -- объём закупки
    sale_type      TEXT,                  -- ПЛАН продажи (может отличаться: труба→лом)
    sale_group     TEXT,                  -- группа аналитического учёта, в которой
                                          -- позиция ПРОДАЁТСЯ: покупаем «Труба НКТ»,
                                          -- продаём «Лом черных металлов» — по ней
                                          -- берётся ориентир цены и выводится тип
    sale_price     REAL,                  -- цена реализации, руб/тн (вариант ДСП)
    sale_price_luk REAL,                  -- цена реализации в варианте «Лукойл»
    contamination_pct REAL,               -- засор позиции, % (NULL = общий засор БП)
    contamination_pct_luk REAL,           -- засор позиции в варианте «Лукойл» (NULL = как ДСП)
    batch_size_t   REAL,                  -- кратность партии покупателя, тн (машина 20 тн)
    -- Реквизиты перечня продавца (лист «база данных»): обоснование цены и работ
    balance_price  REAL,                  -- балансовая цена за единицу, руб
    balance_cost   REAL,                  -- балансовая стоимость позиции, руб
    tech_doc       TEXT,                  -- наличие техдокументации (паспорт по коду отходов)
    storage_conditions TEXT,              -- условия хранения (открытая площадка...)
    condition_note TEXT,                  -- состояние (находилось в эксплуатации...)
    extra_works    TEXT,                  -- необходимость дополнительных работ
    -- Экспертиза коммерсанта (листы «свод Шумейко»/«СВОД» книг экономистов):
    -- вердикт по позиции определяет и цену, и саму возможность продажи.
    liquidity      TEXT,                  -- товарная / не товарная партия /
                                          -- неликвид / не ходовой размер
    expert_note    TEXT,                  -- комментарий коммерсанта к позиции
    price_set_by   TEXT,                  -- кто дал цену (Шумейко, Пуганов…)
    price_set_at   TEXT,                  -- когда дана цена
    -- Реквизиты перечня продавца, влияющие на план вывоза
    sale_period    TEXT,                  -- предполагаемый период реализации
    origin_reason  TEXT,                  -- причина возникновения (от списания…)
    -- Указания продавца по реализации из перечня (столбцы «БП ДСП» /
    -- «БП Лукойл» / «ШУМЕЙКО»): «Продаем с места по 22500 + НДС» и т.п.
    sale_instruction     TEXT,            -- указание для базового варианта (ДСП)
    sale_instruction_luk TEXT,            -- указание для варианта «Лукойл»
    -- Транспорт и переработка по позиции (колонки AD-AG из расчёта экономистов)
    own_transport_pct REAL,               -- доля собственным транспортом, % (остальное наём)
    workshop_cut_pct  REAL,               -- доля подрезки на цехе, % (остальное на базе)
    -- Сопоставление с номенклатурой 1С (для выгрузки отчёта в 1С)
    nomen_1c        TEXT,                 -- наша номенклатура 1С
    nomen_1c_guid   TEXT,
    match_source    TEXT,                 -- авто / ИИ (история) / вручную
    match_confirmed INTEGER NOT NULL DEFAULT 0,  -- подтверждено мастером с места
    -- Построчная экономика (колонки U-W и AH из расчёта экономистов)
    buyer          TEXT,                  -- покупатель по позиции (может отличаться)
    shipment       TEXT,                  -- вид отгрузки (вагон самовывоз / доставка)
    price_owner    TEXT,                  -- ответственный по ценам
    distance_km    REAL,                  -- км перемещения до базы (для норм ГСМ)
    note           TEXT
);

-- Пункты отгрузки: места, откуда физически забирают МТР. В спецификации
-- продавца это адрес хранения («Волгоградская обл., г. Котово, трубная
-- база») плюс ближайший базовый логистический пункт и расстояние до него.
-- Регистрируются при импорте и живут между сделками: один и тот же куст
-- встречается в разных лотах, и накопленные сопоставление со складом 1С,
-- координаты и проверенные расстояния переиспользуются.
CREATE TABLE IF NOT EXISTS shipping_points (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,         -- адрес/название места отгрузки
    name_norm     TEXT NOT NULL UNIQUE,  -- нормализованное имя (ключ поиска дублей)
    region        TEXT,                  -- субъект РФ из адреса
    base_point    TEXT,                  -- базовый логистический пункт (Котово, Самара)
    distance_km   REAL,                  -- расстояние до базового пункта из перечня
    lat           REAL,                  -- координаты (для маршрутов и ссылок 2ГИС)
    lon           REAL,
    coords_note   TEXT,                  -- по какой строке адреса нашлись координаты
    warehouse_id  INTEGER REFERENCES ref_warehouses(id),  -- сопоставленный склад 1С
    warehouse_name TEXT,
    match_source  TEXT,                  -- авто / вручную
    match_confirmed INTEGER NOT NULL DEFAULT 0,
    seller_name   TEXT,                  -- у кого забирали в первый раз
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- Роль места в модели затрат. база: здесь переработка и отгрузка;
    -- цех: только вывоз на базу. NULL = не задана, модель считает
    -- по-старому (все этапы на самом месте).
    kind          TEXT CHECK (kind IN ('база', 'цех') OR kind IS NULL),
    base_name     TEXT                   -- база, куда цех свозит (у базы — само место)
);

-- Маршруты между пунктами: расстояние по дорогам. Заполняется вручную или
-- из 2ГИС; храним, чтобы не пересчитывать и видеть, чем обосновано плечо.
CREATE TABLE IF NOT EXISTS shipping_routes (
    id          INTEGER PRIMARY KEY,
    from_point  INTEGER NOT NULL REFERENCES shipping_points(id) ON DELETE CASCADE,
    to_name     TEXT NOT NULL,           -- куда: база, цех или покупатель
    to_point    INTEGER REFERENCES shipping_points(id) ON DELETE SET NULL,
    distance_km REAL,
    duration_min REAL,
    source      TEXT,                    -- 2gis / вручную / перечень
    checked_at  TEXT,
    note        TEXT,
    UNIQUE (from_point, to_name)
);

CREATE INDEX IF NOT EXISTS idx_ship_points_wh ON shipping_points(warehouse_id);

-- План продажи позиции: одну позицию можно продать нескольким покупателям,
-- разделив объём (БП 1935: труба уходит и на Чермет-Волжский, и на Втор-ресурс).
-- Строки заводятся отдельно для каждого варианта расчёта: у «ДСП» и «Лукойл»
-- покупатели и цены разные (в 1935 «Лукойл» везёт на ВТЗ и Балаково).
-- Если строк нет — позиция продаётся целиком по своим buyer/sale_price.
CREATE TABLE IF NOT EXISTS bp_item_sales (
    id       INTEGER PRIMARY KEY,
    bp_id    INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES bp_items(id) ON DELETE CASCADE,
    variant  TEXT NOT NULL DEFAULT 'bsp' CHECK (variant IN ('bsp', 'luk')),
    buyer    TEXT,
    volume_t REAL NOT NULL DEFAULT 0,     -- объём закупки, уходящий этому покупателю
    sale_price REAL,                      -- цена реализации, руб/ед (пусто = цена позиции)
    sale_type TEXT,                       -- план продажи (труба продаётся ломом и т.п.)
    shipment TEXT,                        -- вид отгрузки для этого покупателя
    contamination_pct REAL,               -- засор строки, % (пусто = засор позиции)
    note     TEXT,
    sort     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bp_item_sales_item
    ON bp_item_sales(bp_id, item_id, variant);

-- Ручная корректировка разреза затрат базы на логистику и переработку.
-- Итог по базе задан фактическими статьями БП; нормативная модель лишь
-- делит его на «пути» и «площадку». Экономист может поправить это деление
-- по факту работы — сумма по базе при этом не меняется, разница уходит в
-- «прочие», поэтому сходимость с P&L сохраняется.
CREATE TABLE IF NOT EXISTS bp_base_split (
    id         INTEGER PRIMARY KEY,
    bp_id      INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    base       TEXT NOT NULL,
    variant    TEXT NOT NULL DEFAULT 'bsp' CHECK (variant IN ('bsp', 'luk')),
    logistics  REAL,
    processing REAL,
    comment    TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (bp_id, base, variant)
);

-- Затраты по статьям P&L (структура из отчёта о прибылях и убытках).
-- Статья может быть привязана к базе (base) и/или к стрелке графа маршрута
-- (edge_id): каждая стрелка перемещения связана с конкретными строками
-- затрат, а затраты распределяются на позиции этой базы/потока.
CREATE TABLE IF NOT EXISTS bp_costs (
    id       INTEGER PRIMARY KEY,
    bp_id    INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    section  TEXT NOT NULL CHECK (section IN
             ('Переменные', 'Персонал', 'Постоянные', 'Административные', 'Прочие')),
    item     TEXT NOT NULL,               -- статья затрат
    amount   REAL NOT NULL DEFAULT 0,     -- сумма, руб за весь срок (вариант ДСП)
    amount_luk REAL,                      -- сумма в варианте «Лукойл» (NULL = как ДСП)
    base     TEXT,                        -- база/узел, к которому относится
    edge_id  INTEGER REFERENCES bp_edges(id) ON DELETE SET NULL,
    comment  TEXT
);

-- Происхождение суммы статьи затрат: откуда взялась цифра и кто её поставил.
-- Требование директора: сумма должна опираться на норматив, факт или книгу, а
-- ручной ввод — быть видимым отклонением с обоснованием, а не раствориться в
-- итоге. Строка своя у каждого варианта расчёта: у ДСП сумма может быть по
-- модели, а у «Лукойла» — поставлена руками.
CREATE TABLE IF NOT EXISTS bp_cost_origin (
    id         INTEGER PRIMARY KEY,
    cost_id    INTEGER NOT NULL REFERENCES bp_costs(id) ON DELETE CASCADE,
    variant    TEXT NOT NULL,            -- bsp / luk
    source     TEXT NOT NULL,            -- модель / вручную / книга
    source_ref TEXT,                     -- чем именно обосновано
    amount     REAL,                     -- сумма на момент фиксации источника
    note       TEXT,                     -- обоснование ручного отклонения
    author     TEXT,
    set_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (cost_id, variant)
);

-- График вывоза по базам: план отгрузки/перемещения по месяцам
-- (остатки и реализация с потерями считаются, структура листа «ЗС НКТ+ЛОМ»).
CREATE TABLE IF NOT EXISTS bp_schedule (
    id           INTEGER PRIMARY KEY,
    bp_id        INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    base         TEXT NOT NULL,            -- база/дивизион (Поставщик позиций)
    month        INTEGER NOT NULL,         -- 1..срок вывоза
    shipment_t   REAL NOT NULL DEFAULT 0,  -- план отгрузки, тн
    relocation_t REAL NOT NULL DEFAULT 0,  -- перемещение, тн (справочно)
    UNIQUE (bp_id, base, month)
);

-- Файлы-основания БП: перечни/КП продавца, загруженные в систему.
CREATE TABLE IF NOT EXISTS bp_attachments (
    id          INTEGER PRIMARY KEY,
    bp_id       INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    kind        TEXT DEFAULT 'перечень',      -- перечень / КП / фото / прочее
    items_count INTEGER,                      -- сколько позиций распознано
    taken_at    TEXT,                         -- дата съёмки фото (EXIF или вручную)
    note        TEXT,                         -- подпись к фото
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- План-факт (заготовка): фактические данные по месяцам вводятся вручную,
-- позже — автоматическая загрузка реализации из 1С. План берётся из БП
-- (график вывоза × средняя цена).
CREATE TABLE IF NOT EXISTS bp_fact (
    id        INTEGER PRIMARY KEY,
    bp_id     INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    month     INTEGER NOT NULL,             -- 1..срок вывоза
    volume_t  REAL,                         -- факт: объём реализации, тн
    revenue   REAL,                         -- факт: выручка без НДС, руб
    costs     REAL,                         -- факт: прямые затраты (без закупки), руб
    comment   TEXT,
    UNIQUE (bp_id, month)
);

-- Сценарии-варианты БП: каждый сценарий переопределяет параметры сделки
-- (сдвиг цены реализации, порог закупки, доля невозмещённого НДС, ставки)
-- и пересчитывается полным P&L. Источник — сценарные листы файлов
-- экономистов (БП 1674: «лук» / «лук (-1000)» с ценой −1000, НДС 30%).
-- NULL в поле = параметр наследуется от БП.
CREATE TABLE IF NOT EXISTS bp_scenarios (
    id                  INTEGER PRIMARY KEY,
    bp_id               INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    price_delta         REAL,             -- сдвиг цены реализации, руб/тн
    lot_cost            REAL,             -- переопределение стоимости лота
    vat_unrecovered_pct REAL,             -- доля невозм. НДС на затраты, %
    vat_rate            REAL,
    capital_rate        REAL,
    removal_months      REAL,
    contamination_pct   REAL,
    tax_rate            REAL,
    control_net_profit  REAL,             -- ЧП из файла-источника для сверки
    comment             TEXT,
    sort                INTEGER NOT NULL DEFAULT 0
);

-- Переопределения параметров расчёта затрат для конкретного БП
-- (по умолчанию берётся норматив из cost_norms).
CREATE TABLE IF NOT EXISTS bp_cost_params (
    id     INTEGER PRIMARY KEY,
    bp_id  INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    key    TEXT NOT NULL,
    value  REAL NOT NULL,
    UNIQUE (bp_id, key)
);

-- Справочник нормативов затрат: общие (base IS NULL) и по базам.
-- Источник значений — расчёт экономистов (Данные_Тест, колонки AD-CQ).
-- Типы бизнес-планов: тип задаёт логику расчёта — какие статьи затрат
-- характерны, какие процессы переработки ожидаются и чьё участие обязательно.
-- Определяется по составу лота (app/bp_types.py), правится человеком.
CREATE TABLE IF NOT EXISTS ref_bp_types (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,  -- pipe / ferrous / cable / nonferrous / dhno / mixed
    name           TEXT NOT NULL,
    sort           INTEGER NOT NULL DEFAULT 100,
    item_types     TEXT,                  -- типы закупки позиций через «;»
    description    TEXT,
    cost_items     TEXT,                  -- характерные статьи затрат через «;»
    processes      TEXT,                  -- ожидаемые процессы переработки через «;»
    required_roles TEXT,                  -- чьё участие обязательно, через «;»
    fact_groups    TEXT,                  -- группы аналитического учёта 1С этого типа, через «;» (для факта из «Реализации»)
    is_active      INTEGER NOT NULL DEFAULT 1
);

-- Матрица фактических затрат: тип сделки → статья → рублей на тонну.
-- Собирается из расчётов сервиса и выгрузок 1С (app/cost_matrix.py);
-- источники хранятся раздельно, чтобы расхождение «расчёт против факта»
-- было видно, а не растворялось в среднем.
CREATE TABLE IF NOT EXISTS stat_cost_matrix (
    id          INTEGER PRIMARY KEY,
    bp_type     TEXT NOT NULL,       -- код типа сделки; '' — применимо ко всем
    section     TEXT NOT NULL,       -- секция P&L
    item        TEXT NOT NULL,       -- статья затрат
    source      TEXT NOT NULL,       -- «расчёты» / «факт 1С: …»
    samples     INTEGER NOT NULL,    -- сколько наблюдений в медиане
    total_t     REAL,                -- суммарный тоннаж наблюдений
    rub_per_t   REAL,                -- медиана
    p_min       REAL,                -- коридор после отсечения выбросов
    p_max       REAL,
    outliers    INTEGER NOT NULL DEFAULT 0,  -- сколько наблюдений отброшено
    period_min  TEXT,
    period_max  TEXT,
    note        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (bp_type, section, item, source)
);

-- ── Техника, категории груза и нормы погрузки ───────────────────────

-- Парк техники из 1С (выгрузка ТС_*.csv). Грузоподъёмность и объём кузова
-- решают, сколько тонн реально войдёт в рейс: лёгкий объёмный лом упирается
-- в кубатуру задолго до тоннажа, а тяжёлый — наоборот, даёт перевес.
CREATE TABLE IF NOT EXISTS ref_vehicles (
    id            INTEGER PRIMARY KEY,
    guid          TEXT UNIQUE,
    name          TEXT NOT NULL,          -- как машина названа в 1С
    plate         TEXT,                   -- госномер
    brand         TEXT,
    vehicle_type  TEXT,                   -- ТипТС: КМУ шоссейный, тягач…
    ownership     TEXT,                   -- собственное / наёмное
    division      TEXT,
    capacity_t    REAL,                   -- паспортная грузоподъёмность, тн
    volume_m3     REAL,                   -- вместимость кузова, м3
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Фактическая загрузка рейсов по типам техники (из Отвесная_*.csv).
-- Медиана и 95-й процентиль показывают, сколько на самом деле грузят, а
-- доля рейсов сверх паспорта — где систематически идёт перевес.
CREATE TABLE IF NOT EXISTS stat_vehicle_load (
    id            INTEGER PRIMARY KEY,
    vehicle_type  TEXT NOT NULL,
    period_from   TEXT,
    trips         INTEGER NOT NULL,
    median_t      REAL,
    p95_t         REAL,
    max_t         REAL,
    capacity_t    REAL,                   -- паспорт по этому типу (медиана)
    over_pct      REAL,                   -- доля рейсов тяжелее паспорта, %
    dropped       INTEGER NOT NULL DEFAULT 0,  -- отброшено рейсов вне диапазона
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vehicle_type)
);

-- Категории груза и насыпная плотность: сколько тонн приходится на кубометр.
-- В 1С этих данных нет — значения заводятся человеком и правятся в
-- справочнике; источник каждого значения указывается явно.
CREATE TABLE IF NOT EXISTS ref_cargo_categories (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    match_words   TEXT,                   -- по каким словам узнаём в перечне
    density_t_m3  REAL,                   -- насыпная плотность, тн/м3
    source        TEXT,                   -- откуда значение
    comment       TEXT
);

-- Состояние справочников: когда обновлялся, откуда, кто отвечает.
-- Без этой карточки расчёт молча опирается на прошлогодние цифры, а
-- спросить «свежие ли данные» не у кого. Заполняется импортёрами
-- (app/refsources.py) и вручную в разделе «Справочники».
CREATE TABLE IF NOT EXISTS ref_sources (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,   -- имя справочника (таблицы)
    title       TEXT NOT NULL,
    origin      TEXT,                   -- выгрузка 1С / вручную / считается
    source_file TEXT,                   -- файл последней загрузки
    rows        INTEGER,                -- строк на момент обновления
    updated_at  TEXT,
    author      TEXT,                   -- кто обновил
    owner       TEXT,                   -- кто отвечает за справочник
    period_days INTEGER,                -- ожидаемая периодичность, дней
    comment     TEXT
);

CREATE TABLE IF NOT EXISTS cost_norms (
    id      INTEGER PRIMARY KEY,
    base    TEXT,                         -- NULL = общий норматив; иначе имя базы
    key     TEXT NOT NULL,
    value   REAL NOT NULL,
    label   TEXT,
    unit    TEXT,
    UNIQUE (base, key)
);

-- ── Граф маршрута сделки ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bp_nodes (
    id       INTEGER PRIMARY KEY,
    bp_id    INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL CHECK (kind IN
             ('seller', 'base', 'workshop', 'production', 'custody', 'buyer')),
             -- продавец / база / цех / производство / ответхранение / покупатель
    label    TEXT NOT NULL,
    ref_kind TEXT,                        -- counterparty / warehouse / NULL
    ref_id   INTEGER,
    x        REAL,
    y        REAL
);

CREATE TABLE IF NOT EXISTS bp_edges (
    id        INTEGER PRIMARY KEY,
    bp_id     INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    from_node INTEGER NOT NULL REFERENCES bp_nodes(id) ON DELETE CASCADE,
    to_node   INTEGER NOT NULL REFERENCES bp_nodes(id) ON DELETE CASCADE,
    transport TEXT,                       -- вид транспорта
    flow_group TEXT,                      -- поток: категория номенклатуры (чёрный лом,
                                          -- цветмет и т.д.); NULL = все потоки
    volume_t  REAL,
    comment   TEXT
);

-- Процессы на узлах: переработка со сменой номенклатуры (труба → лом и т.п.)
CREATE TABLE IF NOT EXISTS bp_processes (
    id           INTEGER PRIMARY KEY,
    bp_id        INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    node_id      INTEGER NOT NULL REFERENCES bp_nodes(id) ON DELETE CASCADE,
    work_type    TEXT NOT NULL,           -- вид работ (справочник 1С)
    input_nomen  TEXT,                    -- входная номенклатура
    output_nomen TEXT,                    -- выходная номенклатура
    volume_t     REAL,
    comment      TEXT
);

-- ── Риски, согласование, версии, аудит ──────────────────────────────

CREATE TABLE IF NOT EXISTS bp_risks (
    id          INTEGER PRIMARY KEY,
    bp_id       INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    risk_type   TEXT NOT NULL,
    description TEXT,
    probability TEXT CHECK (probability IN ('В', 'С', 'Н')),
    impact      TEXT CHECK (impact IN ('К', 'З', 'Н')),
    mitigation  TEXT
);

CREATE TABLE IF NOT EXISTS bp_approvals (
    id         INTEGER PRIMARY KEY,
    bp_id      INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    user_name  TEXT,
    action     TEXT NOT NULL,
    comment    TEXT,
    decided_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Версии расчёта: снимок БП целиком (оба варианта). Пишется автоматически
-- при каждом изменении данных (дедупликация по snapshot_hash) и вручную при
-- смене статуса. В реестре разворачивается списком под строкой БП.
CREATE TABLE IF NOT EXISTS bp_versions (
    id            INTEGER PRIMARY KEY,
    bp_id         INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    author        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    variant       TEXT,                 -- вариант, открытый в момент правки (bsp/luk)
    scenario      TEXT,                 -- сценарий из карточки БП
    net_profit_bsp REAL,                -- ЧП вариантов на момент снимка —
    net_profit_luk REAL,                -- показываются в списке версий
    change_note   TEXT,                 -- что вызвало версию (раздел/действие)
    snapshot_hash TEXT,                 -- дедупликация: правка без изменений не пишется
    approved_at   TEXT,                 -- версия утверждена (одна на БП):
    approved_by   TEXT                  -- по ней формируется печатная форма
);

-- Отмена массового действия по позициям: перед тем как задать цену (группу,
-- покупателя) сразу сотне строк, сервис запоминает ПРЕЖНИЕ значения именно
-- этих строк. В книге экономист отменяет протяжку формулы одним Ctrl+Z, и
-- без такой же отмены массовая правка в сервисе — действие без возврата:
-- восстанавливать из версии пришлось бы весь БП целиком.
CREATE TABLE IF NOT EXISTS bp_bulk_undo (
    id         INTEGER PRIMARY KEY,
    bp_id      INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    author     TEXT,                   -- кто сделал массовую правку
    action     TEXT NOT NULL,          -- что это было: bulk / aggregate
    note       TEXT,                   -- строка для кнопки отмены
    payload    TEXT NOT NULL           -- [{id, колонка: прежнее значение}, …]
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    user_name TEXT,
    bp_id     INTEGER,
    action    TEXT NOT NULL,
    details   TEXT
);

CREATE INDEX IF NOT EXISTS idx_bp_items_bp     ON bp_items(bp_id);
CREATE INDEX IF NOT EXISTS idx_bp_costs_bp     ON bp_costs(bp_id);
CREATE INDEX IF NOT EXISTS idx_bp_nodes_bp     ON bp_nodes(bp_id);
CREATE INDEX IF NOT EXISTS idx_bp_edges_bp     ON bp_edges(bp_id);
CREATE INDEX IF NOT EXISTS idx_bp_risks_bp     ON bp_risks(bp_id);
CREATE INDEX IF NOT EXISTS idx_bp_versions_bp  ON bp_versions(bp_id);
CREATE INDEX IF NOT EXISTS idx_audit_bp        ON audit_log(bp_id);
CREATE INDEX IF NOT EXISTS idx_bp_status       ON business_plans(status);
CREATE INDEX IF NOT EXISTS idx_ref_wh_top      ON ref_warehouses(top_parent);

-- ── Импорт CSV-выгрузок 1С (import_1c_csv.py) ───────────────────────
-- Справочники и агрегированная статистика для автоподсказок в БП.
-- Справочники обновляются upsert-ом по GUID; stat_* пересоздаются
-- целиком при каждом импорте (DELETE + INSERT).

-- Серии номенклатуры (проекты/спецификации закупленных лотов).
CREATE TABLE IF NOT EXISTS ref_series (
    guid              TEXT PRIMARY KEY,   -- СерияГуид (upper-case)
    name              TEXT NOT NULL,      -- Серия
    project           TEXT,               -- Проект
    project_guid      TEXT,
    counterparty      TEXT,               -- Контрагент
    counterparty_guid TEXT,
    removal_date      TEXT,               -- ДатаВывоза (ISO, NULL если 0001-01-01)
    is_additional     INTEGER NOT NULL DEFAULT 0,  -- Дополнительная
    is_paid           INTEGER NOT NULL DEFAULT 0,  -- Плтаная (платная)
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Дополнение к ref_nomenclature_1c: единицы измерения и группа грузов.
CREATE TABLE IF NOT EXISTS ref_nomen_1c_ext (
    guid             TEXT PRIMARY KEY,    -- НоменклатураГуид (upper-case)
    unit             TEXT,                -- ЕдиницаИзмерения
    unit_coef        REAL,                -- КоэффициентПересчета
    cargo_group      TEXT,                -- НоменклатурнаяГруппаГрузов
    cargo_group_guid TEXT,
    report_name      TEXT,                -- НоменклатураОтчета
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Статьи расходов из СтатьиРасходов_*.csv (богаче ref_expense_items).
CREATE TABLE IF NOT EXISTS ref_expense_items_1c (
    guid               TEXT PRIMARY KEY,  -- СсылкаГуид (upper-case)
    name               TEXT NOT NULL,     -- Ссылка
    parent             TEXT,              -- Родитель
    parent_guid        TEXT,
    distribution_var   TEXT,              -- ВариантРаспределенияРасходовУпр
    analytics_type     TEXT,              -- ТипАналитики
    production_process TEXT,              -- ПроизводственныйПроцесс
    cash_flow_item     TEXT,              -- СтатьяДДС
    account            TEXT,              -- счёт из имени: 23/25/26/91
    account_1c         TEXT,              -- СчетУчета (как в 1С)
    excluded           INTEGER NOT NULL DEFAULT 0,  -- ИсключатьИзОтчетов
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Производственные подразделения (цеха, участки, базы).
CREATE TABLE IF NOT EXISTS ref_prod_divisions (
    guid                TEXT PRIMARY KEY, -- СсылкаГуид (upper-case)
    name                TEXT NOT NULL,    -- Наименование
    code                TEXT,             -- Код
    parent              TEXT,             -- Родитель
    functional_division TEXT,             -- ФункциональноеПодразделение
    analytic_division   TEXT,             -- АналитическоеПодразделение
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Засор и недовоз по местам погрузки (из Отвесная_*.csv).
CREATE TABLE IF NOT EXISTS stat_contamination (
    id                    INTEGER PRIMARY KEY,
    loading_place         TEXT NOT NULL UNIQUE,  -- МестоПогрузки
    samples               INTEGER NOT NULL,      -- рейсов с валидным засором
    avg_contamination_pct REAL,   -- средний засор, % (0-30, выбросы отсечены)
    avg_shortage_pct      REAL,   -- средний недовоз к весу ТТН, %
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Тарифы перевозки по видам доставки (из Отвесная_*.csv).
CREATE TABLE IF NOT EXISTS stat_transport (
    id            INTEGER PRIMARY KEY,
    delivery_kind TEXT NOT NULL UNIQUE,  -- ВидДоставки
    trips_total   INTEGER NOT NULL,      -- всего рейсов с весом > 0
    samples       INTEGER NOT NULL,      -- рейсов со стоимостью > 0 (база тарифа)
    total_t       REAL,                  -- суммарный вес по ТТН, т (все рейсы)
    total_cost    REAL,                  -- суммарная стоимость, руб
    total_km      REAL,                  -- километраж рейсов со стоимостью, км
    rub_per_t     REAL,   -- стоимость / вес рейсов со стоимостью, руб/т
    rub_per_tkm   REAL,   -- руб/т·км (по рейсам с км>0, выбросы отсечены)
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ставки переработки (из _Производственная_себестоимость__*.csv).
-- rate_* = Сумма<статья> / Количество: руб на единицу выпуска
-- (т для лома; кабель/комплектующие могут учитываться в кг/м/шт).
CREATE TABLE IF NOT EXISTS stat_processing (
    id          INTEGER PRIMARY KEY,
    work_type   TEXT NOT NULL,           -- ВидРабот
    division    TEXT,                    -- Подразделение
    cargo_group TEXT,                    -- Группа
    samples     INTEGER NOT NULL,
    total_qty   REAL,                    -- суммарный выпуск (ед. учёта)
    rate_fot    REAL,                    -- ФОТ, руб/ед
    rate_prr    REAL,                    -- ПРР, руб/ед
    rate_gsm    REAL,                    -- ГСМ, руб/ед
    rate_amort  REAL,                    -- амортизация, руб/ед
    rate_mat    REAL,                    -- материалы (кислород, пропан, бензин), руб/ед
    labor_per_t REAL,                    -- трудозатраты, ч/ед
    unit        TEXT,                    -- единица выпуска из справочника 1С
                                         -- (т/шт/кг/м): без неё ставки разных
                                         -- единиц складывались бы в одну кучу
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (work_type, division, cargo_group, unit)
);

-- Закупочные цены по контрагентам и группам (из Закупки_*.csv, ЕдИзм = т).
CREATE TABLE IF NOT EXISTS stat_purchase_price (
    id           INTEGER PRIMARY KEY,
    counterparty TEXT NOT NULL,          -- Контрагент
    nomen_group  TEXT,                   -- группа аналитического учёта
    samples      INTEGER NOT NULL,
    total_qty_t  REAL,                   -- объём с учётом возвратов/сторно, т
    total_sum    REAL,                   -- сумма без НДС, руб
    rub_per_t    REAL,
    period_min   TEXT,                   -- первая дата закупки (ISO)
    period_max   TEXT,                   -- последняя дата закупки (ISO)
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (counterparty, nomen_group)
);

-- Цены реализации по группам и дивизионам (из _Выручка_на_загрузку__*.csv).
CREATE TABLE IF NOT EXISTS stat_sale_price (
    id            INTEGER PRIMARY KEY,
    cargo_group   TEXT NOT NULL,         -- ГруппаАналитическогоУчета
    division      TEXT,                  -- Дивизион
    samples       INTEGER NOT NULL,
    total_qty_t   REAL,                  -- факт продаж, т
    total_revenue REAL,                  -- фактическая выручка, руб
    rub_per_t     REAL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (cargo_group, division)
);

-- История фактических продаж по месяцам, покупателям и направлениям.
-- Средняя цена «по типу груза» ничего не значит: в выгрузке 1С труба НКТ
-- уходит Северскому трубному по 18 610, а трубы больших диаметров — по
-- 32 897 руб/тн. Цена определяется связкой «группа × дивизион × покупатель»
-- и меняется во времени, поэтому храним разрез по месяцам: по нему строятся
-- ориентир (свежие сделки весомее) и тренд для прогноза.
CREATE TABLE IF NOT EXISTS stat_sale_price_hist (
    id            INTEGER PRIMARY KEY,
    cargo_group   TEXT NOT NULL,         -- ГруппаАналитическогоУчета
    division      TEXT,                  -- Дивизион
    buyer         TEXT,                  -- Покупатель (канал сбыта)
    period        TEXT NOT NULL,         -- месяц продажи, YYYY-MM
    samples       INTEGER NOT NULL,
    total_qty_t   REAL,
    total_revenue REAL,
    rub_per_t     REAL,
    UNIQUE (cargo_group, division, buyer, period)
);

CREATE INDEX IF NOT EXISTS idx_stat_purchase_group ON stat_purchase_price(nomen_group);
CREATE INDEX IF NOT EXISTS idx_stat_sale_group     ON stat_sale_price(cargo_group);
CREATE INDEX IF NOT EXISTS idx_stat_sale_hist      ON stat_sale_price_hist(cargo_group, period);
CREATE INDEX IF NOT EXISTS idx_stat_sale_hist_buyer ON stat_sale_price_hist(cargo_group, buyer);

-- Распределяемые (косвенные) расходы из 1С: детализация котловых сумм по
-- подразделениям, месяцам и статьям — обоснование ставки руб/тн вместо
-- «магического» норматива (лист «распределяемые» книг экономистов).
CREATE TABLE IF NOT EXISTS stat_overheads (
    id           INTEGER PRIMARY KEY,
    period       TEXT NOT NULL,          -- месяц (YYYY-MM)
    division     TEXT,                   -- подразделение (База Осенцы, Пермь...)
    item         TEXT,                   -- статья расходов (из СтатьиРасходов)
    account      TEXT,                   -- счёт учёта: 23 / 25 / 26 / 91
    amount       REAL NOT NULL,          -- сумма с учётом сторно, руб
    samples      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (period, division, item)
);
CREATE INDEX IF NOT EXISTS idx_stat_overheads_div ON stat_overheads(division);

-- Реестр сделок Битрикса (DEAL_*.xlsx, та же выгрузка, что у «Реализации»):
-- доля лота, партнёр, юрлицо-победитель, стадия. Ключ — «№ в текущем реестре»
-- (номер запроса: 1570, 1865 …), по нему сделка привязывается к БП.
CREATE TABLE IF NOT EXISTS ref_deals (
    deal_no       TEXT PRIMARY KEY,
    name          TEXT,
    kind          TEXT,           -- «Тип» сделки (Лукойл, …)
    stage         TEXT,
    share_pct     REAL,           -- доля Металлолом, %
    share_doubt   INTEGER NOT NULL DEFAULT 0,   -- в реестре стоял «?»
    partner_pct   REAL,           -- доля УВМ, %
    winner        TEXT,
    lot_t         REAL,           -- объём лота из запроса, тн
    contract_t    REAL,
    owner         TEXT,           -- ответственный в Битриксе
    created_at    TEXT,
    source_file   TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Снимок факта реализации по сделке из сервиса «Реализация» (out/sales_data.json,
-- пересобирается ночью из регистров 1С): куплено/продано, выручка и
-- себестоимость продаж, разрез по номенклатуре, плановая выручка книги
-- экономиста (треки «лук»/«дсп»). Снимки копятся по дате сборки —
-- видно, как факт догоняет план. Ключ привязки — номер запроса (deal_no).
CREATE TABLE IF NOT EXISTS bp_fact_snapshot (
    id            INTEGER PRIMARY KEY,
    bp_id         INTEGER NOT NULL REFERENCES business_plans(id) ON DELETE CASCADE,
    deal_no       TEXT NOT NULL,
    generated_at  TEXT NOT NULL,        -- meta.generated сборки «Реализации»
    dump_dt       TEXT,                 -- дата выгрузки регистров 1С
    series_name   TEXT,                 -- головная серия (проект) в 1С
    bought_t      REAL,                 -- куплено, тн
    sold_t        REAL,                 -- продано, тн
    revenue       REAL,                 -- выручка без НДС, руб
    cost_of_sales REAL,                 -- себестоимость продаж (закупка + переработка), руб
    share_pct     REAL,                 -- доля лота по реестру сделок на момент сборки
    plan_rev_luk  REAL,                 -- план выручки книги, трек «лук» (весь лот)
    plan_rev_dsp  REAL,
    plan_t_luk    REAL,
    plan_t_dsp    REAL,
    plan_ver_luk  TEXT,
    plan_ver_dsp  TEXT,
    items_json    TEXT,                 -- [{code, name, bought_t, sold_t, revenue, warehouses}]
    source_file   TEXT,
    taken_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (bp_id, generated_at)
);

-- Фактическая рентабельность по типам сделок — из ночной сборки «Реализации»
-- (все ~1 000 БП компании, не только наши): валовая маржа продаж
-- (выручка − себестоимость продаж) / выручка по закрытым сделкам (продано не
-- меньше type_margin_closed_pct покупки), тип — по доминирующей группе
-- аналитического учёта (ref_bp_types.fact_groups, порог bp_type_threshold_pct).
-- Квартили, не среднее. Строка bp_type = '' — по всем сделкам.
CREATE TABLE IF NOT EXISTS stat_type_margin (
    bp_type       TEXT PRIMARY KEY,
    n             INTEGER NOT NULL,
    p25           REAL,
    median        REAL,
    p75           REAL,
    revenue       REAL,                 -- суммарная выручка сделок выборки
    closed_pct    REAL,                 -- порог «закрытости», %
    generated_at  TEXT,                 -- дата сборки «Реализации»
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Фактические затраты по сделкам из регистра 1С «Прочие расход (все)»
-- (Extractor): только строки с серией (= сделкой) с cost_matrix_from.
-- Статья 1С сложена в статью сервиса по справочнику ref_cost_item_map.
-- Тоннаж и тип сделки — из снимка «Реализации» по имени серии.
CREATE TABLE IF NOT EXISTS stat_fact_costs (
    id            INTEGER PRIMARY KEY,
    series        TEXT NOT NULL,        -- серия 1С (имя, как в регистре)
    deal_no       TEXT,                 -- номер запроса (первые цифры серии)
    bp_type       TEXT,                 -- тип сделки по группам снимка; NULL — не определён
    direction     TEXT,                 -- направление (dir снимка)
    item_1c       TEXT NOT NULL,        -- статья 1С
    section       TEXT,                 -- секция P&L сервиса
    item          TEXT,                 -- статья сервиса; NULL — не сопоставлена
    amount        REAL NOT NULL,        -- руб без НДС
    rows_n        INTEGER NOT NULL,     -- строк регистра
    period_min    TEXT,
    period_max    TEXT,
    bought_t      REAL,                 -- куплено по серии (снимок), тн
    sold_t        REAL,                 -- продано по серии, тн
    closed        INTEGER NOT NULL DEFAULT 0,  -- продано >= type_margin_closed_pct
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (series, item_1c)
);

-- Соответствие статей 1С статьям сервиса (правится на «Справочниках»).
-- section/item NULL = статья не относится к сделке (дивиденды, проценты).
CREATE TABLE IF NOT EXISTS ref_cost_item_map (
    item_1c   TEXT PRIMARY KEY,
    section   TEXT,
    item      TEXT,
    note      TEXT,
    is_fact_only INTEGER NOT NULL DEFAULT 0,   -- статьи, которых нет в плане (засор деньгами)
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Калибровка нормативов фактом (15.09.2026): у норматива появляется
-- фактическое значение из регистров 1С с датой и источником. Норматив
-- не перезаписывается — экономист видит расхождение и решает сам.
CREATE TABLE IF NOT EXISTS norm_fact (
    key         TEXT PRIMARY KEY,        -- ключ cost_norms.key
    fact_value  REAL NOT NULL,
    unit        TEXT,
    samples     INTEGER,                 -- наблюдений (строк / сделок / документов)
    basis       TEXT,                    -- как посчитано, словами
    source      TEXT,                    -- таблица-источник
    period_from TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Распределяемые расходы по подразделениям из регистра 1С «Прочие расход
-- (все)»: строки БЕЗ серии (ФОТ, амортизация, аренда, охрана, ремонт …) по
-- подразделению и месяцу. Из них и тоннажа базы (снимок «Реализации»)
-- считается фактическая ставка руб/т базы — калибровка base_overhead_per_t.
CREATE TABLE IF NOT EXISTS stat_overhead_div (
    id          INTEGER PRIMARY KEY,
    division    TEXT NOT NULL,          -- подразделение 1С
    month       TEXT NOT NULL,          -- ГГГГ-ММ
    item_1c     TEXT NOT NULL,
    amount      REAL NOT NULL,          -- руб без НДС
    rows_n      INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (division, month, item_1c)
);

-- Тоннаж через подразделение по месяцам из «Отвесной» (вес по ТТН) —
-- знаменатель для ставки распределяемых по подразделению: те же имена,
-- что в регистре затрат.
CREATE TABLE IF NOT EXISTS stat_division_tons (
    division    TEXT NOT NULL,
    month       TEXT NOT NULL,
    tons        REAL NOT NULL,
    trips       INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (division, month)
);

-- Регистр затрат по сериям в разрезе подразделений — чтобы определить
-- площадку сделки по факту (где по серии больше всего затрат).
CREATE TABLE IF NOT EXISTS stat_fact_costs_div (
    series      TEXT NOT NULL,
    deal_no     TEXT,
    division    TEXT NOT NULL,
    amount      REAL NOT NULL,
    rows_n      INTEGER NOT NULL,
    PRIMARY KEY (series, division)
);

-- Архив книг экономистов из Битрикса (парсер metoptorg-bp-weekly,
-- БП_версии_*.json): по каждой версии книги — выручка, закупка (Σ позиций),
-- прибыль и затраты как остаток. «Что закладывали» по всей истории.
CREATE TABLE IF NOT EXISTS stat_book_plan (
    id          INTEGER PRIMARY KEY,
    deal_no     TEXT NOT NULL,
    bp_type     TEXT,
    version     TEXT,
    file        TEXT,
    year        TEXT,
    revenue     REAL NOT NULL,
    purchase    REAL NOT NULL,
    profit      REAL NOT NULL,
    costs       REAL NOT NULL,          -- выручка − закупка − прибыль
    volume_t    REAL NOT NULL,
    costs_per_t REAL,
    costs_pct   REAL,                   -- затраты / выручка, %
    gross_pct   REAL,                   -- (выручка − закупка) / выручка, %
    luk         INTEGER NOT NULL DEFAULT 0,
    dsp         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (deal_no, file, version)
);

-- Сводка «что закладывали» по типам сделок (медианы по последним версиям).
CREATE TABLE IF NOT EXISTS stat_type_plan (
    bp_type      TEXT PRIMARY KEY,     -- '' — все сделки
    n            INTEGER NOT NULL,
    costs_per_t  REAL,
    costs_pct    REAL,
    gross_pct    REAL,
    profit_pct   REAL,
    generated_at TEXT
);

-- Факт продаж по НОМЕНКЛАТУРЕ 1С (регистр продаж, последние 24 месяца):
-- ориентир цены для позиции точнее группы — в «цветном ломе» медь и алюминий
-- различаются вдвое (КП 1956).
CREATE TABLE IF NOT EXISTS stat_sale_price_nomen (
    nomen_norm    TEXT NOT NULL,         -- нормализованное имя номенклатуры 1С
    nomen         TEXT NOT NULL,
    cargo_group   TEXT,
    period        TEXT NOT NULL,         -- ГГГГ-ММ
    samples       INTEGER NOT NULL,
    total_qty_t   REAL,
    total_revenue REAL,
    rub_per_t     REAL,
    PRIMARY KEY (nomen_norm, period)
);

-- Площадки компании (аналитические базы из «Цеха и базы» 1С) и слова
-- адресов, по которым новая сделка без серии привязывается к площадке
-- (Чернушка, Оса → Пермь; Усинск, Печора → Усинск). Правится на «Справочниках».
CREATE TABLE IF NOT EXISTS ref_sites (
    site        TEXT PRIMARY KEY,
    match_words TEXT,                 -- через «;», регистр не важен
    is_active   INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Обучающая выборка независимой модели результата (app/outcome.py):
-- закрытые сделки компании с фактом — тип, площадка, тоннаж, выручка,
-- себестоимость продаж, затраты по серии и удельные величины.
CREATE TABLE IF NOT EXISTS stat_outcome_train (
    deal_no       TEXT PRIMARY KEY,
    bp_type       TEXT,
    site          TEXT,
    bought_t      REAL,
    sold_t        REAL,
    revenue       REAL,
    cost_of_sales REAL,
    series_costs  REAL,
    price_per_t   REAL,
    gross_pct     REAL,
    costs_per_t   REAL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Сверка книг экономистов с фактом 1С по каждой сделке: книга (версия,
-- отправленная заказчику), факт (снимок «Реализации» + регистр затрат +
-- распределяемые площадки) и модель сервиса от факта; ошибки против факта.
CREATE TABLE IF NOT EXISTS stat_deal_audit (
    deal_no           TEXT PRIMARY KEY,
    name              TEXT,
    kind              TEXT,
    bp_type           TEXT,
    site              TEXT,
    year              TEXT,
    closed            INTEGER NOT NULL DEFAULT 0,
    tons_ok           INTEGER,             -- тоннаж книги к купленному в допуске (доля лота)
    bought_t          REAL,
    sold_t            REAL,
    fact_rev          REAL,
    fact_cos          REAL,                -- себестоимость продаж 1С
    fact_price        REAL,                -- руб/т проданного
    fact_gross_pct    REAL,
    fact_series       REAL,                -- затраты по серии (регистр, с 2025 г.)
    fact_series_pt    REAL,
    series_items      INTEGER,             -- статей по серии в регистре
    series_full       INTEGER NOT NULL DEFAULT 0, -- >= 3 статей: факт затрат полный
    fact_overhead     REAL,                -- ставка площадки × продано
    fact_costs_pt     REAL,
    fact_profit       REAL,                -- выручка − себестоимость − серия − распределяемые
    plan_version      TEXT,
    plan_file         TEXT,
    plan_t            REAL,
    plan_rev          REAL,
    plan_purchase     REAL,
    plan_costs        REAL,
    plan_profit       REAL,
    plan_price        REAL,
    plan_costs_pt     REAL,
    plan_margin_pct   REAL,
    model_level       TEXT,                -- уровень соседей модели
    model_n           INTEGER,
    model_price       REAL,
    model_costs_pt    REAL,
    model_overhead_pt REAL,
    model_rev         REAL,
    model_costs       REAL,
    model_profit      REAL,
    plan_price_err    REAL,                -- (план − факт) / факт, %
    model_price_err   REAL,
    plan_costs_err    REAL,
    model_costs_err   REAL,                -- только по серии: распределяемые в модели и факте одинаковы
    plan_profit_err   REAL,                -- рентабельность план − факт, п.п.
    model_profit_err  REAL,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
