from flask import Flask, jsonify, render_template, request, Response
from pathlib import Path
import uuid
import json
import threading
import queue
import time
import requests
import os
import dotenv
from datetime import datetime
from queue import Empty

dotenv.load_dotenv(override=True)

app = Flask(__name__)
temp_dir = Path('temp')
temp_dir.mkdir(parents=True, exist_ok=True)

# Simple event broadcaster for SSE
event_queues = {}
event_lock = threading.Lock()

# In-memory cache for transcriptions
transcription_cache = {}
youtube_data_cache = {}

def get_youtube_start_time(video_id) -> datetime:
    """
    Get the actual stream start time for a YouTube video using YouTube Data API v3.
    Returns the actualStartTime if available, otherwise None.
    """
    data = None
    if video_id in youtube_data_cache:
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
    temp_file = temp_dir / f"{id}.json"
    if temp_file.exists():
        data = json.loads(temp_file.read_text(encoding='utf-8'))
    else:
        data = {}
    transcription_cache[id] = (data, time.time())
    return data

def get_event_queue(id):
    with event_lock:
        if id not in event_queues:
            event_queues[id] = queue.Queue()
        return event_queues[id]

def update_event_queue(id, data):
    queue = get_event_queue(id)
    queue.put(data)

@app.route("/")
def hello_world():
    return render_template("index.html")
  
@app.route("/yt/<string:id>", methods=["get"])
def yt(id):
    data = get_cached_transcription(id)
    data["stream_start_time"] = get_youtube_start_time(id)
    return render_template("yt.html", id=id, data=data)

@app.route("/rt/<string:id>", methods=["get"])
def rt(id):
    data = get_cached_transcription(id)
    return render_template("rt.html", id=id, data=data)
  
@app.route("/api/sync/<string:id>", methods=["post"])
def sync(id):
    body = request.get_json()
    temp_file = temp_dir / f"{id}.json"
    body.pop("id")
    if not temp_file.exists():
        data = {"transcriptions": []}
    else:
        data = json.loads(temp_file.read_text(encoding='utf-8'))
    data["stream_start_time"] = get_youtube_start_time(id)
    data["transcriptions"].append(body)
    data["transcriptions"].sort(key=lambda x: x["start_time"])
    temp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print("sync", temp_file)
    
    # Broadcast the update to connected clients
    update_event_queue(id, {"type": "update", "data": data})
    
    return jsonify({"status": "success", "temp_file": temp_file.name})

@app.route("/api/sse/<string:id>")
def sse(id):
    def generate():
        queue = get_event_queue(id)
        # Send initial connection message
        yield f"data: {json.dumps({'type': 'connected', 'id': id})}\n\n"
        
        while True:
            try:
                # Wait for events with timeout
                data = queue.get(timeout=30)
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except Empty:
                # Send keepalive
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
    
    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response
