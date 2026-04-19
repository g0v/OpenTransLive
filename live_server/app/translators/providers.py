"""OpenAI-compatible chat-completion backends.

All providers (Gemini, OpenAI, Groq, Cerebras) expose the same
``/chat/completions`` shape, so ``ChatCompletionTranslator`` implements the
shared transport, retry, and prompt logic once; concrete subclasses only pin
the endpoint, auth, and per-operation request params.
"""
import asyncio
import json
import re

from ..http_client import get_async_client, close_async_client
from ..logger_config import setup_logger, log_exception
from .base import BaseTranslator

logger = setup_logger(__name__)

_RETRY_DELAYS = [0.5, 1.0, 2.0]
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_CORRECT_PROMPT = (
    "Correct the user's text literally. No styling/summaries. \n"
    "Remove timecodes. Remove umms, ahs, and other filler words. \n"
    "Output ONLY the corrected text.\n\n"
    "Context: {keywords}"
)

_TRANSLATE_PROMPT = (
    "Translate <translate_this> to {language}.\n\n"
    "Rules:\n"
    "1. Literal only; no styling/summaries.\n"
    "2. Match <previous_translation> to minimize changes.\n"
    "3. If same language, fix typos only.\n"
    "4. Add punctuation.\n"
    "5. Output ONLY the processed translated text.\n\n"
    "<context>\n{keywords}\n</context>\n\n"
    "<previous_translation>\n{prev_translation}\n</previous_translation>"
)

_EXTRACT_KEYWORDS_PROMPT = (
    "If there are special nouns or names in the provided text, "
    "add them to the special_keywords list.\n"
    'Return in json format:\n{"special_keywords": []}'
)


class ChatCompletionTranslator(BaseTranslator):
    """Shared implementation for OpenAI-compatible chat-completion endpoints.

    Subclasses declare these class attributes:
        endpoint         - full chat completions URL
        api_key_setting  - REALTIME_SETTINGS key holding the bearer token
        system_role      - "system" or "developer" (provider dependent)
        correct_params   - request body fragment for correction calls
        translate_params - request body fragment for translation calls
        extract_params   - request body fragment for keyword extraction
    """
    endpoint: str
    api_key_setting: str
    system_role: str = "system"
    correct_params: dict
    translate_params: dict
    extract_params: dict

    def __init__(self, settings: dict):
        self._api_key = settings.get(self.api_key_setting)

    async def _chat(self, body: dict) -> dict | None:
        if not self._api_key:
            return None

        cls_name = type(self).__name__
        client = get_async_client()
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
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
                    "%s._chat attempt %d got %d, %s",
                    cls_name, attempt + 1, response.status_code,
                    "retrying" if delay != _RETRY_DELAYS[-1] else "giving up",
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
            return (
                self._message_text(result)
                .replace("<correct_this>", "")
                .replace("</correct_this>", "")
                .strip()
            )
        return text

    async def translate(
        self,
        text: str,
        language: str,
        context: str,
        prev_translation: str,
        keywords: str,
    ) -> str:
        body = {
            **self.translate_params,
            "messages": [
                {
                    "role": self.system_role,
                    "content": _TRANSLATE_PROMPT.format(
                        language=language,
                        keywords=keywords,
                        prev_translation=prev_translation,
                    ),
                },
                {
                    "role": "user",
                    "content": f"{context[-25:]}\n<translate_this>\n{text}\n</translate_this>",
                },
            ],
        }
        result = await self._chat(body)
        if result:
            raw = (
                self._message_text(result)
                .replace("<translate_this>", "")
                .replace("</translate_this>", "")
                .strip()
            )
            return re.sub(r"[\n\r]+", " ", raw)
        return text

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

    _MODEL = "gemini-3.1-flash-lite-preview"
    correct_params = {
        "model": _MODEL,
        "reasoning_effort": "minimal",
        "temperature": 0,
    }
    translate_params = {
        "model": _MODEL,
        "reasoning_effort": "minimal",
        "temperature": 0,
    }
    extract_params = {
        "model": _MODEL,
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }


class OpenAITranslator(ChatCompletionTranslator):
    endpoint = "https://api.openai.com/v1/chat/completions"
    api_key_setting = "OPENAI_API_KEY"
    system_role = "developer"

    correct_params = {
        "model": "gpt-5.4-nano",
        "max_completion_tokens": 300,
        "reasoning_effort": "none",
    }
    translate_params = {
        "model": "gpt-5.4-nano",
        "max_completion_tokens": 300,
        "reasoning_effort": "none",
    }
    extract_params = {
        "model": "gpt-5.4-mini",
        "response_format": {"type": "json_object"},
    }


class GroqTranslator(ChatCompletionTranslator):
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    api_key_setting = "GROQ_API_KEY"
    system_role = "system"

    _MODEL = "openai/gpt-oss-120b"
    correct_params = {
        "model": _MODEL,
        "reasoning_effort": "low",
    }
    translate_params = {
        "model": _MODEL,
        "reasoning_effort": "low",
    }
    extract_params = {
        "model": _MODEL,
        "response_format": {"type": "json_object"},
    }


class CerebrasTranslator(ChatCompletionTranslator):
    endpoint = "https://api.cerebras.ai/v1/chat/completions"
    api_key_setting = "CEREBRAS_API_KEY"
    system_role = "system"

    _MODEL = "gpt-oss-120b"
    correct_params = {
        "model": _MODEL,
        "reasoning_effort": "low",
    }
    translate_params = {
        "model": _MODEL,
        "reasoning_effort": "low",
    }
    extract_params = {
        "model": _MODEL,
        "response_format": {"type": "json_object"},
    }
