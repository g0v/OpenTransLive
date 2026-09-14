import unittest
from unittest.mock import AsyncMock, patch

from app.translators.providers import GeminiTranslator


_MISSING_CONTENT_RESPONSE = {
    "choices": [
        {
            "message": {"role": "assistant"},
            "finish_reason": "length",
        }
    ]
}


class MissingMessageContentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.translator = GeminiTranslator({"GEMINI_API_KEY": "test-key"})

    async def test_correct_falls_back_to_transcript(self) -> None:
        with patch.object(
            self.translator,
            "_chat",
            AsyncMock(return_value=_MISSING_CONTENT_RESPONSE),
        ):
            corrected = await self.translator.correct(
                text="raw transcript",
                prev_corrected="",
                keywords="",
            )

        self.assertEqual(corrected, "raw transcript")

    async def test_translate_treats_missing_content_as_failure(self) -> None:
        with patch.object(
            self.translator,
            "_chat",
            AsyncMock(return_value=_MISSING_CONTENT_RESPONSE),
        ):
            translated = await self.translator.translate(
                text="raw transcript",
                language="English",
                context="",
                prev_translation="",
                keywords="",
            )

        self.assertIsNone(translated)


if __name__ == "__main__":
    unittest.main()
