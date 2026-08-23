"""Optional watchdog notification hook."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

from . import config


def send(subject: str, body: str, *, dry_run: bool = False,
         runner: Callable | None = None) -> bool:
    message = f"{subject}\n{body}".rstrip()
    if dry_run:
        print(f"[dry-run] ALERT: {message}\n", file=sys.stderr)
        return True
    try:
        command = config.command("notify_cmd")
    except config.ConfigError as exc:
        print(f"subfleet: notify hook configuration error: {exc}", file=sys.stderr)
        return False
    if not command:
        print(f"ALERT: {message}\n", file=sys.stderr)
        return True
    try:
        result = (runner or subprocess.run)(
            [*command, subject, body],
            input=message + "\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"subfleet: notify hook error: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"subfleet: notify hook failed rc={result.returncode}: {result.stderr[:200]}",
            file=sys.stderr,
        )
    return result.returncode == 0
