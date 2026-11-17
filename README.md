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

## Render Free only (no Supabase / no Termux / no localhost)

The Flask dashboard and Telegram webhook run on Render Free. The database is SQLite at /tmp/manager.db and temporary videos/thumbnails/OAuth token are stored under /tmp. There is no external database or file-storage service.

### Render settings

- Repository: coodwithyogesh-blip/bookish-octo-memory
- Branch: main after this pull request is merged
- Build command: pip install -r requirements.txt
- Start command: gunicorn wsgi:app --workers 1 --threads 8 --timeout 120
- Health check path: /healthz
- No persistent disk and no Supabase variables are required.

### Render environment variables

Set the values used by your app:
- TELEGRAM_BOT_TOKEN
- OWNER_TELEGRAM_ID
- TELEGRAM_GROUP_ID (or use /bindgroup)
- RENDER_EXTERNAL_URL = your complete Render URL
- TELEGRAM_WEBHOOK_SECRET = a random secret of at least 32 characters
- GEMINI_API_KEY
- GEMINI_MODEL and GEMINI_IMAGE_MODEL
- DASHBOARD_PASSWORD
- MANAGER_API_TOKEN
- APP_SECRET
- YOUTUBE_CLIENT_SECRETS_FILE=/etc/secrets/client_secret.json
- YOUTUBE_TOKEN_FILE=/tmp/token.json
- DATABASE_PATH=/tmp/manager.db
- MEDIA_DIR=/tmp/media
- AUTO_UPLOAD_DEFAULT=false
- AUTO_UPLOAD_PRIVACY=private
- AUTO_REPLY_ENABLED=false
- TIMEZONE=Asia/Kolkata

In Render Advanced → Secret Files, add client_secret.json with your complete Google OAuth client JSON. Set YOUTUBE_CLIENT_SECRETS_FILE=/etc/secrets/client_secret.json.

### Telegram webhook

At startup the app registers https://YOUR-RENDER-SERVICE.onrender.com/telegram/webhook with Telegram. No local polling process is needed. Add the bot to your group, disable Group Privacy in @BotFather, remove/re-add the bot, then send /bindgroup as owner.

### Important Render Free limits

- Render Free sleeps after inactivity. Telegram webhook requests can wake it, but scheduled background jobs and continuous comment monitoring are not guaranteed while asleep.
- The SQLite database and files under /tmp are temporary. A service restart, redeploy, or instance replacement can erase the video queue, comment history, pending thumbnails, and saved YouTube OAuth token. You may need to reconnect YouTube and re-upload pending videos.
- Keep uploads small: Telegram's hosted Bot API download limit is 20 MB.
- This setup is suitable for testing/light use. It is not persistent storage or guaranteed 24/7 automation.

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

While Render is awake, the scheduler checks uploaded-video comments every two minutes. The /comments command and dashboard inbox also trigger an on-demand refresh. Use `/comments` to list pending items, `/reply COMMENT_ID your text` to post your own reply, `/approve COMMENT_ID` or the inline Approve button to post the AI suggestion, `/skip COMMENT_ID` to dismiss, and `/deletecomment COMMENT_ID` to delete. Automatic AI replies are OFF by default; use `/autoreply on` only after you intentionally enable unattended replies. The dashboard's Comment inbox also supports manual replies.

## Tests

```bash
python -m unittest discover -s tests -v
```

Never commit `.env`, `client_secret.json`, `token.json`, database files, or downloaded videos.
