"""Stage and watch an interactive Codex lane login.

``carpool login codex N`` starts the vendor login server in the requested
numbered home.  ``app`` targets the separately observed desktop-app home.  A
detached watcher verifies that numbered lanes remain distinct after the user
finishes the browser step; the app account is reported as a shadow instead of
as a duplicate lane.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from . import config, notify, paths


AUTH_URL_RE = re.compile(r"https://auth[^\s\x1b\"'<>]+", re.IGNORECASE)
LOGIN_COMPLETE_RE = re.compile(r"success|logged\s+in", re.IGNORECASE)
DEFAULT_LOGIN_TIMEOUT_SECONDS = 20.0
DEFAULT_WATCH_TIMEOUT_SECONDS = 24 * 60 * 60.0
DEFAULT_WATCH_POLL_SECONDS = 10.0


def _target_home(target: str) -> tuple[Path, str]:
    """Resolve a public login target without treating the app as a lane."""
    if target == "app":
        return paths.app_codex_home(), "app"
    if target.isdigit() and 1 <= int(target) <= 9:
        slot = str(int(target))
        expected_name = f".codex-{slot}"
        configured_homes = paths.codex_homes()
        for configured_home in configured_homes:
            candidate = Path(configured_home)
            if candidate.name == expected_name:
                return candidate, slot
        claimed_slots = {
            match.group(1)
            for home in configured_homes
            if (match := re.fullmatch(r"\.codex-([1-9])", Path(home).name))
        }
        custom_homes = [
            Path(home)
            for home in configured_homes
            if not re.fullmatch(r"\.codex-([1-9])", Path(home).name)
        ]
        unclaimed_slots = [str(index) for index in range(1, 10) if str(index) not in claimed_slots]
        custom_by_slot = dict(zip(unclaimed_slots, custom_homes))
        if slot in custom_by_slot:
            return custom_by_slot[slot], slot
        return Path.home() / f".codex-{slot}", slot
    raise SystemExit(
        "carpool login: target must be a lane number 1-9 or "
        f"'app', got {target!r}"
    )


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _logs_dir() -> Path:
    return _secure_dir(_secure_dir(paths.state_dir()) / "logs")


def _state_file(slot: str, kind: str) -> Path:
    return _secure_dir(paths.state_dir()) / f"codex-login-{kind}{slot}.pid"


def _pidfile(slot: str) -> Path:
    return _state_file(slot, "")


def _watch_pidfile(slot: str) -> Path:
    return _state_file(slot, "watch-")


def _logfile(slot: str) -> Path:
    return _logs_dir() / f"codex-login-{slot}.log"


def _watch_logfile(slot: str) -> Path:
    return _logs_dir() / f"codex-login-watch-{slot}.log"


def _secure_truncate(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _secure_write(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            stream.write(value)
    finally:
        if fd >= 0:
            os.close(fd)


def _command_value(value: object, *, label: str) -> list[str] | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        try:
            return shlex.split(value) or None
        except ValueError as exc:
            raise config.ConfigError(f"invalid {label}: {exc}") from exc
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    raise config.ConfigError(f"{label} must be a command string, an argv list, or null")


def _configured_command(
    env_names: Sequence[str], config_keys: Sequence[str]
) -> list[str] | None:
    """Read an argv command without asking a shell to interpret it."""
    for name in env_names:
        if name in os.environ:
            return _command_value(os.environ[name], label=name)
    document = config.load(strict=True)
    for key in config_keys:
        if key in document:
            return _command_value(document[key], label=key)
    return None


def _with_url(command: Sequence[str], url: str) -> list[str]:
    if any("{url}" in item for item in command):
        return [item.replace("{url}", url) for item in command]
    return [*command, url]


def _open_authorize_url(
    url: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    opener: Callable[..., object] | None = None,
) -> bool:
    """Open an authorize URL with a configured command or the system default."""
    try:
        command = _configured_command(
            ("CARPOOL_LOGIN_BROWSER_CMD", "CARPOOL_BROWSER_CMD"),
            ("login_browser_cmd", "browser_cmd"),
        )
    except config.ConfigError as exc:
        print(f"carpool login: browser configuration error: {exc}", file=sys.stderr)
        return False
    if command:
        try:
            result = (runner or subprocess.run)(
                _with_url(command, url),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            print(f"carpool login: could not start configured browser: {exc}", file=sys.stderr)
            return False
        if result.returncode != 0:
            print(
                "carpool login: configured browser exited with "
                f"status {result.returncode}",
                file=sys.stderr,
            )
            return False
        return True
    try:
        return bool((opener or webbrowser.open)(url, new=2))
    except (OSError, webbrowser.Error) as exc:
        print(f"carpool login: could not open the default browser: {exc}", file=sys.stderr)
        return False


def _extract_authorize_url(text: str) -> str | None:
    match = AUTH_URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def _wait_for_authorize_url(
    log: Path,
    process: subprocess.Popen,
    timeout_s: float,
) -> str | None:
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        try:
            url = _extract_authorize_url(log.read_text(errors="ignore"))
        except OSError:
            url = None
        if url:
            return url
        if process.poll() is not None or time.monotonic() >= deadline:
            return None
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0.0)))


def _watcher_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "carpool-login-watch"


def _destination_label(home: Path, slot: str) -> str:
    if slot == "app":
        default = Path.home() / ".codex"
        return "desktop app home ~/.codex" if _same_path(home, default) else f"desktop app home {home}"
    default = Path.home() / f".codex-{slot}"
    return f"lane ~/.codex-{slot}" if _same_path(home, default) else f"lane {home}"


def codex_login(
    target: str,
    watch: bool = True,
    open_browser: bool = True,
    codex_bin: str | None = None,
    timeout_s: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> int:
    """Start ``codex login`` detached, open its URL, and arm the watcher."""
    from .codex import _codex_binary

    home, slot = _target_home(target)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = _logfile(slot)
    pidfile = _pidfile(slot)
    _secure_truncate(log)
    env = {**os.environ, "CODEX_HOME": str(home)}
    binary = codex_bin or _codex_binary()
    try:
        with log.open("ab") as stream:
            process = subprocess.Popen(
                [binary, "login"],
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
    except OSError as exc:
        print(f"carpool login: could not start {binary!r}: {exc}", file=sys.stderr)
        return 1
    _secure_write(pidfile, f"{process.pid}\n")

    url = _wait_for_authorize_url(log, process, timeout_s)
    if not url:
        print(
            "carpool login: no authorize URL from `codex login` within "
            f"{timeout_s:.0f}s (another login may already own the callback port) "
            f"— log: {log}",
            file=sys.stderr,
        )
        return 1

    destination = _destination_label(home, slot)
    print(f"carpool login: server pid={process.pid} waiting for {destination}")
    if open_browser:
        if _open_authorize_url(url):
            print(
                "carpool login: OAuth URL opened in your browser — select the "
                "account for this target, then authorize."
            )
        else:
            print(f"carpool login: open this OAuth URL manually:\n{url}")
    else:
        print(url)

    if not watch:
        return 0
    watcher = _watcher_path()
    if not watcher.is_file() or not os.access(watcher, os.X_OK):
        print(f"carpool login: watcher is missing or not executable: {watcher}", file=sys.stderr)
        return 1
    watch_log = _watch_logfile(slot)
    _secure_truncate(watch_log)
    try:
        with watch_log.open("ab") as stream:
            watch_process = subprocess.Popen(
                [str(watcher), slot],
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=os.environ.copy(),
                start_new_session=True,
            )
    except OSError as exc:
        print(f"carpool login: could not start completion watcher: {exc}", file=sys.stderr)
        return 1
    _secure_write(_watch_pidfile(slot), f"{watch_process.pid}\n")
    print(
        "carpool login: watcher armed (distinctness check and completion "
        f"notification; log {watch_log})"
    )
    return 0


def _read_account_id(home: Path) -> str | None:
    try:
        value = json.loads((home / "auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tokens = value.get("tokens") if isinstance(value, dict) else None
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    return account_id.strip() if isinstance(account_id, str) and account_id.strip() else None


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def numbered_lane_homes() -> list[Path]:
    """Return configured dispatch lanes, defensively excluding the app home."""
    app_home = paths.app_codex_home()
    result: list[Path] = []
    for value in paths.codex_homes():
        home = Path(value)
        if not _same_path(home, app_home) and home not in result:
            result.append(home)
    return result


def duplicate_lane_report(homes: Iterable[Path] | None = None) -> list[list[Path]]:
    """Group numbered lanes that currently contain the same account."""
    grouped: dict[str, list[Path]] = {}
    for home in numbered_lane_homes() if homes is None else homes:
        account_id = _read_account_id(Path(home))
        if account_id:
            grouped.setdefault(account_id, []).append(Path(home))
    return [group for group in grouped.values() if len(group) > 1]


def app_shadow_report(homes: Iterable[Path] | None = None) -> list[Path]:
    """Return numbered lanes bound to the desktop app's current account."""
    app_id = _read_account_id(paths.app_codex_home())
    if not app_id:
        return []
    candidates = numbered_lane_homes() if homes is None else homes
    return [Path(home) for home in candidates if _read_account_id(Path(home)) == app_id]


def _lane_label(home: Path) -> str:
    match = re.fullmatch(r"\.codex-([1-9])", home.name)
    return f"lane {match.group(1)}" if match else f"lane {home.name}"


def _target_label(slot: str) -> str:
    return "Codex app" if slot == "app" else f"Codex lane {slot}"


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _refresh_command() -> list[str]:
    configured = _configured_command(
        ("CARPOOL_LOGIN_REFRESH_CMD", "CARPOOL_WATCH_CMD"),
        ("login_refresh_cmd", "watch_cmd"),
    )
    if configured:
        return configured
    sibling = Path(__file__).resolve().parent.parent / "bin" / "carpool"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling), "watch"]
    installed = shutil.which("carpool")
    return [installed or "carpool", "watch"]


def _refresh_snapshot(
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[bool, str]:
    try:
        command = _refresh_command()
        result = (runner or subprocess.run)(
            command,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=300,
        )
    except config.ConfigError as exc:
        return False, f"refresh command configuration error: {exc}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"refresh command failed: {exc}"
    if result.returncode != 0:
        return False, f"refresh command exited with status {result.returncode}"
    return True, "carpool watch refresh completed"


def _read_login_log(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _completion_notification(slot: str) -> int:
    duplicates = duplicate_lane_report()
    if duplicates:
        pairs = "; ".join(
            " + ".join(_lane_label(home) for home in group) for group in duplicates
        )
        notify.send(
            f"{_target_label(slot)} re-login bound a duplicate account",
            "Numbered Codex lanes must use distinct accounts. Duplicate binding: "
            f"{pairs}. Repeat this login with a different account.",
        )
        return 0

    shadows = app_shadow_report()
    if shadows:
        shadow_note = (
            " The desktop app currently shares its account with "
            + ", ".join(_lane_label(home) for home in shadows)
            + "; those lanes are shadowed and should not be dispatched normally."
        )
    else:
        shadow_note = " The desktop app does not shadow a configured lane."
    refreshed, refresh_note = _refresh_snapshot()
    notify.send(
        f"{_target_label(slot)} re-login complete",
        "All numbered Codex lanes hold distinct accounts."
        f"{shadow_note} {refresh_note}. Check the table with `carpool status`.",
    )
    return 0


def watch_codex_login(
    target: str,
    *,
    timeout_s: float = DEFAULT_WATCH_TIMEOUT_SECONDS,
    poll_s: float = DEFAULT_WATCH_POLL_SECONDS,
) -> int:
    """Wait for a detached login and report exactly one terminal outcome."""
    home, slot = _target_home(target)
    log = _logfile(slot)
    pidfile = _pidfile(slot)
    _secure_write(_watch_pidfile(slot), f"{os.getpid()}\n")
    if log.exists():
        log.chmod(0o600)
    if pidfile.exists():
        pidfile.chmod(0o600)
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        text = _read_login_log(log)
        complete = bool(LOGIN_COMPLETE_RE.search(text))
        if complete and (home / "auth.json").is_file():
            return _completion_notification(slot)

        pid = _read_pid(pidfile)
        if pid is not None and not _pid_alive(pid) and not complete:
            notify.send(
                f"{_target_label(slot)} login closed unfinished",
                "The waiting login server exited before sign-in completed. "
                f"Run `carpool login codex {slot}` to try again.",
            )
            return 1
        if time.monotonic() >= deadline:
            notify.send(
                f"{_target_label(slot)} login still pending after timeout",
                "The browser authorization was not completed in time. "
                f"Run `carpool login codex {slot}` when ready.",
            )
            return 1
        time.sleep(max(poll_s, 0.01))


def watcher_main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: carpool login codex <N|app>", file=sys.stderr)
        return 2
    try:
        return watch_codex_login(args[0])
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(watcher_main())
