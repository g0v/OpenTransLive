# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

"""Strict schema validation for Socket.IO event payloads.

Validators return a (ok, error_message) tuple so callers can decide how to
surface failures (emit to the client, log, drop silently). They reject
malformed payloads before they reach core handlers so a malicious client
cannot trigger exceptions, exhaust memory, or poison Redis/Mongo state with
unexpected fields.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Size caps. Picked to comfortably fit real segments while bounding memory
# for degenerate clients. Corrected/translated text and `text` share the same
# cap (matches the existing editor endpoint cap).
_MAX_TEXT_LEN = 5000
_MAX_LANG_CODE_LEN = 32
_MAX_TRANSLATED_LANGS = 32
_MAX_SPECIAL_KEYWORDS = 32
_MAX_KEYWORD_LEN = 128
_MAX_SECRET_KEY_LEN = 256
MAX_AUDIO_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB; base64 inflates real audio by ~33%.
# 10^12 covers timestamps up to year ~33658 — big enough that we'll never
# clamp a real value, small enough to reject garbage like 1e308.
_MAX_TIMESTAMP = 10**12

_BASE64_RE = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')


def _is_finite_number(value: Any) -> bool:
    # isinstance(True, int) is True in Python, so guard booleans explicitly.
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _check_timestamp(value: Any, name: str) -> tuple[bool, str]:
    if not _is_finite_number(value):
        return False, f"{name} must be a finite number"
    if value < 0 or value > _MAX_TIMESTAMP:
        return False, f"{name} out of range"
    return True, ""


def _check_safe_key(value: Any, name: str, max_len: int) -> tuple[bool, str]:
    """Non-empty string, length-bounded, no Mongo metachars (`$`, `.`)."""
    if not isinstance(value, str):
        return False, f"{name} must be a string"
    if not value.strip():
        return False, f"{name} must not be empty"
    if len(value) > max_len or '$' in value or '.' in value:
        return False, f"invalid {name}: {value[:32]!r}"
    return True, ""


def _check_optional_bool(data: dict, name: str) -> tuple[bool, str]:
    if name in data and not isinstance(data[name], bool):
        return False, f"{name} must be a boolean"
    return True, ""


def _check_optional_string(data: dict, name: str, max_len: int) -> tuple[bool, str]:
    if name not in data or data[name] is None:
        return True, ""
    value = data[name]
    if not isinstance(value, str) or len(value) > max_len:
        return False, f"{name} must be a string <= {max_len} chars"
    return True, ""


def validate_base64_audio(value: Any) -> tuple[bool, str]:
    """Cheap structural check for a base64-encoded audio chunk.

    Hot path: this runs on every audio chunk (up to 30 Hz/socket, ≤1MB).
    We only sniff the first 16 chars against the base64 alphabet; scanning
    the full string would add O(N) CPU per chunk. The real decoder in
    push_audio catches anything that slips past the prefix check.
    """
    if not isinstance(value, str):
        return False, "audio must be a base64 string"
    if not value:
        return False, "audio must not be empty"
    if len(value) > MAX_AUDIO_CHUNK_SIZE:
        return False, f"audio exceeds {MAX_AUDIO_CHUNK_SIZE} bytes"
    if len(value) % 4 != 0:
        return False, "invalid base64 length"
    if not _BASE64_RE.match(value[:16]):
        return False, "invalid base64 characters"
    return True, ""


def _validate_translated(translated: Any) -> tuple[bool, str]:
    if not isinstance(translated, dict):
        return False, "result.translated must be an object"
    if len(translated) > _MAX_TRANSLATED_LANGS:
        return False, f"result.translated exceeds {_MAX_TRANSLATED_LANGS} languages"
    for k, v in translated.items():
        ok, err = _check_safe_key(k, "language code", _MAX_LANG_CODE_LEN)
        if not ok:
            return False, f"result.translated: {err}"
        if not isinstance(v, str) or len(v) > _MAX_TEXT_LEN:
            return False, f"result.translated[{k}] must be a string <= {_MAX_TEXT_LEN} chars"
    return True, ""


def _validate_special_keywords(special: Any) -> tuple[bool, str]:
    if not isinstance(special, list):
        return False, "result.special_keywords must be a list"
    if len(special) > _MAX_SPECIAL_KEYWORDS:
        return False, f"result.special_keywords exceeds {_MAX_SPECIAL_KEYWORDS} items"
    for kw in special:
        ok, err = _check_safe_key(kw, "special keyword", _MAX_KEYWORD_LEN)
        if not ok:
            return False, f"result.special_keywords: {err}"
    return True, ""


def _validate_sync_result(result: Any) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, "result must be an object"

    ok, err = _check_optional_string(result, "corrected", _MAX_TEXT_LEN)
    if not ok:
        return False, f"result.{err}"

    translated = result.get("translated")
    if translated is not None:
        ok, err = _validate_translated(translated)
        if not ok:
            return False, err

    special = result.get("special_keywords")
    if special is not None:
        ok, err = _validate_special_keywords(special)
        if not ok:
            return False, err

    return True, ""


def validate_sync_payload(data: Any) -> tuple[bool, str]:
    """Strict schema for the `sync` Socket.IO event.

    Required:
      - id: non-empty string (session id; further validated downstream).
      - start_time: finite, non-negative number (used as a ZSET score).
      - partial: bool

    Optional:
      - flow_only: bool
      - text, secret_key: str
      - end_time, init_time: finite non-negative number
      - result: {
            corrected?: str,
            translated?: { lang: str, ... } (lang ≤ 32 chars, no $/.),
            special_keywords?: list[str],
        }
    """
    if not isinstance(data, dict):
        return False, "payload must be an object"

    sid = data.get("id")
    if not isinstance(sid, str) or not sid.strip():
        return False, "id must be a non-empty string"

    ok, err = _check_timestamp(data.get("start_time"), "start_time")
    if not ok:
        return False, err

    if "partial" not in data:
        return False, "partial is required"

    for bool_field in ("partial", "flow_only"):
        ok, err = _check_optional_bool(data, bool_field)
        if not ok:
            return False, err

    if data.get("flow_only") is True:
        if data.get("partial") is not True:
            return False, "flow_only requires partial=true"
        result = data.get("result")
        if not isinstance(result, dict):
            return False, "flow_only requires result object"
        if not isinstance(result.get("translated"), dict):
            return False, "flow_only requires result.translated object"

    for ts_field in ("end_time", "init_time"):
        if ts_field in data and data[ts_field] is not None:
            ok, err = _check_timestamp(data[ts_field], ts_field)
            if not ok:
                return False, err

    ok, err = _check_optional_string(data, "text", _MAX_TEXT_LEN)
    if not ok:
        return False, err
    ok, err = _check_optional_string(data, "secret_key", _MAX_SECRET_KEY_LEN)
    if not ok:
        return False, err

    result = data.get("result")
    if result is not None:
        ok, err = _validate_sync_result(result)
        if not ok:
            return False, err

    return True, ""


def validate_audio_buffer_append_payload(data: Any) -> tuple[bool, str]:
    """Strict schema for the `audio_buffer_append` Socket.IO event.

    `secret_key` is intentionally not checked here — it's only consulted on
    the rare unauthorized branch (first chunk / re-auth), and `audio_buffer_append`
    runs hot (30 Hz/socket). The auth path runs `validate_query_param` anyway.
    """
    if not isinstance(data, dict):
        return False, "payload must be an object"

    return validate_base64_audio(data.get("audio"))
