# This file is part of g0v/realtime_transcribe.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.


from flask import Flask, jsonify, render_template, request, session, Response
from flask_socketio import SocketIO, emit, join_room, leave_room
from pathlib import Path
import uuid
import json
import threading
import time
import requests
import os
import dotenv
from datetime import datetime

dotenv.load_dotenv(override=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = str(uuid.uuid4())
socketio = SocketIO(app, cors_allowed_origins="*")
temp_dir = Path('temp')
temp_dir.mkdir(parents=True, exist_ok=True)

# In-memory cache for transcriptions
transcription_cache = {}
youtube_data_cache = {}

def get_youtube_start_time(video_id) -> datetime:
    """
    Get the actual stream start time for a YouTube video using YouTube Data API v3.
    Returns the actualStartTime if available, otherwise None.
    """
    data = None
    if video_id in youtube_data_cache and youtube_data_cache[video_id] is not None:
        data = youtube_data_cache[video_id]
    else:
        api_key = os.getenv('YOUTUBE_API_KEY')
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

def get_cached_transcription(id):
    if id not in transcription_cache or time.time() - transcription_cache[id][1] > 3600:
        temp_file = temp_dir / f"{id}.json"
        if temp_file.exists():
            data = json.loads(temp_file.read_text(encoding='utf-8'))
        else:
            data = {"transcriptions": []}
        transcription_cache[id] = (data, time.time())
    return transcription_cache[id][0]


def save_to_file_async(temp_file, data):
    """Save data to file in background thread"""
    def save():
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    threading.Thread(target=save, daemon=True).start()

@app.route("/")
def hello_world():
    return render_template("index.html")

@app.route("/download/<string:id>", methods=["get"])
def download(id):
    with open(temp_dir / f"{id}.json", "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/json")

@app.route("/yt/<string:id>", methods=["get"])
def yt(id):
    data = get_cached_transcription(id)
    data["stream_start_time"] = get_youtube_start_time(id)
    return render_template("yt.html", id=id, data=data)

@app.route("/rt/<string:id>", methods=["get"])
def rt(id):
    data = get_cached_transcription(id)
    sliced_data = data.copy()
    sliced_data["transcriptions"] = sliced_data["transcriptions"][-50:]
    return render_template("rt.html", id=id, data=sliced_data)
  
@socketio.on('sync')
def handle_sync(data):
    """Handle WebSocket sync events"""
    if not session.get('is_admin'):
        emit('error', {'message': 'Unauthorized'})
        return
    session_id = data.get('id')
    if not session_id:
        emit('error', {'message': 'Session ID is required'})
        return
    
    temp_file = temp_dir / f"{session_id}.json"
    
    # Remove id from the data before processing
    sync_data = data.copy()
    sync_data.pop("id", None)
    
    # Get cached transcription data
    cached_data = get_cached_transcription(session_id)
    
    # Add stream start time and append new transcription
    cached_data["stream_start_time"] = get_youtube_start_time(session_id)
    if sync_data.get("partial", False):
        cached_data["partial"] = sync_data
    else:
        cached_data["transcriptions"].append(sync_data)
        cached_data["transcriptions"].sort(key=lambda x: x["start_time"])
    transcription_cache[session_id] = (cached_data, time.time())

    # Save to file in background (non-blocking)
    save_to_file_async(temp_file, cached_data)
    
    print("sync", sync_data["start_time"], sync_data["result"]["corrected"])
    
    # Emit update to all clients in the session room
    socketio.emit('transcription_update', sync_data, room=session_id)

@socketio.on('connect')
def handle_connect(auth):
    """Handle client connection"""
    if auth and auth.get('token') == os.getenv('SECRET_KEY'):
        print(f"Admin connected: {request.sid}")
        session['is_admin'] = True
    else:
        print(f"Client connected: {request.sid}")
    emit('connected', {'status': 'connected', 'client_id': request.sid})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print(f"Client disconnected: {request.sid}")

@socketio.on('join_session')
def handle_join_session(data):
    """Handle client joining a session room"""
    session_id = data.get('session_id')
    if session_id:
        join_room(session_id)
        emit('joined_session', {'session_id': session_id})
        print(f"Client joined session: {session_id}")

@socketio.on('leave_session')
def handle_leave_session(data):
    """Handle client leaving a session room"""
    session_id = data.get('session_id')
    if session_id:
        leave_room(session_id)
        emit('left_session', {'session_id': session_id})
        print(f"Client left session: {session_id}")

