import os
import json
import logging
import asyncio
import httpx
from datetime import datetime
from .config import REALTIME_SETTINGS

logger = logging.getLogger(__name__)


_client: httpx.AsyncClient | None = None

def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    return _client

async def close_async_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

async def async_chat_completion(json_body):
    api_key = REALTIME_SETTINGS.get('OPENAI_API_KEY')
    if not api_key:
        return None
    
    client = get_async_client()
    try:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=json_body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
        )
        return response
    except Exception as e:
        logger.error(f"HTTP request error: {e}")
        return None

async def get_current_keywords(redis_client, session_id):
    try:
        keywords = await redis_client.get(f"keywords:{session_id}")
        if keywords:
            return json.loads(keywords)
    except Exception as e:
        logger.error(f"Redis get keywords error: {e}")
    
    common_prompt = REALTIME_SETTINGS.get('COMMON_PROMPT', '')
    default_keywords = [k.strip() for k in common_prompt.split(',') if k.strip()]
    return default_keywords

async def save_current_keywords(redis_client, session_id, keywords):
    try:
        await redis_client.set(f"keywords:{session_id}", json.dumps(keywords), ex=86400)
    except Exception as e:
        logger.error(f"Redis set keywords error: {e}")

async def translate_transcription(session_id, data: dict, cached_data: dict, redis_client, skip_correction=False):
    """
    data: the new transcription segment, e.g. {"partial": True, "result": {"corrected": "..."}}
    cached_data: the history `{"transcriptions": [...]}`
    """
    api_key = REALTIME_SETTINGS.get('OPENAI_API_KEY')
    languages_env = REALTIME_SETTINGS.get('TRANSLATE_LANGUAGES', '')
    if not api_key or not languages_env:
        return data

    languages = [language.strip() for language in languages_env.split(',') if language.strip()]
    if not languages:
        return data

    partial = data.get("partial") is True
    text = data.get("result", {}).get("corrected", "")
    if not text:
        return data

    AI_MODEL = REALTIME_SETTINGS.get("AI_MODEL", "gpt-4.1-mini")
    current_keywords = await get_current_keywords(redis_client, session_id)
    
    context = {
        "corrected": [],
        "translated": {language: [] for language in languages}
    }
    
    # Get last 3 transcriptions for context
    history = cached_data.get("transcriptions", [])[-3:]
    for transcription in history:
        if "result" in transcription:
            context["corrected"].append(transcription["result"].get("corrected", ""))
            translated_dict = transcription["result"].get("translated", {})
            for language in languages:
                context["translated"][language].append(translated_dict.get(language, ""))

    result = {
        "corrected": text,
        "special_keywords": [],
    }
    
    # Correction and Keyword extraction (if not partial) can actually start together if keywords are from raw text, 
    # but usually we want keywords from corrected text. 
    # However, translation MUST wait for correction.
    
    # 1. Correction
    try:
        if not skip_correction:
            # Correct text
            json_body = {
                "model": AI_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "developer", "content": f"This is a transcription about:\n{', '.join(current_keywords)}\n\nCorrect the text **only in <correct_this>** as \"corrected text\" according to the reference and context.\nReturn only the corrected text, no any comment."},
                    {"role": "user", "content": f"{(' '.join(context['corrected']))[-50:]}\n<correct_this>\n{text}\n</correct_this>"}
                ]
            }
            response = await async_chat_completion(json_body)
            if response and response.status_code == 200:
                result["corrected"] = response.json()["choices"][0]["message"]["content"].replace('<correct_this>', '').replace('</correct_this>', '').strip()
        else:
            result["corrected"] = text
    except Exception as e:
        logger.error(f"Correction error: {e}")

    # 2. Parallel: Translation + Keyword Extraction
    translated = {}
    async def _translation_worker(language):
        prev_translation = ""
        if cached_data.get("partial") is True:
            pt_trans = cached_data["partial"].get("result", {}).get("translated", {}).get(language, "")
            if pt_trans:
                prev_translation = f"<prev_translation>\n{pt_trans}......\n</prev_translation>\n"

        json_body = {
            "model": AI_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "developer", 
                 "content": f"This is a transcription about:\n{', '.join(current_keywords)}\n\nRewrite the text **only in <translate_this>** into {language}, the sentence might not ended yet.\nReturn only the translated text, no any comment.\n{prev_translation}"},
                {"role": "user", "content": f"{(' '.join(context['translated'][language]))[-50:]}\n<translate_this>\n{result['corrected']}\n</translate_this>"}
            ]
        }
        try:
            response = await async_chat_completion(json_body)
            if response and response.status_code == 200:
                translated[language] = response.json()["choices"][0]["message"]["content"].replace('<translate_this>', '').replace('</translate_this>', '').strip()
            else:
                translated[language] = result['corrected']
        except Exception as e:
            logger.error(f"Translation error for {language}: {e}")
            translated[language] = result['corrected']

    async def _keyword_worker():
        json_body = {
            "model": AI_MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "developer", 
                 "content": "If there are very special keywords in the provide text, add them to the special_keywords list.\nreturn in json format:\n{\"special_keywords\": []}"},
                {"role": "user", "content": result["corrected"] }
            ]
        }
        try:
            response = await async_chat_completion(json_body)
            if response and response.status_code == 200:
                result["special_keywords"] = json.loads(response.json()["choices"][0]["message"]["content"]).get("special_keywords", [])
        except Exception as e:
            logger.error(f"Keywords extraction error: {e}")

    atasks = [_translation_worker(lang) for lang in languages]
    if not partial:
        atasks.append(_keyword_worker())
    
    if atasks:
        await asyncio.gather(*atasks)
        
    result["translated"] = translated
    
    if not partial:
        keywords = result.get("special_keywords", [])
        new_keywords_added = False
        for keyword in keywords:
            if isinstance(keyword, str) and keyword not in current_keywords:
                current_keywords.append(keyword)
                new_keywords_added = True
        
        if new_keywords_added:
            await save_current_keywords(redis_client, session_id, current_keywords)

    data["result"] = result
    return data

class TranslationQueueManager:
    def __init__(self, callback):
        self.callback = callback
        self.partial_task = None
        self.commit_queue = asyncio.Queue()
        self.is_running = False
        self.task = None

    async def start(self):
        self.is_running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.is_running = False
        if self.partial_task:
            self.partial_task.cancel()
        if self.task:
            self.task.cancel()

    async def put(self, session_id, sync_data, cached_data, redis_client):
        item = (session_id, sync_data, cached_data, redis_client)
        if self.partial_task and not self.partial_task.done():
            print(f"{session_id} partial update too fast, cancel previous", flush=True)
            self.partial_task.cancel()
        if sync_data.get("partial") is True:
            self.partial_task = asyncio.create_task(self._process(*item))
        else:
            await self.commit_queue.put(item)

    async def _loop(self):
        while self.is_running:
            try:
                item = await self.commit_queue.get()
                await self._process(*item)
                self.commit_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue loop error: {e}")
            await asyncio.sleep(0.01)

    async def _process(self, session_id, sync_data, cached_data, redis_client):
        try:
            result_data = await translate_transcription(session_id, sync_data, cached_data, redis_client, skip_correction=True)
            asyncio.create_task(self.callback(session_id, result_data))
        except asyncio.CancelledError:
            logger.debug(f"Translation task cancelled for session {session_id}")
        except Exception as e:
            logger.error(f"Process translation error: {e}", exc_info=True)
