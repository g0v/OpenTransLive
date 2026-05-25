# OpenTransLive — Open-Source Broadcast Realtime Translation Framework for Event Organizers

OpenTransLive is **a broadcast-style (one-to-many) realtime translation framework for event organizers**, not a generic meeting collaboration tool. Fully open source (GNU AGPL v3.0), with a web UI and YouTube sync, designed to be self-hosted and freely modified.

The design assumes a single speaker or subtitle team produces the transcription, while an unlimited audience — onsite or online — watches in their preferred language: on the big screen, on a mobile webpage, or in YouTube live subtitles.

Typical scenarios: conferences, hackathons, public hearings, community meetups, livestreamed talks — anywhere a single audio source must be translated live for a multilingual audience. The viewer side requires no signup, no login, and has no audience cap; only the speaker / subtitle operator pushing audio needs to authenticate.

Language: English ([繁體中文](README.md))

![Screenshot_15-9-2025_1231_transcribe g0v tw](https://github.com/user-attachments/assets/9a7ff25a-557d-43b7-8071-e7a6ca176c5f)
![Screenshot_15-9-2025_115957_transcribe g0v tw](https://github.com/user-attachments/assets/6e36b33b-9d41-4734-a833-4a84fa3943cc)

## Features

- **Realtime transcription**: WhisperX, OpenAI, Groq, ElevenLabs Scribe, Google Speech-to-Text
- **Multilingual translation**: LLM-based, context-aware
- **User accounts**: Email OTP login, admin console, realtime permission, session co-owners
- **Session panel**: `/panel/{session_id}` for translation languages, Scribe language, tone, keywords, text dictionary
- **Edit history**: `/edit/{session_id}` to revise or delete committed segments and all translations
- **Viewer broadcast**: Viewer pages (`/rt`, `/yt`) receive subtitles over SSE — no login, no audience cap
- **YouTube sync**: `/yt/{session_id}` aligns subtitles with a YouTube live stream or VOD
- **Export**: Full JSON and per-language SRT
- **Storage**: MongoDB persistence with Redis for caching and multi-server scaling

## Project Structure

```
opentranslive/
├── live_server/            # FastAPI + Socket.IO web server
│   ├── app/                # Main application
│   │   ├── __init__.py     # FastAPI app, routes, Socket.IO handlers
│   │   ├── config.py       # Config (copy from config.example.py)
│   │   ├── database.py     # MongoDB integration
│   │   ├── email_auth.py   # Email OTP login
│   │   ├── scribe_manager.py      # ElevenLabs Scribe session manager
│   │   ├── translation_service.py # Translation pipeline & queue
│   │   ├── translators/    # Per-provider implementations
│   │   ├── socket_schema.py # Socket.IO event schema
│   │   ├── static/         # Static assets
│   │   └── templates/      # Jinja2 templates
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── README.en.md        # Server configuration details
├── transcribe_client/      # Batch transcription client (WhisperX / OpenAI / Groq)
├── realtime_client/        # Realtime streaming client (ElevenLabs / Google STT)
├── docs/USAGE.en.md        # Full usage guide (roles, flows, APIs, FAQ)
├── milestone.en.md         # Milestones
└── README.en.md            # This file
```

## Quick Start

### Prerequisites

- Python 3.11+
- MongoDB
- Redis
- At least one AI provider API key (any of OpenAI, Gemini, Groq, Cerebras)
- ElevenLabs API key for realtime microphone transcription

### Start the server

```bash
cd live_server

# Docker Compose (recommended)
cp app/config.example.py app/config.py
# Edit app/config.py — set SECRET_KEY, API keys, SMTP, etc.
docker-compose up -d
```

Or manually:

```bash
cd live_server
uv sync
cp app/config.example.py app/config.py
uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
```

After startup:

- Landing / login: `http://localhost:5000/`
- Viewer subtitles: `http://localhost:5000/rt/{session_id}`
- YouTube view: `http://localhost:5000/yt/{session_id}`

Server config details: [live_server/README.en.md](live_server/README.en.md).
Full usage flow (creating sessions, panel, editing, exporting): [docs/USAGE.en.md](docs/USAGE.en.md).

### External clients (optional)

The `live_server` panel can run realtime transcription directly from a browser microphone via ElevenLabs Scribe — no client needed. The clients below exist for local inference or alternative STT providers.

**Batch client** ([transcribe_client/README.en.md](transcribe_client/README.en.md)):

```bash
cd transcribe_client
uv sync
uv run python run.py -t your_session_id
```

**Realtime client** ([realtime_client/README.en.md](realtime_client/README.en.md)):

```bash
cd realtime_client
uv sync
uv run python run.py -t your_session_id
```

## Architecture

### Transcription flow

```
Mic → Panel (browser) → ElevenLabs Scribe → Correct/Translate → Server → SSE → Viewer pages
                                                                     ↓
                                                                MongoDB
                                                              (persistent)
```

### Realtime transport

- **Panel**: Socket.IO bidirectional control + audio upload
- **Viewer pages**: SSE one-to-many broadcast
- **Redis**: Cross-server pub/sub and cache
- **MongoDB**: Committed segments

### Translation system

- **Context-aware**: Uses recent subtitles as context
- **Keyword learning**: Auto-extract domain terms; user can pin
- **Text dictionary**: User-defined direct substitutions
- **Async pipeline**: Doesn't block the main transcription path
- **Parallel languages**: Translate to multiple targets concurrently

## System Requirements

### Server

- CPU: 2+ cores (4 recommended)
- RAM: 4GB+ (8GB recommended)
- Storage: 20GB+
- Stable internet

### Client (local WhisperX inference)

- CPU: 4+ cores
- RAM: 8GB+ (large models need 16GB)
- Optional NVIDIA GPU for major speedup

### Client (cloud APIs only)

- CPU: 2+ cores
- RAM: 2GB+
- Low-latency internet

## Deployment

### Development

```bash
cd live_server
docker-compose up -d
```

### Production

- MongoDB Atlas or self-hosted cluster
- Redis Cloud or self-hosted cluster
- Reverse proxy: Nginx or Caddy
- SSL/TLS: Let's Encrypt
- Multi-server horizontal scaling via Redis pub/sub

## License

GNU AGPL v3.0. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests welcome. Lead contributor: [SeanGau](https://github.com/SeanGau).

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit (`git commit -m 'Add amazing feature'`)
4. Push (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Related Docs

- [Usage guide docs/USAGE.en.md](docs/USAGE.en.md) — roles, flows, URLs, APIs, storage, FAQ
- [Milestones milestone.en.md](milestone.en.md)
- [Live Server config live_server/README.en.md](live_server/README.en.md)
- [Issue tracker](https://github.com/g0v/opentranslive/issues)

## Acknowledgements

Thanks to every contributor and to the g0v community.
