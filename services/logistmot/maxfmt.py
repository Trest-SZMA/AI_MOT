"""Оформление сообщений бота в MAX — один стиль для уведомлений логистам
и руководителям.

MAX понимает markdown: **жирный**, *курсив*, `моно`, [текст](url), > цитата.
Правила, по которым собраны все тексты ниже:
  • первая строка — жирный заголовок с сутью: в списке чатов MAX показывает
    только её, и по ней человек решает, открывать ли;
  • подписи полей тихие, значения — обычные; жирным только то, ради чего
    открывают сообщение (телефон, ставка, номер записки);
  • чужой текст (отклик перевозчика, причина отказа) — цитатой;
  • ссылка — словами «Открыть на панели», не голым адресом;
  • один смысловой маркер в начале и ничего больше: 🔔 отклик, 📝 записка,
    📊 сводка, ⏰ напоминание.

Чужой текст экранируется (`esc`): невидимый пробел U+200B после символов
разметки ломает её, не меняя видимого текста. Если MAX всё же отверг
разметку — отправитель шлёт `plain()` того же текста.
"""
from __future__ import annotations

import re

from maxapi.enums.parse_mode import ParseMode

MD = ParseMode.MARKDOWN
ZW = "​"

_MARK = re.compile(r"([*_~+^`\[\]\\])")
_LINE_START = re.compile(r"(?m)^([#>])")


def esc(text: str | None) -> str:
    """Нейтрализовать разметку в чужом тексте, не меняя его вида."""
    if not text:
        return ""
    s = _MARK.sub(lambda m: m.group(1) + ZW, str(text))
    return _LINE_START.sub(lambda m: m.group(1) + ZW, s)


def plain(text: str) -> str:
    """Тот же текст без разметки — запасной вариант, если MAX её не принял."""
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    s = re.sub(r"\*\*|__|~~|\+\+|\^\^|`", "", s)
    s = re.sub(r"(?m)^> ?", "", s)
    return s.replace(ZW, "")


def b(text) -> str:
    return f"**{text}**"


def link(text: str, url: str) -> str:
    return f"[{text}]({url})"


def quote(text: str | None, limit: int = 300) -> str:
    """Чужой текст цитатой; многострочный — каждая строка с «> »."""
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return "\n".join("> " + esc(line) for line in t.splitlines() if line.strip())


def phone(p: str | None) -> str:
    """+79043044435 → +7 904 304-44-35; что не разобрали — как есть."""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d[0] in "78":
        d = "7" + d[1:]
        return f"+{d[0]} {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"
    if len(d) == 10:
        return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"
    return p or ""


def money(v) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def sign_pct(v) -> str:
    return f"{v:+.1f}%".replace(".", ",")


# --- логистам ---------------------------------------------------------------

def offer_alert(name: str, offer: str | None, phone_raw: str | None,
                label: str | None, text: str | None, panel: str,
                bid: int | None) -> str:
    """Пуш логисту о новом отклике. Заголовок — что и по какому рейсу."""
    head = f"🔔 {b('Новый отклик')}"
    if label:
        head += f" · {esc(label)}"
    who = [esc(name)]
    if offer:
        who.append(esc(offer))
    if phone_raw:
        who.append(b(phone(phone_raw)))
    lines = [head, " · ".join(who)]
    q = quote(text) if text and text not in ("нажал(а) кнопку под заявкой",
                                             "поделился контактом") else ""
    if q:
        lines.append(q)
    lines.append(link("Открыть рейс на панели", f"{panel}/#z={bid}") if bid
                 else link("Открыть панель", panel))
    return "\n".join(lines)


def reminder(stale: list, panel: str) -> str:
    """Утреннее напоминание: заявки истекли, откликов не было."""
    n = len(stale)
    word = "заявка" if n % 10 == 1 and n % 100 != 11 else (
        "заявки" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "заявок")
    lines = [f"⏰ {b(f'Без откликов истекли {n} {word}')}"]
    for lab, bid in stale:
        lines.append(f"• {esc(lab)} — {link('повторить', f'{panel}/#z={bid}')}")
    lines.append("Или боту: «повторить <номер> <новые даты>».")
    return "\n".join(lines)


def sent_summary(bid: int, route: str | None, chat: str, dm: str, panel: str) -> str:
    """Ответ логисту после отправки заявки."""
    return "\n".join([
        f"✓ {b(f'Заявка #{bid} отправлена')}" + (f" · {esc(route)}" if route else ""),
        f"чат перевозчиков: {chat} · личка: {dm}",
        link("Открыть рейс на панели", f"{panel}/#z={bid}"),
    ])
