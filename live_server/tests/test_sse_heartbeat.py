import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app

HEARTBEAT = "event: heartbeat\ndata: {}\n\n"


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


class _PubSub:
    """Each read takes `step` seconds and returns the next scripted message."""

    def __init__(self, clock: _Clock, messages: list, step: float) -> None:
        self.clock, self.messages, self.step = clock, messages, step
        self.timeouts: list[float] = []

    async def subscribe(self, _channel: str) -> None:
        pass

    async def get_message(self, *, timeout: float, **_kwargs):
        self.timeouts.append(timeout)
        self.clock.now += min(self.step, timeout)
        return self.messages.pop(0) if self.messages else None

    async def aclose(self) -> None:
        pass


class SseHeartbeatTest(unittest.IsolatedAsyncioTestCase):
    async def _stream(self, messages: list, step: float, count: int) -> tuple[list[str], _PubSub]:
        clock = _Clock()
        pubsub = _PubSub(clock, messages, step)
        with (
            patch.object(app, "time", SimpleNamespace(monotonic=clock.monotonic, time=clock.time)),
            patch.object(app, "redis_client", SimpleNamespace(pubsub=lambda: pubsub)),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=1)),
            patch.object(app, "_emit_viewer_count", AsyncMock()),
        ):
            stream = app._session_sse_stream(_Request(), "room", None)
            out = [await anext(stream) for _ in range(count)]
            await stream.aclose()
        return out, pubsub

    async def test_skipped_messages_do_not_starve_the_heartbeat(self) -> None:
        # A steady trickle of messages the stream drops (malformed) never leaves the
        # read idle, yet the viewer must still see traffic within its 40 s watchdog.
        malformed = [{"data": "not json"} for _ in range(20)]
        out, pubsub = await self._stream(malformed, step=5, count=2)
        self.assertEqual(out[1], HEARTBEAT)
        # Sent once 15 s had passed (three 5 s reads), not when the trickle ended.
        self.assertEqual(len(pubsub.timeouts), 3)

    async def test_heartbeat_waits_from_the_last_event_sent(self) -> None:
        update = {"data": json.dumps({"start_time": 1.0, "partial": False})}
        out, pubsub = await self._stream([None, update], step=10, count=3)
        self.assertIn("event: transcription_update", out[1])
        self.assertEqual(out[2], HEARTBEAT)
        # The event went out at +15 s, so the next read waits the full interval again.
        self.assertEqual(pubsub.timeouts[:3], [15, 5, 15])


if __name__ == "__main__":
    unittest.main()
