#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import html
from typing import Dict, Any, Optional, List
import requests

REDDIT_BASE = "https://www.reddit.com"
HEADERS = {
    "User-Agent": "VM-Reddit-TG-Bot/1.0 (by u/your_reddit_username)"
}

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SUBREDDIT = os.environ.get("SUBREDDIT", "nba")
LISTING = os.environ.get("LISTING", "hot")  # hot, new, top
LIMIT = int(os.environ.get("LIMIT", "25"))
CHAR_LIMIT = int(os.environ.get("CHAR_LIMIT", "200"))
STATE_FILE = os.environ.get("STATE_FILE", "state_reddit_ids.json")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

def load_state() -> Dict[str, bool]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}

def save_state(state: Dict[str, bool]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len-1].rstrip() + "…"

def build_caption(title: str, permalink: str) -> str:
    title = title.replace("\n", " ").strip()
    short = truncate(title, CHAR_LIMIT)
    link = f"{REDDIT_BASE}{permalink}"
    # Use HTML to avoid markdown escaping hell
    caption = f"{html.escape(short)}\n<a href=\"{html.escape(link)}\">Читать на Reddit →</a>"
    return caption

def send_message(text: str) -> requests.Response:
    assert TG_API and CHAT_ID, "Telegram config missing"
    url = f"{TG_API}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    return requests.post(url, data=payload, timeout=30)

def send_photo(photo_url: str, caption: str) -> requests.Response:
    assert TG_API and CHAT_ID, "Telegram config missing"
    url = f"{TG_API}/sendPhoto"
    payload = {
        "chat_id": CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    }
    return requests.post(url, data=payload, timeout=60)

def send_video(video_url: str, caption: str) -> requests.Response:
    assert TG_API and CHAT_ID, "Telegram config missing"
    url = f"{TG_API}/sendVideo"
    payload = {
        "chat_id": CHAT_ID,
        "video": video_url,
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": True
    }
    return requests.post(url, data=payload, timeout=120)

def first_gallery_image(post: Dict[str, Any]) -> Optional[str]:
    # Reddit gallery: fetch the first original (s) or largest preview (p[-1]) image
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

def extract_crosspost_root(d: Dict[str, Any]) -> Dict[str, Any]:
    # Some posts are crossposts; prefer the original payload for media
    xlist = d.get("crosspost_parent_list")
    if isinstance(xlist, list) and xlist:
        return xlist[0]
    return d

def handle_post(d: Dict[str, Any], state: Dict[str, bool]) -> None:
    post_id = d["name"]  # e.g., t3_abcd
    if post_id in state:
        return

    permalink = d.get("permalink", "")
    title = d.get("title", "").strip()
    url = d.get("url_overridden_by_dest") or d.get("url")
    post_hint = d.get("post_hint", "")
    is_gallery = d.get("is_gallery", False)
    is_video = d.get("is_video", False)

    root = extract_crosspost_root(d)

    caption = build_caption(title, permalink)

    sent_ok = False

    try:
        # 1) Reddit hosted video
        if is_video or post_hint == "hosted:video":
            rv = (root.get("secure_media") or root.get("media") or {}).get("reddit_video") or {}
            fallback = rv.get("fallback_url")
            if fallback:
                r = send_video(fallback, caption)
                sent_ok = r.ok
            else:
                # fallback to link
                text = f"{caption}\n\n{html.escape(url or (REDDIT_BASE + permalink))}"
                r = send_message(text)
                sent_ok = r.ok

        # 2) Rich video (e.g., YouTube, Streamable) -> send link so Telegram embeds
        elif post_hint == "rich:video" and url:
            text = f"{caption}\n\n{html.escape(url)}"
            r = send_message(text)
            sent_ok = r.ok

        # 3) Gallery -> first image
        elif is_gallery:
            img = first_gallery_image(root)
            if img:
                r = send_photo(img, caption)
                sent_ok = r.ok
            else:
                text = f"{caption}\n\n{html.escape(url or (REDDIT_BASE + permalink))}"
                r = send_message(text)
                sent_ok = r.ok

        # 4) Single image
        elif post_hint == "image" and url:
            r = send_photo(url, caption)
            sent_ok = r.ok

        # 5) Link or self-post (text)
        else:
            text = f"{caption}\n\n{html.escape(url or (REDDIT_BASE + permalink))}"
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
    api = f"{REDDIT_BASE}/r/{subreddit}/{listing}.json"
    params = {"limit": limit}
    res = requests.get(api, headers=HEADERS, params=params, timeout=30)
    res.raise_for_status()
    js = res.json()
    children = js.get("data", {}).get("children", [])
    return [c["data"] for c in children]

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing BOT_TOKEN or CHAT_ID env variables")
    state = load_state()
    posts = fetch_listing(SUBREDDIT, LISTING, LIMIT)
    # Send oldest first so chat order is natural
    for d in reversed(posts):
        handle_post(d, state)

if __name__ == "__main__":
    main()
