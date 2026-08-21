# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
"""Application configuration, loaded from secret/config.toml.

Copy secret/config.example.toml to secret/config.toml and fill in your values::

    cp app/secret/config.example.toml app/secret/config.toml

This module only parses config.toml into the module-level settings the rest of
the app imports (SETTINGS, EMAIL_SETTINGS, MONGODB_SETTINGS, REALTIME_SETTINGS,
REDIS_URL, IS_PRODUCTION), so those imports keep working unchanged.

Any value can be overridden by an environment variable of the same name (e.g.
OPENAI_API_KEY, SMTP_HOST, REDIS_URL); the env value is coerced to match the
type of the value in config.toml.
"""
import os
import tomllib
from pathlib import Path

_SECRET_DIR = Path(__file__).with_name("secret")

# Deployment mode, shared by every module that needs to behave differently in
# production (Secure cookies, strict Socket.IO CORS, no dev-only debug logging).
# Defined once here so the polarity can't drift between call sites.
IS_PRODUCTION = os.environ.get("ENVIRONMENT", "development").strip().lower() == "production"

SOURCE_CODE_URL = os.environ.get("SOURCE_CODE_URL", "").strip()
if IS_PRODUCTION and not SOURCE_CODE_URL:
    raise RuntimeError(
        "SOURCE_CODE_URL must point to the corresponding source of the deployed version."
    )
if not SOURCE_CODE_URL:
    SOURCE_CODE_URL = "https://github.com/g0v/OpenTransLive"

# Optional link to a third-party offering commercial support for this deployment
# (managed hosting, customization, on-site event support). Left empty by default:
# OpenTransLive is an independent open-source project, so a deployment only
# advertises a vendor when its own operator opts in.
PROFESSIONAL_SERVICES_URL = os.environ.get("PROFESSIONAL_SERVICES_URL", "").strip()


def load_secret_toml(name: str, *, example_fallback: bool = False) -> dict:
    """Parse ``secret/<name>.toml`` into a dict.

    When ``example_fallback`` is set and the file is absent, fall back to the
    committed ``secret/<name>.example.toml`` (for files that ship working
    defaults); otherwise a missing file is a hard error prompting the copy.
    """
    path = _SECRET_DIR / f"{name}.toml"
    if example_fallback and not path.exists():
        path = _SECRET_DIR / f"{name}.example.toml"
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"{path} not found. Copy secret/{name}.example.toml to "
            f"secret/{name}.toml and fill in your values."
        )


_CONFIG = load_secret_toml("config")


def _coerce(sample, raw: str):
    """Coerce an env string to match the type of the config.toml value."""
    if isinstance(sample, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(sample, int):
        return int(raw)
    if isinstance(sample, float):
        return float(raw)
    if isinstance(sample, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def _apply_env(key: str, value):
    """Override a value from the environment, keeping the original type."""
    raw = os.environ.get(key)
    return _coerce(value, raw) if raw is not None else value


for _section in _CONFIG.values():
    if isinstance(_section, dict):
        for _key, _value in _section.items():
            _section[_key] = _apply_env(_key, _value)
    # top-level scalars (e.g. redis_url) handled below

SETTINGS: dict = _CONFIG.get("settings", {})
EMAIL_SETTINGS: dict = _CONFIG.get("email_settings", {})
MONGODB_SETTINGS: dict = _CONFIG.get("mongodb_settings", {})
REALTIME_SETTINGS: dict = _CONFIG.get("realtime_settings", {})
REDIS_URL: str = str(_apply_env("REDIS_URL", _CONFIG.get("redis_url", "redis://redis:6379")))
