# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
"""Per-user API key helpers.

A user holds at most one API key. We store only its SHA-256 hash (`api_key_hash`)
plus a short display id (`api_key_prefix`); the plaintext is shown once at
creation/rotation and never persisted. Revoking clears the fields; rotating
overwrites them so the previous key stops resolving immediately.

The display id is a fingerprint derived from the *hash* (`otl_` + first hex
chars), never from the plaintext — so nothing shown in a dashboard reveals any
character of the secret. It only identifies which key a row holds.

The key authenticates *who* the caller is — permissions are derived live from the
user record (realtime) and room ownership, never baked into the key. One
exception: admin *management* authority is withheld from key-authenticated
callers (see Identity.can_admin), so a key on a broadcast machine cannot reach
account/settings endpoints even if its owner is a site admin.
"""
import hashlib
import secrets

API_KEY_PREFIX = "otl_"
_KEY_ID_LEN = 8  # hex chars of the (non-secret) hash used as a display id


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256_hash, display_id) for a fresh key.

    `display_id` is derived from the hash, so it leaks no character of the
    plaintext secret while still uniquely tagging the key for dashboards.
    """
    plaintext = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(plaintext)
    return plaintext, key_hash, f"{API_KEY_PREFIX}{key_hash[:_KEY_ID_LEN]}"


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def looks_like_api_key(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(API_KEY_PREFIX) and len(value) > len(API_KEY_PREFIX)
