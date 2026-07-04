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
cp app/secret/config.example.toml app/secret/config.toml
# Edit app/secret/config.toml
uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
```

Dev mode (auto-reload):

```bash
uv run uvicorn app:socket_app --reload --host 0.0.0.0 --port 5000
```

### Docker Compose

```bash
cp app/secret/config.example.toml app/secret/config.toml
# Edit app/secret/config.toml
docker-compose up -d
```

Compose starts the FastAPI server, MongoDB, and Redis.

## Configuration

### Primary config: `app/secret/config.toml`

Copy `app/secret/config.example.toml` to `app/secret/config.toml` and edit the sections
below (`app/config.py` is just the loader that reads this TOML; you rarely touch it):

| Section | Purpose |
|---|---|
| `[settings].SECRET_KEY` | Session cookie and signing |
| `[settings].YOUTUBE_API_KEY` | Look up YouTube live-stream start time |
| `[email_settings].ADMIN_EMAILS` | Emails granted `/dashboard` access |
| `[email_settings].SMTP_*` | OTP delivery; leave empty to log the OTP (dev) |
| `[mongodb_settings]` | MongoDB connection |
| `redis_url` | Redis connection |
| `[realtime_settings].ELEVENLABS_API_KEY` | ElevenLabs Scribe |
| `[realtime_settings].AI_PROVIDER` | Default correction / translation provider (`openai` / `gemini` / `groq` / `cerebras`) |
| `[realtime_settings].CORRECT_PROVIDER` | (Optional) provider used for correction only |
| `[realtime_settings].TRANSLATE_PROVIDER` | (Optional) provider used for translation only |
| `[realtime_settings].TRANSLATE_LANGUAGES` | Default translation targets |
| `[realtime_settings].COMMON_PROMPT` | Event context / translation prompt |
| `[realtime_settings].PARTIAL_INTERVAL` | Partial subtitle flush interval (seconds) |
| `[realtime_settings].SKIP_CORRECTION` | Skip the correction step |

> Each AI provider's model and prompt defaults live in `app/secret/models.example.toml`.
> To customize, copy it to `app/secret/models.toml` and edit (loaded when present,
> otherwise it falls back to the example). `models.toml` is gitignored.

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
├── config.py                # Config loader (reads secret/config.toml)
├── secret/                   # config.toml (secret), models.toml (override), *.example.toml
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
- **Programmatic clients** (transcribe/realtime) authenticate with a personal API key (HTTP `Authorization: Bearer otl_…` or the Socket.IO connect `auth`). One key per user, generated at `/user-dashboard`; the server stores only its SHA-256 hash and shows the plaintext once at creation. The key is verified once at the connection handshake and not resent on every update.
- A key's permissions are derived **live** from the user record (realtime + room ownership), never baked into the key — revoking realtime/room takes effect on the next request. **One exception**: admin *management* endpoints (creating accounts, rotating others, changing settings) are refused for key-authenticated callers even when the owning account is an admin. So broadcast machines should use a **dedicated non-admin account** that merely owns the target room.
- The key id shown in the dashboard is a fingerprint derived from the hash (`otl_` + hash prefix); it leaks no character of the secret.
- `ENVIRONMENT=production` enables Secure cookies and a strict Socket.IO CORS allowlist.
- Socket.IO events are validated via [socket_schema.py](app/socket_schema.py).

## Troubleshooting

See [../docs/USAGE.en.md#8-troubleshooting](../docs/USAGE.en.md#8-troubleshooting).
