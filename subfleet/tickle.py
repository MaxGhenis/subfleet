"""Resume nudges: wake a restarted Claude Code session whose last turn was cut off.

An account switch (or an app relaunch) restarts every open session. The
conversation comes back, but a session that was mid-turn just sits there until
someone opens it and types "." — one session at a time. The SessionStart hook
already runs inside every restarted session; from there we can read the
session's own transcript, decide whether its last turn was interrupted, and
push a *continue* message into the session's inbox (inbox.py), which starts a
turn exactly like the "." would.

Interrupted means the transcript's last main-chain entry is one of:

* an assistant message carrying a ``tool_use`` block — the tool result was
  never recorded (the process died between call and result);
* a user message made of ``tool_result`` blocks — the model owed a
  continuation and never produced it;
* a user message with real text that has no assistant reply.

An assistant message that ends in plain text is a completed turn: the session
was idle by choice and is left alone. So a session that finished its work
never gets poked; only work that was cut off resumes.

The desktop app's own resume writes a synthetic pair into every restarted
session moments after the new process starts — a hidden user line "Continue
from where you left off." and a fake assistant reply "No response requested."
with the same timestamp. That stub makes an interrupted transcript look
completed, so the classifier skips it and judges the turn underneath; each
stub has its own uuid, so every restart earns one nudge.

Guards: off switch (``SUBFLEET_TICKLE=off``); only SessionStart sources
``startup``/``resume`` (never ``compact`` or ``clear``); an age cap on the
interruption (``SUBFLEET_TICKLE_MAX_AGE_S``, default 8h — an old abandoned
turn is not resumed just because the app reopened its tab); one nudge per
interruption point (the transcript uuid is remembered) and a per-session
cooldown; and a re-check after the startup delay, so a session that already
continued on its own is not nudged on top of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import inbox, paths
from .util import atomic_write_json, iso, load_json, now_local, parse_iso

MARKER = "subfleet: this session restarted"
RESUME_STUB_USER = "Continue from where you left off."
RESUME_STUB_ASSISTANT = "No response requested."
DEFAULT_MAX_AGE_S = 8 * 3600
# Dedupe already blocks the same interruption point, so the cooldown only
# absorbs restart storms; a double sign-in (one hop per account) restarts
# sessions twice minutes apart and each hop deserves its nudge.
DEFAULT_COOLDOWN_S = 90
DEFAULT_DELAY_S = 8.0
DEFAULT_MUSTER_MAX_AGE_S = 2 * 3600
MUSTER_MARKER = "subfleet muster: roll call"
_TAIL_BYTES = 512 * 1024
_SCAN_MAX = 64 * 1024 * 1024
ALLOWED_SOURCES = {"startup", "resume"}


# --------------------------------------------------------------------------
# Transcript inspection
# --------------------------------------------------------------------------

def _lines_reversed(path: Path, *, chunk: int = _TAIL_BYTES, max_bytes: int = _SCAN_MAX):
    """Yield lines from the end of the file backwards, reading in chunks, so a
    long run of subagent entries at the tail cannot hide the last main turn."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            end = size
            carry = b""
            scanned = 0
            while end > 0 and scanned < max_bytes:
                start = max(0, end - chunk)
                stream.seek(start)
                block = stream.read(end - start) + carry
                scanned += end - start
                parts = block.split(b"\n")
                carry = parts[0] if start > 0 else b""
                lines = parts[1:] if start > 0 else parts
                for raw in reversed(lines):
                    if raw.strip():
                        yield raw.decode("utf-8", "replace")
                end = start
            if carry.strip() and scanned < max_bytes:
                yield carry.decode("utf-8", "replace")
    except OSError:
        return


def _blocks(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _text_of(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text").strip()


def turn_state(transcript: str | Path | None) -> dict[str, Any]:
    """Classify how the transcript ends: interrupted / completed / tickled / empty."""
    result: dict[str, Any] = {
        "state": "empty", "detail": "no transcript", "last_uuid": None,
        "timestamp": None, "age_s": None, "path": str(transcript) if transcript else None,
    }
    if not transcript:
        return result
    path = Path(transcript).expanduser()
    if not path.is_file():
        return result
    last: dict[str, Any] | None = None
    stubs = 0
    stub_uuid: str | None = None
    limit_banner = False
    for line in _lines_reversed(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") not in {"user", "assistant"}:
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        if entry["type"] == "assistant":
            if _text_of(_blocks(entry.get("message"))) == RESUME_STUB_ASSISTANT:
                # the app's resume stub: not a model turn, look underneath it
                stubs += 1
                if stub_uuid is None and isinstance(entry.get("uuid"), str):
                    stub_uuid = entry["uuid"]
                continue
            if (
                entry.get("error")
                or entry.get("isApiErrorMessage") is True
                or (entry.get("quotaLimits") or {}).get("status") == "rejected"
            ):
                # provider-error banner ("You've reached your usage limit…",
                # "You've hit your session limit · resets …"): written as an
                # assistant entry but not a model turn either
                limit_banner = True
                continue
        last = entry
        break
    result["restart_stubs"] = stubs
    result["stub_uuid"] = stub_uuid
    result["limit_banner"] = limit_banner
    if last is None:
        result["detail"] = "no user/assistant turns"
        return result
    blocks = _blocks(last.get("message"))
    kinds = {block.get("type") for block in blocks}
    stamp = parse_iso(last.get("timestamp")) if isinstance(last.get("timestamp"), str) else None
    result.update({
        "last_uuid": last.get("uuid"),
        "timestamp": iso(stamp) if stamp else None,
        "age_s": round((now_local() - stamp).total_seconds()) if stamp else None,
    })
    marks = []
    if limit_banner:
        marks.append("hit a usage limit")
    if stubs:
        marks.append("behind the app's resume stub")
    suffix = f" ({', '.join(marks)})" if marks else ""
    if last["type"] == "assistant":
        if "tool_use" in kinds:
            names = [str(block.get("name")) for block in blocks if block.get("type") == "tool_use"]
            result.update({"state": "interrupted",
                           "detail": f"a tool call never got its result ({', '.join(names[:3])}){suffix}"})
        elif limit_banner:
            # trailing assistant text with a limit banner above it: the session
            # was still making requests when the limit hit (the text was
            # mid-work narration, or the auto-continue itself was rejected).
            # Err toward resuming — a genuinely finished session answers a
            # nudge with one cheap "nothing pending" turn, while a missed
            # resume strands real work.
            result.update({"state": "interrupted",
                           "detail": f"the model was cut off by a usage limit after its last text{suffix}"})
        else:
            result.update({"state": "completed", "detail": f"last turn ended in assistant text{suffix}"})
        return result
    # user entry
    if "tool_result" in kinds:
        result.update({"state": "interrupted",
                       "detail": f"a tool result arrived but the model never continued{suffix}"})
        return result
    text = _text_of(blocks)
    if any(mark in text[:400] for mark in (MARKER, MUSTER_MARKER)):
        result.update({"state": "tickled", "detail": "the last message is already a subfleet nudge"})
        return result
    if text.startswith("[Request interrupted by user"):
        result.update({"state": "stopped", "detail": "the user interrupted the last turn (Esc)"})
        return result
    preview = text[:80].replace("\n", " ")
    result.update({"state": "interrupted",
                   "detail": (f"an unanswered prompt: “{preview}”" if preview else "an unanswered message") + suffix})
    return result


def dedupe_key(state: dict[str, Any]) -> str | None:
    """One nudge per interruption point — and per restart of it: the app's resume
    stub carries a fresh uuid each time, so a second restart earns a second nudge."""
    return state.get("stub_uuid") or state.get("last_uuid")


# --------------------------------------------------------------------------
# Decision + bookkeeping
# --------------------------------------------------------------------------

def tickles_dir() -> Path:
    return paths.state_dir() / "tickles"


def _record_path(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in session_id) or "unknown"
    return tickles_dir() / f"{safe}.json"


def load_record(session_id: str) -> dict[str, Any]:
    data = load_json(_record_path(session_id))
    return data if isinstance(data, dict) else {}


def save_record(session_id: str, record: dict[str, Any]) -> None:
    path = _record_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, record)


def enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("SUBFLEET_TICKLE") or "on").strip().lower() not in {"off", "0", "false", "no"}


def max_age_s(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        return float(env.get("SUBFLEET_TICKLE_MAX_AGE_S") or DEFAULT_MAX_AGE_S)
    except ValueError:
        return DEFAULT_MAX_AGE_S


def decide(session_id: str, transcript: str | Path | None, *, source: str | None,
           now: datetime | None = None, force: bool = False,
           cooldown_s: float = DEFAULT_COOLDOWN_S) -> dict[str, Any]:
    """Should this session get a resume nudge? Returns {tickle: bool, reason, state}."""
    now = now or now_local()
    state = turn_state(transcript)
    verdict: dict[str, Any] = {"session_id": session_id, "tickle": False, "state": state}
    if not enabled():
        verdict["reason"] = "disabled (SUBFLEET_TICKLE=off)"
        return verdict
    if source is not None and source not in ALLOWED_SOURCES and not force:
        verdict["reason"] = f"source {source!r} is not a restart"
        return verdict
    if state["state"] != "interrupted":
        verdict["reason"] = f"{state['state']}: {state['detail']}"
        return verdict
    if not force:
        age = state.get("age_s")
        if age is not None and age > max_age_s():
            verdict["reason"] = f"interrupted {age}s ago, older than the {int(max_age_s())}s cap"
            return verdict
        record = load_record(session_id)
        if record.get("last_uuid") and record["last_uuid"] == dedupe_key(state):
            verdict["reason"] = "already nudged at this interruption point"
            return verdict
        last_at = parse_iso(record.get("at"))
        if last_at is not None and (now - last_at).total_seconds() < cooldown_s:
            verdict["reason"] = f"nudged {int((now - last_at).total_seconds())}s ago (cooldown {int(cooldown_s)}s)"
            return verdict
    verdict["tickle"] = True
    verdict["reason"] = f"interrupted: {state['detail']}"
    return verdict


def message(state: dict[str, Any]) -> str:
    detail = state.get("detail") or "its last turn was interrupted"
    limit_line = (
        "The previous account or model hit its usage limit mid-task; you are on a fresh one now.\n"
        if state.get("limit_banner") else ""
    )
    return (
        f"{MARKER} (Claude account switch or app relaunch) with its last turn cut off — {detail}. "
        f"Continue where you left off.\n{limit_line}"
        "Before redoing anything: `git log --oneline -5` in your worktree and `subfleet runs --mine` — "
        "detached runs survived the restart and may already be finished (their completion notices arrive separately).\n"
        "(automated resume nudge from subfleet; no reply needed)"
    )


def _fingerprint(transcript: str | Path | None) -> tuple[Any, Any] | None:
    """Identity of the real last turn. The app's resume stub and bookkeeping rows
    (queue-operation, system, attachments) change the file without changing
    this; a session that is actually working changes it within seconds."""
    if not transcript:
        return None
    state = turn_state(transcript)
    if state["state"] == "empty":
        return None
    return (state.get("last_uuid"), state.get("timestamp"))


def muster_message(state: dict[str, Any]) -> str:
    return (
        f"{MUSTER_MARKER} after an account or model switch (nothing was killed — "
        "turns ended normally, e.g. the previous account moved to usage credits). "
        "Check for standing or pending work: your last instructions, any task "
        "notifications above, and `subfleet runs --mine` for detached runs that "
        "finished meanwhile. If something is pending, continue it now; if you are "
        "genuinely done, say so in one line and stand by.\n"
        "(automated roll call from subfleet; no reply beyond that is needed)"
    )


def muster_eligible(session_id: str, transcript: str | Path | None, *,
                    max_age_s: float | None = None) -> dict[str, Any]:
    """A roll call reaches interrupted AND recently-active completed sessions.

    Stopped (Esc), already-nudged, and empty transcripts stay out; so do
    sessions whose last turn is older than the window — a session idle since
    this morning was not orphaned by the switch that just happened.
    """
    if max_age_s is None:
        try:
            max_age_s = float(os.environ.get("SUBFLEET_MUSTER_MAX_AGE_S") or DEFAULT_MUSTER_MAX_AGE_S)
        except ValueError:
            max_age_s = DEFAULT_MUSTER_MAX_AGE_S
    state = turn_state(transcript)
    verdict: dict[str, Any] = {"session_id": session_id, "muster": False, "state": state}
    if state["state"] not in {"interrupted", "completed"}:
        verdict["reason"] = f"{state['state']}: {state['detail']}"
        return verdict
    age = state.get("age_s")
    if age is not None and age > max_age_s:
        verdict["reason"] = f"last turn {age}s ago, outside the {int(max_age_s)}s roll-call window"
        return verdict
    record = load_record(session_id)
    if record.get("last_uuid") and record["last_uuid"] == dedupe_key(state):
        verdict["reason"] = "already called at this point"
        return verdict
    verdict["muster"] = True
    verdict["reason"] = f"{state['state']}: {state['detail']}"
    return verdict


def deliver(session_id: str, transcript: str | Path | None, *, delay_s: float = 0.0,
            force: bool = False, min_idle_s: float = 0.0, sleep=time.sleep) -> dict[str, Any]:
    """Wait for the inbox, re-check the transcript, push the nudge, remember it.

    A session that is actually working keeps producing turns; a session cut off
    by a restart does not (the app's resume stub is not a turn). So the real
    last turn must be the same one across the wait (and, for manual sweeps, at
    least ``min_idle_s`` old) before the nudge goes out.
    """
    before = _fingerprint(transcript)
    if delay_s > 0:
        sleep(delay_s)
    def skipped(verdict: dict[str, Any]) -> dict[str, Any]:
        verdict["delivered"] = False
        record = load_record(session_id)
        history = [item for item in (record.get("history") or []) if isinstance(item, dict)][-19:]
        history.append({"at": iso(now_local()), "skip": verdict.get("reason")})
        record.update({"session_id": session_id, "history": history})
        try:
            save_record(session_id, record)
        except OSError:
            pass
        return verdict

    verdict = decide(session_id, transcript, source=None, force=force)
    if not verdict["tickle"]:
        return skipped(verdict)
    if not force:
        if _fingerprint(transcript) != before:
            verdict.update({"tickle": False,
                            "reason": "session is active (a new turn appeared during the wait)"})
            return skipped(verdict)
        age = verdict["state"].get("age_s")
        if min_idle_s and age is not None and age < min_idle_s:
            verdict.update({"tickle": False,
                            "reason": f"last activity {age}s ago; a manual sweep waits {int(min_idle_s)}s of quiet"})
            return skipped(verdict)
    state = verdict["state"]
    push = inbox.push_to_session(session_id, message(state), from_name="subfleet")
    verdict["push"] = push
    verdict["delivered"] = bool(push.get("delivered"))
    record = load_record(session_id)
    history = [item for item in (record.get("history") or []) if isinstance(item, dict)][-19:]
    history.append({"at": iso(now_local()), "uuid": state.get("last_uuid"),
                    "delivered": verdict["delivered"], "reason": push.get("reason")})
    save_record(session_id, {
        "session_id": session_id,
        "at": iso(now_local()),
        "last_uuid": dedupe_key(state),
        "turn_uuid": state.get("last_uuid"),
        "restart_stubs": state.get("restart_stubs"),
        "delivered": verdict["delivered"],
        "push": push,
        "history": history,
    })
    return verdict


def note(session_id: str, entry: dict[str, Any]) -> None:
    """Append a diagnostic line (hook-time decision) to the session's record."""
    record = load_record(session_id)
    history = [item for item in (record.get("history") or []) if isinstance(item, dict)][-19:]
    history.append({"at": iso(now_local()), **entry})
    record.update({"session_id": session_id, "history": history})
    try:
        save_record(session_id, record)
    except OSError:
        pass


def spawn(session_id: str, transcript: str | Path | None, *, delay_s: float = DEFAULT_DELAY_S,
          executable: str | None = None) -> int | None:
    """Start a detached nudger (the hook must return at once; the inbox binds
    a moment after SessionStart). Returns its pid."""
    launcher = executable or str(Path(__file__).resolve().parent.parent / "bin" / "subfleet")
    cmd = [launcher, "_tickle", "--session", session_id, "--delay", str(delay_s)]
    if transcript:
        cmd += ["--transcript", str(transcript)]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        print(f"subfleet tickle: spawn failed: {exc}", file=sys.stderr)
        return None
    return proc.pid


def survey(*, max_age: float | None = None) -> list[dict[str, Any]]:
    """Every live registered session with its transcript state (for `subfleet tickle --all`)."""
    rows = []
    for session in inbox.live_sessions():
        transcript = inbox.transcript_path(session["session_id"])
        state = turn_state(transcript)
        rows.append({
            "session_id": session["session_id"],
            "name": session.get("name"),
            "pid": session.get("pid"),
            "inbox": bool(session.get("socket_present")),
            "transcript": str(transcript) if transcript else None,
            **{k: state.get(k) for k in ("state", "detail", "age_s", "last_uuid")},
        })
    return rows


def muster_deliver(session_id: str, transcript: str | Path | None, *,
                   quiet_s: float = 120.0, sample_s: float = 3.0,
                   sleep=time.sleep) -> dict[str, Any]:
    """Roll-call one session: interrupted gets the resume nudge, completed gets
    the muster message. Same liveness discipline as the manual sweep."""
    before = _fingerprint(transcript)
    verdict = muster_eligible(session_id, transcript)
    if not verdict["muster"]:
        verdict["delivered"] = False
        return verdict
    state = verdict["state"]
    age = state.get("age_s")
    if age is not None and age < quiet_s:
        verdict.update({"muster": False, "delivered": False,
                        "reason": f"last activity {age}s ago; a roll call waits {int(quiet_s)}s of quiet"})
        return verdict
    if sample_s > 0:
        sleep(sample_s)
    if _fingerprint(transcript) != before:
        verdict.update({"muster": False, "delivered": False,
                        "reason": "session is active (a new turn appeared during the wait)"})
        return verdict
    body = message(state) if state["state"] == "interrupted" else muster_message(state)
    push = inbox.push_to_session(session_id, body, from_name="subfleet")
    verdict["push"] = push
    verdict["delivered"] = bool(push.get("delivered"))
    record = load_record(session_id)
    history = [item for item in (record.get("history") or []) if isinstance(item, dict)][-19:]
    history.append({"at": iso(now_local()), "muster": True, "uuid": dedupe_key(state),
                    "delivered": verdict["delivered"], "reason": push.get("reason")})
    save_record(session_id, {
        **record, "session_id": session_id, "at": iso(now_local()),
        "last_uuid": dedupe_key(state), "delivered": verdict["delivered"],
        "push": push, "history": history,
    })
    return verdict


def cold_sessions(*, max_age_s: float | None = None) -> list[dict[str, Any]]:
    """Recently-active transcripts whose session has NO live process.

    Nothing can wake these from outside — the inbox needs a process — but a
    triage list turns "click through the whole sidebar" into "open these
    two". A restart reaches only what was running at the time; sessions an
    idle reaper already killed come back cold.
    """
    if max_age_s is None:
        try:
            max_age_s = float(os.environ.get("SUBFLEET_MUSTER_MAX_AGE_S") or DEFAULT_MUSTER_MAX_AGE_S)
        except ValueError:
            max_age_s = DEFAULT_MUSTER_MAX_AGE_S
    live_ids = {row["session_id"] for row in inbox.live_sessions()}
    projects = paths.claude_dir() / "projects"
    cutoff = time.time() - max_age_s
    rows: list[dict[str, Any]] = []
    try:
        project_dirs = [d for d in projects.iterdir() if d.is_dir()]
    except OSError:
        return rows
    for directory in project_dirs:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            session_id = entry.name[:-6]
            if session_id in live_ids:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            state = turn_state(Path(entry.path))
            if state["state"] != "interrupted":
                continue
            age = state.get("age_s")
            if age is not None and age > max_age_s:
                continue
            rows.append({
                "session_id": session_id,
                "project": directory.name,
                "state": state["state"],
                "detail": state["detail"],
                "age_s": age,
            })
    rows.sort(key=lambda row: row.get("age_s") or 0)
    return rows
