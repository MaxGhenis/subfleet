"""The Claude desktop app's update state, read from its own main.log.

Read-only, no action. The app stages an update (``Update downloaded and
ready to install``), announces it every 20 minutes (``Staged version V is
still current``), and applies it by quitting — ``beforeQuitForUpdate handler
fired`` followed by ``[CCD] Killing N PTY process tree(s) on quit`` — which
ends every app-hosted Claude Code session (2026-09-05 22:57:55: nine trees,
the ceremony session among them). The next launch logs ``Version changed
since last launch: A → V``. So a staged update is a scheduled kill of
unknown time; ``subfleet status`` says so in one line.
"""

from __future__ import annotations

import os
import plistlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import fmt_clock, iso, now_local

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[\w+\] (.*)$")
_STAGED_CURRENT = re.compile(r"\[updater\] Staged version (\S+) is still current")
_DOWNLOADED = re.compile(r"\[updater\] Update downloaded and ready to install .*releaseName: '(?:Claude )?([^']+)'")
_REPLACING = re.compile(r"\[updater\] \[evt:replacing-staged\] Newer version (\S+) available \(staged: (\S+)\)")
_INSTALLED = re.compile(
    r"\[updater\] (?:Version changed since last launch: \S+ (?:→|->) (\S+)"
    r"|Previous update install succeeded \(\S+ -> (\S+)\))"
)
_QUIT_FOR_UPDATE = "beforeQuitForUpdate handler fired"
# The heartbeat is every 20 minutes; a staging not re-announced for this long
# is no longer current (the app quit, or the log rotated).
DEFAULT_STALE_AFTER_MIN = 90.0
_TAIL_BYTES = 8 * 1024 * 1024


def main_log_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("SUBFLEET_DESKTOP_MAIN_LOG")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / "Library" / "Logs" / "Claude" / "main.log"


def app_plist_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("SUBFLEET_DESKTOP_PLIST")
    if explicit:
        return Path(explicit).expanduser()
    return Path("/Applications/Claude.app/Contents/Info.plist")


def installed_version(plist: Path | None = None) -> str | None:
    """CFBundleVersion of the installed app (the staged version once applied)."""
    path = plist or app_plist_path()
    try:
        with path.open("rb") as stream:
            data = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    version = data.get("CFBundleVersion") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def _stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return None


def _tail_lines(path: Path, max_bytes: int = _TAIL_BYTES) -> tuple[list[str], bool]:
    """Last ``max_bytes`` of the log as lines; second value: was it truncated."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            truncated = size > max_bytes
            if truncated:
                stream.seek(size - max_bytes)
            raw = stream.read()
    except OSError:
        return [], False
    lines = raw.decode("utf-8", "replace").splitlines()
    if truncated and lines:
        lines = lines[1:]  # the first line is a fragment
    return lines, truncated


def staged_update(log_path: Path | None = None, *, now: datetime | None = None,
                  installed: str | None = None, stale_after_min: float = DEFAULT_STALE_AFTER_MIN,
                  ) -> dict[str, Any] | None:
    """The update the app holds staged, or ``None``.

    {"version", "since" (first announcement of this staging in the log),
    "since_at_least" (the staging began before the scanned tail), "last_seen",
    "stale" (not re-announced within ``stale_after_min``), "quit_for_update_at"
    (the app already announced its update quit; the sessions it hosted are
    gone)}. A version the log later reports installed, or that matches the
    installed app's CFBundleVersion, is not staged.
    """
    path = log_path or main_log_path()
    now = now or now_local()
    lines, truncated = _tail_lines(path)
    if not lines:
        return None
    staged: str | None = None
    since: datetime | None = None
    since_at_least = False
    last_seen: datetime | None = None
    quit_at: datetime | None = None
    first_stamp: datetime | None = None
    for line in lines:
        match = _LINE.match(line)
        if not match:
            continue
        stamp = _stamp(match.group(1))
        if stamp is None:
            continue
        if first_stamp is None:
            first_stamp = stamp
        body = match.group(2)
        if "[updater]" not in body and _QUIT_FOR_UPDATE not in body:
            continue
        downloaded = _DOWNLOADED.search(body)
        if downloaded:
            staged, since, since_at_least, last_seen, quit_at = downloaded.group(1), stamp, False, stamp, None
            continue
        replacing = _REPLACING.search(body)
        if replacing:
            staged, since, since_at_least, last_seen, quit_at = replacing.group(1), stamp, False, stamp, None
            continue
        current = _STAGED_CURRENT.search(body)
        if current:
            version = current.group(1)
            if version != staged:
                # A staging that began before this tail (or before a rotation):
                # its first heartbeat here is the earliest "since" we can name.
                staged, since, since_at_least = version, stamp, truncated and stamp == first_stamp or staged is None
                quit_at = None
            last_seen = stamp
            continue
        installed_match = _INSTALLED.search(body)
        if installed_match:
            version = installed_match.group(1) or installed_match.group(2)
            if staged is not None and version == staged:
                staged, since, last_seen, quit_at = None, None, None, None
            continue
        if _QUIT_FOR_UPDATE in body and staged is not None:
            quit_at = stamp
    if staged is None or since is None or last_seen is None:
        return None
    current_version = installed if installed is not None else installed_version()
    if current_version and current_version == staged:
        return None
    age_min = (now - last_seen).total_seconds() / 60
    return {
        "version": staged,
        "since": iso(since),
        "since_at_least": bool(since_at_least),
        "last_seen": iso(last_seen),
        "stale": age_min > stale_after_min,
        "quit_for_update_at": iso(quit_at) if quit_at else None,
        "log": str(path),
    }


def status(*, now: datetime | None = None, log_path: Path | None = None,
           installed: str | None = None) -> dict[str, Any]:
    """Snapshot section: {"staged_update": ... | None, "installed_version", "main_log"}."""
    path = log_path or main_log_path()
    version = installed if installed is not None else installed_version()
    return {
        "staged_update": staged_update(path, now=now, installed=version),
        "installed_version": version,
        "main_log": str(path),
    }


def warning_line(section: dict[str, Any] | None, now: datetime | None = None) -> str | None:
    """The one-line ``subfleet status`` warning, or ``None`` when nothing is staged."""
    staged = (section or {}).get("staged_update") if isinstance(section, dict) else None
    if not isinstance(staged, dict) or not staged.get("version") or staged.get("stale"):
        return None
    now = now or now_local()
    since = fmt_clock(datetime.fromisoformat(staged["since"]), now) if staged.get("since") else "?"
    prefix = "at least " if staged.get("since_at_least") else ""
    suffix = ""
    if staged.get("quit_for_update_at"):
        suffix = (f"; it already quit for the update at "
                  f"{fmt_clock(datetime.fromisoformat(staged['quit_for_update_at']), now)}")
    return (
        f"  WARNING: desktop app update staged since {prefix}{since} ({staged['version']}); "
        f"it will kill every app-hosted session when applied{suffix}"
    )
