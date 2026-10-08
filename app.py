import os
import re
import hmac
import hashlib
import json
import logging
from datetime import datetime
import time
from zoneinfo import ZoneInfo
import secrets
import threading
from collections import OrderedDict, deque
from contextlib import closing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, g, jsonify, render_template, request, Response, send_from_directory
from urllib.parse import parse_qsl

app = Flask(__name__)

# Make Flask's logger visible under Gunicorn (it forwards the "gunicorn.error" handlers).
_gunicorn_logger = logging.getLogger("gunicorn.error")
if _gunicorn_logger.handlers:
    app.logger.handlers = _gunicorn_logger.handlers
    app.logger.setLevel(_gunicorn_logger.level)

DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
ADMIN_ID = os.getenv("ADMIN_ID")
DHAKA = ZoneInfo("Asia/Dhaka")

MIN_ACTIVE_DAYS = 5
ACTIVE_DAYS_CAP = 20
ACTIVE_HOURS_CAP = 20
MESSAGE_COUNT_CAP = 500
WEIGHT_DAYS = 0.40
WEIGHT_TIME = 0.35
WEIGHT_MESSAGES = 0.25

USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{1,32}$")


# ---------------------------------------------------------------------------
# Startup configuration validation (fail fast with a clear message)
# ---------------------------------------------------------------------------
def _validate_config():
    """Validate required environment variables once at import/startup time.

    Required: DATABASE_URL, BOT_TOKEN, GROUP_ID, ADMIN_ID.
    Optional (existing behaviour preserved): PUBLIC_URL, MEDIA_DIR, PORT.
    """
    problems = []
    if not DATABASE_URL:
        problems.append("DATABASE_URL is missing")
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN is missing")
    elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
        problems.append("BOT_TOKEN does not look like a Telegram bot token")
    group_id = admin_id = None
    try:
        group_id = int(GROUP_ID)
    except (TypeError, ValueError):
        problems.append("GROUP_ID is missing or not an integer (example: -1001234567890)")
    try:
        admin_id = int(ADMIN_ID)
    except (TypeError, ValueError):
        problems.append("ADMIN_ID is missing or not an integer")
    if problems:
        message = "Configuration error: " + "; ".join(problems) + \
            ". Set the missing environment variables on the Render Web Service and redeploy."
        app.logger.critical(message)
        raise RuntimeError(message)
    return group_id, admin_id


GROUP_ID_INT, ADMIN_ID_INT = _validate_config()

# ---------------------------------------------------------------------------
# Telegram HTTP session (connection re-use for all Bot API calls)
# ---------------------------------------------------------------------------
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
TG_SESSION = requests.Session()
TG_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0))

# ---------------------------------------------------------------------------
# Bounded in-memory avatar cache (LRU + TTL) and in-flight de-duplication
# ---------------------------------------------------------------------------
PHOTO_CACHE = OrderedDict()           # user_id -> {"time", "data", "content_type", "failed"}
PHOTO_CACHE_MAX = 300                 # hard cap on entries so memory cannot grow without bound
PHOTO_CACHE_TTL = 21600               # 6 hours (same as before)
PHOTO_FAILURE_TTL = 60                # retry temporary Telegram failures quickly (same as before)
PHOTO_CACHE_LOCK = threading.Lock()
AVATAR_IN_FLIGHT = {}                 # user_id -> Future (prevents duplicate concurrent Telegram calls)
AVATAR_WAIT_SECONDS = 10              # how long /avatar waits for a fresh fetch before sending a placeholder
AVATAR_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="avatar")

# Allowlist of Telegram user IDs that the activity bot actually tracks for GROUP_ID.
TRACKED_USERS = set()
TRACKED_USERS_TIME = 0.0
TRACKED_USERS_TTL = 600               # refresh the allowlist from the DB at most every 10 minutes
TRACKED_USERS_LOCK = threading.Lock()

# Short-term leaderboard cache keyed by month.
LEADERBOARD_CACHE = {}                # month -> {"time", "rows"}
LEADERBOARD_CACHE_TTL = 45            # seconds (target 30-60s)
LEADERBOARD_CACHE_LOCK = threading.Lock()

# Temporary card media.
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/tmp/odvut_activity_media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_TTL = 3600
MEDIA_MAX_PNG = 6 * 1024 * 1024       # existing PNG limit (download / story)
MEDIA_MAX_JPEG = 5 * 1024 * 1024      # Telegram InlineQueryResultPhoto limit
MEDIA_EXTENSIONS = {"png": "image/png", "jpg": "image/jpeg"}
UPLOAD_WINDOW = 600                   # per-user upload protection: max UPLOAD_MAX uploads per 10 minutes
UPLOAD_MAX = 12
UPLOAD_MIN_INTERVAL = 0.3             # seconds between two uploads of the same user (burst guard only)
UPLOAD_LOG = {}                       # user_id -> deque[timestamps]
UPLOAD_LOCK = threading.Lock()

# Static asset version for cache busting (changes automatically on every deploy).
def _asset_version():
    try:
        static_dir = Path(app.static_folder)
        stamp = max(int(p.stat().st_mtime) for p in static_dir.iterdir() if p.is_file())
        return str(stamp)
    except Exception:
        return str(int(time.time()))


ASSET_VERSION = _asset_version()

AVATAR_PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
    b'<rect width="96" height="96" rx="48" fill="#252b3a"/><circle cx="48" cy="38" r="17" fill="#8b93a7"/>'
    b'<path d="M19 80c4-17 15-26 29-26s25 9 29 26" fill="#8b93a7"/></svg>'
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def query_rows(sql, params=()):
    """Run a read-only query and always close the connection afterwards.

    psycopg2's ``with conn:`` only ends the transaction; ``closing`` guarantees the
    socket is released even when an exception is raised.
    """
    with closing(db()) as conn:
        conn.autocommit = True
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def current_month():
    return datetime.now(DHAKA).strftime("%Y-%m")


def valid_month(value):
    if not value:
        return current_month()
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("Invalid month")
    parsed = datetime.strptime(value, "%Y-%m")
    if parsed.strftime("%Y-%m") != value:
        raise ValueError("Invalid month")
    return value


def score(row):
    # NOTE: formula intentionally identical to the activity bot. Do not change.
    days = min(max(float(row.get("active_days") or 0), 0), ACTIVE_DAYS_CAP) / ACTIVE_DAYS_CAP * 100
    hours = (row.get("activity_time_seconds") or 0) / 3600
    time_component = min(max(hours, 0), ACTIVE_HOURS_CAP) / ACTIVE_HOURS_CAP * 100
    messages = min(max(float(row.get("message_count") or 0), 0), MESSAGE_COUNT_CAP) / MESSAGE_COUNT_CAP * 100
    return round(days * WEIGHT_DAYS + time_component * WEIGHT_TIME + messages * WEIGHT_MESSAGES, 2)


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _load_rows_from_db(month):
    rows = query_rows("""
        SELECT user_id, username, first_name, month,
               message_count, active_days, activity_time_seconds
        FROM public.activity_logs
        WHERE chat_id = %s AND month = %s
    """, (GROUP_ID_INT, month))
    for r in rows:
        r["score"] = score(r)
        r["estimated_time"] = duration(r.get("activity_time_seconds"))
        r["eligible"] = int(r.get("active_days") or 0) >= MIN_ACTIVE_DAYS
    rows = sorted(rows, key=lambda r: (-r["score"], -int(r.get("active_days") or 0), -int(r.get("activity_time_seconds") or 0), -int(r.get("message_count") or 0), int(r["user_id"])))
    # Every tracked member seen here is allowed to have an avatar served.
    _allow_tracked_users(int(r["user_id"]) for r in rows)
    return rows


def fetch_rows(month, use_cache=True):
    """Return all rows for the month (sorted, scored). Cached per month for a short time."""
    now = time.time()
    if use_cache:
        with LEADERBOARD_CACHE_LOCK:
            entry = LEADERBOARD_CACHE.get(month)
        if entry and now - entry["time"] < LEADERBOARD_CACHE_TTL:
            return entry["rows"]
    rows = _load_rows_from_db(month)
    with LEADERBOARD_CACHE_LOCK:
        LEADERBOARD_CACHE[month] = {"time": time.time(), "rows": rows}
        # Keep the cache tiny: only a handful of months are ever viewed.
        if len(LEADERBOARD_CACHE) > 6:
            oldest = min(LEADERBOARD_CACHE, key=lambda k: LEADERBOARD_CACHE[k]["time"])
            LEADERBOARD_CACHE.pop(oldest, None)
    return rows


def public_row(r, rank=None):
    return {
        "user_id": int(r["user_id"]),
        "username": r.get("username") or "",
        "first_name": r.get("first_name") or "Member",
        "score": r["score"],
        "rank": rank,
        "eligible": r["eligible"],
    }


# ---------------------------------------------------------------------------
# Avatar allowlist (only users tracked by the bot for GROUP_ID, plus the admin)
# ---------------------------------------------------------------------------
def _allow_tracked_users(user_ids):
    with TRACKED_USERS_LOCK:
        TRACKED_USERS.update(int(u) for u in user_ids)


def is_tracked_user(user_id):
    global TRACKED_USERS_TIME
    user_id = int(user_id)
    if user_id == ADMIN_ID_INT:
        return True
    with TRACKED_USERS_LOCK:
        if user_id in TRACKED_USERS:
            return True
        stale = time.time() - TRACKED_USERS_TIME > TRACKED_USERS_TTL
    if not stale:
        return False
    try:
        rows = query_rows("SELECT DISTINCT user_id FROM public.activity_logs WHERE chat_id = %s", (GROUP_ID_INT,))
        with TRACKED_USERS_LOCK:
            TRACKED_USERS.update(int(r["user_id"]) for r in rows)
            TRACKED_USERS_TIME = time.time()
            return user_id in TRACKED_USERS
    except Exception as exc:
        app.logger.warning("Tracked-user allowlist refresh failed: %s", exc)
        with TRACKED_USERS_LOCK:
            # Avoid hammering the DB while it is unavailable.
            TRACKED_USERS_TIME = time.time() - TRACKED_USERS_TTL + 30
        return False


# ---------------------------------------------------------------------------
# Avatar cache + Telegram fetch
# ---------------------------------------------------------------------------
def _cache_get(user_id):
    """Return the cached entry if still fresh, evicting it when expired."""
    now = time.time()
    with PHOTO_CACHE_LOCK:
        entry = PHOTO_CACHE.get(user_id)
        if not entry:
            return None
        ttl = PHOTO_FAILURE_TTL if entry.get("failed") else PHOTO_CACHE_TTL
        if now - entry["time"] >= ttl:
            PHOTO_CACHE.pop(user_id, None)
            return None
        PHOTO_CACHE.move_to_end(user_id)
        return entry


def _cache_set(user_id, data, content_type, failed):
    now = time.time()
    with PHOTO_CACHE_LOCK:
        PHOTO_CACHE[user_id] = {"time": now, "data": data, "content_type": content_type, "failed": failed}
        PHOTO_CACHE.move_to_end(user_id)
        # Drop expired entries first, then enforce the hard size cap (LRU).
        for key in [k for k, v in PHOTO_CACHE.items()
                    if now - v["time"] >= (PHOTO_FAILURE_TTL if v.get("failed") else PHOTO_CACHE_TTL)]:
            PHOTO_CACHE.pop(key, None)
        while len(PHOTO_CACHE) > PHOTO_CACHE_MAX:
            PHOTO_CACHE.popitem(last=False)


def _fetch_profile_photo(user_id):
    """Fetch the current Telegram profile photo bytes (runs inside AVATAR_EXECUTOR)."""
    try:
        resp = TG_SESSION.get(
            f"{TG_API}/getUserProfilePhotos",
            params={"user_id": user_id, "offset": 0, "limit": 1},
            timeout=6,
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        photos = result.get("photos") or []
        if not photos:
            _cache_set(user_id, None, None, failed=True)
            return None, None

        # Telegram returns several PhotoSize entries; use the largest one (last entry).
        sizes = photos[0] or []
        file_id = (sizes[-1] or {}).get("file_id") if sizes else None
        if not file_id:
            raise RuntimeError("Telegram returned a profile photo without file_id")

        file_resp = TG_SESSION.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=6)
        file_resp.raise_for_status()
        file_path = (file_resp.json().get("result") or {}).get("file_path")
        if not file_path:
            raise RuntimeError("Telegram returned no file_path")

        image_resp = TG_SESSION.get(f"{TG_FILE_API}/{file_path}", timeout=8)
        image_resp.raise_for_status()
        data = image_resp.content
        if not data:
            raise RuntimeError("Telegram returned an empty image")
        content_type = image_resp.headers.get("Content-Type", "image/jpeg")
        _cache_set(user_id, data, content_type, failed=False)
        return data, content_type
    except Exception as exc:
        app.logger.warning("Profile photo fetch failed for user %s: %s", user_id, exc)
        _cache_set(user_id, None, None, failed=True)
        return None, None
    finally:
        with PHOTO_CACHE_LOCK:
            AVATAR_IN_FLIGHT.pop(user_id, None)


def telegram_file_bytes_for_user(user_id, wait=True, timeout=AVATAR_WAIT_SECONDS):
    """Return (bytes, content_type) for a user's profile photo.

    * Cached results are returned immediately.
    * Only one Telegram fetch per user runs at a time; concurrent callers share it.
    * ``wait=False`` just schedules the fetch in the background (used for warm-up).
    """
    if not BOT_TOKEN:
        return None, None
    user_id = int(user_id)
    cached = _cache_get(user_id)
    if cached:
        return cached.get("data"), cached.get("content_type")

    with PHOTO_CACHE_LOCK:
        future = AVATAR_IN_FLIGHT.get(user_id)
        if future is None:
            future = AVATAR_EXECUTOR.submit(_fetch_profile_photo, user_id)
            AVATAR_IN_FLIGHT[user_id] = future
    if not wait:
        return None, None
    try:
        return future.result(timeout=timeout)
    except Exception:
        # Timed out waiting: the fetch keeps running in the background and will
        # populate the cache for the next request.
        return None, None


def warm_avatars(user_ids):
    """Schedule background avatar fetches without blocking the caller."""
    for uid in user_ids:
        try:
            telegram_file_bytes_for_user(uid, wait=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Telegram Mini App authentication
# ---------------------------------------------------------------------------
def validate_telegram_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(data.get("auth_date", "0"))
        if not auth_date or time.time() - auth_date > 86400:
            return None
        user = json.loads(data.get("user", "{}"))
        if not user.get("id"):
            return None
        return user
    except Exception:
        return None


def authenticated_miniapp_user():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return validate_telegram_init_data(init_data)


# ---------------------------------------------------------------------------
# Temporary card media
# ---------------------------------------------------------------------------
def cleanup_media():
    cutoff = time.time() - MEDIA_TTL
    try:
        for path in MEDIA_DIR.iterdir():
            try:
                if path.suffix.lstrip(".") in MEDIA_EXTENSIONS and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def save_media_bytes(data, ext):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)  # /tmp may be cleaned while the service is running
    cleanup_media()
    token = secrets.token_urlsafe(24)
    path = MEDIA_DIR / f"{token}.{ext}"
    path.write_bytes(data)
    return token


def _media_path(token, ext):
    if ext not in MEDIA_EXTENSIONS or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
        return None
    path = MEDIA_DIR / f"{token}.{ext}"
    return path if path.exists() else None


def _upload_allowed(user_id):
    """Per-user upload protection: a small sliding window plus a minimum interval."""
    now = time.time()
    with UPLOAD_LOCK:
        log = UPLOAD_LOG.setdefault(user_id, deque())
        while log and now - log[0] > UPLOAD_WINDOW:
            log.popleft()
        if log and now - log[-1] < UPLOAD_MIN_INTERVAL:
            return False
        if len(log) >= UPLOAD_MAX:
            return False
        log.append(now)
        # Opportunistic cleanup of idle users so this dict never grows unbounded.
        if len(UPLOAD_LOG) > 500:
            for uid in [u for u, q in UPLOAD_LOG.items() if not q or now - q[-1] > UPLOAD_WINDOW]:
                UPLOAD_LOG.pop(uid, None)
        return True


def _sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def public_base():
    return os.getenv("PUBLIC_URL", "").rstrip("/") or f"https://{request.host}"


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def rank_in_rows(rows, user_id):
    """Rank among eligible members using the same ordering as the leaderboard."""
    user_id = int(user_id)
    rank = 0
    for r in rows:
        if r["eligible"]:
            rank += 1
            if int(r["user_id"]) == user_id:
                return rank
    return None


def member_payload(row, month, rank=None, is_admin=False, tg_user=None, extra=None):
    payload = {
        "user_id": int(row["user_id"]),
        "username": row.get("username") or "",
        "first_name": row.get("first_name") or "Member",
        "score": row["score"],
        "rank": rank,
        "eligible": row["eligible"],
        "is_admin": bool(is_admin),
        "active_days": int(row.get("active_days") or 0),
        "message_count": int(row.get("message_count") or 0),
        "estimated_time": duration(row.get("activity_time_seconds")),
        "month": month,
    }
    if tg_user:
        # Telegram's verified Mini App identity is authoritative for the
        # display name/username when available.
        payload["first_name"] = tg_user.get("first_name") or payload["first_name"]
        payload["username"] = tg_user.get("username") or payload["username"]
        payload["photo_url"] = tg_user.get("photo_url") or ""
    if extra:
        payload.update(extra)
    return payload


def admin_payload(tg_user, month):
    """Admins are excluded from tracking; return the synthetic admin card payload."""
    row = {
        "user_id": int(tg_user["id"]),
        "username": "",
        "first_name": "Admin",
        "score": 0,
        "eligible": False,
        "active_days": 0,
        "message_count": 0,
        "activity_time_seconds": 0,
    }
    return member_payload(row, month, rank=None, is_admin=True, tg_user=tg_user)


# ---------------------------------------------------------------------------
# Security headers (Telegram Mini App compatible: no X-Frame-Options: DENY)
# ---------------------------------------------------------------------------
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' https://telegram.org https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "media-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.before_request
def _csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "cross-origin")
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CSP_POLICY.format(nonce=getattr(g, "csp_nonce", "")))
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", month=current_month(), asset_version=ASSET_VERSION,
                           csp_nonce=getattr(g, "csp_nonce", ""))


@app.get("/favicon.ico")
def favicon_ico():
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml",
                               max_age=86400)


@app.get("/health")
def health():
    payload = {"ok": True, "service": "Odvut Activity Leaderboard"}
    if request.args.get("deep") in ("1", "true", "yes"):
        try:
            with closing(db()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            payload["database"] = "ok"
        except Exception as exc:
            app.logger.warning("Deep health check failed: %s", exc)
            payload.update({"ok": False, "database": "unavailable"})
            return jsonify(payload), 503
    return jsonify(payload)


@app.get("/api/leaderboard")
def leaderboard():
    try:
        month = valid_month(request.args.get("month"))
        rows = fetch_rows(month)
        eligible = [r for r in rows if r["eligible"]]
        # Public leaderboard: eligible members only, matching /top rules.
        eligible = eligible[:100]
        data = [public_row(r, i + 1) for i, r in enumerate(eligible)]
        # Warm the avatar cache in the background; never block the leaderboard on Telegram.
        warm_avatars(m["user_id"] for m in data)
        resp = jsonify({"ok": True, "month": month, "count": len(data), "members": data})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid month. Use YYYY-MM."}), 400
    except Exception:
        app.logger.exception("Leaderboard query failed")
        return jsonify({"ok": False, "error": "Leaderboard unavailable."}), 500


@app.get("/api/me")
def my_activity():
    """Return only the authenticated Telegram user's activity/card data.

    Admins are intentionally not tracked by the activity bot, so an admin
    gets a synthetic admin payload instead of a misleading "no activity"
    error. No user ID from the browser is trusted.
    """
    user = authenticated_miniapp_user()
    if not user:
        return jsonify({"ok": False, "error": "Open this Activity Mini App from Telegram."}), 401

    try:
        month = valid_month(request.args.get("month"))
        user_id = int(user["id"])

        # Admins are excluded from activity tracking. Still allow the admin
        # to open their own card and receive the special admin message.
        if user_id == ADMIN_ID_INT:
            return jsonify({"ok": True, "month": month, "member": admin_payload(user, month)})

        # One (cached) query gives both the member's row and their rank.
        rows = fetch_rows(month)
        row = next((r for r in rows if int(r["user_id"]) == user_id), None)
        if not row:
            return jsonify({"ok": False, "error": "এই মাসে আপনার কোনো activity record পাওয়া যায়নি।"}), 404

        payload = member_payload(row, month, rank_in_rows(rows, user_id), False, tg_user=user)
        resp = jsonify({"ok": True, "month": month, "member": payload})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid month. Use YYYY-MM."}), 400
    except Exception:
        app.logger.exception("Mini App activity lookup failed")
        return jsonify({"ok": False, "error": "Activity unavailable."}), 500


@app.post("/api/card-media")
def card_media():
    """Store a freshly rendered personal card temporarily for Telegram sharing/downloading.

    PNG (download / story) up to 6 MB, JPEG (group share) up to 5 MB.
    """
    user = authenticated_miniapp_user()
    if not user:
        return jsonify({"ok": False, "error": "Open this Activity Mini App from Telegram."}), 401
    if not _upload_allowed(int(user["id"])):
        return jsonify({"ok": False, "error": "একটু পরে আবার চেষ্টা করুন। অল্প সময়ে অনেকবার card তৈরি করা হয়েছে।"}), 429
    upload = request.files.get("file")
    if not upload:
        return jsonify({"ok": False, "error": "Card image is missing."}), 400
    data = upload.read(MEDIA_MAX_PNG + 1)
    if not data:
        return jsonify({"ok": False, "error": "Card image is missing."}), 400
    ext = _sniff_image(data)
    if not ext:
        return jsonify({"ok": False, "error": "Only PNG or JPEG card images are accepted."}), 400
    limit = MEDIA_MAX_JPEG if ext == "jpg" else MEDIA_MAX_PNG
    if len(data) > limit:
        return jsonify({"ok": False, "error": "Card image is too large."}), 400
    token = save_media_bytes(data, ext)
    base = public_base()
    return jsonify({
        "ok": True,
        "url": f"{base}/media/activity/{token}.{ext}",
        "download_url": f"{base}/media/download/{token}.{ext}",
        "format": ext,
    })


@app.get("/media/activity/<token>.<ext>")
def card_media_file(token, ext):
    path = _media_path(token, ext)
    if not path:
        return jsonify({"ok": False, "error": "Media expired."}), 404
    return Response(
        path.read_bytes(),
        mimetype=MEDIA_EXTENSIONS[ext],
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="odvut-info-activity-card.{ext}"',
        },
    )


@app.get("/media/download/<token>.<ext>")
def card_media_download(token, ext):
    path = _media_path(token, ext)
    if not path:
        return jsonify({"ok": False, "error": "Media expired."}), 404
    return Response(
        path.read_bytes(),
        mimetype=MEDIA_EXTENSIONS[ext],
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'attachment; filename="odvut-info-activity-card.{ext}"',
            "Access-Control-Allow-Origin": "https://web.telegram.org",
        },
    )


def _share_error_message(description, status_code=None):
    """Map a Telegram API error to a user-friendly message (never exposes token/API details)."""
    d = (description or "").lower()
    if "inline" in d and ("disabled" in d or "bot_inline_disabled" in d):
        return ("Bot-এর Inline Mode বন্ধ আছে। Admin-কে @BotFather → Bot Settings → Inline Mode "
                "চালু করতে বলুন, তারপর আবার চেষ্টা করুন।")
    if "too big" in d or "too large" in d or "file is too" in d or "photo_invalid_dimensions" in d:
        return "Card image-টি Telegram-এর size limit-এর বেশি। আবার চেষ্টা করুন।"
    if "wrong file identifier" in d or "http url" in d or "failed to get http url content" in d \
            or "wrong type of the web page content" in d or "webpage" in d or "photo_invalid" in d \
            or "image_process_failed" in d:
        return "Telegram card image-টি গ্রহণ করতে পারেনি। Card আবার তৈরি করে চেষ্টা করুন।"
    if "user not found" in d or "user_id_invalid" in d or "chat not found" in d:
        return "Telegram আপনার account-টি চিনতে পারেনি। Bot-কে একবার /start দিয়ে আবার চেষ্টা করুন।"
    if status_code == 429 or "too many requests" in d or "retry after" in d:
        return "Telegram সাময়িকভাবে request সীমিত করেছে। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
    if status_code and status_code >= 500:
        return "Telegram server সাময়িকভাবে unavailable। একটু পরে আবার চেষ্টা করুন।"
    return "Telegram card-টি share-এর জন্য প্রস্তুত করতে পারেনি। একটু পরে আবার চেষ্টা করুন।"


@app.post("/api/prepare-share")
def prepare_share():
    """Prepare the user's card as a Telegram media message for WebApp.shareMessage."""
    user = authenticated_miniapp_user()
    if not user:
        return jsonify({"ok": False, "error": "Open this Activity Mini App from Telegram."}), 401
    payload = request.get_json(silent=True) or {}
    media_url = str(payload.get("media_url") or "")
    caption = str(payload.get("caption") or "ODVUT INFO Activity Card")[:1024]
    expected_base = public_base()
    match = re.fullmatch(re.escape(expected_base) + r"/media/activity/([A-Za-z0-9_-]{20,64})\.(png|jpg)", media_url)
    if not match:
        return jsonify({"ok": False, "error": "Invalid card media URL."}), 400
    token, ext = match.group(1), match.group(2)
    path = _media_path(token, ext)
    if not path:
        return jsonify({"ok": False, "error": "Card media-র মেয়াদ শেষ হয়ে গেছে। Card আবার তৈরি করুন।"}), 410
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > MEDIA_MAX_JPEG:
        return jsonify({"ok": False, "error": "Card image-টি Telegram-এর 5 MB limit-এর বেশি। আবার চেষ্টা করুন।"}), 413
    if ext != "jpg":
        # Telegram documents JPEG for InlineQueryResultPhoto; PNG is still attempted for
        # backward compatibility with older clients, but we log it for diagnostics.
        app.logger.info("prepare-share called with a non-JPEG card (%s)", ext)
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "BOT_TOKEN is not configured."}), 500

    result = {
        "type": "photo",
        "id": secrets.token_hex(8),
        "photo_url": media_url,
        "thumbnail_url": media_url,
        "caption": caption,
    }
    try:
        r = TG_SESSION.post(
            f"{TG_API}/savePreparedInlineMessage",
            json={
                "user_id": int(user["id"]),
                "result": json.dumps(result, ensure_ascii=False),
                "allow_group_chats": True,
            },
            timeout=12,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not data.get("ok"):
            description = data.get("description") or f"HTTP {r.status_code}"
            app.logger.error("savePreparedInlineMessage failed (%s): %s", r.status_code, description)
            return jsonify({"ok": False, "error": _share_error_message(description, r.status_code)}), 502
        prepared = data.get("result") or {}
        prepared_id = prepared.get("id")
        if not prepared_id:
            return jsonify({"ok": False, "error": _share_error_message("")}), 502
        return jsonify({"ok": True, "id": prepared_id})
    except requests.Timeout:
        app.logger.warning("savePreparedInlineMessage timed out")
        return jsonify({"ok": False, "error": "Telegram সাড়া দিতে দেরি করছে। একটু পরে আবার চেষ্টা করুন।"}), 504
    except Exception:
        app.logger.exception("Prepared Telegram share failed")
        return jsonify({"ok": False, "error": _share_error_message("")}), 502


def _placeholder_response(max_age):
    return Response(AVATAR_PLACEHOLDER_SVG, mimetype="image/svg+xml",
                    headers={"Cache-Control": f"public, max-age={max_age}"})


@app.get("/avatar/<int:user_id>")
def avatar(user_id):
    # Only users tracked by the activity bot for this group (and the admin) can be
    # looked up; random IDs never trigger a Telegram API call.
    if not is_tracked_user(user_id):
        return _placeholder_response(300)
    data, content_type = telegram_file_bytes_for_user(user_id)
    if not data:
        # Visible fallback instead of a transparent 1x1 image.
        return _placeholder_response(60)
    return Response(data, mimetype=content_type, headers={"Cache-Control": "public, max-age=21600"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
