# This file is part of g0v/realtime_transcribe.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import asyncio
import json
import re
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
from .database import rooms_collection, transcription_store_collection, users_collection, init_indexes
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

# Session manager caches: max 512 concurrent sessions, 30-minute inactivity TTL.
# When a session is evicted by cachetools (TTL expiry or capacity), its manager is
# stopped so background tasks are cleaned up promptly.
_MANAGER_CACHE_TTL = 1800   # seconds (30 min)
_MANAGER_CACHE_MAX = 512


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
# Audio usage save tracking: session_id -> last saved bytes


def _get_or_create_scribe_manager(session_id) -> ScribeSessionManager:
    """Return existing running ScribeSessionManager or create and start a new one."""
    manager = active_scribe_managers.get(session_id)
    if not manager or not manager.is_running:
        manager = ScribeSessionManager(session_id, on_scribe_transcription)
        active_scribe_managers[session_id] = manager
        asyncio.create_task(manager.start())
    return manager


def _get_or_create_translation_manager(session_id):
    """Return existing TranslationQueueManager or create and start a new one."""
    manager = active_translation_managers.get(session_id)
    if not manager:
        from .translator import TranslationQueueManager
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
    # Close shared translator client
    from .translator import close_async_client
    await close_async_client()

    # Stop all active scribe managers (snapshot first to avoid mutation during iteration)
    for manager in list(active_scribe_managers.values()):
        await manager.stop()

    # Stop all active translation managers
    for manager in list(active_translation_managers.values()):
        await manager.stop()

# Initialize FastAPI app with lifespan
app = FastAPI(lifespan=lifespan)

# Add session middleware
app.add_middleware(SessionMiddleware, secret_key=SETTINGS.get("SECRET_KEY", str(uuid.uuid4())))

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

youtube_data_cache: TTLCache = TTLCache(maxsize=256, ttl=_MANAGER_CACHE_TTL)

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

async def is_realtime_authorized(session: dict) -> bool:
    """Check if the socket is authorized to use server-side realtime features.

    Returns True if:
    1. The socket is a global admin connection, OR
    2. The user logged in via email and has realtime_enabled=True
    """
    if session.get('admin'):
        return True

    email = session.get('email')
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

async def _get_session_uid(request: Request) -> str:
    uid = request.session.get("user_uid", "")
    if not uid or len(uid) < 36:
        uid = str(uuid.uuid4())
        request.session["user_uid"] = uid
    return uid

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


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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


@app.get("/user-dashboard", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
async def user_dashboard(request: Request):
    email, user_uid = _require_logged_in(request)
    if not email or not user_uid:
        return RedirectResponse(url="/login", status_code=302)
    rooms = await rooms_collection.find(
        {"admin_uid": user_uid},
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
    return templates.TemplateResponse("user_dashboard.html", {
        "request": request,
        "rooms": rooms,
        "current_email": email,
        "is_realtime_enabled": is_realtime_enabled,
    })


@app.post("/api/users/{email}/realtime", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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


@app.post("/auth/send-otp", dependencies=[Depends(RateLimiter(times=10, seconds=60, identifier=_get_session_uid))])
async def send_otp(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    otp = generate_otp()
    await store_otp(redis_client, email, otp)
    try:
        await send_otp_email(email, otp, EMAIL_SETTINGS)
    except Exception as e:
        log_exception(logger, e, f"Failed to send OTP email to {email}")
        raise HTTPException(status_code=500, detail="Failed to send email")
    return {"status": "sent"}


@app.post("/auth/verify-otp", dependencies=[Depends(RateLimiter(times=10, seconds=60, identifier=_get_session_uid))])
async def verify_otp_endpoint(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    otp = body.get("otp", "").strip()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    if not otp or not re.match(r'^\d{4}$', otp):
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


@app.get("/api/session/{sid}/languages", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
async def get_session_languages_endpoint(request: Request, sid: str):
    """Get the current translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    from .translator import get_session_languages
    languages = await get_session_languages(redis_client, sid)
    return {"languages": languages}


@app.post("/api/session/{sid}/languages", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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

    from .translator import save_session_languages
    await save_session_languages(redis_client, sid, languages)
    return {"languages": languages}


@app.get("/api/session/{sid}/keywords", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
async def get_session_keywords_endpoint(request: Request, sid: str):
    """Get the current keywords and locked keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)

    from .translator import get_current_keywords, get_locked_keywords
    keywords = await get_current_keywords(redis_client, sid)
    locked_keywords = await get_locked_keywords(redis_client, sid)
    return {"keywords": keywords, "locked_keywords": locked_keywords}


@app.post("/api/session/{sid}/keywords", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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

    from .translator import save_current_keywords, save_locked_keywords
    await save_current_keywords(redis_client, sid, keywords)

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

    result = {"keywords": keywords}
    if locked_keywords is not None:
        result["locked_keywords"] = locked_keywords
    return result


@app.get("/api/session/{sid}/audio-usage", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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
            from .translator import get_async_client
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
            store = await transcription_store_collection.find_one({"sid": id})
            if store:
                data = {
                    "transcriptions": store.get("transcriptions", []),
                    "stream_start_time": store.get("stream_start_time")
                }
                # Backfill Redis
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
        pipe = redis_client.pipeline()
        for seg in data["transcriptions"]:
            pipe.zadd(f"transcription:{id}:list", {json.dumps(seg): seg["start_time"]})
        
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


async def save_segment_background(sid, segment, stream_start_time):
    """Push segment to MongoDB in background"""
    try:
        await transcription_store_collection.update_one(
            {"sid": sid},
            {
                "$push": {"transcriptions": segment},
                "$set": {
                    "stream_start_time": stream_start_time,
                    "updated_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
    except Exception as e:
        log_exception(logger, e, "Error saving to MongoDB")


# FastAPI Routes
@app.get("/", response_class=HTMLResponse)
async def hello_world(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "email": _get_session_email(request)})

@app.get("/download/{id}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
async def download(id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")
    data = await transcription_store_collection.find_one({"sid": id}, {"_id": 0})
    if not data or not data.get("transcriptions"):
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Ensure all fields are JSON serializable
    if "updated_at" in data and isinstance(data["updated_at"], datetime):
        data["updated_at"] = data["updated_at"].isoformat()

    # Return as JSON
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json")

@app.get("/yt/{id}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
async def yt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    # Note: This might overwrite stream_start_time in the display data, but not cache
    data["stream_start_time"] = await get_youtube_start_time(id) 
    return templates.TemplateResponse("yt.html", {"request": request, "id": id, "data": data})

@app.get("/rt/{id}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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
  
@app.get("/panel/{sid}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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
            "admin_last_heartbeat": None,
            "created_at": now,
            "extra": {}
        })
        room = {"sid": sid, "secret_key": None, "admin_uid": None, "admin_last_heartbeat": None}

    admin_uid = room.get("admin_uid")
    admin_key = room.get("secret_key")

    # If the room is owned by a logged-in user, enforce exclusive ownership.
    # No other user can open the panel unless the owner deletes the session.
    if admin_uid:
        owner_doc = await users_collection.find_one({"user_uid": admin_uid})
        if owner_doc and owner_doc.get("email"):
            current_email = _get_session_email(request)
            if not current_email or current_email.lower() != owner_doc["email"].lower():
                raise HTTPException(status_code=403, detail="This session is owned by another user.")

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
        await rooms_collection.update_one(
            {"sid": sid},
            {"$set": {"secret_key": session_secret_key, "admin_uid": user_uid, "admin_last_heartbeat": now, "updated_at": now}}
        )
        request.session["secret_key"] = session_secret_key
        user_secret_key = session_secret_key
    else:
        user_secret_key = request.session.get("secret_key")

    is_realtime_enabled = await is_realtime_authorized(request.session)
    return templates.TemplateResponse("panel.html", {"request": request, "sid": sid, "user_secret_key": user_secret_key, "is_realtime_enabled": is_realtime_enabled, "email": _get_session_email(request)})

@app.post("/heartbeat/{sid}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
async def heartbeat(request: Request, sid: str):
    """Update admin heartbeat to maintain session lock"""
    sid = sanitize_query_param(sid, "session ID")
    await _verify_session_admin(request, sid)
    now = datetime.now(timezone.utc)
    update = {"admin_last_heartbeat": now, "updated_at": now}
    manager = active_scribe_managers.get(sid)
    if manager and manager.audio_bytes_total > 0:
        stats = manager.get_usage_stats()
        update["audio_bytes"] = stats["audio_bytes"]
        update["audio_duration_secs"] = stats["audio_duration_secs"]
        update["audio_chunks"] = stats["audio_chunks"]
    await rooms_collection.update_one({"sid": sid}, {"$set": update})
    return {"status": "ok"}

@app.post("/release-admin/{sid}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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

@app.delete("/api/sessions/{sid}", dependencies=[Depends(RateLimiter(times=10, seconds=10, identifier=_get_session_uid))])
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
    if room.get("admin_uid") != user_uid:
        raise HTTPException(status_code=403, detail="You do not own this session")
    await rooms_collection.update_one(
        {"sid": sid},
        {"$set": {"admin_uid": None, "secret_key": None, "admin_last_heartbeat": None, "updated_at": datetime.now(timezone.utc)}}
    )
    return {"status": "deleted"}

# Socket.IO Event Handlers
@sio.event
async def connect(socket_id, environ, auth):
    """Handle client connection"""
    # Create a session-like object for socket.io
    if auth and auth.get('secret_key') == SETTINGS["SECRET_KEY"]:
        await sio.save_session(socket_id, {'verified': True, 'admin': True})
        print(f"Admin client connected: {socket_id}")
    else:
        await sio.save_session(socket_id, {'verified': False, 'admin': False})
        print(f"Client connected: {socket_id}")
    await sio.emit('connected', {'status': 'connected', 'client_id': socket_id}, to=socket_id)

@sio.event
async def disconnect(socket_id):
    """Handle client disconnection"""
    print(f"Client disconnected: {socket_id}")
    # Admin lock is NOT cleared here: socket disconnect fires on page refresh
    # and transient network blips, not only on true tab-close.
    # Cleanup is handled by:
    #   1. The /release-admin HTTP beacon sent on true navigation-away (beforeunload, non-reload)
    #   2. The 30-second heartbeat timeout checked on every /panel/{sid} request

async def _process_transcription_update(session_id, sync_data):
    """Internal helper to process transcription updates (cache, DB, and broadcast)"""
    # Fetch cached transcription data (Redis or DB)
    cached_data = await get_cached_transcription(session_id)
    
    # Add stream start time
    yt_start_time = await get_youtube_start_time(session_id)
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
        pipe.zadd(f"transcription:{session_id}:list", {json.dumps(sync_data): sync_data["start_time"]})
        
        # Update Meta (expiry and stream_start_time)
        meta = {"stream_start_time": yt_start_time or cached_data.get("stream_start_time")}
        pipe.setex(f"transcription:{session_id}:meta", 3600, json.dumps(meta))
        pipe.expire(f"transcription:{session_id}:list", 3600)
        
        # Clear partial
        pipe.delete(f"transcription:{session_id}:partial")
        await pipe.execute()
        
        # Get the actual last committed after ZADD (to handle potential out-of-order)
        new_last_json = await redis_client.zrange(f"transcription:{session_id}:list", -1, -1)
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
        log_msg = f"sync {sync_data['start_time']}"
        if "result" in sync_data and "corrected" in sync_data["result"]:
            log_msg += f" {sync_data['result']['corrected']}"
        print(log_msg, flush=True)
        await sio.emit('transcription_update', p, room=session_id)

    if is_partial:
        # Cancel any pending broadcast for this session and schedule a fresh one
        # after 75 ms so only the latest partial is sent when updates burst.
        existing = _partial_debounce_tasks.get(session_id)
        if existing and not existing.done():
            existing.cancel()

        async def _debounced(p):
            await asyncio.sleep(0.075)
            _partial_debounce_tasks.pop(session_id, None)
            await _emit_now(p)

        _partial_debounce_tasks[session_id] = asyncio.create_task(_debounced(payload))
    else:
        # Committed segments broadcast immediately without debounce
        await _emit_now(payload)
    
@sio.event
async def sync(socket_id, data):
    """Handle WebSocket sync events"""
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
                # Look up admin email so is_realtime_authorized can check realtime_enabled
                admin_uid = room.get('admin_uid')
                if admin_uid:
                    user_doc = await users_collection.find_one({"user_uid": admin_uid})
                    session['email'] = user_doc.get('email') if user_doc else None
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

    if await is_realtime_authorized(session):
        _get_or_create_scribe_manager(session_id)
        _get_or_create_translation_manager(session_id)


@sio.event
async def audio_buffer_append(socket_id, data):
    """Handle client audio buffer append events"""
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
        if not await is_realtime_authorized(session):
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
    MAX_AUDIO_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB in bytes
    if len(base64_audio) > MAX_AUDIO_CHUNK_SIZE:
        await sio.emit('error', {'message': f'Audio chunk too large: maximum {MAX_AUDIO_CHUNK_SIZE} bytes allowed'}, to=socket_id)
        return

    # Lightweight base64 format check: validate length is a multiple of 4
    # and that the first 16 characters match the base64 character set.
    # Full decoding a 1MB chunk just for validation is CPU-intensive at scale.
    if len(base64_audio) % 4 != 0 or not re.match(r'^[A-Za-z0-9+/]*={0,2}$', base64_audio[:16]):
        await sio.emit('error', {'message': 'Invalid base64 audio data'}, to=socket_id)
        logger.warning(f"Invalid base64 audio data from socket {socket_id}")
        return

    manager = _get_or_create_scribe_manager(session_id)
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
    