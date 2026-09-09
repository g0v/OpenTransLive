import json
import unittest
from unittest.mock import AsyncMock, patch

import app


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _PubSub:
    def __init__(self) -> None:
        self.closed = False
        self._messages = [
            {"data": json.dumps({"__sse_event": "viewer_clear"})},
        ]

    async def subscribe(self, _channel: str) -> None:
        pass

    async def get_message(self, **_kwargs):
        return self._messages.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _Redis:
    def __init__(self, pubsub: _PubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _PubSub:
        return self._pubsub


class ViewerClearTest(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_requires_active_panel_and_publishes_ephemeral_clear(self) -> None:
        request = object()
        verify_lock_holder = AsyncMock(return_value={"sid": "test-room"})
        publish_clear = AsyncMock()

        with (
            patch.object(app, "_verify_session_lock_holder", verify_lock_holder),
            patch.object(app, "_publish_viewer_clear", publish_clear),
        ):
            response = await app.clear_session_viewers_endpoint(request, "test-room")

        self.assertEqual(response, {"status": "ok"})
        verify_lock_holder.assert_awaited_once_with(request, "test-room")
        publish_clear.assert_awaited_once_with("test-room")

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


if __name__ == "__main__":
    unittest.main()
