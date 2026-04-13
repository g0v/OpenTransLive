# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import random
import re
from datetime import datetime, timezone

from pymongo import ReturnDocument

from .logger_config import setup_logger

logger = setup_logger(__name__)

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

OTP_TTL = 600  # seconds (10 minutes)


def validate_email_format(email: str) -> bool:
    if not isinstance(email, str):
        return False
    return bool(_EMAIL_RE.match(email.strip()))


def generate_otp() -> str:
    return f"{random.randint(0, 9999):04d}"


async def store_otp(redis_client, email: str, otp: str) -> None:
    key = f"auth:otp:{email.lower()}"
    await redis_client.setex(key, OTP_TTL, otp)


async def verify_otp(redis_client, email: str, otp: str) -> bool:
    key = f"auth:otp:{email.lower()}"
    stored = await redis_client.get(key)
    if stored is None:
        return False
    if stored != otp:
        return False
    await redis_client.delete(key)
    return True


async def send_otp_email(email: str, otp: str, email_settings: dict) -> None:
    smtp_host = email_settings.get("SMTP_HOST", "")
    if not smtp_host:
        logger.info(f"[DEV MODE] OTP for {email}: {otp}")
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
