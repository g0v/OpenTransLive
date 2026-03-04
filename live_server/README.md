# Live Server - OpenTransLive Web Server & API

The live_server is a FastAPI-based web server that provides real-time transcription display, translation management, and WebSocket communication for the OpenTransLive system.

## Features

- **FastAPI Web Framework**: Modern, high-performance web server with automatic API documentation
- **WebSocket Communication**: Real-time bidirectional communication using Socket.IO with Redis backend
- **MongoDB Integration**: Persistent storage for rooms, transcriptions, and session data
- **Redis Caching**: Fast in-memory caching for real-time transcription data
- **Real-time Translation**: Automatic translation management with multiple language support
- **ElevenLabs Scribe Integration**: Support for real-time audio transcription via ElevenLabs
- **Google Speech-to-Text Integration**: Alternative STT provider support
- **Session Management**: Secure session handling with token-based authentication
- **YouTube Integration**: Synchronized subtitles for YouTube videos and live streams
- **Multi-view Modes**: Grid layout and single-column layout for different viewing scenarios
- **QR Code Generation**: Easy mobile device access to transcription sessions
- **Security Features**: Input sanitization, NoSQL injection prevention, HMAC authentication

## Architecture

```
live_server/
├── app/
│   ├── __init__.py              # Main FastAPI app, routes, and Socket.IO handlers
│   ├── config.py                # Configuration management
│   ├── database.py              # MongoDB connection and collections
│   ├── logger_config.py         # Logging configuration
│   ├── scribe_manager.py        # ElevenLabs Scribe session management
│   ├── translator.py            # Translation service integration
│   ├── static/                  # Static assets (CSS, JS, images)
│   └── templates/               # Jinja2 HTML templates
├── pyproject.toml               # Python dependencies
├── Dockerfile                   # Docker container configuration
├── docker-compose.yml           # Docker Compose setup
└── README.md                    # This file
```

## Installation

### Prerequisites

- Python 3.11 or higher
- MongoDB instance (local or remote)
- Redis instance (for WebSocket scaling)
- Optional: Docker and Docker Compose

### Setup

1. **Navigate to live_server directory**:
   ```bash
   cd live_server
   ```

2. **Install dependencies**:
   ```bash
   uv sync
   ```

3. **Configure environment variables**:

   Create a `.env` file in the `live_server` directory:

   ```bash
   # Server Configuration
   SECRET_KEY=your-secret-key-here
   HOST=0.0.0.0
   PORT=5000

   # MongoDB Configuration
   MONGODB_URI=mongodb://localhost:27017
   MONGODB_DB_NAME=opentranslive

   # Redis Configuration
   REDIS_URL=redis://localhost:6379/0

   # YouTube API (for live stream time detection)
   YOUTUBE_API_KEY=your-youtube-api-key

   # AI translation provider: "gemini" or "openai"
   AI_PROVIDER=gemini
   GEMINI_API_KEY=your-gemini-api-key
   # OPENAI_API_KEY=your-openai-api-key  # set this instead when AI_PROVIDER=openai
   AI_MODEL=gemini-3.1-flash-lite-preview  # or gpt-4.1-mini for openai

   # Translation Settings
   TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
   COMMON_PROMPT="This is a meeting about software development"

   # ElevenLabs Configuration (optional)
   ELEVENLABS_API_KEY=your-elevenlabs-key

   # Google Cloud Configuration (optional)
   GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
   GOOGLE_CLOUD_PROJECT=your-project-id
   ```

4. **Start required services**:

   Using Docker Compose (recommended):
   ```bash
   docker-compose up -d
   ```

   Or manually start MongoDB and Redis:
   ```bash
   # MongoDB
   mongod --dbpath /path/to/data

   # Redis
   redis-server
   ```

5. **Run the server**:
   ```bash
   uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
   ```

   The server will be available at `http://localhost:5000`

## Configuration Options

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `SECRET_KEY` | Session encryption key | Random UUID | Yes |
| `HOST` | Server bind address | `0.0.0.0` | No |
| `PORT` | Server port | `5000` | No |
| `MONGODB_URI` | MongoDB connection string | `mongodb://localhost:27017` | Yes |
| `MONGODB_DB_NAME` | Database name | `opentranslive` | Yes |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` | Yes |
| `YOUTUBE_API_KEY` | YouTube Data API key | - | For YouTube features |
| `AI_PROVIDER` | Translation AI provider (`gemini` or `openai`) | `gemini` | No |
| `GEMINI_API_KEY` | Gemini API key | - | When `AI_PROVIDER=gemini` |
| `OPENAI_API_KEY` | OpenAI API key | - | When `AI_PROVIDER=openai` |
| `AI_MODEL` | Model name for translation | `gemini-3.1-flash-lite-preview` | No |
| `TRANSLATE_LANGUAGES` | Comma-separated language codes | `zh-Hant,ja,ko,en` | No |
| `COMMON_PROMPT` | Context prompt for transcription | - | No |
| `ELEVENLABS_API_KEY` | ElevenLabs API key | - | For ElevenLabs Scribe |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google Cloud credentials path | - | For Google STT |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID | - | For Google STT |

## API Documentation

### HTTP Endpoints

#### Web Views

```http
GET /                           # Main dashboard (WIP)
GET /rt/{session_id}            # Real-time transcription view
GET /yt/{session_id}            # YouTube integration view
GET /demo                       # Demo page
```

#### API Endpoints

```http
# Transcription sync
POST /api/sync/{session_id}
Content-Type: application/json

{
  "message": "transcription text",
  "start_time": 10.5,
  "end_time": 12.3,
  "created_at": "2024-01-01T12:00:00Z",
  "result": {
    "corrected": "corrected transcription",
    "translated": {
      "en": "Hello world",
      "zh-Hant": "你好世界",
      "ja": "こんにちは世界"
    },
    "special_keywords": ["keyword1", "keyword2"]
  }
}

# Session management
GET /api/sessions/{session_id}  # Get session details
POST /api/sessions              # Create new session

# Real-time tokens (for ElevenLabs/Google STT)
POST /api/user_realtime_token   # Get authentication token
```

### WebSocket Events

The server uses Socket.IO for real-time communication.

#### Client to Server Events

```javascript
// Join a session
socket.emit('join_session', {
  'session_id': 'your_session_id',
  'secret_key': 'optional_secret_key'
});

// Sync transcription data
socket.emit('sync', {
  'id': 'session_id',
  'message': 'transcription text',
  'start_time': 10.5,
  'end_time': 12.3,
  'created_at': '2024-01-01T12:00:00Z',
  'result': {
    'corrected': 'corrected transcription',
    'translated': {
      'en': 'Hello world',
      'zh-Hant': '你好世界',
      'ja': 'こんにちは世界'
    },
    'special_keywords': ['keyword1', 'keyword2']
  }
});

// Start ElevenLabs Scribe session
socket.emit('start_scribe_session', {
  'session_id': 'your_session_id',
  'language': 'en'
});

// Send audio data (for ElevenLabs Scribe)
socket.emit('audio_data', {
  'session_id': 'your_session_id',
  'audio': 'base64_encoded_audio_data'
});

// Stop Scribe session
socket.emit('stop_scribe_session', {
  'session_id': 'your_session_id'
});
```

#### Server to Client Events

```javascript
// Connection confirmation
socket.on('connected', (data) => {
  console.log('Connected:', data.client_id);
});

// Session join confirmation
socket.on('joined_session', (data) => {
  console.log('Joined session:', data.session_id);
});

// Real-time transcription updates
socket.on('transcription_update', (data) => {
  console.log('New transcription:', data);
});

// Scribe session events
socket.on('scribe_session_started', (data) => {
  console.log('Scribe session started:', data.session_id);
});

socket.on('scribe_transcription', (data) => {
  console.log('Scribe transcription:', data.text);
});

socket.on('scribe_error', (data) => {
  console.error('Scribe error:', data.error);
});
```

## Database Schema

### MongoDB Collections

#### rooms
Stores session/room information:
```json
{
  "_id": "session_id",
  "name": "Session Name",
  "created_at": "2024-01-01T12:00:00Z",
  "last_active": "2024-01-01T13:00:00Z",
  "settings": {
    "languages": ["en", "zh-Hant", "ja"],
    "auto_translate": true
  }
}
```

#### transcription_store
Stores transcription entries:
```json
{
  "session_id": "session_id",
  "message": "original transcription",
  "start_time": 10.5,
  "end_time": 12.3,
  "created_at": "2024-01-01T12:00:00Z",
  "result": {
    "corrected": "corrected transcription",
    "translated": {
      "en": "Hello world",
      "zh-Hant": "你好世界"
    },
    "special_keywords": ["keyword1"]
  }
}
```

#### realtime_tokens
Stores authentication tokens for real-time services:
```json
{
  "user_uid": "unique_user_id",
  "token": "authentication_token",
  "service": "elevenlabs",
  "created_at": "2024-01-01T12:00:00Z",
  "expires_at": "2024-01-01T13:00:00Z"
}
```

### Redis Data Structures

- **Session transcriptions**: `session:{session_id}:transcriptions` (List)
- **Session metadata**: `session:{session_id}:metadata` (Hash)
- **Active connections**: `session:{session_id}:clients` (Set)

## View Modes

### Real-time View (/rt/{session_id})

Dedicated view for live transcription display with auto-scrolling.

Features:
- Multi-language grid layout
- Single-column layout option
- Language selection dropdown
- Real-time WebSocket updates
- QR code generation for mobile access
- Responsive design for all screen sizes

### YouTube Integration (/yt/{session_id})

Embedded YouTube player with synchronized subtitles.

Features:
- YouTube player integration
- Synchronized subtitle display
- Live stream support
- Automatic start time detection
- Full-screen support

## Translation System

The server includes an advanced translation system with the following features:

- **Context-aware translation**: Uses conversation history for better accuracy
- **Keyword learning**: Automatically extracts and learns domain-specific keywords
- **Multi-language support**: Translates to multiple target languages simultaneously
- **Async processing**: Non-blocking translation using async HTTP client
- **Error handling**: Graceful fallback and retry mechanisms

Translation configuration in `.env`:

Using Gemini (default):
```bash
AI_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-api-key
AI_MODEL=gemini-3.1-flash-lite-preview
TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
COMMON_PROMPT="Context about the conversation"
```

Using OpenAI:
```bash
AI_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key
AI_MODEL=gpt-4.1-mini
TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
COMMON_PROMPT="Context about the conversation"
```

## ElevenLabs Scribe Integration

The server supports real-time transcription using ElevenLabs Scribe:

1. Client requests authentication token via `/api/user_realtime_token`
2. Client establishes WebSocket connection to server
3. Client emits `start_scribe_session` event
4. Server creates ScribeSessionManager and connects to ElevenLabs
5. Client streams audio via `audio_data` events
6. Server receives transcriptions and broadcasts to all session clients
7. Transcriptions are automatically translated and stored

## Google Speech-to-Text Integration

Alternative to ElevenLabs, using Google Cloud STT:

1. Configure `GOOGLE_APPLICATION_CREDENTIALS` and `GOOGLE_CLOUD_PROJECT`
2. Client requests token via `/api/user_realtime_token`
3. Use Google STT client (see `realtime_client/google_stt_v2.py`)
4. Stream transcriptions to server via WebSocket

## Security Features

- **Input sanitization**: All user inputs are sanitized to prevent injection attacks
- **NoSQL injection prevention**: Query parameter validation and type checking
- **Session security**: HMAC-based authentication for admin operations
- **Token-based auth**: Secure token generation for real-time services
- **CORS configuration**: Configurable CORS policies
- **Rate limiting**: Built-in protection against abuse (configurable)

## Docker Deployment

### Using Docker Compose

```bash
cd live_server
docker-compose up -d
```

This starts:
- FastAPI web server
- MongoDB instance
- Redis instance

### Manual Docker Build

```bash
docker build -t opentranslive-server .
docker run -p 5000:5000 \
  -e MONGODB_URI=mongodb://mongo:27017 \
  -e REDIS_URL=redis://redis:6379 \
  opentranslive-server
```

## Development

### Running in Development Mode

```bash
# With auto-reload
uv run uvicorn app:socket_app --reload --host 0.0.0.0 --port 5000

# With debug logging
export LOG_LEVEL=DEBUG
uv run uvicorn app:socket_app --reload --host 0.0.0.0 --port 5000
```

### API Documentation

FastAPI provides automatic interactive API documentation:
- Swagger UI: `http://localhost:5000/docs`
- ReDoc: `http://localhost:5000/redoc`

### Testing WebSocket Connections

Use the provided test scripts or browser console:

```javascript
const socket = io('http://localhost:5000');

socket.on('connect', () => {
  console.log('Connected');
  socket.emit('join_session', { session_id: 'test-session' });
});

socket.on('transcription_update', (data) => {
  console.log('Update:', data);
});
```

## Troubleshooting

### Common Issues

1. **MongoDB connection failed**
   - Verify MongoDB is running: `systemctl status mongod`
   - Check connection string in `.env`
   - Ensure network connectivity

2. **Redis connection failed**
   - Verify Redis is running: `redis-cli ping`
   - Check Redis URL in `.env`
   - Check firewall settings

3. **WebSocket connection issues**
   - Check CORS settings in code
   - Verify Redis is accessible (required for Socket.IO)
   - Check client-side Socket.IO version compatibility

4. **Translation not working**
   - Verify `GEMINI_API_KEY` is set correctly
   - Check API quota and rate limits
   - Review logs for API errors

5. **ElevenLabs Scribe errors**
   - Verify `ELEVENLABS_API_KEY` is valid
   - Check API quota
   - Ensure audio format is compatible (16-bit PCM, 16kHz)

## Dependencies

Core dependencies (see `pyproject.toml` for full list):

- `fastapi` - Web framework
- `uvicorn` - ASGI server
- `python-socketio` - WebSocket support
- `aioredis` - Async Redis client
- `motor` - Async MongoDB driver
- `pymongo` - MongoDB driver
- `elevenlabs` - ElevenLabs SDK
- `jinja2` - Template engine
- `qrcode` - QR code generation
- `httpx` - Async HTTP client

## License

This project is part of g0v/opentranslive and is licensed under the GNU AGPL v3.0.
See LICENSE for details.

## Contributing

Contributions are welcome! Please submit issues and pull requests to the main repository.
