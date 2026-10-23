import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import app.translation_service as translation_service

SEGMENT = {
    "text": "raw",
    "start_time": 10.0,
    "end_time": 11.0,
    "partial": False,
    "result": {"corrected": "line", "translated": {"en": "line en"}},
}
CONTEXT = [{"start_time": 9.0, "result": {"translated": {"de": "vorher"}}}]


class _Rooms:
    async def find_one(self, query):
        return {"sid": query["sid"], "secret_key": "holder-key"}


def _request(body, secret_key="holder-key"):
    session = {"secret_key": secret_key} if secret_key else {}
    return SimpleNamespace(session=session, headers={}, json=AsyncMock(return_value=body))


class SegmentEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.load = AsyncMock(return_value=(dict(SEGMENT), CONTEXT))
        self.persist = AsyncMock()
        self.publish = AsyncMock()
        self.translate = AsyncMock(return_value="neu")
        for target, name, value in (
            (app, "rooms_collection", _Rooms()),
            (app, "_load_committed_segment_with_context", self.load),
            (app, "_persist_segment_revision", self.persist),
            (app, "_publish_segment_updated", self.publish),
            (translation_service, "translate_committed_language", self.translate),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _status(self, endpoint, body, **request_kwargs) -> int:
        with self.assertRaises(HTTPException) as ctx:
            await endpoint(_request(body, **request_kwargs), "room")
        return ctx.exception.status_code

    async def test_both_routes_require_the_panel_lock(self) -> None:
        body = {"start_time": 10.0, "corrected": "x", "language": "de"}
        for endpoint in (app.update_live_segment_endpoint, app.retranslate_segment_language_endpoint):
            with self.subTest(endpoint=endpoint.__name__):
                self.assertEqual(await self._status(endpoint, body, secret_key=None), 401)
                self.assertEqual(await self._status(endpoint, body, secret_key="other-key"), 403)

    async def test_live_update_publishes_the_merged_segment(self) -> None:
        result = await app.update_live_segment_endpoint(
            _request({"start_time": 10.0, "translated": {"ja": "typed"}}), "room"
        )
        self.assertEqual(result["segment"]["result"]["translated"], {"en": "line en", "ja": "typed"})
        self.publish.assert_awaited_once()

    async def test_live_update_rejects_an_empty_body(self) -> None:
        self.assertEqual(await self._status(app.update_live_segment_endpoint, {"start_time": 10.0}), 400)

    async def test_retranslate_language_validation(self) -> None:
        valid = {"start_time": 10.0, "corrected": "line", "language": "de"}
        cases = {
            "dotted language": {**valid, "language": "a.b"},
            "dollar language": {**valid, "language": "$en"},
            "missing language": {k: v for k, v in valid.items() if k != "language"},
            "missing start_time": {k: v for k, v in valid.items() if k != "start_time"},
            "blank source": {**valid, "corrected": "  "},
            "long source": {**valid, "corrected": "x" * 5001},
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(await self._status(app.retranslate_segment_language_endpoint, body), 400)
        self.translate.assert_not_awaited()

    async def test_retranslate_language_returns_text_without_storing(self) -> None:
        result = await app.retranslate_segment_language_endpoint(
            _request({"start_time": 10.0, "corrected": " edited ", "language": "de"}), "room"
        )
        self.assertEqual(result, {"translation": "neu"})
        self.assertEqual(self.translate.await_args.args[:4], ("room", "edited", "de", CONTEXT))
        self.persist.assert_not_awaited()
        self.publish.assert_not_awaited()

    async def test_retranslate_language_failure_is_502(self) -> None:
        for outcome in (None, "  ", RuntimeError("provider down")):
            with self.subTest(outcome=outcome):
                self.translate.side_effect = outcome if isinstance(outcome, Exception) else None
                self.translate.return_value = outcome
                body = {"start_time": 10.0, "corrected": "line", "language": "de"}
                self.assertEqual(await self._status(app.retranslate_segment_language_endpoint, body), 502)

    async def test_retranslate_language_for_missing_segment_is_404(self) -> None:
        self.load.return_value = (None, [])
        body = {"start_time": 10.0, "corrected": "line", "language": "de"}
        self.assertEqual(await self._status(app.retranslate_segment_language_endpoint, body), 404)
        self.translate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
