import asyncio
import base64
import json
import logging
import re
from urllib.parse import urlencode
import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from datetime import datetime, timezone
from .config import REALTIME_SETTINGS
from .translator import get_async_client
from .logger_config import setup_logger, log_exception

logger = setup_logger(__name__)

class ScribeSessionManager:
    _BYTES_PER_SEC = 16000 * 2          # 16kHz 16-bit mono PCM
    _LOG_INTERVAL_BYTES = 30 * 16000 * 2  # log every 30s of audio

    def __init__(self, session_id, callback):
        self.session_id = session_id
        self.api_key = REALTIME_SETTINGS.get("ELEVENLABS_API_KEY", '') 
        if not self.api_key:
            logger.error(f"Missing ELEVENLABS_API_KEY for {session_id}")
            return
        self.callback = callback
        self.ws = None
        self.is_running = False
        self.ws_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
        self.audio_queue = asyncio.Queue()
        self.seg_start_time = None
        now = datetime.now(timezone.utc)
        self.init_time = now
        self.last_partial_time = now
        self.last_partial_text = ""
        self.task_group = None
        self.partial_interval = REALTIME_SETTINGS.get('PARTIAL_INTERVAL', 2)
        # Usage tracking
        self.audio_bytes_total = 0
        self.audio_chunks = 0
        self._logged_at_bytes = 0
        self._usage_restored = False  # set to True after first DB restore attempt

    async def get_token(self) -> str | None:
        """Get a single-use token for realtime transcription"""
        try:
            client = get_async_client()
            response = await client.post(
                "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe",
                headers={"xi-api-key": self.api_key},
                timeout=10.0
            )
            response.raise_for_status()
            data = response.json()
            return data.get("token")
        except Exception as e:
            log_exception(logger, e, "Error getting Scribe API token")
            return None

    def restore_usage(self, audio_bytes: int, audio_chunks: int):
        """Restore usage counters from a previously saved DB value."""
        self.audio_bytes_total = audio_bytes
        self.audio_chunks = audio_chunks
        self._logged_at_bytes = audio_bytes

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
            await self.audio_queue.put(base64_audio)

    async def send_audio_loop(self):
        try:
            while self.is_running:
                base64_audio = await self.audio_queue.get()
                if base64_audio is None:
                    self.audio_queue.task_done()
                    break

                if self.ws:
                    message = {
                        "message_type": "input_audio_chunk",
                        "audio_base_64": base64_audio,
                        "sample_rate": 16000,
                        "commit": False
                    }
                    await self.ws.send(json.dumps(message))
                self.audio_queue.task_done()
        except (asyncio.CancelledError, ConnectionClosed, ConnectionClosedOK):
            pass
        except Exception as e:
            log_exception(logger, e, "Error in send_audio_loop")
        finally:
            self.is_running = False

    async def receive_messages_loop(self):
        try:
            while self.is_running:
                if not self.ws:
                    await asyncio.sleep(0.1)
                    continue
                
                message = await self.ws.recv()
                data = json.loads(message)
                
                msg_type = data.get("message_type")
                if msg_type == "session_started":
                    logger.info(f"Scribe session started for {self.session_id}")
                elif msg_type in ["partial_transcript", "committed_transcript"]:
                    await self.handle_transcript(data)
                elif msg_type in ["error", "auth_error", "quota_exceeded_error"]:
                    logger.error(f"Scribe Error: {data.get('error')}")
        except (asyncio.CancelledError, ConnectionClosed, ConnectionClosedOK):
            pass
        except Exception as e:
            log_exception(logger, e, "Error in receive_messages_loop")
        finally:
            self.is_running = False
            # Trigger send_audio_loop to exit if it's waiting on queue
            try:
                self.audio_queue.put_nowait(None)
            except Exception:
                pass

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
            if not transcript:
                return

            msg_type = data.get("message_type")
            partial = (msg_type == "partial_transcript")
            now = datetime.now(timezone.utc)

            # Efficiently strip specific punctuation
            transcript = transcript.rstrip(",.。，")

            if self._is_hallucination(transcript):
                logger.warning(f"Hallucination detected, dropping: {repr(transcript)}")
                return
            delta_t = (now - self.last_partial_time).total_seconds()
            if partial and (transcript == self.last_partial_text or 
                            delta_t < self.partial_interval):
                return
            print(f"accept transcript {'partial' if partial else 'committed'}: {transcript}, {delta_t}", flush=True)
            
            if self.seg_start_time is None:
                self.seg_start_time = now

            # Match the format expected by sync() event in __init__.py
            transcription = {
                "text": transcript,
                "partial": partial,
                "start_time": self.seg_start_time.timestamp() - 0.3, # approximate adjust
                "end_time": now.timestamp()
            }
            
            if partial:
                self.last_partial_time = now
                self.last_partial_text = transcript
                asyncio.create_task(self.callback(self.session_id, transcription))
            else:
                self.seg_start_time = None
                asyncio.create_task(self.callback(self.session_id, transcription))

        except Exception as e:
            log_exception(logger, e, "Error handling transcript")

    async def start(self):
        self.is_running = True
        logger.info(f"Starting Scribe session for {self.session_id}")
        try:
            token = await self.get_token()
            if not token:
                logger.error(f"Failed to get Scribe token for {self.session_id}")
                return

            params = urlencode({
                "token": token,
                "model_id": "scribe_v2_realtime",
                "audio_format": "pcm_16000",
                "commit_strategy": "vad",
                "vad_silence_threshold_secs": 0.5,
                "vad_threshold": 0.3,
                "min_speech_duration_ms": 250,
                "min_silence_duration_ms": 250,
                "include_timestamps": "false"
            })
            url = f"{self.ws_url}?{params}"
            
            async with ws_connect(url, additional_headers={"xi-api-key": self.api_key}) as ws:
                self.ws = ws
                logger.info(f"Connected to Scribe for session {self.session_id}")
                
                async with asyncio.TaskGroup() as tg:
                    self.task_group = tg
                    tg.create_task(self.send_audio_loop())
                    tg.create_task(self.receive_messages_loop())
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_exception(logger, e, f"Scribe connection error for {self.session_id}")
        finally:
            self.is_running = False
            self.ws = None

    async def stop(self):
        self.is_running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
