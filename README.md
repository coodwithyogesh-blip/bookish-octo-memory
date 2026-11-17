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

Scheduled publishing, Telegram comment moderation/replies, dashboard comment inbox, dashboard settings, and recorded-video YouTube Live controls are implemented in the codebase. The live stream can start immediately or be scheduled for a future India-time date/time, and loops the selected recording continuously until you stop it. It requires FFmpeg, an eligible YouTube channel with live streaming enabled, and an always-on host; the process stops if the host sleeps or restarts. The current live process is held in memory, so configure a persistent, always-on worker for long streams.

## Render-only deployment (no Termux / localhost)

This project is designed to run entirely on Render. Do not start `app.py`, `bot.py`, or a local server on your phone or PC. The Render web service runs the Flask dashboard, Telegram long-polling bot, comment polling, scheduled uploads and recorded-live scheduler in one single Gunicorn process.

1. Push/merge this branch to the deployment branch in GitHub.
2. In Render, create or open a **Web Service** connected to this repository.
3. Build command: `pip install -r requirements.txt`.
4. Start command: `gunicorn wsgi:app --workers 1 --threads 8 --timeout 120` (also set in `Procfile`).
5. Add a persistent disk and mount it at `/var/data`. Keep exactly one running Render instance because Telegram long polling and the in-process scheduler must not run twice.
6. Add a Render Secret File named `client_secret.json` and set `YOUTUBE_CLIENT_SECRETS_FILE=/etc/secrets/client_secret.json`.
7. Set `DATABASE_PATH=/var/data/manager.db`, `MEDIA_DIR=/var/data/media`, and `YOUTUBE_TOKEN_FILE=/var/data/token.json`.
8. Set the environment variables listed below, then deploy.

Required Render environment variables:
- `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID`, and `TELEGRAM_GROUP_ID` (or bind the group with `/bindgroup`).
- `GEMINI_API_KEY`, `GEMINI_MODEL`, and `GEMINI_IMAGE_MODEL`.
- `DASHBOARD_PASSWORD`, `MANAGER_API_TOKEN`, and `APP_SECRET` (all strong, unique secrets).
- `YOUTUBE_CLIENT_SECRETS_FILE=/etc/secrets/client_secret.json`, `YOUTUBE_TOKEN_FILE=/var/data/token.json`.
- `DATABASE_PATH=/var/data/manager.db`, `MEDIA_DIR=/var/data/media`.
- `AUTO_UPLOAD_DEFAULT=false` initially; turn it on from Telegram only after testing.
- `AUTO_UPLOAD_PRIVACY=private` initially.
- `AUTO_REPLY_ENABLED=false` initially; comment replies require human approval by default.
- `TIMEZONE=Asia/Kolkata`.

Use a paid, always-on Render web instance with a persistent disk for reliable 24/7 polling and to keep the database, downloaded videos, and YouTube OAuth token across deploys. Render free web instances can sleep and do not provide the persistent storage needed for this always-on workflow. Do not scale this service to multiple instances while Telegram uses long polling.

Dashboard URL: `https://YOUR-RENDER-SERVICE.onrender.com/`. Set Google OAuth's authorized redirect URI to `https://YOUR-RENDER-SERVICE.onrender.com/oauth/callback`. After deploy, open the dashboard and connect the YouTube channel once. The generated OAuth token is stored on the persistent disk.

## Google YouTube API setup

1. In Google Cloud Console, create/select a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen and add your Google account as a test user if the app is in testing.
3. Create a Web application OAuth client and configure the authorized redirect URI as `https://YOUR-RENDER-SERVICE.onrender.com/oauth/callback`.
4. Download the client JSON as `client_secret.json` and keep it private.
5. Open your deployed Render dashboard, select **Connect YouTube channel**, and authorize the channel.

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

The bot only accepts commands and media from the bound group and owner-approved IDs. Keep group membership controlled. Telegram's hosted Bot API download is limited to 20 MB per file. Files above that limit are not supported by this Telegram-download path; a future Render-hosted browser upload path would be needed for larger source videos.

## YouTube comment management

The Render scheduler checks uploaded-video comments every two minutes and posts new comments to the connected Telegram group. Use `/comments` to list pending items, `/reply COMMENT_ID your text` to post your own reply, `/approve COMMENT_ID` or the inline Approve button to post the AI suggestion, `/skip COMMENT_ID` to dismiss, and `/deletecomment COMMENT_ID` to delete. Automatic AI replies are OFF by default; use `/autoreply on` only after you intentionally enable unattended replies. The dashboard's Comment inbox also supports manual replies.

## Tests

```bash
python -m unittest discover -s tests -v
```

Never commit `.env`, `client_secret.json`, `token.json`, database files, or downloaded videos.
