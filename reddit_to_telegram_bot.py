#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit (OAuth) → Telegram poster

Формат сообщения:
<b>Заголовок</b>
--------
Первые N символов текста поста (BODY_CHAR_LIMIT)
--------
• u/author1 (+score1): коммент1
• u/author2 (+score2): коммент2
• u/author3 (+score3): коммент3

В конце всегда ссылка "Читать на Reddit →".
Скрипт сам ужимает превью и комментарии под лимиты Telegram:
- 1024 символа для подписи медиа (видео/фото/галерея)
- 4096 символов для обычного текстового сообщения
"""

import os
import json
import html
import time
from typing import Dict, Any, Optional, List, Tuple

import requests
from requests.auth import HTTPBasicAuth

# Reddit OAuth endpoints
REDDIT_OAUTH = "https://oauth.reddit.com"
REDDIT_AUTH = "https://www.reddit.com/api/v1/access_token"

# User-Agent (требование Reddit — осмысленный UA)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "VM-Reddit-TG-Bot/2.2 (by u/YourUserName; GitHub Actions)"
)

# Telegram config
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# Reddit OAuth secrets
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME")
REDDIT_PASSWORD = os.environ.get("REDDIT_PASSWORD")

# Behavior
SUBREDDIT = os.environ.get("SUBREDDIT", "nba")
LISTING = os.environ.get("LISTING", "hot")  # hot | new | top
TITLE_CHAR_LIMIT = int(os.environ.get("CHAR_LIMIT", "200"))          # лимит заголовка
BODY_CHAR_LIMIT = int(os.environ.get("BODY_CHAR_LIMIT", "600"))      # лимит текста поста
COMMENTS_COUNT = int(os.environ.get("COMMENTS_COUNT", "3"))          # сколько топ-комментов добавлять
COMMENT_CHAR_LIMIT = int(os.environ.get("COMMENT_CHAR_LIMIT", "220"))# стартовый лимит на 1 коммент (будет динамически ужиматься)
STATE_FILE = os.environ.get("STATE_FILE", "state_reddit_ids.json")

# Telegram limits
TG_CAPTION_LIMIT = 1024   # подпись к медиа
TG_MESSAGE_LIMIT = 4096   # обычное сообщение

# Requests session
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# In-process OAuth token cache
_OAUTH_TOKEN: Optional[str] = None
_TOKEN_EXP: int = 0  # epoch seconds


# ------------------------ Reddit OAuth helpers ------------------------

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


# ------------------------ Utilities ------------------------

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


# ------------------------ Comments ------------------------

def fetch_top_comments(post_id36: str, count: int) -> List[Tuple[str, int, str]]:
    """
    Возвращает список кортежей (author, score, body) для top-level комментариев,
    отсортированных по score (desc). Удалённые/стикнутые/мод-ответы пропускаем.
    """
    path = f"/comments/{post_id36}.json"
    params = {"sort": "top", "limit": 50}
    res = reddit_get(path, params=params)
    js = res.json()
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
        if cd.get("removed_by_category") or cd.get("body") in ("[removed]", "[deleted]"):
            continue
        if cd.get("author") in ("AutoModerator",):
            continue
        author = cd.get("author") or "unknown"
        score = int(cd.get("score") or 0)
        body = collapse_ws(cd.get("body") or "")
        if not body:
            continue
        rows.append((author, score, body))

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:max(0, count)]


# ------------------------ Telegram senders ------------------------

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


# ------------------------ Caption composer ------------------------

def compose_caption(d: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Собирает текст подписи/сообщения под лимиты Telegram.
    Возвращает (text, is_media), где is_media = True, если придётся отправлять как подпись к медиа.
    """
    title = collapse_ws(d.get("title", ""))
    permalink = d.get("permalink", "")
    link = f"https://www.reddit.com{permalink}"

    # 1) Заголовок
    t_short = truncate(title, TITLE_CHAR_LIMIT)
    title_block = f"<b>{html.escape(t_short)}</b>"

    # 2) Текст поста (расширенный)
    body = selftext_excerpt(d, BODY_CHAR_LIMIT)

    # 3) Комментарии — возьмём топ N прямо сейчас
    post_id36 = d.get("id") or ""
    top_comments = fetch_top_comments(post_id36, COMMENTS_COUNT) if COMMENTS_COUNT > 0 else []

    # Построение базового каркаса (без контента), чтобы рассчитать бюджет
    lines = [title_block]

    # Между заголовком и телом — граница, но только если есть body
    if body:
        lines.append("--------")
        # body добавим позже с учётом бюджета

    # Граница перед комментариями — добавляем всегда, если комменты есть
    if top_comments:
        # body ещё не добавили; просто резервируем место под разделение позже
        pass

    # Заключительная ссылка
    link_line = f'<a href="{html.escape(link)}">Читать на Reddit →</a>'

    # Определим, медиа ли пост (влияет на лимит)
    media = is_media_post(d)
    hard_limit = TG_CAPTION_LIMIT if media else TG_MESSAGE_LIMIT

    # Сначала прикинем "сервисные" части
    fixed_parts = [title_block]
    if body:
        fixed_parts.append("--------")
    if top_comments:
        # перед комментами тоже добавим границу
        # но только если body или вообще какой-то контент кроме заголовка есть
        pass
    # Пока не добавляем комменты/боди/вторую границу — только посчитаем
    base_text = "\n".join(fixed_parts + [link_line])
    # Бюджет под динамический контент
    budget = hard_limit - len(base_text) - 1  # -1 на возможный перевод строки перед ссылкой
    if budget < 0:
        budget = 0

    # Распределим бюджет: сначала body, затем комменты
    body_used = 0
    body_block = ""
    if body and budget > 0:
        body_fit = min(len(body), budget)
        body_block = html.escape(body[:body_fit])
        body_used = len(body_block)
        budget -= body_used

    # Добавим вторую границу и комментарии
    comment_blocks: List[str] = []
    if top_comments and budget > 0:
        # Сначала отнимем место под разделитель и хотя бы переносы
        sep_len = len("\n--------\n")
        if budget > sep_len:
            budget -= sep_len
            # Динамически подберём лимит на каждый комментарий
            # Начинаем с COMMENT_CHAR_LIMIT и уменьшаем до тех пор,
            # пока весь блок не влезет
            per_limit = COMMENT_CHAR_LIMIT
            # Жёсткий нижний предел — 60 символов на комментарий
            min_per_limit = 60
            # Пробуем несколько шагов ужатия
            for _ in range(20):
                tmp_blocks = []
                total_len = 0
                for (author, score, cbody) in top_comments:
                    prefix = f"• u/{author} (+{score}): "
                    c_excerpt = truncate(cbody, per_limit)
                    line = f"{prefix}{html.escape(c_excerpt)}"
                    tmp_blocks.append(line)
                    total_len += len(line) + 1  # +\n
                if total_len <= budget:
                    comment_blocks = tmp_blocks
                    budget -= total_len
                    break
                per_limit = max(min_per_limit, int(per_limit * 0.8))
            # Если даже минималки не хватило — просто отрежем комментарии полностью
            if not comment_blocks:
                comment_blocks = []

    # Сборка итогового текста
    out_lines = [title_block]
    if body_block:
        out_lines.append("--------")
        out_lines.append(body_block)
    if comment_blocks:
        out_lines.append("--------")
        out_lines.extend(comment_blocks)
    out_lines.append(link_line)

    text = "\n".join(out_lines)

    # Подстраховка: если всё-таки превысили лимит — режем хвост
    if len(text) > hard_limit:
        text = text[: hard_limit - 1].rstrip() + "…"

    return text, media


# ------------------------ Main posting logic ------------------------

def handle_post(d: Dict[str, Any], state: Dict[str, bool]) -> None:
    # пропускаем закреплённые посты
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
            # 1) Reddit-hosted video
            if is_video or post_hint == "hosted:video":
                rv = (root.get("secure_media") or root.get("media") or {}).get("reddit_video") or {}
                fallback = rv.get("fallback_url")
                if fallback:
                    r = send_video(fallback, caption)
                    sent_ok = r.ok
                else:
                    # на всякий случай — ссылка
                    text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                    r = send_message(text)
                    sent_ok = r.ok

            # 2) Галерея
            elif is_gallery:
                img = first_gallery_image(root)
                if img:
                    r = send_photo(img, caption)
                    sent_ok = r.ok
                else:
                    text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                    r = send_message(text)
                    sent_ok = r.ok

            # 3) Одиночное изображение
            elif post_hint == "image" and url:
                r = send_photo(url, caption)
                sent_ok = r.ok

            # 4) Бог его знает — отправим текстом с ссылкой
            else:
                text = f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"
                r = send_message(text)
                sent_ok = r.ok

        else:
            # Текст/ссылки без медиа — одно сообщение (до 4096 символов)
            text = caption
            if url and not url.startswith("https://www.reddit.com"):
                # добавим исходную ссылку, чтобы Telegram сделал предпросмотр/плеер (YouTube)
                text = f"{text}\n\n{html.escape(url)}"
            r = send_message(text)
            sent_ok = r.ok

    except Exception as e:
        print("ERROR sending:", repr(e))
        sent_ok = False

    if sent_ok:
        state[post_id] = True
        save_state(state)
    else:
        print("Failed to send post:", post_id, "-", (d.get('title') or "")[:80])


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
    posts = fetch_listing(SUBREDDIT, LISTING, 25)
    # отправляем старые → новые
    for d in reversed(posts):
        handle_post(d, state)


if __name__ == "__main__":
    main()
