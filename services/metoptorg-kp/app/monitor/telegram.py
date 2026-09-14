"""Чтение публичного Telegram-канала через его веб-просмотр.

Почему именно так. Bot API читает только те чаты, куда бота добавили админом, —
для чужих торговых каналов он бесполезен. MTProto (Telethon) читает всё, но
требует пользовательского аккаунта, номера телефона и интерактивного входа.
А публичный веб-просмотр `https://t.me/s/<канал>` отдаёт последние сообщения
обычным HTML — без ключей, без входа и без нарушения чего-либо: это та же
страница, которую видит любой человек по ссылке.

Ограничение честное: закрытые каналы, группы и каналы с выключенным
предпросмотром так не читаются — там нужен user-аккаунт, это отдельное решение.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

BASE = "https://t.me/s/{handle}"
TIMEOUT = 25
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Сообщение целиком: из него достаём id, время и текст
_MESSAGE_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"[^>]*data-post="(?P<post>[^"]+)"'
    r'(?P<body>.*?)(?=<div class="tgme_widget_message[^"]*"[^>]*data-post="|\Z)',
    re.S)
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


class TelegramUnavailable(RuntimeError):
    """Канал недоступен: не существует, закрыт или предпросмотр выключен."""


@dataclass
class Message:
    external_id: str      # «канал/123»
    posted_at: dt.datetime | None
    text: str
    url: str


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def normalize_handle(raw: str) -> str:
    """«@lom_ural», «https://t.me/lom_ural», «t.me/s/lom_ural» → «lom_ural»."""
    s = (raw or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^t\.me/", "", s, flags=re.I)
    s = re.sub(r"^s/", "", s, flags=re.I)
    s = s.lstrip("@").strip("/")
    return s.split("?")[0].split("/")[0]


def _clean(fragment: str) -> str:
    text = _BR_RE.sub("\n", fragment)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch(handle: str) -> tuple[str, list[Message]]:
    """→ (название канала, сообщения). Бросает TelegramUnavailable."""
    handle = normalize_handle(handle)
    if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", handle):
        raise TelegramUnavailable(
            f"«{handle}» не похоже на имя канала: ожидается латиница, цифры "
            "и подчёркивание, например lom_ural")
    req = urllib.request.Request(BASE.format(handle=handle),
                                 headers={"User-Agent": UA,
                                          "Accept-Language": "ru,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=_ssl_context()) as resp:
            page = resp.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise TelegramUnavailable(f"Telegram вернул {e.code} по каналу "
                                  f"@{handle}") from e
    except OSError as e:
        raise TelegramUnavailable(f"сеть недоступна: {e}") from e

    title_m = _TITLE_RE.search(page)
    title = html.unescape(title_m.group(1)) if title_m else handle

    messages: list[Message] = []
    for m in _MESSAGE_RE.finditer(page):
        body = m.group("body")
        text_m = _TEXT_RE.search(body)
        if not text_m:
            continue   # фото/видео без подписи — торговать по нему нечем
        text = _clean(text_m.group(1))
        if not text:
            continue
        when = None
        time_m = _TIME_RE.search(body)
        if time_m:
            try:
                when = dt.datetime.fromisoformat(
                    time_m.group(1).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                when = None
        post = m.group("post")
        messages.append(Message(external_id=post, posted_at=when, text=text,
                                url=f"https://t.me/{post}"))

    if not messages:
        raise TelegramUnavailable(
            f"У канала @{handle} не видно сообщений. Так бывает, если канал "
            "закрытый, это чат/группа, либо у канала выключен предпросмотр в "
            "вебе. Публичные каналы читаются, закрытые — нет.")
    return title, messages
