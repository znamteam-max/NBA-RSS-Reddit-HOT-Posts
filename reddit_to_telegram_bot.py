#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit (OAuth) → Telegram poster:
- Берёт /r/<SUBREDDIT>/<LISTING>
- В подписи к посту: ЖИРНЫЙ заголовок + первые N символов текста поста (BODY_CHAR_LIMIT)
- Видео с Reddit шлём через sendVideo(supports_streaming=True)
"""

import os
import json
import html
import time
from typing import Dict, Any, Optional, List

import requests
from requests.auth import HTTPBasicAuth

# Reddit OAuth endpoints
REDDIT_OAUTH = "https://oauth.reddit.com"
REDDIT_AUTH = "https://www.reddit.com/api/v1/access_token"

# Дай Reddit внятный UA
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "VM-Reddit-TG-Bot/2.1 (by u/YourUserName; GitHub Actions)"
)

# Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# Reddit OAuth secrets
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME")
REDDIT_PASSWORD = os.environ.get("REDDIT_PASSWORD")

# Bot behavior
SUBREDDIT = os.environ.get("SUBREDDIT", "nba")
LISTING = os.environ.get("LISTING", "hot")  # hot | new | top
TITLE_CHAR_LIMIT = int(os.environ.get("CHAR_LIMIT", "200"))      # лимит для заголовка
BODY_CHAR_LIMIT  = int(os.environ.get("BODY_CHAR_LIMIT", "200")) # лимит для текста поста
STATE_FILE = os.environ.get("STATE_FILE", "state_reddit_ids.json")

# Requests session
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# In-process OAuth token cache
_OAUTH_TOKEN: Optional[str] = None
_TOKEN_EXP: int = 0  # epoch seconds


def oauth_token() -> str:
    """Get (and cache) OAuth token via password grant."""
    global _OAUTH_TOKEN, _TOKEN_EXP
    now = int(time.time())
    if _OAUTH_TOKEN and now < _TOKEN_EXP - 30:
        return _OAUTH_TOKEN

    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_USERNAME and REDDIT_PASSWORD):
        raise SystemExit(
            "Missing Reddit OAuth env vars: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD"
        )

    auth = HTTPBasicAuth(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
    data = {
        "grant_type": "password",
        "username": REDDIT_USERNAME,
        "password": REDDIT_PASSWORD,
    }
    headers = {"User-Agent": USER_AGENT}
    r = requests.post(REDDIT_AUTH, auth=auth, data=data, headers=headers, timeout=30)
    r.raise_for_status()
    js = r.json()
    token = js.get("access_token")
    ttl = int(js.get("expires_in") or 3600)
    if not token:
        raise RuntimeError(f"Failed to get access_token: {r.text}")

    _OAUTH_TOKEN = token
    _TOKEN_EXP = now + ttl
    return token


def reddit_get(path: str, params: Optional[dict] = None) -> requests.Response:
    """GET to oauth.reddit.com with bearer; retry once on 401/403."""
    token = oauth_token()
    headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
    url = f"{REDDIT_OAUTH}{path}"
    r = session.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code in (401, 403):
        token = oauth_token()
        headers["Authorization"] = f"bearer {token}"
        r = session.get(url, headers=headers, params=params or {}, timeout=30)
    r.raise_for_status()
    return r


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


def truncate(text: str, max_len: int) -> str:
    text = " ".join(text.strip().split())  # схлопываем пробелы/переводы строк
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def extract_crosspost_root(d: Dict[str, Any]) -> Dict[str, Any]:
    xlist = d.get("crosspost_parent_list")
    if isinstance(xlist, list) and xlist:
        return xlist[0]
    return d


def selftext_excerpt(d: Dict[str, Any]) -> str:
    """
    Берём первые BODY_CHAR_LIMIT символов текста поста.
    Для кросспостов используем root-пост.
    """
    root = extract_crosspost_root(d)
    txt = root.get("selftext") or ""
    # Reddit часто кладёт спецсимволы/маркдаун — просто схлопнем пробелы
    txt = " ".join(txt.strip().split())
    if not txt:
        return ""
    return truncate(txt, BODY_CHAR_LIMIT)


def build_caption(title: str, permalink: str, body_excerpt: str) -> str:
    """
    HTML-подпись:
    <b>Заголовок</b>
    первые N символов текста
    Читать на Reddit →
    """
    t_short = truncate(title, TITLE_CHAR_LIMIT)
    link = f"https://www.reddit.com{permalink}"
    if body_excerpt:
        caption = (
            f"<b>{html.escape(t_short)}</b>\n"
            f"{html.escape(body_excerpt)}\n"
            f"<a href=\"{html.escape(link)}\">Читать на Reddit →</a>"
        )
    else:
        caption = (
            f"<b>{html.escape(t_short)}</b>\n"
            f"<a href=\"{html.escape(link)}\">Читать на Reddit →</a>"
        )
    return caption


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
    return tg_send(
        "sendVideo",
        {"video": video_url, "caption": caption, "parse_mode": "HTML", "supports_streaming": True},
        timeout=120,
    )


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


def handle_post(d: Dict[str, Any], state: Dict[str, bool]) -> None:
    # пропускаем закреплённые посты
    if d.get("stickied"):
        return

    post_id = d.get("name") or f"t3_{d.get('id')}"
    if post_id in state:
        return

    permalink = d.get("permalink", "")
    title = d.get("title", "").strip()
    url = d.get("url_overridden_by_dest") or d.get("url")
    post_hint = d.get("post_hint", "")
    is_gallery = d.get("is_gallery", False)
    is_video = d.get("is_video", False)

    body_excerpt = selftext_excerpt(d)
    caption = build_caption(title, permalink, body_excerpt)

    sent_ok = False
    try:
        # 1) Reddit-hosted video
        root = extract_crosspost_root(d)
        if is_video or post_hint == "hosted:video":
            rv = (root.get("secure_media") or root.get("media") or {}).get("reddit_video") or {}
            fallback = rv.get("fallback_url")
            if fallback:
                r = send_video(fallback, caption)
                sent_ok = r.ok
            else:
                # fallback к ссылке
                text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                r = send_message(text)
                sent_ok = r.ok

        # 2) Rich video (YouTube/Streamable) — отправляем ссылку (телега встроит плеер)
        elif post_hint == "rich:video" and url:
            text = f"{caption}\n\n{html.escape(url)}"
            r = send_message(text)
            sent_ok = r.ok

        # 3) Галерея — берём первую картинку
        elif is_gallery:
            img = first_gallery_image(root)
            if img:
                r = send_photo(img, caption)
                sent_ok = r.ok
            else:
                text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                r = send_message(text)
                sent_ok = r.ok

        # 4) Одиночное изображение
        elif post_hint == "image" and url:
            r = send_photo(url, caption)
            sent_ok = r.ok

        # 5) Ссылка/текстовый пост
        else:
            text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
            r = send_message(text)
            sent_ok = r.ok

    except Exception as e:
        print("ERROR sending:", repr(e))
        sent_ok = False

    if sent_ok:
        state[post_id] = True
        save_state(state)
    else:
        print("Failed to send post:", post_id, "-", title[:80])


def fetch_listing(subreddit: str, listing: str, limit: int) -> List[Dict[str, Any]]:
    path = f"/r/{subreddit}/{listing}.json"
    params = {"limit": limit}
    res = reddit_get(path, params=params)
    js = res.json()
    children = js.get("data", {}).get("children", [])
    return [c["data"] for c in children]


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing BOT_TOKEN or CHAT_ID env variables")

    state = load_state()
    posts = fetch_listing(SUBREDDIT, LISTING, TITLE_CHAR_LIMIT and 25)
    # отправляем старые → новые
    for d in reversed(posts):
        handle_post(d, state)


if __name__ == "__main__":
    main()
