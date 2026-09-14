"""Радар — система раннего предупреждения.

7 рыночных индикаторов (из v1 check_alarms) + внутренние по данным 1С.
Каждая проверка честно указывает, откуда значение и когда оно получено;
если данных нет — уровень 'na', а не выдуманное «всё хорошо».
"""
from __future__ import annotations
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RadarCheck
from app.services.market import latest_quote


def _mk(n, name, level, value_text, message):
    return {"indicator_n": n, "name": name, "level": level,
            "value_text": value_text, "message": message}


def run_radar(db: Session) -> list[dict]:
    checks: list[dict] = []

    # 1. Прайс ММК ВторМет — индикатор №1 «тонкого рынка»
    q = latest_quote(db, "plant_MMK", basis="CPT_AUTO") or latest_quote(db, "mmk_vtormet")
    if q:
        age_d = (datetime.utcnow() - q.collected_at).days
        if age_d > 7:
            checks.append(_mk(1, "Прайс ММК ВторМет", "warn", f"{q.value:,.0f} ₽/т ({age_d} дн назад)",
                              "Прайс устарел — проверить, не приостановил ли ММК закупку"))
        else:
            checks.append(_mk(1, "Прайс ММК ВторМет", "ok", f"{q.value:,.0f} ₽/т",
                              "ММК в рынке. Уход ММК = падение до −13%"))
    else:
        checks.append(_mk(1, "Прайс ММК ВторМет", "na", "нет данных",
                          "Скрапер mmk-vtormet.ru ещё не дал цену"))

    # 2. Арматура А500С — разворот вниз 2 недели = лом через 4-8 недель
    q = latest_quote(db, "armatura_a500")
    if q:
        checks.append(_mk(2, "Арматура А500С", "ok", f"{q.value:,.0f} ₽/т",
                          "Следим за разворотом (лаг до лома 4–8 недель)"))
    else:
        checks.append(_mk(2, "Арматура А500С", "na", "нет данных",
                          "Источник цены арматуры не подключён"))

    # 3. HMS 80/20 CFR Турция < $350
    q = latest_quote(db, "hms_turkey")
    if q:
        lvl = "alarm" if q.value < 350 else ("warn" if q.value < 370 else "ok")
        checks.append(_mk(3, "HMS 80/20 CFR Турция", lvl, f"${q.value:,.0f}/т",
                          "Ниже $350 — экспортный якорь опустился" if lvl != "ok"
                          else "Экспортный паритет поддерживает цену"))
    else:
        checks.append(_mk(3, "HMS 80/20 CFR Турция", "na", "нет данных", ""))

    # 4. USD/RUB < 80
    q = latest_quote(db, "usd_rub")
    if q:
        lvl = "alarm" if q.value < 80 else ("warn" if q.value < 82 else "ok")
        checks.append(_mk(4, "USD/RUB", lvl, f"{q.value:.2f}",
                          "Крепкий рубль запирает лом внутри РФ — давление вниз"
                          if lvl != "ok" else "Курс не давит на рынок"))
    else:
        checks.append(_mk(4, "USD/RUB", "na", "нет данных", "ЦБ РФ недоступен"))

    # 5. Спред FOB ЧМ / CFR Турция < $50 — экспорт оживает (бычий сигнал)
    fob, cfr = latest_quote(db, "fob_black_sea_usd"), latest_quote(db, "hms_turkey")
    if fob and cfr:
        spread = cfr.value - fob.value
        lvl = "warn" if spread < 50 else "ok"
        checks.append(_mk(5, "Спред FOB ЧМ / CFR Турция", lvl, f"${spread:,.0f}",
                          "Спред сузился — экспорт оживает, поддержка цене" if lvl == "warn"
                          else "Экспорт заперт — внутренний рынок сам по себе"))
    else:
        checks.append(_mk(5, "Спред FOB/CFR", "na", "нет данных", ""))

    # 6. Аренда полувагона > 900 ₽/сут
    q = latest_quote(db, "wagon_rate")
    if q:
        lvl = "alarm" if q.value > 900 else "ok"
        checks.append(_mk(6, "Аренда полувагона", lvl, f"{q.value:,.0f} ₽/сут",
                          "Логистика дорожает" if lvl == "alarm"
                          else "Ж/д аномально дешева — окно для ж/д отгрузок"))
    else:
        checks.append(_mk(6, "Аренда полувагона", "na", "нет данных",
                          "Вносится вручную (ИПЕМ) на странице обзвона"))

    # 7. Ключевая ставка ЦБ <= 13% — разворот стройки, бычий сигнал
    q = latest_quote(db, "key_rate")
    if q:
        lvl = "warn" if q.value <= 13 else "ok"
        checks.append(_mk(7, "Ключевая ставка ЦБ", lvl, f"{q.value:.1f}%",
                          "Ставка ≤13% — разворот стройки, бычий сигнал лому"
                          if lvl == "warn" else "Ставка душит стройку и оборотку"))
    else:
        checks.append(_mk(7, "Ключевая ставка ЦБ", "na", "нет данных", ""))

    # ── Внутренние индикаторы (1С) ──
    try:
        from app.services.company import actual_auto_rate, expensive_trips
        exp = expensive_trips(db, days=60, limit=5)
        if exp:
            checks.append(_mk(8, "Дорогие рейсы (60 дн)", "warn",
                              f"{len(exp)}+ рейсов дороже 75-го перцентиля",
                              f"Худший: {exp[0]['from']} → {exp[0]['to']}, "
                              f"{exp[0]['rate_per_100km']} ₽/т/100км"))
        else:
            checks.append(_mk(8, "Дорогие рейсы (60 дн)", "ok", "аномалий нет", ""))
    except Exception:
        checks.append(_mk(8, "Дорогие рейсы", "na", "нет данных 1С", ""))

    return checks


def save_radar(db: Session, checks: list[dict]) -> None:
    for c in checks:
        db.add(RadarCheck(**c))
