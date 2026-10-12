import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import app.translation_service as ts


class TranslateTranscriptionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.translator = MagicMock()
        self.translator.correct = AsyncMock(side_effect=lambda **kw: kw["text"])
        self.translator.translate = AsyncMock(side_effect=lambda **kw: "T:" + kw["text"])
        self.languages: list[str] = []
        self.rules: list[dict] = []
        self.keywords: tuple[dict, list] = ({}, [])

        stack = ExitStack()
        self.addCleanup(stack.close)
        patches = {
            "get_session_ai_provider": AsyncMock(return_value=""),
            "get_translator": MagicMock(return_value=self.translator),
            "get_session_languages": AsyncMock(side_effect=lambda *_: self.languages),
            "get_text_dictionary": AsyncMock(side_effect=lambda *_: self.rules),
            "get_keywords_and_locked": AsyncMock(side_effect=lambda *_: self.keywords),
            "get_session_translate_tone": AsyncMock(return_value=""),
            "get_session_scribe_language": AsyncMock(return_value=""),
            "get_glossary": AsyncMock(return_value=[]),
            "rerank_keywords": AsyncMock(),
        }
        for name, mock in patches.items():
            stack.enter_context(patch.object(ts, name, mock))

    async def _run(self, text: str, partial: bool = False) -> dict:
        data = {"text": text, "partial": partial, "start_time": 1.0, "end_time": 2.0}
        return await ts.translate_transcription(
            "room", data, {"transcriptions": []}, None, skip_correction=False
        )

    async def test_flow_rules_apply_in_a_room_without_target_languages(self) -> None:
        self.rules = [{"from": "Shaun", "to": "Sean", "target": ts.FLOW_TARGET}]

        out = await self._run("Shaun speaks")

        self.assertEqual(out["text"], "Sean speaks")
        self.assertNotIn("result", out)
        self.translator.correct.assert_not_called()
        self.translator.translate.assert_not_called()

    async def test_blank_text_never_reaches_the_llm(self) -> None:
        self.languages = ["en-US"]
        self.rules = [{"from": "嗯", "to": "", "target": ts.FLOW_TARGET}]

        for text in ["  \n\t", "嗯 嗯"]:
            with self.subTest(text=text):
                out = await self._run(text)
                self.assertNotIn("result", out)

        self.translator.correct.assert_not_called()
        self.translator.translate.assert_not_called()

    async def test_short_partials_wait_but_short_commits_are_sent(self) -> None:
        self.languages = ["en-US"]
        with patch.object(ts, "_PARTIAL_MIN_WORDS", 2.5):
            for text in ["好的", "so we"]:
                with self.subTest(held=text):
                    self.assertNotIn("result", await self._run(text, partial=True))
            self.translator.translate.assert_not_called()

            self.assertIn("result", await self._run("好的我們", partial=True))
            self.assertIn("result", await self._run("好的", partial=False))

        self.assertEqual(self.translator.translate.call_count, 2)

    async def test_only_pinned_keywords_reach_prompts_and_translator_gets_mentioned_ones(self) -> None:
        self.languages = ["en-US"]
        self.keywords = (
            {"OpenTransLive": 5, "Sean Gau": 3, "caption": 2},
            ["OpenTransLive", "Sean Gau"],
        )

        await self._run("Sean Gau speaks")

        self.assertEqual(
            self.translator.correct.call_args.kwargs["keywords"], "OpenTransLive, Sean Gau"
        )
        self.assertEqual(self.translator.translate.call_args.kwargs["keywords"], "Sean Gau")


if __name__ == "__main__":
    unittest.main()
