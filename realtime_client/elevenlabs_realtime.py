from datetime import datetime, timezone, timedelta
from typing import Generator, Iterator
from urllib.parse import urlencode
import os
import asyncio
import pyaudio
import base64
import logging
import opencc
import queue
import dotenv
from websockets.asyncio.client import connect as ws_connect
import json
import httpx

dotenv.load_dotenv(override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
pya = pyaudio.PyAudio()
converter = opencc.OpenCC("s2tw")

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms
PARTIAL_INTERVAL = 1.5

class MicrophoneStream:
    """Opens a recording stream as a generator yielding the audio chunks."""

    def __init__(self, rate: int = RATE, chunk: int = CHUNK) -> None:
        """The audio -- and generator -- is guaranteed to be on the main thread."""
        self._rate = rate
        self._chunk = chunk
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self) -> object:
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
            stream_callback=self._fill_buffer,
        )
        self.closed = False
        return self

    def __exit__(self, type, value, traceback) -> None:
        """Closes the stream, regardless of whether the connection was lost or not."""
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data: object, *args) -> object:
        """Continuously collect data from the audio stream, into the buffer."""
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self) -> Generator[bytes, None, None]:
        """Generates audio chunks from the stream of audio data in chunks."""
        while not self.closed:
            chunk = self._buff.get()
            if chunk is None:
                print("chunk is none, exiting generator")
                return
            data = [chunk]

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        print("chunk is none, exiting generator")
                        return
                    data.append(chunk)
                except queue.Empty:
                    break

            yield b"".join(data)


class ScribeRealtime:
    def __init__(self, language_code, callback = None):
        self.out_queue = None
        self.session = None
        self.audio_stream = None
        self.transcription = ""
        self.connection = None
        self.is_running = True
        self.segStartTime = None
        self.callback = callback
        self.init_time = datetime.now(timezone.utc)
        self.last_partial_time = datetime.now(timezone.utc)
        self.last_partial_text = ""
        self.out_buff = b""
        self.ws = None
        self.api_key = os.getenv("ELEVENLABS_API_KEY")
        self.ws_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
        
    def get_token(self) -> str:
        """Get a single-use token for realtime transcription"""
        try:
            with httpx.Client() as client:
                response = client.post(
                    "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe",
                    headers={"xi-api-key": self.api_key}
                )
                data = response.json()
                return data.get("token")
        except Exception as e:
            logger.error(f"Error getting token: {e}")
            return None
    # WebSocket message handlers
    def on_session_started(self, data):
        logger.info(f"Session started: {data}")

    def on_transcript(self, data):
        """Process streaming responses from Google Speech-to-Text API."""
        try:
            transcript: str = data["text"].strip()
            partial = (data["message_type"] == "partial_transcript")
                
            if transcript[-1:] in (",", ".", "。", "，"):
                transcript = transcript[:-1]
            
            if not partial and transcript == self.last_partial_text:
                return
            
            if self.segStartTime is None:
                self.segStartTime = datetime.now(timezone.utc)
    
            transcription = {
                "partial": partial,
                "text": transcript,
                "start_time": self.segStartTime.timestamp() - 0.3,
                "end_time": datetime.now(timezone.utc).timestamp(),
                "init_time": self.init_time.timestamp()
            }
            
            if not partial:
                self.segStartTime = None
                if self.callback:
                    asyncio.create_task(self.callback(transcription))
            else:
                if datetime.now(timezone.utc).timestamp() > self.last_partial_time.timestamp() + PARTIAL_INTERVAL:
                    self.last_partial_time = datetime.now(timezone.utc)
                    if self.callback:
                        asyncio.create_task(self.callback(transcription, partial=True))
                        
                    
        except Exception as e:
            print(f"Error processing response: {str(e)}")
            import traceback
            print(traceback.format_exc())
      
    def on_error(self, error):
        logger.error(f"Error: {error}")
      
    def on_close(self):
        logger.info("Connection closed")
                
    async def send_realtime(self):
        """Send audio data via WebSocket"""
        try:
            with MicrophoneStream(RATE, CHUNK) as stream:
                audio_generator = stream.generator()
                print("Listening...")
                for content in audio_generator:
                    await asyncio.sleep(0.01)
                    if self.ws:
                        base64_data = base64.b64encode(content).decode("utf-8")
                        message = {
                            "message_type": "input_audio_chunk",
                            "audio_base_64": base64_data,
                            "sample_rate": RATE,
                            "commit": False
                        }
                        await self.ws.send(json.dumps(message))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error sending audio: {e}")
            self.is_running = False
    
    async def receive_messages(self):
        """Receive messages from WebSocket"""
        try:
            while True:
                await asyncio.sleep(0.01)
                message = await self.ws.recv()
                data = json.loads(message)
                
                if data.get("message_type") == "session_started":
                    self.on_session_started(data)
                elif data.get("message_type") in ["partial_transcript", "committed_transcript"]:
                    self.on_transcript(data)
                elif data.get("message_type") in ["error", "auth_error", "quota_exceeded_error"]:
                    self.on_error(data.get("error"))
                else:
                    logger.error(f"unhandled message: {data}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error receiving messages: {e}")
            self.is_running = False
    
    async def run(self):
        """Main execution loop using WebSocket"""
        try:
            # Build WebSocket URL with API key
            
            params = urlencode({
                "token": self.get_token(),
                "model_id": "scribe_v2_realtime",
                "audio_format": "pcm_16000",
                "commit_strategy": "vad",
                "include_timestamps": False
            })
            ws_url = f"{self.ws_url}?{params}"
            
            async with ws_connect(ws_url, additional_headers={"xi-api-key": self.api_key}) as ws:
                self.ws = ws
                logger.info("WebSocket connected")
                
                # Run send and receive concurrently
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self.send_realtime())
                    tg.create_task(self.receive_messages())
                    
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            if self.audio_stream:
                self.audio_stream.close()
            self.is_running = False

if __name__ == "__main__":
    try:
        loop = ScribeRealtime(None)
        asyncio.run(loop.run())
    except KeyboardInterrupt:
        logger.info("Exiting...")
    finally:
        pass