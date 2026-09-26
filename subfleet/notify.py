"""Completion notices back to the Claude Code session that dispatched a run.

A detached `subfleet run` outlives the session that launched it (that is the
point: lanes survive Claude account switches). The cost was that nobody told
the session when the run finished — it had to poll. This module closes that
gap with the harness's own cross-session inbox:

* every interactive Claude Code session registers ``~/.claude/sessions/<pid>.json``
  (``sessionId``, ``messagingSocketPath``, ``name``) and publishes its inbox
  auth key beside it (``<pid>.<hash>.key`` → ``peerToken``);
* the inbox speaks newline-delimited JSON on a unix socket:
  ``{"type":"auth","token":…}`` then
  ``{"type":"user","message":{"role":"user","content":…}}``;
* a body wrapped as exactly one ``<cross-session-message …>`` envelope is
  parsed by the recipient; ``from-mode`` declares the sender's permission
  class (``bypass`` / ``prompting``). A same-class message is delivered at
  once; an undeclared or cross-class message is HELD for the user at a
  recipient that runs without permission prompts.

Does a delivered push wake the session? Not always, and "delivered" only
means the socket accepted the bytes (the inbox acknowledges nothing to an
address-less sender):

* verified 2026-08-23 at a CLI-hosted session (the ``carpool run`` detach
  commit, 13ebe223: dispatched from pid 78753, delivered to pid 76529 after a
  restart): an idle session opened a turn on the push at once, and a busy one
  saw it at its next turn boundary.
* measured FALSE 2026-09-06 21:40:10 -04:00 at a desktop-app seat (session
  29c03102, cwd ~/PolicyEngine/social-security-model, registry pid 89257,
  socket /tmp/cc-socks/89257.sock): the push was recorded as
  ``pushed:true, push.delivered:true`` and the seat's transcript holds NO
  entry between 2026-09-07T01:34:15Z and 09:43:30Z — not the ``user`` entry a
  push writes when it opens a turn, not the ``queue-operation`` +
  ``attachment`` pair it writes when it rides along the next turn. The app's
  main.log then shows ``[WarmLifecycle:session] Idle timeout reached,
  disconnecting local_758dc8c3…`` and ``[CCD] Pausing session local_758dc8c3…
  (idle_timeout)`` at 21:49:15 (its 900 s idle timeout, counted from the
  seat's last turn at 21:34:15), and the seat produced nothing until Max
  typed at 05:43. An accepted push into an idle desktop-app seat can vanish
  without a trace.

So a push is never marked ``surfaced`` at push time. It is confirmed only
when the recipient's transcript shows the notice (``push_evidence``: the
notice's own first line, ``subfleet: run <id> …``, in any entry stamped at or
after the push), or when the SessionStart / UserPromptSubmit hook renders it
as context (cli.cmd_session_hook). Until then the notice stays unresolved,
and tickle.notice_followup checks on it after a grace period: a live but
silent session gets ONE re-push; a session with no live registry pid gets a
one-shot ``claude -p --resume`` host (the existing revive machinery) with the
notice as its prompt; a notice still unconfirmed after that is rendered by
the hooks at the session's next prompt or start.

We resolve the recipient by SESSION ID at notification time, never by the pid
or socket captured at dispatch: an account switch restarts the session under
a new pid and a new socket path, but the session id is stable. When no live
inbox exists the notice is parked in ``state/subfleet/notices/<session>.jsonl``
and the SessionStart / UserPromptSubmit hook (``bin/subfleet-hook``) surfaces
it the next time that session is up.

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
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import paths
from .util import iso, load_json, now_local, parse_iso, reversed_lines

FROM_NAME = "subfleet"
MODE_CLASSES = ("bypass", "prompting")
_TRANSCRIPT_TAIL = 1024 * 1024
_TRANSCRIPT_MAX = 64 * 1024 * 1024
_MODE_RE = re.compile(rb'"permissionMode"\s*:\s*"([A-Za-z]+)"')
_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
NOTICE_PREFIX = "subfleet: run "
# A push that lands writes its entry within seconds of ``push.at`` (measured
# 0–14 s over 140 delivered pushes at the ceremony seat, 2026-08-28..09-07);
# ``push.at`` itself is stamped to the second, so evidence a few seconds
# before it is still the same push. Scanning stops well past that.
EVIDENCE_SLACK_S = 5.0
EVIDENCE_STOP_SLACK_S = 600.0
# Timestamps in a transcript are not monotonic (an attachment can carry the
# stamp of the turn it belongs to, written later): stop only after this many
# consecutive entries older than the window.
_EVIDENCE_STOP_RUN = 50


# --------------------------------------------------------------------------
# Who is asking: the Claude Code session around the current process.
# --------------------------------------------------------------------------

def in_claude_session(env: dict[str, str] | None = None) -> bool:
    """True inside a Claude Code session's tool shell (or anything it spawned).

    The harness exports ``CLAUDECODE=1`` and ``CLAUDE_CODE_SESSION_ID`` to every
    Bash tool process; both are inherited by nohup'd children, which is exactly
    the process tree that dies on an account switch.
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
# Session registry (~/.claude/sessions) and transcripts (~/.claude/projects)
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


def live_sessions(include_lanes: bool = False) -> list[dict[str, Any]]:
    """Every registry row whose process is alive (for `subfleet sessions`).

    Lane sessions (headless ``claude -p`` runs) are hidden unless
    ``include_lanes`` — addressing one overwrites its deliverable."""
    from . import lanes  # local import: lanes -> capacity, never back here
    lane_ids = lanes.lane_session_ids()
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
        lane = lanes.is_lane_session(data["sessionId"], transcript_path(data["sessionId"]), lane_ids)
        if lane and not include_lanes:
            continue
        rows.append(
            {
                "session_id": data["sessionId"],
                "lane": lane,
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
                    timeout: float = 5.0, force: bool = False) -> dict[str, Any]:
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
    from . import lanes  # local import (see live_sessions)
    if not force and lanes.is_lane_session(session_id, transcript_path(session_id)):
        # A lane's deliverable is its last message; a pushed notice becomes
        # that message. Relay to the lane's orchestrator instead.
        result["reason"] = "lane-session: headless run, a notice would overwrite its deliverable (relay to its orchestrator; --force overrides)"
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


def unresolved_notices(session_id: str) -> list[dict[str, Any]]:
    """Every notice for the session that nothing has yet confirmed as seen:
    parked ones (no live inbox at finish time) and pushed ones whose push has
    left no trace in the transcript (see ``push_evidence``)."""
    return [row for row in _read_notices(notices_path(session_id)) if not row.get("surfaced")]


def sessions_with_unresolved() -> list[str]:
    """Session ids that have at least one unresolved notice (for the
    follow-up pass). The id comes from the row when it carries one, else
    from the file name (a UUID survives ``notices_path`` unchanged)."""
    found: list[str] = []
    try:
        files = sorted(notices_dir().glob("*.jsonl"))
    except OSError:
        return found
    for path in files:
        rows = [row for row in _read_notices(path) if not row.get("surfaced")]
        if not rows:
            continue
        session_id = next((row["session_id"] for row in rows
                           if isinstance(row.get("session_id"), str) and row["session_id"]), path.stem)
        if session_id not in found:
            found.append(session_id)
    return found


def mark_surfaced(session_id: str, run_ids: list[str], *,
                  at: datetime | None = None,
                  how: str | None = None) -> int:
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
                if how:
                    row["surfaced_by"] = how
                changed += 1
        if changed:
            _write_notices(path, rows)
    return changed


def update_notices(session_id: str, run_ids: Iterable[str],
                   mutate: Callable[[dict[str, Any]], None]) -> int:
    """Apply ``mutate`` to the matching rows under the file lock; returns the
    number of rows touched. The follow-up bookkeeping goes under
    ``row["followup"]`` (pushes, revive, lost) so the original push record
    stays as written."""
    path = notices_path(session_id)
    wanted = set(run_ids)
    touched = 0
    with _NoticeLock(path):
        rows = _read_notices(path)
        for row in rows:
            if row.get("run_id") in wanted:
                mutate(row)
                touched += 1
        if touched:
            _write_notices(path, rows)
    return touched


def followup_of(row: dict[str, Any]) -> dict[str, Any]:
    followup = row.get("followup")
    return followup if isinstance(followup, dict) else {}


def delivery_attempts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every push that the inbox accepted, oldest first: the finish-time push
    (when delivered) and the follow-up's re-pushes."""
    attempts: list[dict[str, Any]] = []
    push = row.get("push")
    if row.get("pushed") and isinstance(push, dict):
        attempts.append(push)
    for item in followup_of(row).get("pushes") or []:
        if isinstance(item, dict) and item.get("delivered"):
            attempts.append(item)
    return attempts


def first_push_at(row: dict[str, Any]) -> datetime | None:
    """When the notice first reached an inbox — the evidence window opens here."""
    attempts = delivery_attempts(row)
    if attempts:
        return parse_iso(attempts[0].get("at"))
    return None


def delivery_anchor(row: dict[str, Any]) -> datetime | None:
    """The moment the follow-up's grace period counts from: the newest push
    the inbox accepted, else the newest follow-up action, else the finish
    stamp (a parked notice waits its grace before anything happens)."""
    stamps: list[datetime] = []
    for attempt in delivery_attempts(row):
        at = parse_iso(attempt.get("at"))
        if at is not None:
            stamps.append(at)
    followup = followup_of(row)
    for key in ("revive", "lost"):
        item = followup.get(key)
        at = parse_iso(item.get("at")) if isinstance(item, dict) else None
        if at is not None:
            stamps.append(at)
    if stamps:
        return max(stamps)
    push = row.get("push") if isinstance(row.get("push"), dict) else {}
    return parse_iso(push.get("at")) or parse_iso(row.get("ts"))


# --------------------------------------------------------------------------
# Did the push reach the transcript?
# --------------------------------------------------------------------------

def notice_signature(run_id: str) -> str:
    """The notice's own first-line stamp (``format_notice`` writes it; the
    transcript scan looks for it). The trailing space keeps ``…-c4`` from
    matching ``…-c4-verify``."""
    return f"{NOTICE_PREFIX}{run_id} "


def _entry_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.search(line)
    return parse_iso(match.group(1)) if match else None


def push_evidence(transcript: str | Path | None, run_id: str, after: datetime | None, *,
                  max_bytes: int = _TRANSCRIPT_MAX) -> dict[str, Any]:
    """What the recipient's transcript shows for this notice since ``after``.

    A push that lands leaves the notice text in the transcript within seconds
    (measured at the ceremony seat, 2026-08-28..09-07): as a ``user`` entry
    (``isMeta``, "Another Claude session sent a message: <cross-session-message
    …>") when it opens a turn in an idle session, or as a ``queue-operation``
    + ``attachment`` pair when the session is busy and the message rides along
    its next turn. A one-shot revive host writes the prompt it was given as a
    ``user`` entry, and the hooks' rendered context lands the same way. So the
    test is simply: an entry stamped at or after the push whose raw line
    carries ``notice_signature(run_id)``.

    Returns ``{"landed_at", "turn_at", "entries", "complete"}``: ``landed_at``
    is the EARLIEST such entry (None when there is none); ``turn_at`` the
    earliest assistant turn after the push (evidence that the session was
    awake, NOT that it saw the notice — the 2026-09-06 21:40 push was followed
    by Max's own prompt eight hours later and the notice never appeared);
    ``complete`` is False when the scan hit ``max_bytes`` before reaching the
    window's start, in which case an absent ``landed_at`` proves nothing.
    """
    result: dict[str, Any] = {"landed_at": None, "turn_at": None, "entries": 0, "complete": True}
    if not transcript or after is None:
        return result
    path = Path(transcript).expanduser()
    if not path.is_file():
        return result
    from .tickle import RESUME_STUB_ASSISTANT  # local: tickle imports this module
    signature = notice_signature(run_id)
    floor = after - timedelta(seconds=EVIDENCE_SLACK_S)
    stop_below = after - timedelta(seconds=EVIDENCE_STOP_SLACK_S)
    older_run = 0
    reached_start = False
    # Scanning backwards, the LAST match kept is the earliest one in the file.
    for line in reversed_lines(path, max_bytes=max_bytes):
        stamp = _entry_timestamp(line)
        if stamp is None:
            continue
        result["entries"] += 1
        if stamp < stop_below:
            older_run += 1
            if older_run >= _EVIDENCE_STOP_RUN:
                reached_start = True
                break
            continue
        older_run = 0
        if stamp < floor:
            continue
        if signature in line:
            result["landed_at"] = iso(stamp)
        if '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (
            not isinstance(entry, dict) or entry.get("type") != "assistant"
            or entry.get("isSidechain") or entry.get("isMeta")
        ):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(str(block.get("text") or "") for block in content
                             if isinstance(block, dict) and block.get("type") == "text")
        else:
            text = ""
        if text.strip() != RESUME_STUB_ASSISTANT:  # the app's restart stub is not a turn
            result["turn_at"] = iso(stamp)
    else:
        # The loop ran out of lines: either the whole file was read (the
        # window's start is before its first line) or the byte cap cut it.
        try:
            reached_start = path.stat().st_size <= max_bytes
        except OSError:
            reached_start = False
    result["complete"] = reached_start
    return result


def evidence_window_start(row: dict[str, Any]) -> datetime | None:
    """From when the notice's text could possibly appear: the earlier of the
    finish stamp (the text is minted then) and the first accepted push."""
    stamps = [stamp for stamp in (parse_iso(row.get("ts")), first_push_at(row)) if stamp is not None]
    return min(stamps) if stamps else None


def _transcript_stat(transcript: str | Path | None) -> dict[str, int] | None:
    if not transcript:
        return None
    try:
        stat = Path(transcript).expanduser().stat()
    except OSError:
        return None
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def confirm_surfaced(session_id: str, transcript: str | Path | None, *,
                     rows: list[dict[str, Any]] | None = None,
                     now: datetime | None = None) -> dict[str, str]:
    """Mark every unresolved notice whose text the transcript shows since it
    was minted as surfaced (``surfaced_by: transcript``). Returns
    ``{run_id: landed_at}`` for the ones confirmed this call.

    The transcript is re-read only when it changed since the row's last look
    (``followup.checked`` remembers mtime and size): the two-minute backstop
    pass must not re-scan a dormant session's transcript every time.
    """
    rows = unresolved_notices(session_id) if rows is None else rows
    stat = _transcript_stat(transcript)
    confirmed: dict[str, str] = {}
    looked: list[str] = []
    for row in rows:
        if row.get("surfaced"):
            continue
        run_id = row.get("run_id")
        after = evidence_window_start(row)
        if not isinstance(run_id, str) or after is None:
            continue
        checked = followup_of(row).get("checked")
        if stat is not None and isinstance(checked, dict) and all(checked.get(k) == v for k, v in stat.items()):
            continue  # nothing new to read since the last look
        evidence = push_evidence(transcript, run_id, after)
        if evidence.get("landed_at"):
            confirmed[run_id] = evidence["landed_at"]
        elif stat is not None and evidence.get("complete"):
            looked.append(run_id)
    stamp = now or now_local()
    if confirmed:
        def mutate(row: dict[str, Any]) -> None:
            if row.get("surfaced"):
                return
            row["surfaced"] = True
            row["surfaced_at"] = confirmed.get(row.get("run_id")) or iso(stamp)
            row["surfaced_by"] = "transcript"
            followup = row.setdefault("followup", {})
            if isinstance(followup, dict):
                followup["landed_at"] = confirmed.get(row.get("run_id"))
                followup["confirmed_at"] = iso(stamp)

        update_notices(session_id, list(confirmed), mutate)
    if looked:
        def remember(row: dict[str, Any]) -> None:
            followup = row.setdefault("followup", {})
            if isinstance(followup, dict):
                followup["checked"] = {**stat, "at": iso(stamp)}

        update_notices(session_id, looked, remember)
    return confirmed


def notices_for_hook(session_id: str, event: str, transcript: str | Path | None, *,
                     source: str | None = None, now: datetime | None = None,
                     grace_s: float | None = None) -> list[dict[str, Any]]:
    """What the SessionStart / UserPromptSubmit hook should render.

    Parked notices (never delivered) always; pushed ones only when the push
    is not confirmed by the transcript AND either the session is (re)starting
    — the inbox that accepted the bytes died with the previous process — or
    the push is older than the follow-up grace (or already marked LOST), i.e.
    it has had its chance to open a turn or ride along one and did neither.
    A push younger than that may still be queued behind the turn the user is
    about to start; rendering it too would duplicate it. A SessionStart for
    ``compact`` / ``clear`` is the same process with its inbox intact, so it
    is gated like a prompt.
    """
    now = now or now_local()
    if grace_s is None:
        from .tickle import followup_grace_s  # local: tickle imports this module
        grace_s = followup_grace_s()
    rows = unresolved_notices(session_id)
    if not rows:
        return []
    confirmed = confirm_surfaced(session_id, transcript, rows=rows, now=now)
    rows = [row for row in rows if row.get("run_id") not in confirmed]
    if event == "session-start" and source not in {"compact", "clear"}:
        return rows
    chosen = []
    for row in rows:
        if not row.get("pushed") or followup_of(row).get("lost"):
            chosen.append(row)
            continue
        anchor = delivery_anchor(row)
        if anchor is None or (now - anchor).total_seconds() >= grace_s:
            chosen.append(row)
    return chosen


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
        f"{notice_signature(run_id)}{state} · {meta.get('model') or '-'} · lane={lane}"
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
    the run was not dispatched from a Claude session.

    A delivered push is recorded as ``pushed`` but NOT ``surfaced``: the
    inbox accepting the bytes is not the session seeing them (module
    docstring: the 2026-09-06 21:40 push). ``confirm_surfaced`` marks it once
    the transcript shows the notice; ``tickle.notice_followup`` acts when it
    does not.
    """
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
            "run_id": run_id, "session_id": session_id, "ts": iso(now_local()), "rc": meta.get("rc"),
            "text": text, "pushed": False, "surfaced": True, "surfaced_at": iso(now_local()),
            "surfaced_by": "inline-waiter",
            "push": {"delivered": False, "reason": "inline-waiter-alive", "waiter_pid": waiter},
        }
    push = push_to_session(session_id, text)
    notice = {
        "run_id": run_id,
        "session_id": session_id,
        "ts": iso(now_local()),
        "rc": meta.get("rc"),
        "text": text,
        "pushed": bool(push.get("delivered")),
        "push": push,
        "surfaced": False,
        "surfaced_at": None,
    }
    try:
        append_notice(session_id, notice)
    except OSError:
        pass
    return notice


def render_pending(session_id: str, rows: list[dict[str, Any]]) -> str:
    """Hook context for notices that never reached the session: parked ones
    (no live inbox at finish time) and pushed ones whose push left no trace
    in the transcript."""
    if not rows:
        return ""
    pushed = [row for row in rows if row.get("pushed")]
    parked = [row for row in rows if not row.get("pushed")]
    count = len(rows)
    if pushed and parked:
        why = ("finished; some notices were pushed into this session's inbox but never "
               "showed in its transcript, the rest arrived while it was not running:")
    elif pushed:
        why = ("finished; their completion notices were pushed into this session's inbox "
               "but never showed in its transcript (the push left no trace):")
    else:
        why = "finished while it was not running:"
    head = f"subfleet: {count} detached run{'s' if count != 1 else ''} dispatched by this session {why}"
    blocks = [head]
    for row in rows:
        text = str(row.get("text") or f"run {row.get('run_id')} finished")
        push = row.get("push") if isinstance(row.get("push"), dict) else {}
        if row.get("pushed") and push.get("at"):
            text += f"\n(pushed {push['at']}; no trace in the transcript since)"
        blocks.append(text)
    blocks.append("List: subfleet runs --mine · details: subfleet runs show <id>")
    return "\n\n".join(blocks)
