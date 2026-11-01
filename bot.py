#!/usr/bin/env python3
"""
Бот для GitHub Actions: следит за 2ch.hk/<доска>/, постит НОВЫЕ треды в
Telegram-канал, а в комментариях (чат обсуждений) воспроизводит ответы из треда.

Один запуск живёт RUN_BUDGET секунд (по умолчанию 4 мин), потом сохраняет
состояние в state.json и выходит. Workflow запускает его каждые 5 минут.

Только стандартная библиотека Python, ничего ставить не нужно.
"""
import html
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ---------------- настройки (переменные окружения) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHANNEL = os.environ["CHANNEL"].strip()                          # @username или -100...
BOARD = os.getenv("BOARD", "b")
RUN_BUDGET = int(os.getenv("RUN_BUDGET", "240"))         # сколько сек живёт один запуск
LOOP_SLEEP = int(os.getenv("LOOP_SLEEP", "20"))          # пауза между проходами
COMMENT_WINDOW = int(os.getenv("COMMENT_WINDOW", "1800"))  # сколько сек тред живёт в комментах
MAX_COMMENTS = int(os.getenv("MAX_COMMENTS", "40"))      # максимум комментов на тред
THREADS_PER_RUN = int(os.getenv("THREADS_PER_RUN", "3"))  # максимум новых тредов за запуск (антифлуд)
SEND_DELAY = float(os.getenv("SEND_DELAY", "3"))         # пауза между отправками
FORWARD_WAIT = int(os.getenv("FORWARD_WAIT", "600"))     # сколько ждать копию поста в чате обсуждений
BLOCKWORDS = [w.strip().lower() for w in os.getenv("BLOCKWORDS", "").split(",") if w.strip()]
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

TG_API = os.getenv("TG_API", "https://api.telegram.org").rstrip("/")
BASE = os.getenv("DVACH_BASE", "https://2ch.hk").rstrip("/")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dvach")


# ---------------- HTTP / Telegram ----------------
def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # сеть, таймаут и т.п. (URL не логируем: в нём токен)
        log.warning("http error: %s", type(e).__name__)
        return 0, b""


class TgError(Exception):
    pass


def multipart(fields: dict, file):
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        if not isinstance(v, str):
            v = json.dumps(v)
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    field, filename, data = file
    out.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
    )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def tg(method, params=None, file=None, retries=3):
    params = {k: v for k, v in (params or {}).items() if v is not None}
    url = f"{TG_API}/bot{BOT_TOKEN}/{method}"
    for _ in range(retries):
        if file:
            body, ctype = multipart(params, file)
        else:
            body, ctype = json.dumps(params).encode(), "application/json"
        status, raw = http(url, body, {"Content-Type": ctype}, timeout=90)
        try:
            d = json.loads(raw)
        except Exception:
            d = {"ok": False, "description": f"bad response (http {status})"}
        if d.get("ok"):
            return d["result"]
        if d.get("error_code") == 429:
            wait = d.get("parameters", {}).get("retry_after", 5)
            log.warning("429 flood control, жду %ss", wait)
            time.sleep(wait + 1)
            continue
        raise TgError(f"{method}: {d.get('description')}")
    raise TgError(f"{method}: retries exhausted")


_last_send = 0.0


def pace():
    global _last_send
    wait = SEND_DELAY - (time.time() - _last_send)
    if wait > 0:
        time.sleep(wait)
    _last_send = time.time()


MEDIA = {
    "jpg": ("sendPhoto", "photo"), "jpeg": ("sendPhoto", "photo"), "png": ("sendPhoto", "photo"),
    "mp4": ("sendVideo", "video"), "gif": ("sendAnimation", "animation"),
}


def send(chat_id, text, files=None, reply_to=None):
    """Шлёт текст или (первый файл + подпись). Возвращает Message или None."""
    media = None
    if files:
        path = files[0].get("path", "")
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "bin"
        status, data = http(BASE + path, timeout=60)
        if status == 200 and 0 < len(data) <= 20 * 1024 * 1024:
            media = (ext, data)
    reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None

    pace()
    if media:
        ext, data = media
        method, field = MEDIA.get(ext, ("sendDocument", "document"))
        try:
            return tg(method, {"chat_id": chat_id, "caption": text[:1024] or None, "reply_parameters": reply},
                      file=(field, f"file.{ext}", data))
        except TgError as e:
            log.warning("медиа не ушло (%s), шлю текстом", e)
            if not text:
                return None
    try:
        return tg("sendMessage", {"chat_id": chat_id, "text": text[:4000] or "…", "reply_parameters": reply})
    except TgError as e:
        log.warning("sendMessage: %s", e)
        return None


# ---------------- 2ch ----------------
def clean(raw: str) -> str:
    raw = re.sub(r"<br\s*/?>", "\n", raw or "")
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def get_json(url):
    status, raw = http(url, timeout=20)
    if status != 200:
        return status, None
    try:
        return status, json.loads(raw)
    except Exception:
        return status, None


def fetch_catalog():
    _, d = get_json(f"{BASE}/{BOARD}/catalog_num.json")
    return d.get("threads") if isinstance(d, dict) else None


def fetch_posts(num):
    status, d = get_json(f"{BASE}/{BOARD}/res/{num}.json")
    try:
        return status, d["threads"][0]["posts"]
    except Exception:
        return status, None


def build_op(t):
    subject = clean(t.get("subject", ""))
    body = clean(t.get("comment", ""))
    link = f"{BASE}/{BOARD}/res/{t['num']}.html"
    s = subject.lower().rstrip("….").strip()
    low = body.lower()
    dup = (not s) or low.startswith(s) or (bool(low) and s.startswith(low))
    head = "" if dup else f"{subject}\n\n"
    limit = (1024 if t.get("files") else 4000) - len(link) - 4
    return f"{(head + body)[:limit].rstrip()}\n\n{link}"


# ---------------- состояние ----------------
def load_state():
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
        except Exception:
            log.error("state.json повреждён — начинаю с чистого листа (старое постить не буду)")
            st = {}
    else:
        st = {}
    st.setdefault("last_num", None)
    st.setdefault("offset", 0)
    st.setdefault("forwards", {})
    st.setdefault("threads", {})
    return st


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    tmp.replace(STATE_FILE)


# ---------------- логика ----------------
def poll_updates(st, channel_id, group_id):
    """Забираем из Telegram авто-копии постов канала в чате обсуждений."""
    while True:
        params = {"allowed_updates": ["message"], "timeout": 0, "limit": 100}
        if st["offset"]:
            params["offset"] = st["offset"]
        try:
            ups = tg("getUpdates", params)
        except TgError as e:
            log.warning("getUpdates: %s", e)
            return
        for u in ups:
            st["offset"] = max(st["offset"], u["update_id"] + 1)
            m = u.get("message")
            if m and m["chat"]["id"] == group_id:
                log.info("Из чата обсуждений пришло сообщение %s (авто-копия поста: %s)",
                         m["message_id"], bool(m.get("is_automatic_forward")))
            if not m or not m.get("is_automatic_forward") or m["chat"]["id"] != group_id:
                continue
            origin = m.get("forward_origin") or {}
            src_chat = (origin.get("chat") or m.get("forward_from_chat") or {}).get("id")
            src_msg = origin.get("message_id") or m.get("forward_from_message_id")
            if src_chat == channel_id and src_msg:
                st["forwards"][str(src_msg)] = m["message_id"]
        if len(ups) < 100:
            break
    if len(st["forwards"]) > 300:   # чистим мусор
        for k in sorted(st["forwards"], key=int)[:-200]:
            st["forwards"].pop(k, None)


def check_catalog(st, channel_id):
    threads = fetch_catalog()
    if not threads:
        return
    nums = [int(t["num"]) for t in threads]
    if st["last_num"] is None:
        st["last_num"] = max(nums)
        save_state(st)
        log.info("Первый запуск: база = %s. Старые треды НЕ постятся, жду новые.", st["last_num"])
        return

    fresh = sorted((t for t in threads if int(t["num"]) > st["last_num"]), key=lambda t: int(t["num"]))
    if not fresh:
        return
    st["last_num"] = int(fresh[-1]["num"])      # всё, что новее, считаем «увиденным»
    save_state(st)
    if len(fresh) > THREADS_PER_RUN - st.get("posted_this_run", 0):
        keep = max(THREADS_PER_RUN - st.get("posted_this_run", 0), 0)
        log.info("Новых тредов %s, беру последние %s (антифлуд)", len(fresh), keep)
        fresh = fresh[len(fresh) - keep:] if keep else []

    for t in fresh:
        blob = (clean(t.get("subject", "")) + " " + clean(t.get("comment", ""))).lower()
        if any(w in blob for w in BLOCKWORDS):
            log.info("Тред %s пропущен по фильтру", t["num"])
            continue
        msg = send(channel_id, build_op(t), t.get("files"))
        if not msg:
            continue
        st["posted_this_run"] = st.get("posted_this_run", 0) + 1
        st["threads"][str(t["num"])] = {
            "channel_msg": msg["message_id"], "gid": None, "id_map": {},
            "last_post": int(t["num"]), "sent": 0,
            "created": time.time(), "deadline": 0,
        }
        save_state(st)
        log.info("Тред %s запощен (msg %s)", t["num"], msg["message_id"])


def process_threads(st, group_id, hard_deadline):
    now = time.time()
    for key in list(st["threads"]):
        th = st["threads"][key]
        if th["gid"] is None:
            g = st["forwards"].pop(str(th["channel_msg"]), None)
            if g:
                th["gid"] = g
                th["id_map"][key] = g
                th["deadline"] = now + COMMENT_WINDOW
            elif now - th["created"] > FORWARD_WAIT:
                log.warning("Тред %s: копия поста в чате обсуждений не пришла, бросаю", key)
                del st["threads"][key]
                continue
            else:
                continue
        if now > th["deadline"] or th["sent"] >= MAX_COMMENTS:
            log.info("Тред %s завершён (%s комментов)", key, th["sent"])
            del st["threads"][key]
            continue

        status, posts = fetch_posts(key)
        if status == 404:
            del st["threads"][key]
            continue
        if posts is None:
            continue
        fails = th.setdefault("fails", {})
        for p in posts:
            n = int(p["num"])
            if n <= th["last_post"]:
                continue
            if time.time() > hard_deadline or th["sent"] >= MAX_COMMENTS:
                break
            text = clean(p.get("comment", ""))
            files = p.get("files") or None
            if not text and not files:
                th["last_post"] = n
                continue
            m = re.search(r">>(\d+)", text)
            target = th["id_map"].get(m.group(1), th["gid"]) if m else th["gid"]
            sent = send(group_id, text, files, reply_to=target)
            if sent:
                th["last_post"] = n
                th["id_map"][str(n)] = sent["message_id"]
                th["sent"] += 1
                log.info("Тред %s: комментарий %s отправлен", key, n)
            else:
                fails[str(n)] = fails.get(str(n), 0) + 1
                if fails[str(n)] >= 3:
                    log.warning("Тред %s: комментарий %s не ушёл 3 раза, пропускаю", key, n)
                    th["last_post"] = n
                else:
                    log.warning("Тред %s: комментарий %s не ушёл, повторю позже", key, n)
                    break          # сохраняем порядок, повторим на следующем проходе
            save_state(st)
    save_state(st)


def main():
    st = load_state()

    def on_term(*_):
        save_state(st)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        tg("deleteWebhook")
        chat = tg("getChat", {"chat_id": CHANNEL})
    except TgError as e:
        sys.exit(f"Не могу открыть канал: {e}")
    channel_id, group_id = chat["id"], chat.get("linked_chat_id")
    if not group_id:
        sys.exit("К каналу не привязан чат обсуждений. Привяжи группу в настройках канала.")

    try:
        me = tg("getMe")
        role = tg("getChatMember", {"chat_id": group_id, "user_id": me["id"]}).get("status")
        log.info("Роль бота в чате обсуждений: %s", role)
        if role not in ("administrator", "creator"):
            print("::error::Бот НЕ админ в группе обсуждений — он не увидит авто-копии постов и не сможет "
                  "писать комментарии. Сделай его администратором группы.")
    except TgError as e:
        print(f"::error::Бота нет в группе обсуждений или нет доступа: {e}")

    start = time.time()
    hard = start + RUN_BUDGET + 40
    st["posted_this_run"] = 0
    while True:
        poll_updates(st, channel_id, group_id)
        check_catalog(st, channel_id)
        process_threads(st, group_id, hard)
        save_state(st)
        for k, th in st["threads"].items():
            log.info("Активный тред %s: копия в обсуждении=%s, комментов=%s, последний пост=%s",
                     k, th["gid"], th["sent"], th["last_post"])
        if time.time() + LOOP_SLEEP >= start + RUN_BUDGET:
            break
        time.sleep(LOOP_SLEEP)
    log.info("Запуск завершён")


if __name__ == "__main__":
    main()
