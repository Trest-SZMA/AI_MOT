"""Факторная модель прогноза (перенос из v1, детерминированная — без ИИ и random).

Модель:
    signal        = sum(вес_i * направление_i) / 100
    monthly_trend = signal * 0.04                  (макс ±4%/мес)
    цена(m)       = base * (1 + sum(trend * 0.85^i))    # затухание
Для чёрного лома поверх — двухфазный сценарий (Астахов/Транслом):
    фаза 1 (до конца сентября): плато, снижение <= 1%
    фаза 2 (октябрь-декабрь):  −1500…−2000 ₽/т от пика
База (`now`) подменяется свежими ценами обзвона из БД, если они есть.
"""
from __future__ import annotations
import json
from datetime import date
from pathlib import Path

from app.config import ROOT

SEED = ROOT / "seed"

BLACK_SERIES = ("perm_cpt", "perm_fca", "hmao", "komi_fca", "black_rf")
# Соответствие ряда → (регион, базис) обзвона
SERIES_SURVEY = {
    "perm_cpt": ("PERM", "CPT_AUTO"),
    "perm_fca": ("PERM", "FCA"),
    "hmao":     ("HMAO", "CPT_AUTO"),
    "komi_fca": ("KOMI_PERM", "FCA"),
}
SERIES_NAMES = {
    "perm_cpt": "Пермь CPT авто", "perm_fca": "Пермь FCA", "hmao": "ХМАО",
    "komi_fca": "Коми FCA", "black_rf": "Лом 3А, среднее РФ",
    "copper_lme": "Медь LME", "copper_m1": "Медь М1 (РФ)",
    "alum_lme": "Алюминий LME", "alum_scrap": "Лом алюминия (РФ)",
}


class ForecastModel:
    def __init__(self, survey_prices: dict | None = None,
                 live_bases: dict | None = None):
        """survey_prices: {(region, basis): (price, date)} — цены обзвона (≤21 дня).
        live_bases: {series: (value, source, date)} — живой рынок (MMI/Транслом)."""
        self.fd = json.loads((SEED / "forecast_data.json").read_text(encoding="utf-8"))
        self.cp = json.loads((SEED / "current_prices.json").read_text(encoding="utf-8"))
        self.survey = survey_prices or {}
        self.live = live_bases or {}

    # ── Сигналы ──────────────────────────────────────────────
    def signal(self, metal: str) -> float:
        key = {"black": "black_factors", "copper": "copper_factors",
               "alum": "alum_factors"}[metal]
        return round(sum(w * d for _, w, d, _ in self.fd[key]) / 100, 3)

    def factors(self, metal: str) -> list:
        key = {"black": "black_factors", "copper": "copper_factors",
               "alum": "alum_factors"}[metal]
        return [{"name": n, "weight": w, "direction": d, "note": note}
                for n, w, d, note in self.fd[key]]

    # ── База ряда: свежий обзвон → живой рынок → сид ─────────
    def base_for(self, series: str) -> tuple[float, str, str]:
        """→ (значение, источник, дата)"""
        rb = SERIES_SURVEY.get(series)
        if rb and rb in self.survey:
            price, d = self.survey[rb]
            return float(price), "обзвон", str(d)
        if series in self.live:
            value, source, d = self.live[series]
            return float(value), source, str(d)
        f6 = self.fd["forecast_6m"]
        return float(f6[series]["now"]), "сид (УСТАРЕЛО — обнови обзвон!)", self.fd["date"]


    # ── Прогноз ──────────────────────────────────────────────
    def forecast(self, series: str, months: int = 6) -> dict:
        f6 = self.fd["forecast_6m"]
        if series not in f6:
            raise KeyError(f"Нет ряда {series}. Доступны: {list(f6)}")
        base, base_source, base_date = self.base_for(series)
        metal = ("black" if series in BLACK_SERIES
                 else "copper" if "copper" in series else "alum")
        sig = self.signal(metal)
        trend = sig * 0.04
        points = []
        for m in range(1, months + 1):
            cum = sum(trend * (0.85 ** i) for i in range(m))
            price = base * (1 + cum)
            if metal == "black":
                target_month = date.today().month + m
                if target_month >= 10:                       # октябрь и позже
                    over = min(target_month - 9, 3) / 3
                    price = min(price, base - 1500 * over)
            points.append({
                "month": m,
                "price": round(price),
                "lower": round(price * (1 - 0.03 * m ** 0.5)),
                "upper": round(price * (1 + 0.03 * m ** 0.5)),
            })
        return {
            "series": series,
            "series_name": SERIES_NAMES.get(series, series),
            "base": round(base), "base_source": base_source, "base_date": base_date,
            "signal": sig, "metal": metal, "points": points,
            "phase_note": self.fd["two_phase_black"] if metal == "black" else None,
        }

    def all_series(self) -> list[str]:
        return list(self.fd["forecast_6m"].keys())

    # ── Сводка ───────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "model_date": self.fd["date"],
            "signals": {m: self.signal(m) for m in ("black", "copper", "alum")},
            "strategy": {
                "black": "Продавать в окно до конца сентября (пик; 4кв: −1500…−2000 ₽/т)",
                "copper": "Накапливать (+9% за 6 мес, лучший актив)",
                "alum": "Держать (+5% за 6 мес)",
            },
            "two_phase": self.fd["two_phase_black"],
        }

    def indicators(self) -> list:
        return self.fd["early_warning_indicators"]


def make_model(db) -> ForecastModel:
    """Собрать модель с живыми базами из БД (обзвон ≤21 дня + рынок MMI/Транслом)."""
    from app.services.market import latest_survey_prices, gather_live_bases
    return ForecastModel(latest_survey_prices(db, max_age_days=21),
                         gather_live_bases(db))


def scenarios(db, series: str, months: int = 6) -> dict:
    """Мультисценарный прогноз + сравнение баз из разных источников.

    Сценарии (сдвиг сигнала факторной модели):
      базовый       — сигнал как есть;
      оптимистичный — сигнал +0.20 (ММК активен, экспорт открылся, ставка вниз);
      пессимистичный— сигнал −0.20 (уход ММК, крепкий рубль, HMS вниз).
    Сравнение баз: одна и та же модель от базы каждого доступного источника
    (обзвон / MMI / Транслом / наш расчёт) — видно коридор расхождения источников.
    """
    from app.services.market import latest_survey_prices, gather_live_bases, latest_quote
    survey = latest_survey_prices(db, max_age_days=21)
    live = gather_live_bases(db)
    fm = ForecastModel(survey, live)
    base_fc = fm.forecast(series, months)
    base_val = base_fc["base"]
    sig = base_fc["signal"]
    metal = base_fc["metal"]

    def _project(base, s):
        pts = []
        for m in range(1, months + 1):
            cum = sum(s * 0.04 * (0.85 ** i) for i in range(m))
            price = base * (1 + cum)
            if metal == "black":
                target_month = date.today().month + m
                if target_month >= 10:
                    over = min(target_month - 9, 3) / 3
                    price = min(price, base - 1500 * over)
            pts.append(round(price))
        return pts

    scen = [
        {"id": "base", "name": f"Базовый (сигнал {sig:+.2f})",
         "points": [p["price"] for p in base_fc["points"]]},
        {"id": "opt", "name": f"Оптимистичный ({min(1, sig+0.2):+.2f})",
         "points": _project(base_val, min(1.0, sig + 0.2))},
        {"id": "pes", "name": f"Пессимистичный ({max(-1, sig-0.2):+.2f})",
         "points": _project(base_val, max(-1.0, sig - 0.2))},
    ]

    # Сравнение источников базы (для чёрных региональных рядов)
    alt_bases = []
    rb = SERIES_SURVEY.get(series)
    if rb and rb in survey:
        p, d = survey[rb]
        alt_bases.append({"source": "Обзвон", "base": round(p), "date": str(d)})
    if series in live:
        v, src, d = live[series]
        alt_bases.append({"source": src, "base": round(v), "date": d})
    region_map = {"perm_fca": "PERM", "komi_fca": "KOMI_NORTH", "hmao": "HMAO"}
    if series in region_map:
        q = latest_quote(db, "lom3a_calc_fca", region=region_map[series])
        if q:
            alt_bases.append({"source": "Наш расчёт (методика MMI)",
                              "base": round(q.value),
                              "date": q.collected_at.date().isoformat()})
        q2 = latest_quote(db, "lom3a_obl", region=region_map[series])
        if q2 and all(abs(a["base"] - q2.value) > 1 for a in alt_bases):
            alt_bases.append({"source": "MMI (обл., без ЖДТ)", "base": round(q2.value),
                              "date": q2.collected_at.date().isoformat()})
    seed_now = fm.fd["forecast_6m"].get(series, {}).get("now")
    if seed_now:
        alt_bases.append({"source": "Сид 22.07 (история)", "base": round(seed_now),
                          "date": "2026-07-22"})
    for a in alt_bases:
        a["points"] = _project(a["base"], sig)

    return {"series": series, "series_name": SERIES_NAMES.get(series, series),
            "months": months, "base": base_val,
            "base_source": base_fc["base_source"], "signal": sig,
            "scenarios": scen, "compare_bases": alt_bases,
            "phase_note": base_fc["phase_note"]}
