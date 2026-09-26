"""Mint a Claude setup-token inside a subfleet-owned pty and capture it.

``claude setup-token`` is interactive: it opens the browser on an OAuth page
(``…/oauth/authorize?code=true…``), the page shows a one-time code, the user
pastes the code into the CLI, and the CLI prints a long-lived token once.
Piping it (``claude setup-token | subfleet enroll``) cannot work: without a
TTY the CLI waits forever, and whatever text does flow is instructions, not
a token (2026-09-15: "token REJECTED by usage endpoint (network-error)").

``mint()`` runs the CLI under a pseudo-terminal that subfleet owns, relays
the authorization URL, answers the paste prompt with a code the caller
supplies, and pulls the token out of the output stream. The token is never
printed: the transcript returned to callers has it masked.

Automatic mode (default): ``claude setup-token`` already runs its own
localhost callback; the URL it hands to ``open`` carries
``redirect_uri=http://localhost:<port>/callback`` and the browser delivers
the code straight to the CLI on approval. The "Paste code here if prompted"
line is only its fallback. subfleet therefore never prompts in this mode: it
wraps ``open`` with a stub that records the URL (for the operator message)
and forwards to the real ``open``, then waits for the token. Rewriting
``redirect_uri`` to a subfleet listener does NOT work: the token exchange
must name the same redirect target the code was issued for, so the CLI gets
a 400 and sits at "Press Enter to retry" (observed 2026-09-15).
``paste=True`` is the explicit fallback: prompt for the hosted page's code
and type it into the CLI.
"""

from __future__ import annotations

import fcntl
import http.server
import os
import pty
import re
import select
import signal
import stat
import struct
import subprocess
import tempfile
import termios
import threading
import time
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Callable

TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{40,}")
URL_RE = re.compile(r"https://[^\s\x1b'\"<>]+/oauth/authorize[^\s\x1b'\"<>]*")
PASTE_RE = re.compile(r"paste\s*(?:the\s*)?code", re.IGNORECASE)
# The TUI drops spaces/letters when rendered through a pty; match loosely.
OAUTH_ERROR_RE = re.compile(r"OAuth\s*error|status\s*code\s*\d{3}|Press\s*Enter\s*to\s*retry|Invalid\s*code", re.IGNORECASE)
ANSI_RE = re.compile(
    r"\x1b\[[0-9;?<=>]*[A-Za-z]"          # CSI (incl. private modes like \x1b[>0q)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (BEL- or ST-terminated, e.g. OSC 8 hyperlinks)
    r"|\x1b[78=>]"                         # save/restore cursor, keypad modes
    r"|\r"
)
TOKEN_SHAPE_RE = re.compile(r"^sk-ant-oat01-[A-Za-z0-9_-]{60,160}$")
MASK = "sk-ant-oat01-…(captured, not shown)"

DEFAULT_TIMEOUT_S = 300.0
COLUMNS = 400          # wide enough that the CLI never wraps the token line


def clean(raw: bytes) -> str:
    """Strip terminal control sequences so regexes see plain text."""
    return ANSI_RE.sub("", raw.decode("utf-8", "ignore"))


TOKEN_RAW_RE = re.compile(rb"sk-ant-oat01-[A-Za-z0-9_-]{40,}")


def token_from_raw(raw: bytes) -> str | None:
    """The token as the CLI emitted it, bounded by its own escape sequences.

    The TUI places the next line ("Store this token securely.") with cursor
    moves rather than a newline; once those are stripped, the cleaned text
    reads token+"Store" and a text-side regex captures 113 characters
    (observed 2026-09-15: usage probe 401). In the raw stream the token is
    wrapped in colour codes, so an ESC ends the run exactly.
    """
    m = TOKEN_RAW_RE.search(raw)
    return m.group(0).decode("ascii") if m else None


def scan(text: str, raw: bytes | None = None) -> dict:
    """Pure: what the CLI has said so far (token / url / paste prompt).

    ``raw`` (the unstripped pty bytes), when given, is the authority for the
    token; the cleaned ``text`` only backs it up."""
    token = TOKEN_RE.search(text)
    raw_token = token_from_raw(raw) if raw else None
    url = URL_RE.search(text)
    err = OAUTH_ERROR_RE.search(text)
    err_line = None
    if err:
        line_start = text.rfind("\n", 0, err.start()) + 1
        line_end = text.find("\n", err.end())
        err_line = text[line_start:(line_end if line_end != -1 else len(text))].strip()[:160]
    return {
        "token": raw_token or (token.group(0) if token else None),
        "url": url.group(0) if url else None,
        "paste_prompt": bool(PASTE_RE.search(text)),
        "oauth_error": err_line,
    }


def _stub_open_dir(url_file: str) -> str:
    """A PATH dir whose ``open`` records the URL the CLI launches (so the
    operator message can show it) and then forwards to the real ``open``."""
    d = tempfile.mkdtemp(prefix="subfleet-mint-")
    stub = os.path.join(d, "open")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\n# subfleet enroll --mint: record the URL, then launch the browser for real\n"
                "for a in \"$@\"; do case \"$a\" in http*) printf '%s\\n' \"$a\" >> \"$SUBFLEET_MINT_URL_FILE\";; esac; done\n"
                "exec \"${SUBFLEET_MINT_REAL_OPEN:-/usr/bin/open}\" \"$@\"\n")
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    return d


def _read_captured_url(url_file: str) -> str | None:
    try:
        with open(url_file) as f:
            for line in f:
                line = line.strip()
                if URL_RE.search(line):
                    return URL_RE.search(line).group(0)
    except OSError:
        return None
    return None


def well_formed(token: str | None) -> bool:
    """Shape check before any network use: prefix, charset, plausible length."""
    return bool(token) and bool(TOKEN_SHAPE_RE.match(token))


def diagnostic(result: "MintResult", probe: dict | None = None, tail_lines: int = 12) -> dict:
    """A masked, safe-to-store record of what happened (never the token)."""
    import hashlib
    tok = result.token
    return {
        "captured": bool(tok),
        "token_len": len(tok) if tok else 0,
        "token_sha256_8": hashlib.sha256(tok.encode()).hexdigest()[:8] if tok else None,
        "well_formed": well_formed(tok),
        "url_seen": bool(result.url),
        "code_sent": result.code_sent,
        "events": list(result.events),
        "exit_code": result.exit_code,
        "error": result.error,
        "probe": {k: probe.get(k) for k in ("status", "error", "checked_at")} if probe else None,
        "transcript_tail": [ln for ln in mask(result.transcript, tok).splitlines() if ln.strip()][-tail_lines:],
    }


def write_diagnostic(path, record: dict) -> None:
    import json
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=1)


def mask(text: str, token: str | None) -> str:
    if not token:
        return text
    return text.replace(token, MASK)


@dataclass
class MintResult:
    token: str | None = None
    url: str | None = None
    exit_code: int | None = None
    error: str | None = None
    transcript: str = ""          # always masked
    code_sent: bool = False
    code_source: str | None = None   # "prompt" when --paste supplied it; None in native mode
    events: list = field(default_factory=list)


def _child_env(env: dict | None) -> dict:
    base = dict(os.environ if env is None else env)
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        base.pop(key, None)
    base.setdefault("TERM", "xterm-256color")
    base["COLUMNS"] = str(COLUMNS)
    return base


def _set_winsize(fd: int, rows: int = 50, cols: int = COLUMNS) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _reap(pid: int) -> int | None:
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return -1
    if done == 0:
        return None
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return -1


def _terminate(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(10):
            if _reap(pid) is not None:
                return
            time.sleep(0.05)


def mint(
    claude_bin: str,
    *,
    on_url: Callable[[str], None] | None = None,
    code_prompt: Callable[[], str | None] | None = None,
    paste: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: dict | None = None,
    poll_s: float = 0.25,
) -> MintResult:
    """Run ``claude setup-token`` under our pty and capture the token.

    Native mode (``paste=False``): the CLI opens the browser and receives the
    code on its own localhost callback; subfleet only records the URL
    (``on_url``) and waits for the token. ``paste=True``: when the CLI shows
    its paste prompt, ``code_prompt()`` is asked once for the hosted page's
    code (None aborts) and it is typed into the CLI. The result's
    ``transcript`` is masked; ``token`` is the only place the secret lives.
    """
    result = MintResult()
    child_env = _child_env(env)
    url_file = os.path.join(tempfile.mkdtemp(prefix="subfleet-mint-"), "url")
    child_env["SUBFLEET_MINT_URL_FILE"] = url_file
    child_env["PATH"] = _stub_open_dir(url_file) + os.pathsep + child_env.get("PATH", "")
    with warnings.catch_warnings():
        # Python warns when forkpty() sees more than one native OS thread;
        # macOS keeps resolver helper threads around and the child execs
        # immediately, so the deadlock concern does not apply.
        warnings.filterwarnings("ignore", message=".*multi-threaded.*fork.*", category=DeprecationWarning)
        pid, fd = pty.fork()
    if pid == 0:                                    # child
        try:
            os.execvpe(claude_bin, [os.path.basename(claude_bin), "setup-token"], child_env)
        except OSError as exc:                      # pragma: no cover - child side
            os.write(2, f"exec failed: {exc}\n".encode())
            os._exit(127)
    _set_winsize(fd)
    raw = b""
    started = time.monotonic()
    paused = 0.0                                    # seconds spent waiting on the human
    url_seen = False
    code_answered = False
    eof = False
    try:
        while True:
            if time.monotonic() - started - paused > timeout_s:
                waiting = "the browser approval" if url_seen else "setup-token"
                result.error = f"timed out after {int(timeout_s)}s waiting for {waiting}"
                _terminate(pid)
                break
            if not eof:
                ready, _, _ = select.select([fd], [], [], poll_s)
                if fd in ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:                 # EIO once the child exits
                        chunk = b""
                    if chunk:
                        raw += chunk
                    else:
                        eof = True
            text = clean(raw)
            seen = scan(text, raw)
            url = seen["url"] or _read_captured_url(url_file)
            if url and not url_seen:
                url_seen = True
                result.url = url
                result.events.append("url")
                if on_url:
                    on_url(url)
            if seen["token"] and not result.token:
                result.token = seen["token"]
                result.events.append("token")
            if seen["oauth_error"] and not result.token:
                result.error = f"the CLI reported an OAuth failure ({seen['oauth_error'].strip()})"
                _terminate(pid)
                break
            if paste and seen["paste_prompt"] and not code_answered and not result.token:
                code_answered = True
                prompt_started = time.monotonic()
                code = code_prompt() if code_prompt else None
                paused += time.monotonic() - prompt_started
                if not code:
                    result.error = "setup-token asked for the browser code and none was supplied"
                    _terminate(pid)
                    break
                os.write(fd, code.strip().encode())
                time.sleep(poll_s)                  # let the TUI ingest the text before Enter
                os.write(fd, b"\r")
                result.code_sent = True
                result.code_source = "prompt"
                result.events.append("code")
            rc = _reap(pid)
            if rc is not None:
                result.exit_code = rc
                # drain whatever is left after exit
                for _ in range(20):
                    ready, _, _ = select.select([fd], [], [], 0.05)
                    if fd not in ready:
                        break
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    raw += chunk
                break
            if eof and rc is None:
                time.sleep(poll_s)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if result.exit_code is None:
        result.exit_code = _reap(pid)
    text = clean(raw)
    seen = scan(text, raw)
    if seen["token"] and not result.token:
        result.token = seen["token"]
    result.transcript = mask(text, result.token)
    if not result.token and not result.error:
        result.error = "setup-token finished without printing a token (rc=%s)" % result.exit_code
    return result
