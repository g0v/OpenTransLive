"""Application configuration, loaded from secret/config.toml.

Copy secret/config.example.toml to secret/config.toml and fill in your values::

    cp app/secret/config.example.toml app/secret/config.toml

This module only parses config.toml into the module-level settings the rest of
the app imports (SETTINGS, EMAIL_SETTINGS, MONGODB_SETTINGS, REALTIME_SETTINGS,
REDIS_URL), so those imports keep working unchanged.

Any value can be overridden by an environment variable of the same name (e.g.
OPENAI_API_KEY, SMTP_HOST, REDIS_URL); the env value is coerced to match the
type of the value in config.toml.
"""
import os
import tomllib
from pathlib import Path

_SECRET_DIR = Path(__file__).with_name("secret")


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
