"""Отчёты ЛогистМОТ.

    python reports.py                 # сводка по людям (отклик, телефоны)
    python reports.py --ignored 3     # кто молчал в последних 3 заявках (из лички)
    python reports.py --zayavka 5     # все ответы на заявку #5 (с телефонами)
    python reports.py --zayavki       # список заявок с числом откликов
"""

import argparse
import time

import db


def summary():
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT u.user_id, u.name, u.phone, u.active,
                      (SELECT COUNT(*) FROM deliveries d
                        WHERE d.user_id = u.user_id AND d.sent_ok = 1)  AS got,
                      (SELECT COUNT(*) FROM replies r
                        WHERE r.user_id = u.user_id)                    AS replies
               FROM users u
               ORDER BY replies DESC, got DESC"""
        ).fetchall()
    print(f"Людей в базе: {len(rows)}\n")
    print(f"{'user_id':>12}  {'имя':<22} {'личка':>5} {'ответов':>8}  {'телефон':<14} статус")
    print("-" * 78)
    for r in rows:
        status = "подписан" if r["active"] else "только чат"
        print(f"{r['user_id']:>12}  {(r['name'] or '')[:22]:<22} {r['got']:>5} "
              f"{r['replies']:>8}  {(r['phone'] or '—'):<14} {status}")


def zayavki():
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT b.id, b.route, b.dates, b.cargo, b.rate, b.sent_at, b.chat_posted,
                      (SELECT COUNT(*) FROM replies r WHERE r.broadcast_id = b.id) AS replies,
                      (SELECT COUNT(*) FROM deliveries d WHERE d.broadcast_id = b.id AND d.sent_ok=1) AS dm_sent
               FROM broadcasts b ORDER BY b.sent_at DESC"""
        ).fetchall()
    print(f"{'#':>4}  {'дата':<11} {'маршрут':<28} {'груз':<10} {'ставка':<22} {'чат':>3} {'личка':>5} {'ответов':>8}")
    print("-" * 100)
    for r in rows:
        d = time.strftime("%d.%m %H:%M", time.localtime(r["sent_at"]))
        print(f"{r['id']:>4}  {d:<11} {(r['route'] or '—')[:28]:<28} {(r['cargo'] or '—')[:10]:<10} "
              f"{(r['rate'] or '—')[:22]:<22} {'да' if r['chat_posted'] else '—':>3} {r['dm_sent']:>5} {r['replies']:>8}")


def zayavka(bid: int):
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM broadcasts WHERE id = ?", (bid,)).fetchone()
        if not b:
            print(f"Заявка #{bid} не найдена.")
            return
        rows = conn.execute(
            """SELECT r.at, r.text, r.source, r.relevant, u.name, u.phone, u.user_id
               FROM replies r JOIN users u ON u.user_id = r.user_id
               WHERE r.broadcast_id = ? ORDER BY r.relevant DESC, r.at""",
            (bid,),
        ).fetchall()
    offers = [r for r in rows if r["relevant"]]
    noise = [r for r in rows if not r["relevant"]]
    print(f"Заявка #{bid}: {b['route'] or ''} {b['dates'] or ''} {b['cargo'] or ''} "
          f"({b['rate'] or 'ставка не указана'})")
    print(f"Предложений: {len(offers)}  ·  прочих сообщений в чате: {len(noise)}\n")
    for r in offers:
        t = time.strftime("%d.%m %H:%M", time.localtime(r["at"]))
        ph = r["phone"] or "тел. не оставил"
        print(f"  [{t}] [{r['source']}] {r['name'] or r['user_id']} · {ph}")
        print(f"      {r['text'][:200]}")
    if noise:
        print(f"\n  — прочее (не похоже на предложения): "
              f"{', '.join(str(r['name'] or r['user_id']) for r in noise[:10])}"
              f"{' …' if len(noise) > 10 else ''}")


def ignored(last_n: int):
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM broadcasts ORDER BY sent_at DESC LIMIT ?", (last_n,))]
        if not ids:
            print("Заявок ещё не было.")
            return
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT u.user_id, u.name, u.phone,
                       COUNT(d.broadcast_id) AS got,
                       SUM(CASE WHEN d.replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replied
                FROM users u JOIN deliveries d
                  ON d.user_id = u.user_id AND d.sent_ok = 1 AND d.broadcast_id IN ({ph})
                WHERE u.active = 1
                GROUP BY u.user_id HAVING replied = 0
                ORDER BY got DESC""",
            ids,
        ).fetchall()
    print(f"Молчали во всех последних {len(ids)} заявках ({len(rows)} чел.):\n")
    for r in rows:
        print(f"  {r['user_id']:>12}  {(r['name'] or '')[:24]:<24} {(r['phone'] or '—'):<14} (получил {r['got']})")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Отчёты ЛогистМОТ")
    p.add_argument("--ignored", type=int, metavar="N")
    p.add_argument("--zayavka", type=int, metavar="ID")
    p.add_argument("--zayavki", action="store_true")
    a = p.parse_args()
    db.init_db()
    if a.zayavka:
        zayavka(a.zayavka)
    elif a.zayavki:
        zayavki()
    elif a.ignored:
        ignored(a.ignored)
    else:
        summary()
