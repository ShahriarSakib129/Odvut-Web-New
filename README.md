# Odvut Activity Leaderboard

Public, mobile-friendly leaderboard for the Telegram INFO GROUP Activity Bot, with a Telegram Mini App
"Activity Card" (download / Story / group share).

## Project structure
```
app.py               Flask backend (API, Telegram Bot API calls, avatar proxy, card media)
templates/index.html Single page (Jinja injects the current month, asset version and CSP nonce)
static/app.js        Frontend logic (leaderboard, Mini App auth, card export/share, theme)
static/style.css     Styles (dark/light theme, activity card, fixed-size export stage)
static/favicon.svg   Favicon
requirements.txt     Python dependencies (Flask, gunicorn, psycopg2-binary, requests)
.python-version      Python version used by Render
```

## Features
- Current month leaderboard, optional month selector (`YYYY-MM`)
- Eligible-only public leaderboard (minimum 5 active days), top 100
- Same 0–100 Activity Score formula as the bot: active days 40% + estimated activity time 35% + messages 25%
- No public member search or personal-activity lookup; leaderboard shows profile name, profile photo and score only
- Telegram Mini App support with server-side `initData` verification (HMAC, 24h validity)
- `Get Your Activity Card` button with a professional Activity Card
- Fixed-size card export (1080×1440) on every device: PNG download, Telegram Story, group-chat sharing (JPEG)
- Admin Activity Card funny-comment mode (admins are not tracked by the bot)
- Short-term leaderboard cache (45 s, per month) to protect the database from refresh spam
- Non-blocking, de-duplicated profile-photo warm-up with a bounded in-memory cache
- `/health` endpoint for Render/UptimeRobot (+ optional `/health?deep=1` database check)
- Supabase PostgreSQL as the only data store (read-only from this service)

## Telegram profile picture
The website gets the member's **current Telegram profile picture** server-side through the Telegram Bot API and
serves it through `/avatar/<telegram_user_id>`. The browser never receives `BOT_TOKEN`.

- Only user IDs that the activity bot tracks for `GROUP_ID` (plus `ADMIN_ID`) can be requested; any other ID gets a
  placeholder without calling Telegram.
- Photos are cached in memory for 6 hours (failures for 60 s) in a bounded LRU cache (300 entries).
- The leaderboard API returns immediately; photos are fetched in the background and each `/avatar` request waits at
  most 10 s before sending a placeholder (the fetch keeps running and fills the cache for the next request).

The existing activity bot does **not** need a new database column for this feature.

## Environment variables
Set these on the **website's Render Web Service** (environment variables are service-specific):

### Required (the app refuses to start without them and logs a clear error)
| Variable | Description |
|---|---|
| `DATABASE_URL` | Same Supabase PostgreSQL connection string used by the bot. Use the **Supavisor pooler** URL (Render has no outbound IPv6; the direct `db.<ref>.supabase.co` host is IPv6-only on free plans). |
| `BOT_TOKEN` | **Same Telegram bot token used by the activity bot**; server-side only |
| `GROUP_ID` | The INFO GROUP numeric chat ID (e.g. `-1001234567890`) |
| `ADMIN_ID` | Telegram user ID of the main admin (admin card behaviour, avatar allowlist) |

### Optional
| Variable | Default | Description |
|---|---|---|
| `PUBLIC_URL` | `https://<request host>` | Public HTTPS base URL of this website. Set it if you use a custom domain so that card media URLs sent to Telegram are correct. |
| `MEDIA_DIR` | `/tmp/odvut_activity_media` | Directory for temporary card images (expire after ~1 hour). Ephemeral storage is fine. |
| `PORT` | `10000` | Provided automatically by Render. |

Do not put `BOT_TOKEN` or `DATABASE_URL` in HTML, JavaScript, GitHub, or a public `.env` file.

## Render
- **Runtime:** Python (version pinned in `.python-version`)
- **Build command:** `pip install -r requirements.txt`
- **Start command:**
  `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 120 --keep-alive 5 app:app`
- **Health check path:** `/health`

One worker keeps memory tiny for the free plan; 8 threads let slow Telegram/profile-photo requests run in parallel
instead of blocking every visitor behind one request.

## UptimeRobot
Monitor the website's `/health` URL with an HTTP monitor. It returns HTTP 200 when the web service is running and
never touches the database (fast and cheap). `/health?deep=1` additionally runs `SELECT 1` and returns 503 if the
database is unreachable — use it for a second, less frequent monitor if you want database alerts.

## Telegram Mini App
The website can be opened as a Telegram Mini App. The `Get Your Activity Card` button uses Telegram `initData` and the
server verifies it before returning the logged-in user's own activity. A browser-supplied Telegram user ID is never
trusted.

For the activity bot, add `WEBAPP_URL` with the exact HTTPS Render URL of this website. The bot can expose the Mini App
with `/leaderboard` and the Telegram chat menu button.

### Requirements / BotFather configuration
- The Mini App URL must be HTTPS (Render provides this).
- **Share to Group** uses `savePreparedInlineMessage` + `WebApp.shareMessage` (Bot API 8.0, Telegram 11.x+). If
  Telegram answers that inline mode is disabled, enable it in **@BotFather → /mybots → Bot Settings → Inline Mode**.
  The website shows a clear message in that case.
- **Telegram Story** uses `WebApp.shareToStory` (Bot API 7.8+). Story links (`widget_link`) are a **Telegram Premium**
  feature; for non-Premium users the story is shared without the link.
- **Download** uses `WebApp.downloadFile` (Bot API 8.0+); older clients fall back to a normal browser download.

### Telegram media sharing
The Mini App temporarily uploads the rendered card to the website so Telegram can fetch it over HTTPS:
- PNG (≤ 6 MB) for download / Story,
- JPEG (≤ 5 MB, solid background) for group sharing, because `InlineQueryResultPhoto` requires JPEG.

Uploads require a valid Mini App `initData`, are limited per user (12 per 10 minutes), and expire after about one hour.

## Security headers
The app sends `Content-Security-Policy` (nonce-based scripts; allows `telegram.org`, `cdnjs.cloudflare.com`,
same-origin API/avatars/media), `X-Content-Type-Options`, `Referrer-Policy` and `Permissions-Policy`.
`X-Frame-Options`/`frame-ancestors` are intentionally **not** set so Telegram Web can embed the Mini App.

## Local run
```
export DATABASE_URL=... BOT_TOKEN=... GROUP_ID=... ADMIN_ID=...
pip install -r requirements.txt
python app.py            # http://localhost:10000
```
