import json
import re
import asyncio
import httpx
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

_KEYWORD_CAP = 30         # max keywords sent in prompts
_KEYWORD_STORE_CAP = _KEYWORD_CAP * 2  # store 2x so low-freq words can recover
_RETRY_DELAYS = [0.5, 1.0, 2.0]
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


async def save_current_keywords(redis_client, session_id, keywords: dict[str, int]):
    try:
        await redis_client.set(f"keywords:{session_id}", json.dumps(keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set keywords error")



def _default_keywords() -> dict[str, int]:
    common_prompt = REALTIME_SETTINGS.get('COMMON_PROMPT', '')
    return {k.strip(): 1 for k in common_prompt.split(',') if k.strip()}


async def get_keywords_and_locked(redis_client, session_id) -> tuple[dict[str, int], list[str]]:
    """Fetch current keywords and locked keywords in a single Redis round-trip via mget."""
    try:
        kw_raw, locked_raw = await redis_client.mget(
            f"keywords:{session_id}",
            f"locked_keywords:{session_id}",
        )
        locked = json.loads(locked_raw) if locked_raw else []
        if kw_raw:
            data = json.loads(kw_raw)
            keywords = data if isinstance(data, dict) else {kw: 1 for kw in data if isinstance(kw, str)}
        else:
            keywords = _default_keywords()
        return keywords, locked
    except Exception as e:
        log_exception(logger, e, "Redis mget keywords error")
        return _default_keywords(), []


async def save_locked_keywords(redis_client, session_id, locked_keywords: list[str]):
    """Persist the locked keywords list for a session."""
    try:
        await redis_client.set(f"locked_keywords:{session_id}", json.dumps(locked_keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set locked_keywords error")


async def rerank_keywords(redis_client, session_id, keywords: dict[str, int], locked_list: list[str], recent_text: str):
    """
    Extract new special nouns/names from recent_text, then increment/decrement keyword
    counts by presence in text. Locked keywords are always preserved at the front.
    Runs as a fire-and-forget background task; result is saved to Redis.
    """
    provider_cfg = get_provider_config()
    api_key = REALTIME_SETTINGS.get(provider_cfg["api_key_setting"])
    if not api_key:
        return

    AI_MODEL = REALTIME_SETTINGS.get("AI_MODEL", provider_cfg["default_model"])
    locked_set = set(locked_list)

    try:
        extract_body = {
            "model": AI_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "developer",
                 "content": "If there are special nouns or names in the provided text, add them to the special_keywords list.\nReturn in json format:\n{\"special_keywords\": []}"},
                {"role": "user", "content": f"reference keywords: {', '.join(keywords.keys())}\n\nText:\n{recent_text}"}
            ]
        }
        response = await async_chat_completion(extract_body)
        if response and response.status_code == 200:
            new_kws = json.loads(response.json()["choices"][0]["message"]["content"]).get("special_keywords", [])
            for kw in new_kws:
                if isinstance(kw, str) and kw not in keywords:
                    keywords[kw] = 1
    except Exception as e:
        log_exception(logger, e, "Keyword extraction error")

    recent_lower = recent_text.lower()
    for kw in list(keywords.keys()):
        if kw.lower() in recent_lower:
            keywords[kw] += 1
        else:
            keywords[kw] -= 1

    locked_kws = {kw: keywords[kw] for kw in keywords if kw in locked_set}
    unlocked_kws = {kw: v for kw, v in keywords.items() if kw not in locked_set and v > -100}
    final = {
        **dict(sorted(locked_kws.items(), key=lambda x: x[1], reverse=True)),
        **dict(sorted(unlocked_kws.items(), key=lambda x: x[1], reverse=True)),
    }
    trimmed_final = dict(list(final.items())[:_KEYWORD_STORE_CAP])
    await save_current_keywords(redis_client, session_id, trimmed_final)
    print("keywords saved: ", trimmed_final)


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
    current_keywords, locked_list = await get_keywords_and_locked(redis_client, session_id)
    # Pinned keywords always fill first; unpinned capped to remaining slots.
    locked_set = set(locked_list)
    sorted_kws = sorted(current_keywords, key=lambda k: current_keywords[k], reverse=True)
    pinned_kws = [kw for kw in sorted_kws if kw in locked_set]
    unpinned_kws = [kw for kw in sorted_kws if kw not in locked_set]
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

    # 2. Parallel: Translation
    translated = {}
    async def _translation_worker(language):
        pt_trans = cached_data.get("partial", {}).get("result", {}).get("translated", {}).get(language, "")
        prev_translation = pt_trans[-50:] if pt_trans else ""
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
                {"role": "user", "content": f"{(' '.join(context['translated'][language]))[-25:]}\n<translate_this>\n{result['corrected']}\n</translate_this>"}
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

    await asyncio.gather(*[_translation_worker(lang) for lang in languages])

    if not partial and languages:
        asyncio.create_task(rerank_keywords(redis_client, session_id, dict(current_keywords), locked_list, result["corrected"]))
        
    result["translated"] = translated
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
