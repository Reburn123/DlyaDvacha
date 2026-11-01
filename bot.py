#!/usr/bin/env python3
"""
Бот для GitHub Actions: следит за 2ch.hk/<доска>/, постит ВСЕ новые треды в
Telegram-канал, а в комментариях (чат обсуждений) воспроизводит ВСЕ ответы из треда.

Ничего не отбрасывается:
  * новые треды падают в постоянную очередь (state.json) и уходят в канал по порядку,
    с той скоростью, которую разрешает Telegram;
  * комментарии доставляются по порядку, без лимита на число;
  * пауза Telegram (flood control) не вешает бота: очередь просто ждёт.

Один запуск живёт RUN_BUDGET секунд (по умолчанию 25 мин), потом сохраняет состояние
и выходит; следующий запуск стартует сразу за ним.
Только стандартная библиотека Python.
"""
import collections
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
CHANNEL = os.environ["CHANNEL"].strip()                  # @username или -100...
BOARD = os.getenv("BOARD", "b")
RUN_BUDGET = int(os.getenv("RUN_BUDGET", "1500"))        # сколько сек живёт один запуск
LOOP_SLEEP = int(os.getenv("LOOP_SLEEP", "10"))          # пауза между проходами, когда делать нечего
CATALOG_EVERY = int(os.getenv("CATALOG_EVERY", "15"))    # как часто смотреть каталог
REFETCH_UNKNOWN = int(os.getenv("REFETCH_UNKNOWN", "60"))  # если каталог не отдаёт счётчик постов
TRACK_MAX = int(os.getenv("TRACK_MAX", "172800"))        # максимум сколько следим за тредом (48 ч)
MAX_COMMENTS = int(os.getenv("MAX_COMMENTS", "0"))       # 0 = без лимита
SEND_DELAY = float(os.getenv("SEND_DELAY", "3.2"))       # пауза между отправками В ОДИН чат
FORWARD_WAIT = int(os.getenv("FORWARD_WAIT", "7200"))    # сколько ждать копию поста в обсуждении
PASS_SECONDS = int(os.getenv("PASS_SECONDS", "15"))      # сколько сек подряд слать, прежде чем снова глянуть доску
LINK_TEXT = os.getenv("LINK_TEXT", "Зайти в обсуждение этого треда на Дваче")
EXTRA_TAGS = os.getenv("EXTRA_TAGS", "")                 # свои теги: "слово=тег,слово2=тег2"
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


flood_until = 0.0     # до какого момента Telegram просит не слать сообщения


def flooded() -> bool:
    return time.time() < flood_until


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
    global flood_until
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
            if wait > 30:
                flood_until = time.time() + wait
                log.warning("429: Telegram просит паузу %s сек. Не жду, очередь подождёт.", wait)
                raise TgError(f"{method}: flood control {wait}s")
            log.warning("429 flood control, жду %ss", wait)
            time.sleep(wait + 1)
            continue
        raise TgError(f"{method}: {d.get('description')}")
    raise TgError(f"{method}: retries exhausted")


_last_send: dict = {}


def pace(chat_id):
    """Не чаще одного сообщения в SEND_DELAY сек в один и тот же чат."""
    wait = SEND_DELAY - (time.time() - _last_send.get(chat_id, 0))
    if wait > 0:
        time.sleep(wait)
    _last_send[chat_id] = time.time()


MEDIA = {
    "jpg": ("sendPhoto", "photo"), "jpeg": ("sendPhoto", "photo"), "png": ("sendPhoto", "photo"),
    "mp4": ("sendVideo", "video"), "gif": ("sendAnimation", "animation"),
}


def send(chat_id, text, files=None, reply_to=None, parse_mode=None):
    """Шлёт текст или (первый файл + подпись). Возвращает Message или None."""
    if flooded():
        return None
    media = None
    if files:
        path = files[0].get("path", "")
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "bin"
        status, data = http(BASE + path, timeout=60)
        if status == 200 and 0 < len(data) <= 20 * 1024 * 1024:
            media = (ext, data)
    reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None

    pace(chat_id)
    if media:
        ext, data = media
        method, field = MEDIA.get(ext, ("sendDocument", "document"))
        try:
            return tg(method, {"chat_id": chat_id, "caption": text[:1024] or None,
                               "parse_mode": parse_mode, "reply_parameters": reply},
                      file=(field, f"file.{ext}", data))
        except TgError as e:
            log.warning("медиа не ушло (%s), шлю текстом", e)
            if flooded() or not text:
                return None
    try:
        return tg("sendMessage", {"chat_id": chat_id, "text": text[:4000] or "…",
                                  "parse_mode": parse_mode, "reply_parameters": reply})
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


# Хештеги: (тег, регулярка). Тред получает теги, слова которых встретились в его заголовке/тексте.
TAGS = [
    ("присер", r"присер"),
    ("дружба", r"дружб|подружит"),
    ("история", r"\bистори|\bрасскаж|\bрассказ|\bбайк|\bслучай\b|было дело|сегодня со мной"),
    ("вопрос", r"\?|\bвопрос|\bподскаж|\bпосоветуй|\bпомогит|кто знает|\bпочему\b|\bзачем\b|как быть"),
    ("аниме", r"\bаним|\bманг|\bтян\b|\bяой"),
    ("игры", r"\bигр[аыуео]|\bгейм|\bsteam|\bстим\b|\bdota|\bдота\b|\bgta\b|\bкс\b"),
    ("музыка", r"\bмузык|\bпесн|\bальбом|\bтрек\b"),
    ("кино", r"\bфильм|\bкино\b|\bсериал"),
    ("работа", r"\bработ|\bзарплат|собеседован|\bвакан"),
    ("отношения", r"\bдевушк|\bпарн[яеюи]|\bотношени|\bлюбов|\bбывш|свидани"),
    ("деньги", r"\bденьг|\bкредит|\bипотек|\bкрипт|\bбиткоин"),
    ("новости", r"\bновост"),
]
MAX_TAGS = 3


def _extra_tags():
    out = []
    for pair in EXTRA_TAGS.split(","):
        if "=" in pair:
            word, tag = (x.strip() for x in pair.split("=", 1))
            if word and tag:
                out.append((tag.lstrip("#").replace(" ", "_"), re.escape(word.lower())))
    return out


def hashtags(subject: str, body: str) -> str:
    blob = (subject + "\n" + body)[:600].lower()
    found = []
    for tag, pattern in _extra_tags() + TAGS:
        if tag not in found and re.search(pattern, blob):
            found.append(tag)
        if len(found) >= MAX_TAGS:
            break
    return " ".join("#" + t for t in (found or ["разное"]))


def _esc(x: str) -> str:
    return html.escape(x, quote=False)


def _fit(text: str, limit: int) -> str:
    """Экранирует текст для HTML и обрезает так, чтобы результат был не длиннее limit."""
    if len(_esc(text)) <= limit:
        return _esc(text)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_esc(text[:mid])) <= limit - 1:
            lo = mid
        else:
            hi = mid - 1
    return _esc(text[:lo]).rstrip() + "…"


def build_op(t):
    """Текст поста в канал в формате HTML: заголовок, текст, хештеги, ссылка на тред."""
    subject = clean(t.get("subject", ""))
    body = clean(t.get("comment", ""))
    link = f"{BASE}/{BOARD}/res/{t['num']}.html"
    s = subject.lower().rstrip("….").strip()
    low = body.lower()
    dup = (not s) or low.startswith(s) or (bool(low) and s.startswith(low))
    head = "" if dup else f"{subject}\n\n"
    footer = f'\n\n{hashtags(subject, body)}\n💬 <a href="{link}">{_esc(LINK_TEXT)}</a>'
    limit = (1024 if t.get("files") else 4000) - len(footer)
    return _fit(head + body, limit).rstrip() + footer


# ---------------- состояние ----------------
def load_state():
    st = {}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
        except Exception:
            log.error("state.json повреждён — начинаю с чистого листа (старое постить не буду)")
            st = {}
    st.setdefault("last_num", None)
    st.setdefault("offset", 0)
    st.setdefault("forwards", {})
    st.setdefault("threads", {})
    st.setdefault("queue", [])
    st.setdefault("flood_until", 0)
    return st


def save_state(st):
    st["flood_until"] = flood_until
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
            if not m or not m.get("is_automatic_forward") or m["chat"]["id"] != group_id:
                continue
            origin = m.get("forward_origin") or {}
            src_chat = (origin.get("chat") or m.get("forward_from_chat") or {}).get("id")
            src_msg = origin.get("message_id") or m.get("forward_from_message_id")
            if src_chat == channel_id and src_msg:
                st["forwards"][str(src_msg)] = m["message_id"]
        if len(ups) < 100:
            break


_last_cat_warn = 0.0


def check_catalog(st):
    """Читает каталог, кладёт ВСЕ новые треды в очередь. Возвращает {num: posts_count} или None."""
    threads = fetch_catalog()
    if not threads:
        global _last_cat_warn
        if time.time() - _last_cat_warn > 60:
            log.warning("Каталог доски не получен (сайт недоступен или блокирует запросы)")
            _last_cat_warn = time.time()
        return None
    cat = {int(t["num"]): t.get("posts_count") for t in threads}
    if st["last_num"] is None:
        st["last_num"] = max(cat)
        save_state(st)
        log.info("Первый запуск: база = %s. Старые треды НЕ постятся, жду новые.", st["last_num"])
        return cat

    fresh = sorted((t for t in threads if int(t["num"]) > st["last_num"]), key=lambda t: int(t["num"]))
    if fresh:
        st["last_num"] = int(fresh[-1]["num"])
        for t in fresh:
            blob = (clean(t.get("subject", "")) + " " + clean(t.get("comment", ""))).lower()
            if any(w in blob for w in BLOCKWORDS):
                log.info("Тред %s пропущен по фильтру", t["num"])
                continue
            f = (t.get("files") or [{}])[0].get("path")
            st["queue"].append({"num": int(t["num"]), "text": build_op(t),
                                "files": [{"path": f}] if f else None, "tries": 0, "html": True})
        save_state(st)
        log.info("Новых тредов: %s, в очереди на публикацию: %s", len(fresh), len(st["queue"]))
    return cat


PEND: dict = {}                      # тред -> список комментариев, ждущих отправки (только в памяти)
RR: collections.deque = collections.deque()   # очередь тредов для раздачи комментариев по кругу


def ready(chat_id) -> bool:
    return time.time() - _last_send.get(chat_id, 0) >= SEND_DELAY


def drop_thread(st, key, why):
    log.info("Тред %s: %s", key, why)
    st["threads"].pop(key, None)
    PEND.pop(key, None)


def send_one_op(st, channel_id):
    """Публикует первый тред из очереди. Возвращает True, если ушёл."""
    item = st["queue"][0]
    msg = send(channel_id, item["text"], item.get("files"),
               parse_mode="HTML" if item.get("html") else None)
    if msg:
        st["queue"].pop(0)
        st["threads"][str(item["num"])] = {
            "channel_msg": msg["message_id"], "gid": None, "id_map": {},
            "last_post": item["num"], "sent": 0, "created": time.time(),
            "pc": None, "more": False, "fetched_at": 0, "fails": {},
        }
        save_state(st)
        log.info("Тред %s запощен (msg %s)", item["num"], msg["message_id"])
        return True
    if not flooded():
        item["tries"] = item.get("tries", 0) + 1
        if item["tries"] >= 5:
            log.error("Тред %s не удалось отправить 5 раз, пропускаю", item["num"])
            st["queue"].pop(0)
            save_state(st)
    return False


def resolve_forwards(st):
    now = time.time()
    for key in list(st["threads"]):
        th = st["threads"][key]
        if th["gid"] is not None:
            continue
        g = st["forwards"].pop(str(th["channel_msg"]), None)
        if g:
            th["gid"] = g
            th["id_map"][key] = g
            log.info("Тред %s: копия в обсуждении найдена (%s)", key, g)
        elif now - th["created"] > FORWARD_WAIT:
            log.warning("Тред %s: копия поста в чате обсуждений так и не пришла — бот точно админ группы?", key)
            del st["threads"][key]


def refresh_threads(st, cat):
    """Смотрит, в каких тредах появились новые посты, и складывает их в PEND."""
    now = time.time()
    for key in list(st["threads"]):
        th = st["threads"].get(key)
        if th is None or th["gid"] is None or PEND.get(key):
            continue
        num = int(key)
        alive = cat is None or num in cat
        pc = cat.get(num) if cat else None
        due = (th.get("more") or not alive or (pc is not None and pc != th.get("pc"))
               or (pc is None and now - th.get("fetched_at", 0) >= REFETCH_UNKNOWN))
        if not due:
            if now - th["created"] > TRACK_MAX:
                drop_thread(st, key, "слежу слишком долго, завершаю")
            continue

        status, posts = fetch_posts(key)
        th["fetched_at"] = now
        if status == 404:
            drop_thread(st, key, "удалён/закрыт, перестаю следить")
            continue
        if posts is None:
            continue
        seen_max, items = th["last_post"], []
        for p in posts:
            n = int(p["num"])
            seen_max = max(seen_max, n)
            if n <= th["last_post"]:
                continue
            text = clean(p.get("comment", ""))
            files = p.get("files") or None
            if text or files:
                items.append({"num": n, "text": text, "files": files})
        if MAX_COMMENTS:
            items = items[:max(0, MAX_COMMENTS - th["sent"])]
        if items:
            PEND[key] = items
            th.update(more=True, seen_max=seen_max, pc_next=pc)
            if key not in RR:
                RR.append(key)
        else:
            th.update(last_post=seen_max, pc=pc, more=False)
            if not alive or (MAX_COMMENTS and th["sent"] >= MAX_COMMENTS):
                drop_thread(st, key, f"завершён ({th['sent']} комментов)")


def drained(st, key, cat):
    th = st["threads"][key]
    th["last_post"] = max(th["last_post"], th.pop("seen_max", 0))
    th["pc"] = th.pop("pc_next", None)
    th["more"] = False
    alive = cat is None or int(key) in cat
    if not alive or (MAX_COMMENTS and th["sent"] >= MAX_COMMENTS):
        drop_thread(st, key, f"завершён ({th['sent']} комментов)")


def pick_key():
    """Следующий тред по кругу, у которого есть что отправить."""
    for _ in range(len(RR)):
        key = RR[0]
        RR.rotate(-1)
        if PEND.get(key):
            return key
    RR.clear()
    return None


def send_one_comment(st, group_id, cat):
    """Отправляет один комментарий (по кругу между тредами). Возвращает True, если ушёл."""
    key = pick_key()
    if key is None:
        return False
    th = st["threads"].get(key)
    if th is None:
        PEND.pop(key, None)
        return False
    it = PEND[key][0]
    m = re.search(r">>(\d+)", it["text"])
    target = th["id_map"].get(m.group(1), th["gid"]) if m else th["gid"]
    sent = send(group_id, it["text"], it["files"], reply_to=target)
    if sent:
        PEND[key].pop(0)
        th["last_post"] = it["num"]
        th["id_map"][str(it["num"])] = sent["message_id"]
        th["sent"] += 1
        log.info("Тред %s: комментарий %s отправлен", key, it["num"])
        if not PEND[key]:
            PEND.pop(key, None)
            drained(st, key, cat)
        save_state(st)
        return True
    if not flooded():
        fails = th.setdefault("fails", {})
        fails[str(it["num"])] = fails.get(str(it["num"]), 0) + 1
        if fails[str(it["num"])] >= 3:
            log.warning("Тред %s: комментарий %s не ушёл 3 раза, пропускаю", key, it["num"])
            PEND[key].pop(0)
            th["last_post"] = it["num"]
            if not PEND[key]:
                PEND.pop(key, None)
                drained(st, key, cat)
        else:
            log.warning("Тред %s: комментарий %s не ушёл, повторю", key, it["num"])
        save_state(st)
    return False


def pump(st, channel_id, group_id, cat, pass_end):
    """
    Шлёт сообщения, пока есть что слать. Канал и группа — два независимых «окна»
    со своей паузой, поэтому треды и комментарии идут параллельно, а не друг за другом.
    """
    ops = cms = 0
    while time.time() < pass_end and not flooded():
        did = False
        if st["queue"] and ready(channel_id):
            did = True
            ops += send_one_op(st, channel_id)
        if any(PEND.values()) and ready(group_id):
            did = True
            cms += send_one_comment(st, group_id, cat)
        if not did:
            if st["queue"] or any(PEND.values()):
                time.sleep(0.2)       # работа есть, но чаты ещё «отдыхают»
                continue
            break
    return ops, cms


def main():
    global flood_until
    st = load_state()
    flood_until = float(st.get("flood_until") or 0)

    def on_term(*_):
        save_state(st)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    def on_alarm(*_):                     # сторожевой таймер: запуск не может висеть вечно
        log.error("Сторожевой таймер: запуск слишком долгий, сохраняю состояние и выхожу")
        save_state(st)
        os._exit(0)
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(RUN_BUDGET + 120)

    chat = None
    for attempt in range(4):
        try:
            tg("deleteWebhook")
            chat = tg("getChat", {"chat_id": CHANNEL})
            break
        except TgError as e:
            log.warning("Старт: %s (попытка %s/4)", e, attempt + 1)
            time.sleep(10)
    if chat is None:
        sys.exit("Не могу открыть канал: проверь CHANNEL и что бот админ канала")
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
    cat, last_cat, last_beat = None, 0.0, start
    log.info("Старт. В очереди: %s, отслеживаю тредов: %s", len(st["queue"]), len(st["threads"]))
    while True:
        poll_updates(st, channel_id, group_id)
        if time.time() - last_cat >= CATALOG_EVERY:
            c = check_catalog(st)
            if c is not None:
                cat = c
            last_cat = time.time()
        resolve_forwards(st)
        refresh_threads(st, cat)
        ops, cms = pump(st, channel_id, group_id, cat, min(time.time() + PASS_SECONDS, start + RUN_BUDGET))
        save_state(st)

        backlog = sum(len(v) for v in PEND.values())
        if ops or cms or time.time() - last_beat > 300:
            log.info("Проход: тредов отправлено %s, комментов %s | в очереди тредов %s, комментов %s, слежу за %s%s",
                     ops, cms, len(st["queue"]), backlog, len(st["threads"]),
                     f" | пауза Telegram ещё {int(flood_until - time.time())}с" if flooded() else "")
            last_beat = time.time()
        busy = (st["queue"] or backlog) and not flooded()
        pause = 1 if busy else LOOP_SLEEP
        if time.time() + pause >= start + RUN_BUDGET:
            break
        if flooded() and flood_until > start + RUN_BUDGET:
            log.info("Пауза Telegram длится дольше запуска, выхожу раньше")
            break
        time.sleep(pause)
    log.info("Запуск завершён. В очереди тредов осталось: %s", len(st["queue"]))


if __name__ == "__main__":
    main()
