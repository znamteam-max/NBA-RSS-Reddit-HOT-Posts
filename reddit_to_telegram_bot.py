#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit → Telegram (устойчивый к 403):
1) Пытаемся OAuth: https://oauth.reddit.com
2) При 403/429/451/сетевых ошибках — fallback на прокси:
   https://r.jina.ai/http://www.reddit.com<path>?...

Сообщение в Telegram:
<b>Заголовок</b>
--------
Первые N символов текста поста (BODY_CHAR_LIMIT)
--------
• u/author1 (+score1): коммент1
• u/author2 (+score2): коммент2
• u/author3 (+score3): коммент3
(Затем «Читать на Reddit →»)

Поддержка:
- видео Reddit: sendVideo(supports_streaming=True) с fallback_url (играет в Telegram)
- изображения, галереи, ссылочные посты
"""

import os
import json
import html
import time
import uuid
from typing import Dict, Any, Optional, List, Tuple

import requests
from requests.auth import HTTPBasicAuth

# ------------------------ Константы и конфиг ------------------------

REDDIT_OAUTH = "https://oauth.reddit.com"
REDDIT_AUTH = "https://www.reddit.com/api/v1/access_token"
REDDIT_WEB  = "https://www.reddit.com"  # раньше стоял http
JINA_PROXY  = os.environ.get("JINA_PROXY", "https://r.jina.ai")

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "VM-Reddit-TG-Bot/2.4 (by u/YourUserName; GitHub Actions)"
)

# Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# Reddit OAuth
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME")
REDDIT_PASSWORD = os.environ.get("REDDIT_PASSWORD")

# Поведение бота
SUBREDDIT = os.environ.get("SUBREDDIT", "nba")
LISTING = os.environ.get("LISTING", "hot")  # hot | new | top
TITLE_CHAR_LIMIT = int(os.environ.get("CHAR_LIMIT", "200"))      # лимит заголовка
BODY_CHAR_LIMIT = int(os.environ.get("BODY_CHAR_LIMIT", "600"))  # лимит текста поста
COMMENTS_COUNT = int(os.environ.get("COMMENTS_COUNT", "3"))      # сколько топ-комментов
COMMENT_CHAR_LIMIT = int(os.environ.get("COMMENT_CHAR_LIMIT", "220"))
STATE_FILE = os.environ.get("STATE_FILE", "state_reddit_ids.json")

# Ограничения Telegram
TG_CAPTION_LIMIT = 1024   # подпись к медиа
TG_MESSAGE_LIMIT = 4096   # обычное сообщение

# HTTP session
session = requests.Session()
_device_id = str(uuid.uuid4())
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "X-Reddit-Device-Id": _device_id,
})

# Кэш токена
_OAUTH_TOKEN: Optional[str] = None
_TOKEN_EXP: int = 0  # epoch seconds

# ------------------------ Утилиты ------------------------

def log(msg: str) -> None:
    print(f"[reddit2tg] {msg}", flush=True)

def load_state() -> Dict[str, bool]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state: Dict[str, bool]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def collapse_ws(text: str) -> str:
    return " ".join((text or "").strip().split())

def truncate(text: str, max_len: int) -> str:
    t = collapse_ws(text)
    return t if len(t) <= max_len else t[: max_len - 1].rstrip() + "…"

def extract_crosspost_root(d: Dict[str, Any]) -> Dict[str, Any]:
    xlist = d.get("crosspost_parent_list")
    if isinstance(xlist, list) and xlist:
        return xlist[0]
    return d

def selftext_excerpt(d: Dict[str, Any], limit: int) -> str:
    root = extract_crosspost_root(d)
    txt = collapse_ws(root.get("selftext") or "")
    if not txt:
        return ""
    return truncate(txt, limit)

def is_media_post(d: Dict[str, Any]) -> bool:
    post_hint = d.get("post_hint", "")
    return bool(d.get("is_video") or post_hint in ("image", "hosted:video") or d.get("is_gallery"))

# ------------------------ OAuth ------------------------

def oauth_token(force: bool = False) -> str:
    """Получить (и кешировать) OAuth токен через password grant (scope=read)."""
    global _OAUTH_TOKEN, _TOKEN_EXP
    now = int(time.time())
    if not force and _OAUTH_TOKEN and now < _TOKEN_EXP - 30:
        return _OAUTH_TOKEN

    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_USERNAME and REDDIT_PASSWORD):
        raise SystemExit("Missing Reddit OAuth env vars: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD")

    auth = HTTPBasicAuth(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
    data = {
        "grant_type": "password",
        "username": REDDIT_USERNAME,
        "password": REDDIT_PASSWORD,
        "scope": "read",
    }
    headers = {"User-Agent": USER_AGENT}
    r = requests.post(REDDIT_AUTH, auth=auth, data=data, headers=headers, timeout=30)
    if r.status_code >= 400:
        log(f"OAuth error {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    js = r.json()
    token = js.get("access_token")
    ttl = int(js.get("expires_in") or 3600)
    if not token:
        raise RuntimeError(f"Failed to get access_token: {r.text}")

    _OAUTH_TOKEN = token
    _TOKEN_EXP = now + ttl
    log("Obtained Reddit OAuth token")
    return token

# ------------------------ Двухконтурный fetch JSON ------------------------

def reddit_json_via_oauth(path: str, params: Optional[dict] = None, max_retries: int = 3) -> dict:
    """GET oauth.reddit.com с ретраями; кидает исключение, если не вышло."""
    params = dict(params or {})
    if "raw_json" not in params:
        params["raw_json"] = 1

    token = oauth_token()
    url = f"{REDDIT_OAUTH}{path}"

    backoff = 1.5
    for attempt in range(1, max_retries + 1):
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Reddit-Device-Id": _device_id,
        }
        r = session.get(url, headers=headers, params=params, timeout=30)
        if r.status_code < 400:
            return r.json()
        # диагностика
        log(f"OAuth GET {url} -> {r.status_code} (attempt {attempt}/{max_retries}). Body: {(r.text or '')[:400]}")
        if r.status_code == 401:
            token = oauth_token(force=True)
        elif r.status_code in (403, 429) or 500 <= r.status_code < 600:
            time.sleep(backoff); backoff *= 1.8
        else:
            break
    r.raise_for_status()  # пробросит последнюю ошибку

def reddit_json_via_proxy(path: str, params: Optional[dict] = None) -> dict:
    """GET через Jina proxy. ВАЖНО: корректно собрать URL с протоколом и слешем."""
    params = dict(params or {})
    if "raw_json" not in params:
        params["raw_json"] = 1

    base = JINA_PROXY.rstrip('/')                      # 'https://r.jina.ai'
    target = f"{REDDIT_WEB}{path}"                     # 'https://www.reddit.com/r/nba/hot.json'
    url = f"{base}/{target}"                           # 'https://r.jina.ai/https://www.reddit.com/...'
    log(f"Proxy GET {url}")

    r = session.get(url, params=params, timeout=30, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    r.raise_for_status()
    text = r.text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Proxy returned non-JSON for {url}: {text[:400]}")

def reddit_json(path: str, params: Optional[dict] = None) -> dict:
    """
    Универсальный вызов:
    - пытаемся OAuth;
    - на падении — уходим в proxy;
    - логируем, каким путём пошли.
    """
    try:
        js = reddit_json_via_oauth(path, params=params)
        return js
    except Exception as e:
        log(f"OAuth path failed for {path}: {e}. Falling back to proxy…")
        js = reddit_json_via_proxy(path, params=params)
        return js

# ------------------------ Комментарии ------------------------

def fetch_top_comments(post_id36: str, count: int) -> List[Tuple[str, int, str]]:
    if not post_id36 or count <= 0:
        return []
    path = f"/comments/{post_id36}.json"
    params = {"sort": "top", "limit": 50, "raw_json": 1}
    js = reddit_json(path, params=params)
    if not isinstance(js, list) or len(js) < 2:
        return []

    comments_listing = js[1].get("data", {}).get("children", [])
    rows: List[Tuple[str, int, str]] = []

    for c in comments_listing:
        if c.get("kind") != "t1":
            continue
        cd = c.get("data", {}) or {}
        if cd.get("stickied") or cd.get("distinguished"):
            continue
        body = cd.get("body") or ""
        if cd.get("removed_by_category") or body in ("[removed]", "[deleted]"):
            continue
        author = cd.get("author") or "unknown"
        if author == "AutoModerator":
            continue
        score = int(cd.get("score") or 0)
        rows.append((author, score, collapse_ws(body)))

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:max(0, count)]

# ------------------------ Telegram ------------------------

def tg_send(endpoint: str, payload: dict, timeout: int = 60) -> requests.Response:
    assert TG_API and CHAT_ID, "Telegram config missing"
    url = f"{TG_API}/{endpoint}"
    payload = {"chat_id": CHAT_ID, **payload}
    return session.post(url, data=payload, timeout=timeout)

def send_message(text: str) -> requests.Response:
    return tg_send("sendMessage", {"text": text, "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=60)

def send_photo(photo_url: str, caption: str) -> requests.Response:
    return tg_send("sendPhoto", {"photo": photo_url, "caption": caption, "parse_mode": "HTML"}, timeout=90)

def send_video(video_url: str, caption: str) -> requests.Response:
    return tg_send("sendVideo", {"video": video_url, "caption": caption, "parse_mode": "HTML", "supports_streaming": True}, timeout=120)

def first_gallery_image(post: Dict[str, Any]) -> Optional[str]:
    media_md = post.get("media_metadata")
    gallery = post.get("gallery_data")
    if not media_md or not gallery:
        return None
    try:
        first_id = gallery["items"][0]["media_id"]
        md = media_md.get(first_id, {})
        if "s" in md and "u" in md["s"]:
            return md["s"]["u"].replace("&amp;", "&")
        if "p" in md and md["p"]:
            return md["p"][-1]["u"].replace("&amp;", "&")
    except Exception:
        return None
    return None

# ------------------------ Компоновка подписи ------------------------

TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096

def compose_caption(d: Dict[str, Any]) -> Tuple[str, bool]:
    """Собирает текст под лимиты Telegram. Возвращает (text, is_media)."""
    title = collapse_ws(d.get("title", ""))
    permalink = d.get("permalink", "")
    link = f"https://www.reddit.com{permalink}"

    # 1) Заголовок
    t_short = truncate(title, TITLE_CHAR_LIMIT)
    title_block = f"<b>{html.escape(t_short)}</b>"

    # 2) Текст поста
    body = selftext_excerpt(d, BODY_CHAR_LIMIT)

    # 3) Комментарии — топ N
    post_id36 = d.get("id") or ""
    top_comments = fetch_top_comments(post_id36, COMMENTS_COUNT) if COMMENTS_COUNT > 0 else []

    link_line = f'<a href="{html.escape(link)}">Читать на Reddit →</a>'

    media = is_media_post(d)
    hard_limit = TG_CAPTION_LIMIT if media else TG_MESSAGE_LIMIT

    # База и бюджет
    base_parts = [title_block]
    if body:
        base_parts.append("--------")
    base_parts.append(link_line)
    base_text = "\n".join(base_parts)
    budget = hard_limit - len(base_text) - 1
    if budget < 0:
        budget = 0

    # Вставим body
    body_block = ""
    if body and budget > 0:
        fit = min(len(body), budget)
        body_block = html.escape(body[:fit])
        budget -= len(body_block)

    # Комментарии (с разделителем)
    comment_blocks: List[str] = []
    if top_comments and budget > 0:
        sep_len = len("\n--------\n")
        if budget > sep_len:
            budget -= sep_len
            per_limit = COMMENT_CHAR_LIMIT
            min_per_limit = 60
            for _ in range(20):
                tmp = []
                total = 0
                for (author, score, cbody) in top_comments:
                    prefix = f"• u/{author} (+{score}): "
                    c_excerpt = truncate(cbody, per_limit)
                    line = f"{prefix}{html.escape(c_excerpt)}"
                    tmp.append(line)
                    total += len(line) + 1
                if total <= budget:
                    comment_blocks = tmp
                    budget -= total
                    break
                per_limit = max(min_per_limit, int(per_limit * 0.8))

    # Сборка
    out = [title_block]
    if body_block:
        out.append("--------")
        out.append(body_block)
    if comment_blocks:
        out.append("--------")
        out.extend(comment_blocks)
    out.append(link_line)

    text = "\n".join(out)
    if len(text) > hard_limit:
        text = text[: hard_limit - 1].rstrip() + "…"

    return text, media

# ------------------------ Основная логика ------------------------

def handle_post(d: Dict[str, Any], state: Dict[str, bool]) -> None:
    if d.get("stickied"):
        return

    post_id = d.get("name") or f"t3_{d.get('id')}"
    if post_id in state:
        return

    permalink = d.get("permalink", "")
    url = d.get("url_overridden_by_dest") or d.get("url")
    post_hint = d.get("post_hint", "")
    is_gallery = d.get("is_gallery", False)
    is_video = d.get("is_video", False)

    caption, media = compose_caption(d)
    root = extract_crosspost_root(d)

    sent_ok = False
    try:
        if media:
            if is_video or post_hint == "hosted:video":
                rv = (root.get("secure_media") or root.get("media") or {}).get("reddit_video") or {}
                fallback = rv.get("fallback_url")
                if fallback:
                    r = send_video(fallback, caption)
                    sent_ok = r.ok
                else:
                    text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                    r = send_message(text)
                    sent_ok = r.ok
            elif is_gallery:
                img = first_gallery_image(root)
                if img:
                    r = send_photo(img, caption)
                    sent_ok = r.ok
                else:
                    text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                    r = send_message(text)
                    sent_ok = r.ok
            elif post_hint == "image" and url:
                r = send_photo(url, caption)
                sent_ok = r.ok
            else:
                text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                r = send_message(text)
                sent_ok = r.ok
        else:
            text = caption
            if url and not url.startswith("https://www.reddit.com"):
                text = f"{text}\n\n{html.escape(url)}"
            r = send_message(text)
            sent_ok = r.ok

    except Exception as e:
        log(f"ERROR sending: {repr(e)}")
        sent_ok = False

    if sent_ok:
        state[post_id] = True
        save_state(state)
    else:
        log(f"Failed to send post: {post_id} - {(d.get('title') or '')[:80]}")

# ------------------------ Получение ленты ------------------------

def fetch_listing(subreddit: str, listing: str, limit: int) -> List[Dict[str, Any]]:
    path = f"/r/{subreddit}/{listing}.json"
    params = {"limit": limit, "raw_json": 1}
    js = reddit_json(path, params=params)
    children = js.get("data", {}).get("children", [])
    return [c["data"] for c in children]

# ------------------------ Точка входа ------------------------

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing BOT_TOKEN or CHAT_ID env variables")

    state = load_state()
    posts = fetch_listing(SUBREDDIT, LISTING, 25)
    # отправляем старые → новые
    for d in reversed(posts):
        handle_post(d, state)

if __name__ == "__main__":
    main()
