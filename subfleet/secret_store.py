"""Minimal configurable storage for per-lane Claude setup tokens."""

from __future__ import annotations

import getpass
import subprocess
from collections.abc import Callable

from . import config

Runner = Callable[..., subprocess.CompletedProcess]


def _runner(runner: Runner | None) -> Runner:
    return runner or subprocess.run


def get(name: str, *, runner: Runner | None = None) -> str | None:
    try:
        custom = config.command("secret_store_cmd")
    except config.ConfigError:
        return None
    argv = (
        [*custom, "get", name]
        if custom
        else ["security", "find-generic-password", "-s", name, "-a", getpass.getuser(), "-w"]
    )
    try:
        result = _runner(runner)(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def set(name: str, value: str, *, runner: Runner | None = None) -> bool:
    try:
        custom = config.command("secret_store_cmd")
    except config.ConfigError:
        return False
    if custom:
        argv = [*custom, "set", name]
        kwargs = {"input": value + "\n", "capture_output": True, "text": True, "timeout": 30}
    else:
        argv = [
            "security", "add-generic-password", "-U", "-a", getpass.getuser(),
            "-s", name, "-l", name, "-w", value,
        ]
        kwargs = {"capture_output": True, "text": True, "timeout": 30}
    try:
        return _runner(runner)(argv, **kwargs).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def delete(name: str, *, runner: Runner | None = None) -> bool:
    try:
        custom = config.command("secret_store_cmd")
    except config.ConfigError:
        return False
    argv = (
        [*custom, "del", name]
        if custom
        else ["security", "delete-generic-password", "-s", name, "-a", getpass.getuser()]
    )
    try:
        return _runner(runner)(argv, capture_output=True, text=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
