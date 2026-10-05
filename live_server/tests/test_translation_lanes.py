import asyncio
import json
import unittest
from unittest.mock import patch

import app.http_client as http_client
import app.translation_service as translation_service
from app.translators import providers


def _segment(start: float, text: str, partial: bool) -> dict:
    return {"text": text, "partial": partial, "start_time": start, "end_time": start + 1.0}


class _Lanes:
    """A TranslationQueueManager over a fake translation that records its timing."""

    def __init__(self, translate_secs: float = 0.0, partial_secs: float | None = None):
        self.translate_secs = translate_secs
        self.partial_secs = translate_secs if partial_secs is None else partial_secs
        self.events: list[tuple[str, str]] = []
        self.delivered: list[dict] = []
        self.manager = translation_service.TranslationQueueManager(self._deliver, self._load)

    async def translate(self, _sid, data, _cached, _redis, skip_correction):
        tag = f"{'partial' if data['partial'] else 'commit'}:{data['start_time']}"
        self.events.append(("start", tag))
        await asyncio.sleep(self.partial_secs if data["partial"] else self.translate_secs)
        self.events.append(("end", tag))
        data["result"] = {"translated": {"en": data["text"]}}
        return data

    async def _deliver(self, _sid, data):
        self.delivered.append(data)

    async def _load(self, _sid, num_committed):
        return {"transcriptions": []}

    def submit(self, segment: dict) -> None:
        self.manager.submit("sid", segment, None)


class TranslationLanesTest(unittest.IsolatedAsyncioTestCase):
    async def test_next_segment_partial_waits_for_outstanding_commit(self) -> None:
        lanes = _Lanes(translate_secs=0.1)
        with patch.object(translation_service, "translate_transcription", lanes.translate):
            lanes.manager.start()
            lanes.submit(_segment(1.0, "first segment opens", True))
            await asyncio.sleep(0.02)
            lanes.submit(_segment(1.0, "first segment opens and closes", False))
            await asyncio.sleep(0.02)
            lanes.submit(_segment(2.0, "second segment opens", True))
            await asyncio.sleep(0.3)
            await lanes.manager.stop()

        commit_end = lanes.events.index(("end", "commit:1.0"))
        next_start = lanes.events.index(("start", "partial:2.0"))
        self.assertLess(commit_end, next_start)
        self.assertEqual(
            [(d["partial"], d["start_time"]) for d in lanes.delivered],
            [(False, 1.0), (True, 2.0)],
        )

    async def test_backlog_still_delivers_partials_of_short_segments(self) -> None:
        # Commits take longer than a segment lasts, and each segment is shorter
        # than the park cap: neither a newer partial nor a commit closing the
        # parked one's segment may restart the wait.
        lanes = _Lanes(translate_secs=0.3, partial_secs=0.01)
        with (
            patch.object(translation_service, "translate_transcription", lanes.translate),
            patch.object(translation_service, "_PARTIAL_PARK_MAX_SECS", 0.1),
        ):
            lanes.manager.start()
            lanes.submit(_segment(1.0, "first segment closes", False))
            for start in (2.0, 3.0, 4.0, 5.0):
                for i in range(1, 4):
                    lanes.submit(_segment(start, "words " + "x" * (5 * i), True))
                    await asyncio.sleep(0.02)
                lanes.submit(_segment(start, "segment closes", False))
            await lanes.manager.stop()

        first_commit_end = lanes.events.index(("end", "commit:1.0"))
        partial_ends = [
            i for i, (kind, tag) in enumerate(lanes.events)
            if kind == "end" and tag.startswith("partial:")
        ]
        self.assertTrue(partial_ends)
        self.assertLess(partial_ends[0], first_commit_end)
        self.assertEqual(
            [d["start_time"] for d in lanes.delivered if not d["partial"]],
            [1.0, 2.0, 3.0, 4.0, 5.0],
        )

    async def test_commit_backlog_keeps_every_commit(self) -> None:
        lanes = _Lanes()
        with patch.object(translation_service, "translate_transcription", lanes.translate):
            lanes.manager.start()
            for i in range(80):
                lanes.submit(_segment(float(i), f"segment {i}", False))
            await lanes.manager.stop()

        self.assertEqual([d["start_time"] for d in lanes.delivered], [float(i) for i in range(80)])

    async def test_stop_finishes_the_commit_being_translated(self) -> None:
        lanes = _Lanes(translate_secs=0.2)
        with patch.object(translation_service, "translate_transcription", lanes.translate):
            lanes.manager.start()
            lanes.submit(_segment(9.0, "final words", False))
            await asyncio.sleep(0.05)  # the worker has taken it off the queue
            await lanes.manager.stop()

        self.assertEqual([d["start_time"] for d in lanes.delivered], [9.0])
        self.assertEqual(lanes.delivered[0]["result"]["translated"], {"en": "final words"})


class SlowBackendTest(unittest.IsolatedAsyncioTestCase):
    """A model slower than the hot-path budget, with that budget scaled down."""

    _REPLY_DELAY = 0.3

    async def asyncSetUp(self) -> None:
        self.server = await asyncio.start_server(self._reply, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        for target, name, value in (
            (http_client, "_READ_TIMEOUT", 0.1),
            (http_client, "DURABLE_READ_TIMEOUT", 3.0),
            (providers, "_BASE_RETRY_DELAY", 0.0),
            (providers.GeminiTranslator, "endpoint", f"http://127.0.0.1:{port}/chat"),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await http_client.close_async_client()
        self.addAsyncCleanup(http_client.close_async_client)
        self.translator = providers.GeminiTranslator({"GEMINI_API_KEY": "test-key"})

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _reply(self, reader, writer) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        body = json.loads(await reader.readexactly(length))
        text = body["messages"][-1]["content"]
        text = text.split("<translate_this>")[-1].split("</translate_this>")[0].strip()
        await asyncio.sleep(self._REPLY_DELAY)
        payload = json.dumps({"choices": [{"message": {"content": f"done: {text}"}}]}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    async def test_commit_waits_for_a_slow_model(self) -> None:
        translated = await self.translator.translate(
            text="a long committed segment", language="English", context="",
            prev_translation="a long", keywords="", commit=True,
        )
        corrected = await self.translator.correct(
            text="a long committed segment", prev_corrected="", keywords="", commit=True,
        )

        self.assertEqual(translated, "done: a long committed segment")
        self.assertEqual(corrected, "done: a long committed segment")

    async def test_partial_fails_fast_on_a_slow_model(self) -> None:
        translated = await self.translator.translate(
            text="a long partial", language="English", context="",
            prev_translation="", keywords="",
        )

        self.assertIsNone(translated)


if __name__ == "__main__":
    unittest.main()
