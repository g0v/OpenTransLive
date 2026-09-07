import unittest
from unittest.mock import AsyncMock, patch

import anyio

import app


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _PubSub:
    def __init__(self) -> None:
        self.waiting = anyio.Event()
        self.closed = False

    async def subscribe(self, _channel: str) -> None:
        pass

    async def get_message(self, **_kwargs):
        self.waiting.set()
        await anyio.sleep_forever()

    async def aclose(self) -> None:
        await anyio.sleep(0)
        self.closed = True


class _Redis:
    def __init__(self, pubsub: _PubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _PubSub:
        return self._pubsub


class SessionSseCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_stream_closes_redis_subscription(self) -> None:
        pubsub = _PubSub()
        presence_op = AsyncMock(return_value=0)
        emit_viewer_count = AsyncMock()

        async def consume_stream() -> None:
            async for _event in app._session_sse_stream(_Request(), "test-room", None):
                pass

        with (
            patch.object(app, "redis_client", _Redis(pubsub)),
            patch.object(app, "_viewer_presence_op", presence_op),
            patch.object(app, "_emit_viewer_count", emit_viewer_count),
        ):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(consume_stream)
                await pubsub.waiting.wait()
                task_group.cancel_scope.cancel()

        self.assertTrue(pubsub.closed)
        self.assertEqual(presence_op.await_count, 2)


if __name__ == "__main__":
    unittest.main()
