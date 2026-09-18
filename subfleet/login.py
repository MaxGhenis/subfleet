"""Stage a codex lane (re)login — the ritual the codex-accounts skill
documents, as one command. Max still does the only thing only he can do:
pick the account on the OAuth page and click Authorize.

    subfleet login codex 3      # lane ~/.codex-3
    subfleet login codex app    # the desktop app's home ~/.codex

Steps: ensure the home dir exists, start `codex login` detached (its server
waits on port 1455 — ONE login at a time), extract the authorize URL from
its log, open it in Chrome, and arm subfleet-login-watch (verifies the new
binding is distinct across lanes, reports an app shadow, notifies Max,
kicks the watchdog snapshot). Never types credentials, never clicks.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import paths

AUTH_URL_RE = re.compile(r"https://auth[^\s]+")


def _target_home(target: str) -> tuple[Path, str]:
    if target == "app":
        return paths.app_codex_home(), "app"
    if target.isdigit() and 1 <= int(target) <= 9:
        return Path.home() / f".codex-{target}", target
    raise SystemExit(f"subfleet login: target must be a lane number 1-9 or 'app', got {target!r}")


def _logs_dir() -> Path:
    d = Path.home() / "chief-of-staff" / "state" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def codex_login(target: str, watch: bool = True, open_browser: bool = True,
                codex_bin: str | None = None, timeout_s: float = 20.0) -> int:
    from .codex import _codex_binary

    home, slot = _target_home(target)
    home.mkdir(parents=True, exist_ok=True)
    logs = _logs_dir()
    log = logs / f"codex-login-{slot}.log"
    pidfile = Path.home() / "chief-of-staff" / "state" / f"codex-login-{slot}.pid"
    log.write_text("")
    env = {**os.environ, "CODEX_HOME": str(home)}
    binary = codex_bin or _codex_binary()
    with open(log, "ab") as lf:
        proc = subprocess.Popen([binary, "login"], stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    pidfile.write_text(f"{proc.pid}\n")
    url = None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = AUTH_URL_RE.search(log.read_text(errors="ignore"))
        if m:
            url = m.group(0)
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if not url:
        print(f"subfleet login: no authorize URL from `codex login` within {timeout_s:.0f}s "
              f"(is port 1455 busy with another login? one at a time) — log: {log}",
              file=sys.stderr)
        return 1
    who = "the desktop app home ~/.codex" if slot == "app" else f"lane ~/.codex-{slot}"
    print(f"subfleet login: server pid={proc.pid} waiting for {who}")
    if open_browser:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
        print("subfleet login: OAuth tab opened in Chrome — pick the account for this "
              "lane (use 'Use another account' if it offers to continue as one already bound), "
              "then Authorize.")
    else:
        print(url)
    if watch:
        watcher = Path(__file__).resolve().parent.parent / "bin" / "subfleet-login-watch"
        wlog = logs / f"codex-login-watch-{slot}.log"
        with open(wlog, "ab") as wf:
            subprocess.Popen([str(watcher), slot], stdout=wf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        print(f"subfleet login: watcher armed (distinctness check + notify on completion; log {wlog})")
    return 0
