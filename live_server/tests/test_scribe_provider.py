import asyncio
import contextlib
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import app
import app.scribe_manager as sm
from app.scribe_manager import SCRIBE_MANAGERS


def _manager(**kwargs):
    return SCRIBE_MANAGERS["elevenlabs"]("room", lambda *_args: None, **kwargs)


class _Socket:
    def __init__(self, *incoming: dict) -> None:
        self.sent: list[dict] = []
        self.incoming = [json.dumps(m) for m in incoming]

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if self.incoming:
            return self.incoming.pop(0)
        await asyncio.sleep(3600)

    async def close(self) -> None:
        pass


class RefusalTest(unittest.IsolatedAsyncioTestCase):
    async def _run_until_stopped(self, *incoming: dict, close_error: bool = False):
        statuses: list[dict] = []

        async def on_status(_sid, status):
            statuses.append(status)

        m = _manager(status_callback=on_status)
        m.api_key = "key"
        sockets: list[_Socket] = []

        @contextlib.asynccontextmanager
        async def connect(*_args, **_kwargs):
            sockets.append(_Socket(*incoming))
            yield sockets[-1]
            if close_error:
                # The socket's close handshake failing after the refusal arrived.
                raise OSError("connection reset")

        with patch.object(sm, "ws_connect", connect):
            run = asyncio.create_task(m.start())
            try:
                async with asyncio.timeout(2):
                    while m._status != "stopped":
                        await asyncio.sleep(0.01)
            finally:
                await m.stop()
                await asyncio.wait_for(run, 2)
        return m, sockets, statuses

    async def test_refusal_stops_without_reconnecting(self) -> None:
        for msg_type in ("auth_error", "quota_exceeded", "unaccepted_terms", "invalid_request"):
            for close_error in (False, True):
                with self.subTest(msg_type, close_error=close_error):
                    _m, sockets, statuses = await self._run_until_stopped(
                        {"message_type": msg_type, "error": "no"}, close_error=close_error)
                    self.assertEqual(len(sockets), 1)
                    self.assertEqual(statuses[-1],
                                     {"state": "stopped", "reason": "rejected", "refusal": msg_type})

    async def test_audio_does_not_rebuild_a_refused_session(self) -> None:
        refused = _manager()
        refused.rejected = "auth_error"
        with patch.dict(app.active_scribe_managers, {"room": refused}, clear=True):
            self.assertIs(await app._get_or_create_scribe_manager("room"), refused)


class PreviousTextTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_socket_sends_no_previous_text(self) -> None:
        m, ws = _manager(), _Socket()
        await m._handshake(ws)
        await m._send_audio(ws, "AAAA", False)
        self.assertNotIn("previous_text", ws.sent[0])

    async def test_reconnect_hands_the_open_partial_to_the_first_chunk_only(self) -> None:
        m, ws = _manager(), _Socket()
        m._parse_message({"message_type": "committed_transcript", "text": "earlier line"})
        partial = "x" * 30 + "a sentence that ends right here"
        m._parse_message({"message_type": "partial_transcript", "text": partial})
        await m._handshake(ws)
        await m._send_audio(ws, "AAAA", False)
        await m._send_audio(ws, "AAAA", False)
        self.assertEqual(ws.sent[0]["previous_text"], partial[-50:])
        self.assertNotIn("previous_text", ws.sent[1])

    async def test_without_an_open_partial_the_last_commit_is_the_context(self) -> None:
        m, ws = _manager(), _Socket()
        m._parse_message({"message_type": "committed_transcript", "text": "大家好。"})
        m._parse_message({"message_type": "committed_transcript", "text": " "})
        await m._handshake(ws)
        await m._send_audio(ws, "AAAA", False)
        self.assertEqual(ws.sent[0]["previous_text"], "大家好。")


class SecondaryLanguagesTest(unittest.TestCase):
    def _query(self, **kwargs) -> dict:
        return parse_qs(urlparse(_manager(**kwargs)._connect_target()[0]).query)

    def test_forced_language_sends_the_other_translation_languages(self) -> None:
        query = self._query(language_code="zho",
                            translate_languages=["zh-Hant-TW", "en-US", "ja", "en", "xx-YY"])
        self.assertEqual(query["secondary_languages"], ["eng", "jpn"])

    def test_auto_detect_sends_none(self) -> None:
        self.assertNotIn("secondary_languages",
                         self._query(language_code="", translate_languages=["en-US", "ja"]))


if __name__ == "__main__":
    unittest.main()
