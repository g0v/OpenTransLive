# This file is part of g0v/realtime_transcribe.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
 


import json
import os
import io
import opencc
import httpx
import socketio
from pathlib import Path
from datetime import datetime, timezone, timedelta
import speech_recognition as sr
import logging
import dotenv
from queue import Queue
from tempfile import NamedTemporaryFile
from openai import OpenAI
from groq import Groq
import asyncio
import argparse
import whisperx


dotenv.load_dotenv(override=True)
parser = argparse.ArgumentParser(prog="opentranslive transcribe client")
parser.add_argument("-t", "--target-sid", help="target session id", default=None)
args = parser.parse_args()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
model = os.getenv("TRANSCRIBE_MODEL", "large-v3")
device = os.getenv("TRANSCRIBE_DEVICE", "auto")
transcriber = os.getenv("TRANSCRIBER", "whisperx")
record_timeout = int(os.getenv("RECORD_TIMEOUT", 5))
energy_threshold = int(os.getenv("RECORD_ENERGY_THRESHOLD", 100))
pause_threshold_ms = int(os.getenv("RECORD_PAUSE_THRESHOLD_MS", 1000))
server_url = os.getenv("SERVER_ENDPOINT", '127.0.0.1:5000')
api_endpoint = f"http://{server_url}/api/sync/{args.target_sid}" if args.target_sid else None
ai_model = os.getenv("AI_MODEL", "gpt-4.1-nano")

# Initialize SocketIO client
sio = socketio.Client()

converter = opencc.OpenCC("s2tw")
source = sr.Microphone(sample_rate=16000)
recorder = sr.Recognizer()
recorder.energy_threshold = energy_threshold
recorder.dynamic_energy_threshold = False
recorder.pause_threshold = pause_threshold_ms / 1000.0
recorder.non_speaking_duration = recorder.pause_threshold
if transcriber == "whisperx":
    audio_model = whisperx.load_model(model, device, compute_type="float16", asr_options={
        "beam_size": 10,
        "temperatures": 0
    })

file_path = Path(f"output/{datetime.now().strftime('%Y-%m-%d')}/{datetime.now().strftime('%H-%M-%S')}.json")
file_path.parent.mkdir(parents=True, exist_ok=True)
transcription_data = {"transcriptions": [], "last_updated": None, "status": "running"}

with open(f"output/current_keywords.txt", "w", encoding="utf-8") as f:
    f.write('\n'.join(os.getenv('COMMON_PROMPT').split(',')))

# WebSocket event handlers
@sio.event
def connect():
    logger.info("Connected to server")
    if args.target_sid:
        sio.emit('join_session', {'session_id': args.target_sid})

@sio.event
def disconnect():
    logger.info("Disconnected from server")

@sio.event
def error(data):
    logger.info(f"WebSocket error: {data}")

def send_transcription_via_websocket(transcription_data):
    """Send transcription data via WebSocket"""
    if args.target_sid and sio.connected:
        try:
            # Add the session ID to the transcription data
            websocket_data = transcription_data.copy()
            websocket_data['id'] = args.target_sid
            sio.emit('sync', websocket_data)
        except Exception as e:
            logger.error(f"Error sending via WebSocket: {e}")
    elif api_endpoint:
        # Fallback to HTTP POST if WebSocket is not available
        try:
            with httpx.Client() as client:
                response = client.post(
                    api_endpoint,
                    json=transcription_data,
                    headers={
                        "Content-Type": "application/json; charset=utf-8"
                    },
                    timeout=None
                )
        except Exception as e:
            logger.error(f"Error sending via HTTP: {e}")

async def async_chat_completion(json_body):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=json_body,
            headers={
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
                "Content-Type": "application/json"
            },
            timeout=None
        )
    return response

async def translate_text(data: dict):
    """language code should be in IETF BCP 47 format"""
    languages = [language.strip() for language in os.getenv('TRANSLATE_LANGUAGES').split(',')]
    try:                    
        context = {
            "corrected": "",
            "translated": {
                language: "" for language in languages
            }
        }
        if data["id"] > 3:
            for transcription in transcription_data["transcriptions"][data["id"] - min(20, len(transcription_data["transcriptions"])):data["id"]]:
                if "result" in transcription:
                    context["corrected"] += transcription["result"]["corrected"]
                    for language in languages:
                        context["translated"][language] += transcription["result"]["translated"][language]
                else:
                    context["corrected"] += transcription["text"]
                    for language in languages:
                        context["translated"][language] += transcription["text"]
        output_dict = {
            "corrected": "corrected text",
            "special_keywords": [],
        }
        
        with open(f"output/current_keywords.txt", "r", encoding="utf-8") as f:
            current_keywords = f.read().split('\n')
        
        json_body = {
            "model": ai_model,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "messages": [
                {"role": "developer", "content": f"""
                 1. Correct the text **only in <correct_this>** as "corrected text" according to the reference and context.
                 2. If there are very special keywords in the "corrected text", add them to the "special_keywords" list.
                 Return in json format:
                 {output_dict}
                 """},
                {"role": "user", "content": f"""
                 <reference>
                 This is a transcription about:
                 { ','.join(current_keywords)}
                 </reference>
                 <context>
                 {context["corrected"]}
                 </context>
                 <correct_this>
                 {data["text"]}
                 </correct_this>
                 """
                }
            ]
        }
        response = await async_chat_completion(json_body)
        if response.status_code != 200:
            raise Exception(response.text)
        result = json.loads(
            response.json()["choices"][0]["message"]["content"].encode('utf-8').decode('utf-8').replace('<correct_this>', '').replace('</correct_this>', ''))
        keywords = result["special_keywords"]
        # Add new keywords
        for keyword in keywords:
            if keyword not in current_keywords:
                current_keywords.append(keyword)
        # Write back to file
        with open(f"output/current_keywords.txt", "w", encoding="utf-8") as f:
            f.write('\n'.join(current_keywords))

        translated = {}
        atasks = []
        async def _worker(language):
            json_body = {
                "model": ai_model,
                "temperature": 0,
                "messages": [
                    {"role": "developer", 
                     "content": f"Rewrite the text only in <translate_this> into {language}, and only return the translated text."},
                    {"role": "user", "content": f"""
                    <reference>
                    This is a transcription about:
                    { ','.join(current_keywords)}
                    </reference>
                    <context>
                    {context["translated"][language]}
                    </context>
                    <translate_this>
                    {result["corrected"]}
                    </translate_this>
                    """
                    }
                ]
            }
            response = await async_chat_completion(json_body)
            if response.status_code != 200:
                raise Exception(response.text)
            translated[language] = response.json()["choices"][0]["message"]["content"].encode('utf-8').decode('utf-8').replace('<translate_this>', '').replace('</translate_this>', '')
        for language in languages:
            atasks.append(_worker(language))
        await asyncio.gather(*atasks)
            
        result["translated"] = translated
        start_time = transcription_data["transcriptions"][data["id"]]["start_time"]
        logger.info(f"{start_time} - {result}")
        transcription_data["transcriptions"][data["id"]]["result"] = result
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(transcription_data, f, ensure_ascii=False, indent=2)
                    
        # Send transcription via WebSocket
        send_transcription_via_websocket(transcription_data["transcriptions"][data["id"]])
    except Exception as e:
        logger.error(f"Error translating text: {str(e)}")
        raise e

async def transcribe_audio():
    """Main transcription function"""
    
    # Initialize microphone
    with source:
        recorder.adjust_for_ambient_noise(source)
        logger.info("Microphone initialized")
    
    # Setup audio processing
    temp_file = NamedTemporaryFile().name + '.wav'
    data_queue = Queue()
    running = True
    init_time = datetime.now(timezone.utc)
    
    async def groq_transcribe(now, duration):
        client = Groq(api_key=os.getenv('GROQ_API_KEY'))
        with open(temp_file, 'rb') as audio_file:
            result = client.audio.transcriptions.create(
                file=(temp_file, audio_file.read()),
                model="whisper-large-v3-turbo",
                response_format="verbose_json",
            )
        for segment in result.segments:
            text = segment['text'].strip()
            if not text or text[-1] in ('.', '?', '!', '。', '？', '！'):
                text = text[:-1] if text else ""
            
            if text and 'chinese' in result.language.lower():
                text = converter.convert(text)
            
            if text.strip():
                transcription = {
                    "id": len(transcription_data["transcriptions"]),
                    "text": text.strip(),
                    "start_time": now.timestamp() + segment['start'] - duration,
                    "end_time": now.timestamp() + segment['end'] - duration,
                    "init_time": init_time.timestamp()
                }
                transcription_data["transcriptions"].append(transcription)
                transcription_data["last_updated"] = datetime.now().isoformat()
                logger.info(f"Transcribed: {text}")
                
                # Save to file
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(transcription_data, f, ensure_ascii=False, indent=2)
                
                # Translate text in a separate thread to avoid blocking
                asyncio.create_task(translate_text(transcription))
        
    async def openai_transcribe(now, duration):
        # Transcribe with OpenAI GPT-4o
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        with open(temp_file, 'rb') as audio_file:
            result = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                chunking_strategy={
                    "type": "server_vad",
                    "prefix_padding_ms": pause_threshold_ms,
                    "silence_duration_ms": pause_threshold_ms,
                    "threshold": 0.0
                },
                include=["logprobs"],
                file=audio_file
            )
        # Process transcription results
        text = converter.convert(result.text.strip())
        if not text or text[-1] in ('.', '?', '!', '。', '？', '！'):
            text = text[:-1] if text else ""
                
        logprobs = result.logprobs
        l_sum = 0
        for l in logprobs:
            l_sum += l.logprob
        l_avg = l_sum/len(logprobs)
        if text.strip() and l_avg > -1:
            transcription = {
                "id": len(transcription_data["transcriptions"]),
                "text": text.strip(),
                "start_time": now.timestamp() - duration,
                "end_time": now.timestamp(),
                "init_time": init_time.timestamp()
            }
            transcription_data["transcriptions"].append(transcription)
            transcription_data["last_updated"] = datetime.now().isoformat()
            logger.info(f"{l_avg} - Transcribed: {text}")
            
            # Save to file
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(transcription_data, f, ensure_ascii=False, indent=2)
            
            # Translate text in a separate thread to avoid blocking
            asyncio.create_task(translate_text(transcription))
            
        else:
            logger.info(f"ignored - {l_avg} - Transcribed: {text}")
            
    async def whisperx_transcribe(now: datetime, duration):        
        audio = whisperx.load_audio(temp_file)
        result = audio_model.transcribe(audio, batch_size=16)

        # Process transcription results
        for segment in result['segments']:
            text = segment['text'].strip()
            if not text or text[-1] in ('.', '?', '!', '。', '？', '！'):
                text = text[:-1] if text else ""
            
            if text and 'zh' in result['language']:
                text = converter.convert(text)
            
            if text.strip():
                transcription = {
                    "id": len(transcription_data["transcriptions"]),
                    "text": text.strip(),
                    "start_time": now.timestamp() + segment['start'] - duration,
                    "end_time": now.timestamp() + segment['end'] - duration,
                    "init_time": init_time.timestamp()
                }
                transcription_data["transcriptions"].append(transcription)
                transcription_data["last_updated"] = datetime.now().isoformat()
                logger.info(f"Transcribed: {text}")
                
                # Save to file
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(transcription_data, f, ensure_ascii=False, indent=2)
                
                # Translate text in a separate thread to avoid blocking
                asyncio.create_task(translate_text(transcription))
        
        
    overlap_buffer = b""
    overlap_duration = recorder.pause_threshold * 0.65  # seconds
    
    def record_callback(_, audio):
        data_queue.put(audio.get_raw_data())
    
    recorder.listen_in_background(source, record_callback, phrase_time_limit=record_timeout)
    logger.info("Started transcribing audio")
    
    try:
        while running:
            await asyncio.sleep(0.1)
            
            if data_queue.empty():
                continue
            
            # Process audio data
            audio_data = b""
            while not data_queue.empty():
                last_audio_data = data_queue.get()
                audio_data += last_audio_data
            
            if not audio_data:
                continue
            
            # Add overlap from previous chunk
            if overlap_buffer:
                audio_data = overlap_buffer + audio_data
            
            # Calculate overlap for next chunk
            sr_audio = sr.AudioData(audio_data, source.SAMPLE_RATE, source.SAMPLE_WIDTH)
            total_duration = len(sr_audio.frame_data) / (sr_audio.sample_rate * sr_audio.sample_width)
            
            if total_duration > overlap_duration:
                # Calculate bytes for overlap
                overlap_bytes = int(overlap_duration * sr_audio.sample_rate * sr_audio.sample_width)
                overlap_buffer = audio_data[-overlap_bytes:]
            else:
                overlap_buffer = b""
            
            # Convert to WAV and transcribe
            sr_audio = sr.AudioData(audio_data, source.SAMPLE_RATE, source.SAMPLE_WIDTH)
            now = datetime.now(timezone.utc)
            duration = len(sr_audio.frame_data) / (sr_audio.sample_rate * sr_audio.sample_width)
            wav_data = io.BytesIO(sr_audio.get_wav_data())
            
            with open(temp_file, 'w+b') as f:
                f.write(wav_data.read())
            
            try:
                if transcriber == "openai":
                   await openai_transcribe(now, duration)
                elif transcriber == "whisperx":
                    await whisperx_transcribe(now, duration)
                elif transcriber == "groq":
                    await groq_transcribe(now, duration)
                else:
                    raise ValueError(f"Invalid transcriber: {transcriber}")
            except Exception as e:
                logger.error(f"Error transcribing audio: {e}")
    
    except KeyboardInterrupt:
        logger.info("Stopping transcription...")
        running = False
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        transcription_data["status"] = "stopped"
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(transcription_data, f, ensure_ascii=False, indent=2)
        logger.info("Transcription stopped")

if __name__ == "__main__":
    # Connect to WebSocket server if target session ID is provided
    if args.target_sid:
        try:
            sio.connect(f"{server_url}", auth={'token': os.getenv('SECRET_KEY')})
            logger.info(f"Connected to WebSocket server at {server_url}")
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket server: {e}\nFalling back to HTTP POST mode")
    
    try:
        asyncio.run(transcribe_audio())
    finally:
        # Disconnect WebSocket when done
        if sio.connected:
            sio.disconnect()
