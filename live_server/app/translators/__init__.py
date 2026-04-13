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
"""

from .base import BaseTranslator
from .gemini import GeminiTranslator
from .openai import OpenAITranslator

# Registry: provider name → class
_BACKENDS: dict[str, type[BaseTranslator]] = {
    "gemini": GeminiTranslator,
    "openai": OpenAITranslator,
    # "anthropic": AnthropicTranslator,
}

_instance: BaseTranslator | None = None


def get_translator() -> BaseTranslator:
    """Return the singleton translator for the configured AI_PROVIDER."""
    global _instance
    if _instance is None:
        from ..config import REALTIME_SETTINGS

        provider = REALTIME_SETTINGS.get("AI_PROVIDER", "gemini").lower()
        cls = _BACKENDS.get(provider, GeminiTranslator)
        _instance = cls(REALTIME_SETTINGS)

    return _instance


async def close_translator() -> None:
    """Release resources held by the active translator."""
    global _instance
    if _instance is not None:
        await _instance.close()
        _instance = None


__all__ = ["BaseTranslator", "get_translator", "close_translator"]
