"""OpenAI-compatible chat-completion backends.

All providers (Gemini, OpenAI, Groq, Cerebras) expose the same
``/chat/completions`` shape, so ``ChatCompletionTranslator`` implements the
shared transport, retry, and prompt logic once; concrete subclasses only pin
the endpoint, auth, and per-operation request params.
"""
import asyncio
import json
import random
import re

import httpx

from ..config import load_secret_toml
from ..http_client import get_async_client, close_async_client, new_isolated_client
from ..logger_config import setup_logger, log_exception
from .base import BaseTranslator

logger = setup_logger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_BASE_RETRY_DELAY = 0.4   # first retry waits up to this many seconds
_MAX_RETRY_DELAY = 8.0    # cap on the exponential backoff window
# Per-operation retry budgets. Partials are on the hot path and must fail fast
# so the queue isn't blocked — a dropped partial is harmless because the client
# keeps showing the previous one. Commits are durable and latency-tolerant, so
# they retry harder: an unrecovered commit is stored with an empty translation
# that the viewer can only render as a gap.
_PARTIAL_RETRIES = 1
_COMMIT_RETRIES = 4
_DEFAULT_RETRIES = 3
# A grounded glossary lookup runs web searches before answering, so it needs far
# longer than the shared client's 10s hot-path budget.
_GLOSSARY_TIMEOUT = 60.0

# Prompts and per-provider model settings live in app/secret/models.toml so
# they can be tuned without editing code. Deployers may drop in their own
# models.toml to override; when absent we fall back to the committed
# models.example.toml defaults. Loaded once at import.
_CONFIG = load_secret_toml("models", example_fallback=True)

_CORRECT_PROMPT = _CONFIG["prompts"]["correct"]
_TRANSLATE_PROMPT = _CONFIG["prompts"]["translate"]
_EXTRACT_KEYWORDS_PROMPT = _CONFIG["prompts"]["extract_keywords"]
# Optional: a deployer's own models.toml predating the glossary feature has no
# such prompt, and load_secret_toml replaces the example wholesale rather than
# merging. Missing prompt/params disable generation (reported as 503) instead of
# crashing the import.
_GENERATE_GLOSSARY_PROMPT = _CONFIG["prompts"].get("generate_glossary")
_TONE_MAP = _CONFIG["tone_map"]
_PROVIDER_PARAMS = _CONFIG["providers"]


async def _post_with_retry(
    url: str, headers: dict, body: dict, max_retries: int, label: str,
    client: httpx.AsyncClient | None = None,
) -> dict | None:
    """POST JSON with full-jitter exponential backoff on retryable statuses.

    Shared by the chat-completions transport and the glossary lookups on Gemini's
    generateContent and OpenAI's Responses API, which need different URLs and
    auth headers but the same retry policy.

    With no *client* the shared hot-path client is used, and a transport error
    replaces it (a half-dead connection would otherwise fail every later call).
    A caller that passes its own client owns it: a slow request of its own must
    not tear down the connections the live translation path is using.
    """
    owned = client is None
    if owned:
        client = get_async_client()
    for attempt in range(max_retries + 1):
        if attempt:
            # Exponential backoff with full jitter so concurrent callers
            # don't retry in lockstep against a rate-limited provider.
            cap = min(_MAX_RETRY_DELAY, _BASE_RETRY_DELAY * (2 ** (attempt - 1)))
            await asyncio.sleep(random.uniform(0, cap))
        try:
            response = await client.post(url, json=body, headers=headers)
            if response.status_code == 200:
                return response.json()
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                logger.error("%s got %d: %s", label, response.status_code, response.text)
                return None
            logger.warning(
                "%s attempt %d/%d got %d%s",
                label, attempt + 1, max_retries + 1, response.status_code,
                ", giving up" if attempt == max_retries else ", retrying",
            )
        except Exception as e:
            log_exception(logger, e, f"HTTP request error in {label} (attempt {attempt + 1})")
            if owned:
                await close_async_client()
                client = get_async_client()
    return None


def _parse_glossary_reply(raw: str, term: str) -> dict[str, str] | None:
    """Parse a glossary lookup's reply into ``{language code: spelling}``.

    Neither backend can ask for a JSON-typed response while a web-search tool is
    attached, so the object arrives as prose and may be fenced.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except ValueError as e:
        log_exception(logger, e, f"generate_glossary JSON parse error for {term!r}")
        return None
    if not isinstance(data, dict):
        return None
    return {
        lang.strip(): spelling.strip()
        for lang, spelling in data.items()
        if isinstance(lang, str) and lang.strip()
        and isinstance(spelling, str) and spelling.strip()
    }


class ChatCompletionTranslator(BaseTranslator):
    """Shared implementation for OpenAI-compatible chat-completion endpoints.

    Subclasses declare these class attributes:
        endpoint         - full chat completions URL
        api_key_setting  - REALTIME_SETTINGS key holding the bearer token
        system_role      - "system" or "developer" (provider dependent)
        provider_key     - key into models.toml's [providers] table; the base
                           class derives the correct/translate/extract
                           request-body fragments from it at subclass creation.
    """
    endpoint: str
    api_key_setting: str
    system_role: str = "system"
    provider_key: str
    correct_params: dict
    translate_params: dict
    extract_params: dict
    glossary_params: dict | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if provider_key := getattr(cls, "provider_key", None):
            p = _PROVIDER_PARAMS[provider_key]
            cls.correct_params, cls.translate_params, cls.extract_params = (
                p["correct"], p["translate"], p["extract"],
            )
            # Optional, and only meaningful for backends that can search the
            # web; absent in a models.toml predating the glossary feature.
            cls.glossary_params = p.get("glossary")

    def __init__(self, settings: dict):
        self._api_key = settings.get(self.api_key_setting)

    async def _chat(self, body: dict, max_retries: int = _DEFAULT_RETRIES) -> dict | None:
        if not self._api_key:
            return None
        return await _post_with_retry(
            self.endpoint,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            body,
            max_retries,
            f"{type(self).__name__}._chat",
        )

    @staticmethod
    def _message_text(response_json: dict) -> str:
        return response_json["choices"][0]["message"]["content"]

    def supports_glossary(self) -> bool:
        return bool(self._api_key and _GENERATE_GLOSSARY_PROMPT and self.glossary_params)

    def _glossary_request(self, prompt: str) -> tuple[str, dict, dict]:
        """Return (url, headers, body) for a web-grounded glossary lookup."""
        raise NotImplementedError

    def _glossary_text(self, result: dict) -> str:
        """Pull the model's answer out of this provider's response envelope."""
        raise NotImplementedError

    async def generate_glossary(self, term: str, languages: list[str]) -> dict[str, str] | None:
        if not self.supports_glossary():
            return None
        url, headers, body = self._glossary_request(
            _GENERATE_GLOSSARY_PROMPT.format(term=term, languages=", ".join(languages))
        )
        # A client of its own: a web search takes far longer than the shared
        # hot-path client's 10s budget, and one user waiting on a lookup must not
        # slow down or recycle the connections carrying live translations.
        async with new_isolated_client(_GLOSSARY_TIMEOUT) as client:
            result = await _post_with_retry(
                url, headers, body,
                # Someone is watching a spinner, so fail fast rather than retry hard.
                _PARTIAL_RETRIES,
                f"{type(self).__name__}.generate_glossary",
                client=client,
            )
        if not result:
            return None
        try:
            raw = self._glossary_text(result)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            log_exception(logger, e, f"generate_glossary: unexpected {type(self).__name__} response")
            return None
        return _parse_glossary_reply(raw, term)

    async def correct(self, text: str, prev_corrected: str, keywords: str) -> str:
        body = {
            **self.correct_params,
            "messages": [
                {
                    "role": self.system_role,
                    "content": _CORRECT_PROMPT.format(keywords=keywords, prev_corrected=prev_corrected),
                },
                {"role": "user", "content": text},
            ],
        }
        result = await self._chat(body)
        if result:
            corrected = (
                (self._message_text(result) or "")
                .replace("<correct_this>", "")
                .replace("</correct_this>", "")
                .strip()
            )
            # An empty model output (e.g. reasoning consumed the token budget)
            # must not blank out the segment — fall back to the raw transcript.
            if corrected:
                return corrected
        return text

    async def translate(
        self,
        text: str,
        language: str,
        context: str,
        prev_translation: str,
        keywords: str,
        tone: str = "",
        commit: bool = False,
    ) -> str | None:
        tone_desc = _TONE_MAP.get(tone, tone) if tone else _TONE_MAP["fluent"]
        body = {
            **self.translate_params,
            "messages": [
                {
                    "role": self.system_role,
                    "content": _TRANSLATE_PROMPT.format(
                        language=language,
                        tone=tone_desc,
                        keywords=keywords,
                        prev_translation=prev_translation,
                    ),
                },
                {
                    "role": "user",
                    "content": f"<context>{context[-50:]}</context>\n<translate_this>\n{text}\n</translate_this>",
                },
            ],
        }
        result = await self._chat(
            body, max_retries=_COMMIT_RETRIES if commit else _PARTIAL_RETRIES
        )
        if result:
            raw = (
                (self._message_text(result) or "")
                .replace("<translate_this>", "")
                .replace("</translate_this>", "")
                .strip()
            )
            # Empty model output is treated as a failure (None) so the worker
            # falls back to the previous partial translation rather than storing
            # a blank gap.
            if raw:
                return re.sub(r"[\n\r]+", " ", raw)
        return None

    async def extract_keywords(
        self, text: str, existing_keywords: dict[str, int]
    ) -> list[str]:
        body = {
            **self.extract_params,
            "messages": [
                {"role": self.system_role, "content": _EXTRACT_KEYWORDS_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"reference keywords: {', '.join(existing_keywords.keys())}\n\n"
                        f"Text:\n{text}"
                    ),
                },
            ],
        }
        result = await self._chat(body)
        if result:
            try:
                return json.loads(self._message_text(result)).get("special_keywords", [])
            except Exception as e:
                log_exception(logger, e, "extract_keywords JSON parse error")
        return []

    async def close(self) -> None:
        await close_async_client()


class GeminiTranslator(ChatCompletionTranslator):
    endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    api_key_setting = "GEMINI_API_KEY"
    system_role = "developer"
    provider_key = "gemini"

    # Google Search grounding is not exposed through the OpenAI-compatible shim,
    # so glossary generation is the one call that goes to the native API.
    _NATIVE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def _glossary_request(self, prompt: str) -> tuple[str, dict, dict]:
        params = dict(self.glossary_params)
        model = params.pop("model")
        return (
            self._NATIVE_ENDPOINT.format(model=model),
            {"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                # Grounds the answer in live search results, which is the point:
                # a term worth adding to a glossary is usually too new or too
                # niche to be reliably in the model's weights.
                "tools": [{"google_search": {}}],
                "generationConfig": params,
            },
        )

    def _glossary_text(self, result: dict) -> str:
        parts = result["candidates"][0]["content"]["parts"]
        return "".join(p["text"] for p in parts if "text" in p)


class OpenAITranslator(ChatCompletionTranslator):
    endpoint = "https://api.openai.com/v1/chat/completions"
    api_key_setting = "OPENAI_API_KEY"
    system_role = "developer"
    provider_key = "openai"

    # The web_search tool lives on the Responses API. The search-tuned chat
    # models (gpt-*-search-*) reach the same web through /chat/completions, but
    # they dump the raw search results into the prompt — measured at ~32k input
    # tokens against ~9k here for the same lookup — and give no signal that a
    # search actually happened.
    _RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"

    def _glossary_request(self, prompt: str) -> tuple[str, dict, dict]:
        return (
            self._RESPONSES_ENDPOINT,
            {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            {**self.glossary_params, "tools": [{"type": "web_search"}], "input": prompt},
        )

    def _glossary_text(self, result: dict) -> str:
        # The output is a list of typed items — reasoning and web_search_call
        # entries alongside the message — so the text has to be picked out.
        output = result["output"]
        if not any(item.get("type") == "web_search_call" for item in output):
            logger.info("generate_glossary answered without searching")
        return "".join(
            content.get("text", "")
            for item in output if item.get("type") == "message"
            for content in item.get("content", [])
        )


class GroqTranslator(ChatCompletionTranslator):
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    api_key_setting = "GROQ_API_KEY"
    system_role = "system"
    provider_key = "groq"


class CerebrasTranslator(ChatCompletionTranslator):
    endpoint = "https://api.cerebras.ai/v1/chat/completions"
    api_key_setting = "CEREBRAS_API_KEY"
    system_role = "system"
    provider_key = "cerebras"
