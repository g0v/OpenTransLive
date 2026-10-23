import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import app
import app.translation_service as translation_service


class SegmentRetranslationTest(unittest.IsolatedAsyncioTestCase):
    async def test_revision_preserves_translations_for_inactive_languages(self) -> None:
        original = {
            "_id": "segment-id",
            "text": "original",
            "start_time": 10.0,
            "end_time": 11.0,
            "partial": False,
            "result": {
                "corrected": "before",
                "translated": {"en": "old English", "ja": "kept Japanese"},
            },
        }
        revised = {
            "text": "original",
            "start_time": 10.0,
            "end_time": 11.0,
            "partial": False,
            "result": {
                "corrected": "after",
                "translated": {"en": "new English"},
            },
        }
        collection = AsyncMock()
        replace_cached = AsyncMock()

        with (
            patch.object(app, "transcription_segments_collection", collection),
            patch.object(app, "_replace_cached_segment", replace_cached),
        ):
            await app._persist_segment_revision("room", original, revised)

        stored = collection.update_one.await_args.args[1]["$set"]
        expected = {"en": "new English", "ja": "kept Japanese"}
        self.assertEqual(stored["result.translated"], expected)
        self.assertEqual(revised["result"]["translated"], expected)
        self.assertEqual(replace_cached.await_args.args[2]["result"]["translated"], expected)

    async def test_same_segment_publish_is_serialized_with_persistence(self) -> None:
        original = {
            "_id": "segment-id",
            "text": "original",
            "start_time": 10.0,
            "end_time": 11.0,
            "partial": False,
            "result": {"corrected": "before", "translated": {"en": "before"}},
        }
        persisted = []
        published = []
        first_publish_started = asyncio.Event()
        release_first_publish = asyncio.Event()

        async def translate(_sid, data, _cached, _redis, *, skip_correction, corrected_override):
            return {
                **data,
                "result": {
                    "corrected": corrected_override,
                    "translated": {"en": f"{corrected_override} English"},
                },
            }

        async def persist(_sid, _original, revised):
            persisted.append(revised["result"]["corrected"])

        async def publish(_sid, revised):
            corrected = revised["result"]["corrected"]
            if corrected == "first":
                first_publish_started.set()
                await release_first_publish.wait()
            published.append(corrected)

        with (
            patch.object(translation_service, "get_session_languages", AsyncMock(return_value=["en"])),
            patch.object(translation_service, "translate_transcription", translate),
            patch.object(app, "_load_committed_segment_with_context", AsyncMock(return_value=(original, []))),
            patch.object(app, "_persist_segment_revision", persist),
            patch.object(app, "_publish_segment_updated", publish),
        ):
            first = asyncio.create_task(
                app._retranslate_committed_segment("room", 10.0, "first")
            )
            await asyncio.wait_for(first_publish_started.wait(), timeout=1)
            second = asyncio.create_task(
                app._retranslate_committed_segment("room", 10.0, "second")
            )
            await asyncio.sleep(0)
            self.assertEqual(persisted, ["first"])
            release_first_publish.set()
            await asyncio.gather(first, second)

        self.assertEqual(persisted, ["first", "second"])
        self.assertEqual(published, ["first", "second"])

    async def test_retranslate_without_target_languages_stores_source(self) -> None:
        original = {
            "_id": "segment-id",
            "text": "original",
            "start_time": 10.0,
            "end_time": 11.0,
            "partial": False,
            "result": {"corrected": "before", "translated": {"source": "before"}},
        }
        persist = AsyncMock()
        publish = AsyncMock()
        getters = {
            "get_session_languages": AsyncMock(return_value=[]),
            "get_session_ai_provider": AsyncMock(return_value=""),
            "get_translator": MagicMock(return_value=MagicMock()),
            "get_text_dictionary": AsyncMock(return_value=[]),
            "get_keywords_and_locked": AsyncMock(return_value=({}, [])),
            "get_session_translate_tone": AsyncMock(return_value=""),
            "get_session_scribe_language": AsyncMock(return_value=""),
            "get_glossary": AsyncMock(return_value=[]),
        }
        with ExitStack() as stack:
            for name, mock in getters.items():
                stack.enter_context(patch.object(translation_service, name, mock))
            stack.enter_context(patch.object(
                app, "_load_committed_segment_with_context", AsyncMock(return_value=(original, []))
            ))
            stack.enter_context(patch.object(app, "_persist_segment_revision", persist))
            stack.enter_context(patch.object(app, "_publish_segment_updated", publish))
            revised = await app._retranslate_committed_segment("room", 10.0, "edited")

        self.assertEqual(revised["result"]["translated"], {"source": "edited"})
        self.assertEqual(persist.await_args.args[2]["result"]["translated"], {"source": "edited"})
        publish.assert_awaited_once()

    async def test_joined_session_backfills_recent_committed_segments(self) -> None:
        segments = [
            {"start_time": 1.0, "result": {"corrected": "first"}},
            {"start_time": 2.0, "result": {"corrected": "second"}},
        ]
        emit = AsyncMock()

        with (
            patch.object(app, "_get_recent_committed_segments", AsyncMock(return_value=segments)),
            patch.object(app.sio, "emit", emit),
        ):
            await app._emit_joined_session("socket", "room", 3)

        event, payload = emit.await_args.args[:2]
        self.assertEqual(event, "joined_session")
        self.assertEqual(payload["committed_segments"], segments)
        self.assertEqual(payload["viewer_count"], 3)
        self.assertEqual(emit.await_args.kwargs, {"to": "socket"})


class LiveSegmentUpdateTest(unittest.IsolatedAsyncioTestCase):
    ORIGINAL = {
        "_id": "segment-id",
        "text": "raw",
        "start_time": 10.0,
        "end_time": 11.0,
        "partial": False,
        "result": {"corrected": "before", "translated": {"en": "old en", "ja": "old ja"}},
    }

    def setUp(self) -> None:
        self.persist = AsyncMock()
        self.publish = AsyncMock()
        self.load = AsyncMock()
        for name, mock in (
            ("_load_committed_segment_with_context", self.load),
            ("_persist_segment_revision", self.persist),
            ("_publish_segment_updated", self.publish),
        ):
            patcher = patch.object(app, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _update(self, original, corrected, translated):
        self.load.return_value = (original, [])
        return await app._update_committed_segment("room", 10.0, corrected, translated)

    async def test_translation_only_update_keeps_source_and_other_languages(self) -> None:
        revised = await self._update(dict(self.ORIGINAL), None, {"en": "new en"})
        self.assertEqual(revised["result"], {"corrected": "before", "translated": {"en": "new en", "ja": "old ja"}})
        self.assertNotIn("_id", revised)
        self.assertEqual(self.persist.await_args.args[2], revised)
        self.publish.assert_awaited_once_with("room", revised)

    async def test_source_only_update_keeps_translations(self) -> None:
        revised = await self._update(dict(self.ORIGINAL), "after", None)
        self.assertEqual(revised["result"], {"corrected": "after", "translated": {"en": "old en", "ja": "old ja"}})

    async def test_segment_without_result_takes_corrected_from_text(self) -> None:
        original = {key: value for key, value in self.ORIGINAL.items() if key != "result"}
        revised = await self._update(original, None, {"en": "typed"})
        self.assertEqual(revised["result"], {"corrected": "raw", "translated": {"en": "typed"}})

    async def test_missing_segment_is_404_and_publishes_nothing(self) -> None:
        with self.assertRaises(app.HTTPException) as ctx:
            await self._update(None, "after", None)
        self.assertEqual(ctx.exception.status_code, 404)
        self.persist.assert_not_awaited()
        self.publish.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
