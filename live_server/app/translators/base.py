from abc import ABC, abstractmethod


class BaseTranslator(ABC):
    """
    Abstract base for translation backends.

    To add a new model/provider, subclass this, implement the three methods,
    and register it in translators/__init__.py.
    """

    @abstractmethod
    async def correct(self, text: str, prev_corrected: str, keywords: str) -> str:
        """Return the ASR-corrected version of *text*.

        Args:
            text: Raw transcription segment to correct.
            prev_corrected: Previous partial's corrected text for continuity.
            keywords: Comma-separated domain keywords to guide correction.
        """

    @abstractmethod
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
        """Return *text* translated into *language*, or None if translation failed.

        Args:
            text: Corrected transcription to translate.
            language: Target language name (e.g. "Traditional Chinese").
            context: Recent translated sentences for continuity.
            prev_translation: Previous partial translation to minimise diffs.
            keywords: Comma-separated domain keywords.
            commit: True for durable committed segments, which retry harder
                since an unrecovered translation is stored as a permanent gap.
        """

    @abstractmethod
    async def extract_keywords(
        self, text: str, existing_keywords: dict[str, int]
    ) -> list[str]:
        """Return new special nouns/names found in *text*.

        Args:
            text: Corrected transcription to analyse.
            existing_keywords: Already-known keywords (name → score).
        """

    async def close(self) -> None:
        """Release any held resources (HTTP connections, etc.)."""
