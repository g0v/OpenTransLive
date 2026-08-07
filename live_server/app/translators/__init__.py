"""
Translator factory.

Usage
-----
    from .translators import get_translator

    translator = get_translator()
    corrected = await translator.correct(text, prev_corrected, keywords)

Adding a new backend
--------------------
1. Create a subclass of ``ChatCompletionTranslator`` (for OpenAI-compatible APIs)
   or ``BaseTranslator`` directly (for other shapes) in ``providers.py``.
2. Add an entry to ``_BACKENDS`` below.
3. Set ``AI_PROVIDER=<key>`` in your config.

Per-operation providers
-----------------------
Set ``CORRECT_PROVIDER`` and/or ``TRANSLATE_PROVIDER`` in REALTIME_SETTINGS to use
different backends for correction vs. translation.  Both fall back to ``AI_PROVIDER``
when not set, so the single-provider setup continues to work unchanged.

Example — Gemini for correction, OpenAI for translation::

    'AI_PROVIDER':        "gemini",   # default / fallback
    'CORRECT_PROVIDER':   "gemini",
    'TRANSLATE_PROVIDER': "openai",
"""

import asyncio
from typing import Callable

from ..logger_config import setup_logger
from .base import BaseTranslator
from .providers import CerebrasTranslator, GeminiTranslator, GroqTranslator, OpenAITranslator

logger = setup_logger(__name__)

_BACKENDS: dict[str, Callable[[dict], BaseTranslator]] = {
    "cerebras": CerebrasTranslator,
    "gemini": GeminiTranslator,
    "groq": GroqTranslator,
    "openai": OpenAITranslator,
}

AVAILABLE_PROVIDERS: list[str] = list(_BACKENDS.keys())

# Cache of translator instances keyed by provider name. The default (no
# per-account override) is stored under ``_DEFAULT_KEY`` and honors the
# AI_PROVIDER / CORRECT_PROVIDER / TRANSLATE_PROVIDER config split.
_DEFAULT_KEY = "__default__"
_instances: dict[str, BaseTranslator] = {}


class _CompositeTranslator(BaseTranslator):
    """Routes correct/extract_keywords to one backend and translate to another."""

    def __init__(self, correct_backend: BaseTranslator, translate_backend: BaseTranslator):
        self._correct = correct_backend
        self._translate = translate_backend

    async def correct(self, text: str, prev_corrected: str, keywords: str) -> str:
        return await self._correct.correct(text, prev_corrected, keywords)

    async def translate(
        self,
        text: str,
        language: str,
        context: str,
        prev_translation: str,
        keywords: str,
        tone: str = "",
        commit: bool = False,
    ) -> str:
        return await self._translate.translate(text, language, context, prev_translation, keywords, tone, commit=commit)

    async def extract_keywords(
        self, text: str, existing_keywords: dict[str, int]
    ) -> list[str]:
        return await self._correct.extract_keywords(text, existing_keywords)

    async def close(self) -> None:
        await asyncio.gather(self._correct.close(), self._translate.close())


def _resolve_backend(provider: str) -> Callable[[dict], BaseTranslator]:
    cls = _BACKENDS.get(provider)
    if cls is None:
        logger.warning("Unknown AI provider %r, falling back to GeminiTranslator", provider)
        return GeminiTranslator
    return cls


def _build_default_translator() -> BaseTranslator:
    """Build the default translator honoring the AI_PROVIDER / CORRECT_PROVIDER /
    TRANSLATE_PROVIDER config split (composite when correct and translate differ)."""
    from ..config import REALTIME_SETTINGS

    default = REALTIME_SETTINGS.get("AI_PROVIDER", "gemini").lower()
    correct_provider = REALTIME_SETTINGS.get("CORRECT_PROVIDER", default).lower()
    translate_provider = REALTIME_SETTINGS.get("TRANSLATE_PROVIDER", default).lower()

    correct_cls = _resolve_backend(correct_provider)
    translate_cls = _resolve_backend(translate_provider)

    if correct_provider == translate_provider:
        return correct_cls(REALTIME_SETTINGS)
    return _CompositeTranslator(
        correct_backend=correct_cls(REALTIME_SETTINGS),
        translate_backend=translate_cls(REALTIME_SETTINGS),
    )


def get_translator(provider: str | None = None) -> BaseTranslator:
    """Return a cached translator instance.

    When ``provider`` is given (a per-account override), returns a single-backend
    translator for that provider. When ``None``, returns the default translator
    which honors the CORRECT_PROVIDER / TRANSLATE_PROVIDER config split.
    Instances are cached per provider so concurrent sessions on different
    accounts reuse one backend each.
    """
    key = provider.lower() if provider else _DEFAULT_KEY
    inst = _instances.get(key)
    if inst is None:
        if key == _DEFAULT_KEY:
            inst = _build_default_translator()
        else:
            from ..config import REALTIME_SETTINGS
            inst = _resolve_backend(key)(REALTIME_SETTINGS)
        _instances[key] = inst
    return inst


async def close_translator() -> None:
    """Release resources held by all cached translators."""
    instances = list(_instances.values())
    _instances.clear()
    await asyncio.gather(*(inst.close() for inst in instances), return_exceptions=True)


__all__ = ["BaseTranslator", "get_translator", "close_translator", "AVAILABLE_PROVIDERS"]
