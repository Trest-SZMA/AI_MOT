#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мониторинг стека ai_mot в Docker: состояние контейнеров (running/healthy),
диск под данными; уведомления в MAX (или Telegram через прокси) только при
изменении состояния; утренняя сводка по расписанию из ofelia.ini.

  monitor.py --loop      проверка каждые 5 минут (команда контейнера monitor)
  monitor.py --once      одна проверка
  monitor.py --summary   сводка
  monitor.py --print     напечатать состояние без отправки
  monitor.py --setup     показать chat_id тех, кто писал боту
  monitor.py --test      тестовое сообщение

Переменные (env/monitor.env): MAX_BOT_TOKEN, MAX_CHAT_ID, TG_BOT_TOKEN, TG_CHAT_ID, TG_PROXY.
"""
import http.client, json, os, shutil, socket, sys, time, datetime, urllib.request, urllib.parse

PROJECT = os.environ.get("COMPOSE_PROJECT", "ai_mot")
HOST = os.environ.get("HOST_NAME", "ai_mot")
STATE_FILE = "/state/monitor.json"
DATA_MOUNT = "/data"
DISK_WARN, DISK_CRIT = 85, 92
MAX_API = "https://botapi.max.ru"
# контейнеры, чьё отсутствие/остановка — норма (одноразовые задачи планировщика)
IGNORE = set()


class DockerConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost")
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def docker_get(path):
    c = DockerConn(); c.request("GET", path); r = c.getresponse()
    data = json.loads(r.read()); c.close(); return data


def check():
    problems, info = [], {}
    filt = urllib.parse.quote(json.dumps({"label": [f"com.docker.compose.project={PROJECT}"]}))
    try:
        cs = docker_get(f"/containers/json?all=true&filters={filt}")
    except Exception as e:
        return [f"🔴 Docker API недоступен: {e}"], info
    if not cs:
        problems.append("🔴 контейнеры проекта не найдены")
    for c in cs:
        name = c["Names"][0].lstrip("/")
        if name in IGNORE: continue
        state, status = c.get("State"), c.get("Status", "")
        if state != "running":
            problems.append(f"🔴 {name}: {state} ({status})")
        elif "unhealthy" in status:
            problems.append(f"🟠 {name}: unhealthy ({status})")
        elif "Restarting" in status:
            problems.append(f"🟠 {name}: перезапускается ({status})")
        info[name] = status
    try:
        du = shutil.disk_usage(DATA_MOUNT)
        pct = round(du.used / du.total * 100)
        info["disk"] = f"{pct}% занято, свободно {du.free // 2**30} ГБ"
        if pct >= DISK_CRIT: problems.append(f"🔴 Диск с данными занят на {pct}%")
        elif pct >= DISK_WARN: problems.append(f"🟠 Диск с данными занят на {pct}%")
    except Exception:
        pass
    return problems, info


# ---------- отправка ----------
def _tg_opener():
    p = os.environ.get("TG_PROXY")
    return urllib.request.build_opener(*([urllib.request.ProxyHandler({"http": p, "https": p})] if p else []))


def max_send(text):
    tok, chat = os.environ.get("MAX_BOT_TOKEN"), os.environ.get("MAX_CHAT_ID")
    if not tok or not chat: return None
    key = "user_id" if chat.startswith("u") else "chat_id"
    req = urllib.request.Request(f"{MAX_API}/messages?{key}={chat.lstrip('u')}", method="POST",
                                 data=json.dumps({"text": text[:4000]}).encode(),
                                 headers={"Authorization": tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: return r.status == 200
    except Exception as e:
        print("MAX:", e, file=sys.stderr); return False


def tg_send(text):
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok or not chat: return None
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        with _tg_opener().open(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print("Telegram:", e, file=sys.stderr); return False


def send(text):
    res = [r for r in (max_send(text), tg_send(text)) if r is not None]
    if not res: print("Ни один канал не настроен (MAX_BOT_TOKEN/MAX_CHAT_ID)", file=sys.stderr)
    return any(res)


def setup():
    tok = os.environ.get("MAX_BOT_TOKEN")
    if not tok: print("Задайте MAX_BOT_TOKEN в env/monitor.env"); return
    req = urllib.request.Request(f"{MAX_API}/updates?limit=100", headers={"Authorization": tok})
    with urllib.request.urlopen(req, timeout=15) as r: upd = json.load(r)
    seen = {}
    for u in upd.get("updates", []):
        m = u.get("message") or {}; rcp, snd = m.get("recipient") or {}, m.get("sender") or {}
        if rcp.get("chat_type") == "dialog" or not rcp.get("chat_id"):
            if snd.get("user_id"): seen[f"u{snd['user_id']}"] = f"личка: {snd.get('name')}"
        else:
            seen[str(rcp["chat_id"])] = "чат"
    print("\n".join(f"MAX_CHAT_ID={k}   ({v})" for k, v in seen.items()) or "Бот ещё не получал сообщений — напишите ему в MAX.")


# ---------- логика ----------
def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {"problems": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True); json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False)


def run_once():
    problems, _ = check()
    stamp = datetime.datetime.now().strftime("%d.%m %H:%M")
    st = load_state(); prev, cur = set(st.get("problems", [])), set(problems)
    appeared, resolved = cur - prev, prev - cur
    if appeared or resolved:
        parts = [f"🐳 {HOST} — {stamp}"]
        if appeared: parts.append("Новые проблемы:\n" + "\n".join(sorted(appeared)))
        if resolved: parts.append("Восстановилось:\n" + "\n".join("✅ " + p.split(" ", 1)[1] for p in sorted(resolved)))
        if not cur: parts.append("Сейчас всё в порядке.")
        send("\n\n".join(parts))
    st.update(problems=sorted(cur), checked_at=stamp); save_state(st)
    return problems


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--once"
    if mode == "--setup": return setup()
    if mode == "--test": print("отправлено" if send(f"✅ {HOST}: тестовое сообщение monitor") else "не отправлено"); return
    if mode == "--print":
        p, info = check(); print("\n".join(p) or "всё в порядке"); print(json.dumps(info, ensure_ascii=False, indent=1)); return
    if mode == "--summary":
        p, info = check(); stamp = datetime.datetime.now().strftime("%d.%m %H:%M")
        head = "✅ Все контейнеры в порядке" if not p else f"⚠️ Проблем: {len(p)}"
        send(f"☀️ {HOST} — сводка {stamp}\n{head}\n" + "\n".join(p) + f"\n\n💽 {info.get('disk', '—')}\n" +
             "\n".join(f"• {k}: {v}" for k, v in info.items() if k != "disk")); return
    if mode == "--loop":
        while True:
            try: run_once()
            except Exception as e: print("monitor:", e, file=sys.stderr)
            time.sleep(300)
    run_once()


if __name__ == "__main__":
    main()
