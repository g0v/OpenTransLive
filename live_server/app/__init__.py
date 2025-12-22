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

# Initialize FastAPI app
app = FastAPI()

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
    logger=True
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
    # Try fetching from Redis first
    try:
        cached_json = await redis_client.get(f"transcription:{id}")
        if cached_json:
            return json.loads(cached_json)
    except Exception as e:
        print(f"Redis get error: {e}")

    # Fallback to DB
    def fetch_db():
        return TranscriptionStore.objects(sid=id).first()
        
    store = await asyncio.to_thread(fetch_db)
    
    if store:
        data = {
            "transcriptions": store.transcriptions,
            "stream_start_time": store.stream_start_time
        }
    else:
        data = {"transcriptions": []}
    
    # Update Redis
    try:
        await redis_client.setex(f"transcription:{id}", 3600, json.dumps(data))
    except Exception as e:
        print(f"Redis set error: {e}")
        
    return data


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


# Socket.IO Event Handlers
@sio.event
async def connect(sid, environ, auth):
    """Handle client connection"""
    # Create a session-like object for socket.io
    if auth and auth.get('secret_key') == SETTINGS["SECRET_KEY"]:
        await sio.save_session(sid, {'verified': True})
        print(f"Admin client connected: {sid}")
    else:
        await sio.save_session(sid, {'verified': False})
        print(f"Client connected: {sid}")
    await sio.emit('connected', {'status': 'connected', 'client_id': sid}, to=sid)

@sio.event
async def disconnect(sid):
    """Handle client disconnection"""
    print(f"Client disconnected: {sid}")

@sio.event
async def sync(sid, data):
    """Handle WebSocket sync events"""
    session = await sio.get_session(sid)
    
    if not session.get('verified'):
        if not session.get('secret_key') or session.get('secret_key') != data.get('secret_key'):
            await sio.emit('error', {'message': 'Unauthorized'}, to=sid)
            return
    
    session_id = data.get('id')
    if not session_id:
        await sio.emit('error', {'message': 'Session ID is required'}, to=sid)
        return
    

    
    # Remove id from the data before processing
    sync_data = data.copy()
    sync_data.pop("id", None)
    
    # Get cached transcription data (Redis or DB)
    cached_data = await get_cached_transcription(session_id)
    
    # Add stream start time
    yt_start_time = get_youtube_start_time(session_id)
    if yt_start_time:
         cached_data["stream_start_time"] = yt_start_time
    
    if sync_data.get("partial", False):
        cached_data["partial"] = sync_data
        # Update Redis Only
        await redis_client.setex(f"transcription:{session_id}", 3600, json.dumps(cached_data))
    else:
        cached_data["transcriptions"].append(sync_data)
        cached_data["transcriptions"].sort(key=lambda x: x["start_time"])
        
        # Update Redis
        await redis_client.setex(f"transcription:{session_id}", 3600, json.dumps(cached_data))
        
        # Save to MongoDB in background - Optimized: Push only the new segment
        asyncio.create_task(save_segment_background(session_id, sync_data, cached_data.get("stream_start_time")))
    
    print("sync", sync_data["start_time"], sync_data["result"]["corrected"])
    
    # Emit update to all clients in the session room
    await sio.emit('transcription_update', sync_data, room=session_id)

@sio.event
async def join_session(sid, data):
    """Handle client joining a session room"""
    session_id = data.get('session_id')
    secret_key = data.get('secret_key')
    
    session = await sio.get_session(sid)
    
    if secret_key:
        # Verify room exists with matching secret key using MongoEngine
        room = Room.objects(sid=session_id, secret_key=secret_key).first()
        if room:
            session['secret_key'] = secret_key
            session['verified'] = True
            await sio.save_session(sid, session)
            print(f"Client verified: {session_id}")
    
    if session_id:
        await sio.enter_room(sid, session_id)
        await sio.emit('joined_session', {'session_id': session_id}, to=sid)

@sio.event
async def leave_session(sid, data):
    """Handle client leaving a session room"""
    session_id = data.get('session_id')
    if session_id:
        await sio.leave_room(sid, session_id)
        await sio.emit('left_session', {'session_id': session_id}, to=sid)
        print(f"Client left session: {session_id}")
