# YouTube AI Manager

Telegram-first YouTube creator automation for **@CoodWithYogesh**.

## Current build

- Telegram bot receives owner-submitted video files and downloads them to the server.
- AI metadata generation uses Gemini when `GEMINI_API_KEY` is configured.
- A usable 1280x720 thumbnail is made from a real video frame with the title overlay (requires FFmpeg).
- Videos are stored in a SQLite queue.
- Google OAuth connects the YouTube channel.
- `/publish ID` uploads a queued video as **private** and applies the generated thumbnail when available.
- Existing YouTube-to-Telegram new-upload notifications remain available through `python bot.py`.

Automatic comment replies, scheduled publishing, and recorded-video livestream orchestration are the next build stages; they are not enabled in this initial manager build.

## Local setup (Termux / Linux)

```bash
pkg update -y
pkg install python git ffmpeg -y
git clone https://github.com/coodwithyogesh-blip/bookish-octo-memory.git
cd bookish-octo-memory
pip install -r requirements.txt
cp .env.example .env
nano .env
python app.py
```

Open `http://127.0.0.1:10000` in your browser.

Set these values in `.env`:
- `TELEGRAM_BOT_TOKEN`: token from BotFather.
- `OWNER_TELEGRAM_ID`: your numeric Telegram user ID; only this account can submit and publish videos.
- `GEMINI_API_KEY`: Google AI Studio key (optional; without it the bot uses a filename-based fallback).
- `APP_SECRET`: long random secret.
- `YOUTUBE_CLIENT_SECRETS_FILE=client_secret.json`: Google OAuth Desktop/Web client JSON file.
- `YOUTUBE_TOKEN_FILE=token.json`: generated OAuth token; do not commit it.

## Google YouTube API setup

1. In Google Cloud Console, create/select a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen and add your Google account as a test user if the app is in testing.
3. Create an OAuth client. For local use, configure the callback as `http://127.0.0.1:10000/oauth/callback`. For hosting, configure the exact HTTPS callback URL shown by your deployed app: `https://YOUR-HOST/oauth/callback`.
4. Download the client JSON as `client_secret.json` and keep it private.
5. Open `/oauth/start` in the browser and authorize the YouTube channel.

The first build intentionally uploads as **private**. Review the title, description, thumbnail, and video in YouTube Studio before making it public.

## Telegram commands

- `/start` — instructions
- `/queue` — latest queued videos
- `/publish ID` — upload a queued video privately, for example `/publish 3`
- Send a video file — add it to the queue and generate metadata.

Send a video as a Telegram video or as a document with a video MIME type. Add a caption to give the AI extra context.

## Deploying

Use a persistent disk for `manager.db`, `media/`, and `token.json`. A normal ephemeral web filesystem can lose queued videos and OAuth tokens after restart. Set the service start command to `python app.py`, configure all environment variables, and set the Google OAuth callback URL to the public HTTPS URL.

## Existing notifier

The original `bot.py` is retained. It monitors the configured YouTube channel RSS feed and posts new public uploads to the Telegram group. Run it separately only if you want those notifications too.

## Tests

```bash
python -m unittest discover -s tests -v
```

Never commit `.env`, `client_secret.json`, `token.json`, database files, or downloaded videos.
