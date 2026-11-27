# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import itsdangerous
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app


def _session_cookie(payload: dict) -> str:
    raw = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return itsdangerous.TimestampSigner(str(app.SETTINGS["SECRET_KEY"])).sign(raw).decode("utf-8")


class _FakeRequest:
    def __init__(self, endpoint_name="generate_session_keywords_endpoint", *, session=None, headers=None, host="198.51.100.9", path_params=None):
        self.session = dict(session or {})
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = SimpleNamespace(host=host)
        self.path_params = dict(path_params or {})
        self.scope = {"route": SimpleNamespace(endpoint=SimpleNamespace(__name__=endpoint_name))}


class LogoutSecurityTest(unittest.TestCase):
    def test_get_logout_is_method_not_allowed_and_post_requires_same_origin(self) -> None:
        client = TestClient(app.app, follow_redirects=False)
        res_get = client.get("/logout")
        self.assertEqual(res_get.status_code, 405)

        cookie = _session_cookie({"email": "user@example.com", "user_uid": "u-1"})
        client.cookies.set("session", cookie)

        with patch("fastapi_limiter.depends.RateLimiter.__call__", AsyncMock(return_value=None)):
            res_evil = client.post(
                "/logout",
                headers={"Origin": "https://evil.example", "Host": "testserver"},
            )
            self.assertEqual(res_evil.status_code, 403)

            res_spoofed_xfh = client.post(
                "/logout",
                headers={"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example", "Host": "testserver"},
            )
            self.assertEqual(res_spoofed_xfh.status_code, 403)

            res_cross_fetch = client.post(
                "/logout",
                headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver", "Host": "testserver"},
            )
            self.assertEqual(res_cross_fetch.status_code, 403)

            # TLS-terminated reverse proxy without X-Forwarded-Proto still accepts same-host HTTPS Origin.
            res_tls_proxy = client.post(
                "/logout",
                headers={"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin", "Host": "testserver"},
            )
            self.assertEqual(res_tls_proxy.status_code, 303)
            self.assertEqual(res_tls_proxy.headers["location"], "/login")

            res_ok = client.post(
                "/logout",
                headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "Host": "testserver"},
            )
            self.assertEqual(res_ok.status_code, 303)
            self.assertEqual(res_ok.headers["location"], "/login")


class RateLimiterHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def test_identifier_keys_by_network_or_account_and_ignores_sid(self) -> None:
        r1 = _FakeRequest(path_params={"sid": "room-a"})
        r2 = _FakeRequest(path_params={"sid": "room-b"})
        k1 = await app._identifier(r1)
        k2 = await app._identifier(r2)
        self.assertTrue(k1.startswith("net:"))
        self.assertEqual(k1, k2)
        self.assertEqual(r1.session, {})

        r_auth = _FakeRequest(session={"email": "a@b.com", "user_uid": "uid-42"}, path_params={"sid": "room-z"})
        self.assertEqual(await app._identifier(r_auth), "user:uid-42:generate_session_keywords_endpoint")

        r_key = _FakeRequest(headers={"Authorization": "Bearer otl_secret_token_value"})
        self.assertTrue((await app._identifier(r_key)).startswith("apikey:"))

    async def test_strict_rate_limiter_fails_closed_with_503_on_redis_error(self) -> None:
        dep = app._strict_rate_limiter(10, 60)
        req = _FakeRequest()
        resp = SimpleNamespace(headers={})
        with patch("fastapi_limiter.depends.RateLimiter.__call__", AsyncMock(side_effect=ConnectionError("redis down"))):
            with self.assertRaises(HTTPException) as ctx:
                await dep(req, resp)
            self.assertEqual(ctx.exception.status_code, 503)

        # Ensure AI generation routes wire strict rate limit dependencies (beyond plain fail-open RateLimiter).
        kw_route = next(r for r in app.app.routes if getattr(r, "path", None) == "/api/session/{sid}/keywords/generate")
        gl_route = next(r for r in app.app.routes if getattr(r, "path", None) == "/api/session/{sid}/glossary/generate")
        self.assertEqual(kw_route.dependencies[0].dependency.__qualname__, "_strict_rate_limiter.<locals>._dependency")
        self.assertEqual(gl_route.dependencies[0].dependency.__qualname__, "_strict_rate_limiter.<locals>._dependency")


class AdminDashboardRedirectTest(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_redirects_anonymous_and_non_admin(self) -> None:
        anon_req = SimpleNamespace(session={}, url=SimpleNamespace(path="/dashboard"))
        res_anon = await app.dashboard(anon_req)
        self.assertEqual(res_anon.status_code, 302)
        self.assertEqual(res_anon.headers["location"], "/login?next=/dashboard")

        user_req = SimpleNamespace(session={"email": "regular@example.com"}, url=SimpleNamespace(path="/dashboard"))
        with patch.dict(app.EMAIL_SETTINGS, {"ADMIN_EMAILS": ["admin@example.com"]}):
            res_user = await app.dashboard(user_req)
            self.assertEqual(res_user.status_code, 302)
            self.assertEqual(res_user.headers["location"], "/user-dashboard")

            report_req = SimpleNamespace(session={"email": "regular@example.com"}, url=SimpleNamespace(path="/dashboard/users/a@b.com"))
            res_report = await app.admin_user_report(report_req, "a@b.com")
            self.assertEqual(res_report.status_code, 302)
            self.assertEqual(res_report.headers["location"], "/user-dashboard")


if __name__ == "__main__":
    unittest.main()
