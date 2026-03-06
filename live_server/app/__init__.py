# This file is part of g0v/realtime_transcribe.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import socketio
from contextlib import asynccontextmanager
from typing import Any
from pathlib import Path
from datetime import datetime, timezone
import uuid
import json
import asyncio
import time
import dotenv

dotenv.load_dotenv(override=True)

# Import MongoDB models
from .database import rooms_collection, transcription_store_collection, realtime_tokens_collection
from .config import SETTINGS, REDIS_URL
from .scribe_manager import ScribeSessionManager
from .logger_config import setup_logger, log_exception, get_generic_error_dict
import os
import hmac
import logging
import re
import base64

# Setup logger
logger = setup_logger(__name__)

active_scribe_managers = {}
active_translation_managers = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    print("Shutting down resources...")
    # Close shared translator client
    from .translator import close_async_client
    await close_async_client()
    
    # Stop all active scribe managers
    for manager in active_scribe_managers.values():
        await manager.stop()
    
    # Stop all active translation managers
    for manager in active_translation_managers.values():
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



# In-memory cache for transcriptions (Deprecated in favor of Redis)
# transcription_cache = {}
youtube_data_cache = {}

# Initialize Redis client
import redis.asyncio as redis
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

def sanitize_query_param(value: str, param_name: str = "parameter") -> str:
    """
    Sanitize user input to prevent NoSQL injection and enumeration attacks.

    For session IDs specifically, enforces:
    - Alphanumeric characters, hyphens, and underscores only
    - Length between 4 and 64 characters
    - No MongoDB special characters ($ and .)

    For other parameters, applies basic sanitization.

    Args:
        value: The input string to sanitize
        param_name: Name of the parameter for error messages

    Returns:
        The sanitized string if valid

    Raises:
        HTTPException: If input contains potentially dangerous characters or invalid format
    """
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name}: must be a string"
        )

    # Additional validation: ensure it's not empty after stripping
    if not value.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name}: cannot be empty"
        )

    # Strict validation for session IDs
    if "session" in param_name.lower() or param_name.lower() == "sid":
        # Enforce length limits (4-64 characters)
        if len(value) < 4 or len(value) > 64:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {param_name}: must be between 4 and 64 characters"
            )

        # Enforce alphanumeric format with hyphens and underscores only
        # This prevents special characters, MongoDB operators, and enumeration attempts
        if not re.match(r'^[a-zA-Z0-9_-]+$', value):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {param_name}: must contain only alphanumeric characters, hyphens, and underscores"
            )
    else:
        # For non-session parameters, check for MongoDB operator characters
        if '$' in value or '.' in value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {param_name}: contains prohibited characters"
            )

    return value

def validate_query_param(value: str, param_name: str = "parameter") -> tuple[bool, str]:
    """
    Validate user input to prevent NoSQL injection and enumeration attacks.
    Returns validation status and error message if invalid.

    For session IDs specifically, enforces:
    - Alphanumeric characters, hyphens, and underscores only
    - Length between 4 and 64 characters
    - No MongoDB special characters ($ and .)

    For other parameters, applies basic validation.

    Args:
        value: The input string to validate
        param_name: Name of the parameter for error messages

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(value, str):
        return False, f"Invalid {param_name}: must be a string"

    # Additional validation: ensure it's not empty after stripping
    if not value.strip():
        return False, f"Invalid {param_name}: cannot be empty"

    # Strict validation for session IDs
    if "session" in param_name.lower() or param_name.lower() == "sid":
        # Enforce length limits (4-64 characters)
        if len(value) < 4 or len(value) > 64:
            return False, f"Invalid {param_name}: must be between 4 and 64 characters"

        # Enforce alphanumeric format with hyphens and underscores only
        if not re.match(r'^[a-zA-Z0-9_-]+$', value):
            return False, f"Invalid {param_name}: must contain only alphanumeric characters, hyphens, and underscores"
    else:
        # For non-session parameters, check for MongoDB operator characters
        if '$' in value or '.' in value:
            return False, f"Invalid {param_name}: contains prohibited characters"

    return True, ""

async def is_realtime_authorized(session: dict, data: dict | None = None) -> bool:
    """Check if the socket is authorized to use server-side realtime features.

    Returns True if:
    1. The socket is an admin connection (global SECRET_KEY), OR
    2. No tokens exist in DB (no restrictions configured), OR
    3. A valid user_uid is found in data or session that matches a token
    """
    if session.get('admin'):
        return True
        
    token = None
    if data and isinstance(data, dict):
        token = data.get('user_uid')
    if not token:
        token = session.get('user_uid')
    if not token:
        return False

    # Validate token to prevent NoSQL injection
    is_valid, _ = validate_query_param(token, "user_uid")
    if not is_valid:
        return False

    # If no tokens in DB, allow everyone with a token (backward compat)
    if await realtime_tokens_collection.count_documents({}, limit=1) == 0:
        return True

    doc = await realtime_tokens_collection.find_one({"token": token})
    return doc is not None


async def verify_socket_auth(socket_id: str, session_id: str, secret_key: str) -> bool:
    """
    Verify WebSocket authentication against database.

    Ensures consistent authentication flow across all WebSocket event handlers.
    Only returns True if the secret_key matches the room's secret_key in the database.

    Args:
        socket_id: The socket ID for error reporting
        session_id: The session/room ID to verify
        secret_key: The secret key to verify against

    Returns:
        bool: True if authenticated, False otherwise
    """
    if not session_id or not secret_key:
        return False

    # Validate session_id to prevent NoSQL injection
    is_valid, _ = validate_query_param(session_id, "session_id")
    if not is_valid:
        return False

    # Validate secret_key to prevent NoSQL injection
    is_valid, _ = validate_query_param(secret_key, "secret_key")
    if not is_valid:
        return False

    # Verify against database
    room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
    return room is not None


def _verify_admin(request: Request):
    """Verify admin via Authorization: Bearer {SECRET_KEY} header."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    provided = auth_header[7:]
    if not hmac.compare_digest(provided, SETTINGS["SECRET_KEY"]):
        raise HTTPException(status_code=403, detail="Invalid SECRET_KEY")


@app.get("/api/tokens")
async def list_tokens(request: Request):
    """List all realtime tokens (admin only)."""
    _verify_admin(request)
    docs = await realtime_tokens_collection.find({}, {"_id": 0}).to_list(length=1000)
    return docs


@app.post("/api/tokens")
async def create_token(request: Request):
    """Create a new realtime token (admin only)."""
    _verify_admin(request)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    token = str(uuid.uuid4())
    doc = {
        "token": token,
        "label": body.get("label", ""),
        "created_at": datetime.now(timezone.utc),
    }
    await realtime_tokens_collection.insert_one(doc)
    return {"token": token, "label": doc["label"], "created_at": doc["created_at"].isoformat()}


@app.delete("/api/tokens/{token}")
async def delete_token(request: Request, token: str):
    """Revoke a realtime token (admin only)."""
    _verify_admin(request)

    # Sanitize token parameter to prevent NoSQL injection
    token = sanitize_query_param(token, "token")

    result = await realtime_tokens_collection.delete_one({"token": token})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"deleted": token}


@app.get("/api/session/{sid}/languages")
async def get_session_languages_endpoint(request: Request, sid: str):
    """Get the current translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid")
    user_secret_key = request.session.get("secret_key")
    if not user_uid or not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if room.get("admin_uid") != user_uid or room.get("secret_key") != user_secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    from .translator import get_session_languages
    languages = await get_session_languages(redis_client, sid)
    return {"languages": languages}


@app.post("/api/session/{sid}/languages")
async def update_session_languages_endpoint(request: Request, sid: str):
    """Update the translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid")
    user_secret_key = request.session.get("secret_key")
    if not user_uid or not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if room.get("admin_uid") != user_uid or room.get("secret_key") != user_secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    languages = body.get("languages")
    if not isinstance(languages, list) or not languages:
        raise HTTPException(status_code=400, detail="languages must be a non-empty list")
    # Basic validation: each entry must be a non-empty string without injection chars
    for lang in languages:
        if not isinstance(lang, str) or not lang.strip():
            raise HTTPException(status_code=400, detail="Each language must be a non-empty string")
        if '$' in lang or len(lang) > 32:
            raise HTTPException(status_code=400, detail=f"Invalid language value: {lang}")
    languages = [lang.strip() for lang in languages]

    from .translator import save_session_languages
    await save_session_languages(redis_client, sid, languages)
    return {"languages": languages}


@app.get("/api/session/{sid}/keywords")
async def get_session_keywords_endpoint(request: Request, sid: str):
    """Get the current keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid")
    user_secret_key = request.session.get("secret_key")
    if not user_uid or not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if room.get("admin_uid") != user_uid or room.get("secret_key") != user_secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    from .translator import get_current_keywords
    keywords = await get_current_keywords(redis_client, sid)
    return {"keywords": keywords}


@app.post("/api/session/{sid}/keywords")
async def update_session_keywords_endpoint(request: Request, sid: str):
    """Update the keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid")
    user_secret_key = request.session.get("secret_key")
    if not user_uid or not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if room.get("admin_uid") != user_uid or room.get("secret_key") != user_secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")

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

    from .translator import save_current_keywords
    await save_current_keywords(redis_client, sid, keywords)
    return {"keywords": keywords}


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
        committed_json_list = await redis_client.zrange(f"transcription:{id}:list", 0, -1)
        meta_json = await redis_client.get(f"transcription:{id}:meta")
        partial_json = await redis_client.get(f"transcription:{id}:partial")
        
        data = None
        if committed_json_list:
            data = {
                "transcriptions": [json.loads(j) for j in committed_json_list],
                "stream_start_time": None
            }
            if meta_json:
                meta = json.loads(meta_json)
                data["stream_start_time"] = meta.get("stream_start_time")
        
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
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/download/{id}")
async def download(id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    if not data or not data.get("transcriptions"):
        # If cache (Redis) returned empty, we might want to ensure DB is checked.
        # But get_cached_transcription already does DB fallback.
        # Just valid safety check manually if needed, but likely redundant if get_cached works.
        pass
    
    # Return as JSON
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json")

@app.get("/yt/{id}", response_class=HTMLResponse)
async def yt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    # Note: This might overwrite stream_start_time in the display data, but not cache
    data["stream_start_time"] = await get_youtube_start_time(id) 
    return templates.TemplateResponse("yt.html", {"request": request, "id": id, "data": data})

@app.get("/rt/{id}", response_class=HTMLResponse)
async def rt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    sliced_data = data.copy()
    sliced_data["transcriptions"] = sliced_data["transcriptions"][-50:]
    return templates.TemplateResponse("rt.html", {"request": request, "id": id, "data": sliced_data})
  
@app.get("/panel/{sid}", response_class=HTMLResponse)
async def panel(request: Request, sid: str):
    # Sanitize sid parameter to prevent NoSQL injection
    sid = sanitize_query_param(sid, "session ID")

    # Ensure user has a UID
    user_uid = request.session.get("user_uid")
    if not user_uid:
        user_uid = str(uuid.uuid4())
        request.session["user_uid"] = user_uid

    # Find or create the room
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        # Create new room without secret_key (will be set by first user)
        await rooms_collection.insert_one({
            "sid": sid,
            "secret_key": None,
            "admin_uid": None,
            "admin_last_heartbeat": None,
            "created_at": datetime.now(timezone.utc),
            "extra": {}
        })
        room = await rooms_collection.find_one({"sid": sid})

    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    # Admin timeout: 30 seconds
    ADMIN_TIMEOUT = 30
    now = datetime.now(timezone.utc)

    # Check if there's already an admin
    if room.get("admin_uid") and room.get("secret_key"):
        # Check if admin heartbeat is stale
        last_heartbeat = room.get("admin_last_heartbeat")
        admin_expired = False
        if last_heartbeat:
            if last_heartbeat.tzinfo is None:
                last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
            elapsed = (now - last_heartbeat).total_seconds()
            admin_expired = elapsed > ADMIN_TIMEOUT
        else:
            # No heartbeat recorded, consider expired
            admin_expired = True

        if admin_expired:
            # Admin session expired, clear it
            await rooms_collection.update_one(
                {"sid": sid},
                {
                    "$set": {
                        "secret_key": None,
                        "admin_uid": None,
                        "admin_last_heartbeat": None,
                        "updated_at": now
                    }
                }
            )
            # Re-fetch room to get clean state
            room = await rooms_collection.find_one({"sid": sid})
        else:
            # Admin exists and is active - check if current user is the admin
            user_secret_key = request.session.get("secret_key")
            if user_secret_key != room["secret_key"] or user_uid != room["admin_uid"]:
                raise HTTPException(status_code=403, detail="Session admin is already connected")
            # User is the existing admin - update heartbeat
            await rooms_collection.update_one(
                {"sid": sid},
                {"$set": {"admin_last_heartbeat": now, "updated_at": now}}
            )
            
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    # If no admin or admin was cleared, make this user the admin
    if not room.get("admin_uid") or not room.get("secret_key"):
        session_secret_key = str(uuid.uuid4())
        await rooms_collection.update_one(
            {"sid": sid},
            {
                "$set": {
                    "secret_key": session_secret_key,
                    "admin_uid": user_uid,
                    "admin_last_heartbeat": now,
                    "updated_at": now
                }
            }
        )
        request.session["secret_key"] = session_secret_key
        user_secret_key = session_secret_key
    else:
        user_secret_key = request.session.get("secret_key")

    is_realtime_enabled = await is_realtime_authorized(request.session)

    return templates.TemplateResponse("panel.html", {"request": request, "sid": sid, "user_uid": user_uid, "user_secret_key": user_secret_key, "is_realtime_enabled": is_realtime_enabled})

@app.post("/heartbeat/{sid}")
async def heartbeat(request: Request, sid: str):
    """Update admin heartbeat to maintain session lock"""
    # Sanitize sid parameter to prevent NoSQL injection
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid")
    user_secret_key = request.session.get("secret_key")

    if not user_uid or not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Verify this user is the current admin
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")

    if room.get("admin_uid") == user_uid and room.get("secret_key") == user_secret_key:
        # Update heartbeat
        await rooms_collection.update_one(
            {"sid": sid},
            {
                "$set": {
                    "admin_last_heartbeat": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )
        return {"status": "ok"}

    raise HTTPException(status_code=403, detail="Not the current admin")

@app.post("/release-admin/{sid}")
async def release_admin(request: Request, sid: str):
    """Release admin lock when admin leaves"""
    # Sanitize sid parameter to prevent NoSQL injection
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid")
    user_secret_key = request.session.get("secret_key")

    if not user_uid or not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Verify this user is the current admin
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")

    if room.get("admin_uid") == user_uid and room.get("secret_key") == user_secret_key:
        # Clear admin lock
        await rooms_collection.update_one(
            {"sid": sid},
            {
                "$set": {
                    "secret_key": None,
                    "admin_uid": None,
                    "admin_last_heartbeat": None,
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )
        # Clear session
        request.session.pop("secret_key", None)
        return {"status": "released"}

    return {"status": "not_admin"}

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

    # Get session data to check if this was an admin
    session = await sio.get_session(socket_id)
    user_uid = session.get('user_uid')
    session_id = session.get('session_id')

    if user_uid and session_id:
        # Check if this user was the admin for this session
        room = await rooms_collection.find_one({"sid": session_id})
        if room and room.get("admin_uid") == user_uid:
            # Release admin lock immediately on disconnect
            await rooms_collection.update_one(
                {"sid": session_id},
                {
                    "$set": {
                        "secret_key": None,
                        "admin_uid": None,
                        "admin_last_heartbeat": None,
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            print(f"Admin lock released for session {session_id} on disconnect")

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
    
    # Log the update
    log_msg = f"sync {sync_data['start_time']}"
    if "result" in sync_data and "corrected" in sync_data["result"]:
        log_msg += f" {sync_data['result']['corrected']}"
    print(log_msg, flush=True)
    
    # Include last_committed in the payload
    payload = sync_data.copy()
    if last_committed:
        payload["last_committed"] = last_committed
    
    # Emit update to all clients in the session room
    await sio.emit('transcription_update', payload, room=session_id)
    
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

    if not session.get('verified'):
        secret_key = session.get('secret_key') or data.get('secret_key')
        if not secret_key:
            await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
            return

        # Use helper function for consistent authentication verification
        if not await verify_socket_auth(socket_id, session_id, secret_key):
            await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
            return

        # Only set verified=True after database verification
        session['verified'] = True
        session['secret_key'] = secret_key
        await sio.save_session(socket_id, session)

    # Remove id from the data before processing
    sync_data = data.copy()
    sync_data.pop("id", None)

    await _process_transcription_update(session_id, sync_data)

@sio.event
async def join_session(socket_id, data):
    """Handle client joining a session room"""
    session_id = data.get('session_id')
    secret_key = data.get('secret_key')
    user_uid = data.get('user_uid')

    session = await sio.get_session(socket_id)

    if secret_key and session_id:
        # Use helper function for consistent authentication verification
        if await verify_socket_auth(socket_id, session_id, secret_key):
            # Fetch room data to get admin_uid
            room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
            if room:
                session['secret_key'] = secret_key
                session['verified'] = True
                session['user_uid'] = user_uid or room.get('admin_uid')
                session['session_id'] = session_id
                await sio.save_session(socket_id, session)
                print(f"Client verified: {session_id}, user_uid: {session.get('user_uid')}")
        else:
            # Authentication failed - do not set verified flag
            print(f"Client authentication failed: {session_id}")

    if session_id:
        await sio.enter_room(socket_id, session_id)
        await sio.emit('joined_session', {'session_id': session_id}, to=socket_id)

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
    
    manager = active_translation_managers.get(session_id)
    if not manager:
        from .translator import TranslationQueueManager
        manager = TranslationQueueManager(on_translation_completed)
        active_translation_managers[session_id] = manager
        asyncio.create_task(manager.start())
        
    await manager.put(session_id, sync_data, cached_data, redis_client)

@sio.event
async def realtime_connect(socket_id, data):
    """Handle client realtime_connect events"""
    session = await sio.get_session(socket_id)

    if not session.get('verified'):
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return

    if not await is_realtime_authorized(session, data):
        await sio.emit('error', {'message': 'Unauthorized: realtime token required'}, to=socket_id)
        return

    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)

    manager = active_scribe_managers.get(session_id)
    if not manager or not manager.is_running:
        manager = ScribeSessionManager(session_id, on_scribe_transcription)
        active_scribe_managers[session_id] = manager
        asyncio.create_task(manager.start())
    
    manager = active_translation_managers.get(session_id)
    if not manager:
        from .translator import TranslationQueueManager
        manager = TranslationQueueManager(on_translation_completed)
        active_translation_managers[session_id] = manager
        asyncio.create_task(manager.start())

@sio.event
async def audio_buffer_append(socket_id, data):
    """Handle client audio buffer append events"""
    session = await sio.get_session(socket_id)

    if not session.get('verified'):
        secret_key = session.get('secret_key') or data.get('secret_key')
        if not secret_key:
            await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
            return

        # Get session_id from rooms to verify against database
        rooms = sio.rooms(socket_id)
        session_id = next((r for r in rooms if r != socket_id), None)

        if not session_id:
            await sio.emit('error', {'message': 'Unauthorized: not in a session room'}, to=socket_id)
            return

        # Use helper function for consistent authentication verification
        if not await verify_socket_auth(socket_id, session_id, secret_key):
            await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
            return

        # Only set verified=True after database verification
        session['verified'] = True
        session['secret_key'] = secret_key
        await sio.save_session(socket_id, session)

    if not await is_realtime_authorized(session, data):
        await sio.emit('error', {'message': 'Unauthorized: realtime token required'}, to=socket_id)
        return

    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)
    
    if not session_id:
        print("No session ID found for socket ID:", socket_id, flush=True)
        return

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

    # Validate base64 format
    try:
        # Attempt to decode to verify it's valid base64
        base64.b64decode(base64_audio, validate=True)
    except Exception as e:
        await sio.emit('error', {'message': 'Invalid base64 audio data'}, to=socket_id)
        logger.warning(f"Invalid base64 audio data from socket {socket_id}: {str(e)}")
        return

    manager = active_scribe_managers.get(session_id)
    if not manager or not manager.is_running:
        manager = ScribeSessionManager(session_id, on_scribe_transcription)
        active_scribe_managers[session_id] = manager
        asyncio.create_task(manager.start())
        
    await manager.push_audio(base64_audio)
    