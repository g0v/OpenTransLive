# Live Server — OpenTransLive Web Server

`live_server` is the core of OpenTransLive: a FastAPI + Socket.IO web server providing the panel, viewer pages, realtime transcription management, translation pipeline, subtitle storage, and export.

Language: English ([繁體中文](README.md))

For full roles, flows, APIs, and FAQ, see [../docs/USAGE.en.md](../docs/USAGE.en.md). This file covers server-specific configuration, startup, and operations only.

## Install

### Prerequisites

- Python 3.11+
- MongoDB
- Redis
- At least one AI provider API key
- (Optional) Docker and Docker Compose
- (Optional) ElevenLabs API key (realtime microphone)
- (Optional) YouTube API key (YouTube live-stream sync)

### Steps

```bash
cd live_server
uv sync
cp app/config.example.py app/config.py
# Edit app/config.py
uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
```

Dev mode (auto-reload):

```bash
uv run uvicorn app:socket_app --reload --host 0.0.0.0 --port 5000
```

### Docker Compose

```bash
cp app/config.example.py app/config.py
# Edit app/config.py
docker-compose up -d
```

Compose starts the FastAPI server, MongoDB, and Redis.

## Configuration

### Primary config: `app/config.py`

Copy from `app/config.example.py` and edit:

| Section | Purpose |
|---|---|
| `SETTINGS.SECRET_KEY` | Session cookie and signing |
| `SETTINGS.YOUTUBE_API_KEY` | Look up YouTube live-stream start time |
| `EMAIL_SETTINGS.ADMIN_EMAILS` | Emails granted `/dashboard` access |
| `EMAIL_SETTINGS.SMTP_*` | OTP delivery; leave empty to log the OTP (dev) |
| `MONGODB_SETTINGS` | MongoDB connection |
| `REDIS_URL` | Redis connection |
| `REALTIME_SETTINGS.ELEVENLABS_API_KEY` | ElevenLabs Scribe |
| `REALTIME_SETTINGS.AI_PROVIDER` | Default correction / translation provider (`openai` / `gemini` / `groq` / `cerebras`) |
| `REALTIME_SETTINGS.CORRECT_PROVIDER` | (Optional) provider used for correction only |
| `REALTIME_SETTINGS.TRANSLATE_PROVIDER` | (Optional) provider used for translation only |
| `REALTIME_SETTINGS.TRANSLATE_LANGUAGES` | Default translation targets |
| `REALTIME_SETTINGS.COMMON_PROMPT` | Event context / translation prompt |
| `REALTIME_SETTINGS.PARTIAL_INTERVAL` | Partial subtitle flush interval (seconds) |
| `REALTIME_SETTINGS.SKIP_CORRECTION` | Skip the correction step |

### Environment Variables

Some runtime options still come from environment variables:

| Variable | Description | Default |
|---|---|---|
| `ENVIRONMENT` | `production` enables Secure cookies and a strict Socket.IO CORS allowlist | `development` |
| `SOCKET_CORS_ALLOWED_ORIGINS` | Comma-separated Socket.IO allowlist in production | Built-in localhost allowlist |
| `SEGMENT_WRITE_WORKERS` | MongoDB segment-write worker count | `2` |
| `SEGMENT_WRITE_QUEUE_MAXSIZE` | Max segments queued before backpressure | `500` |
| `SEGMENT_WRITE_METRICS_LOG_INTERVAL_SEC` | Metrics log interval (seconds) | `10` |

### Committed Segment Write Strategy

Committed segments are persisted through a bounded queue rather than spawning one async task per segment:

- Queue: fixed-size `asyncio.Queue` (`SEGMENT_WRITE_QUEUE_MAXSIZE`)
- Workers: fixed concurrency (`SEGMENT_WRITE_WORKERS`)
- Overflow: when the queue is full, the oldest queued segment is dropped to bound memory while preserving newer context
- Metrics: queue depth, drop count, processed count, failure count, average write latency (cadence controlled by `SEGMENT_WRITE_METRICS_LOG_INTERVAL_SEC`)

## Code Layout

```
app/
├── __init__.py              # FastAPI app, HTTP routes, Socket.IO handlers, SSE
├── config.py                # Copied from config.example.py
├── database.py              # MongoDB client + collections
├── email_auth.py            # Email OTP login
├── http_client.py           # Shared httpx client
├── logger_config.py         # Logging setup
├── scribe_manager.py        # ElevenLabs Scribe session manager
├── socket_schema.py         # Socket.IO event schema validation
├── translation_service.py   # Correction + translation pipeline & queue
├── translators/             # Per-provider implementations
├── static/                  # CSS / JS / icons
└── templates/               # Jinja2 templates
```

## Viewer / Panel Transport

- **Viewer pages** (`/rt/{sid}`, `/yt/{sid}`) use SSE: `GET /api/session/{sid}/stream`, event `transcription_update`.
- **Panel** uses Socket.IO for bidirectional control and audio upload.

Full event list and APIs in [../docs/USAGE.en.md](../docs/USAGE.en.md#5-apis-and-realtime-transport).

## Storage

Field details in [../docs/USAGE.en.md](../docs/USAGE.en.md#6-data-storage). Summary:

- **MongoDB**: `rooms` (session config + owners), `transcription_segments` (committed segments), `transcription_store` (legacy)
- **Redis**: `transcription:{sid}:list` (recent segments), `transcription:{sid}:partial`, `transcription:{sid}:meta`, `keywords:{sid}`, `locked_keywords:{sid}`, `text_dictionary:{sid}`

## Security

- Viewer pages are fully public — no login required.
- Panel / editor / admin APIs use Email OTP login; session-level operations require the secret key or owner / co-owner permission.
- `ENVIRONMENT=production` enables Secure cookies and a strict Socket.IO CORS allowlist.
- Socket.IO events are validated via [socket_schema.py](app/socket_schema.py).

## Troubleshooting

See [../docs/USAGE.en.md#8-troubleshooting](../docs/USAGE.en.md#8-troubleshooting).
