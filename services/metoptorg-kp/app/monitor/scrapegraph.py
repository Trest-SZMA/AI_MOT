"""Клиент ScrapeGraphAI: поиск и разбор страниц, которые нам не отдаются напрямую.

Зачем нужен. Telegram мы читаем сами, но остальной рынок живёт на сайтах, часть
которых обычному HTTP-клиенту не отвечает (Avito отдаёт 429), а часть отдаёт
страницу, из которой ещё надо достать структуру. ScrapeGraphAI делает обе вещи:
`search` ищет по запросу и возвращает разобранный JSON, `extract` разбирает
конкретный URL по текстовому описанию.

Включается ключом `SGAI_API_KEY` в окружении службы. Без ключа модуль молчит и
ничего не ломает — мониторинг Telegram и подбор клиентов работают как работали.

О чём стоит знать владельцу: сервис умеет обходить защиту от роботов, но само
по себе это не делает чтение чужого сайта разрешённым. Правила площадки и её
robots.txt никуда не деваются, и решение, какие адреса сюда заводить, —
хозяйское. Ни одного адреса Avito сервис сам не добавляет.
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import socket
import ssl
import urllib.parse

from ..normalize import web_url

log = logging.getLogger(__name__)

BASE = os.environ.get("SGAI_API_URL", "https://v2-api.scrapegraphai.com/api")
SEARCH_URL = f"{BASE}/search"
EXTRACT_URL = f"{BASE}/extract"
TIMEOUT = 120


class ScrapeGraphUnavailable(RuntimeError):
    """Ключ не задан или сервис недоступен."""


def is_enabled() -> bool:
    return bool(os.environ.get("SGAI_API_KEY"))


def _key() -> str:
    key = os.environ.get("SGAI_API_KEY")
    if not key:
        raise ScrapeGraphUnavailable(
            "ScrapeGraphAI выключен: не задан SGAI_API_KEY в окружении службы "
            "(/etc/metoptorg-kp.env). Telegram-мониторинг и подбор клиентов "
            "работают и без него.")
    return key


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def proxy_url() -> str | None:
    """Прокси для исходящих запросов, если он задан в окружении службы."""
    for name in ("SGAI_PROXY", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(name)
        if value:
            return value
    return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS к конкретному адресу, но с именем домена в SNI и в проверке сертификата.

    Домен сервиса резолвится в несколько адресов, и с сервера компании они
    ведут себя по-разному: до одного TCP не открывается вовсе, до другого
    открывается, но TLS не завершается. Обычный клиент берёт первый адрес из
    списка и молча висит до таймаута — здесь адреса перебираются, а сертификат
    по-прежнему проверяется на домен.

    Если задан прокси, соединение идёт через него: CONNECT, затем TLS уже
    внутри туннеля, снова с именем домена.
    """

    def __init__(self, host: str, ip: str, **kwargs):
        super().__init__(host, **kwargs)
        self._ip = ip

    def _through_proxy(self, proxy: str) -> socket.socket:
        parts = urllib.parse.urlsplit(proxy if "//" in proxy else f"//{proxy}")
        port = parts.port or (443 if parts.scheme == "https" else 8080)
        sock = socket.create_connection((parts.hostname, port), self.timeout)
        request = f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n" \
                  f"Host: {self.host}:{self.port}\r\n"
        if parts.username:
            token = base64.b64encode(
                f"{parts.username}:{parts.password or ''}".encode()).decode()
            request += f"Proxy-Authorization: Basic {token}\r\n"
        sock.sendall((request + "\r\n").encode())
        answer = b""
        while b"\r\n\r\n" not in answer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            answer += chunk
        first = answer.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200 " not in first:
            sock.close()
            raise OSError(f"прокси отказал в туннеле: {first}")
        return sock

    def connect(self):
        proxy = proxy_url()
        if proxy:
            self.sock = self._through_proxy(proxy)
        else:
            self.sock = socket.create_connection((self._ip, self.port),
                                                 self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ScrapeGraphUnavailable(f"не удалось определить адрес {host}: {e}")
    seen, out = set(), []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _post(url: str, payload: dict) -> dict:
    parts = urllib.parse.urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "SGAI-APIKEY": _key(),
               "Accept": "application/json"}
    ctx = _ssl_context()

    problems: list[str] = []
    # через прокси адрес назначения выбирает он сам — перебирать нечего
    targets = ["proxy"] if proxy_url() else _addresses(host, port)
    for ip in targets:
        conn = _PinnedHTTPSConnection(host, ip, port=port, timeout=TIMEOUT,
                                      context=ctx)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                detail = raw.decode(errors="replace")[:300]
                hint = ""
                if resp.status in (401, 403):
                    hint = " Проверьте SGAI_API_KEY."
                elif resp.status == 402:
                    hint = " Закончились кредиты в кабинете ScrapeGraphAI."
                elif resp.status == 429:
                    hint = " Превышена частота запросов, попробуйте позже."
                raise ScrapeGraphUnavailable(
                    f"ScrapeGraphAI вернул {resp.status} {resp.reason}: "
                    f"{detail}{hint}")
            return json.loads(raw.decode())
        except (OSError, http.client.HTTPException) as e:
            problems.append(f"{ip}: {type(e).__name__} {e}")
            continue
        finally:
            conn.close()
    where = "через прокси" if proxy_url() else "ни по одному адресу"
    raise ScrapeGraphUnavailable(
        f"ScrapeGraphAI недоступен {where} — похоже, домен закрыт на сетевом "
        f"периметре. Разрешите исходящие соединения к {host}:{port} либо "
        "задайте HTTPS_PROXY (или SGAI_PROXY) в /etc/metoptorg-kp.env. "
        f"Попытки: {'; '.join(problems)}")


# Схема, в которой мы хотим получать объявления с любой страницы или из поиска.
LISTINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "listings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "direction": {"type": "string",
                                  "enum": ["sell", "buy", "unknown"]},
                    "price": {"type": "number"},
                    "price_unit": {"type": "string"},
                    "seller": {"type": "string"},
                    "region": {"type": "string"},
                    "contacts": {"type": "string"},
                    "date": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["listings"],
}

LISTINGS_PROMPT = (
    "Собери с этой страницы объявления о купле-продаже промышленного "
    "оборудования, труб и металлолома. Для каждого укажи: заголовок, текст, "
    "продают это или покупают (direction: sell или buy), цену числом в рублях "
    "и за какую единицу (шт, т, кг, м), продавца, регион, контакты, дату и "
    "ссылку на само объявление. Цену за партию пересчитай на единицу и напиши "
    "об этом в тексте. Если объявлений нет — верни пустой список."
)


def _normalize(rows: list, fallback_url: str = "") -> list[dict]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        text = str(r.get("text") or "").strip()
        if not (title or text):
            continue
        price = r.get("price")
        try:
            price = float(str(price).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            price = None
        direction = str(r.get("direction") or "").lower()
        out.append({
            "title": title[:300],
            "text": (f"{title}. {text}" if text else title)[:2000],
            "direction": direction if direction in ("sell", "buy") else None,
            "price": price if price and price > 0 else None,
            "price_unit": (str(r.get("price_unit") or "").strip().lower()
                           or None),
            "seller": str(r.get("seller") or "")[:160] or None,
            "region": str(r.get("region") or "")[:120] or None,
            "contacts": str(r.get("contacts") or "")[:200] or None,
            "date": str(r.get("date") or "")[:32] or None,
            "url": web_url(r.get("url")) or web_url(fallback_url),
        })
    return out


def extract_listings(url: str, prompt: str | None = None,
                     stealth: bool = True) -> list[dict]:
    """Разобрать страницу в список объявлений."""
    payload = {
        "url": url,
        "prompt": prompt or LISTINGS_PROMPT,
        "schema": LISTINGS_SCHEMA,
        "mode": "reader",
        "fetchConfig": {"stealth": stealth, "country": "ru"},
    }
    body = _post(EXTRACT_URL, payload)
    data = body.get("json") or {}
    return _normalize(data.get("listings"), fallback_url=url)


_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_JUNK_RE = re.compile(r"[|>#*`]+")


# Строки, ради которых страницу вообще стоит читать: цена, телефон, намерение.
# Слово «Контакты» в меню сайта есть всегда — по нему отбирать нельзя,
# нужен настоящий телефон. Единицы («шт», «т») тоже встречаются в навигации,
# поэтому они считаются признаком только рядом с числом.
_USEFUL_RE = re.compile(
    r"руб\w*|₽|\bцен[аыу]|стоимост|\bкупл|\bпрода|\bб/у\b|"
    r"\+7\d|\+7[\s(-]|\bтел[.: ]|\bтелефон|"
    r"\d\s*(?:шт|тонн\w*|кг|т)\b", re.I)


def clean_markdown(text: str) -> str:
    """Убрать разметку и навигацию, оставив читаемый текст страницы."""
    s = _MD_IMAGE_RE.sub(" ", text or "")
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _MD_JUNK_RE.sub(" ", s)
    s = re.sub(r"https?://\S+", " ", s)
    lines = []
    for line in s.splitlines():
        line = re.sub(r"[ \t\xa0]+", " ", line).strip(" -—·•")
        if len(line) >= 3:
            lines.append(line)
    return "\n".join(lines)


def useful_lines(text: str, limit: int) -> str:
    """Полезная часть страницы, а не её шапка.

    Первые полторы тысячи знаков любой торговой площадки — это меню и баннеры,
    а телефон с ценой лежат ниже. Поэтому берём строки, в которых есть цена,
    контакт или намерение, и только если таких нет — начало страницы.
    """
    lines = [ln for ln in text.splitlines() if _USEFUL_RE.search(ln)]
    picked = "\n".join(lines)[:limit]
    return picked if len(picked) >= 80 else text[:limit]


def search_listings(query: str, num_results: int = 6,
                    time_range: str | None = None,
                    content_chars: int = 1500) -> list[dict]:
    """Найти страницы по запросу и отдать их нашему разборщику.

    ИИ-извлечение здесь сознательно НЕ включается: на пяти страницах оно не
    укладывается в разумное ожидание (замер — минуты против пяти секунд) и
    тратит кредиты, а заголовок объявления и так несёт главное: «Куплю ПЭД
    (погружной электродвигатель) б/у». Русские объявления разбирает
    `app/monitor/parse.py`, он на них и проверен.
    """
    payload = {
        "query": query,
        "numResults": max(1, min(int(num_results), 20)),
        "locationGeoCode": "RU",
    }
    if time_range:
        payload["timeRange"] = time_range
    body = _post(SEARCH_URL, payload)
    out = []
    for r in (body.get("results") or []):
        if not r.get("url"):
            continue
        title = str(r.get("title") or "").strip()
        content = useful_lines(
            clean_markdown(str(r.get("content") or "")), content_chars)
        out.append({"title": title[:300],
                    "text": (f"{title}\n{content}" if title else content)[:2000],
                    "direction": None, "price": None, "price_unit": None,
                    "seller": None, "region": None, "contacts": None,
                    "date": None, "url": web_url(r.get("url"))})
    return out
