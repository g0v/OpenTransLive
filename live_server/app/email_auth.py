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


async def send_otp_email(email: str, otp: str, email_settings: dict) -> None:
    smtp_host = email_settings.get("SMTP_HOST", "")
    if not smtp_host:
        logger.info("[DEV MODE] OTP generated email=%s smtp_configured=false", _mask_email(email))
        return

    import aiosmtplib
    from email.mime.text import MIMEText

    smtp_from = email_settings.get("SMTP_FROM", "noreply@example.com")
    subject = "Your login code"
    body = (
        f"Your OpenTransLive login code is: {otp}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, ignore this email."
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = email

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
