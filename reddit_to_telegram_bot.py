#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, html, time, uuid
from typing import Dict, Any, Optional, List, Tuple
import requests
from requests.auth import HTTPBasicAuth

# ===== Reddit endpoints / proxy =====
REDDIT_OAUTH = "https://oauth.reddit.com"
REDDIT_AUTH  = "https://www.reddit.com/api/v1/access_token"
REDDIT_WEB   = "https://www.reddit.com"
JINA_PROXY   = os.environ.get("JINA_PROXY", "https://r.jina.ai")

# ===== Config / env =====
USER_AGENT = os.environ.get("USER_AGENT", "VM-Reddit-TG-Bot/2.6 (by u/YourUserName; GitHub Actions)")
BOT_TOKEN  = os.environ.get("BOT_TOKEN")
CHAT_ID    = os.environ.get("CHAT_ID")
TG_API     = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = os.environ.get("REDDIT_USERNAME")
REDDIT_PASSWORD      = os.environ.get("REDDIT_PASSWORD")

# Переключатели
FORCE_PROXY     = os.environ.get("FORCE_PROXY", "0") == "1"   # всегда через прокси
PING_ON_START   = os.environ.get("PING_ON_START", "0") == "1" # по умолчанию ВЫКЛ (не шлём 🚀)
FORCE_POST_ONE  = os.environ.get("FORCE_POST_ONE", "0") == "1"
MEDIA_MODE      = os.environ.get("MEDIA_MODE", "auto")        # "auto" | "text_only"
CLEAR_STATE     = os.environ.get("CLEAR_STATE", "0") == "1"

SUBREDDIT = os.environ.get("SUBREDDIT", "nba")
LISTING   = os.environ.get("LISTING", "hot")
LIMIT     = int(os.environ.get("LIMIT", "25"))

TITLE_CHAR_LIMIT  = int(os.environ.get("CHAR_LIMIT", "200"))
BODY_CHAR_LIMIT   = int(os.environ.get("BODY_CHAR_LIMIT", "600"))
COMMENTS_COUNT    = int(os.environ.get("COMMENTS_COUNT", "3"))
COMMENT_CHAR_LIMIT= int(os.environ.get("COMMENT_CHAR_LIMIT", "220"))

STATE_FILE = os.environ.get("STATE_FILE", "state_reddit_ids.json")

# Ограничения Telegram
TG_CAPTION_LIMIT = 1024   # подпись к медиа
TG_MESSAGE_LIMIT = 4096   # обычное сообщение

# ===== HTTP session =====
session = requests.Session()
_device_id = str(uuid.uuid4())
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "X-Reddit-Device-Id": _device_id,
})

# ===== Utils =====
def log(msg: str) -> None:
    print(f"[reddit2tg] {msg}", flush=True)

def collapse_ws(text: str) -> str:
    return " ".join((text or "").strip().split())

def truncate(text: str, max_len: int) -> str:
    t = collapse_ws(text)
    return t if len(t) <= max_len else t[: max_len - 1].rstrip() + "…"

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

# ===== OAuth =====
_OAUTH_TOKEN: Optional[str] = None
_TOKEN_EXP: int = 0

def oauth_token(force: bool = False) -> str:
    global _OAUTH_TOKEN, _TOKEN_EXP
    now = int(time.time())
    if not force and _OAUTH_TOKEN and now < _TOKEN_EXP - 30:
        return _OAUTH_TOKEN
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_USERNAME and REDDIT_PASSWORD):
        raise SystemExit("Missing Reddit OAuth env vars: REDDIT_CLIENT_ID/SECRET, REDDIT_USERNAME/PASSWORD")

    r = requests.post(
        REDDIT_AUTH,
        auth=HTTPBasicAuth(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
        data={"grant_type":"password","username":REDDIT_USERNAME,"password":REDDIT_PASSWORD,"scope":"read"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
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

# ===== Reddit JSON fetch (OAuth + proxy fallback) =====
def reddit_json_via_oauth(path: str, params: Optional[dict] = None, max_retries: int = 3) -> dict:
    params = dict(params or {})
    if "raw_json" not in params:
        params["raw_json"] = 1
    url = f"{REDDIT_OAUTH}{path}"
    token = oauth_token()

    backoff = 1.5
    for attempt in range(1, max_retries + 1):
        r = session.get(
            url, params=params, timeout=30,
            headers={
                "Authorization": f"bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "X-Reddit-Device-Id": _device_id,
            }
        )
        if r.status_code < 400:
            return r.json()
        log(f"OAuth GET {url} -> {r.status_code} (attempt {attempt}/{max_retries}). Body: {(r.text or '')[:400]}")
        if r.status_code == 401:
            token = oauth_token(force=True)
        elif r.status_code in (403, 429) or 500 <= r.status_code < 600:
            time.sleep(backoff); backoff *= 1.8
        else:
            break
    r.raise_for_status()

def reddit_json_via_proxy(path: str, params: Optional[dict] = None) -> dict:
    params = dict(params or {})
    if "raw_json" not in params:
        params["raw_json"] = 1
    base = JINA_PROXY.rstrip('/')
    target = f"{REDDIT_WEB}{path}"          # https://www.reddit.com/...json
    url = f"{base}/{target}"                # https://r.jina.ai/https://www.reddit.com/...json
    log(f"Proxy GET {url}")
    r = session.get(url, params=params, timeout=30, headers={"Accept":"application/json","User-Agent":USER_AGENT})
    r.raise_for_status()
    txt = r.text
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        raise RuntimeError(f"Proxy returned non-JSON for {url}: {txt[:400]}")

def reddit_json(path: str, params: Optional[dict] = None) -> dict:
    if FORCE_PROXY:
        return reddit_json_via_proxy(path, params)
    try:
        return reddit_json_via_oauth(path, params)
    except Exception as e:
        log(f"OAuth path failed for {path}: {e}. Falling back to proxy…")
        return reddit_json_via_proxy(path, params)

# ===== Telegram API =====
def tg_send(endpoint: str, payload: dict, timeout: int = 60) -> requests.Response:
    assert TG_API and CHAT_ID, "Telegram config missing"
    url = f"{TG_API}/{endpoint}"
    payload = {"chat_id": CHAT_ID, **payload}
    resp = session.post(url, data=payload, timeout=timeout)
    if not resp.ok:
        log(f"Telegram {endpoint} -> {resp.status_code}: {resp.text[:300]}")
    else:
        log(f"Telegram {endpoint} OK")
    return resp

def send_message(text: str) -> requests.Response:
    return tg_send("sendMessage", {"text": text, "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=60)

def send_photo(photo_url: str, caption: str) -> requests.Response:
    return tg_send("sendPhoto", {"photo": photo_url, "caption": caption, "parse_mode": "HTML"}, timeout=90)

def send_video(video_url: str, caption: str) -> requests.Response:
    return tg_send("sendVideo", {"video": video_url, "caption": caption, "parse_mode": "HTML", "supports_streaming": True}, timeout=120)

def send_startup_ping() -> None:
    try:
        send_message("🚀 Бот запущен: проверка связи")
    except Exception as e:
        log(f"Startup ping failed: {e}")

# ===== Media helpers =====
def first_gallery_image(post: Dict[str, Any]) -> Optional[str]:
    md = post.get("media_metadata"); gd = post.get("gallery_data")
    if not md or not gd: return None
    try:
        fid = gd["items"][0]["media_id"]
        item = md.get(fid, {})
        if "s" in item and "u" in item["s"]:
            return item["s"]["u"].replace("&amp;", "&")
        if "p" in item and item["p"]:
            return item["p"][-1]["u"].replace("&amp;", "&")
    except Exception:
        return None
    return None

# ===== Caption builder (title + body + top comments) =====
def compose_caption(d: Dict[str, Any]) -> Tuple[str, bool]:
    title = collapse_ws(d.get("title", ""))
    permalink = d.get("permalink", "")
    link = f"https://www.reddit.com{permalink}"

    # 1) Заголовок (жирным)
    t_short = truncate(title, TITLE_CHAR_LIMIT)
    title_block = f"<b>{html.escape(t_short)}</b>"

    # 2) Текст поста
    body = selftext_excerpt(d, BODY_CHAR_LIMIT)

    # 3) Топ-комменты
    post_id36 = d.get("id") or ""
    top_comments = fetch_top_comments(post_id36, COMMENTS_COUNT) if COMMENTS_COUNT > 0 else []

    link_line = f'<a href="{html.escape(link)}">Читать на Reddit →</a>'
    media = is_media_post(d)
    hard_limit = TG_CAPTION_LIMIT if media else TG_MESSAGE_LIMIT

    # Базовая часть и бюджет
    base_parts = [title_block]
    if body:
        base_parts.append("--------")
    base_parts.append(link_line)
    base_text = "\n".join(base_parts)
    budget = max(0, hard_limit - len(base_text) - 1)

    # Вставим body
    body_block = ""
    if body and budget > 0:
        fit = min(len(body), budget)
        body_block = html.escape(body[:fit])
        budget -= len(body_block)

    # Комментарии (через разделитель)
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

    # Итоговая сборка
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

# ===== Comments =====
def fetch_top_comments(post_id36: str, count: int) -> List[Tuple[str, int, str]]:
    if not post_id36 or count <= 0: return []
    js = reddit_json(f"/comments/{post_id36}.json", params={"sort":"top","limit":50,"raw_json":1})
    if not isinstance(js, list) or len(js) < 2: return []
    rows: List[Tuple[str, int, str]] = []
    for c in js[1].get("data", {}).get("children", []):
        if c.get("kind") != "t1": continue
        cd = c.get("data", {}) or {}
        if cd.get("stickied") or cd.get("distinguished"): continue
        body = cd.get("body") or ""
        if cd.get("removed_by_category") or body in ("[removed]", "[deleted]"): continue
        author = cd.get("author") or "unknown"
        if author == "AutoModerator": continue
        score = int(cd.get("score") or 0)
        rows.append((author, score, collapse_ws(body)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:max(0, count)]

# ===== Posting logic =====
def handle_post(d: Dict[str, Any], state: Dict[str, bool]) -> bool:
    # возвращает True, если что-то отправлено
    if d.get("stickied") and not FORCE_POST_ONE:
        log("Skip stickied")
        return False

    post_id = d.get("name") or f"t3_{d.get('id')}"
    if post_id in state and not FORCE_POST_ONE:
        log(f"Skip already sent: {post_id}")
        return False

    permalink = d.get("permalink", "")
    url = d.get("url_overridden_by_dest") or d.get("url")
    post_hint = d.get("post_hint", "")
    is_gallery = d.get("is_gallery", False)
    is_video = d.get("is_video", False)

    caption, media = compose_caption(d)
    root = extract_crosspost_root(d)

    # Диагностический переключатель медиа
    use_media = (MEDIA_MODE != "text_only") and media

    sent_ok = False
    try:
        if use_media:
            if is_video or post_hint == "hosted:video":
                rv = (root.get("secure_media") or root.get("media") or {}).get("reddit_video") or {}
                fallback = rv.get("fallback_url")
                if fallback:
                    log(f"Sending VIDEO: {fallback}")
                    r = send_video(fallback, caption); sent_ok = r.ok
                else:
                    log("No fallback_url, send as text with link")
                    r = send_message(f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"); sent_ok = r.ok
            elif is_gallery:
                img = first_gallery_image(root)
                if img:
                    log(f"Sending PHOTO (gallery first): {img}")
                    r = send_photo(img, caption); sent_ok = r.ok
                else:
                    log("Gallery no image, send as text")
                    r = send_message(f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"); sent_ok = r.ok
            elif post_hint == "image" and url:
                log(f"Sending PHOTO: {url}")
                r = send_photo(url, caption); sent_ok = r.ok
            else:
                log("Unknown media type, send as text")
                r = send_message(f"{caption}\n\n{html.escape(url or ('https://www.reddit.com' + permalink))}"); sent_ok = r.ok
        else:
            # только текст
            text = caption
            if url and not url.startswith("https://www.reddit.com"):
                text = f"{text}\n\n{html.escape(url)}"
            log("Sending TEXT message")
            r = send_message(text); sent_ok = r.ok

    except Exception as e:
        log(f"ERROR sending: {repr(e)}"); sent_ok = False

    if sent_ok:
        state[post_id] = True
        save_state(state)
        log(f"Posted: {(d.get('title') or '')[:80]}")
        return True
    else:
        log(f"Failed to send: {post_id} - {(d.get('title') or '')[:80]}")
        return False

# ===== Fetch listing =====
def fetch_listing(subreddit: str, listing: str, limit: int) -> List[Dict[str, Any]]:
    js = reddit_json(f"/r/{subreddit}/{listing}.json", params={"limit": limit, "raw_json": 1})
    children = js.get("data", {}).get("children", [])
    return [c["data"] for c in children]

# ===== Main =====
def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing BOT_TOKEN or CHAT_ID")

    if FORCE_PROXY:
        log("FORCE_PROXY=1 -> use proxy for all Reddit requests")
    if PING_ON_START:
        # по умолчанию выключено; включай через env, если нужно
        try:
            send_message("🚀 Бот запущен: проверка связи")
        except Exception as e:
            log(f"Startup ping failed: {e}")

    state = load_state()
    if CLEAR_STATE:
        state = {}
        save_state(state)
        log("CLEAR_STATE=1 -> state очищен")

    posts = fetch_listing(SUBREDDIT, LISTING, LIMIT)
    log(f"Fetched posts: {len(posts)}")

    sent_total = 0
    skipped = 0

    # Принудительно отправим один самый свежий (диагностика)
    if FORCE_POST_ONE and posts:
        log("FORCE_POST_ONE=1 -> forcing the newest post")
        newest = posts[-1]  # самый новый
        if handle_post(newest, state):
            sent_total += 1

    # Обычная отправка (старые -> новые)
    for d in reversed(posts):
        if sent_total >= 10:  # предохранитель от спама
            break
        ok = handle_post(d, state)
        if ok:
            sent_total += 1
        else:
            skipped += 1

    log(f"Summary: sent={sent_total}, skipped={skipped}")

if __name__ == "__main__":
    main()
