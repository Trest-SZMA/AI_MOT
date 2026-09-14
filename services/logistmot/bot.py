"""Бот ЛогистМОТ (MAX): приём подписчиков, сбор ответов на заявки, телефоны.

Запуск:  ./.venv/bin/python bot.py

События:
  • BotStarted     — человек нажал «Старт»: в базу, можно слать в личку;
  • BotAdded       — бота добавили в чат: печатаем chat_id (нужен для заявок);
  • MessageCreated — сообщение в личке ИЛИ в групповом чате:
        - текст сохраняется в базу ответов и привязывается к последней заявке;
        - если в тексте есть телефон — сохраняем в карточку человека;
        - «/stop» в личке — отписка от личных сообщений;
  • BotStopped / DialogRemoved — заблокировал бота: больше не шлём в личку.

Сверено с maxapi 1.2.1.
"""

import asyncio
import datetime as dt
import os
import re
import time

from dotenv import load_dotenv
from maxapi import Bot, Dispatcher
from maxapi.types import (BotAdded, BotStarted, BotStopped, CallbackButton,
                          DialogRemoved, MessageCallback, MessageCreated,
                          RequestContactButton)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.utils.vcf import parse_vcf_info

import accounts
import db
import maxfmt as mf
import memo
import weekly
import zayavka

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.environ["MAX_BOT_TOKEN"]
REPLY_WINDOW_SECONDS = int(float(os.getenv("REPLY_WINDOW_HOURS", "48")) * 3600)
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "10"))  # -1 = отключить утреннюю рутину
PANEL_LINK = os.getenv("PANEL_LINK", "http://192.168.6.157:8080").rstrip("/")
WEEKLY_DAY = int(os.getenv("WEEKLY_DAY", "0"))      # 0 = понедельник, -1 = выключить
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "9"))    # сводка уходит в HH:05
CHAT_ID = int(os.getenv("MAX_CHAT_ID")) if os.getenv("MAX_CHAT_ID") else None

# user_id логистов, которым разрешено отправлять заявки через бота
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

# черновики заявок логистов, ожидающие подтверждения: user_id -> fields
pending_zayavki: dict[int, dict] = {}

# мастер «вопрос-ответ»: user_id -> {"step": int, "fields": dict, "ts": float}
wizard: dict[int, dict] = {}
WIZARD_TTL = 1800  # брошенный мастер живёт полчаса

# уточнение «по какому рейсу?»: user_id -> {"reply_id", "options": [bid...], "ts"}
pending_clarify: dict[int, dict] = {}
CLARIFY_TTL = 1800

# после кнопки «Готов взять»: следующие сообщения человека привязываются
# к выбранной заявке: user_id -> (bid, ts)
pending_detail: dict[int, tuple[int, float]] = {}
DETAIL_TTL = 3600

# после кнопки «Отклонить» под запиской ждём от согласующего причину:
# user_id -> (memo_id, ts)
pending_memo_reject: dict[int, tuple[int, float]] = {}
MEMO_REJECT_TTL = 1800


async def send_dm_retry(botobj, user_id: int, text: str, attachments=None,
                        tries: int = 3, md: bool = True) -> str | None:
    """Личное сообщение с повтором: сервер MAX периодически отдаёт 502, и
    пуш, попавший на такую секунду, раньше просто пропадал. None = ушло.

    md=True — текст с разметкой (maxfmt); если MAX её отверг, тут же уходит
    тот же текст без разметки, чтобы уведомление не потерялось из-за вида."""
    err = None
    for attempt in range(tries):
        try:
            if md:
                try:
                    await botobj.send_message(user_id=user_id, text=text,
                                              attachments=attachments,
                                              parse_mode=mf.MD)
                    return None
                except Exception:  # noqa: BLE001 — вдруг дело в разметке
                    pass
            await botobj.send_message(user_id=user_id, text=mf.plain(text) if md else text,
                                      attachments=attachments)
            return None
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"[:120]
            if attempt + 1 < tries:
                await asyncio.sleep(1.5 * (attempt + 1))
    return err


async def notify_logists(botobj, text: str):
    """Мгновенное уведомление всем логистам в личку о новом предложении.
    Итог пишем в журнал: «кому не дошло» раньше терялось молча."""
    ok, failed = 0, []
    for aid in set(ADMIN_IDS) | db.db_admins():
        err = await send_dm_retry(botobj, aid, text)
        if err is None:
            ok += 1
        else:
            failed.append(f"{aid} ({err})")
    print(f"# пуш логистам: ушло {ok}"
          + (f", не дошло: {'; '.join(failed)}" if failed else ""), flush=True)


def offer_alert(name: str, offer: str | None, phone: str | None,
                bid: int | None, text: str) -> str:
    label = broadcast_label(db.get_broadcast(bid)) if bid else None
    return mf.offer_alert(name, offer, phone, label, text, PANEL_LINK, bid)


async def send_memo_notice(botobj, row: dict, event: str) -> None:
    """Уведомить следующего согласующего после решения, принятого в MAX
    (панель для своих решений делает то же в panel.notify_memo)."""
    n = memo.notice(row, event, PANEL_LINK)
    if n is None:
        return
    targets = accounts.role_max_ids(n["role"])
    if not targets:
        print(f"# записка {row['number']}: у роли {n['role']} не задан max_id — "
              f"уведомление не отправлено", flush=True)
        return
    kb = InlineKeyboardBuilder()
    for label, payload in n["buttons"]:
        kb.row(CallbackButton(text=label, payload=payload))
    for uid in targets:
        err = await send_dm_retry(botobj, uid, n["text"], [kb.as_markup()])
        print(f"# записка {row['number']}: уведомление {n['role']} {uid} "
              f"{'ушло' if err is None else 'не ушло (' + err + ')'}", flush=True)


def contact_kb():
    """Кнопка «отправить свой номер» — телефон одним нажатием, без набора."""
    return (InlineKeyboardBuilder()
            .row(RequestContactButton(text="📱 Отправить мой номер"))
            .as_markup())


def phone_from_attachments(message) -> str | None:
    """Телефон из вложения-контакта (кнопка «Отправить мой номер»)."""
    body = getattr(message, "body", None)
    for att in (getattr(body, "attachments", None) or []):
        payload = getattr(att, "payload", None)
        vcf = getattr(payload, "vcf_info", None)
        if not vcf:
            continue
        try:
            phone = parse_vcf_info(vcf).phone
        except Exception:  # noqa: BLE001
            phone = None
        if phone:
            digits = re.sub(r"\D", "", phone)
            if len(digits) == 11:
                return "+7" + digits[1:]
            if len(digits) == 10:
                return "+7" + digits
    return None


KIND_LABEL = {"normal": "обычная", "urgent": "🔥 горящая",
              "new_route": "🆕 новое направление", "rate_up": "📈 ставка повышена"}


def preview_text(fields: dict, head: str = "Проверьте заявку:") -> tuple[dict, str]:
    """Причесать поля (опечатки в городах и тексте) и собрать превью."""
    fixed, fixes = zayavka.fix_typos(fields)
    kind, rate_note = zayavka.detect_kind(fixed)
    note = ("\n\n✏️ Поправил опечатки: " + "; ".join(fixes[:4])) if fixes else ""
    if kind != "normal":
        note += f"\n📌 Тип: {KIND_LABEL.get(kind, kind)} — в чате будет выделена."
    body = (f"{head}\n\n" + zayavka.format_zayavka(fixed, kind, rate_note) + note +
            "\n\n— Отправить всем: «отправить». Отменить: «отмена».")
    return fixed, body


def broadcast_label(b) -> str:
    """Короткое имя заявки для сообщений: «#9 Советский → Пермь (28.07)»."""
    if b is None:
        return "?"
    route = b["route"] or f"заявка"
    dates = b["dates"] or time.strftime("%d.%m", time.localtime(b["sent_at"]))
    return f"#{b['id']} {route} ({dates})"

WIZARD_STEPS = [
    ("route",    "Шаг 1/6 · Маршрут?\nНапример: Покачи - Полевской"),
    ("dates",    "Шаг 2/6 · Даты?\nНапример: 28.07-30.07 или «ежедневно». Пропустить — «-»"),
    ("cargo",    "Шаг 3/6 · Груз и тоннаж?\nНапример: НКТ 73, 100 т. Пропустить — «-»"),
    ("rate",     "Шаг 4/6 · Ставка?\nНапример: 2500 без НДС / 3050 с НДС. Пропустить — «-»"),
    ("ts",       "Шаг 5/6 · Требования к ТС?\nНапример: тент/открытые, верхняя загрузка. Пропустить — «-»"),
    ("contacts", "Шаг 6/6 · Контакты?\nНапример: Светлана 8 912 345-67-89. Пропустить — «-»"),
]

bot = Bot(TOKEN)
dp = Dispatcher()


def full_name(user) -> str | None:
    if user is None:
        return None
    parts = [user.first_name, user.last_name]
    return " ".join(p for p in parts if p) or None


def event_time(event) -> int:
    """Фактическое время события (unix, сек).

    Важно при досборе после простоя: сообщение, пришедшее вечером,
    утром должно записаться вечерним временем, а не временем обработки.
    MAX отдаёт timestamp в миллисекундах.
    """
    ts = getattr(event, "timestamp", None)
    if ts is None and hasattr(event, "message"):
        ts = getattr(event.message, "timestamp", None)
    if ts is None:
        return int(time.time())
    ts = int(ts)
    return ts // 1000 if ts > 10**12 else ts


@dp.bot_started()
async def on_start(event: BotStarted):
    user = event.from_user or event.user
    if user:
        db.upsert_user(user.user_id, full_name(user), getattr(user, "username", None))
        db.activate_user(user.user_id)
        print(f"+ подписчик лички: {user.user_id} {full_name(user)}", flush=True)
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=(
            "Вы подписаны на заявки ЛогистМОТ.\n"
            "Когда появится подходящий рейс — отвечайте на сообщение: "
            "сколько ТС готовы дать и телефон для связи.\n"
            "Отписаться: /stop"
        ),
    )


@dp.bot_added()
async def on_added(event: BotAdded):
    chat_id = getattr(event, "chat_id", None)
    print(f"# бот добавлен в чат: chat_id={chat_id}", flush=True)


@dp.bot_stopped()
async def on_bot_stopped(event: BotStopped):
    user = event.from_user or getattr(event, "user", None)
    if user:
        db.deactivate_user(user.user_id)
        print(f"- заблокировал бота: {user.user_id}", flush=True)


@dp.dialog_removed()
async def on_dialog_removed(event: DialogRemoved):
    user = getattr(event, "from_user", None) or getattr(event, "user", None)
    if user:
        db.deactivate_user(user.user_id)
        print(f"- удалил диалог: {user.user_id}", flush=True)


@dp.message_created()
async def on_message(event: MessageCreated):
    user = event.from_user
    if user is None or user.is_bot:
        return
    body = event.message.body
    text = (body.text or "").strip() if body else ""
    # у пересланных сообщений текст лежит в link.message.text
    link = getattr(event.message, "link", None)
    fwd_text = ""
    if link is not None and str(getattr(link, "type", "")).lower().endswith("forward"):
        lm = getattr(link, "message", None)
        fwd_text = (getattr(lm, "text", None) or "").strip() if lm else ""
    combined = (text + "\n" + fwd_text).strip() if fwd_text else text

    # ⚠️ Откуда пришло — считаем ДО обработки контакта. Раньше `is_dm`
    # вычислялся ниже, а блок «Отправить мой номер» его уже использовал:
    # телефон в базу попадал, а следом падал UnboundLocalError — человек не
    # получал «Спасибо, записал номер», логисты не получали пуш, и остальная
    # обработка сообщения обрывалась. В логах сервера 8 таких падений.
    recipient = event.message.recipient
    chat_type = getattr(recipient, "chat_type", None)
    is_dm = str(chat_type).lower().endswith("dialog") if chat_type else True
    source = "dm" if is_dm else "chat"

    # контакт, присланный кнопкой «Отправить мой номер»
    shared_phone = phone_from_attachments(event.message)
    if shared_phone:
        db.upsert_user(user.user_id, full_name(user), getattr(user, "username", None))
        db.set_phone(user.user_id, shared_phone)
        print(f"✓ контакт кнопкой: {user.user_id} {full_name(user)} {shared_phone}",
              flush=True)
        if is_dm:
            await event.message.answer(
                f"Спасибо! Записал номер {shared_phone}. Логист свяжется с вами.")
        det = pending_detail.get(user.user_id)
        await notify_logists(event.bot, offer_alert(
            full_name(user) or str(user.user_id), "оставил телефон",
            shared_phone, det[0] if det else None, "поделился контактом"))
        if not combined:
            return

    if not combined:
        return
    if not text:
        text = combined  # чистая пересылка без подписи

    # артефакты кнопки подписки — не отклики
    if text.lower() in ("начать", "start", "/start"):
        if is_dm:
            db.upsert_user(user.user_id, full_name(user), getattr(user, "username", None))
        return

    is_admin = user.user_id in ADMIN_IDS or user.user_id in db.db_admins()
    account = accounts.by_max_id(user.user_id)   # учётка панели, если есть

    # --- причина отказа по записке (после кнопки «Отклонить» в MAX) ---
    # Проверяем раньше всего остального: Пуганов не логист, и без этого его
    # сообщение легло бы в базу как отклик перевозчика.
    pr = pending_memo_reject.get(user.user_id)
    if pr and time.time() - pr[1] > MEMO_REJECT_TTL:
        pending_memo_reject.pop(user.user_id, None)
        pr = None
    if is_dm and pr:
        pending_memo_reject.pop(user.user_id, None)
        ok, reply = memo.decide_from_max(pr[0], user.user_id, False, text)
        await event.message.answer(reply)
        if ok:
            row = memo.as_dict(memo.get(pr[0]))
            print(f"# записка {row['number']}: {account['name'] if account else user.user_id} "
                  f"отклонил(а) через MAX: {text[:80]!r}", flush=True)
        return

    # --- сводка по запросу: «статистика», «неделя», «статистика 30» ---
    low0 = text.lower().strip()
    m_st = re.fullmatch(r"(?:статистика|сводка|неделя|итоги)(?:\s+(\d{1,3}))?", low0)
    if is_dm and m_st and (is_admin or account):
        days = int(m_st.group(1) or 7)
        days = max(1, min(days, 366))
        try:
            await event.message.answer(weekly.report(days, PANEL_LINK))
        except Exception as exc:  # noqa: BLE001
            await event.message.answer(f"Сводка не собралась: {exc}")
        return

    # сообщения логистов в чате: заявки-копипасты (без кнопки, вне учёта)
    # бот удаляет и объясняет логисту в личке; остальное просто не учитывает
    if not is_dm and is_admin:
        f = zayavka.parse_freeform(combined)
        looks_like_zayavka = (
            "ЗАЯВКА НА ПЕРЕВОЗКУ" in combined
            or f"— {zayavka.BRAND}" in combined
            or ("freeform" not in f and f.get("route")
                and (f.get("rate") or f.get("cargo") or f.get("dates"))
                and len(combined) > 40)
        )
        if looks_like_zayavka:
            mid = getattr(event.message.body, "mid", None) if event.message.body else None
            deleted = False
            if mid:
                try:
                    await event.bot.delete_message(mid)
                    deleted = True
                except Exception as exc:  # noqa: BLE001
                    print(f"# не удалил копию заявки логиста: {exc}", flush=True)
            print(f"# копия заявки логиста {user.user_id} в чате: "
                  f"{'удалена' if deleted else 'НЕ удалена'}", flush=True)
            head = ("Удалил вашу заявку из чата — она была без кнопки и не попадала в учёт."
                    if deleted else
                    "Вижу вашу заявку в чате — она без кнопки и не попадает в учёт.")
            try:
                await event.bot.send_message(
                    user_id=user.user_id,
                    text=head + "\n\nКак правильно:\n"
                         "• новая заявка — пришлите мне «заявка» + текст, опубликую "
                         "с кнопкой и учётом откликов;\n"
                         "• поднять/обновить существующую — «повторить N новые даты»;\n"
                         "• проще всего: перешлите эту заявку мне в личку — "
                         "я распознаю её и отправлю заново с кнопкой.\n\n"
                         "Ваш текст (чтобы не потерялся):\n\n" + combined[:1500])
            except Exception:  # noqa: BLE001 — логист мог не подписаться на бота
                pass
        return

    if is_dm and text.lower() == "/stop":
        db.deactivate_user(user.user_id)
        print(f"- отписался: {user.user_id}", flush=True)
        await event.message.answer("Вы отписались от личных заявок. Вернуться: «Старт».")
        return

    # --- команды логиста (только в личке; ADMIN_IDS из .env + назначенные) ---
    if is_dm and is_admin:
        low = text.lower()

        # --- мастер «вопрос-ответ» ---
        st = wizard.get(user.user_id)
        if st and time.time() - st["ts"] > WIZARD_TTL:
            wizard.pop(user.user_id, None)
            st = None

        if low == "заявка":  # одно слово без текста → запускаем мастер
            wizard[user.user_id] = {"step": 0, "fields": {}, "ts": time.time()}
            await event.message.answer(
                "Составляем заявку по шагам. Прервать — «отмена».\n\n"
                + WIZARD_STEPS[0][1])
            return

        if st is not None:
            if low in ("отмена", "нет"):
                wizard.pop(user.user_id, None)
                await event.message.answer("Мастер отменён.")
                return
            key, _ = WIZARD_STEPS[st["step"]]
            val = text.strip()
            if val == "-":
                if key == "route":
                    await event.message.answer(
                        "Маршрут пропустить нельзя. Например: Покачи - Полевской")
                    return
                if key == "contacts":
                    prev = db.get_pref_contacts(user.user_id)
                    if prev:
                        st["fields"]["contacts"] = prev
            else:
                if key == "route" and "→" not in val:
                    val = re.sub(r"\s*[-–—]+\s*", " → ", val, count=1)
                st["fields"][key] = val
            st["step"] += 1
            st["ts"] = time.time()
            if st["step"] < len(WIZARD_STEPS):
                next_key, prompt = WIZARD_STEPS[st["step"]]
                if next_key == "contacts":
                    prev = db.get_pref_contacts(user.user_id)
                    if prev:
                        prompt += f"\n«-» подставит прошлые: {prev}"
                await event.message.answer(prompt)
                return
            wizard.pop(user.user_id, None)
            fields, body = preview_text(st["fields"])
            pending_zayavki[user.user_id] = fields
            await event.message.answer(body)
            return

        # «заявка!» — мгновенная отправка без превью и подтверждения
        if low.startswith("заявка!"):
            body = text.split("!", 1)[1].strip()
            fields = zayavka.parse_zayavka_message("заявка\n" + body) if body else None
            if not fields:
                await event.message.answer(zayavka.HELP_TEXT)
                return
            fields, fixes = zayavka.fix_typos(fields)
            summary = await zayavka.dispatch_zayavka(
                event.bot, fields, author_id=user.user_id,
                author_name=full_name(user))
            if fields.get("contacts"):
                db.set_pref_contacts(user.user_id, fields["contacts"])
            print(f"# {summary} (мгновенно, логист {user.user_id})", flush=True)
            note = ("\n✏️ Поправил опечатки: " + "; ".join(fixes[:4])) if fixes else ""
            await event.message.answer(summary + note)
            return

        # управление списком логистов: «логист дарья», «убрать логиста дарья», «логисты»
        if low == "логисты":
            names = []
            for aid in sorted(ADMIN_IDS | db.db_admins()):
                row = db.find_user(str(aid))
                nm = row["name"] if row and not isinstance(row, list) else None
                names.append(f"• {nm or aid} (id {aid})")
            await event.message.answer("Логисты:\n" + "\n".join(names) +
                                       "\n\nДобавить: «логист имя-или-id». Убрать: «убрать логиста имя-или-id».")
            return
        grant = low.startswith("логист ") and not low.startswith("логисты")
        revoke = low.startswith("убрать логиста ")
        if grant or revoke:
            query = text.split(maxsplit=2)[-1].strip() if revoke else text.split(maxsplit=1)[1].strip()
            found = db.find_user(query)
            if found is None:
                await event.message.answer(
                    f"Не нашёл «{query}» в базе. Человек должен сначала написать боту.")
                return
            if isinstance(found, list):
                opts = "\n".join(f"• {r['name']} — id {r['user_id']}" for r in found[:10])
                await event.message.answer(
                    "Нашёл несколько, уточните по id:\n" + opts +
                    f"\n\nНапример: «{'убрать логиста' if revoke else 'логист'} {found[0]['user_id']}»")
                return
            if revoke:
                db.remove_admin(found["user_id"])
                await event.message.answer(f"{found['name']} (id {found['user_id']}) больше не логист.")
            else:
                db.add_admin(found["user_id"])
                await event.message.answer(
                    f"{found['name']} (id {found['user_id']}) теперь логист — "
                    f"может отправлять заявки. Пусть напишет боту «помощь» для проверки.")
            print(f"# {'−' if revoke else '+'} логист: {found['user_id']} {found['name']} "
                  f"(назначил {user.user_id})", flush=True)
            return
        # у черновика можно поправить даты одной строкой: «31.07-1.08»
        if user.user_id in pending_zayavki:
            m = re.fullmatch(
                r"(ежедневно|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?"
                r"(?:\s*[-–—]\s*\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)?)",
                text, re.IGNORECASE)
            if m:
                pending_zayavki[user.user_id]["dates"] = m.group(1)
                await event.message.answer(
                    "Даты обновил. Проверьте:\n\n"
                    + zayavka.format_zayavka(pending_zayavki[user.user_id])
                    + "\n\n— Отправить всем: «отправить». Отменить: «отмена».")
                return

        if low in ("отправить", "да") and user.user_id in pending_zayavki:
            fields = pending_zayavki.pop(user.user_id)
            await event.message.answer("Отправляю…")
            summary = await zayavka.dispatch_zayavka(
                event.bot, fields, author_id=user.user_id,
                author_name=full_name(user))
            if fields.get("contacts"):
                db.set_pref_contacts(user.user_id, fields["contacts"])
            print(f"# {summary} (логист {user.user_id})", flush=True)
            await event.message.answer(summary)
            return
        if low in ("отмена", "нет") and user.user_id in pending_zayavki:
            pending_zayavki.pop(user.user_id)
            await event.message.answer("Черновик удалён.")
            return
        if low.startswith("заявка"):
            parsed = zayavka.parse_zayavka_message(text)
            if not parsed:
                await event.message.answer(zayavka.HELP_TEXT)
                return
            fields, body = preview_text(parsed)
            pending_zayavki[user.user_id] = fields
            await event.message.answer(body)
            return
        if low == "заявки":
            acts = db.active_broadcasts(REPLY_WINDOW_SECONDS, limit=10)
            if not acts:
                await event.message.answer("Активных заявок нет.")
                return
            lines = [f"• {broadcast_label(b)}" for b in acts]
            await event.message.answer(
                "Активные заявки:\n" + "\n".join(lines) +
                "\n\nЗакрыть (машины набраны/рейс отменён): «закрыть 9».")
            return

        if low.startswith("закрыть "):
            arg = text.split(maxsplit=1)[1].strip().lstrip("#№")
            b = db.get_broadcast(int(arg)) if arg.isdigit() else None
            if b is None:
                await event.message.answer(
                    "Не нашёл такую заявку. Список активных — «заявки».")
                return
            db.set_broadcast_closed(b["id"])
            removed = ""
            if b["chat_mid"]:
                try:
                    await event.bot.delete_message(b["chat_mid"])
                    removed = " Сообщение удалено из чата."
                except Exception as exc:  # noqa: BLE001
                    removed = f" (удалить из чата не вышло: {exc})"
            try:
                await zayavka.refresh_digest(event.bot)
            except Exception:  # noqa: BLE001
                pass
            print(f"# заявка #{b['id']} закрыта логистом {user.user_id}", flush=True)
            await event.message.answer(
                f"Заявка {broadcast_label(b)} закрыта: новые отклики к ней "
                f"не привязываются, из уточняющего списка убрана, "
                f"из закрепа исключена.{removed}")
            return

        if low.startswith("повторить"):
            args = text.split()
            b = (db.get_broadcast(int(args[1].lstrip("#№")))
                 if len(args) >= 2 and args[1].lstrip("#№").isdigit() else None)
            if b is None:
                await event.message.answer(
                    "Формат: «повторить 12 30.07-31.07» — заявка #12 уйдёт заново "
                    "с новыми датами. Номера заявок — в напоминании или на панели.")
                return
            fields = {k: b[k] for k in ("route", "cargo", "rate") if b[k]}
            new_dates = " ".join(args[2:]).strip()
            if new_dates:
                fields["dates"] = new_dates
            elif b["dates"]:
                fields["dates"] = b["dates"]
            prev = db.get_pref_contacts(user.user_id)
            if prev:
                fields["contacts"] = prev
            db.mark_reminded(b["id"])
            fields, body = preview_text(fields, f"Повтор заявки #{b['id']}. Проверьте:")
            pending_zayavki[user.user_id] = fields
            await event.message.answer(body)
            return

        if low.startswith("удалить"):
            parts = text.split()
            arg = parts[-1].lstrip("#№") if len(parts) > 1 else ""
            if arg.isdigit() and db.get_broadcast(int(arg)):
                b = db.get_broadcast(int(arg))
                kb = (InlineKeyboardBuilder()
                      .row(CallbackButton(text=f"📥 Да, в архив #{b['id']}",
                                          payload=f"delok:{b['id']}")))
                await event.message.answer(
                    f"Перенести в архив заявку {broadcast_label(b)}?\n"
                    "Она исчезнет из чата и из активных; отклики и статистика "
                    "сохранятся (вкладка «Архив» на панели).",
                    attachments=[kb.as_markup()])
            else:
                rows = db.recent_broadcasts(10)
                if not rows:
                    await event.message.answer("Заявок нет.")
                    return
                kb = InlineKeyboardBuilder()
                for b in rows:
                    status = ("закрыта" if b["closed"]
                              else "активна" if db.broadcast_is_active(b, REPLY_WINDOW_SECONDS)
                              else "истекла")
                    kb.row(CallbackButton(
                        text=f"📥 {broadcast_label(b)} · {status}"[:48],
                        payload=f"del:{b['id']}"))
                await event.message.answer(
                    "Какую заявку перенести в архив? Нажмите "
                    "(дальше спрошу подтверждение):",
                    attachments=[kb.as_markup()])
            return

        if low == "дайджест":
            await event.message.answer("Запускаю утренний цикл…")
            removed, posted, reminders = await daily_routine(event.bot)
            await event.message.answer(
                f"Готово: убрано из чата {removed}, рейсов в дайджесте {posted}, "
                f"напоминаний {reminders}.")
            return

        if low in ("помощь", "/help", "шаблон"):
            await event.message.answer(zayavka.HELP_TEXT)
            return

        # --- копипаста или пересылка заявки в личку → черновик новой ---
        cardish = ("ЗАЯВКА НА ПЕРЕВОЗКУ" in combined
                   or f"— {zayavka.BRAND}" in combined)
        fields = zayavka.parse_card(combined) if cardish else None
        if fields is None and len(combined) > 60:
            f0 = zayavka.parse_freeform(combined)
            if "freeform" not in f0 and f0.get("route") and (
                    f0.get("rate") or f0.get("cargo")):
                fields = f0
        if fields:
            fields, body = preview_text(fields, "Распознал заявку. Проверьте:")
            pending_zayavki[user.user_id] = fields
            print(f"# черновик из копии/пересылки (логист {user.user_id}): "
                  f"{fields.get('route')}", flush=True)
            await event.message.answer(
                body + "\nДругие даты? Пришлите одной строкой, например: 31.07-1.08")
            return

    # участников чата тоже заносим в базу (но личку им слать нельзя,
    # пока сами не нажмут «Старт» — active выставляет только BotStarted)
    uname = getattr(user, "username", None)
    if is_dm:
        db.upsert_user(user.user_id, full_name(user), uname)
    else:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO users (user_id, name, joined_at, active, username)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       name = excluded.name,
                       username = COALESCE(excluded.username, users.username)""",
                (user.user_id, full_name(user), int(time.time()), uname),
            )

    now = event_time(event)

    # --- ответ цифрой на вопрос «по какому рейсу?» ---
    cl = pending_clarify.get(user.user_id)
    if cl and time.time() - cl["ts"] > CLARIFY_TTL:
        pending_clarify.pop(user.user_id, None)
        cl = None
    if is_dm and cl and text.strip().isdigit():
        n = int(text.strip())
        if 1 <= n <= len(cl["options"]):
            chosen = cl["options"][n - 1]
            db.reassign_reply(cl["reply_id"], chosen, now)
            pending_clarify.pop(user.user_id, None)
            pending_detail[user.user_id] = (chosen, time.time())
            label = broadcast_label(db.get_broadcast(chosen))
            print(f"↪ уточнение: ответ {cl['reply_id']} → заявка {label} "
                  f"({user.user_id})", flush=True)
            await event.message.answer(f"Понял, ваш отклик — по рейсу {label}. "
                                       "Передаю логисту.")
            return
        # цифра вне списка — считаем обычным сообщением (например, «5 машин»)

    # --- привязка: после кнопки/уточнения следующие сообщения идут к той заявке ---
    det = pending_detail.get(user.user_id)
    if det and time.time() - det[1] > DETAIL_TTL:
        pending_detail.pop(user.user_id, None)
        det = None
    override = det[0] if det else None

    rid, bid, relevant = db.record_reply(user.user_id, text, source, now,
                                         REPLY_WINDOW_SECONDS, bid_override=override)

    phone = db.extract_phone(text)
    if phone:
        db.set_phone(user.user_id, phone)

    tag = f"заявка #{bid}" if bid else "вне окна заявки"
    ph = f", тел {phone}" if phone else ""
    rel = "" if relevant else " [шум]"
    print(f"✓ ответ [{source}]{rel} {user.user_id} {full_name(user)} ({tag}{ph}): {text[:60]!r}",
          flush=True)

    # пуш логистам: только содержательные предложения (не любой «спасибо»)
    if not is_admin and relevant and (db.parse_offer(text) or phone
                                      or db.looks_like_offer(text)):
        await notify_logists(event.bot, offer_alert(
            full_name(user) or str(user.user_id), db.parse_offer(text),
            phone, bid, text))

    if not (is_dm and bid and relevant):
        return

    with db.connect() as conn:
        row = conn.execute(
            "SELECT phone FROM users WHERE user_id = ?", (user.user_id,)
        ).fetchone()
    has_phone = bool(phone or (row and row["phone"]))
    offer = db.parse_offer(text)
    got = f" ({offer})" if offer else ""
    phone_ask = ("" if has_phone else
                 "\n\nИ оставьте номер телефона — он нужен, чтобы логист связался "
                 "и уточнил детали. Нажмите кнопку ниже или напишите номер вручную.")

    # --- неоднозначность: активных заявок несколько, привязка неточная ---
    opts = db.active_broadcasts(REPLY_WINDOW_SECONDS) if override is None else []
    if len(opts) >= 2:
        pending_clarify[user.user_id] = {
            "reply_id": rid, "options": [o["id"] for o in opts], "ts": time.time(),
        }
        kb = InlineKeyboardBuilder()
        for o in opts:
            kb.row(CallbackButton(
                text=broadcast_label(o)[:48],
                payload=f"pick:{rid}:{o['id']}"))
        await event.message.answer(
            f"✅ Принято{got}. По какому рейсу? Нажмите на нужный:" + phone_ask,
            attachments=[kb.as_markup()])
        return

    if has_phone:
        await event.message.answer(
            f"✅ Принято{got}. Передаю логисту — с вами свяжутся.")
    else:
        await event.message.answer(f"✅ Принято{got}.{phone_ask}",
                                   attachments=[contact_kb()])


async def daily_routine(botobj):
    """Утренний цикл (10:00): уборка чата → дайджест с кнопками → напоминания."""
    # 1) удалить из чата сообщения неактуальных заявок
    removed = 0
    for b in db.stale_chat_messages(REPLY_WINDOW_SECONDS):
        try:
            await botobj.delete_message(b["chat_mid"])
            removed += 1
        except Exception:  # noqa: BLE001 — уже удалено вручную и т.п.
            pass
        db.clear_chat_mid(b["id"])

    # 2-3) свежий дайджест с кнопками, закрепляется в шапке чата
    try:
        posted = await zayavka.refresh_digest(botobj, repost=True)
    except Exception as exc:  # noqa: BLE001
        posted = 0
        print(f"# дайджест не отправлен: {exc}", flush=True)

    # 4) напоминания логистам об истёкших без откликов
    stale = db.expired_unanswered(REPLY_WINDOW_SECONDS)
    if stale:
        for b in stale:
            db.mark_reminded(b["id"])
        await notify_logists(botobj, mf.reminder(
            [(broadcast_label(b), b["id"]) for b in stale], PANEL_LINK))

    print(f"# утренняя рутина: убрано из чата {removed}, в дайджесте {posted}, "
          f"напоминаний {len(stale)}", flush=True)
    return removed, posted, len(stale)


async def daily_loop():
    if DIGEST_HOUR < 0:
        return
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await daily_routine(bot)
        except Exception as exc:  # noqa: BLE001
            print(f"# ошибка утренней рутины: {exc}", flush=True)


async def weekly_loop():
    """Сводка за неделю руководителям (head, chief, director) в понедельник
    в WEEKLY_HOUR:05 — после утренней рутины, чтобы цифры были свежими.
    Защита от повтора после перезапуска — отметка недели в kv."""
    if WEEKLY_DAY < 0:
        return
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=WEEKLY_HOUR, minute=5, second=0, microsecond=0)
        days_ahead = (WEEKLY_DAY - now.weekday()) % 7
        target += dt.timedelta(days=days_ahead)
        if target <= now:
            target += dt.timedelta(days=7)
        await asyncio.sleep((target - now).total_seconds())
        stamp = dt.date.today().isocalendar()
        key = f"weekly_sent:{stamp[0]}-{stamp[1]}"
        if db.get_kv(key):
            continue
        try:
            text = weekly.report(7, PANEL_LINK)
            targets = set()
            for role in ("head", "chief", "director"):
                targets.update(accounts.role_max_ids(role))
            sent, failed = 0, []
            for uid in sorted(targets):
                err = await send_dm_retry(bot, uid, text)
                if err is None:
                    sent += 1
                else:
                    failed.append(f"{uid} ({err})")
            db.set_kv(key, str(int(time.time())))
            print(f"# недельная сводка: ушла {sent}"
                  + (f", не дошло: {'; '.join(failed)}" if failed else ""), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"# ошибка недельной сводки: {exc}", flush=True)


async def digest_watchdog():
    """Раз в 20 минут проверяет закреп: если его удалили вручную —
    публикует и закрепляет заново (пока есть актуальные заявки)."""
    while True:
        await asyncio.sleep(1200)
        try:
            if not db.active_broadcasts(REPLY_WINDOW_SECONDS, limit=1):
                continue
            if await zayavka.digest_alive(bot):
                continue
            n = await zayavka.refresh_digest(bot, repost=True)
            print(f"# закреп с заявками пропал — восстановлен, рейсов: {n}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"# сторож дайджеста: {exc}", flush=True)


@dp.message_callback()
async def on_button(event: MessageCallback):
    """Нажатие «🚛 Готов взять» под заявкой — точная привязка отклика."""
    payload = (event.callback.payload or "") if event.callback else ""
    user = event.callback.user if event.callback else None
    known = any(payload.startswith(p) for p in
                ("take:", "pick:", "del:", "delok:",
                 memo.CB_OK + ":", memo.CB_NO + ":"))
    if user is None or getattr(user, "is_bot", False) or not known:
        try:
            await event.ack()
        except Exception:  # noqa: BLE001
            pass
        return
    now = event_time(event)

    # --- согласование записки прямо из MAX ---
    if payload.startswith((memo.CB_OK + ":", memo.CB_NO + ":")):
        try:
            mid = int(payload.split(":", 1)[1])
        except ValueError:
            mid = 0
        approve = payload.startswith(memo.CB_OK)
        if approve:
            ok, reply = memo.decide_from_max(mid, user.user_id, True)
        else:
            ok, reply = memo.can_decide_from_max(mid, user.user_id)
        try:
            await event.ack()
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            await send_dm_retry(event.bot, user.user_id, reply)
            return
        if approve:
            row = memo.as_dict(memo.get(mid))
            acc = accounts.by_max_id(user.user_id)
            print(f"# записка {row['number']}: {acc['name'] if acc else user.user_id} "
                  f"согласовал(а) через MAX — {row['status']}", flush=True)
            await send_dm_retry(event.bot, user.user_id, reply)
            await send_memo_notice(event.bot, row, "approved")
        else:
            pending_memo_reject[user.user_id] = (mid, time.time())
            await send_dm_retry(
                event.bot, user.user_id,
                f"✗ {mf.b(f'Отклонение {reply}')}\n"
                f"Напишите причину одним сообщением — она уйдёт логисту, "
                f"и записка вернётся на доработку.\n"
                f"Передумали — не отвечайте, запрос сгорит через 30 минут.")
        return

    # удаление заявки (только логисты)
    if payload.startswith(("del:", "delok:")):
        if not (user.user_id in ADMIN_IDS or user.user_id in db.db_admins()):
            try:
                await event.ack(notification="Только для логистов.")
            except Exception:  # noqa: BLE001
                pass
            return
        bid = int(payload.split(":", 1)[1])
        b = db.get_broadcast(bid)
        if b is None:
            try:
                await event.ack(notification="Заявка уже удалена.")
            except Exception:  # noqa: BLE001
                pass
            return
        if payload.startswith("del:"):  # шаг подтверждения
            kb = (InlineKeyboardBuilder()
                  .row(CallbackButton(text=f"📥 Да, в архив #{bid}",
                                      payload=f"delok:{bid}")))
            try:
                await event.ack()
                await event.bot.send_message(
                    user_id=user.user_id,
                    text=f"Перенести в архив заявку {broadcast_label(b)}?\n"
                         "Она исчезнет из чата и из активных; отклики и "
                         "статистика сохранятся.",
                    attachments=[kb.as_markup()])
            except Exception:  # noqa: BLE001
                pass
            return
        label = broadcast_label(b)
        db.set_broadcast_closed(bid)
        if b["chat_mid"]:
            try:
                await event.bot.delete_message(b["chat_mid"])
            except Exception:  # noqa: BLE001
                pass
            db.clear_chat_mid(bid)
        try:
            await zayavka.refresh_digest(event.bot)
        except Exception:  # noqa: BLE001
            pass
        print(f"# заявка #{bid} перенесена в архив логистом {user.user_id}", flush=True)
        try:
            await event.ack(notification=f"В архиве: {label}"[:90])
            await event.bot.send_message(
                user_id=user.user_id,
                text=f"Заявка {label} перенесена в архив: из чата убрана, "
                     "отклики и статистика сохранены (панель → Заявки → Архив).")
        except Exception:  # noqa: BLE001
            pass
        return

    # «pick» — перевозчик кнопкой выбрал, к какому рейсу относится его отклик
    if payload.startswith("pick:"):
        _, rid_s, bid_s = payload.split(":", 2)
        rid, bid = int(rid_s), int(bid_s)
        db.reassign_reply(rid, bid, now)
        pending_clarify.pop(user.user_id, None)
        pending_detail[user.user_id] = (bid, time.time())
        label = broadcast_label(db.get_broadcast(bid))
        print(f"↪ выбор кнопкой: ответ {rid} → {label} ({user.user_id})", flush=True)
        try:
            await event.ack(notification=f"Записал: {label}"[:90])
        except Exception:  # noqa: BLE001
            pass
        try:
            await event.bot.send_message(
                user_id=user.user_id,
                text=f"Понял, ваш отклик — по рейсу {label}. Передаю логисту.")
        except Exception:  # noqa: BLE001
            pass
        return

    bid = int(payload.split(":", 1)[1])

    # нажатия логистов — проверки, а не отклики: не записываем и не уведомляем
    if user.user_id in ADMIN_IDS or user.user_id in db.db_admins():
        try:
            await event.ack(notification="Кнопка работает (тест логиста, в учёт не идёт).")
        except Exception:  # noqa: BLE001
            pass
        return

    # заносим в базу, не меняя статус подписки (кнопку жмут и из чата)
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO users (user_id, name, joined_at, active, username)
               VALUES (?, ?, ?, 0, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   name = excluded.name,
                   username = COALESCE(excluded.username, users.username)""",
            (user.user_id, full_name(user), now, getattr(user, "username", None)),
        )

    label = broadcast_label(db.get_broadcast(bid))

    # повторное нажатие по той же заявке — не плодим карточки у логиста
    if db.has_button_reply(user.user_id, bid):
        pending_detail[user.user_id] = (bid, time.time())
        print(f"↺ повторное нажатие {user.user_id} по {label} — пропущено", flush=True)
        try:
            await event.ack(notification=f"Ваш отклик по рейсу {label} уже принят. "
                            "Логист свяжется с вами."[:90])
        except Exception:  # noqa: BLE001
            pass
        return

    db.record_reply(user.user_id, "Готов взять (кнопка)", "button", now,
                    REPLY_WINDOW_SECONDS, bid_override=bid)
    pending_detail[user.user_id] = (bid, time.time())
    print(f"✓ кнопка «Готов взять» {user.user_id} {full_name(user)} → {label}",
          flush=True)

    with db.connect() as conn:
        prow = conn.execute("SELECT phone FROM users WHERE user_id = ?",
                            (user.user_id,)).fetchone()
    await notify_logists(event.bot, offer_alert(
        full_name(user) or str(user.user_id), "готов взять",
        prow["phone"] if prow else None, bid, "нажал(а) кнопку под заявкой"))

    # просим детали в личке; если человек не подписан — личка не уйдёт,
    # тогда вся инструкция должна поместиться во всплывашку
    with db.connect() as conn:
        row = conn.execute("SELECT phone, active FROM users WHERE user_id = ?",
                           (user.user_id,)).fetchone()
    has_phone = bool(row and row["phone"])
    subscribed = bool(row and row["active"])
    ask_phone = "" if has_phone else " и номер телефона для связи"
    dm_ok = False
    try:
        await event.bot.send_message(
            user_id=user.user_id,
            text=f"Вы откликнулись на рейс {label}.\n"
                 f"Напишите, сколько ТС даёте{ask_phone}."
                 + ("" if has_phone else
                    "\nНомер можно отправить кнопкой ниже — одним нажатием."),
            attachments=None if has_phone else [contact_kb()])
        dm_ok = True
    except Exception:  # noqa: BLE001 — не подписан
        pass
    try:
        if dm_ok:
            await event.ack(notification="Принято! Ответьте боту в личке: сколько ТС"
                            + ("" if has_phone else " и телефон") + ".")
        else:
            await event.ack(notification="Отклик принят, но связаться с вами мы пока "
                            "не можем! Нажмите «Подписаться на заявки» внизу этого "
                            "сообщения (или откройте бота и нажмите «Начать») — "
                            "иначе логист не получит ваш контакт.")
    except Exception:  # noqa: BLE001
        pass


async def main():
    db.init_db()
    asyncio.create_task(daily_loop())        # утренняя рутина
    asyncio.create_task(digest_watchdog())   # восстановление закрепа
    asyncio.create_task(weekly_loop())       # сводка руководителям по понедельникам
    print("ЛогистМОТ запущен, ждём события...", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
