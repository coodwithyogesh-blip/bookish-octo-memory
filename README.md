# YouTube AI Manager

Telegram-first YouTube creator automation for **@CoodWithYogesh**.

## Current build

- Telegram bot receives owner-submitted video files and downloads them to the server.
- AI metadata generation uses Gemini to analyze a representative video frame and draft the title, description, SEO tags, hashtags, and category.
- The bot waits for your own custom thumbnail image in the connected Telegram group, converts it to a 1280×720 JPEG, and applies it to the YouTube upload.
- Videos are stored in a SQLite queue and remain in `awaiting_thumbnail` until a custom image is attached.
- The owner binds one Telegram group with `/bindgroup`, then approves teammates with `/allow USER_ID`; `/revoke USER_ID` removes access.
- Approved teammates can submit videos and thumbnails and use upload-management commands in that group. Other groups and unknown users are rejected.
- Google OAuth connects the YouTube channel.
- `/publish ID` uploads a queued video as **private** and applies the generated thumbnail when available.
- Existing YouTube-to-Telegram new-upload notifications remain available through `python bot.py`.

Scheduled publishing, automatic comment replies, dashboard settings, and recorded-video YouTube Live controls are implemented in the codebase. The live stream can start immediately or be scheduled for a future India-time date/time, and loops the selected recording continuously until you stop it. It requires FFmpeg, an eligible YouTube channel with live streaming enabled, and an always-on host; the process stops if the host sleeps or restarts. The current live process is held in memory, so configure a persistent, always-on worker for long streams.

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

Open `http://127.0.0.1:10000` in your browser. Sign in with username `owner` and the `DASHBOARD_PASSWORD` you set; enter `MANAGER_API_TOKEN` in the dashboard to enable write controls.

Set these values in `.env`:
- `DASHBOARD_PASSWORD`: required password for the web dashboard and its settings API.
- `MANAGER_API_TOKEN`: separate long random token used by dashboard schedule/live controls; enter the same value in the dashboard.
- `TELEGRAM_BOT_TOKEN`: token from BotFather.
- `OWNER_TELEGRAM_ID`: your numeric Telegram user ID; this account owns group binding and team permissions.
- `TELEGRAM_GROUP_ID`: optional fixed group ID. Otherwise run `/bindgroup` in the target group as the owner.
- The owner uses `/allow USER_ID` and `/revoke USER_ID` to manage approved team members.
- `GEMINI_API_KEY`: Google AI Studio key (required for AI metadata and AI thumbnail generation; without it the bot uses a filename-based fallback).
- `GEMINI_IMAGE_MODEL`: image model, default `gemini-3.1-flash-image`.
- `AUTO_UPLOAD_DEFAULT=true`: automatically publish each Telegram video after AI processing.
- `AUTO_UPLOAD_PRIVACY=public`: default visibility (`public`, `unlisted`, or `private`). Owner can change it with `/privacy`.
- `APP_SECRET`: long random secret.
- `MANAGER_API_TOKEN`: separate long random token for dashboard write controls.
- `DASHBOARD_PASSWORD`: strong password for the dashboard's HTTP Basic Auth login.
- `YOUTUBE_CLIENT_SECRETS_FILE=client_secret.json`: Google OAuth Desktop/Web client JSON file.
- `YOUTUBE_TOKEN_FILE=token.json`: generated OAuth token; do not commit it.

## Google YouTube API setup

1. In Google Cloud Console, create/select a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen and add your Google account as a test user if the app is in testing.
3. Create an OAuth client. For local use, configure the callback as `http://127.0.0.1:10000/oauth/callback`. For hosting, configure the exact HTTPS callback URL shown by your deployed app: `https://YOUR-HOST/oauth/callback`.
4. Download the client JSON as `client_secret.json` and keep it private.
5. Open `/oauth/start` in the browser and authorize the YouTube channel.

The `/publish ID` command intentionally uploads as **private**. `/schedule` uploads privately with YouTube's scheduled release time. The upload is blocked until a custom thumbnail is attached. Scheduled uploads use YouTube's publishAt feature; the video is uploaded privately when the schedule is created and becomes public at the requested time.

## Telegram commands

- `/start` — instructions
- `/whoami` — show your numeric Telegram user ID
- `/bindgroup` — bind the current group (owner only)
- `/allow USER_ID` and `/revoke USER_ID` — add/remove team members (owner only)
- `/team` — list approved team IDs
- `/queue` — latest queued videos
- `/publish ID` — manually upload a queued video privately for review, for example `/publish 3`.
- `/autopublish on|off` — enable or pause automatic publishing after Telegram video intake.
- `/privacy public|unlisted|private` — choose the default visibility for automatic uploads.
- `/schedule ID YYYY-MM-DD HH:MM` — upload privately and set YouTube's scheduled public release time in `TIMEZONE` (default: `Asia/Kolkata`). Example: `/schedule 3 2026-11-20 19:30`.
- Dashboard owner controls — schedule uploads, schedule/start/stop a recorded live stream, check live status, and enable/disable automatic comment replies.
- Send a video file in the bound group — AI creates the metadata and asks for a custom thumbnail.
- Reply to the bot's thumbnail prompt with your JPG/PNG image. Once attached, the bot automatically uploads according to `/autopublish` and `/privacy`.
- `/thumbnail ID` requests/replaces the thumbnail for an existing queued video.

Send a video as a Telegram video or as a document with a video MIME type. Add a caption to give the AI extra context. Group members must be explicitly approved by the owner.

## Telegram group setup

1. Add the bot to your Telegram group.
2. In @BotFather, open `/mybots` → select your bot → Bot Settings → Group Privacy → Turn off. Remove and re-add the bot after changing this setting, or make it a group admin so it can receive media.
3. As the owner, send `/bindgroup` in the group.
4. Ask each teammate to send `/whoami`; copy their numeric ID and run `/allow ID` as the owner.
5. Send a video, then reply to the bot's thumbnail prompt with your own image. The bot uses that exact image for the upload.

The bot only accepts commands and media from the bound group and owner-approved IDs. Keep group membership controlled. The hosted Telegram Bot API file download currently limits downloads to 20 MB; larger source videos need a local Telegram Bot API server or a different upload intake path.

## Deploying

Use a persistent disk for `manager.db`, `media/`, and `token.json`. A normal ephemeral web filesystem can lose queued videos and OAuth tokens after restart. Set the service start command to `python app.py`, configure all environment variables, and set the Google OAuth callback URL to the public HTTPS URL.

## Existing notifier

The original `bot.py` is retained. It monitors the configured YouTube channel RSS feed and posts new public uploads to the Telegram group. Run it separately only if you want those notifications too.

## Tests

```bash
python -m unittest discover -s tests -v
```

Never commit `.env`, `client_secret.json`, `token.json`, database files, or downloaded videos.
