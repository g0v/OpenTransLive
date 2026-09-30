import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
from app import translation_service


class _Redis:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = str(value) if isinstance(value, int) else value

    async def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


class _Request:
    def __init__(self, body) -> None:
        self._body = body

    async def json(self):
        return self._body


class SessionLanguageLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_effective_languages_keep_first_n_and_zero_means_unlimited(self) -> None:
        redis = _Redis({"languages:room": json.dumps(["en", "ja", "ko"])})
        for owner, expected in (({"max_languages": 2}, ["en", "ja"]), ({}, ["en", "ja", "ko"])):
            redis.data.pop("max_languages:room", None)
            with patch.object(translation_service, "_resolve_owner_overrides", AsyncMock(return_value=owner)):
                self.assertEqual(await translation_service.get_session_languages(redis, "room"), expected)

    async def test_nonpositive_cached_limit_means_unlimited(self) -> None:
        redis = _Redis({"languages:room": json.dumps(["en", "ja", "ko"]), "max_languages:room": "-1"})
        self.assertEqual(await translation_service.get_session_languages(redis, "room"), ["en", "ja", "ko"])

    async def test_update_rejects_more_languages_than_the_owner_limit(self) -> None:
        redis = _Redis({"max_languages:room": "2"})
        save = AsyncMock()
        with (
            patch.object(app, "redis_client", redis),
            patch.object(app, "_require_session_owner", AsyncMock()),
            patch.object(app, "_emit_session_settings_update", AsyncMock()),
            patch.object(translation_service, "save_session_languages", save),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await app.update_session_languages_endpoint(_Request({"languages": ["en", "ja", "ko"]}), "room")
            self.assertEqual(ctx.exception.status_code, 400)
            save.assert_not_awaited()

            result = await app.update_session_languages_endpoint(_Request({"languages": ["en", "ja"]}), "room")
        self.assertEqual(result, {"languages": ["en", "ja"], "max_languages": 2})
        save.assert_awaited_once()


class AdminLanguageLimitTest(unittest.IsolatedAsyncioTestCase):
    async def _set(self, body, stored=None):
        users = AsyncMock()
        users.find_one_and_update = AsyncMock(return_value={"email": "u@example.com", **(stored or {})})
        redis = _Redis({"max_languages:r1": "5", "ai_provider:r1": "gemini"})
        emit = AsyncMock()
        with (
            patch.object(app, "require_admin", AsyncMock()),
            patch.object(app, "users_collection", users),
            patch.object(app, "redis_client", redis),
            patch.object(app, "_owned_room_sids", AsyncMock(return_value=["r1"])),
            patch.object(app, "_emit_session_settings_update", emit),
        ):
            result = await app.set_user_settings(_Request(body), "u@example.com")
        return result, users.find_one_and_update.await_args.args[1], redis, emit

    async def test_limit_is_stored_and_pushed_to_open_panels(self) -> None:
        result, update, redis, emit = await self._set({"max_languages": "3"}, {"max_languages": 3})
        self.assertEqual(update, {"$set": {"max_languages": 3}})
        self.assertEqual(result["max_languages"], 3)
        self.assertNotIn("max_languages:r1", redis.data)
        self.assertIn("ai_provider:r1", redis.data)
        emit.assert_awaited_once_with("r1", "language-limit")

    async def test_zero_clears_the_limit(self) -> None:
        result, update, _, _ = await self._set({"max_languages": 0})
        self.assertEqual(update, {"$unset": {"max_languages": ""}})
        self.assertEqual(result["max_languages"], 0)

    async def test_invalid_limits_are_rejected(self) -> None:
        for value in (-1, 33, 1.5, True, "two"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as ctx:
                await self._set({"max_languages": value})
            self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
