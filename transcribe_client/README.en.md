# Transcribe Client — Batch Transcription Client

Captures local microphone audio, runs **batch transcription** via **WhisperX / OpenAI Whisper / Groq**, and pushes the result to an OpenTransLive `live_server`.

Language: English ([繁體中文](README.md))

> Most users don't need this client — the `live_server` panel can transcribe directly from the browser microphone via ElevenLabs Scribe.
> Use this client when you need local GPU inference (WhisperX), or when you want to use the OpenAI / Groq APIs instead of ElevenLabs Scribe.
> For lower-latency streaming, use [`../realtime_client`](../realtime_client) instead.

## Install

```bash
cd transcribe_client
uv sync
cp .env.example .env
# Edit .env
```

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `SERVER_ENDPOINT` | OpenTransLive server URL |
| `SECRET_KEY` | Session secret key |
| `TRANSCRIBER` | `whisperx` / `openai` / `groq` |
| `TRANSCRIBE_MODEL` | Whisper model name (e.g. `deepdml/faster-whisper-large-v3-turbo-ct2`) |
| `TRANSCRIBE_DEVICE` | `cuda` or `cpu` |
| `OPENAI_API_KEY` | OpenAI (translation or Whisper API) |
| `GROQ_API_KEY` | Groq (required when using Groq) |
| `AI_MODEL` | Translation model, e.g. `gpt-4.1-mini` |
| `TRANSLATE_LANGUAGES` | Target languages, e.g. `zh-Hant,ja,ko,en` |
| `COMMON_PROMPT` | Event context / translation prompt |
| `RECORD_TIMEOUT` | Recording timeout in seconds |
| `RECORD_ENERGY_THRESHOLD` | Voice activity threshold |
| `RECORD_PAUSE_THRESHOLD_MS` | Sentence pause threshold in milliseconds |

## Run

```bash
uv run python run.py -t your_session_id
```

`-t` specifies which session to push to. The client continuously reads from the default microphone, transcribes, translates, and pushes to the server.

## Relationship to Other Components

- `live_server` panel browser mic (ElevenLabs Scribe): lowest friction, no client install.
- This client: when you need local inference or prefer the Whisper-family APIs.
- `realtime_client`: when you need lower latency via ElevenLabs Scribe Realtime or Google STT streaming.
