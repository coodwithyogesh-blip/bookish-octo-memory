# YouTube → Telegram Group Notifier

A small Python bot that watches a YouTube channel and posts every newly uploaded video to a Telegram group with its thumbnail, title, description, upload time, and link.

## What it does

- Checks the channel's YouTube RSS feed every 5 minutes by default.
- Sends new uploads to the configured Telegram group as a photo with a caption.
- Saves sent video IDs in `state.json` so the same upload is not posted twice.
- Does not send old videos on the first run unless `SEND_LATEST_ON_START=true`.
- Uses environment variables; never commit your real bot token.

## 1. Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather), or use your existing bot.
2. Add the bot to your Telegram group.
3. Give it permission to send messages and photos.
4. Set the group Chat ID in `.env`. A supergroup ID usually starts with `-100`.

## 2. Run on Termux

```bash
pkg update -y
pkg install python git -y
git clone https://github.com/coodwithyogesh-blip/bookish-octo-memory.git
cd bookish-octo-memory
pip install -r requirements.txt
cp .env.example .env
nano .env
```

In `.env`, replace `TELEGRAM_BOT_TOKEN` with the NEW token from BotFather. Keep the group ID and channel handle correct. Save in nano with Ctrl+O, Enter, Ctrl+X.

Start the bot:

```bash
python bot.py
```

Keep Termux running for continuous monitoring. On Android, disable battery optimization for Termux if background work is being stopped.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Secret token from BotFather | Required |
| `TELEGRAM_CHAT_ID` | Destination group ID | Required |
| `YOUTUBE_CHANNEL` | YouTube channel handle or channel URL | Required |
| `POLL_INTERVAL_SECONDS` | How often to check for uploads (minimum 60) | `300` |
| `SEND_LATEST_ON_START` | Send the newest existing video on first run | `false` |
| `LOG_LEVEL` | Logging level | `INFO` |

## Tests

```bash
python -m unittest discover -s tests -v
```

## Notes

- YouTube RSS feeds expose recent public uploads; this is polling, not instant push.
- The description is read from the public YouTube watch page and may be unavailable for some videos.
- Keep `.env` private. If a token is ever shared publicly, revoke it with BotFather and generate a new one.
