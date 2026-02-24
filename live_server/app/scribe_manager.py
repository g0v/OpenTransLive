import asyncio
import base64
import json
import logging
from urllib.parse import urlencode
import httpx
from websockets.asyncio.client import connect as ws_connect
from datetime import datetime, timezone
from .config import REALTIME_SETTINGS
from .translator import get_async_client

logger = logging.getLogger(__name__)

class ScribeSessionManager:
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
            logger.error(f"Error getting token: {e}")
            return None

    async def push_audio(self, base64_audio: str):
        """Called by socket.io to push audio from the client"""
        if self.is_running:
            await self.audio_queue.put(base64_audio)

    async def send_audio_loop(self):
        try:
            while self.is_running:
                base64_audio = await self.audio_queue.get()
                if self.ws:
                    message = {
                        "message_type": "input_audio_chunk",
                        "audio_base_64": base64_audio,
                        "sample_rate": 16000,
                        "commit": False
                    }
                    await self.ws.send(json.dumps(message))
                self.audio_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in send_audio_loop: {e}")

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
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in receive_messages_loop: {e}")

    async def handle_transcript(self, data):
        try:
            transcript = data.get("text", "").strip()
            if not transcript:
                return

            msg_type = data.get("message_type")
            partial = (msg_type == "partial_transcript")
            
            # Efficiently strip specific punctuation
            transcript = transcript.rstrip(",.。，")
            
            if not partial and transcript == self.last_partial_text:
                return
            
            now = datetime.now(timezone.utc)
            if self.seg_start_time is None:
                self.seg_start_time = now

            # Match the format expected by sync() event in __init__.py
            transcription = {
                "partial": partial,
                "start_time": self.seg_start_time.timestamp() - 0.3, # approximate adjust
                "end_time": now.timestamp(),
                "result": {"corrected": transcript}
            }
            
            if not partial:
                self.seg_start_time = None
                await self.callback(self.session_id, transcription)
            else:
                if (now - self.last_partial_time).total_seconds() > self.partial_interval:
                    self.last_partial_time = now
                    self.last_partial_text = transcript
                    await self.callback(self.session_id, transcription)
        except Exception as e:
            logger.error(f"Error handling transcript: {e}")

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
                "include_timestamps": "false"  # urlencode expects str
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
            logger.error(f"Scribe connection error for {self.session_id}: {e}")
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
