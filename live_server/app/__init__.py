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
import requests
import dotenv

dotenv.load_dotenv(override=True)

# Import MongoDB models
from .database import Room, TranscriptionStore
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

def get_youtube_start_time(video_id: str) -> float | None:
    """
    Get the actual stream start time for a YouTube video using YouTube Data API v3.
    Returns the actualStartTime if available, otherwise None.
    """
    data = None
    if video_id in youtube_data_cache and youtube_data_cache[video_id] is not None:
        data = youtube_data_cache[video_id]
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
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if 'items' in data and len(data['items']) > 0:
                data = data['items'][0]
                youtube_data_cache[video_id] = data
        except requests.exceptions.RequestException as e:
            print(f"Error fetching YouTube data: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error: {e}")
            return None
    print(data)
    if 'liveStreamingDetails' in data:
        live_details = data['liveStreamingDetails']
        # Check for actualStartTime (when stream actually started)
        if 'actualStartTime' in live_details:
            return datetime.fromisoformat(live_details['actualStartTime']).timestamp()
        # Fallback to scheduledStartTime if actualStartTime is not available
        elif 'scheduledStartTime' in live_details:
            return datetime.fromisoformat(live_details['scheduledStartTime']).timestamp()
    return None

async def get_cached_transcription(id) -> Any:
    # Try fetching committed transcriptions and partial separately from Redis
    try:
        committed_json = await redis_client.get(f"transcription:{id}")
        partial_json = await redis_client.get(f"transcription:{id}:partial")
        
        data = None
        if committed_json:
            data = json.loads(committed_json)
        
        # Fallback to DB if no committed data in Redis
        if data is None:
            def fetch_db():
                return TranscriptionStore.objects(sid=id).first()
            store = await asyncio.to_thread(fetch_db)
            if store:
                data = {
                    "transcriptions": store.transcriptions,
                    "stream_start_time": store.stream_start_time
                }
                # Backfill Redis
                await redis_client.setex(f"transcription:{id}", 3600, json.dumps(data))
            else:
                data = {"transcriptions": []}
        
        # Merge partial data if exists
        if partial_json:
            data["partial"] = json.loads(partial_json)
            
        return data
    except Exception as e:
        print(f"Redis/DB error in get_cached_transcription: {e}")
        return {"transcriptions": []}


def _push_segment_mongo_sync(sid, segment, stream_start_time):
    """Synchronously push a single segment to MongoDB"""
    try:
        TranscriptionStore.objects(sid=sid).update_one(
            push__transcriptions=segment,
            set__stream_start_time=stream_start_time,
            set__updated_at=datetime.now(timezone.utc),
            upsert=True
        )
    except Exception as e:
        print(f"Error saving to MongoDB: {e}")

async def save_segment_background(sid, segment, stream_start_time):
    """Push segment to MongoDB in background"""
    await asyncio.to_thread(_push_segment_mongo_sync, sid, segment, stream_start_time)


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
    data["stream_start_time"] = get_youtube_start_time(id) 
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
    
    # Check if room already exists using MongoEngine
    existing_room = Room.objects(sid=sid).first()
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
    room = Room(sid=sid, secret_key=session_secret_key)
    room.save()
    
    request.session["secret_key"] = session_secret_key
    return RedirectResponse(url=f"/panel/{sid}?secret_key={session_secret_key}", status_code=303)

@app.get("/panel/{sid}", response_class=HTMLResponse)
async def panel(request: Request, sid: str, secret_key: str | None = Query(default=None)):
    if secret_key:
        request.session["secret_key"] = secret_key
    user_secret_key = request.session.get("secret_key")
    if not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Verify room exists with matching secret key using MongoEngine
    room = Room.objects(sid=sid, secret_key=user_secret_key).first()
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
    yt_start_time = get_youtube_start_time(session_id)
    if yt_start_time:
         cached_data["stream_start_time"] = yt_start_time
    
    if sync_data.get("partial", False):
        # Update Redis Partial Only - Atomically
        await redis_client.setex(f"transcription:{session_id}:partial", 3600, json.dumps(sync_data))
    else:
        # Fetch fresh committed data, append new segment, and update Redis
        # Note: In high-concurrency, we'd use a Lua script or Redis List for atomicity,
        # but here we'll update the main transcription list.
        # (Re-fetching here to match on_translation_completed's pattern which is slightly safer)
        cached_data = await get_cached_transcription(session_id)
        cached_data["transcriptions"].append(sync_data)
        cached_data["transcriptions"].sort(key=lambda x: x["start_time"])
        
        # Separate partial from committed when saving
        committed_only = {
            "transcriptions": cached_data["transcriptions"],
            "stream_start_time": cached_data.get("stream_start_time")
        }
        
        await redis_client.setex(f"transcription:{session_id}", 3600, json.dumps(committed_only))
        # Clear partial when a segment is committed
        await redis_client.delete(f"transcription:{session_id}:partial")
        
        # Save to MongoDB in background
        asyncio.create_task(save_segment_background(session_id, sync_data, cached_data.get("stream_start_time")))
    
    # Log the update
    log_msg = f"sync {sync_data['start_time']}"
    if "result" in sync_data and "corrected" in sync_data["result"]:
        log_msg += f" {sync_data['result']['corrected']}"
    print(log_msg, flush=True)
    
    # Emit update to all clients in the session room
    await sio.emit('transcription_update', sync_data, room=session_id)
    
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
        # Verify room exists with matching secret key using MongoEngine
        room = Room.objects(socket_id=session_id, secret_key=secret_key).first()
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
    