"""Resume nudges: wake a restarted Claude session whose last turn was cut off.

A Claude account switch (or an app relaunch) restarts every open session. The
conversation comes back, but a session that was mid-turn just sits there until
Max opens it and types "." — one session at a time. The SessionStart hook
already runs inside every restarted session; from there we can read the
session's own transcript, decide whether its last turn was interrupted, and
push a *continue* message into the session's inbox (notify.py), which starts
a turn exactly like the "." would.

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
session — a hidden user line "Continue from where you left off." and a fake
assistant reply "No response requested." with the same timestamp (observed
four times in one session on 2026-08-23, ~0.7 s after the new process
started). That stub makes an interrupted transcript look completed, so the
classifier skips it and judges the turn underneath; each stub has its own
uuid, so every restart earns one nudge.

Guards: off switch (``SUBFLEET_TICKLE=off``); only SessionStart sources
``startup``/``resume`` (never ``compact`` or ``clear``); an age cap on the
interruption (``SUBFLEET_TICKLE_MAX_AGE_S``, default 8h — an old abandoned
turn is not resumed just because the app reopened its tab); one nudge per
interruption point (the transcript uuid is remembered) and a per-session
cooldown; and a re-check after the startup delay, so a session that already
continued (Max typed ".") is not nudged on top of it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import capacity, notify, paths
from .util import atomic_write_json, iso, load_json, now_local, parse_iso

MARKER = "subfleet: this session restarted"
RESUME_STUB_USER = "Continue from where you left off."
RESUME_STUB_ASSISTANT = "No response requested."
DEFAULT_MAX_AGE_S = 8 * 3600
# Dedupe already blocks the same interruption point, so the cooldown only
# absorbs restart storms; Max's team→personal double sign-in restarts twice
# minutes apart and each hop deserves its nudge.
DEFAULT_COOLDOWN_S = 90
DEFAULT_DELAY_S = 8.0
DEFAULT_MUSTER_MAX_AGE_S = 2 * 3600
MUSTER_MARKER = "subfleet muster: roll call"
_TAIL_BYTES = 512 * 1024
_SCAN_MAX = 64 * 1024 * 1024
ALLOWED_SOURCES = {"startup", "resume"}
# Claude Code refuses a model newer than itself: "API Error: 400 Claude Code
# 2.1.87 does not support this model; version 2.1.251 or newer is required".
CLI_TOO_OLD = re.compile(
    r"Claude Code [0-9.]+ does not support this model"
    r"(?:; version [0-9.]+ or newer is required)?",
    re.IGNORECASE,
)


class ClaudeCliTooOld(RuntimeError):
    """The Claude binary on this host cannot request the model at all.

    A host fault, not a capacity one: walking more lanes cannot help, and
    reporting it as "no lane serves <model>" parks sessions indefinitely
    (observed 2026-09-02: the launchd revive job ran a Homebrew cask at 2.1.87
    while ~/.local/bin/claude was 2.1.258).
    """


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
    main_entries = 0
    assistant_turns = 0
    for line in _lines_reversed(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") not in {"user", "assistant"}:
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        main_entries += 1
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
                # provider-error banner ("You've reached your Fable 5 limit…",
                # "You've hit your session limit · resets …"): written as an
                # assistant entry but not a model turn either
                limit_banner = True
                continue
            assistant_turns += 1
        if last is None:
            last = entry
        if main_entries >= 12 or (last is not None and assistant_turns > 0):
            break
    result["main_entries"] = main_entries
    result["assistant_turns"] = assistant_turns
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
            # trailing assistant text with a 429/limit banner above it: the
            # session was still making requests when the limit hit (the text
            # was mid-work narration, or the auto-continue itself was
            # rejected). Err toward resuming — a genuinely finished session
            # answers a nudge with one cheap "nothing pending" turn, while a
            # missed resume strands real work (Max's 2026-08-23 sign-in).
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
    this morning was not orphaned by the switch Max just made.
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
    push = notify.push_to_session(session_id, message(state), from_name="subfleet")
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
    for session in notify.live_sessions(include_lanes=True):
        transcript = notify.transcript_path(session["session_id"])
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
    push = notify.push_to_session(session_id, body, from_name="subfleet")
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


from .lanes import (  # noqa: E402  (re-exported for callers and tests)
    HEADLESS_ENTRYPOINT, HEADLESS_PROMPT_SOURCE, headless_transcript, lane_session_ids,
)

def retire_session(session_id: str, reason: str) -> None:
    """Mark a session as deliberately retired: never listed cold, never
    revived. Used for replaced orchestrators — killing their tmux leaves an
    'interrupted' transcript that the sweep would otherwise resurrect
    headlessly (observed 2026-09-04 11:5x, pid 4808 on dca16909)."""
    record = load_record(session_id)
    record["retired"] = {"at": iso(now_local()), "reason": reason}
    save_record(session_id, record)


def retired(session_id: str) -> bool:
    return bool(load_record(session_id).get("retired"))


def cold_sessions(*, max_age_s: float | None = None) -> list[dict[str, Any]]:
    """Recently-active transcripts whose session has NO live process.

    Nothing can wake these from outside — the inbox needs a process — but a
    triage list turns "click through the whole sidebar" into "open these
    two". A morning account switch restarts only what was running at switch
    time; sessions the overnight idle reaper already killed come back cold
    (observed 2026-08-24: two live processes where last night's hot-fleet
    switch restarted fourteen).
    """
    if max_age_s is None:
        try:
            max_age_s = float(os.environ.get("SUBFLEET_MUSTER_MAX_AGE_S") or DEFAULT_MUSTER_MAX_AGE_S)
        except ValueError:
            max_age_s = DEFAULT_MUSTER_MAX_AGE_S
    live_ids = {row["session_id"] for row in notify.live_sessions(include_lanes=True)}
    lane_ids = lane_session_ids()
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
            if session_id in live_ids or session_id in lane_ids:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            if headless_transcript(Path(entry.path)) or retired(session_id):
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


# --------------------------------------------------------------------------
# Revive: start a cold session's own continuation headlessly
# --------------------------------------------------------------------------

PROBE_PROMPT = "Reply with exactly: ok"
REVIVE_MESSAGE = (
    "subfleet: this session was cut off (usage limit or account switch) and its "
    "process died; you are on a fresh account now. Continue where you left off. "
    "Before redoing anything: `git log --oneline -5` in your worktree and "
    "`subfleet runs --mine` — detached runs survived and may be finished. "
    "EXCEPTION — if your last message asked Max a question or offered him a "
    "decision (a design gate, a freeze, a go/no-go), do NOT proceed past it: "
    "re-state the open question in one line and stop; the interruption was not "
    "his answer. (automated resume from subfleet revive; no reply needed)"
)
_PROBE_CACHE_TTL_S = 600.0
DEFAULT_REVIVE_MODELS = ("claude-fable-5-1", "claude-opus-5")


def revive_enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("SUBFLEET_REVIVE") or "on").strip().lower() not in {"off", "0", "false", "no"}


def _subfleet_bin() -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / "subfleet")


def _lane_token(email: str) -> str | None:
    secret = os.environ.get("SUBFLEET_AGENT_SECRET") or str(Path.home() / "bin" / "agent-secret")
    try:
        out = subprocess.run([secret, "get", f"claude-quota-{email}"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    token = out.stdout.strip()
    return token if out.returncode == 0 and token else None


def _ranked_lanes(model: str | None = None) -> list[str]:
    command = [_subfleet_bin(), "pick", "claude", "--json", "--all"]
    if model:
        command.extend(["--model", model])
    try:
        out = subprocess.run(command,
                             capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout)
        return [row["email"] for row in data.get("ranked", []) if row.get("email")]
    except Exception:
        return []


def _probe_cache_path() -> Path:
    return paths.state_dir() / "revive-lane.json"


def _probe_cache_entries(data: Any) -> dict[str, dict[str, Any]]:
    """Read the model-keyed cache, including the original one-entry shape."""
    if not isinstance(data, dict):
        return {}
    entries = data.get("models")
    if isinstance(entries, dict):
        return {
            model: entry
            for model, entry in entries.items()
            if isinstance(model, str) and isinstance(entry, dict)
        }
    if isinstance(data.get("model"), str) and data.get("email"):
        return {
            data["model"]: {
                "email": data["email"],
                "at": data.get("at"),
            },
        }
    return {}


def probe_lane(model: str = "claude-fable-5-1", *, runner=subprocess.run,
               now_fn=time.time) -> tuple[str, str] | None:
    """(email, token) for a lane that can actually serve ``model`` right now.

    The lane ledger's estimates lie (observed 2026-08-25: three "healthy" lanes
    were out of fable), so the only honest check is a 2-second live probe per
    lane, walking the picker's ranking. The winner is cached for 10 minutes;
    a failed use clears the cache (clear_lane_cache).
    """
    model = capacity.normalize_claude_model(model) or model
    cached = load_json(_probe_cache_path())
    cache_entries = _probe_cache_entries(cached)
    cached_model = cache_entries.get(model)
    if cached_model is not None:
        try:
            fresh = now_fn() - float(cached_model.get("at") or 0) < _PROBE_CACHE_TTL_S
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            token = _lane_token(cached_model.get("email") or "")
            if token:
                return cached_model["email"], token
    for email in _ranked_lanes(model):
        token = _lane_token(email)
        if not token:
            continue
        env = dict(os.environ)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        claude = paths.claude_bin()
        try:
            out = runner([claude, "-p", PROBE_PROMPT, "--model", model,
                          "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
                         capture_output=True, text=True, timeout=120, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        too_old = CLI_TOO_OLD.search(
            f"{getattr(out, 'stdout', '') or ''}\n{getattr(out, 'stderr', '') or ''}"
        )
        if too_old:
            raise ClaudeCliTooOld(
                f"{too_old.group(0)} — {claude} cannot request {model}; update the "
                "Claude Code that path resolves to (`claude update` maintains "
                "~/.local/bin/claude; a Homebrew cask is not updated by it)"
            )
        if out.returncode == 0 and out.stdout.strip().endswith("ok"):
            cache_entries[model] = {"email": email, "at": now_fn()}
            atomic_write_json(_probe_cache_path(), {"models": cache_entries})
            return email, token
    return None


def revive_models(env: dict[str, str] | None = None) -> list[str]:
    """Configured model preference order for cold-session revives."""
    env = os.environ if env is None else env
    raw = env.get("SUBFLEET_REVIVE_MODELS")
    values = raw.split(",") if raw and raw.strip() else DEFAULT_REVIVE_MODELS
    models: list[str] = []
    for value in values:
        # Retired pins (claude-fable-5) normalize onto the current id.
        model = capacity.normalize_claude_model(value.strip()) or ""
        if model and model not in models:
            models.append(model)
    return models or list(DEFAULT_REVIVE_MODELS)


def revive_model_preferences(model: str | None = None, *, allow_fallback: bool = True,
                             env: dict[str, str] | None = None) -> list[str]:
    """Resolve an optional explicit model against the configured fallback list."""
    configured = revive_models(env)
    preferred = (capacity.normalize_claude_model(model) or "") if isinstance(model, str) else ""
    if preferred:
        if not allow_fallback:
            return [preferred]
        return [preferred, *(item for item in configured if item != preferred)]
    return configured if allow_fallback else configured[:1]


def probe_lane_any(models: Sequence[str] | None = None, *, runner=None,
                   now_fn=None) -> tuple[str, str, str] | None:
    """Return the first live ``(email, token, model)`` in preference order."""
    preferences = revive_models() if models is None else ([models] if isinstance(models, str) else models)
    probe_kwargs = {}
    if runner is not None:
        probe_kwargs["runner"] = runner
    if now_fn is not None:
        probe_kwargs["now_fn"] = now_fn
    seen: set[str] = set()
    for value in preferences:
        model = capacity.normalize_claude_model(value.strip()) or ""
        if not model or model in seen:
            continue
        seen.add(model)
        lane = probe_lane(model=model, **probe_kwargs)
        if lane is not None:
            return lane[0], lane[1], model
    return None


def clear_lane_cache() -> None:
    try:
        _probe_cache_path().unlink()
    except OSError:
        pass


def _session_meta_from_store(cli_id: str) -> dict[str, Any]:
    """cwd, permissionMode, and model for a session, from any account store copy."""
    base = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    override = os.environ.get("SUBFLEET_SESSION_STORE")
    if override:
        base = Path(override)
    try:
        for path in base.glob(f"*/*/local_*.json"):
            data = load_json(path)
            if isinstance(data, dict) and data.get("cliSessionId") == cli_id and data.get("cwd"):
                model = data.get("model")
                return {"cwd": data["cwd"], "mode": data.get("permissionMode"),
                        "model": model if isinstance(model, str) and model.strip() else None}
    except OSError:
        pass
    transcript = notify.transcript_path(cli_id)
    if transcript:
        try:
            for line in transcript.open(encoding="utf-8", errors="replace"):
                entry = json.loads(line)
                if entry.get("cwd"):
                    return {"cwd": entry["cwd"], "mode": None}
        except (OSError, ValueError):
            pass
    return {}


def revive_session(cli_id: str, cwd: str, token: str, *, bypass: bool,
                   model: str | None = None,
                   popen=subprocess.Popen) -> int | None:
    """Launch the session's own continuation detached (setsid); returns pid."""
    log_dir = paths.state_dir() / "revive"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    cmd = [paths.claude_bin(), "-p", "--resume", cli_id, REVIVE_MESSAGE]
    if model is not None:
        cmd += ["--model", model]
    if bypass:
        cmd.append("--dangerously-skip-permissions")
    try:
        with (log_dir / f"{cli_id}.log").open("ab") as log:
            proc = popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True, env=env)
    except OSError as exc:
        print(f"subfleet revive: launch failed for {cli_id[:8]}: {exc}", file=sys.stderr)
        return None
    return proc.pid


def live_revive_sessions(*, runner=subprocess.run) -> dict[str, list[int]] | None:
    """Detached subfleet resume session IDs and PIDs currently running.

    A real subfleet revive is re-parented to PID 1 and carries the ``subfleet:``
    message marker in argv. Requiring both excludes the user's desktop sessions,
    which remain children of the Claude app, as well as unrelated resume runs.
    ``None`` means the process census failed and callers must fail closed.
    """
    try:
        out = runner(["ps", "-Ao", "pid=,ppid=,command="],
                     capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    live: dict[str, list[int]] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue
        argv = parts[2:]
        if ppid != 1 or "--resume" not in argv or "subfleet:" not in " ".join(argv):
            continue
        resume_at = argv.index("--resume")
        if resume_at + 1 >= len(argv):
            continue
        cli_id = argv[resume_at + 1]
        if len(cli_id) != 36 or cli_id.count("-") != 4:
            continue
        pids = live.setdefault(cli_id, [])
        if pid not in pids:
            pids.append(pid)
    return live


def _acquire_revive_lock():
    """Serialize census + dedupe + launch; return the held stream or ``None``."""
    lock_path = paths.state_dir() / "revive.lock"
    stream = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = lock_path.open("a")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if stream is not None:
            stream.close()
        return None
    return stream


def _release_revive_lock(stream) -> None:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def auto_revive(*, max_batch: int = 8, min_age_s: float = 120.0,
                dry_run: bool = False, model: str | None = None,
                allow_fallback: bool = True) -> list[dict[str, Any]]:
    """Revive every cold interrupted session once per interruption point.

    Guards: SUBFLEET_REVIVE=off kills it; a session younger than ``min_age_s``
    is left to the app's own restart; non-bypass sessions are skipped (a
    headless -p run would auto-deny their tools); one revive per stuck point
    (the dedupe key is remembered as ``revived``); at most ``max_batch``
    detached revives run concurrently; and the preferred models are live-probed
    in order before anything launches.
    """
    if not revive_enabled():
        return [{"skip": "disabled (SUBFLEET_REVIVE=off)"}]
    lock = _acquire_revive_lock()
    if lock is None:
        return [{"skip": "another revive pass is running or the revive lock is unavailable"}]
    try:
        return _auto_revive_pass(
            max_batch=max_batch, min_age_s=min_age_s, dry_run=dry_run,
            model=model, allow_fallback=allow_fallback,
        )
    finally:
        _release_revive_lock(lock)


def _last_assistant_model(transcript: Path | None) -> str | None:
    """The model that actually served the session's last real assistant turn.

    Fallback identity for sessions whose store entry records no model. Skips
    the app's synthetic entries (limit banners, resume stubs carry
    model "<synthetic>").
    """
    if transcript is None:
        return None
    try:
        for raw in _lines_reversed(Path(transcript)):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            model = (entry.get("message") or {}).get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                return model
    except OSError:
        pass
    return None


def _auto_revive_pass(*, max_batch: int, min_age_s: float, dry_run: bool,
                      model: str | None, allow_fallback: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # Cross-tier fallback is an EXPLICIT choice, never automatic (Max 8/26:
    # fable-grade sessions must not silently resume on a lesser tier — a
    # Microcosm-dynamics session on Opus is worse than a parked one). Without
    # --model, each session revives on its own recorded model; a session whose
    # store entry records none gets the configured default tier only.
    explicit = isinstance(model, str) and bool(model.strip())
    configured = revive_model_preferences(model, allow_fallback=allow_fallback)
    default_tier = configured[0]
    lanes: dict[tuple[str, ...], tuple[str, str, str] | None] = {}
    cli_fault: dict[tuple[str, ...], str] = {}
    live = live_revive_sessions()
    if live is None:
        return [{"skip": "could not count live detached revives"}]
    lane_ids = lane_session_ids()
    launched = 0
    for row in cold_sessions():
        if len(live) + launched >= max_batch:
            results.append({"session_id": row["session_id"], "skip": "batch cap reached"})
            continue
        cli_id = row["session_id"]
        age = row.get("age_s")
        outcome: dict[str, Any] = {"session_id": cli_id, "detail": row.get("detail")}
        if cli_id in live:
            outcome["skip"] = "already running as a detached revive"
            results.append(outcome)
            continue
        if age is not None and age < min_age_s:
            outcome["skip"] = f"only {int(age)}s old; the app may still restart it"
            results.append(outcome)
            continue
        record = load_record(cli_id)
        state = turn_state(notify.transcript_path(cli_id))
        key = dedupe_key(state)
        if record.get("revived") and record["revived"] == key:
            outcome["skip"] = "already revived at this point"
            results.append(outcome)
            continue
        if state.get("assistant_turns", 0) == 0:
            # a one-shot husk (e.g. our own lane probes, or a -p that never
            # answered): there is no conversation to resume
            outcome["skip"] = "no assistant history — one-shot session, not resumable work"
            results.append(outcome)
            continue
        if PROBE_PROMPT in (state.get("detail") or ""):
            outcome["skip"] = "subfleet's own lane probe"
            results.append(outcome)
            continue
        if retired(cli_id):
            outcome["skip"] = "retired by the operator — never revived"
            results.append(outcome)
            continue
        if cli_id in lane_ids or headless_transcript(notify.transcript_path(cli_id)):
            # 2026-09-04: the sweep revived five dead `claude -p` lane runs as
            # untracked continuations on lane tokens (no run id, no -o) —
            # a lane's continuation has no reader; it only burns a window.
            outcome["skip"] = "headless lane run (claude -p) — not resumable work"
            results.append(outcome)
            continue
        meta = _session_meta_from_store(cli_id)
        if not meta.get("cwd"):
            outcome["skip"] = "no cwd found (store + transcript)"
            results.append(outcome)
            continue
        mode = meta.get("mode")
        if mode is None:
            transcript = notify.transcript_path(cli_id)
            mode = notify._last_permission_mode(transcript) if transcript else None
        if mode != "bypassPermissions":
            outcome["skip"] = f"permission mode {mode or 'unknown'} — headless run would deny its tools"
            results.append(outcome)
            continue
        if explicit:
            targets = tuple(configured)
        else:
            own = meta.get("model") or _last_assistant_model(notify.transcript_path(cli_id))
            # A session last served by a retired pin (claude-fable-5) revives
            # on the current id of that tier, never on the retired model.
            own = capacity.normalize_claude_model(own) if own else None
            targets = (own or default_tier,)
        if dry_run:
            outcome["would_revive"] = True
            outcome["models"] = list(targets)
            results.append(outcome)
            continue
        if targets not in lanes:
            try:
                lanes[targets] = probe_lane_any(list(targets))
            except ClaudeCliTooOld as exc:
                # host fault: remembered per target tuple so no further lane
                # is probed for it this pass, and never labelled as capacity
                cli_fault[targets] = str(exc)
                lanes[targets] = None
        lane = lanes[targets]
        if lane is None:
            fault = cli_fault.get(targets)
            outcome["skip"] = (
                f"claude CLI too old for {' or '.join(targets)}: {fault}"
                if fault else f"parked: no lane serves {' or '.join(targets)}"
            )
            results.append(outcome)
            continue
        # Probing every lane/model may take minutes. Refresh the census under
        # the pass lock before each launch so the concurrent cap is current.
        refreshed = live_revive_sessions()
        if refreshed is None:
            outcome["skip"] = "could not refresh live detached revives"
            results.append(outcome)
            break
        live = refreshed
        if cli_id in live:
            outcome["skip"] = "already running as a detached revive"
            results.append(outcome)
            continue
        if len(live) + launched >= max_batch:
            outcome["skip"] = "batch cap reached"
            results.append(outcome)
            continue
        pid = revive_session(cli_id, meta["cwd"], lane[1], bypass=True, model=lane[2])
        outcome.update({"revived": bool(pid), "pid": pid, "lane": lane[0], "model": lane[2]})
        if pid:
            launched += 1
            history = [h for h in (record.get("history") or []) if isinstance(h, dict)][-19:]
            history.append({"at": iso(now_local()), "revive": True, "pid": pid,
                            "lane": lane[0], "model": lane[2], "uuid": key})
            save_record(cli_id, {**record, "session_id": cli_id, "revived": key,
                                 "revived_at": iso(now_local()), "history": history})
        results.append(outcome)
    return results
