from google_stt_v2 import GoogleSTTV2
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os
import asyncio
import logging
import opencc
import httpx
import socketio
import dotenv
import json
import argparse

dotenv.load_dotenv(override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
parser = argparse.ArgumentParser()
parser.add_argument("-t", "--target-sid", help="target session id", default=None)
args = parser.parse_args()
sio = socketio.Client()
converter = opencc.OpenCC("s2tw")

SERVER_URL = os.getenv("SERVER_ENDPOINT", 'http://localhost:5000')
API_ENDPOINT = f"http://{SERVER_URL}/api/sync/{args.target_sid}" if args.target_sid else None
AI_MODEL = os.getenv("AI_MODEL", "gpt-4.1-nano")

file_path = Path(f"output/{datetime.now().strftime('%Y-%m-%d')}/{datetime.now().strftime('%H-%M-%S')}.json")
file_path.parent.mkdir(parents=True, exist_ok=True)
transcription_data = {"transcriptions": [], "partial": None, "last_updated": None, "status": "running"}
init_time = datetime.now(timezone.utc)
languages = [language.strip() for language in os.getenv('TRANSLATE_LANGUAGES').split(',')]
with open(f"output/current_keywords.txt", "w", encoding="utf-8") as f:
    f.write('\n'.join(os.getenv('COMMON_PROMPT').split(',')))

partial_tasks = asyncio.Queue(maxsize=5)
commit_tasks = asyncio.Queue()

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

async def send_transcription_via_websocket(transcription_data):
    """Send transcription data via WebSocket"""
    if args.target_sid and sio.connected:
        try:
            # Add the session ID to the transcription data
            websocket_data = transcription_data.copy()
            websocket_data['id'] = args.target_sid
            sio.emit('sync', websocket_data)
        except Exception as e:
            logger.error(f"Error sending via WebSocket: {e}")
    elif API_ENDPOINT:
        # Fallback to HTTP POST if WebSocket is not available
        try:
            with httpx.Client() as client:
                response = client.post(
                    API_ENDPOINT,
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

async def handle_trascribed_text(data: dict, need_correct=True):
    """language code should be in IETF BCP 47 format"""
    partial = data.get("partial", False)
    
    try:                    
        context = {
            "corrected": [],
            "translated": {
                language: [] for language in languages
            }
        }
        if data["id"] > 3:
            for transcription in transcription_data["transcriptions"][data["id"] - min(3, data["id"]):data["id"]]:
                if "result" in transcription:
                    context["corrected"].append(transcription["result"]["corrected"])
                    for language in languages:
                        context["translated"][language].append(transcription["result"]["translated"][language])
                else:
                    context["corrected"].append(transcription["text"])
                    for language in languages:
                        context["translated"][language].append(transcription["text"])
        
        with open(f"output/current_keywords.txt", "r", encoding="utf-8") as f:
            current_keywords = f.read().split('\n')
        
        result = {
            "corrected": "corrected text",
            "special_keywords": [],
        }
        if need_correct:
            json_body = {
                "model": AI_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "developer", "content": f"""
                    This is a transcription about:
                    { ', '.join(current_keywords)}
                    
                    Correct the text **only in <correct_this>** as "corrected text" according to the reference and context.
                    Return only the corrected text, no any comment.
                    """},
                    {"role": "user", "content": f"""
                    {(' '.join(context['corrected']))[-100:]}
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
            result["corrected"] = response.json()["choices"][0]["message"]["content"].encode('utf-8').decode('utf-8').replace('<correct_this>', '').replace('</correct_this>', '').strip()
        else:
            result["corrected"] = data["text"]
        translated = {}
        atasks = []
        async def _worker(language):
            if transcription_data["partial"]:
                prev = f"""
                <prev_translation>
                {transcription_data["partial"]["result"]["translated"][language]}
                </prev_translation>
                """
            else:
                prev = None
            json_body = {
                "model": AI_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "developer", 
                     "content": f"""
                    This is a transcription about:
                    { ', '.join(current_keywords)}
                    
                    Rewrite the text **only in <translate_this>** into {language}, the sentence might not ended yet.                    
                    Return only the translated text, no any comment.
                    
                    {prev}
                    """},
                    {"role": "user", "content": f"""
                    {(' '.join(context['translated'][language]))[-100:]}
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
            translated[language] = response.json()["choices"][0]["message"]["content"].encode('utf-8').decode('utf-8').replace('<translate_this>', '').replace('</translate_this>', '').strip()
        
        async def _worker2():
            _result = {
                "special_keywords": []
            }
            json_body = {
                "model": AI_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "developer", 
                     "content": f"""If there are very special keywords in the provide text, add them to the special_keywords list.
                     return in json format:
                     {_result}
                     """},
                    {"role": "user", "content": result["corrected"] }
                ]
            }
            response = await async_chat_completion(json_body)
            if response.status_code != 200:
                raise Exception(response.text)
            result["special_keywords"] = json.loads(response.json()["choices"][0]["message"]["content"].encode('utf-8').decode('utf-8'))["special_keywords"]
            
        for language in languages:
            atasks.append(_worker(language))
            
        if not partial:
            atasks.append(_worker2())
        
        await asyncio.gather(*atasks)
            
        result["translated"] = translated
        if partial:
            result["partial"] = True
        else:
            keywords = result["special_keywords"]
            # Add new keywords
            for keyword in keywords:
                if keyword not in current_keywords:
                    current_keywords.append(keyword)
            # Write back to file
            with open(f"output/current_keywords.txt", "w", encoding="utf-8") as f:
                f.write('\n'.join(current_keywords))
            transcription_data["transcriptions"][data["id"]]["result"] = result
        
        data["result"] = result
        data["partial"] = partial
        logger.info(f"{data['start_time']} - {result}")
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(transcription_data, f, ensure_ascii=False, indent=2)
        return data
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Error translating text: {str(e)}")
        raise e
            
async def main():
    async def on_transcript(transcription, partial=False):
        transcription["id"] = len(transcription_data["transcriptions"])
        logger.info(f"Received {transcription}")
        if partial:
            try:
                partial_tasks.put_nowait(asyncio.create_task(handle_trascribed_text(transcription)))
            except asyncio.QueueFull:
                logger.warning("Tasks queue is full!! Translate is slower than transcribe, drop.")
            except Exception as e:
                logger.error(f"error: {e}")
                
        else:
            while not partial_tasks.empty():
                _t: asyncio.Task = await partial_tasks.get()
                _t.cancel()
                
            transcription_data["transcriptions"].append(transcription)
            await commit_tasks.put(asyncio.create_task(handle_trascribed_text(transcription)))
        
        await asyncio.sleep(0.01)
        transcription_data["last_updated"] = datetime.now().isoformat()        

    async def senderLoop():
        logger.info("start sender loop")
        while True:
            await asyncio.sleep(0.01)
            try:
                next: asyncio.Task = commit_tasks.get_nowait()
                logger.info("commit task get")
            except asyncio.QueueEmpty:
                try:
                    next: asyncio.Task = partial_tasks.get_nowait()
                    logger.info("partial task get")
                except asyncio.QueueEmpty:
                    continue
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"error: {e}")
            if next:
                result = await next
                
                if result["partial"]:
                    transcription_data["partial"] = result
                else:
                    transcription_data["partial"] = None
                
                await send_transcription_via_websocket(result)
            
    # Connect to WebSocket server if target session ID is provided
    if args.target_sid:
        try:
            sio.connect(f"{SERVER_URL}", auth={'token': os.getenv('SECRET_KEY')})
            logger.info(f"Connected to WebSocket server at {SERVER_URL}")
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket server: {e}\nFalling back to HTTP POST mode")
    
    try:
        stt = GoogleSTTV2(language_code=languages, callback=on_transcript)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(stt.run())
            tg.create_task(senderLoop())
                
    finally:
        # Disconnect WebSocket when done
        if sio.connected:
            sio.disconnect()
            
if __name__ == "__main__":
    asyncio.run(main())