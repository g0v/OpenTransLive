import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _PubSub:
    def __init__(self, *payloads: dict) -> None:
        self.closed = False
        msgs = payloads or ({"__sse_event": "viewer_clear"},)
        self._messages = [{"data": json.dumps(p)} for p in msgs]

    async def subscribe(self, _channel: str) -> None:
        pass

    async def get_message(self, **_kwargs):
        return self._messages.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _Redis:
    def __init__(self, pubsub: _PubSub, *, zset=None, kv=None) -> None:
        self._pubsub = pubsub
        self._zset = list(zset or [])
        self._kv = dict(kv or {})
        self.deleted: list[str] = []

    def pubsub(self) -> _PubSub:
        return self._pubsub

    async def get(self, key: str):
        return self._kv.get(key)

    async def set(self, key: str, value: str, **_kwargs):
        self._kv[key] = value

    async def delete(self, *keys: str):
        self.deleted.extend(keys)
        for k in keys:
            self._kv.pop(k, None)

    async def zrevrange(self, _key: str, start: int, stop: int, withscores: bool = False):
        items = list(reversed(self._zset))[start : stop + 1 if stop >= 0 else None]
        if withscores:
            return [(raw, score) for score, raw in items]
        return [raw for _score, raw in items]

    async def zrangebyscore(self, _key: str, min_score: str, _max_score: str):
        exclusive = min_score.startswith("(")
        bound = float(min_score[1:] if exclusive else min_score)
        return [
            raw for score, raw in self._zset
            if (score > bound if exclusive else score >= bound)
        ]


class ViewerClearTest(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_requires_active_panel_records_then_publishes_clear(self) -> None:
        request = object()
        verify_lock_holder = AsyncMock(return_value={"sid": "test-room"})
        record_clear = AsyncMock()
        publish_clear = AsyncMock()

        with (
            patch.object(app, "_verify_session_lock_holder", verify_lock_holder),
            patch.object(app, "_record_viewers_clear", record_clear),
            patch.object(app, "_publish_viewer_clear", publish_clear),
        ):
            response = await app.clear_session_viewers_endpoint(request, "test-room")

        self.assertEqual(response, {"status": "ok"})
        verify_lock_holder.assert_awaited_once_with(request, "test-room")
        record_clear.assert_awaited_once_with("test-room")
        publish_clear.assert_awaited_once_with("test-room")

    async def test_record_viewers_clear_filters_audience_only_and_drops_partial(self) -> None:
        seg1 = {"start_time": 10.0, "text": "old"}
        seg2 = {"start_time": 12.0, "text": "after"}
        partial = {"start_time": 11.5, "partial": True, "text": "stale partial"}
        fake_redis = _Redis(
            _PubSub(),
            zset=[(10.0, json.dumps(seg1))],
            kv={"transcription:test-room:partial": json.dumps(partial)},
        )
        rooms = SimpleNamespace(
            doc={"sid": "test-room"},
            find_one=AsyncMock(side_effect=lambda *_a, **_kw: rooms.doc),
            update_one=AsyncMock(side_effect=lambda _q, u: rooms.doc.update(u.get("$max", {}))),
        )
        segments_col = SimpleNamespace(find_one=AsyncMock(return_value={"start_time": 10.0}))

        with (
            patch.object(app, "redis_client", fake_redis),
            patch.object(app, "rooms_collection", rooms),
            patch.object(app, "transcription_segments_collection", segments_col),
        ):
            await app._record_viewers_clear("test-room")
            segments_col.find_one.assert_not_awaited()
            self.assertIn("transcription:test-room:partial", fake_redis.deleted)

            # New segment after clear is visible; old segment before clear is hidden.
            fake_redis._zset.append((12.0, json.dumps(seg2)))
            with patch.object(
                app,
                "get_cached_transcription",
                AsyncMock(return_value={"transcriptions": [seg1, seg2], "partial": None}),
            ):
                segments, snap_partial = await app._get_audience_snapshot("test-room")
                self.assertEqual([s["start_time"] for s in segments], [12.0])
                self.assertIsNone(snap_partial)

            replay = [s async for s in app._iter_replay_transcription_events("test-room", 0.0)]
            self.assertEqual([s["start_time"] for s in replay], [12.0])

    async def test_record_viewers_clear_falls_back_to_mongo_when_redis_empty(self) -> None:
        fake_redis = _Redis(_PubSub(), zset=[])
        rooms = SimpleNamespace(
            doc={"sid": "test-room"},
            find_one=AsyncMock(side_effect=lambda *_a, **_kw: rooms.doc),
            update_one=AsyncMock(side_effect=lambda _q, u: rooms.doc.update(u.get("$max", {}))),
        )
        segments_col = SimpleNamespace(find_one=AsyncMock(return_value={"start_time": 15.0}))

        with (
            patch.object(app, "redis_client", fake_redis),
            patch.object(app, "rooms_collection", rooms),
            patch.object(app, "transcription_segments_collection", segments_col),
        ):
            await app._record_viewers_clear("test-room")

        segments_col.find_one.assert_awaited_once()
        self.assertGreater(rooms.doc["viewers_cleared_at"], 15.0)

    async def test_sse_stream_forwards_viewer_clear_as_named_event(self) -> None:
        pubsub = _PubSub()
        presence_op = AsyncMock(return_value=1)
        emit_viewer_count = AsyncMock()

        with (
            patch.object(app, "redis_client", _Redis(pubsub)),
            patch.object(app, "_viewer_presence_op", presence_op),
            patch.object(app, "_emit_viewer_count", emit_viewer_count),
        ):
            stream = app._session_sse_stream(_Request(), "test-room", None)
            retry = await anext(stream)
            clear_event = await anext(stream)
            await stream.aclose()

        self.assertEqual(retry, f"retry: {app.SSE_RETRY_MS}\n\n")
        self.assertEqual(clear_event, "event: viewer_clear\ndata: {}\n\n")
        self.assertTrue(pubsub.closed)

    async def test_process_transcription_update_skips_redis_publish_for_flow_only(self) -> None:
        class _Pipe:
            def __init__(self) -> None:
                self.cmds = []

            def __getattr__(self, name):
                def _rec(*args, **kwargs):
                    self.cmds.append((name, args, kwargs))
                    return self
                return _rec

            async def execute(self):
                return [[], None, None, True, True]

        fake_redis = SimpleNamespace(pipeline=lambda: _Pipe())
        fake_sio = SimpleNamespace(emit=AsyncMock())
        publish_update = AsyncMock()
        with (
            patch.object(app, "redis_client", fake_redis),
            patch.object(app, "sio", fake_sio),
            patch.object(app, "_publish_transcription_update", publish_update),
            patch.object(app, "get_youtube_start_time", AsyncMock(return_value=None)),
            patch.dict(app.active_scribe_managers, {}, clear=True),
            patch.dict(app._partial_last_emit_started, {}, clear=True),
            patch.dict(app._partial_debounce_tasks, {}, clear=True),
        ):
            await app._process_transcription_update(
                "test-room",
                {"start_time": 1.0, "end_time": 1.5, "partial": True, "flow_only": True, "text": "flow"},
            )

        fake_sio.emit.assert_awaited_once()
        publish_update.assert_not_awaited()

    async def test_sse_snapshot_includes_partial(self) -> None:
        pubsub = _PubSub({"__sse_event": "viewer_clear"})
        partial = {"start_time": 20.0, "partial": True, "text": "live"}
        with (
            patch.object(app, "redis_client", _Redis(pubsub)),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=1)),
            patch.object(app, "_emit_viewer_count", AsyncMock()),
            patch.object(app, "_get_audience_snapshot", AsyncMock(return_value=([], partial))),
        ):
            stream = app._session_sse_stream(
                _Request(), "test-room", None, send_segment_snapshot=True
            )
            await anext(stream)  # retry
            snapshot_event = await anext(stream)
            await stream.aclose()

        self.assertIn("event: segment_snapshot\n", snapshot_event)
        payload = json.loads(snapshot_event.split("data: ", 1)[1].strip())
        self.assertEqual(payload["segments"], [])
        self.assertEqual(payload["partial"], partial)


if __name__ == "__main__":
    unittest.main()
