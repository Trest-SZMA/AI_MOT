"""Рассылка сообщения всем активным подписчикам.

Запуск:
    python broadcast.py "Текст сообщения"
    python broadcast.py --file message.txt

Пишет только тем, кто сам подписался на бота (нажал «Старт»). Это единственный
легальный и технически возможный способ: MAX не даёт боту писать первым тем,
кто с ним не общался.
"""

import argparse
import asyncio
import os
import time

from dotenv import load_dotenv
from maxapi import Bot

import db

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.environ["MAX_BOT_TOKEN"]
DELAY = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.1"))


async def send_to_user(bot: Bot, user_id: int, text: str) -> bool:
    """Отправить личное сообщение. Возвращает True при успехе."""
    try:
        await bot.send_message(user_id=user_id, text=text)
        return True
    except Exception as exc:  # noqa: BLE001 — логируем и продолжаем рассылку
        print(f"  не доставлено user_id={user_id}: {exc}")
        # Частая причина — пользователь заблокировал бота: помечаем неактивным.
        db.deactivate_user(user_id)
        return False


async def run(text: str):
    db.init_db()
    users = db.active_users()
    if not users:
        print("Нет активных подписчиков. Рассылать некому.")
        return

    broadcast_id = db.create_broadcast(text)
    bot = Bot(TOKEN)
    ok = 0
    print(f"Рассылка #{broadcast_id} — {len(users)} получателей")
    for row in users:
        sent = await send_to_user(bot, row["user_id"], text)
        db.record_delivery(broadcast_id, row["user_id"], sent)
        ok += int(sent)
        await asyncio.sleep(DELAY)  # держимся ниже лимита ~30 req/sec
    print(f"Готово: доставлено {ok} из {len(users)}.")


def read_text() -> str:
    parser = argparse.ArgumentParser(description="Рассылка подписчикам бота MAX")
    parser.add_argument("text", nargs="?", help="Текст сообщения")
    parser.add_argument("--file", help="Прочитать текст из файла")
    args = parser.parse_args()
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return f.read().strip()
    if args.text:
        return args.text
    parser.error("Укажите текст сообщения или --file")


if __name__ == "__main__":
    asyncio.run(run(read_text()))
