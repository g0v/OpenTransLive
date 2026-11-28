# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import app
import app.translation_service as ts


class _RedisStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def mget(self, *keys: str):
        return [self.data.get(k) for k in keys]

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


class _JsonRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class AutoKeywordsTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_true_and_reads_mongo_fallback(self) -> None:
        redis_store = _RedisStore()
        rooms = MagicMock()
        rooms.find_one = AsyncMock(return_value=None)
        with patch.object(ts, "rooms_collection", rooms):
            self.assertTrue(await ts.get_session_auto_keywords(redis_store, "room-1"))
            self.assertEqual(redis_store.data.get("auto_keywords:room-1"), "1")

        redis_store2 = _RedisStore()
        rooms.find_one = AsyncMock(return_value={"auto_keywords": False})
        with patch.object(ts, "rooms_collection", rooms):
            self.assertFalse(await ts.get_session_auto_keywords(redis_store2, "room-2"))
            self.assertEqual(redis_store2.data.get("auto_keywords:room-2"), "0")

    async def test_rerank_keywords_respects_extract_flag(self) -> None:
        redis_store = _RedisStore()
        redis_store.data["keywords:room-1"] = json.dumps({"Alpha": 5, "Beta": 5, "Pinned": 1, "Gone": -99})
        redis_store.data["locked_keywords:room-1"] = json.dumps(["Pinned"])

        translator = MagicMock()
        translator.extract_keywords = AsyncMock(return_value=["Extracted"])

        with patch.object(ts, "get_translator", return_value=translator):
            await ts.rerank_keywords(
                redis_store,
                "room-1",
                None,
                "We discussed Alpha today.",
                provider=None,
                extract=False,
            )

        translator.extract_keywords.assert_not_awaited()
        saved = json.loads(redis_store.data["keywords:room-1"])
        self.assertEqual(saved["Alpha"], 6)
        self.assertEqual(saved["Beta"], 4)
        self.assertEqual(saved["Pinned"], 1)
        self.assertNotIn("Gone", saved)
        self.assertNotIn("Extracted", saved)

        with patch.object(ts, "get_translator", return_value=translator):
            await ts.rerank_keywords(
                redis_store,
                "room-1",
                saved,
                "Extracted appeared too.",
                provider=None,
                extract=True,
            )

        translator.extract_keywords.assert_awaited_once()
        saved2 = json.loads(redis_store.data["keywords:room-1"])
        self.assertIn("Extracted", saved2)

        # When extract=None (default), rerank_keywords reads the session setting.
        translator.extract_keywords.reset_mock()
        redis_store.data["auto_keywords:room-1"] = "0"
        with patch.object(ts, "get_translator", return_value=translator):
            await ts.rerank_keywords(
                redis_store,
                "room-1",
                saved2,
                "Extracted should not run again.",
                provider=None,
            )
        translator.extract_keywords.assert_not_awaited()

    async def test_mention_resets_negative_score(self) -> None:
        redis_store = _RedisStore()
        redis_store.data["keywords:room-1"] = json.dumps({"Quiet": -58, "Steady": 3, "Fresh": 1})
        redis_store.data["locked_keywords:room-1"] = json.dumps([])

        await ts.rerank_keywords(
            redis_store, "room-1", None, "Quiet and Steady are back.", extract=False,
        )

        saved = json.loads(redis_store.data["keywords:room-1"])
        self.assertEqual(saved["Quiet"], 1)
        self.assertEqual(saved["Steady"], 4)
        self.assertEqual(saved["Fresh"], 0)

    async def test_endpoints_validate_and_persist(self) -> None:
        redis_store = _RedisStore()
        require_owner = AsyncMock()
        emit_update = AsyncMock()
        rooms = MagicMock()
        rooms.update_one = AsyncMock()
        with (
            patch.object(app, "redis_client", redis_store),
            patch.object(app, "_require_session_owner", require_owner),
            patch.object(app, "_emit_session_settings_update", emit_update),
            patch.object(ts, "rooms_collection", rooms),
        ):
            for bad in ("false", 0, None):
                with self.assertRaises(HTTPException) as ctx:
                    await app.update_session_auto_keywords_endpoint(_JsonRequest({"enabled": bad}), "room-1")
                self.assertEqual(ctx.exception.status_code, 400)

            res_post = await app.update_session_auto_keywords_endpoint(_JsonRequest({"enabled": False}), "room-1")
            self.assertEqual(res_post, {"enabled": False})
            emit_update.assert_awaited_once_with("room-1", "auto-keywords")

            res_get = await app.get_session_auto_keywords_endpoint(object(), "room-1")
            self.assertEqual(res_get, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
