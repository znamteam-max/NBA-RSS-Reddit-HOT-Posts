# Reddit → Telegram: r/nba (hot) → личный чат/канал (OAuth)

Скрипт + GitHub Actions, который:
- раз в 10 минут берёт **/r/nba/hot** через **OAuth** (`https://oauth.reddit.com/...`) — без 403
- публикует **каждый новый пост** отдельным сообщением в Telegram
- обрезает заголовок до **200 символов** и добавляет ссылку «Читать на Reddit →»
- отправляет **видео** с Reddit как MP4 `sendVideo(supports_streaming=True)` → **играет прямо в Telegram**
- YouTube/Streamable передаёт ссылкой → Telegram сам встраивает плеер

## Что нужно
1) Создать бота через **@BotFather** → получить `BOT_TOKEN`.
2) Узнать `CHAT_ID` (личный чат, группа или канал). Для канала добавьте бота админом.
3) На странице https://www.reddit.com/prefs/apps создать **script**-приложение:
   - name — любое
   - type — `script`
   - redirect uri — `http://localhost/`
   - забрать **client id** (строка под названием приложения) и **secret**
4) В GitHub → **Settings → Secrets and variables → Actions** добавить:
   - `BOT_TOKEN`, `CHAT_ID`
   - `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`
5) (Опционально) добавить **Variables** → `USER_AGENT` (уникальная строка вида `AppName/1.0 (by u/<username>)`).

## Локальный запуск
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=... CHAT_ID=...
export REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=...
export REDDIT_USERNAME=... REDDIT_PASSWORD=...
python reddit_to_telegram_bot.py
