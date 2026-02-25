# This file is part of g0v/realtime_transcribe.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

from fastapi import FastAPI, Request, Form, HTTPException, Query
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
from .database import rooms_collection, transcription_store_collection
from .config import SETTINGS, REDIS_URL
from .scribe_manager import ScribeSessionManager
import os

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
app.add_middleware(SessionMiddleware, secret_key=str(uuid.uuid4()))

# Setup templates
templates = Jinja2Templates(directory="app/templates")

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
            print(f"Error fetching YouTube data: {e}")
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
        print(f"Redis/DB error in get_cached_transcription: {e}")
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
        print(f"Migration error for {id}: {e}")


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
        print(f"Error saving to MongoDB: {e}")


# FastAPI Routes
@app.get("/", response_class=HTMLResponse)
async def hello_world(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/download/{id}")
async def download(id: str):
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
    data = await get_cached_transcription(id)
    # Note: This might overwrite stream_start_time in the display data, but not cache
    data["stream_start_time"] = await get_youtube_start_time(id) 
    return templates.TemplateResponse("yt.html", {"request": request, "id": id, "data": data})

@app.get("/rt/{id}", response_class=HTMLResponse)
async def rt(request: Request, id: str):
    data = await get_cached_transcription(id)
    sliced_data = data.copy()
    sliced_data["transcriptions"] = sliced_data["transcriptions"][-50:]
    return templates.TemplateResponse("rt.html", {"request": request, "id": id, "data": sliced_data})
  
@app.post("/create-session", response_class=HTMLResponse)
async def create_session(request: Request, sid: str = Form(...)):
    session_secret_key = str(uuid.uuid4())
    
    # Check if room already exists using Motor
    existing_room = await rooms_collection.find_one({"sid": sid})
    if existing_room:
        return HTMLResponse(content="""
        <html lang="en">
        <head>
            <meta charset="UTF-8" />
        </head>
        <body>
            <h1>Session already exists</h1>
            <script>
                alert("Session id already exists, go to panel or use another one.");
                window.location.href = "/";
            </script>
        </body>
        </html>
        """)
    
    # Create new room
    await rooms_collection.insert_one({
        "sid": sid,
        "secret_key": session_secret_key,
        "created_at": datetime.now(timezone.utc),
        "extra": {}
    })
    
    request.session["secret_key"] = session_secret_key
    return RedirectResponse(url=f"/panel/{sid}?secret_key={session_secret_key}", status_code=303)

@app.get("/panel/{sid}", response_class=HTMLResponse)
async def panel(request: Request, sid: str, secret_key: str | None = Query(default=None)):
    if secret_key:
        request.session["secret_key"] = secret_key
    user_secret_key = request.session.get("secret_key")
    if not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Verify room exists with matching secret key using Motor
    room = await rooms_collection.find_one({"sid": sid, "secret_key": user_secret_key})
    if not room:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    return templates.TemplateResponse("panel.html", {"request": request, "sid": sid, "user_secret_key": user_secret_key})

@app.get("/live/{sid}", response_class=HTMLResponse)
async def live(request: Request, sid: str):
    return templates.TemplateResponse("live.html", {"request": request, "sid": sid})


@app.get("/realtime/{sid}", response_class=HTMLResponse)
async def realtime(request: Request, sid: str):
    return templates.TemplateResponse("realtime.html", {"request": request, "sid": sid})


# Socket.IO Event Handlers
@sio.event
async def connect(socket_id, environ, auth):
    """Handle client connection"""
    # Create a session-like object for socket.io
    if auth and auth.get('secret_key') == SETTINGS["SECRET_KEY"]:
        await sio.save_session(socket_id, {'verified': True})
        print(f"Admin client connected: {socket_id}")
    else:
        await sio.save_session(socket_id, {'verified': False})
        print(f"Client connected: {socket_id}")
    await sio.emit('connected', {'status': 'connected', 'client_id': socket_id}, to=socket_id)

@sio.event
async def disconnect(socket_id):
    """Handle client disconnection"""
    print(f"Client disconnected: {socket_id}")

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
    
    if sync_data.get("partial", False):
        # Skip if partial data is older than the last committed one
        if last_committed and sync_data["start_time"] < last_committed["start_time"]:
            print(f"skip older partial: {sync_data['start_time']} < {last_committed['start_time']}", flush=True)
            return

        # Update Redis Partial Only - Atomically
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
    
    if not session.get('verified'):
        if not session.get('secret_key') or session.get('secret_key') != data.get('secret_key'):
            await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
            return
    
    session_id = data.get('id')
    if not session_id:
        await sio.emit('error', {'message': 'Session ID is required'}, to=socket_id)
        return
    
    # Remove id from the data before processing
    sync_data = data.copy()
    sync_data.pop("id", None)
    
    await _process_transcription_update(session_id, sync_data)

@sio.event
async def join_session(socket_id, data):
    """Handle client joining a session room"""
    session_id = data.get('session_id')
    secret_key = data.get('secret_key')
    
    session = await sio.get_session(socket_id)
    
    if secret_key:
        # Verify room exists with matching secret key using Motor
        room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
        if room:
            session['secret_key'] = secret_key
            session['verified'] = True
            await sio.save_session(socket_id, session)
            print(f"Client verified: {session_id}")
    
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
    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)
    
    if not session_id:
        print("No session ID found for socket ID:", socket_id, flush=True)
        return

    base64_audio = data.get("audio")
    if not base64_audio:
        print("No audio data found in request", flush=True)
        return
    
    manager = active_scribe_managers.get(session_id)
    if not manager or not manager.is_running:
        manager = ScribeSessionManager(session_id, on_scribe_transcription)
        active_scribe_managers[session_id] = manager
        asyncio.create_task(manager.start())
        
    await manager.push_audio(base64_audio)
    