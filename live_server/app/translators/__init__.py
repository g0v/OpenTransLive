"""
Translator factory.

Usage
-----
    from .translators import get_translator

    translator = get_translator()
    corrected = await translator.correct(text, context, keywords)

Adding a new backend
--------------------
1. Create e.g. ``translators/anthropic.py`` subclassing ``BaseTranslator``.
2. Add an entry to ``_BACKENDS`` below.
3. Set ``AI_PROVIDER=anthropic`` (or whatever key you choose) in your config.

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

from typing import Callable

from ..logger_config import setup_logger
from .base import BaseTranslator
from .composite import CompositeTranslator
from .gemini import GeminiTranslator
from .groq import GroqTranslator
from .openai import OpenAITranslator

logger = setup_logger(__name__)

# Registry: provider name → class
_BACKENDS: dict[str, Callable[[dict], BaseTranslator]] = {
    "gemini": GeminiTranslator,
    "groq": GroqTranslator,
    "openai": OpenAITranslator,
    # "anthropic": AnthropicTranslator,
}

_instance: BaseTranslator | None = None


def _resolve_backend(provider: str) -> Callable[[dict], BaseTranslator]:
    cls = _BACKENDS.get(provider)
    if cls is None:
        logger.warning("Unknown AI provider %r, falling back to GeminiTranslator", provider)
        return GeminiTranslator
    return cls


def get_translator() -> BaseTranslator:
    """Return the singleton translator.

    When ``CORRECT_PROVIDER`` and ``TRANSLATE_PROVIDER`` differ, returns a
    ``CompositeTranslator`` that routes each operation to the right backend.
    """
    global _instance
    if _instance is None:
        from ..config import REALTIME_SETTINGS

        default = REALTIME_SETTINGS.get("AI_PROVIDER", "gemini").lower()
        correct_provider = REALTIME_SETTINGS.get("CORRECT_PROVIDER", default).lower()
        translate_provider = REALTIME_SETTINGS.get("TRANSLATE_PROVIDER", default).lower()

        correct_cls = _resolve_backend(correct_provider)
        translate_cls = _resolve_backend(translate_provider)

        if correct_provider == translate_provider:
            _instance = correct_cls(REALTIME_SETTINGS)
        else:
            _instance = CompositeTranslator(
                correct_backend=correct_cls(REALTIME_SETTINGS),
                translate_backend=translate_cls(REALTIME_SETTINGS),
            )

    return _instance


async def close_translator() -> None:
    """Release resources held by the active translator."""
    global _instance
    if _instance is not None:
        await _instance.close()
        _instance = None


__all__ = ["BaseTranslator", "get_translator", "close_translator"]
