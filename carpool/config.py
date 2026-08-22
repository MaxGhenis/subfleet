"""Shared configuration and state locations for carpool.

The public configuration is a single ``accounts.json`` file.  Its required
roster keys (``accounts`` and ``enrolled``) coexist with optional toolkit
settings so every entry point reads the same source of truth.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from .util import atomic_write_json

DEFAULT_SECRET_NAME_PREFIX = "claude-quota-"


class ConfigError(ValueError):
    """Raised when a configuration file cannot safely be updated."""


def config_dir() -> Path:
    value = os.environ.get("CARPOOL_CONFIG_DIR")
    return Path(value).expanduser() if value else Path.home() / ".config" / "carpool"


def accounts_path() -> Path:
    return config_dir() / "accounts.json"


def load(*, strict: bool = False) -> dict[str, Any]:
    """Load ``accounts.json``.

    Read-only callers fail closed to an empty roster when the file is absent or
    malformed.  Mutating callers use ``strict=True`` so malformed user data is
    never silently overwritten.
    """
    path = accounts_path()
    try:
        with path.open() as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ConfigError(f"{path} must contain a JSON object")
        return {}
    return value


def save(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ConfigError("configuration must be a JSON object")
    atomic_write_json(accounts_path(), value)


def state_dir() -> Path:
    override = os.environ.get("CARPOOL_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return root / "carpool"


def codex_homes_setting() -> list[Path] | None:
    """Return an explicit home list, or ``None`` for automatic discovery.

    Presence of ``CARPOOL_CODEX_HOMES`` is significant: an empty value is an
    explicit empty list and prevents accidental inspection of real homes in
    isolated environments.
    """
    if "CARPOOL_CODEX_HOMES" in os.environ:
        raw = os.environ["CARPOOL_CODEX_HOMES"]
        return [Path(item).expanduser() for item in raw.split(os.pathsep) if item]
    try:
        document = load(strict=True)
    except ConfigError:
        return []
    marker = object()
    value = document.get("codex_homes", marker)
    if value is marker or value is None:
        return None
    if not isinstance(value, list):
        return []
    return [Path(item).expanduser() for item in value if isinstance(item, str) and item]


def secret_name_prefix() -> str:
    if "CARPOOL_SECRET_NAME_PREFIX" in os.environ:
        return os.environ["CARPOOL_SECRET_NAME_PREFIX"]
    value = load().get("secret_name_prefix", DEFAULT_SECRET_NAME_PREFIX)
    return value if isinstance(value, str) else DEFAULT_SECRET_NAME_PREFIX


def secret_name_for(email: str, *, require_enrolled: bool = False) -> str | None:
    enrolled = load().get("enrolled") or {}
    name = enrolled.get(email) if isinstance(enrolled, dict) else None
    if isinstance(name, str) and name:
        return name
    if require_enrolled:
        return None
    return f"{secret_name_prefix()}{email}"


_COMMAND_ENV = {
    "notify_cmd": "CARPOOL_NOTIFY_CMD",
    "secret_store_cmd": "CARPOOL_SECRET_STORE_CMD",
    "mirror_restart_cmd": "CARPOOL_MIRROR_RESTART_CMD",
}


def path(key: str, env_name: str, default: Path) -> Path:
    """Return a path override from the environment or ``accounts.json``."""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = load().get(key)
    return Path(raw).expanduser() if isinstance(raw, str) and raw else default


def mirror_job_label() -> str | None:
    """Optional scheduler job identifier used only as an extra health signal."""
    raw = os.environ.get("CARPOOL_MIRROR_JOB_LABEL")
    if raw is None:
        raw = load().get("mirror_job_label")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def command(key: str) -> list[str] | None:
    """Return a configured command as argv, never as a shell expression."""
    env_name = _COMMAND_ENV.get(key)
    if env_name and env_name in os.environ:
        value: Any = os.environ[env_name]
    else:
        value = load(strict=True).get(key)
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        try:
            return shlex.split(value) or None
        except ValueError as exc:
            raise ConfigError(f"invalid {key}: {exc}") from exc
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    raise ConfigError(f"{key} must be a command string, an argv list, or null")
