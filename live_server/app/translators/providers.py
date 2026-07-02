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

from ..config import load_secret_toml
from ..http_client import get_async_client, close_async_client
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

# Prompts and per-provider model settings live in app/secret/models.toml so
# they can be tuned without editing code. Deployers may drop in their own
# models.toml to override; when absent we fall back to the committed
# models.example.toml defaults. Loaded once at import.
_CONFIG = load_secret_toml("models", example_fallback=True)

_CORRECT_PROMPT = _CONFIG["prompts"]["correct"]
_TRANSLATE_PROMPT = _CONFIG["prompts"]["translate"]
_EXTRACT_KEYWORDS_PROMPT = _CONFIG["prompts"]["extract_keywords"]
_TONE_MAP = _CONFIG["tone_map"]
_PROVIDER_PARAMS = _CONFIG["providers"]


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

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if provider_key := getattr(cls, "provider_key", None):
            p = _PROVIDER_PARAMS[provider_key]
            cls.correct_params, cls.translate_params, cls.extract_params = (
                p["correct"], p["translate"], p["extract"],
            )

    def __init__(self, settings: dict):
        self._api_key = settings.get(self.api_key_setting)

    async def _chat(self, body: dict, max_retries: int = _DEFAULT_RETRIES) -> dict | None:
        if not self._api_key:
            return None

        cls_name = type(self).__name__
        client = get_async_client()
        for attempt in range(max_retries + 1):
            if attempt:
                # Exponential backoff with full jitter so concurrent callers
                # don't retry in lockstep against a rate-limited provider.
                cap = min(_MAX_RETRY_DELAY, _BASE_RETRY_DELAY * (2 ** (attempt - 1)))
                await asyncio.sleep(random.uniform(0, cap))
            try:
                response = await client.post(
                    self.endpoint,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    logger.error(
                        "%s._chat got %d: %s",
                        cls_name, response.status_code, response.text,
                    )
                    return None
                logger.warning(
                    "%s._chat attempt %d/%d got %d%s",
                    cls_name, attempt + 1, max_retries + 1, response.status_code,
                    ", giving up" if attempt == max_retries else ", retrying",
                )
            except Exception as e:
                log_exception(logger, e, f"HTTP request error in _chat (attempt {attempt + 1})")
                await close_async_client()
                client = get_async_client()
        return None

    @staticmethod
    def _message_text(response_json: dict) -> str:
        return response_json["choices"][0]["message"]["content"]

    async def correct(self, text: str, context: str, keywords: str) -> str:
        body = {
            **self.correct_params,
            "messages": [
                {"role": self.system_role, "content": _CORRECT_PROMPT.format(keywords=keywords)},
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


class OpenAITranslator(ChatCompletionTranslator):
    endpoint = "https://api.openai.com/v1/chat/completions"
    api_key_setting = "OPENAI_API_KEY"
    system_role = "developer"
    provider_key = "openai"


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
