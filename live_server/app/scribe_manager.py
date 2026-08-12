# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
import asyncio
import json
import random
import time
import tiktoken
from urllib.parse import urlencode
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from datetime import datetime, timezone
from .config import REALTIME_SETTINGS
from .logger_config import setup_logger, log_exception
from opencc import OpenCC

logger = setup_logger(__name__)
encoding = tiktoken.get_encoding("o200k_base")
_cc_s2tw = OpenCC('s2tw')

# No retry ceiling on purpose: as long as audio keeps arriving we keep trying to
# reconnect, because a session that silently stops transcribing mid-stream is worse
# than a long gap. What bounds the loop is not a count but rate: attempts back off to
# _RECONNECT_MAX_DELAY and only a connection that proves healthy clears the backoff
# (_HEALTHY_CONNECTION_SECS), and when audio really stops the _IDLE_TIMEOUT_SECS
# watchdog ends the session.
_RECONNECT_BASE_DELAY = 2.0   # seconds; doubles each attempt, capped at 60s
_RECONNECT_MAX_DELAY = 60.0
_MAX_BACKOFF_SHIFT = 10       # cap the doubling exponent; 2.0 * 2**10 already exceeds the max delay
_STATUS_ALERT_ATTEMPT = 2     # attempt number from which the panel is told transcription is interrupted
# A connection must survive this long to count as healthy and clear the backoff.
# ElevenLabs can accept the socket and close it immediately (quota / plan errors);
# resetting the retry counter on connect alone would make every such cycle retry with
# delay 0, i.e. an unthrottled handshake loop for the whole stream.
_HEALTHY_CONNECTION_SECS = 30
_WS_PING_INTERVAL = 15        # keepalive ping cadence; primary dead-socket detector
_WS_PING_TIMEOUT = 10         # drop the connection if a pong is missing this long
_RECV_POLL_INTERVAL = 2.0     # seconds; recv() timeout granularity for stall checks
# Application-level backstop for a socket that stays ping-alive but stops producing
# transcripts. Two thresholds, because how long a silence is normal depends on whether a
# segment is open:
#   - mid-segment: partials should keep coming, and a short commit closes the segment
#     within _COMMIT_TIMEOUT_SECS, so a tight timeout is both safe and fast.
#   - between segments: while audio flows but nobody speaks, Scribe's only output is a
#     periodic empty committed_transcript, measured at ~36s intervals against the live
#     API. The threshold has to clear that heartbeat with margin, otherwise every quiet
#     stretch would force a reconnect.
# Both must stay comfortably above _WS_PING_TIMEOUT so keepalive detects real dead
# sockets first and the two layers don't race into false reconnects.
_OUTPUT_STALL_TIMEOUT = 25       # mid-segment: no server message for this long -> reconnect
_IDLE_OUTPUT_STALL_TIMEOUT = 60  # no open segment: same, but past the ~36s empty-commit heartbeat
_SEGMENT_START_OFFSET = 0.3   # seconds subtracted from seg_start_time to account for ASR processing latency
_IDLE_TIMEOUT_SECS = 60       # stop session after 1 minute with no audio
_IDLE_CHECK_INTERVAL = 30     # how often the watchdog checks (seconds)
_MAX_SESSION_SECS = 300       # while audio keeps flowing, proactively restart a connection older than this
                              # to bound ElevenLabs session length (avoids long-run buildup / queue_overflow)
_MAX_PARTIAL_TOKENS = 150     # maximum length of partial transcript
_MIN_COMMIT_TOKENS = 50       # commits shorter than this are buffered and merged into the next segment
_COMMIT_TIMEOUT_SECS = 10     # force-flush buffered short commits after this delay if no follow-up arrives

class ScribeSessionManager:
    _BYTES_PER_SEC = 16000 * 2          # 16kHz 16-bit mono PCM
    _LOG_INTERVAL_BYTES = 30 * 16000 * 2  # log every 30s of audio
    _AUDIO_QUEUE_MAXSIZE = 1000         # ~30s of audio; drop oldest when full

    def __init__(self, session_id, callback, language_code: str = "", partial_interval: float | None = None,
                 status_callback=None):
        self.session_id = session_id
        # Optional async (session_id, status_dict) hook used to surface transcription
        # health to the panel. Set before the API key guard so _emit_status is always safe.
        self.status_callback = status_callback
        self._status = None
        # The keys reported alongside that state (reason, attempt). Kept whole so the
        # replay path can forward exactly what the live broadcast sent, instead of
        # re-listing fields one at a time and silently dropping the rest.
        self._status_extra = {}
        # Initialize essential state before the API key guard so that stop(),
        # push_audio(), and is_running checks always find these attributes.
        self.ws = None
        self.is_running = False
        self.audio_queue = asyncio.Queue(maxsize=self._AUDIO_QUEUE_MAXSIZE)
        self._stop_event = asyncio.Event()
        self.api_key = REALTIME_SETTINGS.get("ELEVENLABS_API_KEY", '')
        if not self.api_key:
            logger.error(f"Missing ELEVENLABS_API_KEY for {session_id}")
            return
        self.callback = callback
        self.language_code = language_code
        self.ws_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
        self.seg_start_time = None
        self.yt_start_time: float | None = None
        now = datetime.now(timezone.utc)
        self.init_time = now
        self.last_partial_time = now
        self.last_partial_text = ""
        self.last_recv_mono = time.monotonic()  # last time any message arrived from Scribe
        self._connection_start_mono: float | None = None  # set on each successful ws connect
        self._intentional_restart = False  # set when we deliberately drop the ws to reconnect
        self.task_group = None
        self.partial_interval = partial_interval if partial_interval else REALTIME_SETTINGS.get('PARTIAL_INTERVAL', 2)
        self.should_commit = False
        # Prefix holding committed text from segments too short to stand alone.
        # Prepended to subsequent partials/commits so translation gets useful context.
        self.pending_commit_text = ""
        self._commit_timeout_task: asyncio.Task | None = None
        # Usage tracking
        self.audio_bytes_total = 0
        self.audio_chunks = 0
        self._logged_at_bytes = 0
        self._usage_restored = False  # set to True after first DB restore attempt

    def restore_usage(self, audio_bytes: int, audio_chunks: int):
        """Restore usage counters from a previously saved DB value.

        Uses += so that any bytes already counted by concurrent push_audio calls
        during the async DB fetch are preserved rather than overwritten.
        """
        self.audio_bytes_total += audio_bytes
        self.audio_chunks += audio_chunks
        self._logged_at_bytes = self.audio_bytes_total

    def get_usage_stats(self) -> dict:
        """Return audio usage counters for this session."""
        return {
            "audio_bytes": self.audio_bytes_total,
            "audio_chunks": self.audio_chunks,
            "audio_duration_secs": round(self.audio_bytes_total / self._BYTES_PER_SEC, 1),
        }

    async def push_audio(self, base64_audio: str):
        """Called by socket.io to push audio from the client"""
        if self.is_running:
            self._last_audio_mono = time.monotonic()
            # base64: 4 chars encode 3 bytes
            decoded_bytes = len(base64_audio) * 3 // 4
            self.audio_bytes_total += decoded_bytes
            self.audio_chunks += 1
            # Periodic usage logging
            if self.audio_bytes_total - self._logged_at_bytes >= self._LOG_INTERVAL_BYTES:
                self._logged_at_bytes = self.audio_bytes_total
                duration_secs = self.audio_bytes_total / self._BYTES_PER_SEC
                logger.info(
                    f"[audio_usage] session={self.session_id} "
                    f"bytes={self.audio_bytes_total} "
                    f"duration={duration_secs:.1f}s "
                    f"chunks={self.audio_chunks}"
                )
            if self.audio_queue.full():
                try:
                    self.audio_queue.get_nowait()
                    self.audio_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                logger.warning(
                    f"[audio_queue] queue full, dropped oldest chunk "
                    f"for session {self.session_id}"
                )
            await self.audio_queue.put(base64_audio)

    @property
    def status(self) -> str | None:
        """Last transcription state successfully reported to the panel, or None if
        nothing has been reported yet. Read by the socket layer to replay the current
        state to a socket that (re)joins mid-outage."""
        return self._status

    @property
    def status_extra(self) -> dict:
        """Keys reported with `status`, replayed alongside it. See _status_extra."""
        return self._status_extra

    def mark_superseded(self):
        """Hand this session's panel status over to a replacement manager.

        Call it synchronously when restarting a session (e.g. an operator changing the
        detect language), before stop(). Otherwise this manager's terminal "stopped"
        still reaches the room, and because the replacement must stay healthy for
        _HEALTHY_CONNECTION_SECS before it reports "connected", the panel paints
        Transcribe as disconnected for 30s over a restart that took under a second.
        Shutdown itself is unaffected — only the reporting stops.
        """
        self.status_callback = None

    async def _emit_status(self, state: str, **extra):
        """Report transcription health to the panel; deduped so repeated reconnect
        attempts don't spam the room. Never raises into the reconnect loop."""
        if state == self._status:
            return
        if self.status_callback:
            try:
                await self.status_callback(self.session_id, {"state": state, **extra})
            except Exception as e:
                log_exception(logger, e, f"Error emitting scribe status for {self.session_id}")
                # Leave _status untouched: recording a state we failed to deliver would
                # dedupe away every later attempt and the panel would never hear about it.
                return
        self._status = state
        self._status_extra = extra

    async def _maybe_confirm_connected(self):
        """Clear an outage alarm only once the current connection has proven itself.

        Reporting "connected" the moment the socket opens would flicker the panel green
        on every cycle of an accept-then-close failure, which is the false all-clear this
        status exists to prevent. Cheap enough for the receive path: the common case is
        one string compare.
        """
        if self._status == "connected" or self._connection_start_mono is None:
            return
        if time.monotonic() - self._connection_start_mono >= _HEALTHY_CONNECTION_SECS:
            await self._emit_status("connected")

    def _drain_audio_queue(self):
        """Discard queued audio chunks that accumulated while disconnected."""
        drained = 0
        while True:
            try:
                item = self.audio_queue.get_nowait()
                self.audio_queue.task_done()
                if item is not None:
                    drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.info(
                f"[reconnect] drained {drained} stale audio chunks for {self.session_id}"
            )

    def _reset_segment_state(self):
        """Clear per-connection transcript state so a reconnect starts a clean segment.
        The audio that would have continued the open segment is drained on reconnect, so
        keeping seg_start_time / pending text around only leaks stale state into the new
        connection (and can false-trigger the output-stall watchdog)."""
        self.seg_start_time = None
        self.pending_commit_text = ""
        self.last_partial_text = ""
        self.should_commit = False
        self._cancel_commit_timeout()

    def _cancel_commit_timeout(self):
        """Cancel the pending short-commit flush task, if any is scheduled."""
        if self._commit_timeout_task and not self._commit_timeout_task.done():
            self._commit_timeout_task.cancel()

    async def send_audio_loop(self):
        try:
            while True:
                base64_audio = await self.audio_queue.get()
                if base64_audio is None:
                    self.audio_queue.task_done()
                    break

                if self.ws:
                    await self.ws.send(
                        '{"message_type":"input_audio_chunk","audio_base_64":"'
                        + base64_audio
                        + '","sample_rate":16000,"commit":' + str(self.should_commit).lower() + '}'
                    )
                    self.should_commit = False
                self.audio_queue.task_done()
        except (asyncio.CancelledError, ConnectionClosed, ConnectionClosedOK):
            pass
        except Exception as e:
            log_exception(logger, e, "Error in send_audio_loop")

    async def receive_messages_loop(self):
        try:
            while True:
                if not self.ws:
                    await asyncio.sleep(0.1)
                    continue

                await self._maybe_confirm_connected()

                try:
                    message = await asyncio.wait_for(self.ws.recv(), timeout=_RECV_POLL_INTERVAL)
                except asyncio.TimeoutError:
                    # No message this interval. If audio is still arriving but the server
                    # has gone silent for longer than it ever legitimately does, the socket
                    # is wedged (keepalive can stay green) — return to force a clean
                    # reconnect. Deliberately not gated on an open segment: the wedge is
                    # just as likely to start right after a commit, and that state used to
                    # be invisible here until the _MAX_SESSION_SECS restart 5 minutes later.
                    now_mono = time.monotonic()
                    stall_timeout = (_OUTPUT_STALL_TIMEOUT if self.seg_start_time is not None
                                     else _IDLE_OUTPUT_STALL_TIMEOUT)
                    if (now_mono - self.last_recv_mono >= stall_timeout
                            and now_mono - self._last_audio_mono < _RECV_POLL_INTERVAL * 2):
                        logger.warning(
                            f"Scribe output stalled ({now_mono - self.last_recv_mono:.0f}s) "
                            f"for {self.session_id}, forcing reconnect"
                        )
                        self._intentional_restart = True
                        return
                    continue

                self.last_recv_mono = time.monotonic()
                data = json.loads(message)

                msg_type = data.get("message_type")
                if msg_type == "session_started":
                    logger.info(f"Scribe session started for {self.session_id}")
                elif msg_type in ["partial_transcript", "committed_transcript"]:
                    await self.handle_transcript(data)
                elif msg_type in ["error", "auth_error", "quota_exceeded_error"]:
                    logger.error(f"Scribe Error: {data.get('error')}")
                elif msg_type == "queue_overflow":
                    # Server-side audio buffer overflowed; it will close the socket next.
                    # Log and let the reconnect path recover instead of misreporting Unknown.
                    logger.warning(f"Scribe queue_overflow for {self.session_id}; server dropping audio")
                else:
                    logger.error(f"Scribe Unknown message type: {msg_type}")
        except (asyncio.CancelledError, ConnectionClosed, ConnectionClosedOK):
            pass
        except Exception as e:
            log_exception(logger, e, "Error in receive_messages_loop")
        finally:
            # Signal send_audio_loop to exit so the TaskGroup can complete.
            try:
                if self.audio_queue.full():
                    try:
                        self.audio_queue.get_nowait()
                        self.audio_queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                self.audio_queue.put_nowait(None)
            except Exception:
                pass

    def _build_transcription(self, text: str, partial: bool, end_time: datetime) -> dict:
        if self.seg_start_time is None: return {}
        if self.language_code.startswith('zh') or (self.language_code == '' and REALTIME_SETTINGS.get("FORCE_OPENCC", False)):
            text = _cc_s2tw.convert(text)
        return {
            "text": text,
            "partial": partial,
            "start_time": self.seg_start_time.timestamp() - _SEGMENT_START_OFFSET,
            "end_time": end_time.timestamp(),
        }

    @staticmethod
    def _is_hallucination(text: str) -> bool:
        """Detect common ASR hallucinations: repetitive patterns or pure digit sequences."""
        if len(text) < 8:
            return False

        lower = text.lower().replace(" ", "")

        # Detect repetitive unit patterns: hahahaha, lalalala, hmm hmm hmm hmm, etc.
        for unit_len in range(1, min(len(lower) // 4, 8) + 1):
            unit = lower[:unit_len]
            reps = len(lower) // unit_len
            if reps >= 4 and lower.startswith(unit * reps):
                return True

        return False

    async def handle_transcript(self, data):
        try:
            transcript = data.get("text", "").strip()
            if not transcript or len(transcript) <= 2:
                return

            msg_type = data.get("message_type")
            partial = (msg_type == "partial_transcript")
            now = datetime.now(timezone.utc)

            # Efficiently strip specific punctuation
            transcript = transcript.rstrip(",.。，").strip()

            if self._is_hallucination(transcript):
                logger.warning(f"Hallucination detected, dropping: {repr(transcript)}")
                return
            delta_t = (now - self.last_partial_time).total_seconds()
            if partial and transcript == self.last_partial_text:
                return
            # A partial landing inside partial_interval isn't worth an LLM call, but it
            # still carries new source text: emit it as flow-only so the panel's flow
            # shows every partial while translation stays on the interval.
            flow_only = partial and delta_t < self.partial_interval
            kind = "flow-only partial" if flow_only else "partial" if partial else "committed"
            print(f"accept transcript {kind}: {transcript}, {delta_t}", flush=True)

            if self.seg_start_time is None:
                self.seg_start_time = now

            combined_text = self.pending_commit_text + transcript
            encode_len = len(encoding.encode(combined_text))
            emit_partial = partial or encode_len < _MIN_COMMIT_TOKENS

            if emit_partial:
                transcription = self._build_transcription(combined_text, True, now)
                if flow_only:
                    transcription["flow_only"] = True
                else:
                    # The interval counts from the last partial actually sent for
                    # translation, so a flow-only one must not advance the clock.
                    self.last_partial_time = now
                if partial:
                    self.last_partial_text = transcript
                else:
                    # Short commit: keep segment open and accumulate as prefix.
                    print(f"Short commit, appending to pending: {repr(combined_text)}", flush=True)
                    self.pending_commit_text = combined_text + " "
                    if self._commit_timeout_task is None or self._commit_timeout_task.done():
                        self._commit_timeout_task = asyncio.create_task(self._commit_timeout_handler())
                if encode_len > _MAX_PARTIAL_TOKENS:
                    self.should_commit = True
                asyncio.create_task(self.callback(self.session_id, transcription))
            else:
                transcription = self._build_transcription(combined_text, False, now)
                self.pending_commit_text = ""
                self.seg_start_time = None
                self.should_commit = False
                self._cancel_commit_timeout()
                asyncio.create_task(self.callback(self.session_id, transcription))

        except Exception as e:
            log_exception(logger, e, "Error handling transcript")

    async def _commit_timeout_handler(self):
        """Force-flush pending_commit_text after _COMMIT_TIMEOUT_SECS so short
        commits don't sit indefinitely when no longer-form follow-up arrives."""
        try:
            await asyncio.sleep(_COMMIT_TIMEOUT_SECS)
            if self.pending_commit_text and self.seg_start_time is not None:
                now = datetime.now(timezone.utc)
                text = self.pending_commit_text.strip()
                transcription = self._build_transcription(text, False, now)
                self.pending_commit_text = ""
                self.seg_start_time = None
                self.should_commit = False
                logger.info(
                    f"Commit timeout reached for {self.session_id}, flushing: {repr(text)}"
                )
                asyncio.create_task(self.callback(self.session_id, transcription))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_exception(logger, e, "Error in commit timeout handler")

    async def idle_watchdog_loop(self):
        """Watchdog: stop the session after _IDLE_TIMEOUT_SECS with no audio, and
        proactively restart a still-active connection older than _MAX_SESSION_SECS."""
        while self.is_running:
            if await self._reconnect_delay(_IDLE_CHECK_INTERVAL):
                break  # stop_event fired — session is already stopping
            now_mono = time.monotonic()
            idle_secs = now_mono - self._last_audio_mono
            if idle_secs >= _IDLE_TIMEOUT_SECS:
                logger.info(
                    f"Scribe idle timeout ({idle_secs:.0f}s) for {self.session_id}, stopping"
                )
                await self.stop()
                break
            # Audio still flowing but this connection has run too long: close it so the
            # start() loop reconnects with a fresh ElevenLabs session.
            if (self.ws is not None and self._connection_start_mono is not None
                    and now_mono - self._connection_start_mono >= _MAX_SESSION_SECS):
                logger.info(
                    f"Scribe max session ({now_mono - self._connection_start_mono:.0f}s) "
                    f"for {self.session_id}, restarting connection"
                )
                self._intentional_restart = True
                try:
                    await self.ws.close()
                except Exception:
                    pass

    def _next_retry_count(self, retry_count: int) -> int:
        """Advance the retry counter for the connection that just ended.

        Only a connection that lived at least _HEALTHY_CONNECTION_SECS resets the
        backoff (to 1, i.e. one immediate retry). A socket that was accepted and closed
        right away is a failed attempt, not a success, so it keeps the counter climbing.
        """
        lived = (
            time.monotonic() - self._connection_start_mono
            if self._connection_start_mono is not None else 0.0
        )
        return 1 if lived >= _HEALTHY_CONNECTION_SECS else retry_count + 1

    async def _reconnect_delay(self, delay: float) -> bool:
        """Sleep for `delay` seconds, or return early if stop() was called.
        Returns True if we should stop, False if we should continue."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True  # stop event fired
        except asyncio.TimeoutError:
            return False  # normal timeout, continue

    async def start(self):
        if not self.api_key:
            # __init__ returned early, so language_code / ws_url don't even exist. Without
            # this guard the connect path raises AttributeError, which the broad handler
            # below catches and retries — forever, now that there is no retry ceiling.
            logger.error(f"Scribe not starting for {self.session_id}: missing ELEVENLABS_API_KEY")
            await self._emit_status("stopped", reason="misconfigured")
            return

        self.is_running = True
        self._stop_event.clear()
        self._last_audio_mono = time.monotonic()
        logger.info(f"Starting Scribe session for {self.session_id}")

        watchdog_task = asyncio.create_task(self.idle_watchdog_loop())
        retry_count = 0

        try:
            while self.is_running:
                if retry_count > 0:
                    # First reconnect is immediate to minimize the transcription gap after
                    # a server-initiated close (queue_overflow / idle drop); later attempts
                    # back off exponentially.
                    delay = 0.0 if retry_count == 1 else min(
                        _RECONNECT_BASE_DELAY * (2 ** min(retry_count - 2, _MAX_BACKOFF_SHIFT)),
                        _RECONNECT_MAX_DELAY,
                    )
                    if delay:
                        # Jitter (±25%) so many sessions dropped together don't reconnect in lockstep.
                        delay *= 0.75 + random.random() * 0.5
                        logger.warning(
                            f"[reconnect] waiting {delay:.1f}s before attempt "
                            f"{retry_count} for {self.session_id}"
                        )
                    if retry_count >= _STATUS_ALERT_ATTEMPT:
                        # The immediate retry already failed, so the gap is now long enough
                        # for the operator to notice. Routine intentional restarts
                        # (_MAX_SESSION_SECS) reconnect on attempt 1 and stay silent.
                        await self._emit_status("reconnecting", attempt=retry_count)
                    # delay==0 still yields control; a concurrent stop() is caught by the
                    # is_running check below (stop() clears it before setting _stop_event).
                    should_stop = await self._reconnect_delay(delay)
                    if should_stop or not self.is_running:
                        break
                    # Discard audio that piled up while we were disconnected, and reset the
                    # open segment so stale state doesn't leak into the new connection.
                    self._drain_audio_queue()
                    self._reset_segment_state()

                try:
                    # Cleared per attempt so a failed ws_connect can't be mistaken for the
                    # previous (possibly long-lived) connection when scoring health below.
                    self._connection_start_mono = None
                    params_dict = {
                        "model_id": "scribe_v2_realtime",
                        "audio_format": "pcm_16000",
                        "commit_strategy": "vad",
                        "vad_silence_threshold_secs": 1,
                        "vad_threshold": 0.3,
                        "min_speech_duration_ms": 100,
                        "min_silence_duration_ms": 100,
                        "include_timestamps": "false",
                        "enable_logging": "false",
                        "disable_logging": "true"
                    }
                    if self.language_code:
                        params_dict["language_code"] = self.language_code
                        logger.info(f"Scribe forced language: {self.language_code} for {self.session_id}")
                    params = urlencode(params_dict)
                    url = f"{self.ws_url}?{params}"

                    async with ws_connect(
                        url,
                        additional_headers={"xi-api-key": self.api_key},
                        ping_interval=_WS_PING_INTERVAL,
                        ping_timeout=_WS_PING_TIMEOUT,
                    ) as ws:
                        self.ws = ws
                        now_mono = time.monotonic()
                        self.last_recv_mono = now_mono          # fresh baseline for stall detection
                        self._connection_start_mono = now_mono  # fresh baseline for max-session restart
                        if retry_count > 0:
                            logger.info(
                                f"Reconnected to Scribe for {self.session_id} "
                                f"(after {retry_count} attempt(s))"
                            )
                        else:
                            logger.info(f"Connected to Scribe for session {self.session_id}")
                        # Neither retry_count nor the panel status is cleared here: an
                        # accepted socket is not yet a working one. See _next_retry_count
                        # and _maybe_confirm_connected.

                        async with asyncio.TaskGroup() as tg:
                            self.task_group = tg
                            tg.create_task(self.send_audio_loop())
                            tg.create_task(self.receive_messages_loop())

                    # TaskGroup exited — both loops are done.
                    self.ws = None
                    if self.is_running and not self._stop_event.is_set():
                        retry_count = self._next_retry_count(retry_count)
                        if self._intentional_restart:
                            self._intentional_restart = False
                            logger.info(f"Scribe reconnecting for {self.session_id} (intentional restart)")
                        else:
                            logger.warning(
                                f"Scribe WebSocket closed unexpectedly for {self.session_id}"
                            )

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log_exception(logger, e, f"Scribe connection error for {self.session_id}")
                    self.ws = None
                    retry_count = self._next_retry_count(retry_count)

        finally:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        self.is_running = False
        self.ws = None
        await self._emit_status("stopped")
        logger.info(f"Scribe session ended for {self.session_id}")

    async def stop(self):
        self.is_running = False
        self._stop_event.set()
        self._cancel_commit_timeout()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        logger.info(f"Scribe session stopped for {self.session_id}")

        # Commit any pending partial / buffered short commit so they are not
        # lost on stop/mic-off.
        callback = getattr(self, "callback", None)
        final_text = (self.pending_commit_text + self.last_partial_text).strip()
        if callback and final_text and self.seg_start_time is not None:
            try:
                now = datetime.now(timezone.utc)
                transcription = self._build_transcription(final_text, False, now)
                self.seg_start_time = None
                self.pending_commit_text = ""
                logger.info(
                    f"Committing last partial on stop for {self.session_id}: "
                    f"{repr(final_text)}"
                )
                await callback(self.session_id, transcription)
            except Exception as e:
                log_exception(logger, e, "Error committing last partial on stop")
