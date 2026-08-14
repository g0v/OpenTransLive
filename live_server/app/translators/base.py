# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
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
        source: str = "",
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
            source: Source language code, empty on auto detect. Only ever set
                from the operator's explicit setting, so the prompt can state
                it as fact rather than as a guess — a detector's guess here
                would be worse than saying nothing, since the model reads the
                text better than any detector we could put in front of it.
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

    def supports_glossary(self) -> bool:
        """Whether generate_glossary can actually run here.

        False when the backend has no web search at all, and also when it has one
        but this deployment hasn't configured it (no API key, no prompt). Lets a
        caller pick a backend that will work instead of paying for a failed call.
        """
        return False

    async def generate_glossary(self, term: str, languages: list[str]) -> dict[str, str] | None:
        """Return ``{language code: spelling}`` for *term*, or None if unsupported.

        Deliberately not abstract: it needs web-grounded search to be worth
        anything, which only some backends offer. The default says "not
        available here" so callers can report that instead of guessing.

        Args:
            term: The word or name to look up.
            languages: BCP-47 codes wanted in the result. The backend also
                reports the term's own language, and omits any language it
                cannot find an established spelling for.
        """
        return None

    async def close(self) -> None:
        """Release any held resources (HTTP connections, etc.)."""
