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


_OK_RESPONSE = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class RequestBodyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.translator = GeminiTranslator({"GEMINI_API_KEY": "test-key"})

    async def _body(self, method: str, commit: bool, **kwargs) -> dict:
        chat = AsyncMock(return_value=_OK_RESPONSE)
        with patch.object(self.translator, "_chat", chat):
            if method == "correct":
                await self.translator.correct(
                    text="hello", prev_corrected="", keywords="", commit=commit
                )
            else:
                await self.translator.translate(
                    text="hello", language="English", context="", prev_translation="",
                    keywords=kwargs.get("keywords", ""), commit=commit,
                )
        return chat.call_args.args[0]

    async def test_gemini_commits_reason_more_than_partials(self) -> None:
        for method in ("correct", "translate"):
            with self.subTest(method=method):
                partial = await self._body(method, commit=False)
                commit = await self._body(method, commit=True)
                self.assertEqual(partial["reasoning_effort"], "minimal")
                self.assertEqual(commit["reasoning_effort"], "low")
                self.assertEqual(partial["model"], commit["model"])
                self.assertEqual(partial["temperature"], commit["temperature"])

    async def test_translate_prompt_labels_keywords_as_reference_only(self) -> None:
        with_keywords = (await self._body("translate", False, keywords="A, B"))["messages"][0]["content"]
        self.assertIn(
            "Keywords (spelling reference for terms in <translate_this>; never output this list): A, B\n"
            "Previous translation:",
            with_keywords,
        )

        without = (await self._body("translate", False, keywords=""))["messages"][0]["content"]
        self.assertNotIn("Keywords", without)
        self.assertIn("the keyword list.\n\nPrevious translation:", without)


if __name__ == "__main__":
    unittest.main()
