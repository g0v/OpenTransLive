import asyncio
import json
import re

from ..http_client import get_async_client, close_async_client
from ..logger_config import setup_logger, log_exception
from .base import BaseTranslator

logger = setup_logger(__name__)

_RETRY_DELAYS = [0.5, 1.0, 2.0]
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


class GroqTranslator(BaseTranslator):
    def __init__(self, settings: dict):
        self._api_key = settings.get("GROQ_API_KEY")

    async def _chat(self, json_body: dict) -> dict | None:
        if not self._api_key:
            return None

        client = get_async_client()
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await client.post(
                    ENDPOINT,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    logger.error(
                        "GroqTranslator._chat got %d: %s",
                        response.status_code,
                        response.text,
                    )
                    return None
                logger.warning(
                    "GroqTranslator._chat attempt %d got %d, %s",
                    attempt + 1,
                    response.status_code,
                    "retrying" if delay != _RETRY_DELAYS[-1] else "giving up",
                )
            except Exception as e:
                log_exception(logger, e, f"HTTP request error in _chat (attempt {attempt + 1})")
                await close_async_client()
                client = get_async_client()
        return None

    def _message_text(self, response_json: dict) -> str:
        return response_json["choices"][0]["message"]["content"]

    async def correct(self, text: str, context: str, keywords: str) -> str:
        body = {
            "model": "openai/gpt-oss-120b",
            "max_tokens": 500,
            "reasoning_effort": "low",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Correct the user's text literally. No styling/summaries. "
                        "Output ONLY the corrected text.\n\n"
                        f"Context: {keywords}"
                    ),
                },
                {
                    "role": "user",
                    "content": text,
                },
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
            "model": "openai/gpt-oss-120b",
            "max_tokens": 500,
            "reasoning_effort": "low",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Translate <translate_this> to {language}.\n\n"
                        "Rules:\n"
                        "1. Literal only; no styling/summaries.\n"
                        "2. Match <previous_translation> to minimize changes.\n"
                        "3. If same language, fix typos only.\n"
                        "4. Add punctuation. Remove all timecodes.\n"
                        "5. Output ONLY the processed translated text.\n\n"
                        f"<context>\n{keywords}\n</context>\n\n"
                        f"<previous_translation>\n{prev_translation}\n</previous_translation>"
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
            "model": "openai/gpt-oss-120b",
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "If there are special nouns or names in the provided text, "
                        "add them to the special_keywords list.\n"
                        'Return in json format:\n{"special_keywords": []}'
                    ),
                },
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
