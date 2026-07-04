# Realtime Client — Streaming Transcription Client

Captures local microphone audio and **streams it to ElevenLabs Scribe Realtime or Google Speech-to-Text**, then pushes subtitles back to an OpenTransLive `live_server`. Lower latency than the batch mode in [`../transcribe_client`](../transcribe_client).

Language: English ([繁體中文](README.md))

> Most users don't need this client — the `live_server` panel can transcribe directly from the browser microphone via ElevenLabs Scribe.
> Use this client when pushing audio from a non-browser environment, or when you prefer Google STT over ElevenLabs.

## Install

```bash
cd realtime_client
uv sync
cp .env.example .env
# Edit .env
```

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `SERVER_ENDPOINT` | OpenTransLive server URL |
| `API_KEY` | Personal API key (generate at /user-dashboard; the account must own or co-own the target room) |
| `ELEVENLABS_API_KEY` | ElevenLabs Scribe Realtime |
| `GOOGLE_API_KEY` | Google API key |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google service account JSON |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID |
| `OPENAI_API_KEY` | OpenAI (translation) |
| `TRANSLATE_LANGUAGES` | Target languages, e.g. `en-US,cmn-Hant-TW` |
| `COMMON_PROMPT` | Event context / translation prompt |

> **API_KEY security**: use a **dedicated non-admin account** for this machine. The key only needs to own (or co-own) the target room to push subtitles — nothing more. Admin management endpoints (creating accounts, rotating, settings) are refused for key-authenticated callers even if the account is an admin, but least privilege is still the right posture. The key is sent once at connect, not resent on every update.

## Run

```bash
# ElevenLabs Scribe Realtime (default)
uv run python run.py -t your_session_id

# Switch to Google STT
uv run python run.py -t your_session_id -s google
```

Flags:

- `-t / --target-sid`: target session id
- `-s / --service`: `elevenlabs` (default) or `google`

## Relationship to Other Components

- `live_server` panel browser mic: lowest friction, no install, uses ElevenLabs Scribe.
- This client: push audio from a server host or another non-browser environment; can use Google STT.
- `transcribe_client`: local WhisperX / OpenAI / Groq batch inference.
