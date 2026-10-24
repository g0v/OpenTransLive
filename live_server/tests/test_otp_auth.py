import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.responses import Response

import app
from app import email_auth
from app.email_auth import (
    OTP_CHALLENGE_COOKIE,
    client_network,
    new_otp_challenge,
    store_otp,
    verify_otp,
)

EMAIL = "victim@example.com"
NET_A = "203.0.113.5"
NET_B = "198.51.100.7"


class _Redis:
    """Async Redis fake with millisecond TTLs on a manual clock."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.expiry: dict[str, int] = {}
        self.now = 0

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1000)

    def _alive(self, key: str) -> bool:
        deadline = self.expiry.get(key)
        if deadline is not None and deadline <= self.now:
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.data

    async def get(self, key):
        return self.data.get(key) if self._alive(key) else None

    async def set(self, key, value, px=None):
        self.data[key] = value
        if px is None:
            self.expiry.pop(key, None)
        else:
            self.expiry[key] = self.now + px

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self._alive(key):
                removed += 1
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return removed

    async def incr(self, key):
        value = int(self.data.get(key, 0)) + 1 if self._alive(key) else 1
        self.data[key] = value
        return value

    async def pexpire(self, key, ms):
        if self._alive(key):
            self.expiry[key] = self.now + ms
            return 1
        return 0

    async def pttl(self, key):
        if not self._alive(key):
            return -2
        deadline = self.expiry.get(key)
        return -1 if deadline is None else deadline - self.now

    def pipeline(self):
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, redis: _Redis) -> None:
        self._redis = redis
        self._calls = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._calls.append((name, args, kwargs))
        return queue

    async def execute(self):
        return [await getattr(self._redis, n)(*a, **k) for n, a, k in self._calls]


class OtpStateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.redis = _Redis()
        self.victim = new_otp_challenge()
        self.attacker = new_otp_challenge()

    async def _store(self, otp, challenge, network=NET_A):
        return await store_otp(self.redis, EMAIL, otp, challenge=challenge, network=network)

    async def _verify(self, otp, challenge, network=NET_A):
        return await verify_otp(self.redis, EMAIL, otp, challenge=challenge, network=network)

    async def _burn(self, challenge, network=NET_A):
        await self._store("111111", challenge, network)
        for _ in range(email_auth.OTP_MAX_ATTEMPTS):
            self.assertFalse(await self._verify("000000", challenge, network))

    async def test_challenges_hold_independent_codes(self) -> None:
        await self._store("123456", self.victim)
        await self._store("654321", self.attacker, NET_B)
        self.assertFalse(await self._verify("123456", self.attacker, NET_B))
        self.assertTrue(await self._verify("123456", self.victim))

    async def test_burning_another_challenge_leaves_the_victim_code_valid(self) -> None:
        await self._store("123456", self.victim)
        await self._burn(self.attacker, NET_B)
        self.assertTrue(await self._verify("123456", self.victim))

    async def test_resend_replaces_code_without_resetting_attempts(self) -> None:
        await self._store("111111", self.victim)
        for _ in range(4):
            self.assertFalse(await self._verify("000000", self.victim))
        await self._store("222222", self.victim)
        self.assertFalse(await self._verify("111111", self.victim))  # 5th guess burns
        self.assertGreater(await self._store("333333", self.victim), 0)

    async def test_four_wrong_then_right_succeeds(self) -> None:
        await self._store("123456", self.victim)
        for _ in range(4):
            self.assertFalse(await self._verify("000000", self.victim))
        self.assertTrue(await self._verify("123456", self.victim))

    async def test_fifth_wrong_guess_burns_and_starts_cooldown(self) -> None:
        await self._burn(self.victim)
        self.assertGreater(await self._store("222222", self.victim), 0)

    async def test_cooldown_doubles_per_burn_up_to_the_cap(self) -> None:
        for expected in (60, 120, 240, 480, 900, 900):
            await self._burn(self.victim)
            self.assertEqual(await self._store("222222", self.victim), expected * 1000)
            self.redis.advance(expected)

    async def test_success_clears_attempts_burns_and_cooldown(self) -> None:
        await self._burn(self.victim)
        self.redis.advance(60)
        await self._store("123456", self.victim)
        self.assertFalse(await self._verify("000000", self.victim))
        self.assertTrue(await self._verify("123456", self.victim))
        self.assertEqual(self.redis.data, {})
        # Burns restarted: the next burn cools down for the base minute again.
        await self._burn(self.victim)
        self.assertEqual(await self._store("222222", self.victim), 60_000)

    async def test_guess_without_a_stored_code_counts_nothing(self) -> None:
        for _ in range(10):
            self.assertFalse(await self._verify("000000", self.victim))
        self.assertEqual(self.redis.data, {})

    async def test_cooldown_is_per_network(self) -> None:
        await self._burn(self.victim)
        self.assertEqual(await self._store("222222", self.victim, NET_B), 0)

    async def test_code_is_single_use(self) -> None:
        await self._store("123456", self.victim)
        self.assertTrue(await self._verify("123456", self.victim))
        self.assertFalse(await self._verify("123456", self.victim))

    def test_client_network(self) -> None:
        cases = {
            "203.0.113.5": "203.0.113.5",
            "2001:db8::1": "2001:db8::/64",
            "2001:db8::ffff": "2001:db8::/64",
            "fe80::1%eth0": "fe80::/64",
            "::ffff:192.0.2.1": "192.0.2.1",
            "garbage": "garbage",
            None: "unknown",
        }
        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(client_network(host), expected)


def _request(body: dict, cookies: dict | None = None, host: str = NET_A):
    return SimpleNamespace(
        json=AsyncMock(return_value=body),
        cookies=cookies or {},
        client=SimpleNamespace(host=host),
        session={},
    )


class OtpRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.redis = _Redis()
        self.send_email = AsyncMock()
        for target, name, value in (
            (app, "redis_client", self.redis),
            (app, "send_otp_email", self.send_email),
            (app, "get_or_create_user", AsyncMock(return_value={"user_uid": "uid-1"})),
            (app, "generate_otp", lambda: "123456"),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _send(self, cookies=None):
        response = Response()
        await app.send_otp(_request({"email": EMAIL}, cookies), response)
        return response

    @staticmethod
    def _cookie(response: Response) -> str:
        return response.headers.get("set-cookie", "")

    @staticmethod
    def _challenge(response: Response) -> str:
        return response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    async def test_send_sets_a_scoped_httponly_cookie(self) -> None:
        cookie = self._cookie(await self._send())
        self.assertTrue(cookie.startswith(f"{OTP_CHALLENGE_COOKIE}="))
        for attr in ("HttpOnly", "Max-Age=600", "Path=/auth", "SameSite=lax"):
            self.assertIn(attr, cookie)

    async def test_valid_cookie_is_reused_and_malformed_replaced(self) -> None:
        challenge = new_otp_challenge()
        self.assertEqual(self._challenge(await self._send({OTP_CHALLENGE_COOKIE: challenge})), challenge)
        replaced = self._challenge(await self._send({OTP_CHALLENGE_COOKIE: "short"}))
        self.assertNotEqual(replaced, "short")
        self.assertTrue(email_auth.valid_otp_challenge(replaced))

    async def test_cooldown_returns_429_with_retry_after_and_no_cookie(self) -> None:
        challenge = new_otp_challenge()
        await self._send({OTP_CHALLENGE_COOKIE: challenge})
        for _ in range(email_auth.OTP_MAX_ATTEMPTS):
            self.assertFalse(await verify_otp(self.redis, EMAIL, "000000", challenge=challenge, network=NET_A))
        response = Response()
        with self.assertRaises(HTTPException) as ctx:
            await app.send_otp(_request({"email": EMAIL}, {OTP_CHALLENGE_COOKIE: challenge}), response)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers["Retry-After"], "60")
        self.assertNotIn("set-cookie", response.headers)

    async def test_verify_without_cookie_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await app.verify_otp_endpoint(_request({"email": EMAIL, "otp": "123456"}), Response())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Request a new sign-in code in this browser.")

    async def test_verify_success_deletes_the_cookie(self) -> None:
        challenge = self._challenge(await self._send())
        response = Response()
        result = await app.verify_otp_endpoint(
            _request({"email": EMAIL, "otp": "123456"}, {OTP_CHALLENGE_COOKIE: challenge}), response
        )
        self.assertEqual(result["status"], "ok")
        cookie = self._cookie(response)
        self.assertIn(f'{OTP_CHALLENGE_COOKIE}=""', cookie)
        self.assertIn("Max-Age=0", cookie)
        self.assertIn("Path=/auth", cookie)

    async def test_send_failure_discards_the_code(self) -> None:
        self.send_email.side_effect = RuntimeError("smtp down")
        challenge = new_otp_challenge()
        with self.assertRaises(HTTPException) as ctx:
            await self._send({OTP_CHALLENGE_COOKIE: challenge})
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self.redis.data, {})

    def test_send_route_limits_are_ordered_client_first(self) -> None:
        # Direct endpoint calls bypass the dependencies, so only this guards the
        # order: per-network limits must reject a flood before it reaches the
        # per-email budgets it would otherwise exhaust for the targeted address.
        route = next(r for r in app.app.routes if getattr(r, "path", "") == "/auth/send-otp")
        limits = [(d.dependency.times, d.dependency.milliseconds, d.dependency.identifier) for d in route.dependencies]
        minute, hour, day = 60_000, 3_600_000, 86_400_000
        self.assertEqual(limits, [
            (30, 10 * minute, app._otp_network_identifier),
            (100, day, app._otp_network_identifier),
            (5, hour, app._otp_email_network_identifier),
            (10, day, app._otp_email_network_identifier),
            (10, minute, app._otp_email_identifier),
            (30, day, app._otp_email_identifier),
        ])


if __name__ == "__main__":
    unittest.main()
