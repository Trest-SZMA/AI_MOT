"""Единый клиент ИИ-провайдера (OpenRouter / Perplexity) с веб-поиском.

Раньше вызов провайдера был скопирован в ai_price, ai_weight и ai_yield —
каждый со своим разбором ошибок и своим SSL-контекстом. Теперь одно место:
здесь же живут подсказки поиска по русским торговым площадкам, без которых
модель ищет «вообще» и по нишевому б/у оборудованию не находит ничего.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "perplexity/sonar"
TIMEOUT = 90

# Площадки, где реально котируется б/у нефтепромысловое оборудование и лом.
# Без такой подсказки поиск уходит в маркетплейсы новой техники и возвращает
# «нет данных» по позициям, которые на самом деле продаются каждый месяц.
TRADE_DOMAINS = [
    "avito.ru", "farpost.ru", "drom.ru", "pulscen.ru", "tiu.ru",
    "equipnet.ru", "promportal.su", "b2b-center.ru", "zakupki.gov.ru",
    "flagma.ru", "all.biz", "supl.biz", "rusprofile.ru",
]


class AiUnavailable(RuntimeError):
    """Ключ не задан или провайдер недоступен."""


def is_enabled() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("PERPLEXITY_API_KEY"))


def provider() -> tuple[str, str, dict, str]:
    """→ (url, модель, заголовки, имя провайдера)."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return (OPENROUTER_URL,
                os.environ.get("AI_PRICE_MODEL", DEFAULT_MODEL),
                {"Authorization": f"Bearer {key}",
                 "HTTP-Referer": "https://metopt-torg.ru",
                 "X-Title": "MetOptTorg KP"},
                "openrouter")
    key = os.environ.get("PERPLEXITY_API_KEY")
    if key:
        return (PERPLEXITY_URL, os.environ.get("AI_PRICE_MODEL", "sonar"),
                {"Authorization": f"Bearer {key}"}, "perplexity")
    raise AiUnavailable(
        "ИИ-поиск выключен: не задан OPENROUTER_API_KEY (или PERPLEXITY_API_KEY) "
        "в окружении службы. Оценка и подбор клиентов продолжают работать на "
        "фактах 1С — ключ нужен только для внешних источников.")


def _ssl_context() -> ssl.SSLContext:
    """Системные корни, с запасным вариантом certifi (macOS их не видит)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# ---------------------------------------------------- частота и повторы

# Кампания подбора клиентов бьёт три запроса подряд (профиль, закупки,
# отраслевой поиск), пакетный поиск цен — до трёх на каждую позицию. Провайдер
# считает запросы в минуту и отвечает 429. Поэтому вызовы к нему сериализованы
# и разнесены во времени, а на 429 и временные ошибки делается повтор.
_MIN_INTERVAL_DEFAULT = 1.5
_MAX_RETRIES_DEFAULT = 3
_RETRYABLE = (408, 425, 429, 500, 502, 503, 504)

_rate_lock = threading.Lock()
_last_call_at = 0.0


def _min_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("AI_MIN_INTERVAL_SEC",
                                             _MIN_INTERVAL_DEFAULT)))
    except ValueError:
        return _MIN_INTERVAL_DEFAULT


def _max_retries() -> int:
    try:
        return max(1, int(os.environ.get("AI_MAX_RETRIES", _MAX_RETRIES_DEFAULT)))
    except ValueError:
        return _MAX_RETRIES_DEFAULT


def _throttle() -> None:
    """Не чаще одного запроса в AI_MIN_INTERVAL_SEC — на весь процесс."""
    global _last_call_at
    interval = _min_interval()
    with _rate_lock:   # блокировку держим и во сне: очередь должна быть общей
        wait = _last_call_at + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _retry_after(headers, attempt: int) -> float:
    """Сколько ждать: сначала слушаем провайдера, иначе удваиваем паузу."""
    raw = None
    try:
        raw = headers.get("Retry-After") if headers else None
    except Exception:  # noqa: BLE001 — заголовков может не быть вовсе
        raw = None
    if raw:
        try:
            return min(60.0, max(1.0, float(str(raw).strip())))
        except ValueError:
            pass
    return min(30.0, _min_interval() * (2 ** attempt) + 1.0)


def _call_with_retry(url: str, body: bytes, headers: dict) -> dict:
    attempts = _max_retries()
    last_detail = ""
    for attempt in range(attempts):
        _throttle()
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=_ssl_context()) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:300]
            except Exception:  # noqa: BLE001 — тело ошибки не обязано читаться
                pass
            last_detail = detail
            if e.code in _RETRYABLE and attempt < attempts - 1:
                pause = _retry_after(getattr(e, "headers", None), attempt)
                log.warning("провайдер вернул %s, повтор через %.1f с "
                            "(попытка %s из %s)", e.code, pause,
                            attempt + 1, attempts)
                time.sleep(pause)
                continue
            hint = ""
            if e.code == 429:
                hint = (" Провайдер ограничивает частоту запросов. Сделано "
                        f"{attempts} попытки с паузами. Увеличьте паузу "
                        "(AI_MIN_INTERVAL_SEC) или число повторов "
                        "(AI_MAX_RETRIES) в /etc/metoptorg-kp.env, либо "
                        "поднимите тариф у провайдера.")
            elif "security policy" in detail or e.code == 403:
                hint = (" Похоже, домен провайдера закрыт на сетевом периметре. "
                        "С этого сервера работает Perplexity (api.perplexity.ai): "
                        "задайте PERPLEXITY_API_KEY в /etc/metoptorg-kp.env.")
            raise AiUnavailable(
                f"провайдер вернул {e.code} {e.reason}: {detail}{hint}") from e
        except OSError as e:
            if attempt < attempts - 1:
                time.sleep(_retry_after(None, attempt))
                continue
            raise AiUnavailable(f"сеть недоступна: {e}") from e
    raise AiUnavailable(f"провайдер не ответил после {attempts} попыток: "
                        f"{last_detail}")


def chat(prompt: str, *, system: str | None = None, temperature: float = 0.0,
         search_domains: list[str] | None = None,
         max_tokens: int | None = None) -> tuple[str, list[str]]:
    """Запрос к модели с веб-поиском. → (текст ответа, список ссылок)."""
    url, model, headers, kind = provider()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {"model": model, "messages": messages,
                     "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if search_domains:
        # Perplexity ограничивает список; лишнее молча игнорируется провайдером
        payload["search_domain_filter"] = search_domains[:10]

    req_body = json.dumps(payload).encode()
    req_headers = {"Content-Type": "application/json", **headers}
    body = _call_with_retry(url, req_body, req_headers)

    content = body["choices"][0]["message"]["content"]
    urls: list[str] = []
    for key in ("citations", "search_results"):
        extra = body.get(key)
        if not extra:
            continue
        for c in extra:
            u = c if isinstance(c, str) else (c or {}).get("url")
            if u:
                urls.append(u)
    return content, urls


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)
_JSON_ARR_RE = re.compile(r"\[.*\]", re.S)


def extract_json(text: str, want_list: bool = False):
    """Достать JSON из ответа модели, даже если он обёрнут пояснениями."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    pattern = _JSON_ARR_RE if want_list else _JSON_OBJ_RE
    m = pattern.search(text)
    if not m:
        # список мог приехать внутри объекта и наоборот — пробуем второй вариант
        m = (_JSON_OBJ_RE if want_list else _JSON_ARR_RE).search(text)
    if not m:
        raise ValueError(f"ответ ИИ без JSON: {text[:200]}")
    return json.loads(m.group(0))


def ask_json(prompt: str, *, want_list: bool = False, **kwargs):
    """Запрос, ответ которого обязан быть JSON. → (данные, ссылки)."""
    content, urls = chat(prompt, **kwargs)
    return extract_json(content, want_list=want_list), urls
