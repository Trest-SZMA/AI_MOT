"""Справочник рынка лома (перенос из v1 knowledge_base.py, без изменений данных).

Источник: Rusmet №26/2025 + закладки Павла. Цены здесь — СПРАВОЧНЫЕ ориентиры
(quality=cache); живые цены приходят из обзвона и скраперов в market_quotes.
"""

TARGET_REGIONS = {
    "PERM":       {"name": "Пермский край",            "region_rusmet": "УРАЛ"},
    "KOMI_PERM":  {"name": "Коми-Пермяцкий округ",     "region_rusmet": "УРАЛ"},
    "KOMI_NORTH": {"name": "Коми (Усинск/Ухта)",       "region_rusmet": "СЗФО"},
    "HMAO":       {"name": "ХМАО",                     "region_rusmet": "УРАЛ"},
    "SOUTH":      {"name": "Юг (Волгоградская обл.)",  "region_rusmet": "ЮГ"},
}

# Заводы-покупатели лома. base-цены = Rusmet 06/2025 (справочник, НЕ live)
STEEL_PLANTS = [
    {"id": "MMK",           "name": "ММК",           "city": "Магнитогорск", "region": "УРАЛ",
     "region_target": ["PERM", "KOMI_PERM", "HMAO"], "transport": ["ЖД", "АВТО"],
     "price_fca": 17500, "price_cpt_auto": 20200, "price_cpt_rd": 19200,
     "price_page": "https://mmk.ru/for-supplier/scrap/",
     "notes": "Крупнейший потребитель лома на Урале; индикатор №1 — прайс mmk-vtormet.ru"},
    {"id": "PNTZ",          "name": "ПНТЗ",          "city": "Первоуральск", "region": "УРАЛ",
     "region_target": ["PERM", "KOMI_PERM", "HMAO"], "transport": ["ЖД", "АВТО"],
     "price_fca": 16500, "price_cpt_auto": 19000, "price_cpt_rd": 18200,
     "price_page": "", "notes": "Цех №1 остановлен 14.02.2026 (−320 тыс т/год)"},
    {"id": "SEVERSTAL",     "name": "Северсталь",    "city": "Череповец",    "region": "ЦЕНТР",
     "region_target": ["PERM", "KOMI_PERM"],         "transport": ["ЖД"],
     "price_fca": 17000, "price_cpt_auto": 19500, "price_cpt_rd": 18800,
     "price_page": "https://severstal.com/rus/business/steel/buying-scrap/", "notes": ""},
    {"id": "NLMK",          "name": "НЛМК",          "city": "Липецк",       "region": "ЦЕНТР",
     "region_target": ["PERM"],                      "transport": ["ЖД"],
     "price_fca": 16800, "price_cpt_auto": 19363, "price_cpt_rd": 18500,
     "price_page": "https://nlmk.com/ru/business/purchase/scrap/", "notes": ""},
    {"id": "BMZ",           "name": "БМЗ",           "city": "Жлобин",       "region": "ЦЕНТР",
     "region_target": ["PERM", "KOMI_PERM"],         "transport": ["ЖД"],
     "price_fca": 18500, "price_cpt_auto": 21435, "price_cpt_rd": 20500,
     "price_page": "", "notes": "Экспорт, платит в рублях через агентов"},
    {"id": "PROMSORT_URAL", "name": "ПромСорт-Урал", "city": "Магнитогорск", "region": "УРАЛ",
     "region_target": ["PERM", "KOMI_PERM", "HMAO"], "transport": ["ЖД", "АВТО"],
     "price_fca": 17300, "price_cpt_auto": 20199, "price_cpt_rd": 19500,
     "price_page": "", "notes": "Агент ММК"},
    {"id": "SEVERSTAL_TZ",  "name": "Северский ТЗ",  "city": "Полевской",    "region": "УРАЛ",
     "region_target": ["PERM", "KOMI_PERM", "HMAO"], "transport": ["ЖД", "АВТО"],
     "price_fca": 17000, "price_cpt_auto": 19500, "price_cpt_rd": 18700,
     "price_page": "", "notes": "ТМК; реальный покупатель МетОптТорг (466 млн ₽ за 2024-26)"},
    {"id": "ABINSK_EMZ",    "name": "Абинский ЭМЗ",  "city": "Абинск",       "region": "ЮГ",
     "region_target": ["SOUTH"],                     "transport": ["ЖД", "АВТО"],
     "price_fca": 16200, "price_cpt_auto": 18875, "price_cpt_rd": 18000,
     "price_page": "https://absemz.ru/suppliers/scrap/", "notes": ""},
    {"id": "TAGANROG_MZ",   "name": "Таганрогский МЗ", "city": "Таганрог",   "region": "ЮГ",
     "region_target": ["SOUTH"],                     "transport": ["ЖД", "АВТО"],
     "price_fca": 18000, "price_cpt_auto": 20953, "price_cpt_rd": 20000,
     "price_page": "", "notes": ""},
    {"id": "ROSTOV_MZ",     "name": "Ростовский ЭМЗ", "city": "Ростов-на-Дону", "region": "ЮГ",
     "region_target": ["SOUTH"],                     "transport": ["ЖД", "АВТО"],
     "price_fca": 16800, "price_cpt_auto": 19377, "price_cpt_rd": 18500,
     "price_page": "", "notes": ""},
    {"id": "VOLZHSKY_TZ",   "name": "Волжский ТЗ",   "city": "Волжский",     "region": "ЮГ",
     "region_target": ["SOUTH"],                     "transport": ["ЖД", "АВТО"],
     "price_fca": 18800, "price_cpt_auto": 21666, "price_cpt_rd": 20800,
     "price_page": "", "notes": "Волгоградская область — рядом база Волгоград"},
    {"id": "OMK_STAL",      "name": "ОМК-Сталь",     "city": "Выкса",        "region": "ЦЕНТР",
     "region_target": ["PERM", "KOMI_PERM"],         "transport": ["ЖД"],
     "price_fca": 16700, "price_cpt_auto": 19199, "price_cpt_rd": 18400,
     "price_page": "", "notes": ""},
    {"id": "EVRAZ_ZSMK",    "name": "ЕВРАЗ ЗСМК",    "city": "Новокузнецк",  "region": "СИБИРЬ",
     "region_target": ["HMAO"],                      "transport": ["ЖД"],
     "price_fca": 17500, "price_cpt_auto": 20535, "price_cpt_rd": 19700,
     "price_page": "", "notes": "Ближайший завод для ХМАО с востока"},
    {"id": "UGMK_TUMEN",    "name": "УГМК-Тюмень",   "city": "Тюмень",       "region": "УРАЛ",
     "region_target": ["HMAO", "KOMI_PERM"],         "transport": ["ЖД", "АВТО"],
     "price_fca": 17200, "price_cpt_auto": 19800, "price_cpt_rd": 19000,
     "price_page": "", "notes": "Ближайший к ХМАО"},
]

# Расстояния (км) от целевых регионов до заводов — ориентировочно
DISTANCES = {
    "PERM": {"MMK": 550, "PNTZ": 380, "SEVERSTAL": 1350, "NLMK": 1800, "BMZ": 2100,
             "PROMSORT_URAL": 560, "SEVERSTAL_TZ": 420, "ABINSK_EMZ": 2800,
             "TAGANROG_MZ": 2200, "EVRAZ_ZSMK": 2000, "UGMK_TUMEN": 900,
             "OMK_STAL": 1500, "VOLZHSKY_TZ": 1600},
    "KOMI_PERM": {"MMK": 700, "PNTZ": 500, "SEVERSTAL": 1200, "NLMK": 1950,
                  "PROMSORT_URAL": 710, "SEVERSTAL_TZ": 550, "EVRAZ_ZSMK": 2100,
                  "UGMK_TUMEN": 750, "ABINSK_EMZ": 3000, "BMZ": 2000, "OMK_STAL": 1400},
    "HMAO": {"MMK": 1200, "UGMK_TUMEN": 550, "EVRAZ_ZSMK": 1400, "PROMSORT_URAL": 1200,
             "SEVERSTAL_TZ": 1100, "PNTZ": 1100},
    # Север Коми: расстояния от Усинска (там основной объём), ориентировочно
    "KOMI_NORTH": {"SEVERSTAL": 1500, "MMK": 2200, "PNTZ": 1800, "OMK_STAL": 2000,
                   "UGMK_TUMEN": 1900, "NLMK": 2500, "BMZ": 2700},
    "SOUTH": {"ABINSK_EMZ": 250, "TAGANROG_MZ": 180, "ROSTOV_MZ": 80,
              "VOLZHSKY_TZ": 350, "NLMK": 1100},
}

# Справочные тарифы (fallback, если факта из 1С нет по маршруту)
LOGISTICS = {
    "AUTO_PER_100KM": 200.0,      # ₽/т/100км — медиана факта МетОптТорг: 190
    "RD_PER_100KM": 130.0,
    "RD_WAGON_SURCHARGE": 1000.0,
    "RD_MIN": 800.0,
}

METAL_MULTIPLIER = {"A3_SCRAP": 1.0, "A5_SCRAP": 0.97, "A12_SCRAP": 0.95,
                    "SHRED": 1.09, "CAST": 0.60}

METAL_NAMES = {"A3_SCRAP": "Лом 3А", "A5_SCRAP": "Лом 5А", "A12_SCRAP": "Лом 12А",
               "SHRED": "Шред", "CAST": "Чугун"}
