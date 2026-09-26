"""Completion-notice follow-up (notify.py + tickle.py): a delivered push is
not a wake.

Measured 2026-09-06 21:40:10 -04:00 at the ceremony seat (desktop app,
session 29c03102, registry pid 89257): subfleet pushed the notice for run
20260906-212548-gate3-floors-v3-c4 into /tmp/cc-socks/89257.sock, recorded
it ``pushed:true, surfaced:true``, and the seat's transcript holds NO entry
between 2026-09-07T01:34:15Z and 09:43:30Z; the app paused the seat on its
900 s idle timeout at 21:49:15 and nothing in subfleet re-issued the notice
or resumed the session. These tests pin the replacement: a push is never
surfaced at push time; the transcript confirms it; after a grace the
follow-up re-pushes a live silent session once, revives a dead one into a
one-shot host with the notice as the prompt, and the hooks render what is
left.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import cli, liveness, notify, run_ledger, tickle
from subfleet.util import iso, now_local, parse_iso
from test_notify import SESSION, _wait_lines, registry  # noqa: F401  (fixture reuse)
from test_tickle import _entry, _write

SEAT = "29c03102-0afc-452f-a605-14a356d334bc"
RUN = "20260906-212548-gate3-floors-v3-c4"


def _ts(age_s: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")


def _iso_ago(age_s: float) -> str:
    return iso(now_local() - timedelta(seconds=age_s))


def _claude_dir() -> Path:
    return Path(os.environ["SUBFLEET_CLAUDE_DIR"])


def _notice_text(run_id: str = RUN) -> str:
    return (
        f"{notify.notice_signature(run_id)}FINISHED · claude-opus-5 · lane=max@example.com · 14m21s\n"
        "out: /Users/example/lanes/scratch/ceremony-29c03102/gate3-floors-v3-c4.out.md (3,646 bytes)\n"
        "first line: The report is closed. Everything below was measured in this sitting.\n"
        f"ledger: subfleet runs show {run_id}\n"
        "Automated completion notice for a run this session dispatched with `subfleet run`. "
        "Read the output file and continue; no reply is needed."
    )


def _record_notice(session: str, run_id: str = RUN, *, pushed: bool = True, age_s: float = 600,
                   pid: int = 89257, **extra) -> dict:
    """A notice row as notify.on_finish writes it (pushed but never surfaced)."""
    at = _iso_ago(age_s)
    if pushed:
        push = {"delivered": True, "session_id": session, "at": at, "pid": pid,
                "socket": f"/tmp/cc-socks/{pid}.sock", "name": "social-security-model-e8", "mode_class": "bypass"}
    else:
        push = {"delivered": False, "session_id": session, "at": at, "reason": "session-not-running"}
    row = {"run_id": run_id, "session_id": session, "ts": at, "rc": 0, "text": _notice_text(run_id),
           "pushed": pushed, "push": push, "surfaced": False, "surfaced_at": None, **extra}
    notify.append_notice(session, row)
    return row


def _inbox_entry(text: str, age_s: float, uuid: str | None = None) -> dict:
    """How the harness records a push that opened a turn (isMeta user entry)."""
    body = "Another Claude session sent a message:\n" + notify.envelope(text, mode_class="bypass")
    return _entry("user", body, uuid=uuid or f"m-{age_s}", age_s=age_s, isMeta=True, promptSource="sdk")


def _seat_transcript(session: str, *, last_turn_age_s: float = 960, extra=()) -> Path:
    """The seat's tail as measured: a completed assistant text turn, then nothing."""
    rows = [
        _entry("user", "status?", uuid="u1", age_s=last_turn_age_s + 30,
               permissionMode="bypassPermissions", cwd="/work/seat"),
        _entry("assistant", [{"type": "text", "text": "Status at 21:33 EDT, all read from disk and the live panes."}],
               uuid="a1", age_s=last_turn_age_s),
        *extra,
    ]
    return _write(_claude_dir() / "projects" / "-Users-max" / f"{session}.jsonl", rows)


def _dead_registry(session: str, *, pid: int = 999999, cwd: str = "/work/seat") -> None:
    """A registry row whose pid is gone: the app paused the seat."""
    sessions = _claude_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session, "cwd": cwd, "name": "social-security-model-e8",
        "startedAt": 1787500000000, "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock", "kind": "interactive",
    }))


def _store(tmp_path: Path, monkeypatch, session: str, *, cwd: str, mode: str = "bypassPermissions",
           model: str | None = "claude-opus-5") -> None:
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    directory = store / "acct" / "org"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"local_{session}.json").write_text(json.dumps({
        "cliSessionId": session, "cwd": cwd, "permissionMode": mode, "model": model,
    }))


class _Proc:
    pid = 777


def _fake_popen(calls: list):
    def popen(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return _Proc()
    return popen


def _probe(lane: str = "max@lane.ai", token: str = "lane-token"):
    return lambda models, **kwargs: (lane, token, models[0])


def _dead_seat(tmp_path: Path, monkeypatch, *, mode: str = "bypassPermissions", age_s: float = 600) -> Path:
    path = _seat_transcript(SEAT)
    _dead_registry(SEAT, cwd=str(tmp_path))
    _store(tmp_path, monkeypatch, SEAT, cwd=str(tmp_path), mode=mode)
    _record_notice(SEAT, age_s=age_s)
    return path


# --------------------------------------------------------------------------
# What the transcript shows
# --------------------------------------------------------------------------

def test_push_evidence_reads_the_notice_from_any_entry_after_the_push(tmp_path):
    text = _notice_text()
    push_at = now_local() - timedelta(seconds=600)
    rows = [
        # before the push: a tool result quoting the same signature (`subfleet
        # notices` output) is outside the window and proves nothing
        _entry("user", [{"type": "tool_result", "tool_use_id": "t0", "content": text}], uuid="r0", age_s=1200),
        _entry("assistant", [{"type": "text", "text": "noted"}], uuid="a0", age_s=1190),
        # the app's restart stub after the push is not a turn
        {"type": "user", "isMeta": True, "uuid": "stub-u", "timestamp": _ts(500),
         "message": {"role": "user", "content": [{"type": "text", "text": tickle.RESUME_STUB_USER}]}},
        {"type": "assistant", "uuid": "stub-a", "timestamp": _ts(500),
         "message": {"role": "assistant", "content": [{"type": "text", "text": tickle.RESUME_STUB_ASSISTANT}]}},
    ]
    path = _write(_claude_dir() / "projects" / "-Users-max" / f"{SEAT}.jsonl", rows)
    nothing = notify.push_evidence(path, RUN, push_at)
    assert nothing == {"landed_at": None, "turn_at": None, "entries": 4, "complete": True}
    assert notify.push_evidence(None, RUN, push_at)["landed_at"] is None
    assert notify.push_evidence(tmp_path / "missing.jsonl", RUN, push_at)["entries"] == 0

    # a busy session: the harness queues the message (queue-operation) and
    # attaches it to the next turn — landed at the enqueue, then a turn
    enqueue, attach, turn = _ts(599), _ts(598), _ts(590)
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "queue-operation", "timestamp": enqueue, "operation": "enqueue",
                                 "content": notify.envelope(text, mode_class="bypass")}) + "\n")
        stream.write(json.dumps({"type": "attachment", "timestamp": attach,
                                 "attachment": {"type": "queued_command", "prompt": notify.envelope(text, mode_class="bypass")}}) + "\n")
        stream.write(json.dumps(_entry("assistant", [{"type": "text", "text": "reading the report"}], uuid="a2", age_s=590)) + "\n")
    busy = notify.push_evidence(path, RUN, push_at)
    assert busy["landed_at"] == iso(parse_iso(enqueue)) and busy["turn_at"] == iso(parse_iso(turn))

    # an idle session: the message opens a turn and is written as a user entry
    idle = _write(_claude_dir() / "projects" / "-Users-max" / f"{SESSION}.jsonl",
                  rows[:2] + [_inbox_entry(text, 599), _entry("assistant", "on it", uuid="a3", age_s=595)])
    seen = notify.push_evidence(idle, RUN, push_at)
    assert seen["landed_at"] and seen["turn_at"] and parse_iso(seen["turn_at"]) > parse_iso(seen["landed_at"])

    # the seat's own case: a turn eight hours later (Max typed "update") is
    # a turn after the push, not the notice landing
    answer = _entry("assistant", "checking", uuid="a9", age_s=90)
    typed = _write(_claude_dir() / "projects" / "-Users-max" / f"{SESSION}.jsonl",
                   rows[:2] + [_entry("user", "update", uuid="u9", age_s=100), answer])
    late = notify.push_evidence(typed, RUN, push_at)
    assert late["landed_at"] is None and late["turn_at"] == iso(parse_iso(answer["timestamp"]))

    # a sibling run's notice (…-c4-verify) never confirms …-c4, and vice versa
    sibling = _write(_claude_dir() / "projects" / "-Users-max" / f"{SESSION}.jsonl",
                     rows[:2] + [_inbox_entry(_notice_text(RUN + "-verify"), 599)])
    assert notify.push_evidence(sibling, RUN, push_at)["landed_at"] is None
    assert notify.push_evidence(sibling, RUN + "-verify", push_at)["landed_at"]
    assert notify.notice_signature(RUN) == f"subfleet: run {RUN} "
    assert notify.format_notice({"id": RUN, "rc": 0}).startswith(notify.notice_signature(RUN))


def test_confirm_surfaced_reads_the_transcript_once_per_change(tmp_path, monkeypatch):
    path = _seat_transcript(SEAT)
    _record_notice(SEAT, age_s=600)
    scans = []
    real = notify.push_evidence
    monkeypatch.setattr(notify, "push_evidence", lambda *args, **kwargs: scans.append(1) or real(*args, **kwargs))
    assert notify.confirm_surfaced(SEAT, path) == {} and len(scans) == 1
    [row] = notify.unresolved_notices(SEAT)
    assert row["followup"]["checked"]["size"] == path.stat().st_size
    assert notify.confirm_surfaced(SEAT, path) == {} and len(scans) == 1, "unchanged transcript: no re-read"
    with path.open("a") as stream:
        stream.write(json.dumps(_inbox_entry(_notice_text(), 1)) + "\n")
    confirmed = notify.confirm_surfaced(SEAT, path)
    assert list(confirmed) == [RUN] and len(scans) == 2
    [row] = notify._read_notices(notify.notices_path(SEAT))
    assert row["surfaced"] is True and row["surfaced_by"] == "transcript"
    assert row["surfaced_at"] == confirmed[RUN] == row["followup"]["landed_at"]
    assert notify.unresolved_notices(SEAT) == [] and notify.sessions_with_unresolved() == []


def test_sessions_with_unresolved_reads_legacy_rows_by_file_name():
    legacy = "aaaaaaaa-1111-4111-8111-111111111111"
    notify.append_notice(legacy, {"run_id": "old", "ts": _iso_ago(100), "text": "x", "pushed": False, "surfaced": False})
    _record_notice(SEAT, "new", age_s=100)
    notify.append_notice("bbbbbbbb-2222-4222-8222-222222222222",
                         {"run_id": "seen", "ts": _iso_ago(100), "text": "z", "pushed": True, "surfaced": True})
    assert set(notify.sessions_with_unresolved()) == {legacy, SEAT}


# --------------------------------------------------------------------------
# The hooks
# --------------------------------------------------------------------------

def test_hook_renders_parked_and_lost_pushes_but_not_fresh_or_landed_ones(capsys):
    path = _seat_transcript(SEAT)
    _record_notice(SEAT, "20260906-1-parked", pushed=False, age_s=30)
    _record_notice(SEAT, "20260906-2-fresh", age_s=30)
    _record_notice(SEAT, "20260906-3-lost", age_s=900)
    landed = _record_notice(SEAT, "20260906-4-landed", age_s=900)
    with path.open("a") as stream:
        stream.write(json.dumps(_inbox_entry(landed["text"], 899)) + "\n")
    rows = notify.notices_for_hook(SEAT, "user-prompt", path)
    assert [row["run_id"] for row in rows] == ["20260906-1-parked", "20260906-3-lost"]
    states = {row["run_id"]: row for row in notify._read_notices(notify.notices_path(SEAT))}
    assert states["20260906-4-landed"]["surfaced"] and states["20260906-4-landed"]["surfaced_by"] == "transcript"
    # a restart renders every unconfirmed push (the inbox that accepted it
    # died with the process); a compaction is the same process, gated like a prompt
    assert [row["run_id"] for row in notify.notices_for_hook(SEAT, "session-start", path)] == [
        "20260906-1-parked", "20260906-2-fresh", "20260906-3-lost"]
    assert [row["run_id"] for row in notify.notices_for_hook(SEAT, "session-start", path, source="compact")] == [
        "20260906-1-parked", "20260906-3-lost"]
    # a push marked LOST by the follow-up is rendered at once, whatever its age
    notify.update_notices(SEAT, ["20260906-2-fresh"], lambda row: row.setdefault("followup", {}).__setitem__("lost", {"at": _iso_ago(1)}))
    assert [row["run_id"] for row in notify.notices_for_hook(SEAT, "user-prompt", path)] == [
        "20260906-1-parked", "20260906-2-fresh", "20260906-3-lost"]
    notify.update_notices(SEAT, ["20260906-2-fresh"], lambda row: row["followup"].pop("lost"))

    # through the CLI hook: rendered once, marked by the hook, quiet next time
    sys.stdin = io.StringIO(json.dumps({"session_id": SEAT, "hook_event_name": "UserPromptSubmit",
                                        "transcript_path": str(path), "prompt": "update"}))
    try:
        assert cli.main(["_session-hook", "user-prompt"]) == 0
    finally:
        sys.stdin = sys.__stdin__
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "20260906-1-parked" in context and "20260906-3-lost" in context and "20260906-2-fresh" not in context
    assert "pushed into this session's inbox" in context and "no trace in the transcript since" in context
    states = {row["run_id"]: row for row in notify._read_notices(notify.notices_path(SEAT))}
    assert states["20260906-3-lost"]["surfaced_by"] == "hook:user-prompt"
    assert states["20260906-1-parked"]["surfaced"] and not states["20260906-2-fresh"]["surfaced"]
    assert notify.notices_for_hook(SEAT, "user-prompt", path) == []
    assert notify.render_pending(SEAT, []) == ""


# --------------------------------------------------------------------------
# The follow-up: live and silent → one re-push, then LOST
# --------------------------------------------------------------------------

def test_followup_repushes_a_live_silent_session_once_then_marks_it_lost(registry):
    path = _seat_transcript(SESSION)
    _record_notice(SESSION, age_s=600, pid=registry["pid"])
    first = tickle.notice_followup(SESSION, grace_s=300)
    assert first["live"] is True and first["due"] == [RUN] and first["pushed"] == [RUN]
    assert first["again"] is True and first["revive"] is None and first["lost"] == []
    lines = _wait_lines(registry["inbox"], 2)
    assert lines[0] == {"type": "auth", "token": "peer-secret"}
    assert notify.notice_signature(RUN) in lines[1]["message"]["content"]
    [row] = notify.unresolved_notices(SESSION)
    assert len(notify.delivery_attempts(row)) == 2 and row["followup"]["pushes"][0]["delivered"] is True
    # inside the new grace nothing more happens
    quiet = tickle.notice_followup(SESSION, grace_s=300)
    assert quiet["due"] == [] and quiet["waiting"] == [RUN] and quiet["again"] is False
    assert len(registry["inbox"].lines) == 2
    # the re-push left no trace either: LOST, no third push, the ledger word follows
    later = tickle.notice_followup(SESSION, grace_s=300, now=now_local() + timedelta(seconds=400))
    assert later["lost"] == [RUN] and later["pushed"] == [] and len(registry["inbox"].lines) == 2
    [row] = notify.unresolved_notices(SESSION)
    assert row["followup"]["lost"]["at"] and "2 accepted push(es)" in row["followup"]["lost"]["detail"]
    assert run_ledger._notify_state({"caller": {}, "finished_at": "t", "notify": {
        "pushed": True, "surfaced": False, "followup": row["followup"]}}) == "lost"
    # the hooks now render it at the next prompt, and a later pass repeats the verdict without pushing
    assert [r["run_id"] for r in notify.notices_for_hook(SESSION, "user-prompt", path)] == [RUN]
    assert tickle.notice_followup(SESSION, grace_s=300, now=now_local() + timedelta(seconds=900))["lost"] == [RUN]
    assert len(registry["inbox"].lines) == 2
    # once the transcript shows the notice (it rode along Max's next turn), it is confirmed
    with path.open("a") as stream:
        stream.write(json.dumps(_inbox_entry(_notice_text(), 1)) + "\n")
    done = tickle.notice_followup(SESSION, grace_s=300)
    assert done["confirmed"] == [RUN] and done["due"] == [] and notify.unresolved_notices(SESSION) == []


def test_followup_pushes_a_parked_notice_once_the_session_is_back(registry):
    """No inbox at finish time, a live one now (the SessionStart hook that
    normally renders it did not run, or the notice was parked after it)."""
    _seat_transcript(SESSION)
    _record_notice(SESSION, pushed=False, age_s=600)
    result = tickle.notice_followup(SESSION, grace_s=300)
    assert result["pushed"] == [RUN] and result["again"] is True
    assert notify.notice_signature(RUN) in _wait_lines(registry["inbox"], 2)[1]["message"]["content"]
    [row] = notify.unresolved_notices(SESSION)
    assert row["pushed"] is False and len(notify.delivery_attempts(row)) == 1
    assert notify.delivery_anchor(row) == parse_iso(row["followup"]["pushes"][0]["at"])


def test_a_failed_repush_counts_toward_the_cap(registry, monkeypatch):
    _seat_transcript(SESSION)
    _record_notice(SESSION, age_s=600, pid=registry["pid"])
    monkeypatch.setattr(notify, "push_to_session", lambda *a, **k: {"delivered": False, "reason": "send-failed: boom", "at": _iso_ago(0)})
    first = tickle.notice_followup(SESSION, grace_s=300)
    assert first["pushed"] == [] and first["waiting"] == [RUN] and first["again"] is False
    second = tickle.notice_followup(SESSION, grace_s=300)
    assert second["lost"] == [RUN], "an accepted push plus one failed attempt: nothing is pushed at forever"
    assert len(notify.unresolved_notices(SESSION)[0]["followup"]["pushes"]) == 1


# --------------------------------------------------------------------------
# The follow-up: no live process → the notice rides a one-shot host
# --------------------------------------------------------------------------

def test_followup_revives_a_dead_session_with_the_notice_as_the_prompt(tmp_path, monkeypatch):
    """The 2026-09-06 21:40 case, five minutes on: no live registry pid, the
    push left no trace, so the existing revive machinery's print host resumes
    the session with the notice itself as the prompt."""
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda runner=None: {})
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/opt/claude")
    # the worker inherits the lane runner's environment, which still carries
    # the DISPATCHING session's identity — none of it may reach the host
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-dispatching-session")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "metered-must-not-leak")
    path = _dead_seat(tmp_path, monkeypatch)
    notice = notify.unresolved_notices(SEAT)[0]
    calls = []
    result = tickle.notice_followup(SEAT, grace_s=300, popen=_fake_popen(calls), probe=_probe())
    assert result["live"] is False and result["due"] == [RUN] and result["pushed"] == []
    revive = result["revive"]
    assert revive["revived"] is True and revive["pid"] == 777 and revive["host"] == "print"
    assert revive["lane"] == "max@lane.ai" and revive["run_ids"] == [RUN]
    assert revive["model"] == "claude-opus-5", "the session's own model from the store; no cross-tier fallback"
    [(cmd, kwargs)] = calls
    assert cmd[:4] == ["/opt/claude", "-p", "--resume", SEAT]
    assert cmd[-3:] == ["--model", "claude-opus-5", "--dangerously-skip-permissions"]
    prompt = cmd[4]
    assert prompt.startswith(tickle.NOTICE_HOST_MARKER) and prompt.startswith("subfleet:")
    assert notice["text"] in prompt
    assert f"pushed into this session's inbox at {notice['push']['at']} (accepted by the socket)" in prompt
    assert "no trace of it in the transcript since" in prompt and "lane max@lane.ai (model claude-opus-5)" in prompt
    assert tickle.CAUSE_UNKNOWN in prompt and tickle.PRINT_HOST_NOTE in prompt
    assert tickle.REVIVE_DECISION_GUARD in prompt and tickle.REVIVE_REARM_NOTE in prompt
    assert "usage limit" not in prompt and "account switch" not in prompt
    assert kwargs["cwd"] == str(tmp_path) and kwargs["start_new_session"] is True
    env = kwargs["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "lane-token"
    assert not {"CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "ANTHROPIC_API_KEY"} & set(env)
    [row] = notify.unresolved_notices(SEAT)
    launched = row["followup"]["revive"]
    assert launched["pid"] == 777 and launched["lane"] == "max@lane.ai" and launched["host"] == "print"
    record = tickle.load_record(SEAT)
    assert "revive_pending" not in record, "no second 'cut off' nudge framed on top of the prompt"
    last = record["history"][-1]
    assert last["revive"] is True and last["notice"] == [RUN] and last["host"] == "print" and last["pid"] == 777
    assert tickle.recent_revives(record), "the launch counts toward the loop guard"
    assert run_ledger._notify_state({"caller": {}, "finished_at": "t", "notify": {
        "pushed": True, "surfaced": False, "followup": row["followup"]}}) == "revived"
    # a second decision inside the cooldown launches nothing
    again = tickle.notice_followup(SEAT, grace_s=300, now=now_local() + timedelta(seconds=400),
                                   popen=_fake_popen(calls), probe=_probe())
    assert again["revive"]["revived"] is False and "cooldown" in again["revive"]["skip"] and len(calls) == 1
    # the one-shot host wrote its prompt into the transcript: confirmed, resolved
    with path.open("a") as stream:
        stream.write(json.dumps(_entry("user", prompt, uuid="p1", age_s=1, promptSource="sdk", entrypoint="sdk-cli")) + "\n")
    done = tickle.notice_followup(SEAT, grace_s=300, popen=_fake_popen(calls), probe=_probe())
    assert done["confirmed"] == [RUN] and done["due"] == [] and notify.unresolved_notices(SEAT) == []
    assert len(calls) == 1


def test_followup_batches_every_due_notice_into_one_host(tmp_path, monkeypatch):
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda runner=None: {})
    _dead_seat(tmp_path, monkeypatch)
    _record_notice(SEAT, "20260906-1-also", pushed=False, age_s=700)
    _record_notice(SEAT, "20260907-2-fresh", age_s=10)  # inside the grace: not this time
    calls = []
    result = tickle.notice_followup(SEAT, grace_s=300, popen=_fake_popen(calls), probe=_probe())
    assert result["due"] == [RUN, "20260906-1-also"] and result["waiting"] == ["20260907-2-fresh"]
    assert result["revive"]["run_ids"] == [RUN, "20260906-1-also"]
    prompt = calls[0][0][4]
    assert "2 completion notices" in prompt and _notice_text() in prompt and _notice_text("20260906-1-also") in prompt
    assert "finished at" in prompt and "no live inbox to push to (session-not-running)" in prompt
    states = {row["run_id"]: row for row in notify.unresolved_notices(SEAT)}
    assert states[RUN]["followup"]["revive"]["pid"] == 777 and states["20260906-1-also"]["followup"]["revive"]["pid"] == 777
    assert "revive" not in states["20260907-2-fresh"].get("followup", {}), "not due: no host launched for it"


def test_followup_revive_guards(tmp_path, monkeypatch):
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    hosts: dict = {}
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda runner=None: dict(hosts))
    calls: list = []

    def decide(**kwargs):
        probe = kwargs.pop("probe", _probe())
        return tickle.notice_followup(SEAT, grace_s=300, popen=_fake_popen(calls), probe=probe, **kwargs)

    _dead_seat(tmp_path, monkeypatch, mode="default")
    assert "permission mode default" in decide()["revive"]["skip"]
    _store(tmp_path, monkeypatch, SEAT, cwd=str(tmp_path))
    hosts[SEAT] = [4242]
    assert "already running for it (pid [4242])" in decide()["revive"]["skip"]
    hosts.clear()
    assert decide(probe=lambda models, **kw: None)["revive"]["skip"] == "parked: no lane serves claude-opus-5"

    def too_old(models, **kw):
        raise tickle.ClaudeCliTooOld("Claude Code 2.1.87 does not support this model")
    assert "claude CLI too old" in decide(probe=too_old)["revive"]["skip"]
    tickle.retire_session(SEAT, "replaced")
    assert "retired" in decide()["revive"]["skip"]
    tickle.save_record(SEAT, {"history": [
        {"at": iso(now_local() - timedelta(minutes=30 + i)), "revive": True, "pid": i, "host": "print"} for i in range(4)]})
    assert "revive loop guard: 4 launches" in decide()["revive"]["skip"]
    tickle.save_record(SEAT, {})
    lock = tickle._acquire_revive_lock()
    try:
        assert "another revive pass is running" in decide()["revive"]["skip"]
    finally:
        tickle._release_revive_lock(lock)
    plan = decide(dry_run=True)["revive"]
    assert plan["would_revive"] is True and plan["model"] == "claude-opus-5" and plan["revived"] is False
    assert calls == [], "nothing launched by any guard or the dry run"
    # a lane session (headless first prompt, or a run subfleet launched) is
    # neither pushed at nor a revive target — the 2026-09-07 06:08 pass
    # tried to push into lane 8e44ce31 and reported the refusal as LOST
    _write(_claude_dir() / "projects" / "-Users-max" / f"{SEAT}.jsonl", [
        _entry("user", "# lane brief", uuid="l1", age_s=900, promptSource="sdk", entrypoint="sdk-cli"),
        _entry("assistant", "done", uuid="l2", age_s=890),
    ])
    skipped = decide()
    assert "headless lane run" in skipped["skip"] and skipped["revive"] is None and skipped["due"] == []
    assert "skipped — headless lane run" in tickle.format_followup([skipped])
    _seat_transcript(SEAT)
    assert "skip" not in decide(lane_ids={SEAT}) or "headless lane run" in decide(lane_ids={SEAT})["skip"]
    assert decide(lane_ids=set())["revive"]["revived"] is True and len(calls) == 1
    calls.clear()
    # disabled: the pass does nothing at all
    monkeypatch.setenv("SUBFLEET_NOTICE_FOLLOWUP", "off")
    assert tickle.notice_followup_pass() == [] and tickle.spawn_followup(SEAT) is None
    assert calls == []


def test_old_notices_are_left_to_the_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda runner=None: {})
    path = _dead_seat(tmp_path, monkeypatch, age_s=13 * 3600)
    calls: list = []
    result = tickle.notice_followup(SEAT, grace_s=300, popen=_fake_popen(calls), probe=_probe())
    assert result["ignored"] == [RUN] and result["due"] == [] and result["revive"] is None
    assert [row["run_id"] for row in notify.notices_for_hook(SEAT, "user-prompt", path)] == [RUN]
    monkeypatch.setenv("SUBFLEET_NOTICE_FOLLOWUP_MAX_AGE_H", "24")
    result = tickle.notice_followup(SEAT, grace_s=300, popen=_fake_popen(calls), probe=_probe())
    assert result["ignored"] == [] and result["revive"]["revived"] is True and len(calls) == 1


# --------------------------------------------------------------------------
# Plumbing: the worker, its spawn, the cadence backstop, the listing
# --------------------------------------------------------------------------

def test_worker_checks_again_only_after_a_push_and_the_cli_routes_it(monkeypatch, capsys):
    outcomes = iter([{"again": True}, {"again": False}, {"again": True}, {"again": True}, {"again": True}])
    seen: list = []
    monkeypatch.setattr(tickle, "notice_followup", lambda sid, **kw: seen.append((sid, kw.get("grace_s"))) or next(outcomes))
    slept: list = []
    results = tickle.followup_worker(SEAT, delay_s=7, sleep=slept.append)
    assert len(results) == 2 and slept == [7, 7] and seen == [(SEAT, 7), (SEAT, 7)]
    # rounds are capped even when every round pushes; a zero delay never sleeps
    results = tickle.followup_worker(SEAT, delay_s=0, rounds=3, sleep=slept.append)
    assert len(results) == 3 and slept == [7, 7]
    monkeypatch.setattr(tickle, "notice_followup", lambda sid, **kw: {"session_id": sid, "again": False})
    assert cli.main(["_notice-followup", "--session", SEAT, "--delay", "0", "--rounds", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"session_id": SEAT, "again": False}]


def test_spawn_followup_is_detached_and_honours_the_off_switch(tmp_path, monkeypatch):
    log = tmp_path / "spawn.log"
    fake = tmp_path / "fake-subfleet"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" > "{log}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("SUBFLEET_NOTICE_FOLLOWUP", "on")
    assert tickle.spawn_followup(SEAT, delay_s=7, executable=str(fake))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not log.exists():
        time.sleep(0.05)
    assert log.read_text().strip() == f"_notice-followup --session {SEAT} --delay 7"
    log.unlink()
    assert tickle.spawn_followup(SEAT, executable=str(fake))
    while time.monotonic() < deadline and not log.exists():
        time.sleep(0.05)
    assert log.read_text().strip() == f"_notice-followup --session {SEAT}"
    monkeypatch.setenv("SUBFLEET_NOTICE_FOLLOWUP", "off")
    log.unlink()
    assert tickle.spawn_followup(SEAT, executable=str(fake)) is None
    time.sleep(0.2)
    assert not log.exists()


def test_revive_pass_runs_the_followup_and_reports_it(monkeypatch, capsys):
    monkeypatch.setenv("SUBFLEET_NOTICE_FOLLOWUP", "on")
    monkeypatch.setattr(tickle, "auto_revive", lambda **kw: [])
    monkeypatch.setattr(liveness, "run", lambda **kw: {"alerts_sent": [], "recovered": [], "cold": [], "due": [], "census_ok": True})
    _record_notice(SEAT, age_s=600)
    seen: list = []
    monkeypatch.setattr(tickle, "notice_followup", lambda sid, **kw: seen.append((sid, kw.get("dry_run"))) or {
        "session_id": sid, "confirmed": [], "pushed": [RUN], "lost": [],
        "revive": {"revived": False, "skip": "parked: no lane serves claude-opus-5", "run_ids": [RUN]}})
    assert cli.main(["revive"]) == 0
    out = capsys.readouterr().out
    assert "nothing cold to revive" in out and "subfleet notices: follow-up" in out
    assert f"re-pushed {RUN}" in out and "not revived — parked: no lane serves claude-opus-5" in out
    assert seen == [(SEAT, False)]
    assert cli.main(["revive", "--dry-run"]) == 0 and seen[-1] == (SEAT, True)
    # one broken session does not stop the pass, and never takes the revive pass down
    def boom(sid, **kw):
        raise RuntimeError("transcript exploded")
    monkeypatch.setattr(tickle, "notice_followup", boom)
    assert tickle.notice_followup_pass() == [{"session_id": SEAT, "error": "RuntimeError: transcript exploded"}]
    assert cli.main(["revive"]) == 0 and "follow-up failed: RuntimeError: transcript exploded" in capsys.readouterr().out
    assert tickle.format_followup([{"session_id": SEAT, "confirmed": [], "pushed": [], "lost": [], "revive": None}]) == ""


def test_notices_command_shows_where_each_notice_stands(capsys):
    _record_notice(SEAT, "r-pushed", age_s=60)
    _record_notice(SEAT, "r-parked", pushed=False, age_s=60)
    _record_notice(SEAT, "r-lost", age_s=900, followup={"pushes": [{"delivered": True, "at": _iso_ago(500)}],
                                                       "lost": {"at": _iso_ago(100)}})
    _record_notice(SEAT, "r-revived", age_s=900,
                   followup={"revive": {"at": _iso_ago(100), "pid": 5, "lane": "l@x", "model": "m", "host": "print"}})
    _record_notice(SEAT, "r-landed", age_s=900, surfaced=True, surfaced_at=_iso_ago(890), surfaced_by="transcript")
    assert cli.main(["notices", "--session", SEAT, "--json"]) == 0
    rows = {row["run_id"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["r-pushed"]["state"] == "pushed ×1, unconfirmed" and rows["r-pushed"]["pushes"] == 1
    assert rows["r-parked"]["state"] == "parked (session-not-running)"
    assert rows["r-lost"]["state"] == "LOST (hooks render it)" and rows["r-lost"]["pushes"] == 2
    assert rows["r-revived"]["state"] == "revived pid=5 lane=l@x"
    assert "r-landed" not in rows
    assert cli.main(["notices", "--all"]) == 0
    table = capsys.readouterr().out
    assert "r-landed" in table and " landed" in table and SEAT[:8] in table
    assert cli.main(["notices", "--session", "nobody"]) == 0 and "nothing unresolved" in capsys.readouterr().out


def test_print_host_strips_the_launcher_identity(monkeypatch):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/opt/claude")
    for key in tickle._SESSION_ENV:
        monkeypatch.setenv(key, "leak")
    calls: list = []
    assert tickle._launch_print_host(SEAT, "/tmp", "tok", bypass=True, model="m", prompt="p", popen=_fake_popen(calls)) == 777
    env = calls[0][1]["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and not set(tickle._SESSION_ENV) & set(env)
