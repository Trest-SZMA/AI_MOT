"""Помощник ИИ — Claude API. Три функции по кнопке, ни одна не трогает деньги.

Решение 16.09.2026 (записка «Независимая модель и ИИ»): языковая модель не
считает выручку и затраты — это делают факт 1С и медианы закрытых сделок,
у которых есть воспроизводимость и измеренная точность. Модель делает то,
что сейчас делает человек руками:

  1. разбор входящего КП любого формата (pdf, скан, xlsx с любой шапкой,
     письмо) в перечень позиций — человек смотрит и подтверждает загрузку;
  2. спорные позиции — предложение номенклатуры 1С с объяснением там, где
     матчер не уверен; ответ уходит в nomen_match_history после подтверждения;
  3. объяснение расхождений — что говорят три слоя расчёта (книга,
     нормативы, факт, независимая оценка) человеческим языком для директора.

Ключ — только ANTHROPIC_API_KEY в окружении контейнера (env/bp-service.env).
Без ключа каждая функция возвращает {"ok": False, "reason": ...}, кнопки
показывают это словами. Модель — claude-opus-5 (задаётся BP_ASSIST_MODEL).
Каждый вызов пишется в журнал (log) с числом токенов.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path

MODEL = os.environ.get("BP_ASSIST_MODEL", "claude-opus-5")
MAX_FILE_MB = 25

# JSON-схема перечня: ровно то, что понимает штатный импорт (list_import).
KP_SCHEMA = {
    "type": "object",
    "properties": {
        "seller": {"type": "string", "description": "продавец лота (организация)"},
        "request_no": {"type": "string", "description": "номер запроса/перечня/лота, если есть"},
        "deadline": {"type": "string", "description": "срок подачи КП, если указан"},
        "notes": {"type": "string", "description": "условия, которые влияют на цену: самовывоз, сроки вывоза, НДС, предоплата"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "наименование позиции как в документе"},
                    "code": {"type": "string", "description": "код/номенклатурный номер продавца, если есть"},
                    "unit": {"type": "string", "description": "единица: тн, кг, шт, м"},
                    "qty": {"type": "number", "description": "количество в указанной единице"},
                    "place": {"type": "string", "description": "место хранения / адрес / подразделение"},
                    "condition": {"type": "string", "description": "состояние, засор, упаковка — если указано"},
                    "price": {"type": "number", "description": "цена за единицу, если продавец её указал (порог/стартовая)"},
                },
                "required": ["name", "unit", "qty", "place"],
                "additionalProperties": False,
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"},
                     "description": "что в документе непонятно или требует уточнения у продавца"},
    },
    "required": ["seller", "items", "warnings"],
    "additionalProperties": False,
}

MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "nomen_1c": {"type": "string", "description": "имя из предложенного списка кандидатов, строго как в списке, или пустая строка"},
                    "confidence": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
                    "why": {"type": "string", "description": "одна фраза: по какому признаку (металл, категория, размер, ГОСТ)"},
                },
                "required": ["item_id", "nomen_1c", "confidence", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    import anthropic
    return anthropic.Anthropic(timeout=180.0, max_retries=1)


def _off() -> dict:
    return {"ok": False, "reason": "Помощник ИИ не подключён: задайте ANTHROPIC_API_KEY "
                                   "в env/bp-service.env и перезапустите контейнер."}


def _log(conn: sqlite3.Connection, actor: str, bp_id: int | None, action: str,
         response, detail: str = "") -> None:
    from .db import log
    u = getattr(response, "usage", None)
    tokens = f"{getattr(u, 'input_tokens', 0)}+{getattr(u, 'output_tokens', 0)} токенов" if u else ""
    log(conn, actor, bp_id, action, f"{MODEL}; {tokens}; {detail}"[:300])


def _call_json(system: str, content: list, schema: dict, max_tokens: int = 16000):
    """Один вызов с гарантированным JSON по схеме. Возвращает (dict, response)."""
    import anthropic
    client = _client()
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "medium"},
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("ключ ANTHROPIC_API_KEY отклонён")
    except anthropic.RateLimitError:
        raise RuntimeError("лимит запросов к ИИ, повторите через минуту")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"ошибка ИИ {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("нет связи с api.anthropic.com из контейнера")
    if response.stop_reason == "refusal":
        raise RuntimeError("ИИ отказался отвечать на этот запрос")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text), response


# ── 1. Разбор КП из файла ──────────────────────────────────────────

_MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def _file_block(path: Path) -> dict:
    """Файл → блок содержимого: pdf и картинки как есть, таблицы и текст —
    текстом (xlsx читается openpyxl во все листы, как есть)."""
    ext = path.suffix.lower()
    if path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise RuntimeError(f"файл больше {MAX_FILE_MB} МБ")
    if ext == ".pdf":
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if ext in _MEDIA:
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return {"type": "image", "source": {"type": "base64", "media_type": _MEDIA[ext], "data": data}}
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    rows.append("\t".join(cells))
                if len(rows) >= 400:
                    rows.append("… (лист обрезан на 400 строках)")
                    break
            parts.append(f"=== Лист «{ws.title}» ===\n" + "\n".join(rows))
        wb.close()
        return {"type": "text", "text": "\n\n".join(parts)[:150000]}
    if ext in (".csv", ".txt", ".md", ".eml"):
        return {"type": "text", "text": path.read_text(encoding="utf-8", errors="replace")[:150000]}
    if ext in (".docx",):
        from docx import Document
        doc = Document(str(path))
        chunks = [p.text for p in doc.paragraphs if p.text.strip()]
        for t in doc.tables:
            for r in t.rows:
                chunks.append("\t".join(c.text.strip() for c in r.cells))
        return {"type": "text", "text": "\n".join(chunks)[:150000]}
    raise RuntimeError(f"формат {ext} не поддерживается (pdf, xlsx, docx, csv, txt, картинки)")


KP_SYSTEM = """Ты разбираешь входящие коммерческие запросы на выкуп металлолома, труб, кабеля,
оборудования (перечни продавца, КП, спецификации, письма) для компании-ломозаготовителя.
Извлеки ВСЕ позиции лота с количеством и единицей ровно как в документе, не объединяя и
не переводя единицы (кг остаются кг). Место хранения — адрес или подразделение продавца
для каждой позиции; если общее для всех — повтори у каждой. Цена — только если продавец
её указал (порог, стартовая, балансовая), не придумывай. В warnings перечисли всё, что
непонятно: нечитаемые строки, позиции без количества, единицы вроде «компл.», условия,
которые влияют на цену. Ничего не считай и не оценивай — только извлекай."""


def parse_kp(conn: sqlite3.Connection, path: str | Path, actor: str, bp_id: int | None = None) -> dict:
    if not enabled():
        return _off()
    path = Path(path)
    try:
        block = _file_block(path)
        data, response = _call_json(
            KP_SYSTEM,
            [block, {"type": "text", "text": f"Файл: {path.name}. Извлеки перечень позиций."}],
            KP_SCHEMA)
    except (RuntimeError, ValueError) as e:
        return {"ok": False, "reason": str(e)}
    _log(conn, actor, bp_id, "assist_parse_kp", response, f"{path.name}: {len(data.get('items') or [])} позиций")
    return {"ok": True, **data, "file": path.name}


# ── 2. Спорные позиции ─────────────────────────────────────────────

MATCH_SYSTEM = """Ты сопоставляешь наименования позиций из перечня продавца металлолома с
номенклатурой 1С компании. Для каждой позиции выбери ОДНО имя строго из предложенного
списка кандидатов (скопируй его буква в букву) или пустую строку, если ни один кандидат
не подходит. Правила: металл в названии решает (алюминий никогда не сопоставляется с
медью, нержавейка — с чёрным ломом); категория лома (5А, 12А, 20А, Б26) важнее слов;
труба сопоставляется по диаметру и толщине стенки; б/у остаётся б/у. Объясни одной фразой."""


def suggest_matches(conn: sqlite3.Connection, items: list, actor: str, bp_id: int | None,
                    candidates_per_item: int = 6) -> dict:
    """items — строки bp_items без подтверждённого сопоставления."""
    if not enabled():
        return _off()
    from . import matcher
    payload = []
    cand_by_item: dict[int, set[str]] = {}
    for it in items:
        cands = matcher.suggest(conn, it["nomenclature"], candidates_per_item, it["seller_code"])
        names = [c["name"] for c in cands]
        cand_by_item[it["id"]] = set(names)
        payload.append({"item_id": it["id"], "name": it["nomenclature"], "unit": it["unit"],
                        "current": it["nomen_1c"], "candidates": names})
    if not payload:
        return {"ok": True, "matches": []}
    try:
        data, response = _call_json(
            MATCH_SYSTEM,
            [{"type": "text", "text": "Позиции и кандидаты (JSON):\n" + json.dumps(payload, ensure_ascii=False)}],
            MATCH_SCHEMA, max_tokens=8000)
    except (RuntimeError, ValueError) as e:
        return {"ok": False, "reason": str(e)}
    out = []
    for m in data.get("matches") or []:
        allowed = cand_by_item.get(int(m["item_id"]), set())
        if m["nomen_1c"] and m["nomen_1c"] not in allowed:
            m = {**m, "nomen_1c": "", "why": "ИИ назвал имя вне списка кандидатов — отклонено; " + m["why"]}
        out.append(m)
    _log(conn, actor, bp_id, "assist_match", response, f"{len(out)} позиций")
    return {"ok": True, "matches": out}


# ── 3. Объяснение расхождений ──────────────────────────────────────

EXPLAIN_SYSTEM = """Ты — помощник директора ломозаготовительной компании. Тебе дают цифры
бизнес-плана сделки в четырёх слоях: книга/расчёт экономиста, нормативная модель,
факт 1С по похожим сделкам, независимая оценка по закрытым сделкам. Объясни по-русски,
коротко и без таблиц, где и почему слои расходятся, и что это значит для решения: 3–6
абзацев, цифры округляй до тысяч рублей и процентов, называй причины прямо (цена
занижена, распределяемые не заложены, тоннаж на долю). Не придумывай цифр, которых нет
во входе. Не давай советов по закупке — только объясни картину. Без заголовков и списков."""


def explain(conn: sqlite3.Connection, ctx: dict, actor: str, bp_id: int) -> dict:
    """ctx — то, что уже посчитано на вкладке «Экономика» (см. main.assist_explain)."""
    if not enabled():
        return _off()
    import anthropic
    client = _client()
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=4000, system=EXPLAIN_SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": "Данные сделки (JSON):\n" + json.dumps(ctx, ensure_ascii=False, default=str)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "reason": "ключ ANTHROPIC_API_KEY отклонён"}
    except anthropic.RateLimitError:
        return {"ok": False, "reason": "лимит запросов к ИИ, повторите через минуту"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "reason": f"ошибка ИИ {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError:
        return {"ok": False, "reason": "нет связи с api.anthropic.com из контейнера"}
    if response.stop_reason == "refusal":
        return {"ok": False, "reason": "ИИ отказался отвечать"}
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    _log(conn, actor, bp_id, "assist_explain", response, f"{len(text)} симв.")
    return {"ok": True, "text": text}
