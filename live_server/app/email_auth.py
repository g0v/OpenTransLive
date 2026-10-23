# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import datetime, timezone

from pymongo import ReturnDocument

from .config import IS_PRODUCTION
from .logger_config import setup_logger

logger = setup_logger(__name__)

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

OTP_TTL = 600  # seconds (10 minutes)
OTP_MAX_ATTEMPTS = 5
OTP_CHALLENGE_COOKIE = "otl_otp"  # matches the otl_ API-key prefix
OTP_COOLDOWN_BASE = 60  # seconds; doubles per burn
OTP_COOLDOWN_MAX = 900  # seconds
OTP_BURN_WINDOW = 86400  # seconds; attempt and burn counters live this long
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _mask_email(email: str | None) -> str:
    if not email:
        return "unknown"
    parts = email.split("@", 1)
    if len(parts) != 2:
        return "***"
    local, domain = parts
    if len(local) <= 2:
        masked_local = f"{local[:1]}***"
    else:
        masked_local = f"{local[:2]}***{local[-1:]}"
    return f"{masked_local}@{domain}"


def validate_email_format(email: str) -> bool:
    if not isinstance(email, str):
        return False
    return bool(_EMAIL_RE.match(email.strip()))


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def new_otp_challenge() -> str:
    return secrets.token_urlsafe(32)


def valid_otp_challenge(value) -> bool:
    return isinstance(value, str) and bool(_CHALLENGE_RE.match(value))


def client_network(host: str | None) -> str:
    """Network key for per-client limits: an IPv4 address as is, an IPv6 address
    as its /64 (one subscriber usually owns the whole prefix). IPv4-mapped IPv6
    is unmapped and a zone is stripped; a non-IP host is returned verbatim and
    an empty one as "unknown"."""
    if not host:
        return "unknown"
    raw = host.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _otp_key(prefix: str, email: str, part: str) -> str:
    digest = hashlib.sha256(f"{email.lower()}\x00{part}".encode("utf-8")).hexdigest()
    return prefix + digest


def _otp_keys(email: str, challenge: str, network: str) -> tuple[str, str, str, str]:
    """(code, attempts, burns, cooldown). The code is bound to the browser that
    requested it; guesses and cooldowns are counted per (email, network), so a
    stranger who knows the address can neither burn nor lock out its code."""
    return (
        _otp_key("auth:otp:", email, challenge),
        _otp_key("auth:otp-attempts:", email, network),
        _otp_key("auth:otp-burns:", email, network),
        _otp_key("auth:otp-cooldown:", email, network),
    )


async def store_otp(redis_client, email: str, otp: str, *, challenge: str, network: str) -> int:
    """Store a code for this browser's challenge. Returns 0 when stored, else the
    milliseconds of cooldown left (nothing stored). Never resets the attempt
    counter, so resending does not grant fresh guesses."""
    code_key, _, _, cooldown_key = _otp_keys(email, challenge, network)
    wait = await redis_client.pttl(cooldown_key)
    if wait is not None and wait > 0:
        return wait
    await redis_client.set(code_key, otp, px=OTP_TTL * 1000)
    return 0


async def discard_otp(redis_client, email: str, challenge: str) -> None:
    await redis_client.delete(_otp_key("auth:otp:", email, challenge))


async def verify_otp(redis_client, email: str, otp: str, *, challenge: str, network: str) -> bool:
    """Verify the code stored for this browser's challenge. OTP_MAX_ATTEMPTS wrong
    guesses from one network burn the code and start a cooldown that doubles per
    burn (OTP_COOLDOWN_BASE up to OTP_COOLDOWN_MAX)."""
    code_key, attempts_key, burns_key, cooldown_key = _otp_keys(email, challenge, network)
    stored = await redis_client.get(code_key)
    if stored is None:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8")

    pipe = redis_client.pipeline()
    pipe.incr(attempts_key)
    pipe.pexpire(attempts_key, OTP_BURN_WINDOW * 1000)
    attempts, _ = await pipe.execute()
    if attempts > OTP_MAX_ATTEMPTS:
        return False

    if hmac.compare_digest(stored, otp):
        # Only the request that actually deletes the code wins a concurrent race.
        if await redis_client.delete(code_key) == 1:
            await redis_client.delete(attempts_key, burns_key, cooldown_key)
            return True
        return False

    if attempts >= OTP_MAX_ATTEMPTS:
        await redis_client.delete(code_key, attempts_key)
        burns = await redis_client.incr(burns_key)
        await redis_client.pexpire(burns_key, OTP_BURN_WINDOW * 1000)
        cooldown = min(OTP_COOLDOWN_BASE * 2 ** (burns - 1), OTP_COOLDOWN_MAX)
        await redis_client.set(cooldown_key, "1", px=cooldown * 1000)
    return False


def _build_otp_email_text(otp: str) -> str:
    return (
        f"Your OpenTransLive login code is: {otp}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, you can safely ignore this email.\n"
    )


def _build_otp_email_html(otp: str) -> str:
    # Inline styles + table layout to satisfy email-client rendering rules.
    # Web-safe font stack, 600px max width, no external assets.
    return f"""\
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light dark" />
<meta name="supported-color-schemes" content="light dark" />
<title>Your OpenTransLive login code</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
    Your OpenTransLive login code is {otp}. Expires in 10 minutes.
  </div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#f4f5f7;">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(17,24,39,0.08);">
          <tr>
            <td style="background-color:#111827;padding:28px 32px;">
              <h1 style="margin:0;font-size:20px;font-weight:700;color:#f3f4f6;letter-spacing:-0.01em;">OpenTransLive</h1>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 32px 8px 32px;">
              <h2 style="margin:0 0 12px 0;font-size:22px;font-weight:600;color:#111827;line-height:1.3;">Your login code</h2>
              <p style="margin:0;font-size:15px;line-height:1.6;color:#4b5563;">
                Use the code below to finish signing in. It expires in 10 minutes.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 32px 8px 32px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td align="center" style="background-color:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:24px 16px;">
                    <div style="font-family:'SFMono-Regular',Menlo,Consolas,'Liberation Mono',monospace;font-size:34px;font-weight:700;letter-spacing:0.35em;color:#111827;">{otp}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 32px 32px 32px;">
              <p style="margin:0;font-size:13px;line-height:1.6;color:#6b7280;">
                If you did not request this code, you can safely ignore this email. Someone may have typed your address by mistake.
              </p>
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #e5e7eb;padding:20px 32px;">
              <p style="margin:0;font-size:12px;line-height:1.5;color:#9ca3af;">
                This is an automated message from OpenTransLive. Please do not reply.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


_SMTP_IMPLICIT_TLS_PORT = 465  # SMTPS: TLS from the first byte, no STARTTLS


def _smtp_tls_options(port: int, use_tls_setting: bool) -> dict:
    """Port 465 speaks implicit TLS and is never upgraded with STARTTLS; other
    ports upgrade via STARTTLS when SMTP_USE_TLS is set."""
    if port == _SMTP_IMPLICIT_TLS_PORT:
        return {"use_tls": True, "start_tls": False}
    return {"use_tls": False, "start_tls": use_tls_setting}


async def send_otp_email(email: str, otp: str, email_settings: dict) -> None:
    smtp_host = email_settings.get("SMTP_HOST", "")
    if not smtp_host:
        if IS_PRODUCTION:
            # Never log the OTP here: production logs are shipped and retained,
            # and a plaintext code is enough to take over the account.
            logger.error(
                "SMTP_HOST is not configured, OTP email not sent email=%s",
                _mask_email(email),
            )
        else:
            # One record so concurrent requests can't interleave a code with
            # the wrong address.
            logger.info(
                "[DEV MODE] OTP generated email=%s otp=%s smtp_configured=false",
                _mask_email(email),
                otp,
            )
        return

    import aiosmtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_from = email_settings.get("SMTP_FROM", "noreply@example.com")
    subject = "Your OpenTransLive login code"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = email

    msg.attach(MIMEText(_build_otp_email_text(otp), "plain", "utf-8"))
    msg.attach(MIMEText(_build_otp_email_html(otp), "html", "utf-8"))

    port = int(email_settings.get("SMTP_PORT", 587))
    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=port,
        username=email_settings.get("SMTP_USERNAME") or None,
        password=email_settings.get("SMTP_PASSWORD") or None,
        **_smtp_tls_options(port, bool(email_settings.get("SMTP_USE_TLS", True))),
    )


async def get_or_create_user(users_collection, email: str, user_uid: str) -> dict:
    """Upsert a user by email. user_uid is fixed on first login and preserved
    across subsequent logins so multiple devices on the same email share one
    canonical id — keeps legacy rooms (admin_uid only, no admin_email) reachable."""
    now = datetime.now(timezone.utc)
    email = email.lower().strip()
    result = await users_collection.find_one_and_update(
        {"email": email},
        {
            "$set": {"last_login_at": now, "last_active_at": now},
            "$setOnInsert": {
                "email": email,
                "user_uid": user_uid,
                "realtime_enabled": False,
                "created_at": now,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result
