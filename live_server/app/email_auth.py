# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import re
import secrets
from datetime import datetime, timezone

from pymongo import ReturnDocument

from .logger_config import setup_logger

logger = setup_logger(__name__)

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

OTP_TTL = 600  # seconds (10 minutes)
OTP_MAX_ATTEMPTS = 5


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


def _otp_keys(email: str) -> tuple[str, str]:
    key_email = email.lower()
    return f"auth:otp:{key_email}", f"auth:otp:{key_email}:attempts"


async def store_otp(redis_client, email: str, otp: str) -> None:
    otp_key, attempts_key = _otp_keys(email)
    pipe = redis_client.pipeline()
    pipe.setex(otp_key, OTP_TTL, otp)
    pipe.delete(attempts_key)
    await pipe.execute()


async def verify_otp(redis_client, email: str, otp: str) -> bool:
    """Verify OTP. Burns the code after OTP_MAX_ATTEMPTS wrong guesses so an
    attacker cannot brute-force the 10-minute TTL across concurrent connections."""
    otp_key, attempts_key = _otp_keys(email)
    stored = await redis_client.get(otp_key)
    if stored is None:
        return False
    if stored != otp:
        attempts = await redis_client.incr(attempts_key)
        if attempts == 1:
            await redis_client.expire(attempts_key, OTP_TTL)
        if attempts >= OTP_MAX_ATTEMPTS:
            pipe = redis_client.pipeline()
            pipe.delete(otp_key)
            pipe.delete(attempts_key)
            await pipe.execute()
        return False
    pipe = redis_client.pipeline()
    pipe.delete(otp_key)
    pipe.delete(attempts_key)
    await pipe.execute()
    return True


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


async def send_otp_email(email: str, otp: str, email_settings: dict) -> None:
    smtp_host = email_settings.get("SMTP_HOST", "")
    if not smtp_host:
        logger.info("[DEV MODE] OTP generated email=%s smtp_configured=false", _mask_email(email))
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

    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=email_settings.get("SMTP_PORT", 587),
        username=email_settings.get("SMTP_USERNAME") or None,
        password=email_settings.get("SMTP_PASSWORD") or None,
        use_tls=False,
        start_tls=email_settings.get("SMTP_USE_TLS", True),
    )


async def get_or_create_user(users_collection, email: str, user_uid: str) -> dict:
    """Upsert a user by email. Always updates user_uid on login."""
    now = datetime.now(timezone.utc)
    email = email.lower().strip()
    result = await users_collection.find_one_and_update(
        {"email": email},
        {
            "$set": {"user_uid": user_uid, "last_login_at": now},
            "$setOnInsert": {
                "email": email,
                "realtime_enabled": False,
                "created_at": now,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result
