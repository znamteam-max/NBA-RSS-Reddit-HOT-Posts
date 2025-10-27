# Reddit → Telegram: r/nba (hot) → Personal Chat/Channel

This repo contains a small Python script + GitHub Actions workflow that:
- Polls **https://www.reddit.com/r/nba/hot/** every 10 minutes
- Posts each new Reddit post as a separate Telegram message
- Limits the caption text to **200 characters**, with a "read on Reddit" link
- Sends **playable video** to Telegram (uses the Reddit `fallback_url` MP4 when available; YouTube links are sent as links so Telegram embeds the player)

## What you'll need

1) Create a Telegram bot via **@BotFather** and copy the **BOT_TOKEN**.  
2) Decide where to post:
   - **Personal chat with the bot:** DM your bot `/start`, then fetch your `chat_id` via `getUpdates`:
     - Open: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
     - Look for `"message":{"chat":{"id": ... }}` — that's your `CHAT_ID`.
   - **Private/Public channel:** add the bot as **admin** and use the channel ID (often starts with `-100...`).
3) Add GitHub **secrets**: `BOT_TOKEN`, `CHAT_ID`.
4) (Optional) Adjust environment variables in the workflow:
   - `SUBREDDIT` (default `nba`)
   - `LISTING` (default `hot`)
   - `LIMIT` (default `25`)
   - `CHAR_LIMIT` (default `200`)

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=12345:ABC...  # from @BotFather
export CHAT_ID=123456789       # your personal chat or channel id
python reddit_to_telegram_bot.py
```

## Notes

- **Video playback in Telegram**: for Reddit-hosted videos we send the `fallback_url` MP4 via `sendVideo(supports_streaming=True)`, which plays inside Telegram. Some Reddit videos may be **video-only** (Reddit often separates audio), but they will still play.
- **YouTube** links are sent as regular URLs; Telegram automatically renders an inline player.
- The script stores already-posted Reddit IDs in `state_reddit_ids.json` and the workflow commits changes back to the repo.
