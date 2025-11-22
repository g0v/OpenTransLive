import queue
import asyncio
import dotenv
import os
from typing import Generator, Iterator
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech as cloud_speech_types
from google.api_core.client_options import ClientOptions
from google.protobuf.duration_pb2 import Duration
from datetime import datetime, timezone

import pyaudio
dotenv.load_dotenv()

# Audio recording parameters
RATE = 16000
CHUNK = int(RATE / 10)
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
REGION = "us"
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
                return
            data = [chunk]

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break

            yield b"".join(data)

class GoogleSTTV2:
    def __init__(self, language_code, callback = None):
        self.language_code = language_code  # a BCP-47 language tag
        self.model = "latest_long"
        self.callback = callback
        self.init_time = datetime.now(timezone.utc)
        self.last_partial_time = datetime.now(timezone.utc)
        self.segStartTime = None
        self.buff = {
            "partial": queue.Queue(),
            "final": queue.Queue()
        }
        with self.buff['partial'].mutex:
            self.buff['partial'].queue.clear()

    def requests_iterator(self, audio_generator):
        """Creates an iterator of StreamingRecognizeRequest objects."""
        # See http://g.co/cloud/speech/docs/languages
        # for a list of supported languages.
        language_code = self.language_code  # a BCP-47 language tag

        recognition_config = cloud_speech_types.RecognitionConfig(
            auto_decoding_config=cloud_speech_types.AutoDetectDecodingConfig(),
            language_codes=language_code,
            model=self.model,
            explicit_decoding_config=cloud_speech_types.ExplicitDecodingConfig(
                encoding=cloud_speech_types.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=RATE,
                audio_channel_count=1
            ),
            features=cloud_speech_types.RecognitionFeatures(
                enable_automatic_punctuation=True,
                max_alternatives=1,
            )
        )
        streaming_config = cloud_speech_types.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=cloud_speech_types.StreamingRecognitionFeatures(
                interim_results=True,
            )
        )
        # enable_voice_activity_events=True,
        # voice_activity_timeout=cloud_speech_types.StreamingRecognitionFeatures.VoiceActivityTimeout(
        #     speech_start_timeout=Duration(seconds=10),
        #     speech_end_timeout=Duration(seconds=1)
        # )
        config_request = cloud_speech_types.StreamingRecognizeRequest(
            recognizer=f"projects/{PROJECT_ID}/locations/{REGION}/recognizers/_",
            streaming_config=streaming_config,
        )
        first_request_sent = False
        def request_iterator():
            nonlocal first_request_sent

            if not first_request_sent:
                first_request_sent = True
                yield config_request

            for content in audio_generator:
                try:
                    yield cloud_speech_types.StreamingRecognizeRequest(audio=content)
                except KeyboardInterrupt:
                    return
                except Exception as e:
                    print(f"Error generating audio request: {str(e)}")
                    break
        return request_iterator()

    async def process_response(self, result) -> None:
        """Process streaming responses from Google Speech-to-Text API."""
        try:
            transcript: str = result.alternatives[0].transcript.strip()
            
            if self.segStartTime is None:
                self.segStartTime = datetime.now(timezone.utc)
                
            if transcript[-1] in (",", ".", "。", "，"):
                transcript = transcript[:-1]
    
            transcription = {
                "partial": result.is_final == False,
                "text": transcript,
                "start_time": self.segStartTime.timestamp() - 0.3,
                "end_time": datetime.now(timezone.utc).timestamp(),
                "init_time": self.init_time.timestamp()
            }
            
            if result.is_final:
                self.segStartTime = None
                if self.callback:
                    await self.callback(transcription)
            else:
                if datetime.now(timezone.utc).timestamp() > self.last_partial_time.timestamp() + PARTIAL_INTERVAL:
                    self.last_partial_time = datetime.now(timezone.utc)
                    if self.callback:
                        await self.callback(transcription, partial=True)
                        
                    
        except Exception as e:
            print(f"Error processing response: {str(e)}")
            import traceback
            print(traceback.format_exc())
            
    async def run(self) -> None: 
        with MicrophoneStream(RATE, CHUNK) as stream:
            audio_generator = stream.generator()
            
            print("Listening...")
            try:
                request_iterator = self.requests_iterator(audio_generator)
                client = SpeechClient(
                    client_options=ClientOptions(
                        api_endpoint=f"{REGION}-speech.googleapis.com",
                    )
                )   
                responses = client.streaming_recognize(requests=request_iterator)
                for response in responses:
                    if response.results:
                        result = response.results[0]
                        if result.alternatives:
                            await self.process_response(result)
                    await asyncio.sleep(0.1)
                    
            except KeyboardInterrupt:
                print("Stopping transcription.")
            except Exception as e:
                print(f"Error during transcription: {str(e)}")
                import traceback
                print(traceback.format_exc())