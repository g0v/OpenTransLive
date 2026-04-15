import asyncio

from .base import BaseTranslator


class CompositeTranslator(BaseTranslator):
    """Routes correct/extract_keywords to one backend and translate to another."""

    def __init__(self, correct_backend: BaseTranslator, translate_backend: BaseTranslator):
        self._correct = correct_backend
        self._translate = translate_backend

    async def correct(self, text: str, context: str, keywords: str) -> str:
        return await self._correct.correct(text, context, keywords)

    async def translate(
        self,
        text: str,
        language: str,
        context: str,
        prev_translation: str,
        keywords: str,
    ) -> str:
        return await self._translate.translate(text, language, context, prev_translation, keywords)

    async def extract_keywords(
        self, text: str, existing_keywords: dict[str, int]
    ) -> list[str]:
        return await self._correct.extract_keywords(text, existing_keywords)

    async def close(self) -> None:
        await asyncio.gather(self._correct.close(), self._translate.close())
