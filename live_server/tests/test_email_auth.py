import unittest
from unittest.mock import AsyncMock, patch

import aiosmtplib

from app.email_auth import send_otp_email


def _settings(**overrides) -> dict:
    return {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "noreply@example.com", **overrides}


class SmtpTlsModeTest(unittest.IsolatedAsyncioTestCase):
    async def _send(self, settings: dict) -> AsyncMock:
        with patch.object(aiosmtplib, "send", AsyncMock()) as send:
            await send_otp_email("user@example.com", "123456", settings)
        return send

    async def test_port_465_uses_implicit_tls(self) -> None:
        send = await self._send(_settings(SMTP_PORT=465))
        kwargs = send.await_args.kwargs
        self.assertTrue(kwargs["use_tls"])
        self.assertFalse(kwargs["start_tls"])

    async def test_port_465_ignores_use_tls_false(self) -> None:
        send = await self._send(_settings(SMTP_PORT=465, SMTP_USE_TLS=False))
        kwargs = send.await_args.kwargs
        self.assertTrue(kwargs["use_tls"])
        self.assertFalse(kwargs["start_tls"])

    async def test_port_587_upgrades_with_starttls(self) -> None:
        send = await self._send(_settings(SMTP_PORT=587))
        kwargs = send.await_args.kwargs
        self.assertFalse(kwargs["use_tls"])
        self.assertTrue(kwargs["start_tls"])

    async def test_plain_relay_without_tls(self) -> None:
        send = await self._send(_settings(SMTP_PORT=1025, SMTP_USE_TLS=False))
        kwargs = send.await_args.kwargs
        self.assertFalse(kwargs["use_tls"])
        self.assertFalse(kwargs["start_tls"])

    async def test_string_port_465_is_implicit_tls(self) -> None:
        send = await self._send(_settings(SMTP_PORT="465"))
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["port"], 465)
        self.assertTrue(kwargs["use_tls"])

    async def test_no_host_skips_sending(self) -> None:
        send = await self._send(_settings(SMTP_HOST=""))
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
