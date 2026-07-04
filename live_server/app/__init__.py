# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import asyncio
import collections
import hashlib
import json
import os
import re
import secrets
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import dotenv
import redis.asyncio as redis
import socketio
from cachetools import TTLCache
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
from starlette.middleware.sessions import SessionMiddleware

dotenv.load_dotenv(override=True)

from .config import SETTINGS, REDIS_URL
try:
    from .config import EMAIL_SETTINGS
except ImportError:
    EMAIL_SETTINGS = {}

if not SETTINGS.get("SECRET_KEY"):
    raise RuntimeError(
        "SECRET_KEY must be set in config/env. Refusing to start with an "
        "ephemeral fallback — that silently invalidates every session cookie "
        "on restart."
    )
from .database import rooms_collection, transcription_store_collection, transcription_segments_collection, users_collection, init_indexes
from .logger_config import setup_logger, log_exception
from .scribe_manager import ScribeSessionManager
from .socket_schema import (
    validate_sync_payload,
    validate_audio_buffer_append_payload,
)
from .email_auth import (
    validate_email_format,
    generate_otp,
    store_otp,
    verify_otp,
    send_otp_email,
    get_or_create_user,
)
from .api_key import generate_api_key, hash_api_key, looks_like_api_key

# Setup logger
logger = setup_logger(__name__)

# Session manager caches: max 512 concurrent sessions.
# When a session is evicted by cachetools (TTL expiry or capacity), its manager is
# stopped so background tasks are cleaned up promptly.
# TTL is refreshed by every heartbeat (30s) and every audio chunk, so it only
# fires when a session is truly idle. 300s gives plenty of slack for network
# jitter that drops a heartbeat or two without tearing down the ElevenLabs WS.
_MANAGER_CACHE_TTL = 300  # seconds (5 min)
_MANAGER_CACHE_MAX = 512
_YOUTUBE_CACHE_TTL = 60   # seconds; unrelated to manager lifecycle
_SEGMENT_WRITE_WORKERS = max(1, int(os.getenv("SEGMENT_WRITE_WORKERS", "2")))
_SEGMENT_WRITE_QUEUE_MAXSIZE = max(1, int(os.getenv("SEGMENT_WRITE_QUEUE_MAXSIZE", "500")))
_SEGMENT_WRITE_METRICS_LOG_INTERVAL = max(
    1.0, float(os.getenv("SEGMENT_WRITE_METRICS_LOG_INTERVAL_SEC", "10"))
)


class _SocketRateLimiter:
    """In-memory sliding-window rate limiter for Socket.IO events."""

    def __init__(self):
        self._timestamps: dict[str, collections.deque] = {}

    def check(self, socket_id: str, event: str, max_calls: int, window: float) -> bool:
        """Return True if the call is allowed, False if rate-limited."""
        now = time.monotonic()
        key = f"{socket_id}:{event}"
        timestamps = self._timestamps.get(key)
        if timestamps is None:
            timestamps = collections.deque()
            self._timestamps[key] = timestamps

        # Prune expired entries -- O(1) per pop with deque
        cutoff = now - window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= max_calls:
            return False

        timestamps.append(now)
        return True

    def cleanup(self, socket_id: str) -> None:
        """Remove all tracking data for a disconnected socket."""
        keys_to_remove = [k for k in self._timestamps if k.startswith(f"{socket_id}:")]
        for k in keys_to_remove:
            del self._timestamps[k]


_socket_limiter = _SocketRateLimiter()


class _ManagerTTLCache(TTLCache):
    """TTLCache that calls manager.stop() when an entry is evicted."""

    def popitem(self):
        key, manager = super().popitem()
        # Schedule the async stop without blocking the eviction path.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(manager.stop())
        except RuntimeError:
            pass  # No running loop (e.g. during interpreter shutdown) — skip.
        return key, manager


active_scribe_managers: TTLCache = _ManagerTTLCache(
    maxsize=_MANAGER_CACHE_MAX, ttl=_MANAGER_CACHE_TTL
)
active_translation_managers: TTLCache = _ManagerTTLCache(
    maxsize=_MANAGER_CACHE_MAX, ttl=_MANAGER_CACHE_TTL
)
# Pending debounce tasks for partial transcription broadcasts, keyed by session_id.
_partial_debounce_tasks: dict = {}
# Per-session locks serializing the read-modify-write on transcription:{sid}:partial so that
# flow_only client partials, server scribe translation partials, and commits cannot interleave
# across the read->write await gap and stomp each other. Valid only while SERVER_WORKERS == 1
# (single uvicorn process / event loop); with multiple workers this must move to Redis-level CAS.
# WeakValueDictionary so locks vanish once no coroutine holds or waits on them.
_partial_rmw_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()
# Per-session locks to prevent concurrent _get_or_create_scribe_manager calls from racing.
# WeakValueDictionary so entries vanish once no caller is holding / waiting on the lock;
# otherwise every session_id ever seen would leak an asyncio.Lock for the life of the process.
_scribe_create_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _get_or_create_lock(registry: "weakref.WeakValueDictionary[str, asyncio.Lock]", key: str) -> asyncio.Lock:
    # Atomic in asyncio: no await between .get() and the assignment, so two concurrent
    # callers cannot each install a separate Lock for the same key.
    lock = registry.get(key)
    if lock is None:
        lock = asyncio.Lock()
        registry[key] = lock
    return lock


class SegmentWriteQueue:
    """Bounded Mongo write queue with fixed workers and basic backpressure metrics."""

    def __init__(self, *, workers: int, maxsize: int, metrics_log_interval: float):
        self.queue: asyncio.Queue[tuple[str, dict, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self.workers = workers
        self.metrics_log_interval = metrics_log_interval
        self._tasks: list[asyncio.Task] = []
        self._enqueued = 0
        self._processed = 0
        self._dropped = 0
        self._failed = 0
        self._write_latency_total_ms = 0.0
        self._last_metrics_log = time.monotonic()

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker_loop(idx), name=f"segment-writer-{idx}")
            for idx in range(self.workers)
        ]

    async def stop(self) -> None:
        if not self._tasks:
            return
        # Drain queued writes before worker shutdown so committed segments are persisted.
        await self.queue.join()
        for _ in self._tasks:
            await self.queue.put(None)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._log_metrics(force=True)

    def enqueue(self, sid: str, segment: dict, stream_start_time: Any) -> bool:
        if self.queue.full():
            # Drop the oldest queued item to keep memory bounded and preserve recency.
            try:
                _ = self.queue.get_nowait()
                self.queue.task_done()
                self._dropped += 1
            except asyncio.QueueEmpty:
                pass

        try:
            self.queue.put_nowait((sid, segment, stream_start_time))
        except asyncio.QueueFull:
            self._dropped += 1
            return False

        self._enqueued += 1
        return True

    async def _worker_loop(self, worker_idx: int) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                break

            sid, segment, stream_start_time = item
            started = time.perf_counter()
            try:
                await _save_segment_to_mongo(sid, segment, stream_start_time)
                self._processed += 1
            except Exception as e:
                self._failed += 1
                log_exception(logger, e, f"segment write worker {worker_idx} failed")
            finally:
                self._write_latency_total_ms += (time.perf_counter() - started) * 1000
                self.queue.task_done()
                self._log_metrics()

    def snapshot(self) -> dict[str, float | int]:
        avg_latency_ms = (
            self._write_latency_total_ms / self._processed if self._processed > 0 else 0.0
        )
        return {
            "queue_depth": self.queue.qsize(),
            "queue_maxsize": self.queue.maxsize,
            "workers": self.workers,
            "enqueued": self._enqueued,
            "processed": self._processed,
            "dropped": self._dropped,
            "failed": self._failed,
            "avg_write_latency_ms": round(avg_latency_ms, 2),
        }

    def _log_metrics(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_metrics_log) < self.metrics_log_interval:
            return
        metrics = self.snapshot()
        logger.info(
            "segment_write_queue metrics depth=%s/%s workers=%s enqueued=%s processed=%s dropped=%s failed=%s avg_write_ms=%s",
            metrics["queue_depth"],
            metrics["queue_maxsize"],
            metrics["workers"],
            metrics["enqueued"],
            metrics["processed"],
            metrics["dropped"],
            metrics["failed"],
            metrics["avg_write_latency_ms"],
        )
        self._last_metrics_log = now


segment_write_queue = SegmentWriteQueue(
    workers=_SEGMENT_WRITE_WORKERS,
    maxsize=_SEGMENT_WRITE_QUEUE_MAXSIZE,
    metrics_log_interval=_SEGMENT_WRITE_METRICS_LOG_INTERVAL,
)


async def _get_or_create_scribe_manager(session_id, *, force_new: bool = False) -> ScribeSessionManager:
    """Return the existing running ScribeSessionManager for the session, or create a new one.
    Pass force_new=True to unconditionally restart (e.g. after a language change).
    """
    async with _get_or_create_lock(_scribe_create_locks, session_id):
        existing: ScribeSessionManager | None = active_scribe_managers.get(session_id)
        if existing and existing.is_running and not force_new:
            return existing

        if existing is not None:
            asyncio.create_task(existing.stop())

        from .translation_service import get_session_scribe_language, get_session_partial_interval
        language_code = await get_session_scribe_language(redis_client, session_id)
        partial_interval = await get_session_partial_interval(session_id)
        manager = ScribeSessionManager(session_id, on_scribe_transcription, language_code=language_code, partial_interval=partial_interval)
        manager.yt_start_time = await get_youtube_start_time(session_id)
        active_scribe_managers[session_id] = manager
        asyncio.create_task(manager.start())
        return manager


def _get_or_create_translation_manager(session_id):
    """Return existing TranslationQueueManager or create and start a new one."""
    manager = active_translation_managers.get(session_id)
    if not manager:
        from .translation_service import TranslationQueueManager
        manager = TranslationQueueManager(on_translation_completed)
        active_translation_managers[session_id] = manager
        asyncio.create_task(manager.start())
    return manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_indexes()
    _limiter_redis = redis.from_url(REDIS_URL, decode_responses=True)
    await FastAPILimiter.init(_limiter_redis)
    segment_write_queue.start()
    yield
    # Shutdown
    logger.info("Shutting down resources")
    await FastAPILimiter.close()
    # Close translator and shared HTTP client
    from .translators import close_translator
    await close_translator()

    # Stop all active scribe managers (snapshot first to avoid mutation during iteration)
    for manager in list(active_scribe_managers.values()):
        await manager.stop()

    # Stop all active translation managers
    for manager in list(active_translation_managers.values()):
        await manager.stop()
    await segment_write_queue.stop()

# Initialize FastAPI app with lifespan.
# ReDoc/Swagger are served at /redoc and /docs but expose only the API-key
# surface (see _public_api_openapi below), never the browser-only endpoints.
app = FastAPI(
    title="OpenTransLive API",
    version="1.0.0",
    description=(
        "Endpoints callable with a personal API key.\n\n"
        "Authenticate every request with `Authorization: Bearer otl_...` "
        "(create the key from your user dashboard). Permissions are derived "
        "live from your account, so revoking realtime/admin takes effect "
        "immediately."
    ),
    lifespan=lifespan,
)

# Endpoint function names reachable with an API key (they resolve the caller via
# get_identity / require_identity / require_realtime / _require_session_owner /
# _require_session_primary_owner). Admin-management endpoints are cookie-only
# (Identity.can_admin) and browser pages are HTML, so both are excluded here.
_PUBLIC_API_ENDPOINTS = {
    "create_api_key", "revoke_api_key", "get_me",
    "create_room", "list_rooms",
    "get_session_languages_endpoint", "update_session_languages_endpoint",
    "get_session_keywords_endpoint", "update_session_keywords_endpoint",
    "get_session_text_dictionary_endpoint", "update_session_text_dictionary_endpoint",
    "get_session_scribe_language_endpoint", "update_session_scribe_language_endpoint",
    "get_session_translate_tone_endpoint", "update_session_translate_tone_endpoint",
    "get_session_co_owners_endpoint", "add_session_co_owner_endpoint",
    "remove_session_co_owner_endpoint",
    "update_session_segment_endpoint", "delete_session_segment_endpoint",
    "delete_session",
}

# JSON request bodies for the endpoints that read `await request.json()`. The
# handlers keep their own validation; these entries only teach ReDoc what to
# send (path params and typed query params are documented automatically).
_PUBLIC_API_REQUEST_BODIES: dict[str, dict] = {
    "create_room": {
        "description": "Optional. Supply `sid` to claim a specific room id; omit to auto-generate one.",
        "schema": {"type": "object", "properties": {
            "sid": {"type": "string", "description": "Desired room id (4–64 chars, [A-Za-z0-9_-])."}}},
        "example": {"sid": "my-live-room"},
    },
    "update_session_languages_endpoint": {
        "required": True,
        "schema": {"type": "object", "required": ["languages"], "properties": {
            "languages": {"type": "array", "minItems": 1,
                          "items": {"type": "string", "maxLength": 32},
                          "description": "Target translation languages."}}},
        "example": {"languages": ["en", "ja", "ko"]},
    },
    "update_session_keywords_endpoint": {
        "required": True,
        "schema": {"type": "object", "required": ["keywords"], "properties": {
            "keywords": {"type": "array", "items": {"type": "string", "maxLength": 128},
                         "description": "Domain keywords that bias transcription."},
            "locked_keywords": {"type": "array", "items": {"type": "string", "maxLength": 128},
                                "description": "Optional. Keywords that must never be dropped."}}},
        "example": {"keywords": ["OpenTransLive", "g0v"], "locked_keywords": ["g0v"]},
    },
    "update_session_text_dictionary_endpoint": {
        "required": True,
        "schema": {"type": "object", "required": ["text_dictionary"], "properties": {
            "text_dictionary": {"type": "array", "maxItems": 200,
                "items": {"type": "object", "required": ["from", "to"], "properties": {
                    "from": {"type": "string", "maxLength": 200},
                    "to": {"type": "string", "maxLength": 200}}},
                "description": "Replacement rules applied to recognized text."}}},
        "example": {"text_dictionary": [{"from": "open trans", "to": "OpenTrans"}]},
    },
    "update_session_scribe_language_endpoint": {
        "required": True,
        "schema": {"type": "object", "properties": {
            "language": {"type": "string",
                         "description": "ISO 639-1/639-3 code to force detection; empty string clears (auto-detect)."}}},
        "example": {"language": "en"},
    },
    "update_session_translate_tone_endpoint": {
        "required": True,
        "schema": {"type": "object", "properties": {
            "tone": {"type": "string", "maxLength": 64,
                     "description": "Free-text tone (1–64 word chars, spaces, or hyphens); empty clears."}}},
        "example": {"tone": "formal"},
    },
    "add_session_co_owner_endpoint": {
        "required": True,
        "schema": {"type": "object", "required": ["email"], "properties": {
            "email": {"type": "string", "format": "email", "description": "Co-owner email to grant access."}}},
        "example": {"email": "cohost@example.com"},
    },
    "update_session_segment_endpoint": {
        "required": True,
        "schema": {"type": "object", "required": ["start_time"], "properties": {
            "start_time": {"type": "number", "description": "Identifies the committed segment to edit."},
            "corrected": {"type": "string", "maxLength": 5000, "description": "Optional. Corrected source text."},
            "translated": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 5000},
                           "description": "Optional. Per-language corrected translations, keyed by language code."}}},
        "example": {"start_time": 12.34, "corrected": "Hello world", "translated": {"ja": "こんにちは世界"}},
    },
}


def _apply_request_bodies(schema: dict, routes: list) -> None:
    """Attach the documented request bodies to their operations in `schema`."""
    for route in routes:
        spec = _PUBLIC_API_REQUEST_BODIES.get(getattr(route, "name", None))
        if not spec:
            continue
        media: dict = {"schema": spec["schema"]}
        if "example" in spec:
            media["example"] = spec["example"]
        request_body = {"required": spec.get("required", False),
                        "content": {"application/json": media}}
        if spec.get("description"):
            request_body["description"] = spec["description"]
        for method in route.methods:
            op = schema["paths"].get(route.path, {}).get(method.lower())
            if op is not None:
                op["requestBody"] = request_body


def _public_api_openapi() -> dict:
    """Build an OpenAPI schema containing only the API-key-callable endpoints,
    with a Bearer security scheme applied globally so ReDoc renders the auth."""
    if app.openapi_schema:
        return app.openapi_schema
    routes = [r for r in app.routes if getattr(r, "name", None) in _PUBLIC_API_ENDPOINTS]
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=routes,
    )
    _apply_request_bodies(schema, routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKey": {"type": "http", "scheme": "bearer", "bearerFormat": "otl_..."}
    }
    schema["security"] = [{"ApiKey": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _public_api_openapi

# Add session middleware.
# SameSite=Lax blocks cross-site POST/DELETE cookie attachment — the main CSRF
# mitigation for /api/session/{sid}/..., /heartbeat, /release-admin. Secure is
# enabled in production so the cookie is never sent over plain HTTP.
_IS_PRODUCTION = os.environ.get("ENVIRONMENT", "development").lower() == "production"
app.add_middleware(
    SessionMiddleware,
    secret_key=SETTINGS["SECRET_KEY"],
    same_site="lax",
    https_only=_IS_PRODUCTION,
)

# Setup templates
timestamp = datetime.now(timezone.utc).timestamp()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["timestamp"] = timestamp

# Mount static files
static_dir = Path("app/static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

mgr = socketio.AsyncRedisManager(REDIS_URL)


def _parse_socket_cors_origins() -> list[str] | str:
    """Resolve the Socket.IO CORS allowlist from SOCKET_CORS_ALLOWED_ORIGINS.

    Production: the env var is required, must be a comma-separated list of
    explicit origins, and rejects '*' outright. Development: if unset, falls
    back to common localhost origins; '*' is allowed so frontend devs can
    point arbitrary tools at a local server.
    """
    raw = os.environ.get("SOCKET_CORS_ALLOWED_ORIGINS", "").strip()

    if _IS_PRODUCTION:
        if not raw:
            raise RuntimeError(
                "SOCKET_CORS_ALLOWED_ORIGINS must be set in production. "
                "Provide a comma-separated allowlist of origins "
                "(e.g. 'https://example.com,https://app.example.com'). "
                "Wildcard '*' is not accepted in production."
            )
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if not origins or any(o == "*" for o in origins):
            raise RuntimeError(
                "SOCKET_CORS_ALLOWED_ORIGINS must not contain '*' in production."
            )
        return origins

    if not raw:
        return [
            "http://localhost:5000",
            "http://127.0.0.1:5000",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    if raw == "*":
        return "*"
    return [o.strip() for o in raw.split(",") if o.strip()]


_SOCKET_CORS_ORIGINS = _parse_socket_cors_origins()

# Initialize Socket.IO with ASGI support
sio = socketio.AsyncServer(
    async_mode='asgi',
    client_manager=mgr,
    cors_allowed_origins=_SOCKET_CORS_ORIGINS,
    logger=False
)

# Wrap with ASGI application
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

youtube_data_cache: TTLCache = TTLCache(maxsize=256, ttl=_YOUTUBE_CACHE_TTL)

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

TRANSCRIPTION_TTL = 3600
TRANSCRIPTION_ZSET_MAX = 250
SSE_RETRY_MS = 3000
SSE_HEARTBEAT_SECONDS = 15
VIEWER_PRESENCE_TTL = 35
VIEWER_PRESENCE_REFRESH = 10


def _transcription_room_channel(sid: str) -> str:
    return f"room:{sid}"


def _viewer_presence_key(sid: str) -> str:
    return f"viewer_presence:{sid}"


async def _viewer_presence_op(
    sid: str,
    *,
    add: str | None = None,
    remove: str | None = None,
    label: str = "viewer presence",
) -> int:
    key = _viewer_presence_key(sid)
    now = time.time()
    try:
        pipe = redis_client.pipeline()
        if add is not None:
            pipe.zadd(key, {add: now})
        if remove is not None:
            pipe.zrem(key, remove)
        pipe.zremrangebyscore(key, "-inf", now - VIEWER_PRESENCE_TTL)
        pipe.zcard(key)
        pipe.expire(key, VIEWER_PRESENCE_TTL)
        results = await pipe.execute()
        return int(results[-2] or 0)
    except Exception as e:
        log_exception(logger, e, f"Redis {label} error for {_hash_token(sid)}")
        return 0


async def _emit_viewer_count(sid: str, count: int) -> None:
    await sio.emit("viewer_count_update", {"session_id": sid, "viewer_count": count}, room=sid)


async def _emit_session_settings_update(sid: str, kind: str) -> None:
    """Notify panel clients that a session setting has changed.

    Only sockets that completed `join_session` (panel primary owner + co-owners,
    not viewers) are in the room, so this is safe to broadcast unconditionally.
    """
    await sio.emit("session_settings_updated", {"session_id": sid, "kind": kind}, room=sid)


def _transcription_event_id(payload: dict) -> str:
    if payload.get("partial") is True:
        return ""
    return str(payload.get("start_time", ""))


def _format_sse(payload: dict, *, event: str = "transcription_update") -> str:
    event_id = _transcription_event_id(payload)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    lines = [f"event: {event}", f"data: {data}"]
    if event_id:
        lines.insert(0, f"id: {event_id}")
    return "\n".join(lines) + "\n\n"


async def _publish_transcription_update(sid: str, payload: dict) -> None:
    try:
        await redis_client.publish(
            _transcription_room_channel(sid),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception as e:
        log_exception(logger, e, f"Redis publish error for {_hash_token(sid)}")


async def _iter_replay_transcription_events(sid: str, last_event_id: float) -> AsyncIterator[dict]:
    zset_key = f"transcription:{sid}:list"
    try:
        raw_segments = await redis_client.zrangebyscore(zset_key, f"({last_event_id}", "+inf")
    except Exception as e:
        log_exception(logger, e, f"Redis replay error for {_hash_token(sid)}")
        return

    for raw in raw_segments:
        try:
            yield json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Skipping malformed replay segment sid_hash=%s", _hash_token(sid))


async def _session_sse_stream(request: Request, sid: str, last_event_id: float | None) -> AsyncIterator[str]:
    channel = _transcription_room_channel(sid)
    pubsub = redis_client.pubsub()
    max_sent_id = last_event_id
    viewer_id = f"sse:{uuid.uuid4()}"
    last_emitted_count = -1
    try:
        await pubsub.subscribe(channel)
        count = await _viewer_presence_op(sid, add=viewer_id, label="viewer presence join")
        if count != last_emitted_count:
            await _emit_viewer_count(sid, count)
            last_emitted_count = count
        last_presence_refresh = time.monotonic()
        yield f"retry: {SSE_RETRY_MS}\n\n"

        if last_event_id is not None:
            async for payload in _iter_replay_transcription_events(sid, last_event_id):
                if await request.is_disconnected():
                    return
                event_id_raw = payload.get("start_time")
                if isinstance(event_id_raw, (int, float)) and not isinstance(event_id_raw, bool):
                    max_sent_id = max(max_sent_id or event_id_raw, float(event_id_raw))
                yield _format_sse(payload)

        while True:
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - last_presence_refresh >= VIEWER_PRESENCE_REFRESH:
                count = await _viewer_presence_op(sid, add=viewer_id, label="viewer presence refresh")
                if count != last_emitted_count:
                    await _emit_viewer_count(sid, count)
                    last_emitted_count = count
                last_presence_refresh = now

            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=SSE_HEARTBEAT_SECONDS,
            )
            if message is None:
                yield ": heartbeat\n\n"
                continue

            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                logger.warning("Skipping malformed pubsub event sid_hash=%s", _hash_token(sid))
                continue

            event_id_raw = payload.get("start_time")
            if (
                payload.get("partial") is not True
                and isinstance(event_id_raw, (int, float))
                and not isinstance(event_id_raw, bool)
            ):
                event_id = float(event_id_raw)
                if max_sent_id is not None and event_id <= max_sent_id:
                    continue
                max_sent_id = event_id

            yield _format_sse(payload)
    finally:
        count = await _viewer_presence_op(sid, remove=viewer_id, label="viewer presence leave")
        if count != last_emitted_count:
            await _emit_viewer_count(sid, count)
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception as e:
            log_exception(logger, e, f"Redis pubsub close error for {_hash_token(sid)}")

def validate_query_param(value: str, param_name: str = "parameter") -> tuple[bool, str]:
    """Validate user input to prevent NoSQL injection. Returns (is_valid, error_message)."""
    if not isinstance(value, str):
        return False, f"Invalid {param_name}: must be a string"
    if not value.strip():
        return False, f"Invalid {param_name}: cannot be empty"

    # Strict validation for session IDs
    if "session" in param_name.lower() or param_name.lower() == "sid":
        if len(value) < 4 or len(value) > 64:
            return False, f"Invalid {param_name}: must be between 4 and 64 characters"
        if not re.match(r'^[a-zA-Z0-9_-]+$', value):
            return False, f"Invalid {param_name}: must contain only alphanumeric characters, hyphens, and underscores"
    else:
        if '$' in value or '.' in value:
            return False, f"Invalid {param_name}: contains prohibited characters"

    return True, ""

def sanitize_query_param(value: str, param_name: str = "parameter") -> str:
    """Validate user input; raise HTTPException on failure. Returns value if valid."""
    is_valid, error_msg = validate_query_param(value, param_name)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    return value


def _mask_email(email: str | None) -> str:
    """Mask email for logs so no full address is emitted."""
    if not email:
        return "unknown"
    parts = email.split("@", 1)
    if len(parts) != 2:
        return "***"
    local, domain = parts
    if len(local) <= 2:
        masked_local = f"{local[:1]}***"
    else:
        masked_local = f"{local[:2]}***{local[-1:]}"
    return f"{masked_local}@{domain}"


def _hash_token(value: str | None) -> str:
    """Stable short hash for IDs/tokens in logs."""
    if not value:
        return "none"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

async def _identifier(request: Request) -> str:
    uid = request.session.get("user_uid", "")
    if not uid or len(uid) < 36:
        uid = str(uuid.uuid4())
        request.session["user_uid"] = uid
    func_name = request.scope["route"].endpoint.__name__
    room = request.path_params.get("sid") or request.path_params.get("id") or ""
    return f"{uid}:{func_name}:{room}"


async def _otp_email_identifier(request: Request) -> str:
    """Rate-limit OTP endpoints by submitted email so an attacker cannot
    multiply their quota by opening many connections / rotating cookies."""
    try:
        body = await request.json()
        email = (body.get("email") or "").strip().lower()
    except Exception:
        email = ""
    func_name = request.scope["route"].endpoint.__name__
    if email and validate_email_format(email):
        return f"otp:{email}:{func_name}"
    return await _identifier(request)

def _user_has_realtime(user_doc: dict | None, email: str) -> bool:
    """The realtime tier rule: a site admin always qualifies, otherwise the
    user's stored `realtime_enabled` flag decides. Single source of truth for
    both the socket path (is_realtime_authorized) and the HTTP path (Identity)."""
    return _is_admin_email(email) or bool(user_doc and user_doc.get("realtime_enabled"))


async def is_realtime_authorized(session: dict, session_id: str | None = None) -> bool:
    """Check if the socket is authorized to use server-side realtime features.

    Returns True if the user logged in via email and has realtime_enabled=True.

    When the socket session has lost its email (e.g. after a reconnect that
    failed to re-verify), falls back to looking up the room's admin_email
    from MongoDB so the authoritative realtime_enabled flag is always read
    from the users collection.
    """
    email = session.get('email')

    # Fallback: derive email from the room document when the socket session
    # lost it (reconnect, worker migration, Redis TTL expiry, etc.).
    if not email and session_id:
        room = await rooms_collection.find_one({"sid": session_id}, {"admin_email": 1})
        if room:
            email = room.get("admin_email")

    if not email:
        return False

    # Admins qualify without a lookup; skip the DB round-trip for them.
    if _is_admin_email(email):
        return True
    user_doc = await users_collection.find_one({"email": email})
    return _user_has_realtime(user_doc, email)


# ---------------------------------------------------------------------------
# Identity resolution (cookie session OR API key) and permission tiers
# ---------------------------------------------------------------------------
# Every authorization decision resolves to an Identity — a user record plus the
# live-computed tier flags — regardless of whether the caller authenticated via
# the browser cookie session or an `Authorization: Bearer otl_...` API key.
# Permissions are never encoded in the credential; they are read from the user
# record here so revoking realtime/admin takes effect on the next request.

class Identity:
    __slots__ = ("email", "user_uid", "is_admin", "realtime_enabled", "api_key_prefix", "source")

    def __init__(self, user: dict, source: str):
        self.email: str = (user.get("email") or "").lower()
        self.user_uid: str | None = user.get("user_uid")
        self.is_admin: bool = _is_admin_email(self.email)
        self.realtime_enabled: bool = _user_has_realtime(user, self.email)
        self.api_key_prefix: str | None = user.get("api_key_prefix")
        self.source: str = source  # "cookie" | "api_key"

    @property
    def can_admin(self) -> bool:
        """Admin *management* authority (create users, rotate others, settings).

        Withheld from key-authenticated callers: a key stored on a broadcast
        machine must not reach management endpoints even when its owning account
        is a site admin. Realtime and room ownership still flow from `is_admin`
        regardless of source, so an admin's key keeps pushing subtitles/audio.
        """
        return self.is_admin and self.source == "cookie"

    def permissions(self) -> list[str]:
        """Ordered capability slugs for display and for API consumers."""
        perms = ["subtitle:push", "room:list"]
        if self.realtime_enabled:
            perms = ["room:create", "room:manage", "audio:push"] + perms
        if self.can_admin:
            perms = ["admin:users", "admin:accounts", "admin:settings"] + perms
        return perms


async def _user_from_api_key(api_key: str | None) -> dict | None:
    if not looks_like_api_key(api_key):
        return None
    return await users_collection.find_one({"api_key_hash": hash_api_key(api_key)})


async def get_identity(request: Request) -> Identity | None:
    """Resolve the caller to an Identity, preferring the cookie session and
    falling back to a Bearer API key. Returns None when unauthenticated."""
    email = request.session.get("email")
    user_uid = request.session.get("user_uid")
    if email and user_uid:
        user = await users_collection.find_one({"email": email.lower()})
        if user:
            return Identity(user, "cookie")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        user = await _user_from_api_key(auth_header[7:].strip())
        if user:
            return Identity(user, "api_key")
    return None


async def require_identity(request: Request) -> Identity:
    ident = await get_identity(request)
    if not ident:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return ident


async def require_realtime(request: Request) -> Identity:
    ident = await require_identity(request)
    if not ident.realtime_enabled:
        raise HTTPException(status_code=403, detail="Realtime access required")
    return ident


async def require_admin(request: Request) -> Identity:
    ident = await require_identity(request)
    if not ident.can_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return ident


async def _owns_room(email: str | None, room: dict) -> bool:
    """True if the email is the room's primary owner, a co-owner, or a site admin."""
    if not email:
        return False
    email_lc = email.lower()
    if _is_admin_email(email_lc):
        return True
    owner = await _get_room_owner_email(room)
    if owner and owner.lower() == email_lc:
        return True
    return email_lc in _get_room_co_owner_emails(room)


async def verify_socket_auth(socket_id: str, session_id: str, secret_key: str) -> bool:
    """Verify WebSocket authentication against database. Returns True if valid."""
    if not session_id or not secret_key:
        return False
    is_valid, _ = validate_query_param(session_id, "session_id")
    if not is_valid:
        return False
    is_valid, _ = validate_query_param(secret_key, "secret_key")
    if not is_valid:
        return False
    room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
    return room is not None


async def _authorize_api_key_socket(socket_id, session, api_key, session_id) -> bool:
    """Verify an API-key socket owns `session_id`, upgrading the socket session
    on success. Re-resolved against the DB so a revoked key stops working on the
    next event. Mirrors verify_socket_auth's role for the secret_key path."""
    api_key = api_key or session.get('api_key')
    if not api_key or not session_id:
        return False
    is_valid, _ = validate_query_param(session_id, "session_id")
    if not is_valid:
        return False
    # Independent reads (user by key, room by sid) — resolve them concurrently.
    user, room = await asyncio.gather(
        _user_from_api_key(api_key),
        rooms_collection.find_one({"sid": session_id}),
    )
    if not user:
        return False
    if not room or not await _owns_room(user.get("email"), room):
        return False
    session['verified'] = True
    session['session_id'] = session_id
    session['email'] = user.get("email")
    session['api_key'] = api_key
    session['auth_via'] = 'api_key'
    # realtime_authorized is derived per-event from is_realtime_authorized(email);
    # clear any stale flag so a downgraded user is re-checked.
    session.pop('realtime_authorized', None)
    await sio.save_session(socket_id, session)
    return True


async def _check_socket_already_verified(socket_id, session, *, silent: bool = False) -> bool:
    """Guard for socket events that require a previously verified session.

    Returns True if session['verified'] is already set (in-memory, no DB hit).
    When not verified, emits an 'Unauthorized' error unless silent=True.
    """
    if session.get('verified'):
        return True
    if not silent:
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
    return False


async def _verify_socket_credentials(socket_id, session, secret_key, session_id, api_key=None) -> bool:
    """Verify an unverified socket against the DB, upgrading the session on success.

    Re-checks verified sockets against the current room secret so rotating the
    room key immediately revokes stale co-owner sockets for sync events.
    Used by handlers (e.g. `sync`) that can receive `secret_key`/`api_key` in the
    event payload and should attempt to verify on the fly rather than require a
    prior `join_session`.
    """
    # API-key sockets carry no room secret; authorize by key + room ownership.
    # (_authorize_api_key_socket does the `session['api_key']` fallback itself.)
    if api_key or session.get('auth_via') == 'api_key':
        # Verified at the connect handshake for this same room — trust the session
        # and skip the DB round-trip. The key no longer rides on every event, so
        # revocation now takes effect on reconnect (same trade-off audio_buffer_append
        # already makes by caching realtime_authorized).
        if session.get('verified') and session.get('session_id') == session_id:
            return True
        if await _authorize_api_key_socket(socket_id, session, api_key, session_id):
            return True
        session['verified'] = False
        await sio.save_session(socket_id, session)
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return False
    if session.get('verified'):
        cached_secret = session.get('secret_key') or secret_key
        if cached_secret and session_id and await verify_socket_auth(socket_id, session_id, cached_secret):
            return True
        if secret_key and secret_key != cached_secret and session_id and await verify_socket_auth(socket_id, session_id, secret_key):
            session['secret_key'] = secret_key
            await sio.save_session(socket_id, session)
            return True
        session['verified'] = False
        await sio.save_session(socket_id, session)
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return False
    if not secret_key:
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return False
    if not session_id:
        await sio.emit('error', {'message': 'Unauthorized: not in a session room'}, to=socket_id)
        return False
    if not await verify_socket_auth(socket_id, session_id, secret_key):
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return False
    session['verified'] = True
    session['secret_key'] = secret_key
    await sio.save_session(socket_id, session)
    return True



async def _verify_session_lock_holder(request: Request, sid: str):
    """Verify the request holds the room's active lock (secret_key). Used by the
    lock-lifecycle endpoints (heartbeat) where mere ownership is not enough.
    Returns the room doc."""
    user_secret_key = request.session.get("secret_key")
    if not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if room.get("secret_key") != user_secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    return room


# ---------------------------------------------------------------------------
# Email login routes
# ---------------------------------------------------------------------------

def _get_session_email(request: Request) -> str | None:
    return request.session.get("email")


def _is_admin_email(email: str) -> bool:
    return email.lower() in [e.lower() for e in EMAIL_SETTINGS.get("ADMIN_EMAILS", [])]


def _require_admin_email(request: Request):
    email = _get_session_email(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not logged in")
    if not _is_admin_email(email):
        raise HTTPException(status_code=403, detail="Admin access required")
    return email


def _require_logged_in(request: Request) -> tuple[str | None, str | None]:
    """Require the user to be logged in. Returns (email, user_uid)."""
    email = _get_session_email(request)
    user_uid = request.session.get("user_uid")
    if not email or not user_uid:
        return None, None
    return email, user_uid


async def _get_room_owner_email(room: dict) -> str | None:
    """Resolve the owner email from a room document, handling backward-compat admin_uid fallback."""
    email = room.get("admin_email")
    if not email and room.get("admin_uid"):
        doc = await users_collection.find_one({"user_uid": room["admin_uid"]})
        email = doc.get("email") if doc else None
    return email


def _get_room_co_owner_emails(room: dict) -> list[str]:
    """Return the lowercased co-owner email list for a room (empty if unset)."""
    raw = room.get("co_owner_emails") or []
    return [e.lower() for e in raw if isinstance(e, str) and e]


async def _require_room_owner(request: Request, room: dict) -> None:
    """Raise 403 if the room is owned and the current user is not the owner, a co-owner, or a site admin."""
    owner_email = await _get_room_owner_email(room)
    if not owner_email:
        return
    if not await _owns_room(_get_session_email(request), room):
        raise HTTPException(status_code=403, detail="This session is owned by another user.")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    email = _get_session_email(request)
    if email:
        target = "/dashboard" if _is_admin_email(email) else "/user-dashboard"
        return RedirectResponse(url=target, status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def dashboard(request: Request):
    _require_admin_email(request)
    users = await users_collection.find({}, {"_id": 0}).to_list(length=1000)

    # Aggregate Scribe usage per owner in one pass (rooms accumulate audio
    # duration via the heartbeat handler), keyed by lowercased admin_email.
    usage_by_email: dict[str, dict] = {}
    async for row in await rooms_collection.aggregate([
        {"$match": {"admin_email": {"$ne": None}}},
        {"$group": {
            "_id": {"$toLower": "$admin_email"},
            "total_audio_secs": {"$sum": {"$ifNull": ["$audio_duration_secs", 0]}},
            "session_count": {"$sum": 1},
        }},
    ]):
        usage_by_email[row["_id"]] = row

    # Convert datetimes to ISO strings for template rendering
    for u in users:
        _isoformat_fields(u, "created_at", "last_login_at")
        stats = usage_by_email.get((u.get("email") or "").lower(), {})
        secs = round(stats.get("total_audio_secs", 0))
        u["session_count"] = stats.get("session_count", 0)
        u["audio_display"] = f"{secs / 3600:.1f} h" if secs >= 3600 else f"{secs // 60} m"
    from .translators import AVAILABLE_PROVIDERS
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "users": users,
        "current_email": _get_session_email(request),
        "providers": AVAILABLE_PROVIDERS,
    })


@app.get("/user-dashboard", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def user_dashboard(request: Request):
    email, user_uid = _require_logged_in(request)
    if not email or not user_uid:
        return RedirectResponse(url="/login", status_code=302)
    email_lc = email.lower()
    rooms = await rooms_collection.find(
        {"$or": [
            {"admin_email": email_lc},
            {"co_owner_emails": email_lc},
        ]},
        {"_id": 0, "sid": 1, "admin_email": 1, "created_at": 1, "admin_last_heartbeat": 1,
         "audio_bytes": 1, "audio_duration_secs": 1}
    ).sort("created_at", -1).to_list(length=200)
    max_audio_secs = max((r.get("audio_duration_secs") or 0 for r in rooms), default=0)
    for r in rooms:
        _isoformat_fields(r, "created_at", "admin_last_heartbeat")
        owner = r.get("admin_email")
        r["is_co_owner"] = bool(owner and owner.lower() != email_lc)
        dur = r.get("audio_duration_secs") or 0
        r["audio_pct"] = min(int(dur / max_audio_secs * 100), 100) if max_audio_secs > 0 else 0
    ident = await get_identity(request)
    is_realtime_enabled = bool(ident and ident.realtime_enabled)
    response = templates.TemplateResponse("user_dashboard.html", {
        "request": request,
        "rooms": rooms,
        "current_email": email,
        "is_realtime_enabled": is_realtime_enabled,
        "permissions": ident.permissions() if ident else [],
        "api_key_prefix": ident.api_key_prefix if ident else None,
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.post("/api/users/{email}/realtime", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def set_user_realtime(request: Request, email: str):
    """Toggle realtime_enabled for a user (admin only)."""
    await require_admin(request)
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="'enabled' must be a boolean")
    from pymongo import ReturnDocument
    result = await users_collection.find_one_and_update(
        {"email": email.lower()},
        {"$set": {"realtime_enabled": enabled}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return {"email": result["email"], "realtime_enabled": result["realtime_enabled"]}


@app.post("/api/users/{email}/settings", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def set_user_settings(request: Request, email: str):
    """Set per-account overrides (ai_provider, partial_interval) for a user (admin only).
    A null/empty value clears the override so the user falls back to config defaults."""
    await require_admin(request)
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    from .translators import AVAILABLE_PROVIDERS
    body = await request.json()

    update: dict = {"$set": {}, "$unset": {}}

    if "ai_provider" in body:
        provider = body["ai_provider"]
        if provider in (None, ""):
            update["$unset"]["ai_provider"] = ""
        elif isinstance(provider, str) and provider.lower() in AVAILABLE_PROVIDERS:
            update["$set"]["ai_provider"] = provider.lower()
        else:
            raise HTTPException(status_code=400, detail=f"'ai_provider' must be one of {AVAILABLE_PROVIDERS} or empty")

    if "partial_interval" in body:
        interval = body["partial_interval"]
        if interval in (None, ""):
            update["$unset"]["partial_interval"] = ""
        else:
            try:
                interval = float(interval)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="'partial_interval' must be a number")
            if not (0.1 <= interval <= 10):
                raise HTTPException(status_code=400, detail="'partial_interval' must be between 0.1 and 10")
            update["$set"]["partial_interval"] = interval

    update = {op: fields for op, fields in update.items() if fields}
    if not update:
        raise HTTPException(status_code=400, detail="No settings provided")

    from pymongo import ReturnDocument
    result = await users_collection.find_one_and_update(
        {"email": email.lower()},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="User not found")

    # Invalidate cached ai_provider for this user's active sessions so the new
    # provider takes effect without waiting for the 24h Redis TTL.
    if "ai_provider" in body:
        await _invalidate_user_ai_provider_cache(email.lower())

    return {
        "email": result["email"],
        "ai_provider": result.get("ai_provider") or "",
        "partial_interval": result.get("partial_interval"),
    }


async def _invalidate_user_ai_provider_cache(email_lc: str) -> None:
    """Delete the cached ai_provider Redis keys for every room this user owns."""
    try:
        rooms = await rooms_collection.find(
            {"admin_email": email_lc}, {"_id": 0, "sid": 1}
        ).to_list(length=1000)
        keys = [f"ai_provider:{r['sid']}" for r in rooms]
        if keys:
            await redis_client.delete(*keys)
    except Exception as e:
        log_exception(logger, e, "Failed to invalidate ai_provider cache")


# ---------------------------------------------------------------------------
# API key management (one key per user; hash stored, plaintext shown once)
# ---------------------------------------------------------------------------
@app.post("/api/apikey", dependencies=[Depends(RateLimiter(times=20, seconds=60, identifier=_identifier))])
async def create_api_key(request: Request):
    """Generate (or rotate) the caller's API key. Returns the plaintext once —
    it is never retrievable again. Rotating invalidates the previous key."""
    ident = await require_identity(request)
    plaintext, key_hash, prefix = generate_api_key()
    result = await users_collection.find_one_and_update(
        {"email": ident.email},
        {"$set": {
            "api_key_hash": key_hash,
            "api_key_prefix": prefix,
            "api_key_created_at": datetime.now(timezone.utc),
        }},
    )
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("api_key_created email=%s", _mask_email(ident.email))
    # Report what the *key* can do (no admin management), not the cookie caller.
    return {"api_key": plaintext, "prefix": prefix, "permissions": Identity(result, "api_key").permissions()}


@app.delete("/api/apikey", dependencies=[Depends(RateLimiter(times=20, seconds=60, identifier=_identifier))])
async def revoke_api_key(request: Request):
    """Revoke the caller's API key, if any."""
    ident = await require_identity(request)
    await users_collection.update_one(
        {"email": ident.email},
        {"$unset": {"api_key_hash": "", "api_key_prefix": "", "api_key_created_at": ""}},
    )
    logger.info("api_key_revoked email=%s", _mask_email(ident.email))
    return {"status": "revoked"}


@app.get("/api/me", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_me(request: Request):
    """Return the caller's identity, permission tiers, and API-key metadata."""
    ident = await require_identity(request)
    return {
        "email": ident.email,
        "is_admin": ident.is_admin,
        "realtime_enabled": ident.realtime_enabled,
        "permissions": ident.permissions(),
        "api_key_prefix": ident.api_key_prefix,
    }


# ---------------------------------------------------------------------------
# Room management via API key / cookie identity
# ---------------------------------------------------------------------------
def _isoformat_fields(doc: dict, *keys: str) -> None:
    """In place, convert the named datetime fields on a Mongo doc to ISO strings."""
    for k in keys:
        if isinstance(doc.get(k), datetime):
            doc[k] = doc[k].isoformat()


@app.post("/api/rooms", dependencies=[Depends(RateLimiter(times=60, seconds=60, identifier=_identifier))])
async def create_room(request: Request):
    """Create a room owned by the caller (realtime access required). An optional
    `sid` may be supplied; otherwise one is generated. Idempotent for a sid the
    caller already owns."""
    ident = await require_realtime(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    requested_sid = body.get("sid") if isinstance(body, dict) else None

    now = datetime.now(timezone.utc)
    base_doc = {
        "secret_key": None,
        "admin_uid": ident.user_uid,
        "admin_email": ident.email,
        "admin_last_heartbeat": None,
        "created_at": now,
        "updated_at": now,
        "co_owner_emails": [],
        "extra": {},
    }

    if requested_sid:
        sid = sanitize_query_param(str(requested_sid), "session ID")
        existing = await rooms_collection.find_one({"sid": sid})
        if existing:
            if await _owns_room(ident.email, existing):
                return {"sid": sid, "existed": True}
            raise HTTPException(status_code=409, detail="Room already owned by another user")
        try:
            await rooms_collection.insert_one({"sid": sid, **base_doc})
        except Exception:
            raise HTTPException(status_code=409, detail="Room already exists")
        return {"sid": sid, "existed": False}

    # Auto-generate a unique sid, retrying on the (rare) unique-index collision.
    for _ in range(5):
        sid = secrets.token_urlsafe(9)
        try:
            await rooms_collection.insert_one({"sid": sid, **base_doc})
            return {"sid": sid, "existed": False}
        except Exception:
            continue
    raise HTTPException(status_code=500, detail="Could not allocate a room id")


@app.get("/api/rooms", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def list_rooms(request: Request):
    """List rooms the caller owns or co-owns (admins see all)."""
    ident = await require_identity(request)
    if ident.can_admin:
        query: dict = {}
    else:
        query = {"$or": [{"admin_email": ident.email}, {"co_owner_emails": ident.email}]}
    rooms = await rooms_collection.find(
        query,
        {"_id": 0, "sid": 1, "admin_email": 1, "co_owner_emails": 1, "created_at": 1,
         "admin_last_heartbeat": 1, "audio_duration_secs": 1},
    ).sort("created_at", -1).to_list(length=500)
    for r in rooms:
        _isoformat_fields(r, "created_at", "admin_last_heartbeat")
        owner = r.get("admin_email")
        r["is_owner"] = bool(owner and owner.lower() == ident.email)
    return {"rooms": rooms}


# ---------------------------------------------------------------------------
# Admin: account & user management via API key / cookie identity
# ---------------------------------------------------------------------------
@app.post("/api/users", dependencies=[Depends(RateLimiter(times=60, seconds=60, identifier=_identifier))])
async def admin_create_user(request: Request):
    """Create an account for an email if it does not exist (admin only). The
    account activates when the user next logs in via OTP; realtime access can be
    granted immediately with `realtime_enabled`."""
    await require_admin(request)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    realtime = bool(body.get("realtime_enabled", False))
    now = datetime.now(timezone.utc)
    from pymongo import ReturnDocument
    result = await users_collection.find_one_and_update(
        {"email": email},
        {"$setOnInsert": {
            "email": email,
            "user_uid": str(uuid.uuid4()),
            "realtime_enabled": realtime,
            "created_at": now,
        }},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return {
        "email": result["email"],
        "realtime_enabled": bool(result.get("realtime_enabled")),
        # created_at equals `now` only on the insert branch of the upsert.
        "created": result.get("created_at") == now,
    }


@app.get("/api/users", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def admin_list_users(request: Request):
    """List all users (admin only)."""
    await require_admin(request)
    users = await users_collection.find(
        {}, {"_id": 0, "email": 1, "realtime_enabled": 1, "ai_provider": 1,
             "partial_interval": 1, "api_key_prefix": 1, "created_at": 1, "last_login_at": 1}
    ).to_list(length=1000)
    for u in users:
        _isoformat_fields(u, "created_at", "last_login_at")
        u["has_api_key"] = bool(u.pop("api_key_prefix", None))
        u["is_admin"] = _is_admin_email(u.get("email", ""))
    return {"users": users}


@app.post("/auth/send-otp", dependencies=[Depends(RateLimiter(times=10, seconds=60, identifier=_otp_email_identifier))])
async def send_otp(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    otp = generate_otp()
    logger.info("send_otp requested email=%s", _mask_email(email))
    await store_otp(redis_client, email, otp)
    try:
        await send_otp_email(email, otp, EMAIL_SETTINGS)
    except Exception as e:
        log_exception(logger, e, f"Failed to send OTP email to {_mask_email(email)}")
        raise HTTPException(status_code=500, detail="Failed to send email")
    return {"status": "sent"}


@app.post("/auth/verify-otp", dependencies=[Depends(RateLimiter(times=10, seconds=60, identifier=_otp_email_identifier))])
async def verify_otp_endpoint(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    otp = body.get("otp", "").strip()
    if not validate_email_format(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    if not otp or not re.match(r'^\d{6}$', otp):
        raise HTTPException(status_code=400, detail="Invalid OTP format")

    if not await verify_otp(redis_client, email, otp):
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    # Reuse existing user_uid from session or generate a new one. The DB will
    # only persist this on first creation; subsequent logins keep the existing
    # value so all devices for the same email share one canonical user_uid.
    user_uid = request.session.get("user_uid") or str(uuid.uuid4())
    user_doc = await get_or_create_user(users_collection, email, user_uid)
    user_uid = user_doc.get("user_uid", user_uid)

    request.session["email"] = email.lower()
    request.session["user_uid"] = user_uid

    is_admin = _is_admin_email(email)
    return {"status": "ok", "is_admin": is_admin, "redirect": "/dashboard" if is_admin else "/user-dashboard"}


@app.get("/api/session/{sid}/languages", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_languages_endpoint(request: Request, sid: str):
    """Get the current translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    from .translation_service import get_session_languages
    languages = await get_session_languages(redis_client, sid)
    return {"languages": languages}


@app.post("/api/session/{sid}/languages", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def update_session_languages_endpoint(request: Request, sid: str):
    """Update the translate languages for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    body = await request.json()
    languages = body.get("languages")
    if not isinstance(languages, list) or not languages:
        raise HTTPException(status_code=400, detail="languages must be a non-empty list")
    for lang in languages:
        if not isinstance(lang, str) or not lang.strip():
            raise HTTPException(status_code=400, detail="Each language must be a non-empty string")
        if '$' in lang or len(lang) > 32:
            raise HTTPException(status_code=400, detail=f"Invalid language value: {lang}")
    languages = [lang.strip() for lang in languages]

    from .translation_service import save_session_languages
    await save_session_languages(redis_client, sid, languages)
    await _emit_session_settings_update(sid, "languages")
    return {"languages": languages}


@app.get("/api/session/{sid}/keywords", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_keywords_endpoint(request: Request, sid: str):
    """Get the current keywords and locked keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    from .translation_service import get_keywords_and_locked
    keywords, locked_keywords = await get_keywords_and_locked(redis_client, sid)
    sorted_keywords = sorted(keywords, key=lambda k: keywords[k], reverse=True)
    return {"keywords": sorted_keywords, "locked_keywords": locked_keywords}


@app.post("/api/session/{sid}/keywords", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def update_session_keywords_endpoint(request: Request, sid: str):
    """Update the keywords and locked keywords for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    body = await request.json()
    keywords = body.get("keywords")
    if not isinstance(keywords, list):
        raise HTTPException(status_code=400, detail="keywords must be a list")
    for kw in keywords:
        if not isinstance(kw, str) or not kw.strip():
            raise HTTPException(status_code=400, detail="Each keyword must be a non-empty string")
        if '$' in kw or len(kw) > 128:
            raise HTTPException(status_code=400, detail=f"Invalid keyword value: {kw}")
    keywords = [kw.strip() for kw in keywords]

    from .translation_service import save_current_keywords, save_locked_keywords, get_keywords_and_locked
    existing_keywords, _ = await get_keywords_and_locked(redis_client, sid)
    keywords_dict = {kw: existing_keywords.get(kw, 1) for kw in keywords}
    await save_current_keywords(redis_client, sid, keywords_dict)

    if "locked_keywords" in body:
        locked_keywords = body.get("locked_keywords")
        if not isinstance(locked_keywords, list):
            raise HTTPException(status_code=400, detail="locked_keywords must be a list")
        for kw in locked_keywords:
            if not isinstance(kw, str) or not kw.strip():
                raise HTTPException(status_code=400, detail="Each locked keyword must be a non-empty string")
            if '$' in kw or len(kw) > 128:
                raise HTTPException(status_code=400, detail=f"Invalid locked keyword value: {kw}")
        locked_keywords = [kw.strip() for kw in locked_keywords]
        await save_locked_keywords(redis_client, sid, locked_keywords)
    else:
        locked_keywords = None

    result = {"keywords": list(keywords_dict.keys())}
    if locked_keywords is not None:
        result["locked_keywords"] = locked_keywords
    await _emit_session_settings_update(sid, "keywords")
    return result


@app.get("/api/session/{sid}/text-dictionary", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_text_dictionary_endpoint(request: Request, sid: str):
    """Get the user-defined text replacement dictionary for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    from .translation_service import get_text_dictionary
    mapping = await get_text_dictionary(redis_client, sid)
    return {"text_dictionary": mapping}


@app.post("/api/session/{sid}/text-dictionary", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def update_session_text_dictionary_endpoint(request: Request, sid: str):
    """Update the user-defined text replacement dictionary for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    body = await request.json()
    # normalize_text_dictionary owns format migration (legacy {from: to} dict ->
    # rule list) and shape validation; the endpoint only enforces HTTP caps.
    from .translation_service import normalize_text_dictionary, save_text_dictionary
    rules = normalize_text_dictionary(body.get("text_dictionary"))
    if len(rules) > 200:
        raise HTTPException(status_code=400, detail="text_dictionary too large (max 200 entries)")

    cleaned: list[dict[str, str]] = []
    for r in rules:
        r["from"] = r["from"].strip()
        if not r["from"]:
            continue
        if len(r["from"]) > 200 or len(r["to"]) > 200:
            raise HTTPException(status_code=400, detail="text_dictionary entries too long (max 200 chars)")
        cleaned.append(r)

    await save_text_dictionary(redis_client, sid, cleaned)
    await _emit_session_settings_update(sid, "text-dictionary")
    return {"text_dictionary": cleaned}


@app.get("/api/session/{sid}/display-dictionary", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_display_dictionary_endpoint(sid: str):
    """Public per-language replacement maps for viewers (rt.html) to apply at
    render time, so rules added mid-session also reflow already-displayed lines.

    Only language-target rules are exposed; flow (source) rules never reach the
    viewer since rt.html renders translated text only. No admin auth: this is the
    public viewer surface, same as /stream and /rt.
    """
    sid = sanitize_query_param(sid, "session ID")
    from .translation_service import get_language_maps
    return {"language_maps": await get_language_maps(redis_client, sid)}


@app.get("/api/session/{sid}/scribe-language", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_scribe_language_endpoint(request: Request, sid: str):
    """Get the forced detect language for Scribe (empty means auto-detect)."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    from .translation_service import get_session_scribe_language
    language = await get_session_scribe_language(redis_client, sid)
    return {"language": language}


@app.post("/api/session/{sid}/scribe-language", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def update_session_scribe_language_endpoint(request: Request, sid: str):
    """Set or clear the forced detect language for Scribe."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    body = await request.json()
    language = body.get("language", "")
    if not isinstance(language, str):
        raise HTTPException(status_code=400, detail="language must be a string")
    language = language.strip().lower()
    if language and not re.fullmatch(r'[a-z]{2,3}', language):
        raise HTTPException(status_code=400, detail="language must be an ISO 639-1 (2-char) or ISO 639-3 (3-char) code")

    from .translation_service import save_session_scribe_language
    await save_session_scribe_language(redis_client, sid, language)

    # Restart the active scribe manager so the new language takes effect immediately.
    if active_scribe_managers.get(sid):
        await _get_or_create_scribe_manager(sid, force_new=True)

    await _emit_session_settings_update(sid, "scribe-language")
    return {"language": language}


@app.get("/api/session/{sid}/translate-tone", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_translate_tone_endpoint(request: Request, sid: str):
    """Get the translation tone for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    from .translation_service import get_session_translate_tone
    tone = await get_session_translate_tone(redis_client, sid)
    return {"tone": tone}


@app.post("/api/session/{sid}/translate-tone", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def update_session_translate_tone_endpoint(request: Request, sid: str):
    """Set the translation tone for a session."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    body = await request.json()
    tone = body.get("tone", "")
    if not isinstance(tone, str):
        raise HTTPException(status_code=400, detail="tone must be a string")
    tone = tone.strip()
    if tone and not re.fullmatch(r'[\w\s\-]{1,64}', tone, re.UNICODE):
        raise HTTPException(status_code=400, detail="Tone must be 1–64 word characters, spaces, or hyphens")

    from .translation_service import save_session_translate_tone
    await save_session_translate_tone(redis_client, sid, tone)
    await _emit_session_settings_update(sid, "translate-tone")
    return {"tone": tone}


_MAX_CO_OWNERS = 20


@app.get("/api/session/{sid}/co-owners", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def get_session_co_owners_endpoint(request: Request, sid: str):
    """List co-owners for a session. Visible to the primary owner and any co-owner."""
    sid = sanitize_query_param(sid, "session ID")
    email, room = await _require_session_owner(request, sid)
    owner_email = await _get_room_owner_email(room)
    return {
        "owner": owner_email,
        "co_owners": _get_room_co_owner_emails(room),
        "is_primary_owner": bool(owner_email and owner_email.lower() == email.lower()),
    }


@app.post("/api/session/{sid}/co-owners", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def add_session_co_owner_endpoint(request: Request, sid: str):
    """Add a co-owner email to the session. Primary owner only."""
    sid = sanitize_query_param(sid, "session ID")
    _, room = await _require_session_primary_owner(request, sid)

    body = await request.json()
    raw_email = body.get("email", "")
    if not isinstance(raw_email, str):
        raise HTTPException(status_code=400, detail="email must be a string")
    new_email = raw_email.strip().lower()
    if not validate_email_format(new_email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    owner_email = await _get_room_owner_email(room)
    if owner_email and new_email == owner_email.lower():
        raise HTTPException(status_code=400, detail="This email is already the primary owner")

    existing = _get_room_co_owner_emails(room)
    if new_email in existing:
        return {"co_owners": existing}
    if len(existing) >= _MAX_CO_OWNERS:
        raise HTTPException(status_code=400, detail=f"Too many co-owners (max {_MAX_CO_OWNERS})")

    from pymongo import ReturnDocument
    updated_room = await rooms_collection.find_one_and_update(
        {
            "sid": sid,
            "co_owner_emails": {"$ne": new_email},
            "$expr": {"$lt": [{"$size": {"$ifNull": ["$co_owner_emails", []]}}, _MAX_CO_OWNERS]},
        },
        {
            "$addToSet": {"co_owner_emails": new_email},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
        projection={"co_owner_emails": 1},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_room:
        # Filter rejected the write: either the room was deleted, the cap was
        # hit by a concurrent add, or someone else already added this email.
        # Re-read to disambiguate so the message reflects the actual cause.
        latest = await rooms_collection.find_one({"sid": sid}, {"co_owner_emails": 1})
        if latest is None:
            raise HTTPException(status_code=404, detail="Session not found")
        latest_emails = _get_room_co_owner_emails(latest)
        if new_email in latest_emails:
            return {"co_owners": latest_emails}
        if len(latest_emails) >= _MAX_CO_OWNERS:
            raise HTTPException(status_code=400, detail=f"Too many co-owners (max {_MAX_CO_OWNERS})")
        raise HTTPException(status_code=409, detail="Failed to add co-owner; please retry")
    await _emit_session_settings_update(sid, "co-owners")
    return {"co_owners": _get_room_co_owner_emails(updated_room)}


@app.delete("/api/session/{sid}/co-owners/{email}", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def remove_session_co_owner_endpoint(request: Request, sid: str, email: str):
    """Remove a co-owner email from the session. Primary owner only."""
    sid = sanitize_query_param(sid, "session ID")
    _, room = await _require_session_primary_owner(request, sid)

    target = email.strip().lower()
    if not validate_email_format(target):
        raise HTTPException(status_code=400, detail="Invalid email address")

    if target not in _get_room_co_owner_emails(room):
        raise HTTPException(status_code=404, detail="Co-owner not found")

    now = datetime.now(timezone.utc)
    set_fields: dict[str, Any] = {"updated_at": now}
    rotated_secret = None
    if room.get("secret_key"):
        rotated_secret = str(uuid.uuid4())
        set_fields.update({
            "secret_key": rotated_secret,
            "admin_uid": request.session.get("user_uid"),
            "admin_last_heartbeat": now,
        })
        request.session["secret_key"] = rotated_secret

    from pymongo import ReturnDocument
    updated_room = await rooms_collection.find_one_and_update(
        {"sid": sid, "co_owner_emails": target},
        {"$pull": {"co_owner_emails": target}, "$set": set_fields},
        projection={"co_owner_emails": 1},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_room:
        raise HTTPException(status_code=404, detail="Co-owner not found")

    response = {"co_owners": _get_room_co_owner_emails(updated_room)}
    if rotated_secret:
        response["secret_key"] = rotated_secret
    await _emit_session_settings_update(sid, "co-owners")
    return response


async def _require_session_owner(request: Request, sid: str) -> tuple[str, dict]:
    """Owner gate for editor/settings endpoints. Allows the primary owner, any
    co-owner, or a site admin, authenticated via cookie session or API key.
    Returns (email, room) or raises."""
    ident, room = await asyncio.gather(
        get_identity(request),
        rooms_collection.find_one({"sid": sid}),
    )
    if not ident:
        raise HTTPException(status_code=401, detail="Not logged in")
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await _owns_room(ident.email, room):
        raise HTTPException(status_code=403, detail="You do not own this session")
    return ident.email, room


async def _require_session_primary_owner(request: Request, sid: str) -> tuple[str, dict]:
    """Strict gate that allows only the primary owner. Returns (email, room)."""
    email, room = await _require_session_owner(request, sid)
    owner_email = await _get_room_owner_email(room)
    if not owner_email or owner_email.lower() != email.lower():
        raise HTTPException(status_code=403, detail="Only the session owner can perform this action.")
    return email, room


@app.get("/edit/{sid}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def edit_transcriptions_page(request: Request, sid: str):
    """Owner-only editor: load saved segments and let the owner correct translations."""
    sid = sanitize_query_param(sid, "session ID")
    if not _get_session_email(request):
        return RedirectResponse(url="/login", status_code=302)
    email, _ = await _require_session_owner(request, sid)
    segments, _ = await _load_segments_from_db(sid, limit=10000)
    languages = _collect_srt_languages(segments)
    response = templates.TemplateResponse("edit_transcriptions.html", {
        "request": request,
        "sid": sid,
        "segments": segments,
        "languages": languages,
        "current_email": email,
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.put("/api/session/{sid}/segments", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def update_session_segment_endpoint(request: Request, sid: str):
    """Update a saved committed segment's corrected text and/or per-language translations.
    Identified by start_time. Owner-only. Refreshes the Redis cache so future viewers
    see the edit; currently-connected viewers must reload."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    body = await request.json()
    start_time = body.get("start_time")
    if not isinstance(start_time, (int, float)) or isinstance(start_time, bool):
        raise HTTPException(status_code=400, detail="start_time must be a number")

    corrected = body.get("corrected")
    if corrected is not None:
        if not isinstance(corrected, str) or len(corrected) > 5000:
            raise HTTPException(status_code=400, detail="Invalid corrected text")

    translated = body.get("translated")
    if translated is not None:
        if not isinstance(translated, dict):
            raise HTTPException(status_code=400, detail="translated must be an object")
        for k, v in translated.items():
            if not isinstance(k, str) or not k.strip() or '$' in k or '.' in k or len(k) > 32:
                raise HTTPException(status_code=400, detail=f"Invalid language code: {k}")
            if not isinstance(v, str) or len(v) > 5000:
                raise HTTPException(status_code=400, detail=f"Invalid translation for {k}")

    if corrected is None and translated is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    seg = await transcription_segments_collection.find_one(
        {"sid": sid, "start_time": start_time, "partial": {"$ne": True}}
    )
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    set_fields: dict = {}
    if corrected is not None:
        set_fields["result.corrected"] = corrected
    merged_translated = None
    if translated is not None:
        existing = (seg.get("result") or {}).get("translated") or {}
        merged_translated = {**existing, **translated}
        set_fields["result.translated"] = merged_translated

    await transcription_segments_collection.update_one(
        {"_id": seg["_id"]},
        {"$set": set_fields}
    )

    # Build the segment shape that mirrors what's stored in Redis (no _id/sid/created_at).
    new_seg = {k: v for k, v in seg.items() if k not in {"_id", "sid", "created_at"}}
    new_seg.setdefault("result", {})
    if corrected is not None:
        new_seg["result"]["corrected"] = corrected
    if merged_translated is not None:
        new_seg["result"]["translated"] = merged_translated

    # Only refresh the cache if it already exists; otherwise we'd seed a partial cache
    # that's missing every other segment.
    zset_key = f"transcription:{sid}:list"
    if await redis_client.exists(zset_key):
        pipe = redis_client.pipeline()
        pipe.zremrangebyscore(zset_key, start_time, start_time)
        pipe.zadd(zset_key, {json.dumps(new_seg): start_time})
        pipe.expire(zset_key, 3600)
        await pipe.execute()

    return {"status": "ok", "segment": new_seg}


@app.delete("/api/session/{sid}/segments", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def delete_session_segment_endpoint(request: Request, sid: str, start_time: float):
    """Delete a saved committed segment identified by start_time. Owner-only.
    Also removes the segment from the Redis cache if present."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_owner(request, sid)

    result = await transcription_segments_collection.delete_one(
        {"sid": sid, "start_time": start_time, "partial": {"$ne": True}}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Segment not found")

    zset_key = f"transcription:{sid}:list"
    if await redis_client.exists(zset_key):
        await redis_client.zremrangebyscore(zset_key, start_time, start_time)

    return {"status": "deleted"}


async def get_youtube_start_time(video_id: str) -> float | None:
    """
    Get the actual stream start time for a YouTube video using YouTube Data API v3.
    Returns the actualStartTime if available, otherwise None.
    """
    data = None
    if video_id in youtube_data_cache and youtube_data_cache[video_id] is not None:
        data = youtube_data_cache[video_id]
    elif video_id in youtube_data_cache: # Negative cache
        return None
    else:
        api_key = SETTINGS["YOUTUBE_API_KEY"]
        if not api_key:
            logger.warning("YOUTUBE_API_KEY environment variable not set")
            return None
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            'part': 'liveStreamingDetails',
            'id': video_id,
            'key': api_key
        }
        try:
            from .http_client import get_async_client
            client = get_async_client()
            response = await client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            
            data = response.json()
            
            if 'items' in data and len(data['items']) > 0:
                data = data['items'][0]
                youtube_data_cache[video_id] = data
            else:
                youtube_data_cache[video_id] = None # negative cache

        except Exception as e:
            log_exception(logger, e, "Error fetching YouTube data")
            return None
    
    if data and 'liveStreamingDetails' in data:
        live_details = data['liveStreamingDetails']
        # Check for actualStartTime (when stream actually started)
        if 'actualStartTime' in live_details:
            return datetime.fromisoformat(live_details['actualStartTime']).timestamp()
        # Fallback to scheduledStartTime if actualStartTime is not available
        elif 'scheduledStartTime' in live_details:
            return datetime.fromisoformat(live_details['scheduledStartTime']).timestamp()
    return None

async def get_cached_transcription(id, num_committed: int = 10) -> Any:
    # Try fetching committed transcriptions (ZSET) and partial from Redis
    try:
        # One pipeline = one round trip: fetch all keys and refresh sliding TTL together.
        # EXPIRE on missing keys is a harmless no-op, so we issue them unconditionally.
        pipe = redis_client.pipeline()
        pipe.zrevrangebyscore(
            f"transcription:{id}:list", "+inf", "-inf", start=0, num=num_committed
        )
        pipe.get(f"transcription:{id}:meta")
        pipe.get(f"transcription:{id}:partial")
        pipe.expire(f"transcription:{id}:list", TRANSCRIPTION_TTL)
        pipe.expire(f"transcription:{id}:meta", TRANSCRIPTION_TTL)
        pipe.expire(f"transcription:{id}:partial", TRANSCRIPTION_TTL)
        committed_json_list, meta_json, partial_json, *_ = await pipe.execute()
        committed_json_list = list(reversed(committed_json_list))

        data = None
        if committed_json_list:
            data = {
                "transcriptions": [json.loads(j) for j in committed_json_list],
                "stream_start_time": None
            }
            if meta_json:
                meta = json.loads(meta_json)
                data["stream_start_time"] = meta.get("stream_start_time")

        # Migration/Fallback: Check if old String-style cache exists
        if data is None:
            old_committed_json = await redis_client.get(f"transcription:{id}")
            if old_committed_json:
                data = json.loads(old_committed_json)
                # Migrate to ZSET in background
                asyncio.create_task(migrate_to_zset(id, data))

        # Final fallback to DB if no data in Redis
        if data is None:
            segments, store = await _load_segments_from_db(id, limit=1000)
            if segments:
                data = {
                    "transcriptions": segments,
                    "stream_start_time": store.get("stream_start_time") if store else None
                }
                asyncio.create_task(migrate_to_zset(id, data))
            else:
                data = {"transcriptions": []}

        # Merge partial data if exists
        if partial_json:
            data["partial"] = json.loads(partial_json)

        return data
    except Exception as e:
        log_exception(logger, e, "Redis/DB error in get_cached_transcription")
        return {"transcriptions": []}

async def migrate_to_zset(id, data):
    """Helper to migrate old list storage to Redis ZSET and Meta keys"""
    try:
        transcriptions = data.get("transcriptions")
        if not transcriptions:
            return

        # _load_segments_from_db sorts ascending by start_time; keep only the most
        # recent N so we never grow the ZSET past its cap. Long sessions (multi-day
        # conferences with multi-language translations) would otherwise blow
        # maxmemory mid-pipeline, before the trailing zremrangebyrank could run.
        recent = transcriptions[-TRANSCRIPTION_ZSET_MAX:]

        zset_key = f"transcription:{id}:list"
        pipe = redis_client.pipeline()
        pipe.zadd(zset_key, {json.dumps(seg): seg["start_time"] for seg in recent})
        pipe.setex(
            f"transcription:{id}:meta",
            TRANSCRIPTION_TTL,
            json.dumps({"stream_start_time": data.get("stream_start_time")}),
        )
        pipe.expire(zset_key, TRANSCRIPTION_TTL)
        pipe.delete(f"transcription:{id}")
        await pipe.execute()
    except Exception as e:
        log_exception(logger, e, f"Migration error for {id}")


async def _load_segments_from_db(sid: str, limit: int | None = None) -> tuple[list, dict | None]:
    """Fetch committed segments and session metadata from DB in parallel.
    Falls back to the legacy transcription_store embedded array if the segments
    collection has no data (for sessions written before the migration).
    """
    query = transcription_segments_collection.find(
        {"sid": sid, "partial": {"$ne": True}},
        {"_id": 0, "sid": 0, "created_at": 0}
    ).sort("start_time", 1)
    if limit:
        query = query.limit(limit)
    segments, store = await asyncio.gather(
        query.to_list(length=limit),
        transcription_store_collection.find_one({"sid": sid})
    )
    if not segments and store and store.get("transcriptions"):
        segments = store.get("transcriptions", [])
    return segments, store


def _seconds_to_srt_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds % 1) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _collect_srt_languages(segments: list) -> list[str]:
    seen: dict[str, None] = {}
    for seg in segments:
        translated = ((seg.get("result") or {}).get("translated") or {})
        for lang in translated.keys():
            if lang and lang not in seen:
                seen[lang] = None
    return list(seen.keys())


def _build_srt_for_language(segments: list, lang: str) -> str:
    if not segments:
        return ""
    epoch_offset = segments[0].get("start_time", 0) or 0
    parts: list[str] = []
    idx = 0
    for seg in segments:
        translated = ((seg.get("result") or {}).get("translated") or {})
        text = translated.get(lang)
        if not text:
            continue
        start = (seg.get("start_time") or 0) - epoch_offset
        end = max((seg.get("end_time") or seg.get("start_time") or 0), start + 0.5) - epoch_offset
        idx += 1
        parts.append(str(idx))
        parts.append(f"{_seconds_to_srt_timestamp(start)} --> {_seconds_to_srt_timestamp(end)}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts) if idx > 0 else ""


async def _save_segment_to_mongo(sid, segment, stream_start_time):
    """Save one committed segment and refresh session metadata in MongoDB."""
    now = datetime.now(timezone.utc)
    await transcription_segments_collection.insert_one({**segment, "sid": sid, "created_at": now})
    await transcription_store_collection.update_one(
        {"sid": sid},
        {"$set": {"stream_start_time": stream_start_time, "updated_at": now}},
        upsert=True
    )


# FastAPI Routes
@app.get("/", response_class=HTMLResponse)
async def hello_world(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "email": _get_session_email(request)})

@app.get("/download/{id}", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def download(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    room, (segments, meta) = await asyncio.gather(
        rooms_collection.find_one({"sid": id}, {"admin_email": 1, "admin_uid": 1, "co_owner_emails": 1}),
        _load_segments_from_db(id),
    )
    if room:
        await _require_room_owner(request, room)
    if not segments:
        raise HTTPException(status_code=404, detail="Session not found")

    updated_at = meta.get("updated_at") if meta else None
    data = {
        "sid": id,
        "transcriptions": segments,
        "stream_start_time": meta.get("stream_start_time") if meta else None,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else None,
    }

    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json")

@app.get("/download/{id}/srt/{lang}", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def download_srt(request: Request, id: str, lang: str):
    id = sanitize_query_param(id, "session ID")
    if not re.match(r'^[a-zA-Z0-9_-]{1,32}$', lang):
        raise HTTPException(status_code=400, detail="Invalid language code")

    room, (segments, _) = await asyncio.gather(
        rooms_collection.find_one({"sid": id}, {"admin_email": 1, "admin_uid": 1, "co_owner_emails": 1}),
        _load_segments_from_db(id),
    )
    if room:
        await _require_room_owner(request, room)
    if not segments:
        raise HTTPException(status_code=404, detail="Session not found")

    content = _build_srt_for_language(segments, lang)
    if not content:
        raise HTTPException(status_code=404, detail=f"No transcripts for language '{lang}'")

    filename = f"{id}.{lang}.srt"
    return Response(
        content=content,
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/api/session/{sid}/stream")
async def session_stream(request: Request, sid: str):
    sid = sanitize_query_param(sid, "session ID")
    last_event_id_raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    last_event_id = None
    if last_event_id_raw:
        try:
            last_event_id = float(last_event_id_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")

    return StreamingResponse(
        _session_sse_stream(request, sid, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/yt/{id}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def yt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    # Note: This might overwrite stream_start_time in the display data, but not cache
    data["stream_start_time"] = await get_youtube_start_time(id) 
    return templates.TemplateResponse("yt.html", {"request": request, "id": id, "data": data})

@app.get("/rt/{id}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def rt(request: Request, id: str):
    # Sanitize id parameter to prevent NoSQL injection
    id = sanitize_query_param(id, "session ID")

    data = await get_cached_transcription(id)
    if not data:
        data = {
            "stream_start_time": None,
            "transcriptions": []
        }
    sliced_data = data.copy()
    sliced_data["transcriptions"] = sliced_data["transcriptions"][-50:]
    # Per-language replacement maps applied client-side at render time so rules
    # added mid-session also reflow lines already on screen (see rt.html).
    from .translation_service import get_language_maps
    language_maps = await get_language_maps(redis_client, id)
    logger.debug(
        "rt_view sid_hash=%s segment_count=%s",
        _hash_token(id),
        len(sliced_data["transcriptions"]),
    )
    return templates.TemplateResponse(
        "rt.html",
        {"request": request, "id": id, "data": sliced_data, "language_maps": language_maps},
    )
  
@app.get("/panel/{sid}", response_class=HTMLResponse, dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def panel(request: Request, sid: str):
    # Sanitize sid parameter to prevent NoSQL injection
    sid = sanitize_query_param(sid, "session ID")

    user_uid = request.session.get("user_uid") or str(uuid.uuid4())
    request.session["user_uid"] = user_uid

    now = datetime.now(timezone.utc)
    ADMIN_TIMEOUT = 30

    # Find or create the room
    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        await rooms_collection.insert_one({
            "sid": sid,
            "secret_key": None,
            "admin_uid": None,
            "admin_email": None,
            "admin_last_heartbeat": None,
            "created_at": now,
            "extra": {}
        })
        room = {"sid": sid, "secret_key": None, "admin_uid": None, "admin_email": None, "admin_last_heartbeat": None}

    admin_uid = room.get("admin_uid")
    admin_key = room.get("secret_key")

    await _require_room_owner(request, room)

    if admin_uid and admin_key:
        last_heartbeat = room.get("admin_last_heartbeat")
        if last_heartbeat and last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        admin_expired = not last_heartbeat or (now - last_heartbeat).total_seconds() > ADMIN_TIMEOUT

        if admin_expired:
            # Lock expired — clear the lock but preserve ownership.
            await rooms_collection.update_one(
                {"sid": sid},
                {"$set": {"secret_key": None, "admin_last_heartbeat": None, "updated_at": now}}
            )
            admin_key = None
        else:
            # Active lock — caller is already authorized as owner/co-owner above.
            # Share the existing secret_key with this caller so they can call
            # settings APIs and stay in sync with the active admin. The lock
            # itself stays single (admin_uid is unchanged); both users sharing
            # the key just means either can heartbeat.
            if request.session.get("secret_key") != admin_key:
                request.session["secret_key"] = admin_key
            await rooms_collection.update_one(
                {"sid": sid},
                {"$set": {"admin_last_heartbeat": now, "updated_at": now}}
            )

    current_email = _get_session_email(request)

    if not admin_uid or not admin_key:
        session_secret_key = str(uuid.uuid4())
        # Only seed admin_email when the room has no primary owner yet — a
        # co-owner reclaiming an expired lock must NOT overwrite the owner.
        update_fields: dict = {
            "secret_key": session_secret_key,
            "admin_uid": user_uid,
            "admin_last_heartbeat": now,
            "updated_at": now,
        }
        if not room.get("admin_email"):
            update_fields["admin_email"] = current_email
        await rooms_collection.update_one({"sid": sid}, {"$set": update_fields})
        request.session["secret_key"] = session_secret_key
        user_secret_key = session_secret_key
        # Reflect the in-memory room dict so the template sees the updated owner.
        room.update(update_fields)
    else:
        user_secret_key = request.session.get("secret_key")

    owner_email = await _get_room_owner_email(room)
    is_primary_owner = bool(
        owner_email and current_email and current_email.lower() == owner_email.lower()
    )
    is_realtime_enabled = await is_realtime_authorized(request.session)
    response = templates.TemplateResponse("panel.html", {
        "request": request,
        "sid": sid,
        "user_secret_key": user_secret_key,
        "is_realtime_enabled": is_realtime_enabled,
        "email": current_email,
        "is_primary_owner": is_primary_owner,
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@app.post("/heartbeat/{sid}", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def heartbeat(request: Request, sid: str):
    """Update admin heartbeat to maintain session lock"""
    sid = sanitize_query_param(sid, "session ID")
    # Heartbeat refreshes the single-holder lock, so it stays gated on the
    # lock's secret_key (not mere ownership) — only the active panel holds it.
    await _verify_session_lock_holder(request, sid)
    now = datetime.now(timezone.utc)
    update = {"admin_last_heartbeat": now, "updated_at": now}
    viewer_count = await _viewer_presence_op(sid, label="viewer count heartbeat")
    response: dict = {"status": "ok", "viewer_count": viewer_count}
    scribe_manager = active_scribe_managers.get(sid)
    if scribe_manager and scribe_manager.audio_bytes_total > 0:
        stats = scribe_manager.get_usage_stats()
        audio_fields = {
            "audio_bytes": stats["audio_bytes"],
            "audio_duration_secs": stats["audio_duration_secs"],
            "audio_chunks": stats["audio_chunks"],
        }
        update.update(audio_fields)
        response.update(audio_fields)
    if scribe_manager and scribe_manager.is_running:
        # Refresh the TTL so an active session is never evicted mid-recording.
        active_scribe_managers[sid] = scribe_manager
    translation_manager = active_translation_managers.get(sid)
    if translation_manager:
        # Refresh translation manager TTL alongside scribe manager to prevent
        # the 60s default expiry from evicting it mid-commit and silently
        # dropping committed translations.
        active_translation_managers[sid] = translation_manager
    await rooms_collection.update_one({"sid": sid}, {"$set": update})
    return response

@app.post("/release-admin/{sid}", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def release_admin(request: Request, sid: str):
    """Release admin lock when admin leaves"""
    sid = sanitize_query_param(sid, "session ID")

    user_secret_key = request.session.get("secret_key")
    if not user_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room = await rooms_collection.find_one({"sid": sid})
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")

    if room.get("secret_key") != user_secret_key:
        return {"status": "not_admin"}

    if room.get("admin_uid") != request.session.get("user_uid"):
        return {"status": "not_lock_holder"}

    await rooms_collection.update_one(
        {"sid": sid},
        {"$set": {"secret_key": None, "admin_last_heartbeat": None, "updated_at": datetime.now(timezone.utc)}}
    )
    request.session.pop("secret_key", None)
    return {"status": "released"}

@app.delete("/api/sessions/{sid}", dependencies=[Depends(RateLimiter(times=100, seconds=10, identifier=_identifier))])
async def delete_session(request: Request, sid: str):
    """Release session ownership, removing it from the owner's My Sessions list.
    Once deleted, anyone can claim the session again. Primary owner only —
    co-owners cannot release the session out from under the owner."""
    sid = sanitize_query_param(sid, "session ID")
    await _require_session_primary_owner(request, sid)
    await rooms_collection.update_one(
        {"sid": sid},
        {"$set": {
            "admin_uid": None,
            "admin_email": None,
            "secret_key": None,
            "admin_last_heartbeat": None,
            "co_owner_emails": [],
            "updated_at": datetime.now(timezone.utc),
        }}
    )
    return {"status": "deleted"}

# Socket.IO Event Handlers
async def _auto_rejoin_from_auth(socket_id, auth) -> dict:
    """Pre-verify a panel socket from the `auth` payload supplied to `io()`.

    Returns the initial session dict to save. When the auth payload carries a
    valid (session_id, secret_key), the socket is marked verified and joined
    to its room immediately, so events fired before the frontend's explicit
    `join_session` round-trip don't race and see verified=False on reconnect.
    Without valid auth, returns an unverified session (viewers, fresh tabs).
    """
    session_data: dict = {'verified': False}
    if not isinstance(auth, dict):
        return session_data
    session_id = auth.get('session_id')
    api_key = auth.get('api_key')
    secret_key = auth.get('secret_key')

    # API-key clients (external transcribe/realtime senders) may not know the
    # room yet at connect time; stash the key so join_session can authorize it.
    if looks_like_api_key(api_key):
        session_data['api_key'] = api_key
        session_data['auth_via'] = 'api_key'
        if isinstance(session_id, str) and await _authorize_api_key_socket(socket_id, session_data, api_key, session_id):
            await sio.enter_room(socket_id, session_id)
            logger.info(
                "connect auto_rejoin_apikey sid_hash=%s socket_hash=%s",
                _hash_token(session_id), _hash_token(socket_id),
            )
        return session_data

    if not isinstance(session_id, str) or not isinstance(secret_key, str):
        return session_data
    sid_ok, _ = validate_query_param(session_id, "session_id")
    key_ok, _ = validate_query_param(secret_key, "secret_key")
    if not sid_ok or not key_ok:
        return session_data
    room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
    if not room:
        logger.warning(
            "connect auto_rejoin_failed sid_hash=%s socket_hash=%s",
            _hash_token(session_id),
            _hash_token(socket_id),
        )
        return session_data
    session_data.update({
        'verified': True,
        'secret_key': secret_key,
        'session_id': session_id,
        'email': await _get_room_owner_email(room),
    })
    await sio.enter_room(socket_id, session_id)
    logger.info(
        "connect auto_rejoin sid_hash=%s socket_hash=%s",
        _hash_token(session_id),
        _hash_token(socket_id),
    )
    return session_data


@sio.event
async def connect(socket_id, environ, auth):
    """Handle client connection.

    If `auth` includes a valid {session_id, secret_key}, the socket is
    pre-verified and joined to its room before any other event can arrive.
    The frontend still emits `join_session` as the canonical handshake
    (it's idempotent and triggers the joined_session UI update).
    """
    session_data = await _auto_rejoin_from_auth(socket_id, auth)
    await sio.save_session(socket_id, session_data)
    logger.info(
        "client_connected socket_hash=%s verified=%s",
        _hash_token(socket_id),
        session_data['verified'],
    )
    await sio.emit('connected', {'status': 'connected', 'client_id': socket_id}, to=socket_id)

@sio.event
async def time_sync(socket_id, data):
    """NTP-style clock handshake.

    Latency displays subtract a server-generated end_time from the viewer's
    local clock, so any skew between the browser and the (NTP-synced) server
    leaks directly into the number. The client times its own t0/rtt locally;
    we just return the server time t1 as an ack, from which the client derives
    offset = t1 - (t0 + rtt/2) to correct its own clock.
    """
    return {"t1": time.time() * 1000}

@sio.event
async def disconnect(socket_id):
    """Handle client disconnection"""
    _socket_limiter.cleanup(socket_id)
    logger.info("client_disconnected socket_hash=%s", _hash_token(socket_id))
    # Admin lock is NOT cleared here: socket disconnect fires on page refresh
    # and transient network blips, not only on true tab-close.
    # Cleanup is handled by:
    #   1. The /release-admin HTTP beacon sent on true navigation-away (beforeunload, non-reload)
    #   2. The 30-second heartbeat timeout checked on every /panel/{sid} request

async def _process_transcription_update(session_id, sync_data):
    """Process a transcription update: cache, persist, broadcast. Hot path."""
    proc_start = time.perf_counter()
    redis_rtts = 0
    is_partial = sync_data.get("partial") is True

    list_key = f"transcription:{session_id}:list"
    meta_key = f"transcription:{session_id}:meta"
    partial_key = f"transcription:{session_id}:partial"

    manager = active_scribe_managers.get(session_id)
    yt_start_time = manager.yt_start_time if manager else await get_youtube_start_time(session_id)

    # Serialize the read-modify-write on partial_key per session so concurrent partials
    # (flow_only client vs scribe translation) and commits can't interleave across the
    # read->write await gap and stomp each other.
    async with _get_or_create_lock(_partial_rmw_locks, session_id):
        pipe = redis_client.pipeline()
        pipe.zrange(list_key, -1, -1)
        pipe.get(meta_key)
        pipe.get(partial_key)
        pipe.expire(list_key, TRANSCRIPTION_TTL)
        pipe.expire(meta_key, TRANSCRIPTION_TTL)
        try:
            last_json, meta_json, partial_json, *_ = await pipe.execute()
            redis_rtts += 1
        except Exception as e:
            log_exception(logger, e, "Redis pipeline error in _process_transcription_update (read)")
            last_json, meta_json, partial_json = [], None, None

        last_committed = json.loads(last_json[0]) if last_json else None
        stream_start_time = yt_start_time
        if not stream_start_time and meta_json:
            try:
                stream_start_time = json.loads(meta_json).get("stream_start_time")
            except (TypeError, ValueError):
                pass

        if is_partial:
            if last_committed and sync_data["start_time"] < last_committed["start_time"]:
                logger.debug(
                    "skip_older_partial sid_hash=%s incoming_start=%s last_committed_start=%s",
                    _hash_token(session_id),
                    sync_data.get("start_time"),
                    last_committed.get("start_time"),
                )
                return

            if sync_data.get("flow_only") and partial_json:
                try:
                    last_partial = json.loads(partial_json)
                    sync_data["result"]["translated"] = last_partial["result"]["translated"]
                except (KeyError, TypeError, ValueError):
                    pass

            await redis_client.setex(partial_key, TRANSCRIPTION_TTL, json.dumps(sync_data))
            redis_rtts += 1
        else:
            pipe = redis_client.pipeline()
            pipe.zadd(list_key, {json.dumps(sync_data): sync_data["start_time"]})
            pipe.zremrangebyrank(list_key, 0, -(TRANSCRIPTION_ZSET_MAX + 1))
            pipe.expire(list_key, TRANSCRIPTION_TTL)
            # Skip meta write when we have no stream_start_time so a transient read
            # failure (which zeroes stream_start_time) doesn't stomp good meta.
            if stream_start_time is not None:
                pipe.setex(meta_key, TRANSCRIPTION_TTL, json.dumps({"stream_start_time": stream_start_time}))
            pipe.delete(partial_key)
            pipe.zrange(list_key, -1, -1)
            try:
                results = await pipe.execute()
                redis_rtts += 1
                new_last_json = results[-1]
                if new_last_json:
                    last_committed = json.loads(new_last_json[0])
            except Exception as e:
                log_exception(logger, e, "Redis pipeline error in _process_transcription_update (write)")
                # sync_data shares the ZSET member shape — safe to use as last_committed.
                if not last_committed or sync_data["start_time"] >= last_committed["start_time"]:
                    last_committed = sync_data

            accepted = segment_write_queue.enqueue(session_id, sync_data, stream_start_time)
            if not accepted:
                logger.warning(
                    "segment_write_queue dropped incoming segment sid_hash=%s start=%s",
                    _hash_token(session_id),
                    sync_data.get("start_time"),
                )

    payload = sync_data.copy()
    if last_committed:
        payload["last_committed"] = last_committed

    proc_elapsed_ms = (time.perf_counter() - proc_start) * 1000

    async def _emit_now(p):
        latency = datetime.now(timezone.utc).timestamp() - sync_data['end_time']
        logger.info(
            "sync_update sid_hash=%s start=%s end=%s partial=%s latency=%.3fs redis_rtts=%s proc_ms=%.1f",
            _hash_token(session_id),
            sync_data.get("start_time"),
            sync_data.get("end_time"),
            is_partial,
            latency,
            redis_rtts,
            proc_elapsed_ms,
        )
        await sio.emit('transcription_update', p, room=session_id)
        await _publish_transcription_update(session_id, p)

    if is_partial:
        # Cancel any pending broadcast for this session and schedule a fresh one
        # after 75 ms so only the latest partial is sent when updates burst.
        existing = _partial_debounce_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()

        async def _debounced(p):
            await asyncio.sleep(0.075)
            _partial_debounce_tasks.pop(session_id, None)
            await _emit_now(p)

        _partial_debounce_tasks[session_id] = asyncio.create_task(_debounced(payload))
    else:
        # Committed segments broadcast immediately; cancel any pending partial debounce.
        existing = _partial_debounce_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()
        await _emit_now(payload)
    
@sio.event
async def sync(socket_id, data):
    """Handle WebSocket sync events"""
    if not _socket_limiter.check(socket_id, 'sync', 20, 1.0):
        return

    ok, schema_err = validate_sync_payload(data)
    if not ok:
        logger.warning("sync invalid_payload socket_hash=%s err=%s", _hash_token(socket_id), schema_err)
        await sio.emit('error', {'code': 'invalid_payload', 'message': schema_err}, to=socket_id)
        return

    session = await sio.get_session(socket_id)
    session_id = data['id']

    # Validate session_id against the stricter identifier rules (alnum/_/-).
    is_valid, error_msg = validate_query_param(session_id, "session ID")
    if not is_valid:
        await sio.emit('error', {'code': 'invalid_payload', 'message': error_msg}, to=socket_id)
        return

    secret_key = data.get('secret_key') or session.get('secret_key')
    if not await _verify_socket_credentials(socket_id, session, secret_key, session_id, api_key=data.get('api_key')):
        return

    sync_data = data.copy()
    sync_data.pop("id", None)
    await _process_transcription_update(session_id, sync_data)

@sio.event
async def join_session(socket_id, data):
    """Handle an authenticated panel joining a session room."""
    if not _socket_limiter.check(socket_id, 'join_session', 5, 10.0):
        await sio.emit('error', {'message': 'Rate limit exceeded'}, to=socket_id)
        return
    session_id = data.get('session_id')
    secret_key = data.get('secret_key')
    api_key = data.get('api_key')
    if not session_id or not (secret_key or api_key):
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return
    is_valid, error_msg = validate_query_param(session_id, "session ID")
    if not is_valid:
        await sio.emit('error', {'code': 'invalid_payload', 'message': error_msg}, to=socket_id)
        return

    session = await sio.get_session(socket_id)

    # API-key path: authorize by key + room ownership, then join.
    if api_key or session.get('auth_via') == 'api_key':
        if not await _authorize_api_key_socket(socket_id, session, api_key, session_id):
            logger.warning(
                "join_session apikey_unauthorized sid_hash=%s socket_hash=%s",
                _hash_token(session_id), _hash_token(socket_id),
            )
            await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
            return
        await sio.enter_room(socket_id, session_id)
        viewer_count = await _viewer_presence_op(session_id, label="viewer count")
        await sio.emit(
            'joined_session',
            {'session_id': session_id, 'authorized': True, 'viewer_count': viewer_count},
            to=socket_id,
        )
        await _emit_viewer_count(session_id, viewer_count)
        logger.info(
            "join_session apikey_verified sid_hash=%s owner_email=%s socket_hash=%s",
            _hash_token(session_id), _mask_email(session.get("email")), _hash_token(socket_id),
        )
        return

    if not await verify_socket_auth(socket_id, session_id, secret_key):
        logger.warning(
            "join_session authentication_failed sid_hash=%s socket_hash=%s",
            _hash_token(session_id),
            _hash_token(socket_id),
        )
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return

    room = await rooms_collection.find_one({"sid": session_id, "secret_key": secret_key})
    if not room:
        await sio.emit('error', {'message': 'Unauthorized'}, to=socket_id)
        return

    session['secret_key'] = secret_key
    session['verified'] = True
    session['session_id'] = session_id
    session['email'] = await _get_room_owner_email(room)
    await sio.save_session(socket_id, session)
    await sio.enter_room(socket_id, session_id)
    logger.info(
        "join_session verified sid_hash=%s owner_email=%s socket_hash=%s",
        _hash_token(session_id),
        _mask_email(session.get("email")),
        _hash_token(socket_id),
    )
    viewer_count = await _viewer_presence_op(session_id, label="viewer count")
    await sio.emit(
        'joined_session',
        {'session_id': session_id, 'authorized': True, 'viewer_count': viewer_count},
        to=socket_id,
    )
    await _emit_viewer_count(session_id, viewer_count)

@sio.event
async def leave_session(socket_id, data):
    """Handle client leaving a session room"""
    session_id = data.get('session_id')
    if session_id:
        is_valid, error_msg = validate_query_param(session_id, "session ID")
        if not is_valid:
            await sio.emit('error', {'code': 'invalid_payload', 'message': error_msg}, to=socket_id)
            return
        await sio.leave_room(socket_id, session_id)
        await sio.emit('left_session', {'session_id': session_id}, to=socket_id)
        logger.info(
            "leave_session sid_hash=%s socket_hash=%s",
            _hash_token(session_id),
            _hash_token(socket_id),
        )

async def on_translation_completed(session_id, sync_data):
    await _process_transcription_update(session_id, sync_data)

async def on_scribe_transcription(session_id, transcription):
    """Callback for Scribe transcription"""
    # Hot path (>10 Hz on partials); translator only needs the last 3 committed segments.
    cached_data = await get_cached_transcription(session_id, num_committed=5)
    sync_data = transcription.copy()
    
    manager = _get_or_create_translation_manager(session_id)
    await manager.put(session_id, sync_data, cached_data, redis_client)

@sio.event
async def realtime_connect(socket_id, data):
    """Handle client realtime_connect events"""
    session = await sio.get_session(socket_id)
    if not await _check_socket_already_verified(socket_id, session):
        return

    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)

    if await is_realtime_authorized(session, session_id):
        _get_or_create_translation_manager(session_id)


@sio.event
async def mic_on(socket_id, data):
    """Start the scribe session when the panel mic is turned on."""
    session = await sio.get_session(socket_id)
    if not await _check_socket_already_verified(socket_id, session):
        return
    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)
    if session_id and await is_realtime_authorized(session, session_id):
        await _get_or_create_scribe_manager(session_id)


@sio.event
async def mic_off(socket_id, data):
    """Stop the scribe session immediately when the panel mic is turned off."""
    session = await sio.get_session(socket_id)
    if not await _check_socket_already_verified(socket_id, session, silent=True):
        return
    rooms = sio.rooms(socket_id)
    session_id = next((r for r in rooms if r != socket_id), None)
    if not session_id:
        return
    manager: ScribeSessionManager | None = active_scribe_managers.pop(session_id, None)
    if manager:
        logger.info("mic_off stopping_scribe sid_hash=%s", _hash_token(session_id))
        await manager.stop()


@sio.event
async def audio_buffer_append(socket_id, data):
    """Handle client audio buffer append events"""
    if not _socket_limiter.check(socket_id, 'audio_buffer_append', 30, 1.0):
        return

    ok, schema_err = validate_audio_buffer_append_payload(data)
    if not ok:
        logger.warning("audio_buffer_append invalid_payload socket_hash=%s err=%s", _hash_token(socket_id), schema_err)
        await sio.emit('error', {'code': 'invalid_payload', 'message': schema_err}, to=socket_id)
        return

    session = await sio.get_session(socket_id)

    session_id = session.get('session_id')

    if not session_id:
        logger.debug("audio_buffer_append no_session socket_hash=%s", _hash_token(socket_id))
        return

    # On every chunk after the first, skip all auth awaits: realtime_authorized
    # being True already implies verified is True (set together on first chunk).
    if not session.get('realtime_authorized'):
        secret_key = session.get('secret_key') or data.get('secret_key')
        if not await _verify_socket_credentials(socket_id, session, secret_key, session_id, api_key=data.get('api_key')):
            return
        if not await is_realtime_authorized(session, session_id):
            await sio.emit('error', {'message': 'Unauthorized: realtime token required'}, to=socket_id)
            return
        session['realtime_authorized'] = True
        await sio.save_session(socket_id, session)

    base64_audio = data["audio"]

    manager = active_scribe_managers.get(session_id)
    if not manager or not manager.is_running:
        manager = await _get_or_create_scribe_manager(session_id)
    else:
        # Refresh TTL so a session with continuous audio is never evicted mid-stream.
        # cachetools' .get() does not reset TTL — only __setitem__ does.
        active_scribe_managers[session_id] = manager
        translation_manager = active_translation_managers.get(session_id)
        if translation_manager:
            active_translation_managers[session_id] = translation_manager
    # On first audio chunk of a new manager instance, restore previously saved usage from DB
    # so counts survive page refreshes. Flag is set before the await to prevent double-restore.
    if not manager._usage_restored:
        manager._usage_restored = True
        room_usage = await rooms_collection.find_one(
            {"sid": session_id}, {"_id": 0, "audio_bytes": 1, "audio_chunks": 1}
        )
        if room_usage and room_usage.get("audio_bytes"):
            manager.restore_usage(room_usage["audio_bytes"], room_usage.get("audio_chunks", 0))
    await manager.push_audio(base64_audio)
    
