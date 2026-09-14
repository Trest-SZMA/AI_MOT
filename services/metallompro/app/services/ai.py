"""ИИ-слой: абстракция провайдера (Perplexity / Claude) + прикладные функции.

Провайдер задаётся LLM_PROVIDER в .env. Без ключа функции возвращают None,
платформа продолжает работать — на дашборде честно пишем «ИИ не подключён».
"""
from __future__ import annotations
import json
import logging
import re

import httpx

from app.config import LLM_PROVIDER, PERPLEXITY_API_KEY, ANTHROPIC_API_KEY
from app.db import session_scope
from app.models import NewsItem, AiOutput

log = logging.getLogger("ai")

PPLX_URL = "https://api.perplexity.ai/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def llm_available() -> bool:
    if LLM_PROVIDER == "perplexity":
        return bool(PERPLEXITY_API_KEY)
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    return False


def llm_name() -> str:
    return {"perplexity": "Perplexity sonar", "anthropic": "Claude"}.get(LLM_PROVIDER, "выключен")


def complete(system: str, user: str, max_tokens: int = 1500,
             web_search: bool = False) -> str | None:
    """Единая точка вызова ИИ. web_search имеет смысл только для Perplexity."""
    try:
        if LLM_PROVIDER == "perplexity" and PERPLEXITY_API_KEY:
            model = "sonar-pro" if web_search else "sonar"
            r = httpx.post(PPLX_URL, timeout=90,
                           headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
                           json={"model": model, "max_tokens": max_tokens,
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        if LLM_PROVIDER == "anthropic" and ANTHROPIC_API_KEY:
            r = httpx.post(ANTHROPIC_URL, timeout=90,
                           headers={"x-api-key": ANTHROPIC_API_KEY,
                                    "anthropic-version": "2023-06-01"},
                           json={"model": "claude-sonnet-5", "max_tokens": max_tokens,
                                 "system": system,
                                 "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"])
    except Exception as e:
        log.warning("LLM (%s): %s", LLM_PROVIDER, e)
    return None


SYSTEM_ANALYST = (
    "Ты — аналитик рынка лома чёрных и цветных металлов России, работаешь на компанию "
    "МетОптТорг (Пермь; площадки: Пермский край, Коми, ХМАО, Волгоград). Компания заготавливает "
    "и продаёт лом 3А/5А/12А, трубы НКТ б/у, кабель, цветмет. Пиши по-русски, кратко и по делу, "
    "цифры с указанием источника. Никогда не выдумывай цены — если данных нет, так и скажи."
)


# ── Разметка новостей ────────────────────────────────────────────
def annotate_fresh_news(limit: int = 15) -> int:
    if not llm_available():
        return 0
    with session_scope() as db:
        items = (db.query(NewsItem).filter(NewsItem.ai_done == False)  # noqa: E712
                 .order_by(NewsItem.collected_at.desc()).limit(limit).all())
        if not items:
            return 0
        listing = "\n".join(f"{i}. {n.title}" for i, n in enumerate(items))
        answer = complete(
            SYSTEM_ANALYST,
            "Оцени влияние каждой новости на цену лома чёрных металлов в РФ. "
            "Ответ — строго JSON-массив объектов {\"i\": номер, \"dir\": число от -1 до 1, "
            "\"impact\": \"краткое влияние, 1 фраза\", \"region\": \"регион или РФ\"}.\n\n" + listing,
            max_tokens=1800)
        if not answer:
            return 0
        try:
            m = re.search(r"\[.*\]", answer, re.DOTALL)
            data = json.loads(m.group(0)) if m else []
        except Exception:
            data = []
        marked = 0
        by_i = {d.get("i"): d for d in data if isinstance(d, dict)}
        for i, n in enumerate(items):
            d = by_i.get(i)
            if d:
                n.ai_direction = max(-1.0, min(1.0, float(d.get("dir", 0))))
                n.ai_impact = str(d.get("impact", ""))[:500]
                n.ai_region = str(d.get("region", ""))[:60]
                marked += 1
            n.ai_done = True
        return marked


# ── Утренний брифинг ─────────────────────────────────────────────
def _platform_context(db) -> str:
    """Собираем контекст платформы для ИИ: цены, сигналы, радар, остатки."""
    from app.services.market import latest_quote
    from app.services.forecast import make_model
    from app.services.radar import run_radar
    parts = []
    fm = make_model(db)
    s = fm.summary()
    parts.append(f"Сигналы модели: чёрный {s['signals']['black']:+.2f}, "
                 f"медь {s['signals']['copper']:+.2f}, алюминий {s['signals']['alum']:+.2f}")
    for metric, label in [("translom_index", "Индекс Транслом"), ("usd_rub", "USD/RUB"),
                          ("key_rate", "Ставка ЦБ"), ("hms_turkey", "HMS CFR Турция"),
                          ("copper_lme", "Медь LME")]:
        q = latest_quote(db, metric)
        if q:
            parts.append(f"{label}: {q.value:,.0f} {q.unit} ({q.source}, {q.quality}, "
                         f"{q.collected_at:%d.%m})")
    alarms = [c for c in run_radar(db) if c["level"] in ("warn", "alarm")]
    if alarms:
        parts.append("Радар: " + "; ".join(f"{c['name']} — {c['message']}" for c in alarms[:5]))
    try:
        from app.services.company import stock_by_warehouse
        st = stock_by_warehouse(db, "chermet")
        if st:
            tot = sum(x["qty_t"] for x in st)
            parts.append(f"Остатки чермета по 1С: ~{tot:,.0f} т на {len(st)} складах")
    except Exception:
        pass
    return "\n".join(parts)


def generate_briefing() -> str | None:
    if not llm_available():
        return None
    with session_scope() as db:
        ctx = _platform_context(db)
    answer = complete(
        SYSTEM_ANALYST,
        "Составь утренний брифинг для директора (5-8 пунктов, маркированный список, Markdown). "
        "Сначала найди свежие новости рынка лома/стали РФ за последние сутки (остановки заводов, "
        "прайсы ММК/НЛМК, ставка ЦБ, экспорт, санкции). Затем свяжи их с нашими данными ниже и "
        "закончи одной строкой «Что делать сегодня». Данные платформы:\n\n" + ctx,
        max_tokens=1200, web_search=True)
    if answer:
        with session_scope() as db:
            db.add(AiOutput(kind="briefing", content=answer, model=llm_name()))
    return answer


# ── ИИ-ревизия факторной модели ──────────────────────────────────
def review_factors() -> dict | None:
    """ИИ смотрит на текущие факторы модели, свежие данные платформы и новости
    рынка — и предлагает обновления весов/направлений с обоснованием.

    Модель НЕ меняется автоматически: предложение сохраняется и показывается
    рядом с текущими весами — решение за человеком.
    """
    if not llm_available():
        return None
    from app.services.forecast import ForecastModel
    with session_scope() as db:
        ctx = _platform_context(db)
    fm = ForecastModel()
    factors_txt = ""
    for metal in ("black", "copper", "alum"):
        factors_txt += f"\n[{metal}] сигнал {fm.signal(metal):+.2f}:\n"
        for f in fm.factors(metal):
            factors_txt += (f"  • {f['name']} | вес {f['weight']} | "
                            f"направление {f['direction']:+.1f}\n")
    answer = complete(
        SYSTEM_ANALYST,
        "Проведи ревизию факторной модели прогноза цен лома. Сначала найди свежие "
        "данные (последние 2 недели): цены лома РФ, HMS Турция, LME, ставка ЦБ, "
        "загрузка заводов, экспорт. Затем для КАЖДОГО фактора скажи, актуальны ли "
        "вес и направление, и что поменять. Ответ строго в Markdown:\n"
        "## Вердикт (2-3 фразы)\n## Предлагаемые правки\n"
        "Таблица: Фактор | Сейчас | Предлагаю | Почему\n"
        "## Новые факторы (если рынок изменился)\n"
        "Пиши компактно: в «Почему» не больше 20 слов.\n\n"
        f"ТЕКУЩАЯ МОДЕЛЬ:{factors_txt}\n\nДАННЫЕ ПЛАТФОРМЫ:\n{ctx}",
        max_tokens=3500, web_search=True)
    if answer:
        with session_scope() as db:
            db.add(AiOutput(kind="factors", content=answer, model=llm_name()))
        return {"content": answer}
    return None


# ── Паутина влияний ──────────────────────────────────────────────
def _parse_json_answer(answer: str | None) -> dict | None:
    """Достать JSON из ответа ИИ: убрать ```-заборы, сноски [1], хвосты."""
    if not answer:
        return None
    t = answer.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    t = re.sub(r"\[\d+\]", "", t)                      # сноски-цитаты
    start = t.find("{")
    if start < 0:
        return None
    # балансный поиск конца объекта
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i, ch in enumerate(t[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
    if end < 0:
        return None
    frag = t[start:end + 1]
    for candidate in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):   # хвостовые запятые
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def build_influence_web() -> dict | None:
    """ИИ строит причинно-следственную паутину: события/факторы → каналы →
    влияние на цену лома по регионам, с направлением, силой, лагом и объяснением.

    Возвращает строгий JSON (events / links / targets) — фронт рисует граф.
    """
    if not llm_available():
        return None
    with session_scope() as db:
        ctx = _platform_context(db)
        from app.models import NewsItem
        from datetime import datetime, timedelta
        news = (db.query(NewsItem)
                .filter(NewsItem.collected_at >= datetime.utcnow() - timedelta(days=10),
                        NewsItem.ai_impact != "").all())
        news.sort(key=lambda n: -abs(n.ai_direction or 0))
        news_txt = "\n".join(f"- {n.title[:100]} (ИИ-оценка {n.ai_direction:+.1f}: {n.ai_impact[:80]})"
                             for n in news[:12])
    answer = complete(
        SYSTEM_ANALYST,
        "Построй сеть влияний на рынок лома чёрных металлов РФ — граф КОНКРЕТНЫХ "
        "событий (стиль сетевой карты: узлы-события связаны напрямую с ценами).\n"
        "ПРАВИЛА УЗЛОВ:\n"
        "1) НИКАКИХ общих фраз (\"ценовой фон\", \"баланс спроса\", \"логистика в целом\") — "
        "каждый узел это КОНКРЕТНОЕ событие/факт с цифрой и именем: завод, компания, "
        "регион, %, ₽/т, $/т. Хорошо: \"ПНТЗ поднял CPT на +1555 ₽/т за неделю\", "
        "\"Кыргызстан продлил запрет экспорта лома на 6 мес\". Плохо: \"рост цен\".\n"
        "2) Узлы-события — только ВНЕШНИЕ (не данные МетОптТорг). Компания — только "
        "узлы-цели kind=us: \"Маржа МетОптТорг (чёрный)\", \"Стратегия остатков МетОптТорг\".\n"
        "3) Узлы-цены (kind=price): \"Цена лома Пермь\", \"Цена лома РФ\", \"Цветмет\".\n"
        "4) size узла 1..5 — важность для рынка.\n"
        "СВЯЗИ: событие→событие (если одно вызывает/усиливает другое) и событие→цена/цель "
        "НАПРЯМУЮ, без промежуточных абстракций. У связи: sign, strength 1..3, lag, "
        "why (конкретно, ≤15 слов).\n"
        "Ответ — СТРОГО один JSON-объект:\n"
        '{"nodes":[{"id":"n1","title":"конкретное событие с цифрой","kind":'
        '"supply|demand|macro|export|regul|price|us","sign":-1..1,"size":1..5,'
        '"src":"источник, дата"}],\n'
        ' "links":[{"from":"n1","to":"n2","sign":-1..1,"strength":1..3,"lag":"2-4 нед",'
        '"why":"конкретно почему"}],\n'
        ' "summary":"3-4 фразы: главные цепочки и что делать МетОптТорг"}\n'
        "14-20 узлов-событий (большинство — из новостной ленты + найденные в интернете), "
        "3 узла-цены, 2 узла-цели МетОптТорг, 20-30 связей. Каждый price/us узел должен "
        "иметь входящие связи.\n\n"
        f"НОВОСТНАЯ ЛЕНТА С ИИ-ОЦЕНКАМИ:\n{news_txt}\n\n"
        "СПРАВКА О КОМПАНИИ (только для калибровки узлов-целей, В СОБЫТИЯ НЕ ВКЛЮЧАТЬ):\n"
        f"{ctx}",
        max_tokens=3500, web_search=True)
    data = _parse_json_answer(answer)
    if not (data and data.get("nodes") and data.get("links")):
        log.warning("Паутина: невалидный JSON, пробую ещё раз. Голова ответа: %r",
                    (answer or "")[:200])
        answer = complete(SYSTEM_ANALYST,
                          "Верни СТРОГО валидный JSON без markdown, без сносок [1] и без "
                          "текста вокруг. Исправь и верни полностью этот JSON:\n" + (answer or "")[:6000],
                          max_tokens=2500)
        data = _parse_json_answer(answer)
    if not (data and data.get("nodes") and data.get("links")):
        log.warning("Паутина: ИИ так и не вернул валидный JSON")
        return None
    # Страховка: наши внутренние показатели не могут быть «событиями»
    _INTERNAL = ("метоптторг", "остатки", "остатков", "сигнал модели", "наши рейсы",
                 "наша маржа", "наших", "222")
    bad_ids = {n.get("id") for n in data["nodes"]
               if n.get("kind") not in ("price", "us")
               and any(k in str(n.get("title", "")).lower() for k in _INTERNAL)}
    if bad_ids:
        data["nodes"] = [n for n in data["nodes"] if n.get("id") not in bad_ids]
        data["links"] = [l for l in data["links"]
                         if l.get("from") not in bad_ids and l.get("to") not in bad_ids]
        log.info("Паутина: отфильтровано внутренних «событий»: %d", len(bad_ids))
    with session_scope() as db:
        db.add(AiOutput(kind="web", content=json.dumps(data, ensure_ascii=False),
                        model=llm_name()))
    return data


# ── Чат по данным ────────────────────────────────────────────────
def answer_question(question: str) -> str | None:
    if not llm_available():
        return None
    with session_scope() as db:
        ctx = _platform_context(db)
        # немного данных о продажах для вопросов «кому/почём продаём»
        try:
            from app.services.company import sales_by_buyer
            top = sales_by_buyer(db)[:6]
            if top:
                ctx += "\nНаши продажи (12 мес): " + "; ".join(
                    f"{b['buyer']}: {b['qty_t']:,.0f} т по ср. {b['avg_price']:,.0f} ₽/т"
                    for b in top)
        except Exception:
            pass
    answer = complete(
        SYSTEM_ANALYST,
        f"Вопрос директора: {question}\n\nДанные платформы:\n{ctx}\n\n"
        "Если для ответа нужны свежие рыночные данные — найди их в интернете и укажи источник.",
        max_tokens=1200, web_search=True)
    if answer:
        with session_scope() as db:
            db.add(AiOutput(kind="chat", content=answer, question=question, model=llm_name()))
    return answer
