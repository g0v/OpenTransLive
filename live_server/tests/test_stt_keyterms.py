import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import app
import app.translation_service as ts
from app.scribe_manager import SCRIBE_MANAGERS


def _manager(provider: str, keyterms=()):
    return SCRIBE_MANAGERS[provider]("room", lambda *_args: None, keyterms=keyterms)


class _SetupSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return json.dumps({"setupComplete": {}})


class ProviderKeytermsTest(unittest.IsolatedAsyncioTestCase):
    def test_elevenlabs_sends_each_pin_as_a_repeated_query_parameter(self) -> None:
        url, _ = _manager("elevenlabs", ["g0v", "台北市政府"])._connect_target()
        self.assertEqual(parse_qs(urlparse(url).query)["keyterms"], ["g0v", "台北市政府"])

    def test_elevenlabs_drops_terms_past_its_documented_limits_instead_of_truncating(self) -> None:
        exact, over = "a" * 20, "b" * 21
        terms = [exact, over, *(f"t{i}" for i in range(60))]
        sent = parse_qs(urlparse(_manager("elevenlabs", terms)._connect_target()[0]).query)["keyterms"]
        self.assertEqual(len(sent), 50)
        self.assertEqual(sent[0], exact)
        self.assertNotIn(over, sent)
        self.assertNotIn(over[:20], sent)

    def test_without_pins_elevenlabs_omits_the_billed_parameter(self) -> None:
        url, _ = _manager("elevenlabs")._connect_target()
        self.assertNotIn("keyterms", parse_qs(urlparse(url).query))

    def test_malformed_stored_pins_do_not_break_the_manager(self) -> None:
        self.assertEqual(_manager("elevenlabs", [" g0v ", "g0v", 3, None, ""]).keyterms, ["g0v"])
        # A bare string must not become one keyterm per character.
        self.assertEqual(_manager("elevenlabs", "g0v").keyterms, [])

    async def test_gemini_sends_pins_as_custom_vocabulary(self) -> None:
        long_term = "x" * 40  # Gemini documents no per-term length
        ws = _SetupSocket()
        await _manager("gemini", ["Kubernetes", long_term])._handshake(ws)
        self.assertEqual(ws.sent[0]["setup"]["inputAudioTranscription"]["customVocabulary"],
                         ["Kubernetes", long_term])

    async def test_without_pins_gemini_setup_has_no_custom_vocabulary(self) -> None:
        ws = _SetupSocket()
        await _manager("gemini")._handshake(ws)
        self.assertNotIn("customVocabulary", ws.sent[0]["setup"]["inputAudioTranscription"])


class _Request:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class KeywordsEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def _post(self, manager, body: dict) -> AsyncMock:
        get_or_create = AsyncMock()
        with (
            patch.object(app, "_require_session_owner", AsyncMock()),
            patch.object(app, "_emit_session_settings_update", AsyncMock()),
            patch.object(app, "_get_or_create_scribe_manager", get_or_create),
            patch.object(ts, "get_keywords_and_locked", AsyncMock(return_value=({}, []))),
            patch.object(ts, "save_locked_keywords", AsyncMock()),
            patch.object(ts, "save_current_keywords", AsyncMock()),
            patch.dict(app.active_scribe_managers, {"room": manager}, clear=True),
        ):
            await app.update_session_keywords_endpoint(_Request(body), "room")
        return get_or_create

    async def test_new_pins_reach_the_live_manager_without_restarting_it(self) -> None:
        manager = _manager("elevenlabs", ["old"])
        with patch.object(manager, "stop", AsyncMock()) as stop:
            get_or_create = await self._post(
                manager, {"keywords": ["a", "pin"], "locked_keywords": ["pin"]})
        self.assertEqual(manager.keyterms, ["pin"])
        stop.assert_not_awaited()
        get_or_create.assert_not_awaited()
        # The next connection, whenever it happens, carries the new pins.
        url, _ = manager._connect_target()
        self.assertEqual(parse_qs(urlparse(url).query)["keyterms"], ["pin"])

    async def test_unpinned_keywords_never_bias_recognition(self) -> None:
        manager = _manager("elevenlabs", ["pin"])
        await self._post(manager, {"keywords": ["a", "b"]})
        self.assertEqual(manager.keyterms, ["pin"])

    async def test_overlapping_pin_updates_leave_the_manager_on_the_saved_list(self) -> None:
        manager = _manager("elevenlabs")
        stored: list[list[str]] = []
        first_saved = asyncio.Event()
        release_first = asyncio.Event()

        async def save_locked(_redis, _sid, pins):
            stored.append(pins)
            if len(stored) == 1:
                # The first write has landed but its request has not updated the manager yet.
                first_saved.set()
                await release_first.wait()

        with (
            patch.object(app, "_require_session_owner", AsyncMock()),
            patch.object(app, "_emit_session_settings_update", AsyncMock()),
            patch.object(ts, "get_keywords_and_locked", AsyncMock(return_value=({}, []))),
            patch.object(ts, "save_locked_keywords", save_locked),
            patch.object(ts, "save_current_keywords", AsyncMock()),
            patch.dict(app.active_scribe_managers, {"room": manager}, clear=True),
        ):
            first = asyncio.create_task(app.update_session_keywords_endpoint(
                _Request({"keywords": ["a"], "locked_keywords": ["a"]}), "room"))
            await first_saved.wait()
            second = asyncio.create_task(app.update_session_keywords_endpoint(
                _Request({"keywords": ["b"], "locked_keywords": ["b"]}), "room"))
            await asyncio.sleep(0)
            # The second request reaches the save without suspending, unless the lock
            # held by the first one keeps it out of persistence.
            self.assertEqual(stored, [["a"]])
            release_first.set()
            await asyncio.gather(first, second)
        self.assertEqual(stored, [["a"], ["b"]])
        self.assertEqual(manager.keyterms, ["b"])


class ManagerCreationTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_manager_is_biased_by_pinned_keywords_only(self) -> None:
        with (
            patch.object(ts, "get_session_scribe_language", AsyncMock(return_value="")),
            patch.object(ts, "get_session_partial_interval", AsyncMock(return_value=None)),
            patch.object(ts, "get_session_stt_provider", AsyncMock(return_value="elevenlabs")),
            patch.object(ts, "get_keywords_and_locked",
                         AsyncMock(return_value=({"unpinned": 5, "pin": 1}, ["pin"]))),
            patch.object(app, "get_youtube_start_time", AsyncMock(return_value=None)),
            patch.object(SCRIBE_MANAGERS["elevenlabs"], "start", AsyncMock()),
            patch.dict(app.active_scribe_managers, {}, clear=True),
        ):
            manager = await app._get_or_create_scribe_manager("room")
        self.assertEqual(manager.keyterms, ["pin"])


if __name__ == "__main__":
    unittest.main()
