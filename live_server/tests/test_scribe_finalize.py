import asyncio
import json
import time
import unittest

import websockets.exceptions

from app.scribe_manager import _FINALIZE_CHUNK, SCRIBE_MANAGERS

_CLOSED = object()


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.incoming.get()
        if item is _CLOSED:
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        return item

    async def close(self) -> None:
        self.incoming.put_nowait(_CLOSED)


class ScribeFinalizeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.out: list[dict] = []
        self.ws = _FakeSocket()
        self.m = SCRIBE_MANAGERS["elevenlabs"]("room", lambda _sid, t: self.out.append(t))
        self.m.is_running = True
        self.m.ws = self.ws
        self.m._last_audio_mono = time.monotonic()
        tasks = [
            asyncio.create_task(self.m.send_audio_loop()),
            asyncio.create_task(self.m.receive_messages_loop()),
        ]
        for task in tasks:
            self.addCleanup(task.cancel)

    def _commits(self) -> list[str]:
        return [t["text"] for t in self.out if t and not t["partial"]]

    async def _wait_sent(self, count: int) -> None:
        async with asyncio.timeout(1):
            while len(self.ws.sent) < count:
                await asyncio.sleep(0.01)

    async def _open_segment_and_send_audio(self) -> None:
        await self.m.handle_transcript("the last", True)
        await self.m.push_audio("AAAA")
        await self.m.push_audio("AAAA")

    async def test_finalize_sends_queued_audio_then_marker_and_keeps_the_final(self) -> None:
        await self._open_segment_and_send_audio()
        finalize = asyncio.create_task(self.m.finalize(timeout=2))

        await self._wait_sent(3)
        self.assertEqual([f["audio_base_64"] for f in self.ws.sent[:2]], ["AAAA", "AAAA"])
        self.assertEqual(self.ws.sent[2]["audio_base_64"], _FINALIZE_CHUNK)
        self.assertIs(self.ws.sent[2]["commit"], True)

        bytes_before = self.m.audio_bytes_total
        await self.m.push_audio("AAAA")
        self.assertEqual(self.m.audio_bytes_total, bytes_before)

        final = "the last words we said today friends"
        started = time.monotonic()
        self.ws.incoming.put_nowait(json.dumps({"message_type": "committed_transcript", "text": final}))
        await asyncio.wait_for(finalize, timeout=2)

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self._commits(), [final])

    async def test_finalize_without_reply_commits_the_last_partial_on_timeout(self) -> None:
        await self._open_segment_and_send_audio()

        await self.m.finalize(timeout=0.2)

        self.assertEqual(self._commits(), ["the last"])

    async def test_stop_does_not_repeat_an_interim_a_short_commit_finalized(self) -> None:
        await self.m.handle_transcript("hello there", True)
        await self.m.handle_transcript("hello there", False)

        await self.m.stop()

        self.assertEqual(self._commits(), ["hello there"])


if __name__ == "__main__":
    unittest.main()
