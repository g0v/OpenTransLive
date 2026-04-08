from ast import Delete
import os
import json
import copy
import logging
import re
import asyncio
import httpx
from datetime import datetime
from .config import REALTIME_SETTINGS
from .logger_config import setup_logger, log_exception

logger = setup_logger(__name__)


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

_PROVIDER_CONFIG = {
    "gemini": {
        "provider": "gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "api_key_setting": "GEMINI_API_KEY",
        "default_model": "gemini-3.1-flash-lite-preview",
        "default_temp": 0
    },
    "openai": {
        "provider": "openai",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "api_key_setting": "OPENAI_API_KEY",
        "default_model": "gpt-4.1-mini",
        "default_temp": 0
    },
}

def get_provider_config():
    provider = REALTIME_SETTINGS.get("AI_PROVIDER", "gemini").lower()
    return _PROVIDER_CONFIG.get(provider, _PROVIDER_CONFIG["gemini"])

_RETRY_DELAYS = [0.5, 1.0, 2.0]

async def async_chat_completion(json_body: dict):
    provider_cfg = get_provider_config()
    api_key = REALTIME_SETTINGS.get(provider_cfg["api_key_setting"])
    if not api_key:
        return None
    json_body['temperature'] = provider_cfg["default_temp"]

    if "gpt-4.1" in provider_cfg["default_model"]:
        json_body.pop("reasoning_effort", None)

    client = get_async_client()
    last_response = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            response = await client.post(
                provider_cfg["endpoint"],
                json=json_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
            )
            if response.status_code == 200:
                return response
            last_response = response
            if response.status_code not in (429, 500, 502, 503, 504):
                print("response error", response.status_code, response.text, flush=True)
                return response
            logger.warning(
                "async_chat_completion attempt %d got %d, %s",
                attempt + 1, response.status_code,
                "retrying" if delay != _RETRY_DELAYS[-1] else "giving up"
            )
        except Exception as e:
            log_exception(logger, e, f"HTTP request error in async_chat_completion (attempt {attempt + 1})")
            last_response = None
    return last_response

async def get_session_languages(redis_client, session_id) -> list[str]:
    """Return translate languages for a session, falling back to config."""
    try:
        raw = await redis_client.get(f"languages:{session_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        log_exception(logger, e, "Redis get languages error")

    languages_env = REALTIME_SETTINGS.get('TRANSLATE_LANGUAGES', '')
    return [lang.strip() for lang in languages_env.split(',') if lang.strip()]


async def save_session_languages(redis_client, session_id, languages: list[str]):
    """Persist translate languages for a session in Redis."""
    try:
        await redis_client.set(f"languages:{session_id}", json.dumps(languages), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set languages error")


async def get_session_scribe_language(redis_client, session_id) -> str:
    """Return forced detect language for Scribe, empty string means auto-detect."""
    try:
        raw = await redis_client.get(f"scribe_language:{session_id}")
        if raw:
            return raw.decode() if isinstance(raw, bytes) else raw
    except Exception as e:
        log_exception(logger, e, "Redis get scribe_language error")
    return ""


async def save_session_scribe_language(redis_client, session_id, language: str):
    """Persist forced detect language for Scribe in Redis."""
    try:
        if language:
            await redis_client.set(f"scribe_language:{session_id}", language, ex=86400)
        else:
            await redis_client.delete(f"scribe_language:{session_id}")
    except Exception as e:
        log_exception(logger, e, "Redis set scribe_language error")


async def get_current_keywords(redis_client, session_id):
    try:
        keywords = await redis_client.get(f"keywords:{session_id}")
        if keywords:
            return json.loads(keywords)
    except Exception as e:
        log_exception(logger, e, "Redis get keywords error")

    common_prompt = REALTIME_SETTINGS.get('COMMON_PROMPT', '')
    default_keywords = [k.strip() for k in common_prompt.split(',') if k.strip()]
    return default_keywords

async def save_current_keywords(redis_client, session_id, keywords):
    try:
        await redis_client.set(f"keywords:{session_id}", json.dumps(keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set keywords error")


async def get_locked_keywords(redis_client, session_id) -> list[str]:
    """Return the list of locked (pinned) keywords for a session."""
    try:
        raw = await redis_client.get(f"locked_keywords:{session_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        log_exception(logger, e, "Redis get locked_keywords error")
    return []


async def get_keywords_and_locked(redis_client, session_id) -> tuple[list[str], list[str]]:
    """Fetch current keywords and locked keywords in a single Redis round-trip via mget."""
    try:
        kw_raw, locked_raw = await redis_client.mget(
            f"keywords:{session_id}",
            f"locked_keywords:{session_id}",
        )
        keywords = json.loads(kw_raw) if kw_raw else None
        locked = json.loads(locked_raw) if locked_raw else []
        if keywords is None:
            common_prompt = REALTIME_SETTINGS.get('COMMON_PROMPT', '')
            keywords = [k.strip() for k in common_prompt.split(',') if k.strip()]
        return keywords, locked
    except Exception as e:
        log_exception(logger, e, "Redis mget keywords error")
        common_prompt = REALTIME_SETTINGS.get('COMMON_PROMPT', '')
        return [k.strip() for k in common_prompt.split(',') if k.strip()], []


async def save_locked_keywords(redis_client, session_id, locked_keywords: list[str]):
    """Persist the locked keywords list for a session."""
    try:
        await redis_client.set(f"locked_keywords:{session_id}", json.dumps(locked_keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set locked_keywords error")


async def rerank_keywords(redis_client, session_id, keywords: list[str], recent_text: str):
    """
    Ask the LLM to re-rank keywords by relevance to the current transcript context.
    The most relevant keywords are moved to the front so that the prompt cap [:30]
    always includes the most important terms.
    Locked keywords are always preserved at the front and excluded from LLM reranking.
    Runs as a fire-and-forget background task; result is saved to Redis.
    """
    locked = await get_locked_keywords(redis_client, session_id)
    locked_set = set(locked)
    unlocked = [kw for kw in keywords if kw not in locked_set]

    if len(unlocked) < 10:
        # Not enough unlocked keywords to bother reranking; still preserve locked order
        if locked:
            merged = locked + [kw for kw in keywords if kw not in locked_set]
            await save_current_keywords(redis_client, session_id, merged)
        return

    provider_cfg = get_provider_config()
    api_key = REALTIME_SETTINGS.get(provider_cfg["api_key_setting"])
    if not api_key:
        return

    AI_MODEL = REALTIME_SETTINGS.get("AI_MODEL", provider_cfg["default_model"])
    numbered = "\n".join(f"{i+1}. {kw}" for i, kw in enumerate(unlocked))
    json_body = {
        "model": AI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "developer",
                "content": (
                    "You are managing a keyword list for a live transcription session. \n"
                    "1. Remove similar keywords.\n"
                    "2. Re-rank the provided keywords from most relevant/important to least, based on the recent transcript excerpt. \n\n"
                    "Return only JSON: {\"keywords\": [<ordered list of keyword strings>]}"
                )
            },
            {
                "role": "user",
                "content": f"Recent transcript:\n{recent_text[-200:]}\n\nKeywords to rank:\n{numbered}"
            }
        ]
    }
    try:
        response = await async_chat_completion(json_body)
        if response and response.status_code == 200:
            try:
                content = response.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                reranked = data.get("keywords", [])
            except (json.JSONDecodeError, KeyError):
                logger.error(f"Failed to parse re-ranked keywords from LLM response: {response.text}")
                return

            if isinstance(reranked, list) and len(reranked) > 0:
                # Locked keywords go first (in original locked order), then reranked unlocked ones
                final = locked + [kw for kw in reranked if kw not in locked_set]
                await save_current_keywords(redis_client, session_id, final)
                logger.debug(f"Keywords re-ranked for session {session_id}: {final}")
    except Exception as e:
        log_exception(logger, e, "Keyword re-ranking error")


async def translate_transcription(session_id, data: dict, cached_data: dict, redis_client, skip_correction=False):
    """
    data: the new transcription segment, e.g. {"partial": True, "text": "..."}
    cached_data: the history `{"transcriptions": [...]}`
    """
    provider_cfg = get_provider_config()
    api_key = REALTIME_SETTINGS.get(provider_cfg["api_key_setting"])
    if not api_key:
        return data

    languages = await get_session_languages(redis_client, session_id)
    if not languages:
        return data

    partial = data.get("partial") is True
    text = data.get("text", None)
    if not text:
        return data

    AI_MODEL = REALTIME_SETTINGS.get("AI_MODEL", provider_cfg["default_model"])
    # Single round-trip: fetch keywords and locked keywords together.
    current_keywords, locked_list = await get_keywords_and_locked(redis_client, session_id)
    # Cap keywords to avoid unbounded prompt growth.
    # Pinned keywords are always included first; remaining slots go to unpinned ones.
    _KEYWORD_CAP = 30
    locked_set = set(locked_list)
    pinned_kws = [kw for kw in current_keywords if kw in locked_set]
    unpinned_kws = [kw for kw in current_keywords if kw not in locked_set]
    keywords_str = ', '.join(pinned_kws + unpinned_kws[:max(0, _KEYWORD_CAP - len(pinned_kws))])

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
                "max_completion_tokens": 300,
                "reasoning_effort": "minimal",
                "messages": [
                    {"role": "developer", "content": f"This is a transcription about:\n{keywords_str}\n\nCorrect the text **only in <correct_this>** as \"corrected text\" according to the reference and context.\nReturn only the corrected text, no any comment."},
                    {"role": "user", "content": f"{(' '.join(context['corrected']))[-50:]}\n<correct_this>\n{text}\n</correct_this>"}
                ]
            }
            response = await async_chat_completion(json_body)
            if response and response.status_code == 200:
                result["corrected"] = response.json()["choices"][0]["message"]["content"].replace('<correct_this>', '').replace('</correct_this>', '').strip()
        else:
            result["corrected"] = text.strip()
    except Exception as e:
        log_exception(logger, e, "Correction error")

    # 2. Parallel: Translation + Keyword Extraction
    translated = {}
    async def _translation_worker(language):
        prev_translation = ""
        if cached_data.get("partial"):
            pt_trans = cached_data["partial"].get("result", {}).get("translated", {}).get(language, "")
            if pt_trans:
                pt_trans = pt_trans[-50:]
                prev_translation = pt_trans
        json_body = {
            "model": AI_MODEL,
            "max_completion_tokens": 300,
            "reasoning_effort": "minimal",
            "messages": [
                {
                    "role": "developer",
                    "content": f"""Context: This transcription is about {keywords_str}.
Task: Translate the text within <translate_this> into language {language}.

Constraints:
1. Strict Fidelity: Literal meaning only; no stylistic changes or summaries.
2. Minimal Edit: If languages match, only fix typos.
3. Format: Output ONLY processed text.
4. Punctuation: Add punctuation marks.

<previous_translation>
{prev_translation}
</previous_translation>
"""
                },
                {"role": "user", "content": f"{(' '.join(context['translated'][language]))[-50:]}\n<translate_this>\n{result['corrected']}\n</translate_this>"}
            ]
        }
        try:
            response = await async_chat_completion(json_body)
            _translated_text = ''
            if response and response.status_code == 200:
                _translated_text = response.json()["choices"][0]["message"]["content"].replace('<translate_this>', '').replace('</translate_this>', '').strip()
            else:
                _translated_text = result['corrected']
            
            translated[language] = re.sub(r'[\n\r]+', ' ',_translated_text)
        except Exception as e:
            log_exception(logger, e, f"Translation error for {language}")
            translated[language] = result['corrected']

    async def _keyword_worker():
        json_body = {
            "model": AI_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "developer", 
                 "content": "If there are special nouns or names in the provide text, add them to the special_keywords list.\nreturn in json format:\n{\"special_keywords\": []}"},
                {"role": "user", "content": result["corrected"] }
            ]
        }
        try:
            response = await async_chat_completion(json_body)
            if response and response.status_code == 200:
                result["special_keywords"] = json.loads(response.json()["choices"][0]["message"]["content"]).get("special_keywords", [])
        except Exception as e:
            log_exception(logger, e, "Keywords extraction error")

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
        
        # Re-rank in background if list is substantial, keeping top[:30] relevant
        if len(current_keywords) >= 10:
            history_context = " ".join(context["corrected"] + [result["corrected"]])
            asyncio.create_task(
                rerank_keywords(redis_client, session_id, current_keywords, history_context)
            )

    data["result"] = result
    return data

class TranslationQueueManager:
    _COMMIT_QUEUE_MAXSIZE = 50  # bound commit queue to prevent OOM under slow LLM

    def __init__(self, callback):
        self.callback = callback
        self.partial_task = None
        self.commit_queue = asyncio.Queue(maxsize=self._COMMIT_QUEUE_MAXSIZE)
        self._commit_in_flight = False
        self.is_running = False
        self.task = None

    async def start(self):
        self.is_running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.is_running = False
        if self.partial_task:
            self.partial_task.cancel()
            try:
                await self.partial_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def put(self, session_id, sync_data, cached_data, redis_client):
        item = (session_id, sync_data, cached_data, redis_client)
        if sync_data.get("partial") is True:
            if self._commit_in_flight or not self.commit_queue.empty():
                return
            if self.partial_task and not self.partial_task.done():
                print(f"{session_id} partial update too fast, skip it.", flush=True)
                return
            else:
                self.partial_task = asyncio.create_task(self._process(*item))
        else:
            if self.partial_task and not self.partial_task.done():
                self.partial_task.cancel()
            if self.commit_queue.full():
                try:
                    self.commit_queue.get_nowait()
                    self.commit_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                logger.warning(
                    f"[commit_queue] queue full, dropped oldest item "
                    f"for session {item[0]}"
                )
            await self.commit_queue.put(item)

    async def _loop(self):
        while self.is_running:
            try:
                item = await self.commit_queue.get()
                self._commit_in_flight = True
                await self._process(*item)
                self.commit_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_exception(logger, e, "Queue loop error")
            finally:
                self._commit_in_flight = False
            await asyncio.sleep(0.01)

    async def _process(self, session_id, sync_data, cached_data, redis_client):
        try:
            result_data = await translate_transcription(session_id, sync_data, cached_data, redis_client, skip_correction=True)
            asyncio.create_task(self.callback(session_id, result_data))

        except asyncio.CancelledError:
            logger.debug(f"Translation task cancelled for session {session_id}")
        except Exception as e:
            log_exception(logger, e, f"Process translation error for session {session_id}")
