import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app


class _Segments:
    """Mongo segments collection: delete_one plus the initial write's upsert."""

    def __init__(self, *start_times: float) -> None:
        self.stored = set(start_times)
        self.inserted: list[float] = []

    async def delete_one(self, query: dict):
        start_time = query["start_time"]
        deleted = start_time in self.stored
        self.stored.discard(start_time)
        return SimpleNamespace(deleted_count=int(deleted))

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        if "$setOnInsert" in update and query["start_time"] not in self.stored:
            self.stored.add(query["start_time"])
            self.inserted.append(query["start_time"])


class _Redis:
    def __init__(self, *cached: float) -> None:
        self.cached = set(cached)

    async def zremrangebyscore(self, _key: str, low: float, _high: float) -> int:
        removed = low in self.cached
        self.cached.discard(low)
        return int(removed)


class DeleteSegmentTest(unittest.IsolatedAsyncioTestCase):
    def _patch(self, segments: _Segments, redis_client: _Redis) -> AsyncMock:
        publish = AsyncMock()
        for name, value in (
            ("transcription_segments_collection", segments),
            ("transcription_store_collection", AsyncMock()),
            ("redis_client", redis_client),
            ("_publish_segment_deleted", publish),
        ):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return publish

    async def test_queued_initial_write_does_not_resurrect_a_deleted_line(self) -> None:
        # Just committed: cached and broadcast, its Mongo write still queued.
        segments = _Segments()
        publish = self._patch(segments, _Redis(10.0))

        await app._delete_committed_segment("del-queued", 10.0)
        await app._save_segment_to_mongo("del-queued", {"start_time": 10.0, "text": "gone"}, None)

        self.assertEqual(segments.inserted, [])
        publish.assert_awaited_once_with("del-queued", 10.0)

    async def test_missing_line_is_404_and_leaves_later_writes_alone(self) -> None:
        segments = _Segments()
        publish = self._patch(segments, _Redis())

        with self.assertRaises(HTTPException) as ctx:
            await app._delete_committed_segment("del-missing", 10.0)
        self.assertEqual(ctx.exception.status_code, 404)
        publish.assert_not_awaited()

        await app._save_segment_to_mongo("del-missing", {"start_time": 10.0, "text": "new"}, None)
        self.assertEqual(segments.inserted, [10.0])

    async def test_live_delete_requires_the_panel_lock(self) -> None:
        self._patch(_Segments(10.0), _Redis())
        rooms = SimpleNamespace(find_one=AsyncMock(return_value={"sid": "room", "secret_key": "holder-key"}))
        with patch.object(app, "rooms_collection", rooms):
            for secret_key, status in ((None, 401), ("other-key", 403)):
                with self.subTest(secret_key=secret_key):
                    session = {"secret_key": secret_key} if secret_key else {}
                    request = SimpleNamespace(session=session, headers={},
                                              json=AsyncMock(return_value={"start_time": 10.0}))
                    with self.assertRaises(HTTPException) as ctx:
                        await app.delete_live_segment_endpoint(request, "room")
                    self.assertEqual(ctx.exception.status_code, status)


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _PubSub:
    def __init__(self, *payloads: dict) -> None:
        self._messages = [{"data": json.dumps(p)} for p in payloads]

    async def subscribe(self, _channel: str) -> None:
        pass

    async def get_message(self, **_kwargs):
        return self._messages.pop(0)

    async def aclose(self) -> None:
        pass


class SegmentDeletedStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_deletion_does_not_move_the_viewers_resume_point(self) -> None:
        pubsub = _PubSub({"__sse_event": "segment_deleted", "start_time": 99.5})
        with (
            patch.object(app, "redis_client", SimpleNamespace(pubsub=lambda: pubsub)),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=1)),
            patch.object(app, "_emit_viewer_count", AsyncMock()),
        ):
            stream = app._session_sse_stream(_Request(), "room", None)
            await anext(stream)  # retry hint
            event = await anext(stream)
            await stream.aclose()

        # An `id:` line would make a reconnect skip commits older than the deleted line.
        self.assertEqual(event, 'event: segment_deleted\ndata: {"start_time":99.5}\n\n')


class TxtExportTest(unittest.TestCase):
    def test_zip_has_one_line_per_segment_for_source_and_each_language(self) -> None:
        segments = [
            {"start_time": 1.0, "text": "raw one", "result": {"corrected": "one", "translated": {"en": "One", "ja": "一"}}},
            {"start_time": 2.0, "text": "two\nlines", "result": {"translated": {"en": "Two\nlines"}}},
            {"start_time": 3.0, "text": "", "result": {"corrected": "three", "translated": {"source": "three", "en": ""}}},
        ]
        with zipfile.ZipFile(io.BytesIO(app._build_txt_zip("s1", segments))) as archive:
            files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

        self.assertEqual(files, {
            "s1.source.txt": "one\ntwo lines\nthree\n",
            "s1.en.txt": "One\nTwo lines\n",
            "s1.ja.txt": "一\n",
        })


if __name__ == "__main__":
    unittest.main()
