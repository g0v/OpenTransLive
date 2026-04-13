import asyncio
import json
from .config import REALTIME_SETTINGS
from .database import rooms_collection
from .http_client import get_async_client, close_async_client  # re-exported for callers
from .logger_config import setup_logger, log_exception
from .translators import get_translator

logger = setup_logger(__name__)

_KEYWORD_CAP = 30          # max keywords sent in prompts
_KEYWORD_STORE_CAP = _KEYWORD_CAP * 2  # store 2x so low-freq words can recover


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

async def _save_room_field_to_mongo(session_id: str, field: str, value):
    try:
        await rooms_collection.update_one({"sid": session_id}, {"$set": {field: value}})
    except Exception as e:
        log_exception(logger, e, f"MongoDB set {field} error")


# ---------------------------------------------------------------------------
# Session: languages
# ---------------------------------------------------------------------------

async def get_session_languages(redis_client, session_id) -> list[str]:
    """Return translate languages for a session, falling back to MongoDB then config."""
    try:
        raw = await redis_client.get(f"languages:{session_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        log_exception(logger, e, "Redis get languages error")

    try:
        room = await rooms_collection.find_one({"sid": session_id}, {"languages": 1})
        if room and room.get("languages"):
            langs = room["languages"]
            await redis_client.set(f"languages:{session_id}", json.dumps(langs), ex=86400)
            return langs
    except Exception as e:
        log_exception(logger, e, "MongoDB get languages error")

    languages_env = REALTIME_SETTINGS.get('TRANSLATE_LANGUAGES', '')
    return [lang.strip() for lang in languages_env.split(',') if lang.strip()]


async def save_session_languages(redis_client, session_id, languages: list[str]):
    """Persist translate languages for a session in Redis and MongoDB."""
    try:
        await redis_client.set(f"languages:{session_id}", json.dumps(languages), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set languages error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, "languages", languages))


# ---------------------------------------------------------------------------
# Session: scribe language
# ---------------------------------------------------------------------------

async def get_session_scribe_language(redis_client, session_id) -> str:
    """Return forced detect language for Scribe, empty string means auto-detect."""
    try:
        raw = await redis_client.get(f"scribe_language:{session_id}")
        if raw:
            return raw.decode() if isinstance(raw, bytes) else raw
    except Exception as e:
        log_exception(logger, e, "Redis get scribe_language error")

    try:
        room = await rooms_collection.find_one({"sid": session_id}, {"scribe_language": 1})
        if room and room.get("scribe_language"):
            lang = room["scribe_language"]
            await redis_client.set(f"scribe_language:{session_id}", lang, ex=86400)
            return lang
    except Exception as e:
        log_exception(logger, e, "MongoDB get scribe_language error")

    return ""


async def save_session_scribe_language(redis_client, session_id, language: str):
    """Persist forced detect language for Scribe in Redis and MongoDB."""
    try:
        if language:
            await redis_client.set(f"scribe_language:{session_id}", language, ex=86400)
        else:
            await redis_client.delete(f"scribe_language:{session_id}")
    except Exception as e:
        log_exception(logger, e, "Redis set scribe_language error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, "scribe_language", language))


# ---------------------------------------------------------------------------
# Session: keywords
# ---------------------------------------------------------------------------

def _default_keywords() -> dict[str, int]:
    common_prompt = REALTIME_SETTINGS.get('COMMON_PROMPT', '')
    return {k.strip(): 1 for k in common_prompt.split(',') if k.strip()}


async def save_current_keywords(redis_client, session_id, keywords: dict[str, int]):
    try:
        await redis_client.set(f"keywords:{session_id}", json.dumps(keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set keywords error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, "keywords", keywords))


async def get_keywords_and_locked(redis_client, session_id) -> tuple[dict[str, int], list[str]]:
    """Fetch current keywords and locked keywords in a single Redis round-trip via mget."""
    try:
        kw_raw, locked_raw = await redis_client.mget(
            f"keywords:{session_id}",
            f"locked_keywords:{session_id}",
        )
        if kw_raw or locked_raw:
            locked = json.loads(locked_raw) if locked_raw else []
            if kw_raw:
                data = json.loads(kw_raw)
                keywords = data if isinstance(data, dict) else {kw: 1 for kw in data if isinstance(kw, str)}
            else:
                keywords = _default_keywords()
            return keywords, locked
    except Exception as e:
        log_exception(logger, e, "Redis mget keywords error")

    try:
        room = await rooms_collection.find_one({"sid": session_id}, {"keywords": 1, "locked_keywords": 1})
        if room and (room.get("keywords") or room.get("locked_keywords")):
            keywords_raw = room.get("keywords") or {}
            keywords = keywords_raw if isinstance(keywords_raw, dict) else {kw: 1 for kw in keywords_raw if isinstance(kw, str)}
            locked = room.get("locked_keywords") or []
            await redis_client.mset({
                f"keywords:{session_id}": json.dumps(keywords),
                f"locked_keywords:{session_id}": json.dumps(locked),
            })
            await redis_client.expire(f"keywords:{session_id}", 86400)
            await redis_client.expire(f"locked_keywords:{session_id}", 86400)
            return keywords, locked
    except Exception as e:
        log_exception(logger, e, "MongoDB get keywords error")

    return _default_keywords(), []


async def save_locked_keywords(redis_client, session_id, locked_keywords: list[str]):
    """Persist the locked keywords list for a session."""
    try:
        await redis_client.set(f"locked_keywords:{session_id}", json.dumps(locked_keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set locked_keywords error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, "locked_keywords", locked_keywords))


# ---------------------------------------------------------------------------
# Keyword reranking (background task)
# ---------------------------------------------------------------------------

async def rerank_keywords(redis_client, session_id, keywords: dict[str, int], locked_list: list[str], recent_text: str):
    """
    Extract new special nouns/names from recent_text, then increment/decrement keyword
    counts by presence in text. Locked keywords are always preserved at the front.
    Runs as a fire-and-forget background task; result is saved to Redis.
    """
    translator = get_translator()
    locked_set = set(locked_list)

    try:
        new_kws = await translator.extract_keywords(recent_text, keywords)
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


# ---------------------------------------------------------------------------
# Main translation entry point
# ---------------------------------------------------------------------------

async def translate_transcription(session_id, data: dict, cached_data: dict, redis_client, skip_correction):
    """
    data: the new transcription segment, e.g. {"partial": True, "text": "..."}
    cached_data: the history `{"transcriptions": [...]}`
    """
    translator = get_translator()

    languages = await get_session_languages(redis_client, session_id)
    if not languages:
        return data

    partial = data.get("partial") is True
    text = data.get("text", None)
    if not text:
        return data

    current_keywords, locked_list = await get_keywords_and_locked(redis_client, session_id)
    locked_set = set(locked_list)
    sorted_kws = sorted(current_keywords, key=lambda k: current_keywords[k], reverse=True)
    pinned_kws = [kw for kw in sorted_kws if kw in locked_set]
    unpinned_kws = [kw for kw in sorted_kws if kw not in locked_set]
    keywords_str = ', '.join(pinned_kws + unpinned_kws[:max(0, _KEYWORD_CAP - len(pinned_kws))])

    context = {
        "corrected": [],
        "translated": {language: [] for language in languages},
    }

    history = cached_data.get("transcriptions", [])[-3:]
    for transcription in history:
        if "result" in transcription:
            context["corrected"].append(transcription["result"].get("corrected", ""))
            translated_dict = transcription["result"].get("translated", {})
            for language in languages:
                context["translated"][language].append(translated_dict.get(language, ""))

    corrected_context = ' '.join(context['corrected'])
    translated_context = {lang: ' '.join(context['translated'][lang]) for lang in languages}

    result = {"corrected": text}

    # 1. Correction
    try:
        if not skip_correction:
            result["corrected"] = await translator.correct(
                text=text,
                context=corrected_context,
                keywords=keywords_str,
            )
        else:
            result["corrected"] = text.strip()
    except Exception as e:
        log_exception(logger, e, "Correction error")

    # 2. Parallel translations
    translated = {}

    async def _translation_worker(language):
        pt_trans = cached_data.get("partial", {}).get("result", {}).get("translated", {}).get(language, "")
        try:
            translated[language] = await translator.translate(
                text=result['corrected'],
                language=language,
                context=translated_context[language],
                prev_translation=pt_trans,
                keywords=keywords_str,
            )
        except Exception as e:
            log_exception(logger, e, f"Translation error for {language}")
            translated[language] = result['corrected']

    await asyncio.gather(*[_translation_worker(lang) for lang in languages])

    if not partial and languages:
        asyncio.create_task(
            rerank_keywords(redis_client, session_id, dict(current_keywords), locked_list, result["corrected"])
        )

    result["translated"] = translated
    data["result"] = result
    return data


# ---------------------------------------------------------------------------
# Queue manager
# ---------------------------------------------------------------------------

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
            result_data = await translate_transcription(
                session_id, sync_data, cached_data, redis_client, skip_correction=REALTIME_SETTINGS.get('SKIP_CORRECTION', False)
            )
            asyncio.create_task(self.callback(session_id, result_data))
        except asyncio.CancelledError:
            logger.debug(f"Translation task cancelled for session {session_id}")
        except Exception as e:
            log_exception(logger, e, f"Process translation error for session {session_id}")
