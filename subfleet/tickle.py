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

What subfleet does NOT know is WHY a session restarted or died. The nudge
text therefore carries only what the transcript shows (the interruption
classification and, when the last provider response was a limit banner,
that fact) and says so: "cause not determined by subfleet". The 2026-09-05
incident (seat-death forensics, 2026-09-06) had a revived session repeat a
guessed limit-or-account-switch template to Max as the diagnosis when the
real cause was the desktop app's own update quit.

Cold sessions (no live process at all) are revived by ``auto_revive`` into a
PERSISTENT host — a detached tmux session running interactive
``claude --resume`` — so the session stays resumable and the background
tasks it arms survive its first turn. The headless ``claude -p --resume``
one-shot is kept only as a fallback when tmux is unavailable, and a
one-shot's exit after a turn that armed watchers or left detached runs
pending is classified as *needing continuation*, never as "completed".

Completion notices get their own follow-up (``notice_followup``, the section
"a delivered push is not a wake" below): a push the inbox accepted can leave
no trace in the recipient's transcript (measured 2026-09-06 21:40 at a
desktop-app seat, notify.py), so after a grace a live silent session is
re-pushed once and a dead one gets a one-shot host with the notice as its
prompt.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import resource
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import capacity, notify, paths, run_ledger
from .util import atomic_write_json, iso, load_json, now_local, parse_iso, reversed_lines

MARKER = "subfleet: this session restarted"
CAUSE_UNKNOWN = "cause not determined by subfleet"
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
    long run of subagent entries at the tail cannot hide the last main turn.
    (The scanner itself lives in util.reversed_lines; notify.py shares it.)"""
    yield from reversed_lines(path, chunk=chunk, max_bytes=max_bytes)


def _blocks(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _text_of(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text").strip()


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


# Background work a turn can arm, recognised by the harness's own tool-result
# text (the result proves the arm succeeded; a tool_use alone may have
# failed). Measured on this machine's transcripts 2026-09-06: run_in_background
# Bash ("Command running in background with ID"), Agent ("Async agent
# launched"), Monitor, ScheduleWakeup, Workflow.
_ARM_RESULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shell", re.compile(r"Command running in background with ID: ([A-Za-z0-9_-]+)")),
    ("agent", re.compile(r"Async agent launched")),
    ("monitor", re.compile(r"Monitor started \(task ([A-Za-z0-9_-]+)")),
    ("workflow", re.compile(r"Workflow launched in background\. Task ID: ([A-Za-z0-9_-]+)")),
    ("wakeup", re.compile(r"Next wakeup scheduled for ([^\s(]+)")),
)
_AGENT_ID = re.compile(r"agentId: ([A-Za-z0-9_-]+)")
_ARMING_TOOLS = {"Agent", "Task", "Monitor", "Workflow", "ScheduleWakeup"}
_ARM_SCAN_MAX_ENTRIES = 600


def _arm_from_result(block: dict[str, Any]) -> dict[str, Any] | None:
    text = _result_text(block)
    if not text:
        return None
    for kind, pattern in _ARM_RESULT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        ident = match.group(1) if pattern.groups else None
        if kind == "agent":
            agent = _AGENT_ID.search(text)
            ident = agent.group(1) if agent else None
        return {"kind": kind, "id": ident, "tool_use_id": block.get("tool_use_id")}
    return None


def _arming_tool_use(block: dict[str, Any]) -> bool:
    name = str(block.get("name") or "")
    params = block.get("input") if isinstance(block.get("input"), dict) else {}
    if params.get("run_in_background"):
        return True
    if name in {"Agent", "Task"}:
        return params.get("run_in_background") is not False
    if name == "ScheduleWakeup":
        return not params.get("stop")
    return name in _ARMING_TOOLS


def last_turn_arms(transcript: str | Path | None, *,
                   max_entries: int = _ARM_SCAN_MAX_ENTRIES) -> list[dict[str, Any]]:
    """Background tasks, agents, monitors, timers, and workflows the LAST
    turn armed — work whose completion can only ever be delivered into a
    live process of this session.

    Scans the main chain backwards from the tail to the prompt that started
    the turn (a user entry with text, not tool results). Arms are read from
    the harness's own tool-result text; a tool_use with no result at all
    (the process died mid-call) counts too. Sidechain and meta entries are
    ignored, as is anything before the turn boundary: watchers the previous
    process armed were already reported dead by the app's task-notification.
    """
    if not transcript:
        return []
    path = Path(transcript).expanduser()
    if not path.is_file():
        return []
    arms: list[dict[str, Any]] = []
    answered: set[str] = set()
    seen = 0
    for line in _lines_reversed(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") not in {"user", "assistant"}:
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        seen += 1
        blocks = _blocks(entry.get("message"))
        if entry["type"] == "user":
            results = [block for block in blocks if block.get("type") == "tool_result"]
            if not results:
                break  # the prompt that started this turn
            for block in results:
                if isinstance(block.get("tool_use_id"), str):
                    answered.add(block["tool_use_id"])
                arm = _arm_from_result(block)
                if arm is not None:
                    arms.append(arm)
        else:
            for block in blocks:
                if block.get("type") != "tool_use" or not _arming_tool_use(block):
                    continue
                if block.get("id") in answered:
                    continue  # its result decided above
                arms.append({"kind": "tool_use", "id": block.get("id"),
                             "tool": block.get("name"), "unanswered": True})
        if seen >= max_entries:
            break
    arms.reverse()
    return arms


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
            result.update({"state": "completed", "detail": f"last turn ended in assistant text{suffix}",
                           "armed_tasks": last_turn_arms(path)})
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


def pending_runs(session_id: str) -> list[str]:
    """Ids of detached ``subfleet run`` dispatches by this session that are
    still RUNNING (runner alive, not finished). Orphaned rows do not count."""
    try:
        rows = run_ledger.list_runs(50, session_id=session_id, running_only=True)
    except Exception:  # a broken ledger must not break the sweep
        return []
    return [str(row["id"]) for row in rows if row.get("status") == "RUNNING" and row.get("id")]


def _arm_label(arm: dict[str, Any]) -> str:
    kind = arm.get("kind") or "?"
    if kind == "tool_use":
        kind = str(arm.get("tool") or "tool").lower()
    ident = arm.get("id")
    return f"{kind}:{ident}" if ident else str(kind)


def continuation_needed(state: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """Why a *completed* tail still needs a live process: the last turn armed
    background work (tasks, agents, monitors, timers, workflows) whose
    notifications can only land in a live process, or detached runs the
    session dispatched are still pending. ``None`` when nothing is pending.

    This is the 2026-09-05 gap: a one-shot ``claude -p --resume`` revive
    answered one turn, armed four watchers, and exited; the tail read
    "completed", so nothing revived the session for seven hours.
    """
    if state.get("state") != "completed":
        return None
    armed = [arm for arm in (state.get("armed_tasks") or []) if isinstance(arm, dict)]
    runs = pending_runs(session_id)
    if not armed and not runs:
        return None
    parts = []
    if armed:
        labels = ", ".join(_arm_label(arm) for arm in armed[:6])
        more = f", +{len(armed) - 6} more" if len(armed) > 6 else ""
        parts.append(f"{len(armed)} background task(s) armed in its last turn ({labels}{more})")
    if runs:
        listed = ", ".join(runs[:4]) + (f", +{len(runs) - 4} more" if len(runs) > 4 else "")
        parts.append(f"{len(runs)} detached run(s) still pending ({listed})")
    return {
        "armed": armed,
        "runs": runs,
        "detail": ("last turn ended in assistant text, but " + " and ".join(parts)
                   + " — their notifications need a live process"),
    }


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
    record = load_record(session_id)
    pending = revive_pending(record, now)
    if state["state"] != "interrupted" and not (pending and state["state"] == "completed"):
        # A completed tail is left alone — except right after subfleet itself
        # started this process to continue pending work (revive_pending): the
        # tail then reads "completed" only because the previous host exited.
        verdict["reason"] = f"{state['state']}: {state['detail']}"
        return verdict
    if not force:
        age = state.get("age_s")
        if age is not None and age > max_age_s():
            verdict["reason"] = f"interrupted {age}s ago, older than the {int(max_age_s())}s cap"
            return verdict
        if record.get("last_uuid") and record["last_uuid"] == dedupe_key(state):
            verdict["reason"] = "already nudged at this interruption point"
            return verdict
        last_at = parse_iso(record.get("at"))
        if last_at is not None and (now - last_at).total_seconds() < cooldown_s:
            verdict["reason"] = f"nudged {int((now - last_at).total_seconds())}s ago (cooldown {int(cooldown_s)}s)"
            return verdict
    verdict["tickle"] = True
    if pending and state["state"] == "completed":
        verdict["reason"] = f"revived by subfleet: {pending.get('detail') or state['detail']}"
    else:
        verdict["reason"] = f"interrupted: {state['detail']}"
    return verdict


def limit_line(state: dict[str, Any]) -> str:
    """The one cause-shaped line subfleet can back with evidence: the last
    provider response before the cut-off was a usage-limit banner (an
    assistant entry flagged error / isApiErrorMessage / quota rejected).
    Empty when the detector did not fire."""
    if not state.get("limit_banner"):
        return ""
    return (
        "Detected in the transcript: the last provider response before the cut-off "
        "was a usage-limit banner (the previous process was being refused when it stopped).\n"
    )


def message(state: dict[str, Any], revive: dict[str, Any] | None = None) -> str:
    """The resume nudge. Only what subfleet observed goes in: the transcript
    classification (``detail``), the limit banner when that detector fired,
    and — for a subfleet revive — the lane, model, and host it launched.
    The cause of the restart or death is NOT asserted; it is not determined
    by subfleet (the 2026-09-05 template that named a limit or an account
    switch was repeated to Max as diagnosis when the desktop app's update
    had quit)."""
    detail = (revive or {}).get("detail") or state.get("detail") or "its last turn was interrupted"
    if revive:
        lane = revive.get("lane") or "?"
        model = revive.get("model") or "?"
        host = revive.get("host") or "?"
        head = (
            f"{MARKER}: subfleet found no live process for it and started this one "
            f"on lane {lane} (model {model}, {host} host). Its last turn was cut off — "
            f"{detail}; {CAUSE_UNKNOWN}."
        )
    else:
        head = f"{MARKER} with its last turn cut off — {detail}; {CAUSE_UNKNOWN}."
    text = (
        f"{head} Continue where you left off.\n{limit_line(state)}"
        "Before redoing anything: `git log --oneline -5` in your worktree and `subfleet runs --mine` — "
        "detached runs survive restarts and may already be finished (their completion notices arrive separately).\n"
    )
    if revive:
        text += f"{REVIVE_DECISION_GUARD}\n{REVIVE_REARM_NOTE}\n"
        if revive.get("host") == REVIVE_HOST_PRINT:
            text += f"{PRINT_HOST_NOTE}\n"
        return text + "(automated resume from subfleet revive; no reply needed)"
    return text + "(automated resume nudge from subfleet; no reply needed)"


# The sentences a revived session is given, shared by the resume nudge and
# the completion-notice delivery prompt so the two never drift apart.
REVIVE_DECISION_GUARD = (
    "EXCEPTION — if your last message asked Max a question or offered him a "
    "decision (a design gate, a freeze, a go/no-go), do NOT proceed past it: "
    "re-state the open question in one line and stop; the interruption was not "
    "his answer."
)
REVIVE_REARM_NOTE = (
    "Background tasks, monitors, and timers armed by the previous process died "
    "with it; re-arm what you still need."
)
PRINT_HOST_NOTE = (
    "This host is a one-shot (`claude -p`): it exits when this turn ends and "
    "anything armed in the background dies with it. Prefer `subfleet run` for "
    "detached work; subfleet re-issues a continue while work stays pending."
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


def _inbox_ready(session_id: str) -> bool:
    entry = notify.find_session(session_id)
    return bool(entry and entry.get("alive") and entry.get("socket_present"))


def deliver(session_id: str, transcript: str | Path | None, *, delay_s: float = 0.0,
            force: bool = False, min_idle_s: float = 0.0, sleep=time.sleep,
            await_inbox_s: float = 0.0, clock=time.monotonic) -> dict[str, Any]:
    """Wait for the inbox, re-check the transcript, push the nudge, remember it.

    A session that is actually working keeps producing turns; a session cut off
    by a restart does not (the app's resume stub is not a turn). So the real
    last turn must be the same one across the wait (and, for manual sweeps, at
    least ``min_idle_s`` old) before the nudge goes out.

    ``await_inbox_s`` (the revive launcher's deliverer) first waits for the
    session's process to register a live inbox — an interactive
    ``claude --resume`` takes a while to bind it — and gives up, without
    consuming the dedupe key, when none appears in time.
    """
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

    if await_inbox_s > 0:
        deadline = clock() + await_inbox_s
        while not _inbox_ready(session_id):
            if clock() >= deadline:
                return skipped({
                    "session_id": session_id, "tickle": False, "state": turn_state(transcript),
                    "reason": f"no live inbox for {session_id[:8]} within {int(await_inbox_s)}s",
                })
            sleep(1.0)
    before = _fingerprint(transcript)
    if delay_s > 0:
        sleep(delay_s)

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
    record = load_record(session_id)
    pending = revive_pending(record)
    push = notify.push_to_session(session_id, message(state, revive=pending), from_name="subfleet")
    verdict["push"] = push
    verdict["delivered"] = bool(push.get("delivered"))
    record = load_record(session_id)  # re-read: the launcher may have written meanwhile
    history = [item for item in (record.get("history") or []) if isinstance(item, dict)][-19:]
    history.append({"at": iso(now_local()), "uuid": state.get("last_uuid"),
                    "delivered": verdict["delivered"], "reason": push.get("reason"),
                    **({"revive": pending.get("host")} if pending else {})})
    updated = {
        **record,  # keep retired / revived / revive_pending markers intact
        "session_id": session_id,
        "at": iso(now_local()),
        "turn_uuid": state.get("last_uuid"),
        "restart_stubs": state.get("restart_stubs"),
        "delivered": verdict["delivered"],
        "push": push,
        "history": history,
    }
    if verdict["delivered"]:
        # Only a nudge that reached the inbox consumes the interruption point;
        # a failed push (inbox not bound yet) leaves it for the next deliverer.
        updated["last_uuid"] = dedupe_key(state)
        if pending is not None:
            updated.pop("revive_pending", None)
            updated["revive_delivered"] = {**pending, "delivered_at": iso(now_local())}
    save_record(session_id, updated)
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
          executable: str | None = None, await_inbox_s: float = 0.0) -> int | None:
    """Start a detached nudger (the hook must return at once; the inbox binds
    a moment after SessionStart). Returns its pid."""
    launcher = executable or str(Path(__file__).resolve().parent.parent / "bin" / "subfleet")
    cmd = [launcher, "_tickle", "--session", session_id, "--delay", str(delay_s)]
    if await_inbox_s > 0:
        cmd += ["--await-inbox", str(await_inbox_s)]
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
            continuation = None
            if state["state"] == "completed":
                # A completed tail with no process is still cold work when the
                # turn armed watchers or left detached runs pending: nothing
                # can deliver their notifications (the 2026-09-05 one-shot
                # revive exited exactly this way and sat for seven hours).
                continuation = continuation_needed(state, session_id)
                if continuation is None:
                    continue
            elif state["state"] != "interrupted":
                continue
            age = state.get("age_s")
            if age is not None and age > max_age_s:
                continue
            rows.append({
                "session_id": session_id,
                "project": directory.name,
                "state": "needs-continuation" if continuation else state["state"],
                "detail": continuation["detail"] if continuation else state["detail"],
                "age_s": age,
                **({"continuation": continuation} if continuation else {}),
            })
    rows.sort(key=lambda row: row.get("age_s") or 0)
    return rows


# --------------------------------------------------------------------------
# Revive: start a cold session's own continuation in a persistent host
# --------------------------------------------------------------------------

PROBE_PROMPT = "Reply with exactly: ok"
_PROBE_CACHE_TTL_S = 600.0
DEFAULT_REVIVE_MODELS = ("claude-fable-5-1", "claude-opus-5")
# Hosts. tmux (default): a detached tmux session running interactive
# ``claude --resume`` — the session stays resumable, keeps its inbox, and the
# background tasks it arms live as long as it does; Max can attach to it.
# print: the legacy one-shot ``claude -p --resume`` — exits after one turn,
# orphaning whatever that turn armed. Fallback only (no tmux).
REVIVE_HOST_TMUX = "tmux"
REVIVE_HOST_PRINT = "print"
TMUX_SESSION_PREFIX = "subfleet-revive-"
REVIVE_PENDING_TTL_S = 30 * 60
# The launcher's own nudge deliverer: wait this long before the first inbox
# check, then up to this long for the interactive session to bind its inbox.
REVIVE_NUDGE_DELAY_S = 15.0
REVIVE_NUDGE_AWAIT_INBOX_S = 240.0
# Loop guard: a session revived this many times inside the window is parked
# (a host that dies at once, or a one-shot that re-arms watchers every turn,
# would otherwise be relaunched every pass); the liveness alert reports it.
REVIVE_LOOP_WINDOW_S = 2 * 3600
REVIVE_LOOP_MAX = 4
# Persistent hosts live on their OWN tmux server (socket name below), started
# with no config file. 2026-09-06 15:56: the revive job found no default
# server and became it — a launchd child capped at 256 open files — and
# tmux-continuum restored Max's 245 sessions into it; from then on new panes
# failed ("fork failed: Too many open files", then "Device not configured" as
# PTYs ran out) and every revive fell back to the one-shot. A dedicated
# server restores nothing and shares no limit; the census and the stall
# reaper still look at the default server for hosts launched before this.
TMUX_SOCKET = "subfleet"
NOFILE_TARGET = 8192
# An interactive host can block on a TUI dialog no automation answers — seen
# 2026-09-06 21:07 on two hosts whose lane hit its limit ("You've hit your
# limit … 1. Stop and wait for limit to reset … 3. Switch to usage
# credits"), and they sat there for a day looking alive. Such a host is torn
# down, the lane cache cleared, and the session revived again on a lane that
# answers.
STALL_MARKERS = (
    "Stop and wait for limit to reset",
    "Wait here, then continue automatically",
    "Switch to usage credits",
)


def revive_enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("SUBFLEET_REVIVE") or "on").strip().lower() not in {"off", "0", "false", "no"}


def revive_host(env: dict[str, str] | None = None) -> str:
    """Configured host: ``SUBFLEET_REVIVE_HOST`` = tmux (default) | print."""
    env = os.environ if env is None else env
    value = (env.get("SUBFLEET_REVIVE_HOST") or REVIVE_HOST_TMUX).strip().lower()
    return REVIVE_HOST_PRINT if value in {"print", "p", "oneshot", "one-shot"} else REVIVE_HOST_TMUX


def tmux_bin(env: dict[str, str] | None = None) -> str | None:
    """The tmux binary (``SUBFLEET_TMUX`` overrides; tests point it at nothing)."""
    env = os.environ if env is None else env
    explicit = env.get("SUBFLEET_TMUX")
    if explicit:
        return explicit
    found = shutil.which("tmux")
    if found:
        return found
    brew = "/opt/homebrew/bin/tmux"
    return brew if os.access(brew, os.X_OK) else None


def revive_host_script() -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / "subfleet-revive-host")


def tmux_session_name(cli_id: str) -> str:
    return TMUX_SESSION_PREFIX + cli_id


def tmux_socket(env: dict[str, str] | None = None) -> str:
    """The dedicated server's socket name (``SUBFLEET_TMUX_SOCKET``; empty or
    ``default`` means the default server)."""
    env = os.environ if env is None else env
    value = env.get("SUBFLEET_TMUX_SOCKET")
    if value is None:
        return TMUX_SOCKET
    value = value.strip()
    return "" if value.lower() == "default" else value


def _tmux_base(socket: str | None = None) -> list[str] | None:
    """``tmux`` plus the server selector: ``-L <socket> -f /dev/null`` for the
    dedicated server (no config, so no plugin restores anything into it),
    bare for the default server. ``None`` when tmux is missing."""
    tmux = tmux_bin()
    if tmux is None:
        return None
    sock = tmux_socket() if socket is None else socket
    return [tmux, "-L", sock, "-f", "/dev/null"] if sock else [tmux]


def _tmux_servers() -> list[list[str]]:
    """Every server that may hold a revive host: the dedicated one, then the
    default (hosts launched before the dedicated server existed)."""
    servers: list[list[str]] = []
    for socket in (tmux_socket(), ""):
        base = _tmux_base(socket)
        if base is not None and base not in servers:
            servers.append(base)
    return servers


def tmux_attach_hint(cli_id: str, socket: str | None = None) -> str:
    sock = tmux_socket() if socket is None else socket
    selector = f"tmux -L {sock} " if sock else "tmux "
    return f"{selector}attach -t {tmux_session_name(cli_id)}"


def _raise_nofile(target: int = NOFILE_TARGET) -> None:
    """Lift this process's soft open-files limit (inherited by a tmux server
    it starts): launchd children get 256, and a server that ends up holding
    many panes fails to fork new ones at that cap."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft != resource.RLIM_INFINITY and soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ValueError, OSError):
        pass


def revive_pending(record: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """The launch subfleet made for this session whose nudge is still owed
    (written by revive_session, cleared by deliver). Expires after
    REVIVE_PENDING_TTL_S so a stale marker never reframes a later restart."""
    pending = record.get("revive_pending")
    if not isinstance(pending, dict):
        return None
    at = parse_iso(pending.get("at"))
    now = now or now_local()
    if at is None or (now - at).total_seconds() > REVIVE_PENDING_TTL_S:
        return None
    return pending


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


_SESSION_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
)


def _revive_log(cli_id: str) -> Path:
    log_dir = paths.state_dir() / "revive"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return log_dir / f"{cli_id}.log"


def _note_launch(cli_id: str, launch: dict[str, Any]) -> None:
    """Write the launch under ``revive_pending`` so the nudge deliverer (the
    SessionStart hook's worker or the launcher's own) frames its message with
    what was actually done, and clears it once delivered."""
    record = load_record(cli_id)
    record["revive_pending"] = {"at": iso(now_local()), **launch}
    try:
        save_record(cli_id, record)
    except OSError:
        pass


def _launch_tmux_host(cli_id: str, cwd: str, *, email: str, model: str | None,
                      bypass: bool, runner) -> dict[str, Any]:
    """Detached tmux session running the persistent host.

    The pane runs ``bin/subfleet-revive-host``, which fetches the lane token
    itself (``agent-secret get claude-quota-<email>``) and execs interactive
    ``claude --resume``: no token in argv, in the tmux server's environment,
    or in the pane's scrollback. Returns {"ok", "pid", "session", "error"}.
    """
    base = _tmux_base()
    if base is None:
        return {"ok": False, "pid": None, "session": tmux_session_name(cli_id), "error": "tmux not found"}
    name = tmux_session_name(cli_id)
    log = _revive_log(cli_id)
    parts = [revive_host_script(), "--session", cli_id, "--lane", email,
             "--claude", paths.claude_bin(), "--log", str(log)]
    if model is not None:
        parts += ["--model", model]
    if bypass:
        parts.append("--bypass")
    shell = "exec " + " ".join(shlex.quote(part) for part in parts)
    env = dict(os.environ)
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", *_SESSION_ENV):
        env.pop(key, None)
    cmd = [*base, "new-session", "-d", "-s", name, "-c", cwd, "-x", "220", "-y", "60", shell]
    _raise_nofile()
    try:
        out = runner(cmd, capture_output=True, text=True, timeout=30, env=env, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "pid": None, "session": name, "error": f"{exc.__class__.__name__}: {exc}"}
    if out.returncode != 0:
        detail = (getattr(out, "stderr", "") or "").strip()[:200]
        return {"ok": False, "pid": None, "session": name, "error": f"tmux new-session rc={out.returncode} {detail}"}
    pid = None
    for query in ("#{pane_pid}", "#{pid}"):
        try:
            shown = runner([*base, "display-message", "-p", "-t", name, query],
                           capture_output=True, text=True, timeout=15, env=env)
            pid = int((getattr(shown, "stdout", "") or "").strip())
            break
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    if not pid:
        # The host exists but cannot be tracked: take it down rather than
        # leave a ghost the census would miss and the pass would duplicate.
        try:
            runner([*base, "kill-session", "-t", name], capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError):
            pass
        return {"ok": False, "pid": None, "session": name, "error": "tmux session created but its pane pid could not be read"}
    return {"ok": True, "pid": pid, "session": name, "server": tmux_socket(), "error": None}


def _launch_print_host(cli_id: str, cwd: str, token: str, *, bypass: bool,
                       model: str | None, prompt: str, popen) -> int | None:
    """The legacy one-shot: ``claude -p --resume`` detached (setsid).

    The launcher's own session identity is stripped (as the tmux host does):
    the follow-up worker inherits the lane runner's environment, which still
    carries the DISPATCHING session's ``CLAUDE_CODE_SESSION_ID``."""
    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", *_SESSION_ENV):
        env.pop(key, None)
    cmd = [paths.claude_bin(), "-p", "--resume", cli_id, prompt]
    if model is not None:
        cmd += ["--model", model]
    if bypass:
        cmd.append("--dangerously-skip-permissions")
    try:
        with _revive_log(cli_id).open("ab") as log:
            proc = popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True, env=env)
    except OSError as exc:
        print(f"subfleet revive: launch failed for {cli_id[:8]}: {exc}", file=sys.stderr)
        return None
    return proc.pid


def revive_session(cli_id: str, cwd: str, token: str, *, bypass: bool,
                   model: str | None = None, popen=subprocess.Popen,
                   email: str | None = None, state: dict[str, Any] | None = None,
                   host: str | None = None, runner=subprocess.run,
                   prompt: str | None = None, note_pending: bool = True) -> int | None:
    """Launch the session's own continuation; returns the host's pid.

    Host choice (revive_host): tmux unless configured otherwise, the lane
    email is unknown, tmux is missing, or the tmux launch fails — then the
    one-shot print host. Either way the launch (host, lane, model) is noted
    on the session's tickle record as ``revive_pending``; for the tmux host
    the nudge is delivered into the live inbox afterwards, for the print
    host it IS the prompt.

    ``prompt`` replaces the print host's resume nudge (the completion-notice
    follow-up hands it the notice itself); ``note_pending=False`` skips the
    ``revive_pending`` marker, so the SessionStart hook of the new process
    does not frame a second "your last turn was cut off" nudge on top of a
    prompt that already says what happened.
    """
    state = state or {}
    chosen = host or revive_host()
    reasons: list[str] = []
    if chosen == REVIVE_HOST_TMUX:
        if not email:
            reasons.append("no lane email for the tmux host")
        elif tmux_bin() is None:
            reasons.append("tmux not found")
        else:
            launch = _launch_tmux_host(cli_id, cwd, email=email, model=model, bypass=bypass, runner=runner)
            if launch["ok"]:
                if note_pending:
                    _note_launch(cli_id, {"host": REVIVE_HOST_TMUX, "lane": email, "model": model,
                                          "tmux_session": launch["session"], "server": launch.get("server"),
                                          "pid": launch["pid"], "detail": state.get("detail")})
                return launch["pid"]
            reasons.append(launch["error"] or "tmux launch failed")
        print(f"subfleet revive: {cli_id[:8]}: falling back to the one-shot host ({'; '.join(reasons)})",
              file=sys.stderr)
    launch = {"host": REVIVE_HOST_PRINT, "lane": email, "model": model, "detail": state.get("detail")}
    if prompt is None:
        prompt = message(state, revive=launch)
    pid = _launch_print_host(cli_id, cwd, token, bypass=bypass, model=model, prompt=prompt, popen=popen)
    if pid and note_pending:
        _note_launch(cli_id, {**launch, "pid": pid, "fallback": reasons or None})
    return pid


# --------------------------------------------------------------------------
# Completion-notice follow-up: a delivered push is not a wake
# --------------------------------------------------------------------------
#
# notify.on_finish records a push as ``pushed`` but never as ``surfaced``;
# this is what happens next (the seat-wake daemon's rules, folded in:
# pending = a notice nothing has confirmed; live = a registry pid that is
# alive; a one-shot resume only when no live host exists).
#
#   grace passes with no trace of the notice in the transcript, and
#     the session is live  → re-push once (the inbox may have dropped it;
#                            a busy session sees it at its next turn); a
#                            second silence marks the notice LOST, and the
#                            hooks render it at the next prompt or start
#     no live registry pid → the existing revive machinery, print host,
#                            with the notice as the prompt (a desktop-app
#                            seat paused on its idle timeout comes back this
#                            way); one launch per notice, the loop guard and
#                            a cooldown between launches
#
# Two callers: a detached worker spawned when the notice is written
# (``spawn_followup`` → ``subfleet _notice-followup``), for the timely path,
# and every ``subfleet revive`` pass (launchd, two minutes), as the backstop
# that survives reboots. Both are idempotent over the notice rows.

NOTICE_FOLLOWUP_DEFAULT_S = 5 * 60.0   # seat-wake.sh PENDING_MIN
NOTICE_FOLLOWUP_MAX_AGE_H = 12.0       # liveness.DEFAULT_MAX_AGE_H
NOTICE_PUSH_ATTEMPTS = 2               # the finish-time push + one re-push
NOTICE_REVIVE_COOLDOWN_S = 25 * 60.0   # seat-wake.sh COOLDOWN_MIN
NOTICE_WORKER_ROUNDS = 2               # check, (re-push,) check again
NOTICE_HOST_MARKER = "subfleet: completion delivery"  # live_revive_sessions keys on "subfleet:"


def followup_enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("SUBFLEET_NOTICE_FOLLOWUP") or "on").strip().lower() not in {"off", "0", "false", "no"}


def followup_grace_s(env: dict[str, str] | None = None) -> float:
    """How long a push gets to show in the transcript before the follow-up
    acts (``SUBFLEET_NOTICE_FOLLOWUP_S``, default 300)."""
    env = os.environ if env is None else env
    try:
        value = float(env.get("SUBFLEET_NOTICE_FOLLOWUP_S") or NOTICE_FOLLOWUP_DEFAULT_S)
    except ValueError:
        return NOTICE_FOLLOWUP_DEFAULT_S
    return value if value >= 0 else NOTICE_FOLLOWUP_DEFAULT_S


def followup_max_age_s(env: dict[str, str] | None = None) -> float:
    """Notices older than this (``SUBFLEET_NOTICE_FOLLOWUP_MAX_AGE_H``,
    default 12 h) are left to the hooks: the two-minute backstop must not
    chase a session that finished a run yesterday and never came back."""
    env = os.environ if env is None else env
    try:
        hours = float(env.get("SUBFLEET_NOTICE_FOLLOWUP_MAX_AGE_H") or NOTICE_FOLLOWUP_MAX_AGE_H)
    except ValueError:
        hours = NOTICE_FOLLOWUP_MAX_AGE_H
    return (hours if hours > 0 else NOTICE_FOLLOWUP_MAX_AGE_H) * 3600.0


def notice_revive_prompt(rows: Sequence[dict[str, Any]], launch: dict[str, Any], *,
                         now: datetime | None = None) -> str:
    """The one-shot host's prompt: the completion notice(s), framed with only
    what subfleet measured — when each push went out, that the transcript
    showed nothing of it, and that no live process was found now. The cause
    is not asserted (see ``message``)."""
    now = now or now_local()
    lane = launch.get("lane") or "?"
    model = launch.get("model") or "?"
    lines = [
        f"{NOTICE_HOST_MARKER}: subfleet found no live process for this session at "
        f"{iso(now)} while {len(rows)} completion notice{'s' if len(rows) != 1 else ''} for "
        f"detached run{'s' if len(rows) != 1 else ''} it dispatched with `subfleet run` "
        f"{'were' if len(rows) != 1 else 'was'} unacknowledged, so it started this one-shot host "
        f"(`claude -p --resume`) on lane {lane} (model {model}) to deliver "
        f"{'them' if len(rows) != 1 else 'it'}; {CAUSE_UNKNOWN}.",
    ]
    for row in rows:
        push = row.get("push") if isinstance(row.get("push"), dict) else {}
        attempts = notify.delivery_attempts(row)
        if attempts:
            stamps = ", ".join(str(item.get("at")) for item in attempts)
            lines.append(
                f"- run {row.get('run_id')}: pushed into this session's inbox at {stamps} "
                "(accepted by the socket); no trace of it in the transcript since."
            )
        else:
            lines.append(
                f"- run {row.get('run_id')}: finished at {row.get('ts')} with no live inbox to push to "
                f"({push.get('reason') or 'not delivered'})."
            )
    lines.append("")
    for row in rows:
        lines.append(str(row.get("text") or f"{notify.notice_signature(str(row.get('run_id')))}finished"))
        lines.append("")
    lines.append(
        "Read each output file from disk and continue the work it unblocks "
        "(`subfleet runs show <id>` for details).\n"
        f"{REVIVE_DECISION_GUARD}\n"
        f"{REVIVE_REARM_NOTE}\n"
        f"{PRINT_HOST_NOTE}\n"
        "(automated completion delivery from subfleet; no reply needed)"
    )
    return "\n".join(lines)


def _notice_history(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (record.get("history") or []) if isinstance(item, dict)]


def _last_notice_revive_at(record: dict[str, Any]) -> datetime | None:
    stamps = [parse_iso(item.get("at")) for item in _notice_history(record)
              if item.get("revive") and item.get("notice")]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None


def _sync_ledger(row: dict[str, Any]) -> None:
    """Mirror the notice's state onto the run's ledger entry (``subfleet
    runs`` shows pushed / landed / lost / revived). Best effort: the run may
    have been reaped."""
    run_id = row.get("run_id")
    if not isinstance(run_id, str):
        return
    try:
        run_ledger.set_notify(run_id, {
            "pushed": row.get("pushed"), "surfaced": row.get("surfaced"), "at": row.get("ts"),
            "push": row.get("push"), "surfaced_at": row.get("surfaced_at"),
            "surfaced_by": row.get("surfaced_by"), "followup": notify.followup_of(row) or None,
        })
    except Exception:  # noqa: BLE001 - accounting never blocks the follow-up
        pass


def _repush(session_id: str, rows: list[dict[str, Any]], *, now: datetime,
            grace_s: float, dry_run: bool) -> dict[str, list[str]]:
    """The live branch: one re-push per notice, then LOST."""
    outcome: dict[str, list[str]] = {"pushed": [], "lost": [], "waiting": []}
    for row in rows:
        run_id = str(row.get("run_id"))
        anchor = notify.delivery_anchor(row)
        if anchor is not None and (now - anchor).total_seconds() < grace_s:
            outcome["waiting"].append(run_id)
            continue
        followup = notify.followup_of(row)
        if followup.get("lost"):
            outcome["lost"].append(run_id)
            continue
        # Every attempt counts, delivered or not: a session whose registry
        # pid is alive but whose socket is gone must not be pushed at forever.
        attempts = (1 if row.get("pushed") else 0) + len(followup.get("pushes") or [])
        if attempts >= NOTICE_PUSH_ATTEMPTS:
            outcome["lost"].append(run_id)
            if not dry_run:
                def mark_lost(item: dict[str, Any]) -> None:
                    item.setdefault("followup", {})["lost"] = {
                        "at": iso(now),
                        "detail": (f"{len(notify.delivery_attempts(item))} accepted push(es) left no trace "
                                   "in the transcript; the hooks render it at the next prompt or start"),
                    }
                notify.update_notices(session_id, [run_id], mark_lost)
                row.setdefault("followup", {})["lost"] = {"at": iso(now)}
                _sync_ledger(row)
            continue
        if dry_run:
            outcome["pushed"].append(run_id)
            continue
        push = notify.push_to_session(session_id, str(row.get("text") or ""))

        def note_push(item: dict[str, Any], push=push) -> None:
            pushes = item.setdefault("followup", {}).setdefault("pushes", [])
            pushes.append(push)

        notify.update_notices(session_id, [run_id], note_push)
        row.setdefault("followup", {}).setdefault("pushes", []).append(push)
        _sync_ledger(row)
        if push.get("delivered"):
            outcome["pushed"].append(run_id)
        else:
            outcome["waiting"].append(run_id)
    return outcome


def _revive_for_notices(session_id: str, rows: list[dict[str, Any]], *, now: datetime,
                        dry_run: bool, popen, runner, probe) -> dict[str, Any]:
    """The no-live-process branch, with auto_revive's guards: never a retired
    session, only a bypass session (a headless host would deny its tools),
    never on top of a host subfleet already runs for it, never past the loop
    guard or inside the cooldown, and only on a lane that serves the
    session's own model. (Lane sessions were excluded by the caller.)"""
    outcome: dict[str, Any] = {"revived": False, "run_ids": [str(row.get("run_id")) for row in rows]}
    transcript = notify.transcript_path(session_id)
    if retired(session_id):
        outcome["skip"] = "retired by the operator — never revived"
        return outcome
    record = load_record(session_id)
    last = _last_notice_revive_at(record)
    if last is not None and (now - last).total_seconds() < NOTICE_REVIVE_COOLDOWN_S:
        outcome["skip"] = f"a completion-delivery host was launched {int((now - last).total_seconds())}s ago (cooldown {int(NOTICE_REVIVE_COOLDOWN_S)}s)"
        return outcome
    loops = recent_revives(record, now=now)
    if len(loops) >= REVIVE_LOOP_MAX:
        outcome["skip"] = (f"revive loop guard: {len(loops)} launches in the last "
                           f"{REVIVE_LOOP_WINDOW_S // 3600}h — parked for the liveness alert")
        return outcome
    live = live_revive_sessions(runner=runner)
    if live is None:
        outcome["skip"] = "could not count live detached revives"
        return outcome
    if session_id in live:
        outcome["skip"] = f"a subfleet host is already running for it (pid {live[session_id]})"
        return outcome
    meta = _session_meta_from_store(session_id)
    if not meta.get("cwd"):
        outcome["skip"] = "no cwd found (store + transcript)"
        return outcome
    mode = meta.get("mode")
    if mode is None:
        mode = notify._last_permission_mode(transcript) if transcript else None
    if mode != "bypassPermissions":
        outcome["skip"] = f"permission mode {mode or 'unknown'} — headless run would deny its tools"
        return outcome
    own = meta.get("model") or _last_assistant_model(transcript)
    own = capacity.normalize_claude_model(own) if own else None
    model = own or revive_model_preferences(allow_fallback=False)[0]
    outcome["model"] = model
    if dry_run:
        outcome["would_revive"] = True
        return outcome
    try:
        lane = (probe or probe_lane_any)([model])
    except ClaudeCliTooOld as exc:
        outcome["skip"] = f"claude CLI too old for {model}: {exc}"
        return outcome
    if lane is None:
        outcome["skip"] = f"parked: no lane serves {model}"
        return outcome
    email, token, model = lane
    launch = {"host": REVIVE_HOST_PRINT, "lane": email, "model": model}
    prompt = notice_revive_prompt(rows, launch, now=now)
    state = turn_state(transcript)
    pid = revive_session(session_id, meta["cwd"], token, bypass=True, model=model, email=email,
                         state=state, host=REVIVE_HOST_PRINT, prompt=prompt, popen=popen,
                         runner=runner, note_pending=False)
    outcome.update({"revived": bool(pid), "pid": pid, "lane": email, "model": model, "host": REVIVE_HOST_PRINT})
    if not pid:
        outcome["skip"] = "print host launch failed"
        return outcome
    launched = {"at": iso(now), "pid": pid, "lane": email, "model": model, "host": REVIVE_HOST_PRINT}

    def note_revive(item: dict[str, Any]) -> None:
        item.setdefault("followup", {})["revive"] = launched

    notify.update_notices(session_id, outcome["run_ids"], note_revive)
    for row in rows:
        row.setdefault("followup", {})["revive"] = launched
        _sync_ledger(row)
    history = _notice_history(record)[-19:]
    history.append({**launched, "revive": True, "notice": outcome["run_ids"],
                    "uuid": state.get("last_uuid")})
    try:
        save_record(session_id, {**load_record(session_id), "session_id": session_id, "history": history})
    except OSError:
        pass
    return outcome


def notice_followup(session_id: str, *, now: datetime | None = None,
                    grace_s: float | None = None, dry_run: bool = False,
                    popen=subprocess.Popen, runner=subprocess.run,
                    probe=None, lane_ids: set[str] | None = None) -> dict[str, Any]:
    """One follow-up decision for one session's unresolved notices.

    Returns ``{"session_id", "confirmed", "due", "live", "pushed", "lost",
    "waiting", "ignored", "revive", "again"}`` (``skip`` when the session is
    a lane). ``again`` is True when this call pushed something whose landing
    is worth checking after another grace. ``lane_ids`` lets a pass share
    one census of lane sessions instead of re-reading every run's metadata
    per session.
    """
    now = now or now_local()
    grace = followup_grace_s() if grace_s is None else grace_s
    result: dict[str, Any] = {"session_id": session_id, "confirmed": [], "due": [], "live": None,
                              "pushed": [], "lost": [], "waiting": [], "ignored": [],
                              "revive": None, "again": False}
    rows = []
    max_age = followup_max_age_s()
    for row in notify.unresolved_notices(session_id):
        minted = parse_iso(row.get("ts"))
        if minted is not None and (now - minted).total_seconds() > max_age:
            result["ignored"].append(str(row.get("run_id")))
            continue
        rows.append(row)
    if not rows:
        return result
    transcript = notify.transcript_path(session_id)
    if session_id in (lane_session_ids() if lane_ids is None else lane_ids) or headless_transcript(transcript):
        # A lane's deliverable is its last message (lanes.py): its notices
        # are never pushed (push_to_session refuses them) nor carried by a
        # host. Whatever dispatched from inside a lane waits inline.
        result["skip"] = "headless lane run (claude -p) — its notices are neither pushed nor carried by a host"
        return result
    confirmed = notify.confirm_surfaced(session_id, transcript, rows=rows, now=now)
    result["confirmed"] = sorted(confirmed)
    for row in rows:
        if row.get("run_id") in confirmed:
            _sync_ledger({**row, "surfaced": True, "surfaced_at": confirmed[row["run_id"]],
                          "surfaced_by": "transcript"})
    open_rows = [row for row in rows if row.get("run_id") not in confirmed]
    due = []
    for row in open_rows:
        anchor = notify.delivery_anchor(row)
        if anchor is None or (now - anchor).total_seconds() >= grace:
            due.append(row)
        else:
            result["waiting"].append(str(row.get("run_id")))
    result["due"] = [str(row.get("run_id")) for row in due]
    if not due:
        return result
    entry = notify.find_session(session_id)
    live = bool(entry and entry.get("alive"))
    result["live"] = live
    if live:
        outcome = _repush(session_id, due, now=now, grace_s=grace, dry_run=dry_run)
        result["pushed"] = outcome["pushed"]
        result["lost"] = outcome["lost"]
        result["waiting"].extend(outcome["waiting"])
        result["again"] = bool(outcome["pushed"]) and not dry_run
        return result
    lock = _acquire_revive_lock()
    if lock is None:
        result["revive"] = {"revived": False, "skip": "another revive pass is running or the revive lock is unavailable"}
        return result
    try:
        result["revive"] = _revive_for_notices(session_id, due, now=now, dry_run=dry_run,
                                               popen=popen, runner=runner, probe=probe)
    finally:
        _release_revive_lock(lock)
    return result


def notice_followup_pass(*, now: datetime | None = None, dry_run: bool = False,
                         popen=subprocess.Popen, runner=subprocess.run,
                         probe=None) -> list[dict[str, Any]]:
    """Every session with an unresolved notice, one decision each (the
    revive-cadence backstop; ``subfleet revive`` runs it after its own pass)."""
    if not followup_enabled():
        return []
    results = []
    sessions = notify.sessions_with_unresolved()
    lane_ids = lane_session_ids() if sessions else set()
    for session_id in sessions:
        try:
            results.append(notice_followup(session_id, now=now, dry_run=dry_run,
                                           popen=popen, runner=runner, probe=probe, lane_ids=lane_ids))
        except Exception as exc:  # noqa: BLE001 - one broken session must not stop the pass
            results.append({"session_id": session_id, "error": f"{exc.__class__.__name__}: {exc}"})
    return results


def followup_worker(session_id: str, *, delay_s: float | None = None,
                    rounds: int = NOTICE_WORKER_ROUNDS, sleep=time.sleep) -> list[dict[str, Any]]:
    """The detached worker: wait a grace, decide; if that pushed something,
    wait another grace and decide again (did the re-push land?). The
    cadence pass keeps watching after the worker exits."""
    grace = followup_grace_s() if delay_s is None else delay_s
    results = []
    for _ in range(max(1, rounds)):
        if grace > 0:
            sleep(grace)
        result = notice_followup(session_id, grace_s=grace)
        results.append(result)
        if not result.get("again"):
            break
    return results


def spawn_followup(session_id: str, *, delay_s: float | None = None,
                   executable: str | None = None) -> int | None:
    """Start the detached follow-up worker for a freshly written notice.
    Returns its pid, or None when disabled or the spawn failed."""
    if not followup_enabled():
        return None
    launcher = executable or _subfleet_bin()
    cmd = [launcher, "_notice-followup", "--session", session_id]
    if delay_s is not None:
        cmd += ["--delay", str(delay_s)]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        print(f"subfleet notify: follow-up spawn failed: {exc}", file=sys.stderr)
        return None
    return proc.pid


def format_followup(results: Sequence[dict[str, Any]]) -> str:
    """One line per session that had something to decide (quiet otherwise)."""
    lines = []
    for item in results:
        sid = str(item.get("session_id") or "-")[:8]
        if item.get("error"):
            lines.append(f"  {sid}… notice follow-up failed: {item['error']}")
            continue
        parts = []
        if item.get("skip"):
            parts.append(f"skipped — {item['skip']}")
        if item.get("confirmed"):
            parts.append(f"landed {len(item['confirmed'])}")
        if item.get("pushed"):
            parts.append(f"re-pushed {', '.join(item['pushed'])}")
        if item.get("lost"):
            parts.append(f"LOST {', '.join(item['lost'])} (hooks will render)")
        revive = item.get("revive")
        if isinstance(revive, dict):
            if revive.get("revived"):
                parts.append(f"revived pid={revive.get('pid')} host={revive.get('host')} "
                             f"lane={revive.get('lane')} model={revive.get('model')} for {', '.join(revive.get('run_ids') or [])}")
            elif revive.get("would_revive"):
                parts.append(f"would revive (model {revive.get('model')}) for {', '.join(revive.get('run_ids') or [])}")
            else:
                parts.append(f"not revived — {revive.get('skip')}")
        if parts:
            lines.append(f"  {sid}… " + " · ".join(parts))
    if not lines:
        return ""
    return "subfleet notices: follow-up\n" + "\n".join(lines)


def _tmux_session_names(runner=subprocess.run) -> list[tuple[str, list[str]]]:
    """(session name, server base command) over every server that may hold a host."""
    names: list[tuple[str, list[str]]] = []
    env = dict(os.environ)
    for base in _tmux_servers():
        try:
            out = runner([*base, "list-sessions", "-F", "#{session_name}"],
                         capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(out, "returncode", 1) != 0:
            continue
        for line in (getattr(out, "stdout", "") or "").splitlines():
            if line.strip():
                names.append((line.strip(), base))
    return names


def tmux_host_bases(runner=subprocess.run) -> dict[str, list[str]]:
    """Session id → the server base command of its persistent revive host."""
    hosts: dict[str, list[str]] = {}
    for name, base in _tmux_session_names(runner):
        if not name.startswith(TMUX_SESSION_PREFIX):
            continue
        cli_id = name[len(TMUX_SESSION_PREFIX):]
        if len(cli_id) == 36 and cli_id.count("-") == 4 and cli_id not in hosts:
            hosts[cli_id] = base
    return hosts


def tmux_revive_hosts(runner=subprocess.run) -> set[str]:
    """Session ids whose persistent revive host (tmux session) exists."""
    return set(tmux_host_bases(runner))


def stalled_revive_hosts(runner=subprocess.run) -> dict[str, dict[str, Any]]:
    """Hosts whose pane shows a blocking dialog (STALL_MARKERS): alive to
    every census, doing nothing, waiting for a human."""
    stalled: dict[str, dict[str, Any]] = {}
    env = dict(os.environ)
    for cli_id, base in tmux_host_bases(runner).items():
        name = tmux_session_name(cli_id)
        try:
            out = runner([*base, "capture-pane", "-p", "-t", name],
                         capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(out, "returncode", 1) != 0:
            continue
        text = getattr(out, "stdout", "") or ""
        marker = next((mark for mark in STALL_MARKERS if mark in text), None)
        if marker:
            stalled[cli_id] = {"session": name, "server": base, "marker": marker}
    return stalled


def reap_stalled_hosts(*, runner=subprocess.run, dry_run: bool = False,
                       now: datetime | None = None) -> list[dict[str, Any]]:
    """Tear down stalled hosts so their sessions read cold again and the pass
    revives them on a lane that answers: kill the tmux session, clear the
    lane cache (the cached lane is the one that hit its limit), reopen the
    interruption point (``revived`` / ``revive_pending`` are dropped — that
    launch did not take), and remember why in the record."""
    now = now or now_local()
    reaped: list[dict[str, Any]] = []
    env = dict(os.environ)
    for cli_id, info in stalled_revive_hosts(runner).items():
        entry = {"session_id": cli_id, "tmux_session": info["session"], "marker": info["marker"],
                 "reaped": not dry_run}
        if not dry_run:
            try:
                runner([*info["server"], "kill-session", "-t", info["session"]],
                       capture_output=True, text=True, timeout=15, env=env)
            except (OSError, subprocess.SubprocessError) as exc:
                entry["reaped"] = False
                entry["error"] = f"{exc.__class__.__name__}: {exc}"
            record = load_record(cli_id)
            history = [item for item in (record.get("history") or []) if isinstance(item, dict)][-19:]
            history.append({"at": iso(now), "host_stalled": info["marker"], "tmux_session": info["session"],
                            "killed": entry["reaped"]})
            record.pop("revived", None)
            record.pop("revive_pending", None)
            record.update({"session_id": cli_id, "history": history})
            try:
                save_record(cli_id, record)
            except OSError:
                pass
            clear_lane_cache()
        reaped.append(entry)
    return reaped


def live_revive_sessions(*, runner=subprocess.run) -> dict[str, list[int]] | None:
    """Session IDs (with PIDs) currently hosted by a subfleet revive.

    A one-shot host is re-parented to PID 1 and carries the ``subfleet:``
    message marker in argv (excludes desktop sessions, which stay children of
    the app, and unrelated resume runs). A persistent host is a tmux session
    named ``subfleet-revive-<id>``; its ``claude --resume <id>`` process is
    listed when already running (the pane may still be fetching its token).
    ``None`` means the process census failed and callers must fail closed.
    """
    try:
        out = runner(["ps", "-Ao", "pid=,ppid=,command="],
                     capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    processes: list[tuple[int, int, list[str]]] = []
    for line in out.stdout.splitlines():
        parts = line.split()
        try:
            processes.append((int(parts[0]), int(parts[1]), parts[2:]))
        except (ValueError, IndexError):
            continue

    def resumed(argv: list[str]) -> str | None:
        if "--resume" not in argv:
            return None
        at = argv.index("--resume")
        cli_id = argv[at + 1] if at + 1 < len(argv) else ""
        return cli_id if len(cli_id) == 36 and cli_id.count("-") == 4 else None

    live: dict[str, list[int]] = {}
    for pid, ppid, argv in processes:
        cli_id = resumed(argv)
        if cli_id is None or ppid != 1 or "subfleet:" not in " ".join(argv):
            continue
        pids = live.setdefault(cli_id, [])
        if pid not in pids:
            pids.append(pid)
    for cli_id in tmux_revive_hosts(runner):
        pids = live.setdefault(cli_id, [])
        for pid, _ppid, argv in processes:
            if resumed(argv) == cli_id and pid not in pids:
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


def recent_revives(record: dict[str, Any], *, now: datetime | None = None,
                   window_s: float = REVIVE_LOOP_WINDOW_S) -> list[dict[str, Any]]:
    """Revive launches recorded for this session inside the window."""
    now = now or now_local()
    found = []
    for item in record.get("history") or []:
        if not isinstance(item, dict) or not item.get("revive"):
            continue
        at = parse_iso(item.get("at"))
        if at is not None and (now - at).total_seconds() <= window_s:
            found.append(item)
    return found


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
    # A host blocked on a limit dialog is not a live session: take it down
    # first so its session is cold to this very pass.
    for reaped in reap_stalled_hosts(dry_run=dry_run):
        results.append({"session_id": reaped["session_id"], "stalled_host": reaped["marker"],
                        "tmux_session": reaped["tmux_session"], "reaped": reaped["reaped"],
                        **({"error": reaped["error"]} if reaped.get("error") else {})})
    live = live_revive_sessions()
    if live is None:
        return [{"skip": "could not count live detached revives"}]
    # Persistent hosts are ordinary live sessions from here on; only one-shot
    # hosts still in flight occupy a slot of the concurrent-launch cap.
    persistent = tmux_revive_hosts()
    lane_ids = lane_session_ids()
    launched = 0

    def in_flight() -> int:
        return sum(1 for cli_id in live if cli_id not in persistent) + launched

    for row in cold_sessions():
        if in_flight() >= max_batch:
            results.append({"session_id": row["session_id"], "skip": "batch cap reached"})
            continue
        cli_id = row["session_id"]
        age = row.get("age_s")
        outcome: dict[str, Any] = {"session_id": cli_id, "detail": row.get("detail"),
                                   "tail": row.get("state")}
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
        if row.get("continuation"):
            state = {**state, "state": "needs-continuation", "detail": row["detail"],
                     "continuation": row["continuation"]}
        key = dedupe_key(state)
        if record.get("revived") and record["revived"] == key:
            outcome["skip"] = "already revived at this point"
            results.append(outcome)
            continue
        loops = recent_revives(record)
        if len(loops) >= REVIVE_LOOP_MAX:
            outcome["skip"] = (f"revive loop guard: {len(loops)} launches in the last "
                               f"{REVIVE_LOOP_WINDOW_S // 3600}h — parked for the liveness alert")
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
        if in_flight() >= max_batch:
            outcome["skip"] = "batch cap reached"
            results.append(outcome)
            continue
        pid = revive_session(cli_id, meta["cwd"], lane[1], bypass=True, model=lane[2],
                             email=lane[0], state=state)
        # revive_session noted its launch (host, tmux session) on the record.
        record = load_record(cli_id)
        launch = record.get("revive_pending") if isinstance(record.get("revive_pending"), dict) else {}
        host = launch.get("host") or REVIVE_HOST_PRINT
        outcome.update({"revived": bool(pid), "pid": pid, "lane": lane[0], "model": lane[2],
                        "host": host, "tmux_session": launch.get("tmux_session")})
        if pid:
            launched += 1
            history = [h for h in (record.get("history") or []) if isinstance(h, dict)][-19:]
            history.append({"at": iso(now_local()), "revive": True, "pid": pid, "host": host,
                            "lane": lane[0], "model": lane[2], "uuid": key,
                            **({"tmux_session": launch["tmux_session"]} if launch.get("tmux_session") else {})})
            save_record(cli_id, {**record, "session_id": cli_id, "revived": key,
                                 "revived_at": iso(now_local()), "history": history})
            if host == REVIVE_HOST_TMUX:
                # The persistent host takes its nudge through the inbox, once
                # the interactive session has bound it. The SessionStart hook's
                # own worker usually gets there first; this one is the backstop
                # (dedupe keeps the second from double-delivering).
                spawn(cli_id, notify.transcript_path(cli_id), delay_s=REVIVE_NUDGE_DELAY_S,
                      await_inbox_s=REVIVE_NUDGE_AWAIT_INBOX_S)
        results.append(outcome)
    return results
