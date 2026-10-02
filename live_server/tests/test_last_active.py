import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app


class LastActiveTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app._last_active_touched.clear()
        self.addCleanup(app._last_active_touched.clear)

    async def _stream_chunk(self, session: dict) -> list[str]:
        """Push one chunk on an already-authorized socket; return DB/queue call order."""
        order: list[str] = []
        users = AsyncMock()
        users.update_one.side_effect = lambda *a, **k: order.append(f"touch:{a[0]['email']}")
        manager = SimpleNamespace(is_running=True, _usage_restored=True,
                                  push_audio=AsyncMock(side_effect=lambda _a: order.append("push")))
        with (
            patch.object(app, "users_collection", users),
            patch.object(app.sio, "get_session", AsyncMock(return_value=session)),
            patch.dict(app.active_scribe_managers, {"room": manager}, clear=True),
            patch.dict(app.active_translation_managers, {}, clear=True),
        ):
            await app.audio_buffer_append("socket-a", {"audio": base64.b64encode(b"\0" * 320).decode()})
        return order

    async def test_streaming_api_key_socket_records_activity_after_auth_is_cached(self) -> None:
        # Every chunk after the first skips re-authorization; a broadcaster pushing
        # for hours must still advance last_active_at, not freeze at connect time.
        # The write follows the enqueue so concurrent chunks cannot overtake it.
        order = await self._stream_chunk({"session_id": "room", "realtime_authorized": True,
                                          "auth_via": "api_key", "email": "Caster@Example.com"})
        self.assertEqual(order, ["push", "touch:caster@example.com"])

    async def test_secret_key_panel_socket_does_not_credit_the_room_owner(self) -> None:
        # A secret_key socket's session email is the room owner's even when a
        # co-owner drives the panel; crediting it would mark an absent owner active.
        order = await self._stream_chunk({"session_id": "room", "realtime_authorized": True,
                                          "secret_key": "s", "email": "owner@example.com"})
        self.assertEqual(order, ["push"])

    async def test_heartbeat_renews_the_lock_before_crediting_the_panel_driver(self) -> None:
        # The lock expires after ADMIN_TIMEOUT; a slow users write must not delay
        # the room renewal. The cookie identity (here a co-owner) gets the credit.
        order: list[str] = []
        rooms, users = AsyncMock(), AsyncMock()
        rooms.update_one.side_effect = lambda *a, **k: order.append("lock")
        users.update_one.side_effect = lambda *a, **k: order.append(f"touch:{a[0]['email']}")
        with (
            patch.object(app, "_verify_session_lock_holder",
                         AsyncMock(return_value={"sid": "room", "admin_email": "owner@example.com"})),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=0)),
            patch.object(app, "rooms_collection", rooms),
            patch.object(app, "users_collection", users),
            patch.dict(app.active_scribe_managers, {}, clear=True),
            patch.dict(app.active_translation_managers, {}, clear=True),
        ):
            await app.heartbeat(SimpleNamespace(session={"email": "coowner@example.com"}), "room")
        self.assertEqual(order, ["lock", "touch:coowner@example.com"])

    async def test_touch_writes_once_per_window_and_retries_after_failure(self) -> None:
        users = AsyncMock()
        with patch.object(app, "users_collection", users):
            for _ in range(3):
                await app._touch_last_active("a@example.com")
        self.assertEqual(users.update_one.await_count, 1)

        failing = AsyncMock()
        failing.update_one.side_effect = RuntimeError("mongo down")
        with patch.object(app, "users_collection", failing):
            await app._touch_last_active("b@example.com")
        # A failed write must not suppress the account for the rest of the window.
        with patch.object(app, "users_collection", users):
            await app._touch_last_active("b@example.com")
        self.assertEqual(users.update_one.await_count, 2)


if __name__ == "__main__":
    unittest.main()
