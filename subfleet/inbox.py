"""Completion notices back to the Claude Code session that dispatched a run.

A detached ``subfleet run`` outlives the session that launched it — that is
the point of detaching. The cost was that nobody told the session when the
run finished; it had to poll. This module closes that gap with the harness's
own cross-session inbox:

* every interactive Claude Code session registers
  ``<claude_dir>/sessions/<pid>.json`` (``sessionId``, ``messagingSocketPath``,
  ``name``) and publishes its inbox auth key beside it
  (``<pid>.<hash>.key`` → ``peerToken``);
* the inbox speaks newline-delimited JSON on a unix socket:
  ``{"type":"auth","token":…}`` then
  ``{"type":"user","message":{"role":"user","content":…}}``;
* a body wrapped as exactly one ``<cross-session-message …>`` envelope is
  parsed by the recipient; ``from-mode`` declares the sender's permission
  class (``bypass`` / ``prompting``). A same-class message is delivered at
  once (it wakes an idle session into a turn); an undeclared or cross-class
  message is held for the user at a recipient that runs without permission
  prompts.

We resolve the recipient by SESSION ID at notification time, never by the pid
or socket captured at dispatch: a restarted session comes back under a new
pid and a new socket path, but the session id is stable. When no live inbox
exists the notice is parked in ``<state_dir>/notices/<session>.jsonl`` and
the SessionStart / UserPromptSubmit hook (``bin/subfleet-hook``) surfaces it
the next time that session is up.

Permission-mode attestation: subfleet is not a session, so there is no "own"
mode to attest. A completion notice carries no instructions beyond "your run
finished, here is the file", so we declare the RECIPIENT's current class
(read from its transcript) — the same treatment the harness gives the
session's own background-task completions. Set ``SUBFLEET_NOTIFY_MODE`` to
``bypass``/``prompting``/``none`` to override.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .util import iso, load_json, now_local, parse_iso

FROM_NAME = "subfleet"
MODE_CLASSES = ("bypass", "prompting")
_TRANSCRIPT_TAIL = 1024 * 1024
_TRANSCRIPT_MAX = 64 * 1024 * 1024
_MODE_RE = re.compile(rb'"permissionMode"\s*:\s*"([A-Za-z]+)"')
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# Who is asking: the Claude Code session around the current process.
# --------------------------------------------------------------------------

def in_claude_session(env: dict[str, str] | None = None) -> bool:
    """True inside a Claude Code session's tool shell (or anything it spawned).

    The harness exports ``CLAUDECODE=1`` and ``CLAUDE_CODE_SESSION_ID`` to
    every Bash tool process; both are inherited by detached children.
    """
    env = os.environ if env is None else env
    return bool((env.get("CLAUDECODE") or "").strip()) or bool(
        (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    )


def _int(value: str | None) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def caller_context(env: dict[str, str] | None = None, *,
                   cwd: str | None = None) -> dict[str, Any] | None:
    """The dispatching session, captured from the environment at dispatch time.

    ``session_id`` is the durable key; everything else is advisory (the pid and
    socket are stale after a restart). ``mode_class`` records what the session's
    transcript said at dispatch; it is re-read at notification time.
    """
    env = os.environ if env is None else env
    session_id = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not session_id:
        return None
    return {
        "session_id": session_id,
        "host_session_id": env.get("CLAUDE_CODE_HOST_SESSION_ID") or None,
        "pid": _int(env.get("CLAUDE_PID")),
        "cwd": cwd or os.getcwd(),
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT") or None,
        "socket": env.get("CLAUDE_CODE_MESSAGING_SOCKET") or None,
        "mode_class": session_mode_class(session_id),
        "captured_at": iso(now_local()),
    }


# --------------------------------------------------------------------------
# Session registry (<claude_dir>/sessions) and transcripts (<claude_dir>/projects)
# --------------------------------------------------------------------------

def sessions_dir() -> Path:
    return paths.claude_dir() / "sessions"


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_socket(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_socket()
    except OSError:
        return False


def find_session(session_id: str) -> dict[str, Any] | None:
    """The registry row for ``session_id`` — the LIVE one when several exist.

    Registry files are keyed by pid, so a restarted session leaves an old row
    behind for a while; rank live pid, then present socket, then newest start.
    """
    if not session_id:
        return None
    best: tuple[tuple, dict[str, Any]] | None = None
    try:
        entries = list(sessions_dir().glob("*.json"))
    except OSError:
        return None
    for entry in entries:
        data = load_json(entry)
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        pid = data.get("pid") if isinstance(data.get("pid"), int) else None
        sock = data.get("messagingSocketPath")
        started = data.get("startedAt")
        candidate = {
            "session_id": session_id,
            "pid": pid,
            "socket": sock if isinstance(sock, str) else None,
            "name": data.get("name") if isinstance(data.get("name"), str) else None,
            "cwd": data.get("cwd") if isinstance(data.get("cwd"), str) else None,
            "started_at": started if isinstance(started, (int, float)) else None,
            "alive": _pid_alive(pid),
            "socket_present": _is_socket(sock if isinstance(sock, str) else None),
            "registry_path": str(entry),
        }
        key = (candidate["alive"], candidate["socket_present"], candidate["started_at"] or 0)
        if best is None or key > best[0]:
            best = (key, candidate)
    return best[1] if best else None


def live_sessions() -> list[dict[str, Any]]:
    """Every registry row whose process is alive (for `subfleet sessions`)."""
    rows = []
    try:
        entries = list(sessions_dir().glob("*.json"))
    except OSError:
        return rows
    for entry in entries:
        data = load_json(entry)
        if not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
            continue
        pid = data.get("pid") if isinstance(data.get("pid"), int) else None
        if not _pid_alive(pid):
            continue
        rows.append(
            {
                "session_id": data["sessionId"],
                "pid": pid,
                "name": data.get("name"),
                "cwd": data.get("cwd"),
                "socket": data.get("messagingSocketPath"),
                "socket_present": _is_socket(data.get("messagingSocketPath")),
                "started_at": data.get("startedAt"),
            }
        )
    rows.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
    return rows


def peer_token(pid: int | None) -> str | None:
    """The inbox auth key the session published for peers (newest if several)."""
    if not isinstance(pid, int):
        return None
    try:
        files = sorted(
            sessions_dir().glob(f"{pid}.*.key"),
            key=lambda item: item.stat().st_mtime,
        )
    except OSError:
        return None
    for path in reversed(files):
        data = load_json(path)
        token = data.get("peerToken") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            return token
    return None


def transcript_path(session_id: str) -> Path | None:
    projects = paths.claude_dir() / "projects"
    candidates: list[Path] = []
    direct = projects / f"{session_id}.jsonl"
    if direct.is_file():
        candidates.append(direct)
    try:
        candidates.extend(
            item for item in projects.glob(f"*/{session_id}.jsonl") if item.is_file()
        )
    except OSError:
        pass
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda item: item.stat().st_mtime)
    except OSError:
        return candidates[-1]


def _last_permission_mode(path: Path) -> str | None:
    """Last ``permissionMode`` stamped on a user turn, scanning from the end."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            scanned = 0
            end = size
            carry = b""
            while end > 0 and scanned < _TRANSCRIPT_MAX:
                start = max(0, end - _TRANSCRIPT_TAIL)
                stream.seek(start)
                chunk = stream.read(end - start) + carry
                matches = list(_MODE_RE.finditer(chunk))
                if matches:
                    return matches[-1].group(1).decode("ascii", "replace")
                carry = chunk[:64]
                scanned += end - start
                end = start
    except OSError:
        return None
    return None


def mode_class_of(permission_mode: str | None) -> str | None:
    """Map a harness permission mode to the inbox's two attestation classes."""
    if not permission_mode:
        return None
    return "bypass" if permission_mode == "bypassPermissions" else "prompting"


def session_mode_class(session_id: str) -> str | None:
    path = transcript_path(session_id)
    if path is None:
        return None
    return mode_class_of(_last_permission_mode(path))


# --------------------------------------------------------------------------
# The wire: envelope + socket send
# --------------------------------------------------------------------------

def _clean_name(name: str) -> str:
    cleaned = re.sub(r'["<>\r\n]+', " ", name or "").strip()
    return cleaned[:64] or FROM_NAME


def envelope(body: str, *, from_name: str = FROM_NAME,
             mode_class: str | None = None) -> str:
    """Exactly one harness-formed envelope around ``body``.

    The recipient parses ``from-name``/``from-mode`` only when the whole message
    is one envelope, so the body must not contain the closing tag.
    """
    safe_body = body.replace("</cross-session-message>", "</cross-session-message >")
    attrs = f' from-name="{_clean_name(from_name)}"'
    if mode_class in MODE_CLASSES:
        attrs += f' from-mode="{mode_class}"'
    return f"<cross-session-message{attrs}>\n{safe_body.strip()}\n</cross-session-message>"


def send_to_socket(socket_path: str, token: str | None, content: str, *,
                   timeout: float = 5.0) -> None:
    """Deliver one user message into a session inbox. Raises OSError on failure."""
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": content}}))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(socket_path)
        client.sendall(payload)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        # The inbox only answers senders that gave a reply address; drain
        # briefly so a receipt never lands as ECONNRESET on the server side.
        client.settimeout(min(timeout, 1.0))
        try:
            while client.recv(65536):
                pass
        except (socket.timeout, OSError):
            pass
    finally:
        client.close()


def resolve_mode_class(session_id: str, requested: str | None = None) -> str | None:
    """Which class to declare: explicit > SUBFLEET_NOTIFY_MODE > recipient's own."""
    if requested in MODE_CLASSES:
        return requested
    if requested == "none":
        return None
    override = (os.environ.get("SUBFLEET_NOTIFY_MODE") or "").strip().lower()
    if override in MODE_CLASSES:
        return override
    if override == "none":
        return None
    return session_mode_class(session_id)


def push_to_session(session_id: str, body: str, *, from_name: str = FROM_NAME,
                    mode_class: str | None = None,
                    timeout: float = 5.0) -> dict[str, Any]:
    """Best-effort push; never raises. ``delivered`` means the inbox accepted
    the bytes — the harness gives no acknowledgement to address-less senders."""
    entry = find_session(session_id)
    result: dict[str, Any] = {
        "delivered": False,
        "session_id": session_id,
        "at": iso(now_local()),
    }
    if entry is None:
        result["reason"] = "session-not-registered"
        return result
    result.update({"pid": entry["pid"], "socket": entry["socket"], "name": entry["name"]})
    if not entry["alive"]:
        result["reason"] = "session-not-running"
        return result
    if not entry["socket_present"]:
        result["reason"] = "no-inbox-socket"
        return result
    token = peer_token(entry["pid"])
    if token is None:
        result["reason"] = "no-peer-token"
        return result
    declared = resolve_mode_class(session_id, mode_class)
    result["mode_class"] = declared
    try:
        send_to_socket(entry["socket"], token, envelope(body, from_name=from_name, mode_class=declared),
                       timeout=timeout)
    except (OSError, ValueError) as exc:
        result["reason"] = f"send-failed: {exc.__class__.__name__}: {exc}"
        return result
    result["delivered"] = True
    return result


# --------------------------------------------------------------------------
# Parked notices for sessions that were not live at finish time
# --------------------------------------------------------------------------

def notices_dir() -> Path:
    return paths.state_dir() / "notices"


def notices_path(session_id: str) -> Path:
    safe = _SAFE_ID.sub("-", session_id).strip("-") or "unknown"
    return notices_dir() / f"{safe}.jsonl"


class _NoticeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> "_NoticeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = (self.path.parent / f".{self.path.name}.lock").open("a")
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def _read_notices(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("run_id"), str):
            rows.append(row)
    return rows


def _write_notices(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.parent / f".{path.name}.tmp"
    with tmp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def append_notice(session_id: str, notice: dict[str, Any]) -> None:
    path = notices_path(session_id)
    with _NoticeLock(path):
        rows = _read_notices(path)
        rows = [row for row in rows if row.get("run_id") != notice.get("run_id")]
        rows.append(notice)
        _write_notices(path, rows)


def pending_notices(session_id: str, *, include_pushed: bool = False) -> list[dict[str, Any]]:
    rows = _read_notices(notices_path(session_id))
    return [
        row for row in rows
        if not row.get("surfaced") and (include_pushed or not row.get("pushed"))
    ]


def mark_surfaced(session_id: str, run_ids: list[str], *,
                  at: datetime | None = None) -> int:
    path = notices_path(session_id)
    wanted = set(run_ids)
    stamp = iso(at or now_local())
    changed = 0
    with _NoticeLock(path):
        rows = _read_notices(path)
        for row in rows:
            if row.get("run_id") in wanted and not row.get("surfaced"):
                row["surfaced"] = True
                row["surfaced_at"] = stamp
                changed += 1
        if changed:
            _write_notices(path, rows)
    return changed


def prune_notices(*, max_age_days: int = 14, now: datetime | None = None) -> int:
    """Drop surfaced notices older than ``max_age_days`` (keeps files small)."""
    now = now or now_local()
    removed = 0
    try:
        files = list(notices_dir().glob("*.jsonl"))
    except OSError:
        return 0
    for path in files:
        with _NoticeLock(path):
            rows = _read_notices(path)
            keep = []
            for row in rows:
                stamp = parse_iso(row.get("surfaced_at") or row.get("ts"))
                old = stamp is not None and (now - stamp).days >= max_age_days
                if row.get("surfaced") and old:
                    removed += 1
                    continue
                keep.append(row)
            if len(keep) != len(rows):
                _write_notices(path, keep)
            if not keep:
                try:
                    path.unlink()
                except OSError:
                    pass
    return removed


# --------------------------------------------------------------------------
# The notice itself
# --------------------------------------------------------------------------

def _short_home(value: str | None) -> str:
    if not value:
        return "-"
    home = str(Path.home())
    return "~" + value[len(home):] if value.startswith(home) else value


def _duration(seconds: float | int | None) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _first_line(path: Path | None, limit: int = 160) -> str | None:
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    return line if len(line) <= limit else line[: limit - 1] + "…"
    except OSError:
        return None
    return None


def _tail(path: Path | None, lines: int = 3, limit: int = 400) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rows = [row.rstrip() for row in text.splitlines() if row.strip()]
    if not rows:
        return None
    joined = "\n".join(rows[-lines:])
    return joined if len(joined) <= limit else "…" + joined[-limit:]


def format_notice(meta: dict[str, Any], run_dir: Path | None = None) -> str:
    """The completion message a session receives. Metadata and paths only —
    never the prompt or the output body (those stay on disk)."""
    run_id = str(meta.get("id") or "?")
    rc = meta.get("rc")
    state = "FINISHED" if rc == 0 else f"FAILED rc={rc}"
    lane = _short_home(meta.get("lane"))
    out_path = meta.get("original_out_path") or meta.get("out_path")
    if not out_path and run_dir is not None:
        out_path = str(run_dir / "out.md")
    out_file = Path(out_path) if out_path else None
    try:
        size = out_file.stat().st_size if out_file else 0
    except OSError:
        size = 0
    lines = [
        f"subfleet: run {run_id} {state} · {meta.get('model') or '-'} · lane={lane}"
        f" · {_duration(meta.get('duration_s'))}",
        f"out: {out_path or '-'} ({size:,} bytes)",
    ]
    first = _first_line(out_file) if size else None
    if first:
        lines.append(f"first line: {first}")
    if meta.get("salvage_refs"):
        refs = ", ".join(str(item.get("ref")) for item in meta["salvage_refs"] if isinstance(item, dict))
        lines.append(f"salvage refs: {refs}")
    if rc != 0 and run_dir is not None:
        err_tail = _tail(run_dir / "err.log")
        if err_tail:
            lines.append("err tail:\n" + err_tail)
    lines.append(f"ledger: subfleet runs show {run_id}")
    lines.append(
        "Automated completion notice for a run this session dispatched with "
        "`subfleet run`. Read the output file and continue; no reply is needed."
    )
    return "\n".join(lines)


def on_finish(run_id: str, run_dir: Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Push the completion notice to the dispatching session; park it if the
    session is not live. Returns what happened for the ledger, or None when
    the run was not dispatched from a Claude session."""
    caller = meta.get("caller")
    if not isinstance(caller, dict) or not isinstance(caller.get("session_id"), str):
        return None
    session_id = caller["session_id"]
    text = format_notice(meta, run_dir)
    waiter = caller.get("waiter_pid")
    if isinstance(waiter, int) and _pid_alive(waiter):
        # `subfleet run --attach` is still blocked on this run and will report
        # it itself; a push now would only duplicate that. (A dead waiter —
        # the session restarted mid-wait — falls through to the push.)
        return {
            "run_id": run_id, "ts": iso(now_local()), "rc": meta.get("rc"), "text": text,
            "pushed": False, "surfaced": True, "surfaced_at": iso(now_local()),
            "push": {"delivered": False, "reason": "inline-waiter-alive", "waiter_pid": waiter},
        }
    push = push_to_session(session_id, text)
    notice = {
        "run_id": run_id,
        "ts": iso(now_local()),
        "rc": meta.get("rc"),
        "text": text,
        "pushed": bool(push.get("delivered")),
        "push": push,
        "surfaced": bool(push.get("delivered")),
        "surfaced_at": push.get("at") if push.get("delivered") else None,
    }
    try:
        append_notice(session_id, notice)
    except OSError:
        pass
    return notice


def render_pending(session_id: str, rows: list[dict[str, Any]]) -> str:
    """Hook context for notices that could not be pushed live."""
    if not rows:
        return ""
    head = (
        f"subfleet: {len(rows)} detached run{'s' if len(rows) != 1 else ''} "
        "dispatched by this session finished while it was not running:"
    )
    blocks = [head]
    for row in rows:
        text = str(row.get("text") or f"run {row.get('run_id')} finished")
        blocks.append(text)
    blocks.append("List: subfleet runs --mine · details: subfleet runs show <id>")
    return "\n\n".join(blocks)
