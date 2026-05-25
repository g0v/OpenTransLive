# OpenTransLive Milestones

This document only records the project's milestones. For actual usage, APIs, and deployment, see [docs/USAGE.en.md](docs/USAGE.en.md).

Language: English ([繁體中文](milestone.md))

| Date | Commit | Theme | Notes |
|---|---|---|---|
| 2025-11-22 | 0637440 | Realtime transcription research | Added `realtime_client` with ElevenLabs and Google STT prototypes |
| 2025-11-22 | 7eb521a | Initial web version | Reorganized into `live_server`, `transcribe_client`, `realtime_client`; added web templates and service skeleton |
| 2025-11-26 | 6f50a9e | User-created sessions | Users can create sessions, server issues secret keys, added the panel page |
| 2025-12-22 | 0ad34f2 | WebSocket / Docker / DB foundation | Rewrote on FastAPI, Redis, MongoDB; added `Dockerfile` and `docker-compose.yml` |
| 2026-02-24 | cfb50c4 | Realtime transcription & translation flow | Added Scribe manager, realtime frontend JS, translation module, Docker setup |
| 2026-03-06 | f9bc8ff | Domain terms / keywords | Added session keywords API and panel editing UI |
| 2026-03-10 | c12f3a7 | User account system | Added email OTP login, admin dashboard, and realtime permission management |
| 2026-04-09 | b536b0f | Auto keywords | Keywords moved to a frequency dictionary with extraction and ranking |
| 2026-04-16 | e1be265 | Subtitle data storage | Moved transcription data into its own segments collection for better management |
| 2026-04-21 | 32cd088 | History management | Added subtitle editor and session segments management API |
| 2026-04-23 | 25f77ef | Export | Added per-language SRT export |
| 2026-05-06 | a698ad2 | User text dictionary | Added text dictionary API and panel UI; translation flow applies user-defined substitutions |
| 2026-05-16 | 9154e4b | Viewer transport refresh | Switched viewer realtime transport to SSE; added viewer count |
