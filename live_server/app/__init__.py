# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import asyncio
import collections
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dotenv
import redis.asyncio as redis
import socketio
from cachetools import TTLCache
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
from starlette.middleware.sessions import SessionMiddleware

dotenv.load_dotenv(override=True)

from .config import SETTINGS, REDIS_URL
try:
    from .config import EMAIL_SETTINGS
except ImportError:
    EMAIL_SETTINGS = {}

if not SETTINGS.get("SECRET_KEY"):
    raise RuntimeError(
        "SECRET_KEY must be set in config/env. Refusing to start with an "
        "ephemeral fallback — that silently invalidates every session cookie "
        "on restart."
    )
from .database import rooms_collection, transcription_store_collection, transcription_segments_collection, users_collection, init_indexes
from .logger_config import setup_logger, log_exception
from .scribe_manager import ScribeSessionManager
from .email_auth import (
    validate_email_format,
    generate_otp,
    store_otp,
    verify_otp,
    send_otp_email,
    get_or_create_user,
)

# Setup logger
logger = setup_logger(__name__)

# Session manager caches: max 512 concurrent sessions.
# When a session is evicted by cachetools (TTL expiry or capacity), its manager is
# stopped so background tasks are cleaned up promptly.
# TTL is refreshed by every heartbeat (30s) and every audio chunk, so it only
# fires when a session is truly idle. 300s gives plenty of slack for network
# jitter that drops a heartbeat or two without tearing down the ElevenLabs WS.
_MANAGER_CACHE_TTL = 300  # seconds (5 min)
_MANAGER_CACHE_MAX = 512
_YOUTUBE_CACHE_TTL = 60   # seconds; unrelated to manager lifecycle

_MAX_AUDIO_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB in bytes
_BASE64_RE = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')


class _SocketRateLimiter:
    """In-memory sliding-window rate limiter for Socket.IO events."""

    def __init__(self):
        self._timestamps: dict[str, collections.deque] = {}

    def check(self, socket_id: str, event: str, max_calls: int, window: float) -> bool:
        """Return True if the call is allowed, False if rate-limited."""
        now = time.monotonic()
        key = f"{socket_id}:{event}"
        timestamps = self._timestamps.get(key)
        if timestamps is None:
            timestamps = collections.deque()
            self._timestamps[key] = timestamps

        # Prune expired entries -- O(1) per pop with deque
        cutoff = now - window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= max_calls:
            return False

        timestamps.append(now)
        return True

    def cleanup(self, socket_id: str) -> None:
        """Remove all tracking data for a disconnected socket."""
        keys_to_remove = [k for k in self._timestamps if k.startswith(f"{socket_id}:")]
        for k in keys_to_remove:
            del self._timestamps[k]


_socket_limiter = _SocketRateLimiter()


class _ManagerTTLCache(TTLCache):
    """TTLCache that calls manager.stop() when an entry is evicted."""

    def popitem(self):
        key, manager = super().popitem()
        # Schedule the async stop without blocking the eviction path.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(manager.stop())
        except RuntimeError:
            pass  # No running loop (e.g. during interpreter shutdown) — skip.
        return key, manager


active_scribe_managers: TTLCache = _ManagerTTLCache(
    maxsize=_MANAGER_CACHE_MAX, ttl=_MANAGER_CACHE_TTL
)
active_translation_managers: TTLCache = _ManagerTTLCache(
    maxsize=_MANAGER_CACHE_MAX, ttl=_MANAGER_CACHE_TTL
)
# Pending debounce tasks for partial transcription broadcasts, keyed by session_id.
_partial_debounce_tasks: dict = {}
# Per-session locks to prevent concurrent _get_or_create_scribe_manager calls from racing.
_scribe_create_locks: collections.defaultdict = collections.defaultdict(asyncio.Lock)


async def _get_or_create_scribe_manager(session_id, *, force_new: bool = False) -> ScribeSessionManager:
    """Return the existing running ScribeSessionManager for the session, or create a new one.
    Pass force_new=True to unconditionally restart (e.g. after a language change).
    """
    async with _scribe_create_locks[session_id]:
        existing: ScribeSessionManager | None = active_scribe_managers.get(session_id)
        if existing and existing.is_running and not force_new:
            return existing

        if existing is not None:
            asyncio.create_task(existing.stop())

        from .translation_service import get_session_scribe_language
        language_code = await get_session_scribe_language(redis_client, session_id)
        manager = ScribeSessionManager(session_id, on_scribe_transcription, language_code=language_code)
        manager.yt_start_time = await get_youtube_start_time(session_id)
        active_scribe_managers[session_id] = manager
        asyncio.create_task(manager.start())
        return manager


def _get_or_create_translation_manager(session_id):
    """Return existing TranslationQueueManager or create and start a new one."""
    manager = active_translation_managers.get(session_id)
    if not manager:
        from .translation_service import TranslationQueueManager
        manager = TranslationQueueManager(on_translation_completed)
        active_translation_managers[session_id] = manager
        asyncio.create_task(manager.start())
    return manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_indexes()
    _limiter_redis = redis.from_url(REDIS_URL, decode_responses=True)
    await FastAPILimiter.init(_limiter_redis)
    yield
    # Shutdown
    print("Shutting down resources...")
    await FastAPILimiter.close()
    # Close translator and shared HTTP client
    from .translators import close_translator
    await close_translator()

    # Stop all active scribe managers (snapshot first to avoid mutation during iteration)
    for manager in list(active_scribe_managers.values()):
        await manager.stop()

    # Stop all active translation managers
    for manager in list(active_translation_managers.values()):
        await manager.stop()

# Initialize FastAPI app with lifespan
app = FastAPI(lifespan=lifespan)

# Add session middleware.
# SameSite=Lax blocks cross-site POST/DELETE cookie attachment — the main CSRF
# mitigation for /api/session/{sid}/..., /heartbeat, /release-admin. Secure is
# enabled in production so the cookie is never sent over plain HTTP.
_IS_PRODUCTION = os.environ.get("ENVIRONMENT", "development").lower() == "production"
app.add_middleware(
    SessionMiddleware,
    secret_key=SETTINGS["SECRET_KEY"],
    same_site="lax",
    https_only=_IS_PRODUCTION,
)

# Setup templates
timestamp = datetime.now(timezone.utc).timestamp()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["timestamp"] = timestamp

# Mount static files
static_dir = Path("app/static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

mgr = socketio.AsyncRedisManager(REDIS_URL)

# Initialize Socket.IO with ASGI support
sio = socketio.AsyncServer(
    async_mode='asgi',
    client_manager=mgr,
    cors_allowed_origins='*',
    logger=False
)

# Wrap with ASGI application
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

youtube_data_cache: TTLCache = TTLCache(maxsize=256, ttl=_YOUTUBE_CACHE_TTL)

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

def validate_query_param(value: str, param_name: str = "parameter") -> tuple[bool, str]:
    """Validate user input to prevent NoSQL injection. Returns (is_valid, error_message)."""
    if not isinstance(value, str):
        return False, f"Invalid {param_name}: must be a string"
    if not value.strip():
        return False, f"Invalid {param_name}: cannot be empty"

    # Strict validation for session IDs
    if "session" in param_name.lower() or param_name.lower() == "sid":
        if len(value) < 4 or len(value) > 64:
            return False, f"Invalid {param_name}: must be between 4 and 64 characters"
        if not re.match(r'^[a-zA-Z0-9_-]+$', value):
            return False, f"Invalid {param_name}: must contain only alphanumeric characters, hyphens, and underscores"
    else:
        if '$' in value or '.' in value:
            return False, f"Invalid {param_name}: contains prohibited characters"

    return True, ""

def sanitize_query_param(value: str, param_name: str = "parameter") -> str:
    """Validate user input; raise HTTPException on failure. Returns value if valid."""
    is_valid, error_msg = validate_query_param(value, param_name)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    return value

async def _identifier(request: Request) -> str:
    uid = request.session.get("user_uid", "")
    if not uid or len(uid) < 36:
        uid = str(uuid.uuid4())
        request.session["user_uid"] = uid
    func_name = request.scope["route"].endpoint.__name__
    return f"{uid}:{func_name}"


async def _otp_email_identifier(request: Request) -> str:
    """Rate-limit OTP endpoints by submitted email so an attacker cannot
    multiply their quota by opening many connections / rotating cookies."""
    try:
        body = await request.json()
        email = (body.get("email") or "").strip().lower()
    except Exception:
        email = ""
    func_name = request.scope["route"].endpoint.__name__
    if email and validate_email_format(email):
        return f"otp:{email}:{func_name}"
    return await _identifier(request)

async def is_realtime_authorized(session: dict, session_id: str | None = None) -> bool:
    """Check if the socket is authorized to use server-side realtime features.

    Returns True if the user logged in via email and has realtime_enabled=True.

    When the socket session has lost its email (e.g. after a reconnect that
    failed to re-verify), falls back to looking up the room's admin_email
    from MongoDB so the authoritative realtime_enabled flag is always read
    from the users collection.
    """
    email = session.get('email')

    # Fallback: derive email from the room document when the socket session
    # lost it (reconnect, worker migration, Redis TTL expiry, etc.).
    if not email and session_id:
        room = await rooms_collection.find_one({"sid": session_id}, {"admin_email": 1})
        if room:
            email = room.get("admin_email")

    if not email:
        return False

    user_doc = await users_collection.find_one({"email": email})
    return bool(user_doc and user_doc.get("realtime_enabled"))


async def verify_socket_auth(socket_id: str, session_id: str, secret_key: str) -> bool:
    """Verify WebSocket authentication against database. Returns True if valid."""
    if not session_id or not secret_key:
        return False
    is_valid, _ = validate_query_param(session_id, "session_id")
    if not is_valid:
        return False
    is_valid, _ = validate_query_param(secret_key, "secret_key")
    if not is_valid:
        return False
    room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
    return room is not None


async def _ensure_socket_verified(socket_id, session, secret_key, session_id) -> bool:
    """Try to verify an unverified socket. Returns True if now verified, False otherwise.
    Emits an error to the socket on failure."""
    if session.get('verified'):
        return True
    if not secret_key:
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return False
    if not session_id:
        await sio.emit('error', {'message': 'Unauthorized: not in a session room'}, to=socket_id)
        return False
    if not await verify_socket_auth(socket_id, session_id, secret_key):
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return False
    session['verified'] = True
    session['secret_key'] = secret_key
    await sio.save_session(socket_id, session)
    return True



async def _verify_session_admin(request: Request, sid: str):
    """Verify the request user is the admin of the given session. Returns the room doc."""
    user_secret_key = request.session.get("secret_key")
    if not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if room.get("secret_key") != user_secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    return room


# ---------------------------------------------------------------------------
# Email login routes
# ---------------------------------------------------------------------------

def _get_session_email(request: Request) -> str | None:
    return request.session.get("email")


def _is_admin_email(email: str) -> bool:
    return email.lower() in [e.lower() for e in EMAIL_SETTINGS.get("ADMIN_EMAILS", [])]


def _require_admin_email(request: Request):
    email = _get_session_email(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not logged in")
    if not _is_admin_email(email):
        raise HTTPException(status_code=403, detail="Admin access required")
    return email


def _require_logged_in(request: Request) -> tuple[str | None, str | None]:
    """Require the user to be logged in. Returns (email, user_uid)."""
    email = _get_session_email(request)
    user_uid = request.session.get("user_uid")
    if not email or not user_uid:
        return None, None
    return email, user_uid


async def _get_room_owner_email(room: dict) -> str | None:
    """Resolve the owner email from a room document, handling backward-compat admin_uid fallback."""
    email = room.get("admin_email")
    if not email and room.get("admin_uid"):
        doc = await users_collection.find_one({"user_uid": room["admin_uid"]})
        email = doc.get("email") if doc else None
    return email


async def _require_room_owner(request: Request, room: dict) -> None:
    """Raise 403 if the room is owned and the current user is not the owner."""
    owner_email = await _get_room_owner_email(room)
    if owner_email:
        current = _get_session_email(request)
        if not current or current.lower() != owner_email.lower():
            raise HTTPException(status_code=403, detail="This session is owned by another user.")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    email = _get_session_email(request)
    if email:
        target = "/dashboard" if _is_admin_email(email) else "/user-dashboard"
        return RedirectResponse(url=target, status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def dashboard(request: Request):
    _require_admin_email(request)
    users = await users_collection.find({}, {"_id": 0}).to_list(length=1000)
    # Convert datetimes to ISO strings for template rendering
    for u in users:
        if isinstance(u.get("created_at"), datetime):
            u["created_at"] = u["created_at"].isoformat()
        if isinstance(u.get("last_login_at"), datetime):
            u["last_login_at"] = u["last_login_at"].isoformat()
    return templates.TemplateResponse("dashboard.html", {"request": request, "users": users, "current_email": _get_session_email(request)})


@app.get("/user-dashboard", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def user_dashboard(request: Request):
    email, user_uid = _require_logged_in(request)
    if not email or not user_uid:
        return RedirectResponse(url="/login", status_code=302)
    rooms = await rooms_collection.find(
        {"admin_email": email.lower()},
        {"_id": 0, "sid": 1, "created_at": 1, "admin_last_heartbeat": 1,
         "audio_bytes": 1, "audio_duration_secs": 1}
    ).sort("created_at", -1).to_list(length=200)
    for r in rooms:
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
        if isinstance(r.get("admin_last_heartbeat"), datetime):
            r["admin_last_heartbeat"] = r["admin_last_heartbeat"].isoformat()
    max_audio_secs = max((r.get("audio_duration_secs") or 0 for r in rooms), default=0)
    for r in rooms:
        dur = r.get("audio_duration_secs") or 0
        r["audio_pct"] = min(int(dur / max_audio_secs * 100), 100) if max_audio_secs > 0 else 0
    is_realtime_enabled = await is_realtime_authorized(request.session)
    response = templates.TemplateResponse("user_dashboard.html", {
        "request": request,
        "rooms": rooms,
        "current_email": email,
        "is_realtime_enabled": is_realtime_enabled,
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.post("/api/users/{email}/realtime", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def set_user_realtime(request: Request, email: str):
    """Toggle realtime_enabled for a user (admin only)."""
    _require_admin_email(request)
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="'enabled' must be a boolean")
    from pymongo import ReturnDocument
    result = await users_collection.find_one_and_update(
        {"email": email.lower()},
        {"$set": {"realtime_enabled": enabled}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return {"email": result["email"], "realtime_enabled": result["realtime_enabled"]}


@app.post("/auth/send-otp", dependencies=[Depends(RateLimiter(times=3, seconds=60, identifier=_otp_email_identifier))])
async def send_otp(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    otp = generate_otp()
    print(f"OTP for {email}: {otp}")
    await store_otp(redis_client, email, otp)
    try:
        await send_otp_email(email, otp, EMAIL_SETTINGS)
    except Exception as e:
        log_exception(logger, e, f"Failed to send OTP email to {email}")
        raise HTTPException(status_code=500, detail="Failed to send email")
    return {"status": "sent"}


@app.post("/auth/verify-otp", dependencies=[Depends(RateLimiter(times=10, seconds=60, identifier=_otp_email_identifier))])
async def verify_otp_endpoint(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    otp = body.get("otp", "").strip()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    if not otp or not re.match(r'^\d{6}$', otp):
        raise HTTPException(status_code=400, detail="Invalid OTP format")

    if not await verify_otp(redis_client, email, otp):
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    # Reuse existing user_uid from session or generate a new one
    user_uid = request.session.get("user_uid") or str(uuid.uuid4())
    await get_or_create_user(users_collection, email, user_uid)

    request.session["email"] = email.lower()
    request.session["user_uid"] = user_uid

    is_admin = _is_admin_email(email)
    return {"status": "ok", "is_admin": is_admin, "redirect": "/dashboard" if is_admin else "/user-dashboard"}


@app.get("/api/session/{sid}/languages", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def get_session_languages_endpoint(request: Request, sid: str):
    """Get the current translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    from .translation_service import get_session_languages
    languages = await get_session_languages(redis_client, sid)
    return {"languages": languages}


@app.post("/api/session/{sid}/languages", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def update_session_languages_endpoint(request: Request, sid: str):
    """Update the translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    body = await request.json()
    languages = body.get("languages")
    if not isinstance(languages, list) or not languages:
        raise HTTPException(status_code=400, detail="languages must be a non-empty list")
    for lang in languages:
        if not isinstance(lang, str) or not lang.strip():
            raise HTTPException(status_code=400, detail="Each language must be a non-empty string")
        if '$' in lang or len(lang) > 32:
            raise HTTPException(status_code=400, detail=f"Invalid language value: {lang}")
    languages = [lang.strip() for lang in languages]

    from .translation_service import save_session_languages
    await save_session_languages(redis_client, sid, languages)
    return {"languages": languages}


@app.get("/api/session/{sid}/keywords", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def get_session_keywords_endpoint(request: Request, sid: str):
    """Get the current keywords and locked keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    from .translation_service import get_keywords_and_locked
    keywords, locked_keywords = await get_keywords_and_locked(redis_client, sid)
    sorted_keywords = sorted(keywords, key=lambda k: keywords[k], reverse=True)
    return {"keywords": sorted_keywords, "locked_keywords": locked_keywords}


@app.post("/api/session/{sid}/keywords", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def update_session_keywords_endpoint(request: Request, sid: str):
    """Update the keywords and locked keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    body = await request.json()
    keywords = body.get("keywords")
    if not isinstance(keywords, list):
        raise HTTPException(status_code=400, detail="keywords must be a list")
    for kw in keywords:
        if not isinstance(kw, str) or not kw.strip():
            raise HTTPException(status_code=400, detail="Each keyword must be a non-empty string")
        if '$' in kw or len(kw) > 128:
            raise HTTPException(status_code=400, detail=f"Invalid keyword value: {kw}")
    keywords = [kw.strip() for kw in keywords]

    from .translation_service import save_current_keywords, save_locked_keywords, get_keywords_and_locked
    existing_keywords, _ = await get_keywords_and_locked(redis_client, sid)
    keywords_dict = {kw: existing_keywords.get(kw, 1) for kw in keywords}
    await save_current_keywords(redis_client, sid, keywords_dict)

    if "locked_keywords" in body:
        locked_keywords = body.get("locked_keywords")
        if not isinstance(locked_keywords, list):
            raise HTTPException(status_code=400, detail="locked_keywords must be a list")
        for kw in locked_keywords:
            if not isinstance(kw, str) or not kw.strip():
                raise HTTPException(status_code=400, detail="Each locked keyword must be a non-empty string")
            if '$' in kw or len(kw) > 128:
                raise HTTPException(status_code=400, detail=f"Invalid locked keyword value: {kw}")
        locked_keywords = [kw.strip() for kw in locked_keywords]
        await save_locked_keywords(redis_client, sid, locked_keywords)
    else:
        locked_keywords = None

    result = {"keywords": list(keywords_dict.keys())}
    if locked_keywords is not None:
        result["locked_keywords"] = locked_keywords
    return result


@app.get("/api/session/{sid}/scribe-language", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def get_session_scribe_language_endpoint(request: Request, sid: str):
    """Get the forced detect language for Scribe (empty means auto-detect)."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    from .translation_service import get_session_scribe_language
    language = await get_session_scribe_language(redis_client, sid)
    return {"language": language}


@app.post("/api/session/{sid}/scribe-language", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def update_session_scribe_language_endpoint(request: Request, sid: str):
    """Set or clear the forced detect language for Scribe."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    body = await request.json()
    language = body.get("language", "")
    if not isinstance(language, str):
        raise HTTPException(status_code=400, detail="language must be a string")
    language = language.strip().lower()
    if language and not re.fullmatch(r'[a-z]{2,3}', language):
        raise HTTPException(status_code=400, detail="language must be an ISO 639-1 (2-char) or ISO 639-3 (3-char) code")

    from .translation_service import save_session_scribe_language
    await save_session_scribe_language(redis_client, sid, language)

    # Restart the active scribe manager so the new language takes effect immediately.
    if active_scribe_managers.get(sid):
        await _get_or_create_scribe_manager(sid, force_new=True)

    return {"language": language}


@app.get("/api/session/{sid}/audio-usage", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def get_session_audio_usage_endpoint(request: Request, sid: str):
    """Return audio buffer usage stats for the active scribe session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    manager = active_scribe_managers.get(sid)
    if not manager:
        return {"audio_bytes": 0, "audio_chunks": 0, "audio_duration_secs": 0.0}
    return manager.get_usage_stats()


async def get_youtube_start_time(video_id: str) -> float | None:
    """
    Get the actual stream start time for a YouTube video using YouTube Data API v3.
    Returns the actualStartTime if available, otherwise None.
    """
    data = None
    if video_id in youtube_data_cache and youtube_data_cache[video_id] is not None:
        data = youtube_data_cache[video_id]
    elif video_id in youtube_data_cache: # Negative cache
        return None
    else:
        api_key = SETTINGS["YOUTUBE_API_KEY"]
        if not api_key:
            print("Warning: YOUTUBE_API_KEY environment variable not set")
            return None
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            'part': 'liveStreamingDetails',
            'id': video_id,
            'key': api_key
        }
        try:
            from .translation_service import get_async_client
            client = get_async_client()
            response = await client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            
            data = response.json()
            
            if 'items' in data and len(data['items']) > 0:
                data = data['items'][0]
                youtube_data_cache[video_id] = data
            else:
                youtube_data_cache[video_id] = None # negative cache

        except Exception as e:
            log_exception(logger, e, "Error fetching YouTube data")
            return None
    
    if data and 'liveStreamingDetails' in data:
        live_details = data['liveStreamingDetails']
        # Check for actualStartTime (when stream actually started)
        if 'actualStartTime' in live_details:
            return datetime.fromisoformat(live_details['actualStartTime']).timestamp()
        # Fallback to scheduledStartTime if actualStartTime is not available
        elif 'scheduledStartTime' in live_details:
            return datetime.fromisoformat(live_details['scheduledStartTime']).timestamp()
    return None

async def get_cached_transcription(id) -> Any:
    # Try fetching committed transcriptions (ZSET) and partial from Redis
    try:
        # ZSET for committed transcriptions, stored as JSON strings with start_time as score
        # Fetch only the latest 10 entries to limit data transfer; translation context needs only the last 3
        committed_json_list = await redis_client.zrevrangebyscore(
            f"transcription:{id}:list", "+inf", "-inf", start=0, num=10
        )
        committed_json_list = list(reversed(committed_json_list))
        meta_json = await redis_client.get(f"transcription:{id}:meta")
        partial_json = await redis_client.get(f"transcription:{id}:partial")

        REDIS_TTL = 3600

        data = None
        if committed_json_list:
            data = {
                "transcriptions": [json.loads(j) for j in committed_json_list],
                "stream_start_time": None
            }
            if meta_json:
                meta = json.loads(meta_json)
                data["stream_start_time"] = meta.get("stream_start_time")

            # Sliding expiry: reset TTL on each read so active sessions stay warm
            pipe = redis_client.pipeline()
            pipe.expire(f"transcription:{id}:list", REDIS_TTL)
            pipe.expire(f"transcription:{id}:meta", REDIS_TTL)
            if partial_json:
                pipe.expire(f"transcription:{id}:partial", REDIS_TTL)
            await pipe.execute()

        # Migration/Fallback: Check if old String-style cache exists
        if data is None:
            old_committed_json = await redis_client.get(f"transcription:{id}")
            if old_committed_json:
                data = json.loads(old_committed_json)
                # Migrate to ZSET in background
                asyncio.create_task(migrate_to_zset(id, data))

        # Final fallback to DB if no data in Redis
        if data is None:
            segments, store = await _load_segments_from_db(id, limit=1000)
            if segments:
                data = {
                    "transcriptions": segments,
                    "stream_start_time": store.get("stream_start_time") if store else None
                }
                asyncio.create_task(migrate_to_zset(id, data))
            else:
                data = {"transcriptions": []}

        # Merge partial data if exists
        if partial_json:
            data["partial"] = json.loads(partial_json)

        return data
    except Exception as e:
        log_exception(logger, e, "Redis/DB error in get_cached_transcription")
        return {"transcriptions": []}

async def migrate_to_zset(id, data):
    """Helper to migrate old list storage to Redis ZSET and Meta keys"""
    try:
        if not data.get("transcriptions"):
            return
            
        # Add to ZSET
        zset_key = f"transcription:{id}:list"
        pipe = redis_client.pipeline()
        for seg in data["transcriptions"]:
            pipe.zadd(zset_key, {json.dumps(seg): seg["start_time"]})
        # Cap ZSET size by removing oldest entries beyond limit
        pipe.zremrangebyrank(zset_key, 0, -1001)

        # Set Meta
        meta = {"stream_start_time": data.get("stream_start_time")}
        pipe.setex(f"transcription:{id}:meta", 3600, json.dumps(meta))
        
        # Set expiry for ZSET
        pipe.expire(f"transcription:{id}:list", 3600)
        
        # Delete old key
        pipe.delete(f"transcription:{id}")
        await pipe.execute()
    except Exception as e:
        log_exception(logger, e, f"Migration error for {id}")


async def _load_segments_from_db(sid: str, limit: int | None = None) -> tuple[list, dict | None]:
    """Fetch committed segments and session metadata from DB in parallel.
    Falls back to the legacy transcription_store embedded array if the segments
    collection has no data (for sessions written before the migration).
    """
    query = transcription_segments_collection.find(
        {"sid": sid, "partial": {"$ne": True}},
        {"_id": 0, "sid": 0, "created_at": 0}
    ).sort("start_time", 1)
    if limit:
        query = query.limit(limit)
    segments, store = await asyncio.gather(
        query.to_list(length=limit),
        transcription_store_collection.find_one({"sid": sid})
    )
    if not segments and store and store.get("transcriptions"):
        segments = store.get("transcriptions", [])
    return segments, store


async def save_segment_background(sid, segment, stream_start_time):
    """Save segment to MongoDB transcription_segments collection"""
    try:
        now = datetime.now(timezone.utc)
        await transcription_segments_collection.insert_one({**segment, "sid": sid, "created_at": now})
        await transcription_store_collection.update_one(
            {"sid": sid},
            {"$set": {"stream_start_time": stream_start_time, "updated_at": now}},
            upsert=True
        )
    except Exception as e:
        log_exception(logger, e, "Error saving to MongoDB")


# FastAPI Routes
@app.get("/", response_class=HTMLResponse)
async def hello_world(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "email": _get_session_email(request)})

@app.get("/download/{id}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def download(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    room, (segments, meta) = await asyncio.gather(
        rooms_collection.find_one({"sid": id}, {"admin_email": 1, "admin_uid": 1}),
        _load_segments_from_db(id),
    )
    if room:
        await _require_room_owner(request, room)
    if not segments:
        raise HTTPException(status_code=404, detail="Session not found")

    updated_at = meta.get("updated_at") if meta else None
    data = {
        "sid": id,
        "transcriptions": segments,
        "stream_start_time": meta.get("stream_start_time") if meta else None,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else None,
    }

    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json")

@app.get("/yt/{id}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def yt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    # Note: This might overwrite stream_start_time in the display data, but not cache
    data["stream_start_time"] = await get_youtube_start_time(id) 
    return templates.TemplateResponse("yt.html", {"request": request, "id": id, "data": data})

@app.get("/rt/{id}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def rt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    if not data:
        data = {
            "stream_start_time": None,
            "transcriptions": []
        }
    sliced_data = data.copy()
    sliced_data["transcriptions"] = sliced_data["transcriptions"][-50:]
    print(sliced_data, flush=True)
    return templates.TemplateResponse("rt.html", {"request": request, "id": id, "data": sliced_data})
  
@app.get("/panel/{sid}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def panel(request: Request, sid: str):
    # Sanitize sid parameter to prevent NoSQL injection
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid") or str(uuid.uuid4())
    request.session["user_uid"] = user_uid

    now = datetime.now(timezone.utc)
    ADMIN_TIMEOUT = 30

    # Find or create the room
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        await rooms_collection.insert_one({
            "sid": sid,
            "secret_key": None,
            "admin_uid": None,
            "admin_email": None,
            "admin_last_heartbeat": None,
            "created_at": now,
            "extra": {}
        })
        room = {"sid": sid, "secret_key": None, "admin_uid": None, "admin_email": None, "admin_last_heartbeat": None}

    admin_uid = room.get("admin_uid")
    admin_key = room.get("secret_key")

    await _require_room_owner(request, room)

    if admin_uid and admin_key:
        last_heartbeat = room.get("admin_last_heartbeat")
        if last_heartbeat and last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        admin_expired = not last_heartbeat or (now - last_heartbeat).total_seconds() > ADMIN_TIMEOUT

        if admin_expired:
            # Lock expired — clear the lock but preserve ownership.
            await rooms_collection.update_one(
                {"sid": sid},
                {"$set": {"secret_key": None, "admin_last_heartbeat": None, "updated_at": now}}
            )
            admin_key = None
        else:
            # Active admin: verify caller owns the session
            if request.session.get("secret_key") != admin_key:
                raise HTTPException(status_code=403, detail="Session admin is already connected, only one admin is allowed.")
            await rooms_collection.update_one(
                {"sid": sid},
                {"$set": {"admin_last_heartbeat": now, "updated_at": now}}
            )

    if not admin_uid or not admin_key:
        session_secret_key = str(uuid.uuid4())
        current_email = _get_session_email(request)
        await rooms_collection.update_one(
            {"sid": sid},
            {"$set": {"secret_key": session_secret_key, "admin_uid": user_uid, "admin_email": current_email, "admin_last_heartbeat": now, "updated_at": now}}
        )
        request.session["secret_key"] = session_secret_key
        user_secret_key = session_secret_key
    else:
        user_secret_key = request.session.get("secret_key")

    is_realtime_enabled = await is_realtime_authorized(request.session)
    response = templates.TemplateResponse("panel.html", {"request": request, "sid": sid, "user_secret_key": user_secret_key, "is_realtime_enabled": is_realtime_enabled, "email": _get_session_email(request)})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@app.post("/heartbeat/{sid}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def heartbeat(request: Request, sid: str):
    """Update admin heartbeat to maintain session lock"""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)
    now = datetime.now(timezone.utc)
    update = {"admin_last_heartbeat": now, "updated_at": now}
    response: dict = {"status": "ok"}
    scribe_manager = active_scribe_managers.get(sid)
    if scribe_manager and scribe_manager.audio_bytes_total > 0:
        stats = scribe_manager.get_usage_stats()
        audio_fields = {
            "audio_bytes": stats["audio_bytes"],
            "audio_duration_secs": stats["audio_duration_secs"],
            "audio_chunks": stats["audio_chunks"],
        }
        update.update(audio_fields)
        response.update(audio_fields)
    if scribe_manager and scribe_manager.is_running:
        # Refresh the TTL so an active session is never evicted mid-recording.
        active_scribe_managers[sid] = scribe_manager
    translation_manager = active_translation_managers.get(sid)
    if translation_manager:
        # Refresh translation manager TTL alongside scribe manager to prevent
        # the 60s default expiry from evicting it mid-commit and silently
        # dropping committed translations.
        active_translation_managers[sid] = translation_manager
    await rooms_collection.update_one({"sid": sid}, {"$set": update})
    return response

@app.post("/release-admin/{sid}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def release_admin(request: Request, sid: str):
    """Release admin lock when admin leaves"""
    sid = sanitize_query_param(sid, "session ID")

    user_secret_key = request.session.get("secret_key")
    if not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")

    if room.get("secret_key") != user_secret_key:
        return {"status": "not_admin"}

    await rooms_collection.update_one(
        {"sid": sid},
        {"$set": {"secret_key": None, "admin_last_heartbeat": None, "updated_at": datetime.now(timezone.utc)}}
    )
    request.session.pop("secret_key", None)
    return {"status": "released"}

@app.delete("/api/sessions/{sid}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_identifier))])
async def delete_session(request: Request, sid: str):
    """Release session ownership, removing it from the owner's My Sessions list.
    Once deleted, anyone can claim the session again."""
    sid = sanitize_query_param(sid, "session ID")
    email, user_uid = _require_logged_in(request)
    if not email or not user_uid:
        raise HTTPException(status_code=401, detail="Not logged in")
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    room_admin_email = await _get_room_owner_email(room)
    if not room_admin_email or room_admin_email.lower() != email.lower():
        raise HTTPException(status_code=403, detail="You do not own this session")
    await rooms_collection.update_one(
        {"sid": sid},
        {"$set": {"admin_uid": None, "admin_email": None, "secret_key": None, "admin_last_heartbeat": None, "updated_at": datetime.now(timezone.utc)}}
    )
    return {"status": "deleted"}

# Socket.IO Event Handlers
@sio.event
async def connect(socket_id, environ, auth):
    """Handle client connection"""
    await sio.save_session(socket_id, {'verified': False})
    logger.info(f"Client connected: {socket_id}")
    await sio.emit('connected', {'status': 'connected', 'client_id': socket_id}, to=socket_id)

@sio.event
async def disconnect(socket_id):
    """Handle client disconnection"""
    _socket_limiter.cleanup(socket_id)
    logger.info(f"Client disconnected: {socket_id}")
    # Admin lock is NOT cleared here: socket disconnect fires on page refresh
    # and transient network blips, not only on true tab-close.
    # Cleanup is handled by:
    #   1. The /release-admin HTTP beacon sent on true navigation-away (beforeunload, non-reload)
    #   2. The 30-second heartbeat timeout checked on every /panel/{sid} request

async def _process_transcription_update(session_id, sync_data):
    """Internal helper to process transcription updates (cache, DB, and broadcast)"""
    # Fetch cached transcription data (Redis or DB)
    cached_data = await get_cached_transcription(session_id)

    # Add stream start time (fetched once at manager start; fall back if no manager)
    manager = active_scribe_managers.get(session_id)
    yt_start_time = manager.yt_start_time if manager else await get_youtube_start_time(session_id)
    if yt_start_time:
         cached_data["stream_start_time"] = yt_start_time
    
    # Get last committed segment if available
    last_committed = None
    last_committed_json = await redis_client.zrange(f"transcription:{session_id}:list", -1, -1)
    if last_committed_json:
        last_committed = json.loads(last_committed_json[0])
    
    if sync_data.get("partial", False) == True:
        # Skip if partial data is older than the last committed one
        if last_committed and sync_data["start_time"] < last_committed["start_time"]:
            print(f"skip older partial: {sync_data['start_time']} < {last_committed['start_time']}", flush=True)
            return

        # Update Redis Partial Only - Atomically
        if sync_data.get("flow_only"):
            last_partial = await redis_client.get(f"transcription:{session_id}:partial")
            if last_partial:
                last_partial = json.loads(last_partial)
                sync_data["result"]["translated"] = last_partial["result"]["translated"]
                
        await redis_client.setex(f"transcription:{session_id}:partial", 3600, json.dumps(sync_data))
    else:
        # Atomic ZSET update
        pipe = redis_client.pipeline()
        # Add new segment to ZSET with start_time as score
        zset_key = f"transcription:{session_id}:list"
        pipe.zadd(zset_key, {json.dumps(sync_data): sync_data["start_time"]})
        # Cap ZSET size by removing oldest entries beyond limit
        pipe.zremrangebyrank(zset_key, 0, -1001)

        # Update Meta (expiry and stream_start_time)
        meta = {"stream_start_time": yt_start_time or cached_data.get("stream_start_time")}
        pipe.setex(f"transcription:{session_id}:meta", 3600, json.dumps(meta))
        pipe.expire(zset_key, 3600)

        # Clear partial
        pipe.delete(f"transcription:{session_id}:partial")
        await pipe.execute()

        # Get the actual last committed after ZADD (to handle potential out-of-order)
        new_last_json = await redis_client.zrange(zset_key, -1, -1)
        if new_last_json:
            last_committed = json.loads(new_last_json[0])
        
        # Save to MongoDB in background
        asyncio.create_task(save_segment_background(session_id, sync_data, meta["stream_start_time"]))
    
    # Build the broadcast payload
    payload = sync_data.copy()
    if last_committed:
        payload["last_committed"] = last_committed

    is_partial = sync_data.get("partial", False) is True

    async def _emit_now(p):
        log_msg = f"sync {sync_data['start_time']} {sync_data['partial']}"
        if "result" in sync_data and "corrected" in sync_data["result"]:
            log_msg += f" {sync_data['result']['corrected']}"
        print(log_msg, flush=True)
        await sio.emit('transcription_update', p, room=session_id)

    if is_partial:
        # Cancel any pending broadcast for this session and schedule a fresh one
        # after 75 ms so only the latest partial is sent when updates burst.
        existing = _partial_debounce_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()

        async def _debounced(p):
            await asyncio.sleep(0.075)
            _partial_debounce_tasks.pop(session_id, None)
            await _emit_now(p)

        _partial_debounce_tasks[session_id] = asyncio.create_task(_debounced(payload))
    else:
        # Committed segments broadcast immediately; cancel any pending partial debounce.
        existing = _partial_debounce_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()
        await _emit_now(payload)
    
@sio.event
async def sync(socket_id, data):
    """Handle WebSocket sync events"""
    if not _socket_limiter.check(socket_id, 'sync', 20, 1.0):
        return
    session = await sio.get_session(socket_id)

    session_id = data.get('id')
    if not session_id:
        await sio.emit('error', {'message': 'Session ID is required'}, to=socket_id)
        return

    # Validate session_id to prevent NoSQL injection
    is_valid, error_msg = validate_query_param(session_id, "session ID")
    if not is_valid:
        await sio.emit('error', {'message': error_msg}, to=socket_id)
        return

    secret_key = session.get('secret_key') or data.get('secret_key')
    if not await _ensure_socket_verified(socket_id, session, secret_key, session_id):
        return

    sync_data = data.copy()
    sync_data.pop("id", None)
    await _process_transcription_update(session_id, sync_data)

@sio.event
async def join_session(socket_id, data):
    """Handle client joining a session room"""
    if not _socket_limiter.check(socket_id, 'join_session', 5, 10.0):
        await sio.emit('error', {'message': 'Rate limit exceeded'}, to=socket_id)
        return
    session_id = data.get('session_id')
    secret_key = data.get('secret_key')

    session = await sio.get_session(socket_id)

    if secret_key and session_id:
        # Use helper function for consistent authentication verification
        if await verify_socket_auth(socket_id, session_id, secret_key):
            # Fetch room data to look up admin email for realtime authorization
            room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
            if room:
                session['secret_key'] = secret_key
                session['verified'] = True
                session['session_id'] = session_id
                session['email'] = await _get_room_owner_email(room)
                await sio.save_session(socket_id, session)
                print(f"Client verified: {session_id}, email: {session.get('email')}")
        else:
            # Authentication failed - do not set verified flag
            print(f"Client authentication failed: {session_id}")

    if session_id:
        await sio.enter_room(socket_id, session_id)
        authorized = session.get('verified', False)
        await sio.emit('joined_session', {'session_id': session_id, 'authorized': authorized}, to=socket_id)

@sio.event
async def leave_session(socket_id, data):
    """Handle client leaving a session room"""
    session_id = data.get('session_id')
    if session_id:
        await sio.leave_room(socket_id, session_id)
        await sio.emit('left_session', {'session_id': session_id}, to=socket_id)
        print(f"Client left session: {session_id}")

async def on_translation_completed(session_id, sync_data):
    await _process_transcription_update(session_id, sync_data)

async def on_scribe_transcription(session_id, transcription):
    """Callback for Scribe transcription"""
    # Prepare context
    cached_data = await get_cached_transcription(session_id)
    sync_data = transcription.copy()
    
    manager = _get_or_create_translation_manager(session_id)
    await manager.put(session_id, sync_data, cached_data, redis_client)

@sio.event
async def realtime_connect(socket_id, data):
    """Handle client realtime_connect events"""
    session = await sio.get_session(socket_id)

    if not session.get('verified'):
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return

    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)

    if await is_realtime_authorized(session, session_id):
        _get_or_create_translation_manager(session_id)


@sio.event
async def mic_on(socket_id, data):
    """Start the scribe session when the panel mic is turned on."""
    session = await sio.get_session(socket_id)
    if not session.get('verified'):
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return
    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)
    if session_id and await is_realtime_authorized(session, session_id):
        await _get_or_create_scribe_manager(session_id)


@sio.event
async def mic_off(socket_id, data):
    """Stop the scribe session immediately when the panel mic is turned off."""
    session = await sio.get_session(socket_id)
    if not session.get('verified'):
        return
    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)
    if not session_id:
        return
    manager: ScribeSessionManager | None = active_scribe_managers.pop(session_id, None)
    if manager:
        logger.info(f"mic_off: stopping scribe for {session_id}")
        await manager.stop()


@sio.event
async def audio_buffer_append(socket_id, data):
    """Handle client audio buffer append events"""
    if not _socket_limiter.check(socket_id, 'audio_buffer_append', 30, 1.0):
        return
    session = await sio.get_session(socket_id)

    session_id = session.get('session_id')

    if not session_id:
        print("No session ID found for socket ID:", socket_id, flush=True)
        return

    # On every chunk after the first, skip all auth awaits: realtime_authorized
    # being True already implies verified is True (set together on first chunk).
    if not session.get('realtime_authorized'):
        secret_key = session.get('secret_key') or data.get('secret_key')
        if not await _ensure_socket_verified(socket_id, session, secret_key, session_id):
            return
        if not await is_realtime_authorized(session, session_id):
            await sio.emit('error', {'message': 'Unauthorized: realtime token required'}, to=socket_id)
            return
        session['realtime_authorized'] = True
        await sio.save_session(socket_id, session)

    base64_audio = data.get("audio")
    if not base64_audio:
        print("No audio data found in request", flush=True)
        return

    # Validate audio data type
    if not isinstance(base64_audio, str):
        await sio.emit('error', {'message': 'Invalid audio data format: must be string'}, to=socket_id)
        return

    # Validate size limit (max 1MB per chunk)
    # Base64 encoding increases size by ~33%, so 1MB limit = ~750KB of actual data
    if len(base64_audio) > _MAX_AUDIO_CHUNK_SIZE:
        await sio.emit('error', {'message': f'Audio chunk too large: maximum {_MAX_AUDIO_CHUNK_SIZE} bytes allowed'}, to=socket_id)
        return

    # Lightweight base64 format check: validate length is a multiple of 4
    # and that the first 16 characters match the base64 character set.
    # Full decoding a 1MB chunk just for validation is CPU-intensive at scale.
    if len(base64_audio) % 4 != 0 or not _BASE64_RE.match(base64_audio[:16]):
        await sio.emit('error', {'message': 'Invalid base64 audio data'}, to=socket_id)
        logger.warning(f"Invalid base64 audio data from socket {socket_id}")
        return

    manager = active_scribe_managers.get(session_id)
    if not manager or not manager.is_running:
        if not session.get('realtime_authorized') and not await is_realtime_authorized(session, session_id):
            return
        manager = await _get_or_create_scribe_manager(session_id)
    else:
        # Refresh TTL so a session with continuous audio is never evicted mid-stream.
        # cachetools' .get() does not reset TTL — only __setitem__ does.
        active_scribe_managers[session_id] = manager
        translation_manager = active_translation_managers.get(session_id)
        if translation_manager:
            active_translation_managers[session_id] = translation_manager
    # On first audio chunk of a new manager instance, restore previously saved usage from DB
    # so counts survive page refreshes. Flag is set before the await to prevent double-restore.
    if not manager._usage_restored:
        manager._usage_restored = True
        room_usage = await rooms_collection.find_one(
            {"sid": session_id}, {"_id": 0, "audio_bytes": 1, "audio_chunks": 1}
        )
        if room_usage and room_usage.get("audio_bytes"):
            manager.restore_usage(room_usage["audio_bytes"], room_usage.get("audio_chunks", 0))
    await manager.push_audio(base64_audio)
    