"""Сводка за неделю для руководителей — уходит ботом в MAX по понедельникам
и по команде «статистика» в личку.

Только то, что база знает точно. Ставки к прайсу берутся из служебных
записок (там ставка разобрана на число, единицу и НДС); у заявок ставка —
свободный текст, её к прайсу не прикладываем.
"""
from __future__ import annotations

import datetime as dt
import time

import accounts
import db
import maxfmt as mf
import memo

WEEK = 7 * 86400


def _span(days: int) -> tuple[int, int, int]:
    """(с, по, предыдущее «с») — для сравнения с прошлым периодом."""
    end = int(time.time())
    start = end - days * 86400
    return start, end, start - days * 86400


def _d(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m")


def _delta(now: int, before: int, days: int = 7) -> str:
    if before == 0:
        return ""
    diff = now - before
    prev = "прошлой неделе" if days == 7 else f"предыдущим {days} дням"
    if diff == 0:
        return f" (как за {prev.replace('прошлой неделе', 'прошлую неделю')})"
    return f" ({'+' if diff > 0 else ''}{diff} к {prev})"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def collect(days: int = 7) -> dict:
    start, end, prev = _span(days)
    with db.connect() as conn:
        q = conn.execute
        z_now = q("SELECT COUNT(*) FROM broadcasts WHERE sent_at >= ?", (start,)).fetchone()[0]
        z_prev = q("SELECT COUNT(*) FROM broadcasts WHERE sent_at >= ? AND sent_at < ?",
                   (prev, start)).fetchone()[0]
        # имя — из учётки панели по id в MAX: в author_name лежит имя из
        # профиля мессенджера («Елена» у Ольги, «Светлана ИВЦ»), а не ФИО
        by_author_raw = q(
            """SELECT author_id, author_name, COUNT(*) AS n
               FROM broadcasts WHERE sent_at >= ? GROUP BY 1, 2""",
            (start,)).fetchall()
        by_author: dict[str, int] = {}
        for aid, aname, n in by_author_raw:
            acc = accounts.by_max_id(aid) if aid else None
            label = memo_short(acc["name"]) if acc else (aname or "без автора")
            by_author[label] = by_author.get(label, 0) + n
        by_author = sorted(by_author.items(), key=lambda x: -x[1])
        routes = q("SELECT COUNT(DISTINCT route) FROM broadcasts WHERE sent_at >= ?",
                   (start,)).fetchone()[0]
        kinds = q("""SELECT kind, COUNT(*) FROM broadcasts WHERE sent_at >= ?
                     GROUP BY kind""", (start,)).fetchall()

        r_now = q("""SELECT COUNT(*), COUNT(DISTINCT user_id),
                            SUM(processed = 1)
                     FROM replies WHERE at >= ? AND relevant = 1""", (start,)).fetchone()
        r_prev = q("SELECT COUNT(*) FROM replies WHERE at >= ? AND at < ? AND relevant = 1",
                   (prev, start)).fetchone()[0]
        with_phone = q(
            """SELECT COUNT(DISTINCT r.user_id) FROM replies r
               JOIN users u ON u.user_id = r.user_id
               WHERE r.at >= ? AND r.relevant = 1 AND u.phone IS NOT NULL""",
            (start,)).fetchone()[0]
        silent = q(
            """SELECT b.id, b.route, b.dates FROM broadcasts b
               WHERE b.sent_at >= ? AND b.closed = 0
                 AND NOT EXISTS (SELECT 1 FROM replies r
                                 WHERE r.broadcast_id = b.id AND r.relevant = 1)
               ORDER BY b.id""", (start,)).fetchall()
        top = q(
            """SELECT b.id, b.route, b.dates, COUNT(r.id) AS n
               FROM broadcasts b JOIN replies r ON r.broadcast_id = b.id AND r.relevant = 1
               WHERE b.sent_at >= ? GROUP BY b.id ORDER BY n DESC LIMIT 3""",
            (start,)).fetchall()

        subs_new = q("SELECT COUNT(*) FROM users WHERE joined_at >= ? AND active = 1",
                     (start,)).fetchone()[0]
        subs_all = q("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0]
        base_all = q("SELECT COUNT(*) FROM users").fetchone()[0]

    ms = memo.stats(days=days)
    return {
        "start": start, "end": end,
        "z_now": z_now, "z_prev": z_prev, "by_author": by_author, "days": days,
        "routes": routes, "kinds": dict(kinds),
        "r_now": r_now[0] or 0, "r_users": r_now[1] or 0, "r_done": r_now[2] or 0,
        "r_prev": r_prev, "with_phone": with_phone,
        "silent": [tuple(r) for r in silent], "top": [tuple(r) for r in top],
        "subs_new": subs_new, "subs_all": subs_all, "base_all": base_all,
        "memo": ms,
    }


def render(d: dict, link: str) -> str:
    period = "недели" if d["days"] == 7 else f"{d['days']} дней"
    e = mf.esc
    lines = [f"📊 {mf.b(f'Итоги {period} {_d(d['start'])}–{_d(d['end'])}')}", ""]

    # --- заявки ---
    lines.append(mf.b(f"Заявки — {d['z_now']}") + _delta(d["z_now"], d["z_prev"], d["days"])
                 + (f", маршрутов {d['routes']}" if d["z_now"] else ""))
    if d["by_author"]:
        lines.append(" · ".join(f"{e(a)} — {n}" for a, n in d["by_author"]))
    special = {"urgent": "срочных", "rate_up": "ставка повышена",
               "new_route": "новых направлений"}
    sp = [f"{v} {d['kinds'][k]}" for k, v in special.items() if d["kinds"].get(k)]
    if sp:
        lines.append(" · ".join(sp))

    # --- отклики ---
    lines.append("")
    lines.append(mf.b(f"Отклики — {d['r_now']}") + _delta(d["r_now"], d["r_prev"], d["days"])
                 + (f", от {d['r_users']} перевозчиков, с телефоном {d['with_phone']}"
                    if d["r_now"] else ""))
    if d["r_now"]:
        lines.append(f"отмечено обработанными: {mf.b(f'{d['r_done']} из {d['r_now']}')}")
    if d["top"]:
        lines.append("больше всего откликов: " + "; ".join(
            f"{e(f'#{i} {r}')} — {n}" for i, r, dd, n in d["top"]))
    if d["silent"]:
        s_ = d["silent"]
        lines.append(f"без откликов ({len(s_)}): "
                     + ", ".join(e(f"#{i} {r}") for i, r, _ in s_[:5])
                     + (" …" if len(s_) > 5 else ""))

    # --- база ---
    lines.append("")
    lines.append(mf.b("Перевозчики") + f" — подписано на личку {d['subs_all']} из {d['base_all']}"
                 + (f", новых {d['subs_new']}" if d["subs_new"] else ""))

    # --- записки и ставки ---
    t = d["memo"]["totals"]
    lines.append("")
    if not t.get("memos"):
        lines.append(mf.b("Служебные записки") + " — за период не подавались")
    else:
        lines.append(mf.b(f"Служебные записки — {t['memos']}") + f": согласовано "
                     f"{t['approved'] or 0}, отклонено {t['rejected'] or 0}, "
                     f"на согласовании {t['pending'] or 0}")
        if t.get("avg_pct") is not None:
            lines.append(f"ставки к прайсу: в среднем {mf.b(mf.sign_pct(t['avg_pct']))}, "
                         f"выше прайса {t['over_count'] or 0} из "
                         f"{t['memos'] - (t['no_price'] or 0)}"
                         + (f", худший случай {mf.sign_pct(t['max_pct'])}"
                            if (t.get("max_pct") or 0) > 0 else ""))
        over = [r for r in d["memo"]["by_route"]
                if (r.get("avg_pct") or 0) > 0][:3]
        if over:
            lines.append("выше прайса: " + "; ".join(
                f"{e(r['route'])} {mf.sign_pct(r['avg_pct'])}" for r in over))
        if t.get("no_price"):
            lines.append(f"без сверки с прайсом: {t['no_price']} "
                         f"(маршрута или единицы нет в прайсе)")

    lines.append("")
    lines.append(mf.link("Аналитика на панели", f"{link}/#stats"))
    return "\n".join(lines)


def memo_short(full: str) -> str:
    """«Гамирзанова Светлана Александровна» → «Гамирзанова С. А.»"""
    parts = (full or "").split()
    if len(parts) < 2:
        return full or ""
    return parts[0] + " " + " ".join(p[0].upper() + "." for p in parts[1:3])


def report(days: int = 7, link: str = "") -> str:
    return render(collect(days), link)
