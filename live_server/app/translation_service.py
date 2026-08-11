import asyncio
import json
import re
from functools import lru_cache
from opencc import OpenCC
from .config import REALTIME_SETTINGS
from .database import rooms_collection, users_collection
from .logger_config import setup_logger, log_exception
from .socket_schema import is_finite_number
from .translators import get_translator

logger = setup_logger(__name__)
_cc_s2tw = OpenCC('s2tw')
_cc_tw2s = OpenCC('tw2s')


def _normalize_chinese_output(text: str, language: str) -> str:
    """Normalize Chinese translation output to match the target script.

    LLMs sometimes mix Simplified and Traditional Chinese; OpenCC enforces
    consistency based on the BCP-47 target language (e.g. zh-Hant-TW, zh-Hans-CN).
    """
    if not text or not language:
        return text
    lang = language.lower()
    if "hant" in lang or lang.endswith("-tw"):
        return _cc_s2tw.convert(text)
    if "hans" in lang or lang.endswith("-cn"):
        return _cc_tw2s.convert(text)
    return text

_KEYWORD_CAP = 30          # max keywords sent in prompts
_KEYWORD_STORE_CAP = _KEYWORD_CAP * 2  # store 2x so low-freq words can recover
# Skip translating a partial when its source text has grown by fewer than this
# many chars since the last dispatched partial. Re-translating tiny extensions
# wastes calls and is a major source of caption flicker; the LLM tends to
# rewrite the whole sentence even when only one word was added.
_MIN_PARTIAL_DELTA_CHARS = 4

# Outcomes of TranslationQueueManager.classify(): translate it, broadcast the source
# text alone (the flow shows every partial, translation stays on its own interval),
# or drop it because its segment is already closed.
GATE_DISPATCH = "dispatch"
GATE_FLOW_ONLY = "flow_only"
GATE_DROP = "drop"


def segment_start(transcription) -> float | None:
    """Identity of the segment a transcription belongs to, or None if unusable.

    scribe clears seg_start_time on every commit, so all partials of a segment
    and the commit that closes it share one start_time, and each segment scribe
    opens gets a strictly larger one. That makes start_time the segment id, and
    `a <= b` read as "segment a is the same as, or older than, segment b".
    """
    start = (transcription or {}).get("start_time")
    return float(start) if is_finite_number(start) else None


def carried_partial_result(cached_partial, transcription) -> dict:
    """The cached partial's result, but only if it belongs to `transcription`'s segment.

    An older segment's result must not carry over: as translator context it asks the
    LLM to continue a sentence that already ended, and on the broadcast path it would
    show that translation twice (parked line + live line).
    """
    cached_partial = cached_partial or {}
    if segment_start(cached_partial) != segment_start(transcription):
        return {}
    return cached_partial.get("result") or {}


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
# Session: string field helpers (Redis + MongoDB)
# ---------------------------------------------------------------------------

async def _get_session_string_field(redis_client, session_id, field: str) -> str:
    key = f"{field}:{session_id}"
    try:
        raw = await redis_client.get(key)
        if raw is not None:
            return raw.decode() if isinstance(raw, bytes) else raw
    except Exception as e:
        log_exception(logger, e, f"Redis get {field} error")

    value = ""
    try:
        room = await rooms_collection.find_one({"sid": session_id}, {field: 1})
        value = (room or {}).get(field) or ""
    except Exception as e:
        log_exception(logger, e, f"MongoDB get {field} error")

    try:
        await redis_client.set(key, value, ex=86400)
    except Exception:
        pass

    return value


async def _get_session_json_field(redis_client, session_id, field: str, normalize) -> list:
    """Read a JSON-encoded session field, normalizing whatever shape is stored.

    Redis is the live copy; MongoDB is the durable fallback that re-warms it.
    `normalize` owns format migration, so a legacy document is upgraded on read.
    """
    key = f"{field}:{session_id}"
    try:
        raw = await redis_client.get(key)
        if raw:
            return normalize(json.loads(raw))
    except Exception as e:
        log_exception(logger, e, f"Redis get {field} error")

    try:
        room = await rooms_collection.find_one({"sid": session_id}, {field: 1})
        if room and room.get(field) is not None:
            value = normalize(room[field])
            await redis_client.set(key, json.dumps(value), ex=86400)
            return value
    except Exception as e:
        log_exception(logger, e, f"MongoDB get {field} error")

    return []


async def _save_session_json_field(redis_client, session_id, field: str, value):
    try:
        await redis_client.set(f"{field}:{session_id}", json.dumps(value), ex=86400)
    except Exception as e:
        log_exception(logger, e, f"Redis set {field} error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, field, value))


async def _save_session_string_field(redis_client, session_id, field: str, value: str):
    key = f"{field}:{session_id}"
    try:
        if value:
            await redis_client.set(key, value, ex=86400)
        else:
            await redis_client.delete(key)
    except Exception as e:
        log_exception(logger, e, f"Redis set {field} error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, field, value))


# ---------------------------------------------------------------------------
# Session: scribe language
# ---------------------------------------------------------------------------

async def get_session_scribe_language(redis_client, session_id) -> str:
    return await _get_session_string_field(redis_client, session_id, "scribe_language")


async def save_session_scribe_language(redis_client, session_id, language: str):
    await _save_session_string_field(redis_client, session_id, "scribe_language", language)


# ---------------------------------------------------------------------------
# Session: per-account overrides (ai_provider, partial_interval)
# ---------------------------------------------------------------------------

async def _resolve_owner_overrides(session_id) -> dict:
    """Resolve the room owner's per-account override fields from the users
    collection. Returns {} when there is no room/owner."""
    try:
        room = await rooms_collection.find_one(
            {"sid": session_id}, {"admin_email": 1, "admin_uid": 1}
        )
        if not room:
            return {}
        email = room.get("admin_email")
        if not email and room.get("admin_uid"):
            doc = await users_collection.find_one({"user_uid": room["admin_uid"]}, {"email": 1})
            email = doc.get("email") if doc else None
        if not email:
            return {}
        user = await users_collection.find_one(
            {"email": email.lower()}, {"ai_provider": 1, "partial_interval": 1}
        )
        return user or {}
    except Exception as e:
        log_exception(logger, e, "Resolve owner overrides error")
        return {}


async def get_session_ai_provider(redis_client, session_id) -> str:
    """Return the owner's AI provider override for the session ('' = use default).
    Cached in Redis to keep the translation hot path off MongoDB."""
    key = f"ai_provider:{session_id}"
    try:
        cached = await redis_client.get(key)
        if cached is not None:
            return cached
    except Exception:
        pass
    provider = (await _resolve_owner_overrides(session_id)).get("ai_provider") or ""
    try:
        await redis_client.set(key, provider, ex=86400)
    except Exception:
        pass
    return provider


async def get_session_partial_interval(session_id) -> float | None:
    """Return the owner's PARTIAL_INTERVAL override (None = use config default)."""
    pi = (await _resolve_owner_overrides(session_id)).get("partial_interval")
    return pi if isinstance(pi, (int, float)) and not isinstance(pi, bool) else None


# ---------------------------------------------------------------------------
# Session: translate tone
# ---------------------------------------------------------------------------

async def get_session_translate_tone(redis_client, session_id) -> str:
    return await _get_session_string_field(redis_client, session_id, "translate_tone")


async def save_session_translate_tone(redis_client, session_id, tone: str):
    await _save_session_string_field(redis_client, session_id, "translate_tone", tone)


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
                if not locked:
                    locked = list(keywords.keys())
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

    defaults = _default_keywords()
    return defaults, list(defaults.keys())


async def save_locked_keywords(redis_client, session_id, locked_keywords: list[str]):
    """Persist the locked keywords list for a session."""
    try:
        await redis_client.set(f"locked_keywords:{session_id}", json.dumps(locked_keywords), ex=86400)
    except Exception as e:
        log_exception(logger, e, "Redis set locked_keywords error")
    asyncio.create_task(_save_room_field_to_mongo(session_id, "locked_keywords", locked_keywords))


# ---------------------------------------------------------------------------
# Session: text dictionary (user-defined direct replacements)
# ---------------------------------------------------------------------------

# Reserved target meaning "replace in the source transcript"; never a language code.
FLOW_TARGET = "flow"

def normalize_text_dictionary(data) -> list[dict[str, str]]:
    """Normalize stored text-dictionary data into a rule list.

    Each rule is ``{"from", "to", "target"}`` where ``target`` is
    ``FLOW_TARGET`` (replace in the source transcript) or a language code
    (replace in that language's translated output). Accepts the legacy
    ``{from: to}`` dict format and migrates it to flow-scoped rules.
    """
    out: list[dict[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str) and k:
                out.append({"from": k, "to": v, "target": FLOW_TARGET})
    elif isinstance(data, list):
        for e in data:
            if not isinstance(e, dict):
                continue
            src, dst, target = e.get("from"), e.get("to"), e.get("target", FLOW_TARGET)
            if isinstance(src, str) and isinstance(dst, str) and src and isinstance(target, str):
                out.append({"from": src, "to": dst, "target": target or FLOW_TARGET})
    return out


def split_text_dictionary(rules: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Partition rules into a flow map (source text) and per-language maps."""
    flow_map: dict[str, str] = {}
    by_lang: dict[str, dict[str, str]] = {}
    for r in rules:
        src = r.get("from") or ""
        dst = r.get("to") or ""
        target = r.get("target") or FLOW_TARGET
        if not src:
            continue
        if target == FLOW_TARGET:
            flow_map[src] = dst
        else:
            by_lang.setdefault(target, {})[src] = dst
    return flow_map, by_lang


async def get_text_dictionary(redis_client, session_id) -> list[dict[str, str]]:
    """Return the user-defined text replacement rules for a session."""
    return await _get_session_json_field(
        redis_client, session_id, "text_dictionary", normalize_text_dictionary
    )


async def get_language_maps(redis_client, session_id) -> dict[str, dict[str, str]]:
    """Per-language replacement maps for a session (flow/source rules dropped)."""
    _flow_map, by_lang = split_text_dictionary(await get_text_dictionary(redis_client, session_id))
    return by_lang


async def save_text_dictionary(redis_client, session_id, rules: list[dict[str, str]]):
    """Persist the user-defined text replacement rules for a session."""
    await _save_session_json_field(redis_client, session_id, "text_dictionary", rules)


def apply_text_dictionary(text: str, mapping: dict[str, str]) -> str:
    """Apply user-defined direct text replacements. Longer keys win on overlap."""
    if not text or not mapping:
        return text
    for src in sorted(mapping.keys(), key=len, reverse=True):
        if src:
            text = text.replace(src, mapping[src])
    return text


# ---------------------------------------------------------------------------
# Session: glossary (multilingual term table)
# ---------------------------------------------------------------------------
#
# A glossary entry is one term written in several languages, e.g.
#   {"en-US": "example", "zh-Hant-TW": "範例", "ja-JP": "例文"}
# Before translating into language L, every other spelling in the entry is
# swapped for L's spelling in the *source text handed to the LLM*, so the model
# sees the wanted term already in place. This is deliberately separate from the
# text dictionary: that one rewrites the translated output, which can never fire
# for a cross-language term (the English word is long gone by then), and running
# both over the same string would double-apply (`str.replace` is not idempotent).

GLOSSARY_MAX_ENTRIES = 200      # API cap; panel.html mirrors it to reject before POSTing
GLOSSARY_MAX_LANGS = 20         # spellings per entry
GLOSSARY_MAX_TERM_LEN = 200     # per language code and per spelling
_GLOSSARY_KEYWORD_CAP = 30      # max glossary spellings added to the correction prompt

# Shape of a BCP-47 code (en-US, zh-Hant-TW). Only applied to machine-generated
# entries: hand-written and imported ones are the user's business, and a typo
# there already shows up in the panel as an `unused` badge.
LANGUAGE_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,4}$")


def normalize_glossary(data) -> list[dict[str, str]]:
    """Normalize stored glossary data into a list of term entries.

    Each entry maps a language code to that language's spelling. Entries with
    fewer than two non-empty spellings are dropped: with only one there is
    nothing to replace.
    """
    out: list[dict[str, str]] = []
    if not isinstance(data, list):
        return out
    for e in data:
        if not isinstance(e, dict):
            continue
        entry = {
            k: v.strip()
            for k, v in e.items()
            if isinstance(k, str) and k and isinstance(v, str) and v.strip()
        }
        if len(entry) >= 2:
            out.append(entry)
    return out


def normalize_generated_glossary_entry(entry) -> dict[str, str]:
    """Clean one machine-generated entry down to what the glossary can store.

    Stricter than `normalize_glossary`, which trusts that a human wrote the file:
    keys here must look like language codes, because a model that finds nothing
    sometimes keys a spelling by the term itself. The caps that `POST /glossary`
    would reject outright are trimmed instead — a chatty answer should not make
    the user's whole save fail later.
    """
    cleaned = normalize_glossary([entry])
    if not cleaned:
        return {}
    out: dict[str, str] = {}
    for lang, spelling in cleaned[0].items():
        if not LANGUAGE_CODE_RE.match(lang) or len(spelling) > GLOSSARY_MAX_TERM_LEN:
            continue
        out[lang] = spelling
        if len(out) >= GLOSSARY_MAX_LANGS:
            break
    return out


async def get_glossary(redis_client, session_id) -> list[dict[str, str]]:
    """Return the multilingual glossary entries for a session."""
    return await _get_session_json_field(redis_client, session_id, "glossary", normalize_glossary)


async def save_glossary(redis_client, session_id, entries: list[dict[str, str]]):
    """Persist the multilingual glossary entries for a session."""
    await _save_session_json_field(redis_client, session_id, "glossary", entries)


def build_glossary_map(entries: list[dict[str, str]], language: str) -> dict[str, str]:
    """Source-spelling -> target-spelling map for one target language.

    Every other language's spelling in an entry becomes a source, so a mixed-
    language speaker is covered without having to declare which language the
    audio is in. First entry wins on a collision, so import order can't make an
    already-working term start resolving somewhere else.
    """
    mapping: dict[str, str] = {}
    for entry in entries:
        dst = entry.get(language)
        if not dst:
            continue
        for lang, src in entry.items():
            if lang == language or not src or src == dst:
                continue
            mapping.setdefault(src, dst)
    return mapping


def glossary_entry_is_inert(entry: dict[str, str]) -> bool:
    """True when an entry can never swap anything, so storing it is pointless.

    `build_glossary_map` skips a source spelling equal to its target, so an entry
    down to one spelling, or one whose languages all write the term the same way,
    produces an empty map for every target language.
    """
    return len({spelling.casefold() for spelling in entry.values()}) < 2


def glossary_keywords(entries: list[dict[str, str]], cap: int = _GLOSSARY_KEYWORD_CAP) -> list[str]:
    """Every spelling in the glossary, deduped in entry order.

    Fed to the correction pass: a glossary swap is a literal match, so a name the
    ASR mangles ("Shaun Gow" for "Sean Gau") never matches and the term silently
    does nothing. Showing the corrector the wanted spellings gives it the chance
    to fix the name first, which is what the swap then keys off.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for term in entry.values():
            if term in seen:
                continue
            seen.add(term)
            out.append(term)
            if len(out) >= cap:
                return out
    return out


@lru_cache(maxsize=64)
def _compile_glossary(items: tuple[tuple[str, str], ...]):
    """Compile a glossary map into (pattern, casefolded lookup).

    Longest source first so `AI Agent` wins over `AI`. Sources that start/end
    with an ASCII alphanumeric get word-boundary lookarounds (`example` must not
    match inside `examples`); CJK has no word boundaries, so those stay plain
    substrings.
    """
    parts = []
    lookup: dict[str, str] = {}
    for src, dst in sorted(items, key=lambda kv: len(kv[0]), reverse=True):
        lookup.setdefault(src.casefold(), dst)
        p = re.escape(src)
        if src[0].isascii() and src[0].isalnum():
            p = r"(?<![A-Za-z0-9_])" + p
        if src[-1].isascii() and src[-1].isalnum():
            p = p + r"(?![A-Za-z0-9_])"
        parts.append(p)
    return re.compile("|".join(parts), re.IGNORECASE), lookup


def apply_glossary(text: str, mapping: dict[str, str]) -> str:
    """Replace glossary source spellings in `text` with the target spellings.

    One single-pass regex rather than repeated `str.replace`, so a replacement
    can never be re-matched by a later rule (`{A: B, B: C}` turns A into B, not C).
    """
    if not text or not mapping:
        return text
    # build_glossary_map walks the entries in a fixed order, so the plain items
    # tuple is already a stable cache key — no need to sort it on every partial.
    pattern, lookup = _compile_glossary(tuple(mapping.items()))
    return pattern.sub(lambda m: lookup.get(m.group(0).casefold(), m.group(0)), text)


# ---------------------------------------------------------------------------
# Keyword reranking (background task)
# ---------------------------------------------------------------------------

def rank_keywords(keywords: dict[str, int], locked_list: list[str], cap: int) -> dict[str, int]:
    """
    Order keywords pinned-first, then by score descending, capped to `cap`.
    Only the unpinned tail is trimmed, so a pin is never dropped by the cap.
    Keyed off locked_list rather than the score map so a freshly pinned keyword
    that has not been scored yet is restored instead of lost.
    """
    pinned = {kw: keywords.get(kw, 1) for kw in locked_list}
    unpinned = sorted(
        ((kw, v) for kw, v in keywords.items() if kw not in pinned),
        key=lambda x: x[1], reverse=True,
    )
    return {
        **dict(sorted(pinned.items(), key=lambda x: x[1], reverse=True)),
        **dict(unpinned[:max(0, cap - len(pinned))]),
    }


async def rerank_keywords(redis_client, session_id, extraction_context: dict[str, int], recent_text: str, provider: str | None = None):
    """
    Extract new special nouns/names from recent_text, then increment/decrement keyword
    counts by presence in text. `extraction_context` is the caller's keyword snapshot,
    used only to prime extraction; the state that gets rewritten is re-read from Redis.
    Locked keywords are always preserved: they never decay and are never trimmed.
    Runs as a fire-and-forget background task; result is saved to Redis.
    """
    translator = get_translator(provider)

    new_kws = []
    try:
        new_kws = await translator.extract_keywords(recent_text, extraction_context)
    except Exception as e:
        log_exception(logger, e, "Keyword extraction error")

    # Re-read instead of writing back the caller's snapshot: extraction and the
    # translation before it take seconds, and the panel may have added, removed
    # or pinned keywords meanwhile. Saving the stale snapshot would drop those.
    keywords, locked_list = await get_keywords_and_locked(redis_client, session_id)
    locked_set = set(locked_list)

    for kw in new_kws:
        if isinstance(kw, str) and kw not in keywords:
            keywords[kw] = 1

    recent_lower = recent_text.lower()
    for kw in list(keywords.keys()):
        if kw.lower() in recent_lower:
            keywords[kw] += 1
        elif kw not in locked_set:
            # Pinned keywords never decay: going unmentioned is not evidence
            # against a keyword the user explicitly asked us to keep.
            keywords[kw] -= 1

    kept = {kw: v for kw, v in keywords.items() if v > -100 or kw in locked_set}
    trimmed_final = rank_keywords(kept, locked_list, _KEYWORD_STORE_CAP)
    await save_current_keywords(redis_client, session_id, trimmed_final)
    logger.debug("keywords saved (%d, %d pinned): %s", len(trimmed_final), len(locked_list), trimmed_final)


# ---------------------------------------------------------------------------
# Main translation entry point
# ---------------------------------------------------------------------------

async def translate_transcription(session_id, data: dict, cached_data: dict, redis_client, skip_correction):
    """
    data: the new transcription segment, e.g. {"partial": True, "text": "..."}
    cached_data: the history `{"transcriptions": [...]}`
    """
    provider = await get_session_ai_provider(redis_client, session_id) or None
    translator = get_translator(provider)

    languages = await get_session_languages(redis_client, session_id)
    if not languages:
        return data

    partial = data.get("partial") is True
    text = data.get("text", None)
    if not text:
        return data

    (current_keywords, locked_list), tone, text_dict, scribe_language, glossary = await asyncio.gather(
        get_keywords_and_locked(redis_client, session_id),
        get_session_translate_tone(redis_client, session_id),
        get_text_dictionary(redis_client, session_id),
        get_session_scribe_language(redis_client, session_id),
        get_glossary(redis_client, session_id),
    )
    flow_map, lang_maps = split_text_dictionary(text_dict)
    if flow_map:
        text = apply_text_dictionary(text, flow_map)
        data["text"] = text
    ranked = rank_keywords(current_keywords, locked_list, _KEYWORD_CAP)
    keywords_str = ', '.join(ranked)
    # Only the corrector gets the glossary spellings: by translation time the term
    # has already been swapped into the text, so repeating the other languages'
    # spellings there would just be prompt noise.
    correct_keywords = ', '.join(
        list(ranked) + [t for t in glossary_keywords(glossary) if t not in ranked]
    )

    translated_lists = {language: [] for language in languages}
    for transcription in cached_data.get("transcriptions", []):
        if "result" in transcription:
            translated_dict = transcription["result"].get("translated", {})
            for language in languages:
                translated_lists[language].append(translated_dict.get(language, ""))

    translated_context = {lang: ' '.join(translated_lists[lang]) for lang in languages}
    # Previous partial's result, shared by the correction and translation stages so
    # both keep the in-progress line consistent instead of re-editing it on each
    # partial.
    partial_result = carried_partial_result(cached_data.get("partial"), data)
    prev_corrected = partial_result.get("corrected", "")

    result = {"corrected": text}

    # 1. Correction
    try:
        if not skip_correction:
            result["corrected"] = await translator.correct(
                text=text,
                prev_corrected=prev_corrected,
                keywords=correct_keywords,
            )
        else:
            result["corrected"] = text.strip()
    except Exception as e:
        log_exception(logger, e, "Correction error")

    # The correction LLM can reintroduce Simplified characters even when scribe
    # already handed us Traditional; re-apply the same s2tw gate scribe uses so
    # the flow (source) panel stays consistent downstream (translate + rerank).
    if scribe_language.startswith("zh"):
        result["corrected"] = _cc_s2tw.convert(result["corrected"])

    # 2. Parallel translations
    translated = {}

    async def _translation_worker(language):
        lang_map = lang_maps.get(language)
        pt_trans = partial_result.get("translated", {}).get(language, "")
        lang_context = translated_context[language]
        # Apply language-scoped replacements to the prior committed context and
        # previous partial too, so the LLM sees consistent terms (and won't
        # "undo" the replacement) and the fallback inherits the mapped text.
        if lang_map:
            pt_trans = apply_text_dictionary(pt_trans, lang_map)
            lang_context = apply_text_dictionary(lang_context, lang_map)
        # Glossary swaps happen only on the text handed to the LLM: the source
        # transcript (data["text"]) and the shared corrected line stay untouched,
        # so the flow panel and keyword reranking still see what was actually said.
        src_text = apply_glossary(result['corrected'], build_glossary_map(glossary, language))
        try:
            out = await translator.translate(
                text=src_text,
                language=language,
                context=lang_context,
                prev_translation=pt_trans,
                keywords=keywords_str,
                tone=tone,
                commit=not partial,
            )
        except Exception as e:
            log_exception(logger, e, f"Translation error for {language}")
            out = None
        if out is None:
            # Invariant: must not write source-language text into translated[language].
            # Fall back to the last partial translation; for a committed segment with
            # no prior partial this is empty, which the viewer renders as a gap marker
            # rather than dropping the line entirely.
            if not partial:
                logger.warning(
                    "Commit translation unrecovered for %s (start=%s); storing empty",
                    language, data.get("start_time"),
                )
            translated[language] = pt_trans or ""
            return
        out_text = _normalize_chinese_output(out, language)
        if lang_map:
            out_text = apply_text_dictionary(out_text, lang_map)
        translated[language] = out_text

    await asyncio.gather(*[_translation_worker(lang) for lang in languages])

    if not partial and languages:
        asyncio.create_task(
            rerank_keywords(redis_client, session_id, current_keywords, result["corrected"], provider)
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
        self._pending_partial = None  # latest partial waiting for in-flight to finish
        self.commit_queue = asyncio.Queue(maxsize=self._COMMIT_QUEUE_MAXSIZE)
        # Most recently dispatched partial. Its text gates tiny extensions that would
        # only cause LLM rewrites without giving the reader new content; its segment
        # decides whether a commit gets to drop the claim.
        self._claimed_partial = None
        # Segment of the partial currently translating, so a commit tears down only
        # partial work it actually supersedes instead of killing the live segment's.
        self._inflight_partial_start: float | None = None
        # Segment of the newest commit accepted into the queue: everything at or
        # before it is superseded. Partials for *later* segments keep flowing while
        # that commit translates — a commit can hold the queue for tens of seconds
        # (correct() plus _COMMIT_RETRIES worth of translate()), and parking every
        # partial behind it froze the caption on a segment scribe had already closed.
        self._committed_through = float("-inf")
        self.is_running = False
        self.task = None

    async def start(self):
        self.is_running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.is_running = False
        self._pending_partial = None
        self._claimed_partial = None
        self._inflight_partial_start = None
        self._committed_through = float("-inf")
        partial_task, self.partial_task = self.partial_task, None
        if partial_task:
            partial_task.cancel()
            try:
                await partial_task
            except (asyncio.CancelledError, Exception):
                pass
        worker_task, self.task = self.task, None
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass

        # A stopped manager is never restarted. Release every queued item's
        # transcription context immediately instead of retaining up to 50 copies
        # until the manager itself is garbage-collected. The worker is stopped first,
        # so no coroutine can race this drain or consume an item between get/task_done.
        while True:
            try:
                self.commit_queue.get_nowait()
                self.commit_queue.task_done()
            except asyncio.QueueEmpty:
                break

    def _superseded(self, start: float | None) -> bool:
        """True when a commit we already accepted closed the segment at `start`."""
        return start is not None and start <= self._committed_through

    def _stale(self, sync_data) -> bool:
        """True when this transcription's segment has already been committed."""
        return self._superseded(segment_start(sync_data))

    def _claimed_text(self) -> str:
        return (self._claimed_partial or {}).get("text", "") or ""

    def classify(self, sync_data):
        """Gate a transcription before the caller fetches its cached context.

        Returns one of GATE_DISPATCH / GATE_FLOW_ONLY / GATE_DROP. Commits always
        dispatch. Call it exactly once per transcription, immediately before put():
        on GATE_DISPATCH it *claims* the partial slot, so put() only re-checks what
        can change across the caller's cache fetch. Staying synchronous is what makes
        that safe — the check and the claim happen in one event-loop step, so two
        concurrent partials can't both pass on the same text.
        """
        if sync_data.get("partial") is not True:
            return GATE_DISPATCH
        # Drop partials for a segment scribe has already closed; the broadcast path
        # would reject them again as skip_older_partial.
        if self._stale(sync_data):
            return GATE_DROP
        # scribe already decided this one lands inside its partial_interval.
        if sync_data.get("flow_only"):
            return GATE_FLOW_ONLY
        # Throttle by content delta: don't translate partials whose source text grew
        # by fewer than _MIN_PARTIAL_DELTA_CHARS chars. A shrinking text
        # (negative delta) means ASR corrected itself — always pass that
        # through since it's a meaningful change worth re-translating.
        new_text = sync_data.get("text", "") or ""
        delta = len(new_text) - len(self._claimed_text())
        if 0 <= delta < _MIN_PARTIAL_DELTA_CHARS:
            return GATE_FLOW_ONLY
        self._claimed_partial = sync_data
        return GATE_DISPATCH

    async def put(self, session_id, sync_data, cached_data, redis_client):
        item = (session_id, sync_data, cached_data, redis_client)
        if sync_data.get("partial") is True:
            # classify() already gated this partial and claimed the slot for its
            # text; anything claimed since means this one is stale. The caller awaits a
            # cache fetch between the two calls and scribe_manager fires those callbacks
            # concurrently, so a slow fetch can land here after a newer partial's and
            # would otherwise push the older text out as the newest one.
            if (sync_data.get("text", "") or "") != self._claimed_text():
                return
            # The commit closing this segment can also land in that same window, and
            # only put() sees it. Re-check rather than spend an LLM call on text the
            # broadcast path would just reject as skip_older_partial.
            if self._stale(sync_data):
                return
            if self.partial_task and not self.partial_task.done():
                # Replace pending slot with the latest partial; it will be
                # dispatched as soon as the in-flight translation finishes.
                self._pending_partial = item
            else:
                self._inflight_partial_start = segment_start(sync_data)
                self.partial_task = asyncio.create_task(self._process_partial(*item))
        else:
            start = segment_start(sync_data)
            if start is not None:
                self._committed_through = max(self._committed_through, start)
            # Tear down only the partial work this commit supersedes; partials for the
            # segment scribe opened after it have to survive.
            if (self.partial_task and not self.partial_task.done()
                    and self._superseded(self._inflight_partial_start)):
                self.partial_task.cancel()
            if self._pending_partial is not None and self._stale(self._pending_partial[1]):
                self._pending_partial = None
            if self._stale(self._claimed_partial):
                self._claimed_partial = None
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
                await self._process(*item)
                self.commit_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_exception(logger, e, "Queue loop error")
            await asyncio.sleep(0.01)

    async def _process_partial(self, session_id, sync_data, cached_data, redis_client):
        completed = await self._process(session_id, sync_data, cached_data, redis_client)
        # Dispatch the next queued partial if one arrived while we were in-flight,
        # unless a commit has since closed its segment.
        pending, self._pending_partial = self._pending_partial, None
        self._inflight_partial_start = None
        if pending is not None and not self._stale(pending[1]):
            # pending was snapshotted while this request was still in flight, so its
            # cached partial is one translation behind. Hand it the result we just
            # produced directly; waiting for the callback to persist it would race
            # the next dispatch and an extra Redis read would not fix that race.
            if completed and segment_start(completed) == segment_start(pending[1]):
                sid, data, context, redis = pending
                pending = (sid, data, {**context, "partial": completed}, redis)
            self._inflight_partial_start = segment_start(pending[1])
            self.partial_task = asyncio.create_task(self._process_partial(*pending))

    async def _process(self, session_id, sync_data, cached_data, redis_client):
        try:
            result_data = await translate_transcription(
                session_id, sync_data, cached_data, redis_client, skip_correction=REALTIME_SETTINGS.get('SKIP_CORRECTION', False)
            )
            # The partial key is cleared by the callback's broadcast path, which owns
            # it and holds the per-session lock (see _should_delete_partial).
            asyncio.create_task(self.callback(session_id, result_data))
            return result_data
        except asyncio.CancelledError:
            logger.debug(f"Translation task cancelled for session {session_id}")
        except Exception as e:
            log_exception(logger, e, f"Process translation error for session {session_id}")
