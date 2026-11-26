import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import itsdangerous
import socketio

import app

NET = "203.0.113.5"


class _Sio:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.emit = AsyncMock()
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.enter_room = AsyncMock()
        self.leave_room = AsyncMock()

    async def _disconnect(self, socket_id):
        self.sessions.pop(socket_id, None)

    async def save_session(self, socket_id, session):
        self.sessions[socket_id] = dict(session)

    async def get_session(self, socket_id):
        return dict(self.sessions[socket_id])

    def emitted_codes(self, socket_id):
        return [
            c.args[1]["code"] for c in self.emit.await_args_list
            if c.args[0] == "error" and c.kwargs.get("to") == socket_id
        ]


def _environ(host=NET, cookie=""):
    return {"asgi.scope": {"client": (host, 1234)}, "HTTP_COOKIE": cookie}


class OperatorSocketLimiterTest(unittest.TestCase):
    def test_per_network_cap(self) -> None:
        limiter = app._OperatorSocketLimiter()
        with patch.object(app, "_MAX_OPERATOR_SOCKETS_PER_NETWORK", 2):
            self.assertTrue(limiter.acquire("n1", "a"))
            self.assertTrue(limiter.acquire("n1", "b"))
            self.assertFalse(limiter.acquire("n1", "c"))
            self.assertTrue(limiter.acquire("n2", "c"))

    def test_per_account_and_anonymous_caps(self) -> None:
        limiter = app._OperatorSocketLimiter()
        with (
            patch.object(app, "_MAX_OPERATOR_SOCKETS_PER_ACCOUNT", 1),
            patch.object(app, "_MAX_ANONYMOUS_OPERATOR_SOCKETS", 1),
        ):
            self.assertTrue(limiter.acquire("n1", "a"))
            self.assertFalse(limiter.acquire("n2", "a"))
            self.assertTrue(limiter.acquire("n1", ""))
            self.assertFalse(limiter.acquire("n2", ""))
            limiter.release("n1", "")
            self.assertTrue(limiter.acquire("n2", ""))

    def test_total_cap_and_zero_count_cleanup(self) -> None:
        limiter = app._OperatorSocketLimiter()
        with patch.object(app, "_MAX_OPERATOR_SOCKETS", 2):
            self.assertTrue(limiter.acquire("n1", "a"))
            self.assertTrue(limiter.acquire("n2", "b"))
            self.assertFalse(limiter.acquire("n3", "c"))
        limiter.release("n1", "a")
        limiter.release("n2", "b")
        self.assertEqual((limiter._total, limiter._per_network, limiter._per_account), (0, {}, {}))


class SocketConnectTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sio = _Sio()
        self.limiter = app._OperatorSocketLimiter()
        self.slots: dict = {}
        self.deadlines: dict = {}
        for name, value in (
            ("sio", self.sio),
            ("_operator_sockets", self.limiter),
            ("_socket_slots", self.slots),
            ("_join_deadline_tasks", self.deadlines),
            ("_socket_upgrade_limiter", app._NetworkRateLimiter(max_calls=120, window=60.0)),
        ):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        for task in list(self.deadlines.values()):
            task.cancel()

    async def test_refuses_over_the_network_cap_and_releases_on_disconnect(self) -> None:
        with patch.object(app, "_MAX_OPERATOR_SOCKETS_PER_NETWORK", 1):
            await app.connect("s1", _environ(), None)
            with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
                await app.connect("s2", _environ(), None)
            self.assertNotIn("s2", self.slots)
            await app.disconnect("s1")
            self.assertEqual(self.slots, {})
            self.assertEqual(self.deadlines, {})
            await app.connect("s3", _environ(), None)

    async def test_refuses_the_121st_connect_per_minute(self) -> None:
        for i in range(120):
            await app.connect(f"s{i}", _environ(), None)
            await app.disconnect(f"s{i}")
        with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
            await app.connect("s120", _environ(), None)
        await app.connect("other", _environ(host="198.51.100.7"), None)

    async def test_failed_connect_releases_its_slot(self) -> None:
        with patch.object(app, "_auto_rejoin_from_auth", AsyncMock(side_effect=RuntimeError("db"))):
            with self.assertRaises(RuntimeError):
                await app.connect("s1", _environ(), None)
        self.assertEqual(self.slots, {})
        self.assertEqual(self.limiter._total, 0)

    async def test_unjoined_socket_is_disconnected_at_the_deadline(self) -> None:
        with patch.object(app, "_JOIN_DEADLINE_SECS", 0.05):
            await app.connect("idle", _environ(), None)
            await app.connect("joined", _environ(), None)
            session = await self.sio.get_session("joined")
            session["session_id"] = "room"
            await self.sio.save_session("joined", session)
            await asyncio.sleep(0.1)
        self.sio.disconnect.assert_awaited_once_with("idle")
        self.assertEqual(self.deadlines, {})

    async def test_session_cookie_resolves_the_account(self) -> None:
        payload = base64.b64encode(json.dumps({"user_uid": "uid-1"}).encode())
        signed = itsdangerous.TimestampSigner(str(app.SETTINGS["SECRET_KEY"])).sign(payload).decode()
        self.assertEqual(await app._socket_account(_environ(cookie=f"session={signed}"), None), "uid-1")
        forged = itsdangerous.TimestampSigner("not-the-secret").sign(payload).decode()
        self.assertEqual(await app._socket_account(_environ(cookie=f"session={forged}"), None), "")
        self.assertEqual(await app._socket_account(_environ(), None), "")


class SocketSyncScopeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sio = _Sio()
        self.verify = AsyncMock(return_value=True)
        self.process = AsyncMock()
        for name, value in (
            ("sio", self.sio),
            ("_verify_socket_credentials", self.verify),
            ("_process_transcription_update", self.process),
            ("_touch_socket_actor", AsyncMock()),
            ("_socket_limiter", app._SocketRateLimiter()),
        ):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _payload(sid="room"):
        return {"id": sid, "start_time": 1.0, "partial": True, "text": "hi", "secret_key": "k"}

    async def test_unjoined_socket_cannot_sync(self) -> None:
        await self.sio.save_session("s1", {"verified": False})
        await app.sync("s1", self._payload())
        self.assertEqual(self.sio.emitted_codes("s1"), ["unauthorized"])
        self.verify.assert_not_awaited()
        self.process.assert_not_awaited()

    async def test_sync_to_another_session_is_rejected(self) -> None:
        await self.sio.save_session("s1", {"verified": True, "session_id": "room"})
        await app.sync("s1", self._payload("other-room"))
        self.assertEqual(self.sio.emitted_codes("s1"), ["unauthorized"])
        self.verify.assert_not_awaited()

        await app.sync("s1", self._payload("room"))
        self.process.assert_awaited_once()

    async def test_sync_is_rejected_after_leaving(self) -> None:
        await self.sio.save_session("s1", {"verified": True, "session_id": "room"})
        await app.leave_session("s1", {"session_id": "room"})
        await app.sync("s1", self._payload())
        self.assertEqual(self.sio.emitted_codes("s1"), ["unauthorized"])
        self.verify.assert_not_awaited()


class SocketOutageAndShutdownTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sio = _Sio()
        self.limiter = app._OperatorSocketLimiter()
        self.slots: dict = {}
        self.deadlines: dict = {}
        for name, value in (
            ("sio", self.sio),
            ("_operator_sockets", self.limiter),
            ("_socket_slots", self.slots),
            ("_join_deadline_tasks", self.deadlines),
            ("_socket_limiter", app._SocketRateLimiter()),
            ("_socket_upgrade_limiter", app._NetworkRateLimiter(max_calls=120, window=60.0)),
        ):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_join_session_lookup_error_emits_service_unavailable_and_disconnects(self) -> None:
        await self.sio.save_session("s1", {"verified": False})
        rooms = MagicMock()
        rooms.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        with (
            patch.object(app, "rooms_collection", rooms),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=1)),
            patch.object(app, "_emit_joined_session", AsyncMock()) as joined,
            patch.object(app, "_emit_viewer_count", AsyncMock()),
            patch.object(app, "_replay_scribe_status", AsyncMock()),
        ):
            await app.join_session("s1", {"session_id": "room1", "secret_key": "k1"})
            self.assertNotIn("unauthorized", self.sio.emitted_codes("s1"))
            self.assertEqual(self.sio.emitted_codes("s1"), ["service_unavailable"])
            self.sio.disconnect.assert_awaited_once_with("s1")

            # After the backend recovers, the reconnect join succeeds cleanly.
            rooms.find_one = AsyncMock(return_value={"sid": "room1", "secret_key": "k1", "admin_email": "o@e.com"})
            await self.sio.save_session("s2", {"verified": False})
            await app.join_session("s2", {"session_id": "room1", "secret_key": "k1"})
            joined.assert_awaited_once()

    async def test_shutdown_disconnects_connected_sockets_and_refuses_new_connect(self) -> None:
        self.slots["s1"] = (NET, "u1")
        write_queue = MagicMock()
        write_queue.stop = AsyncMock()
        with (
            patch.object(app, "init_indexes", AsyncMock()),
            patch.object(app.FastAPILimiter, "init", AsyncMock()),
            patch.object(app.FastAPILimiter, "close", AsyncMock()),
            patch.object(app, "segment_write_queue", write_queue),
            patch("app.translators.close_translator", AsyncMock()),
            patch.object(app, "active_scribe_managers", {}),
            patch.object(app, "active_translation_managers", {}),
        ):
            async with app.lifespan(app.app):
                self.assertFalse(app._shutting_down)
                self.slots["s1"] = (NET, "u1")
            self.assertTrue(app._shutting_down)
            self.sio.disconnect.assert_awaited_with("s1")
            with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
                await app.connect("s2", _environ(), None)
        app._shutting_down = False


if __name__ == "__main__":
    unittest.main()
