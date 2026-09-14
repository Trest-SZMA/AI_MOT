#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка здоровья сервисов МетОптТорг с уведомлениями в MAX или Telegram.

Запуск по таймеру каждые 5 минут. Пишет в Telegram только когда что-то
изменилось (проблема появилась или ушла), чтобы не спамить. Утром — сводка.

  metoptorg-healthcheck            обычная проверка (алерт при изменении)
  metoptorg-healthcheck --summary  сводка за сутки (таймер 08:00)
  metoptorg-healthcheck --test     отправить тестовое сообщение
  metoptorg-healthcheck --setup    показать chat_id тех, кто писал боту
  metoptorg-healthcheck --print    только напечатать состояние, без отправки

Настройки: /etc/metoptorg-healthcheck.env.
  MAX_BOT_TOKEN, MAX_CHAT_ID — мессенджер MAX (botapi.max.ru), основной канал:
                               Telegram с сервера недоступен (заблокирован по сети).
  TG_BOT_TOKEN, TG_CHAT_ID   — Telegram; работает только через TG_PROXY=http://host:port.
Если заданы оба — шлём в оба.
"""
import json, os, shutil, subprocess, sys, datetime, urllib.request, urllib.parse

ENV_FILE = "/etc/metoptorg-healthcheck.env"
STATE_FILE = "/var/lib/metoptorg-healthcheck/state.json"
HOST = os.uname().nodename

# (юнит, порт, подпись)
SERVICES = [
    ("fines-service",          8077, "Штрафы"),
    ("metoptorg-portal",       8079, "Портал"),
    ("logistmot-panel",        8080, "ЛогистМОТ панель"),
    ("logistmot-bot",          None, "ЛогистМОТ бот"),
    ("metoptorg-ostatki",      8090, "Остатки"),
    ("metoptorg-kp",           8091, "Оценка КП"),
    ("metoptorg-realizaciya",  8092, "Реализация"),
    ("bp-service",             8093, "Бизнес-планы"),
    ("parser-bp",              8094, "Парсер БП"),
    ("metallompro",            8095, "MetalLomPro"),
    ("nginx",                  8443, "nginx (https БП)"),
    ("postgresql@16-main",     None, "PostgreSQL"),
]
# oneshot-задачи: (юнит, когда ждём результат — час, после которого сегодняшний запуск обязателен)
JOBS = [
    ("metoptorg-realizaciya-nightly", 5,  "Ночная сборка реализации"),
    ("metoptorg-kp-backup",           4,  "Бэкап КП"),
]
DISK_WARN, DISK_CRIT = 85, 92
MEM_MIN_MB = 300


def load_env():
    cfg = {}
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return cfg


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def http_ok(port):
    """Любой HTTP-ответ считаем живым (401/303 — это норма для наших сервисов)."""
    import http.client, ssl
    try:
        if port == 8443:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            c = http.client.HTTPSConnection("127.0.0.1", port, timeout=8, context=ctx)
        else:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        c.request("GET", "/")
        c.getresponse()
        return True
    except Exception:
        return False


def check():
    problems, info = [], {}
    for unit, port, label in SERVICES:
        _, act = sh("systemctl", "is-active", unit)
        if act != "active":
            problems.append(f"🔴 {label} ({unit}): {act}")
        elif port and not http_ok(port):
            problems.append(f"🟠 {label}: юнит активен, но порт {port} не отвечает")
    _, failed = sh("systemctl", "--failed", "--no-legend", "--plain")
    for line in failed.splitlines():
        u = line.split()[0]
        if u != "man-db.service":
            problems.append(f"🔴 юнит в failed: {u}")

    now = datetime.datetime.now()
    for unit, hour, label in JOBS:
        _, out = sh("systemctl", "show", unit, "-p", "Result", "-p", "ExecMainExitTimestamp")
        props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        res, ts = props.get("Result", ""), props.get("ExecMainExitTimestamp", "")
        ran_today = ts and now.strftime("%Y-%m-%d") in ts
        if now.hour >= hour and not ran_today:
            problems.append(f"🟠 {label}: сегодня ещё не отработала (последний запуск: {ts or 'нет'})")
        elif ran_today and res not in ("success", ""):
            problems.append(f"🔴 {label}: завершилась с ошибкой ({res})")
        info[unit] = f"{ts or '—'} / {res or '—'}"

    du = shutil.disk_usage("/")
    pct = round(du.used / du.total * 100)
    info["disk"] = f"{pct}% занято, свободно {du.free // 2**30} ГБ"
    if pct >= DISK_CRIT:
        problems.append(f"🔴 Диск / занят на {pct}%")
    elif pct >= DISK_WARN:
        problems.append(f"🟠 Диск / занят на {pct}%")

    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    avail_mb = mem.get("MemAvailable", 0) // 1024
    info["mem"] = f"доступно {avail_mb} МБ"
    if avail_mb < MEM_MIN_MB:
        problems.append(f"🟠 Мало памяти: доступно {avail_mb} МБ")

    _, la = sh("cut", "-d ", "-f1-3", "/proc/loadavg")
    info["load"] = la
    return problems, info


MAX_API = "https://botapi.max.ru"


def _tg_opener(cfg):
    proxy = cfg.get("TG_PROXY")
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    return urllib.request.build_opener(*handlers)


def max_send(cfg, text):
    """MAX Bot API: POST /messages?chat_id=… (или user_id=… для личного диалога)."""
    tok, chat = cfg.get("MAX_BOT_TOKEN"), cfg.get("MAX_CHAT_ID")
    if not tok or not chat:
        return None
    key = "user_id" if chat.startswith("u") else "chat_id"
    url = f"{MAX_API}/messages?{key}={chat.lstrip('u')}"
    body = json.dumps({"text": text[:4000]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print("MAX:", e, file=sys.stderr)
        return False


def tg_send(cfg, text):
    tok, chat = cfg.get("TG_BOT_TOKEN"), cfg.get("TG_CHAT_ID")
    if not tok or not chat:
        return None
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000],
                                   "disable_web_page_preview": "1"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
    try:
        with _tg_opener(cfg).open(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print("Telegram:", e, file=sys.stderr)
        return False


def send(cfg, text):
    """Шлёт во все настроенные каналы. True, если хоть один доставил."""
    results = [r for r in (max_send(cfg, text), tg_send(cfg, text)) if r is not None]
    if not results:
        print("Ни один канал не настроен в", ENV_FILE, file=sys.stderr)
    return any(results)


def setup(cfg):
    """Показывает id чатов/пользователей, писавших боту — для MAX_CHAT_ID / TG_CHAT_ID."""
    found = False
    if cfg.get("MAX_BOT_TOKEN"):
        found = True
        req = urllib.request.Request(f"{MAX_API}/updates?limit=100",
                                     headers={"Authorization": cfg["MAX_BOT_TOKEN"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            upd = json.load(r)
        seen = {}
        for u in upd.get("updates", []):
            m = u.get("message") or {}
            rcp, snd = m.get("recipient") or {}, m.get("sender") or {}
            if rcp.get("chat_type") == "dialog" or not rcp.get("chat_id"):
                if snd.get("user_id"):
                    seen[f"u{snd['user_id']}"] = f"личка: {snd.get('name')}"
            else:
                seen[str(rcp["chat_id"])] = f"чат: {rcp.get('chat_id')}"
            # приглашение бота в группу тоже приходит как update
            if u.get("chat_id") and u.get("update_type", "").startswith("bot_"):
                seen[str(u["chat_id"])] = "чат (бота добавили)"
        if not seen:
            print("MAX: бот пока не получал сообщений. Напишите ему в MAX и повторите.")
        for cid, name in seen.items():
            print(f"MAX_CHAT_ID={cid}   ({name})")
    if cfg.get("TG_BOT_TOKEN"):
        found = True
        try:
            with _tg_opener(cfg).open(f"https://api.telegram.org/bot{cfg['TG_BOT_TOKEN']}/getUpdates", timeout=15) as r:
                upd = json.load(r)
        except Exception as e:
            print(f"Telegram недоступен ({e}). Нужен TG_PROXY или используйте MAX."); upd = {}
        seen = {}
        for u in upd.get("result", []):
            m = u.get("message") or u.get("channel_post") or {}
            ch = m.get("chat") or {}
            if ch.get("id"):
                seen[ch["id"]] = ch.get("title") or ch.get("username") or ch.get("first_name")
        for cid, name in seen.items():
            print(f"TG_CHAT_ID={cid}   ({name})")
    if not found:
        print("Сначала положите MAX_BOT_TOKEN (или TG_BOT_TOKEN) в", ENV_FILE)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"problems": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_env()
    if mode == "--setup":
        return setup(cfg)
    if mode == "--test":
        ok = send(cfg, f"✅ {HOST}: тестовое сообщение healthcheck")
        print("отправлено" if ok else "не отправлено"); return

    problems, info = check()
    stamp = datetime.datetime.now().strftime("%d.%m %H:%M")
    if mode == "--print":
        print("\n".join(problems) or "всё в порядке"); print(json.dumps(info, ensure_ascii=False, indent=1)); return

    if mode == "--summary":
        head = "✅ Все сервисы в порядке" if not problems else f"⚠️ Проблем: {len(problems)}"
        text = (f"☀️ {HOST} — сводка {stamp}\n{head}\n" + "\n".join(problems) +
                f"\n\n💽 Диск: {info['disk']}\n🧠 Память: {info['mem']}\n📈 Load: {info['load']}\n" +
                "\n".join(f"🕓 {lbl}: {info.get(u)}" for u, _, lbl in JOBS))
        send(cfg, text); return

    st = load_state()
    prev = set(st.get("problems", []))
    cur = set(problems)
    appeared, resolved = cur - prev, prev - cur
    if appeared or resolved:
        parts = [f"🖥 {HOST} — {stamp}"]
        if appeared:
            parts.append("Новые проблемы:\n" + "\n".join(sorted(appeared)))
        if resolved:
            parts.append("Восстановилось:\n" + "\n".join("✅ " + p.split(" ", 1)[1] for p in sorted(resolved)))
        if not cur:
            parts.append("Сейчас всё в порядке.")
        send(cfg, "\n\n".join(parts))
    st["problems"] = sorted(cur)
    st["checked_at"] = stamp
    save_state(st)


if __name__ == "__main__":
    main()
