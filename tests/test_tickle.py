"""Resume nudges for sessions restarted mid-turn (subfleet/tickle.py)."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import cli, tickle
from test_inbox import SESSION, FakeInbox, _wait_lines, registry, session_env  # noqa: F401  (fixture reuse)


def _entry(kind: str, content, *, uuid: str, age_s: float = 300, **extra) -> dict:
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    row = {"type": kind, "uuid": uuid, "timestamp": stamp.isoformat().replace("+00:00", "Z"),
           "message": {"role": kind, "content": content}}
    row.update(extra)
    return row


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


TOOL_USE = [{"type": "text", "text": "running tests"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]
TOOL_RESULT = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]


@pytest.mark.parametrize(("rows", "state", "detail_part"), [
    ([_entry("user", "do x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1")], "interrupted", "tool call never got its result (Bash)"),
    ([_entry("assistant", TOOL_USE, uuid="a1"), _entry("user", TOOL_RESULT, uuid="u2")], "interrupted", "tool result arrived"),
    ([_entry("assistant", "done", uuid="a0"), _entry("user", "please continue with the next file", uuid="u3")], "interrupted", "unanswered prompt: “please continue"),
    ([_entry("user", "do x", uuid="u1"), _entry("assistant", [{"type": "text", "text": "All done."}], uuid="a2")], "completed", "assistant text"),
    ([_entry("user", "do x", uuid="u1"), _entry("assistant", "plain string reply", uuid="a3")], "completed", "assistant text"),
    ([_entry("assistant", "x", uuid="a0"), _entry("user", "[Request interrupted by user for tool use]", uuid="u4")], "stopped", "Esc"),
    ([_entry("assistant", "x", uuid="a0"), _entry("user", tickle.message({"detail": "d"}), uuid="u5")], "tickled", "already a subfleet nudge"),
    # sidechain / meta entries after the real last turn are ignored
    ([_entry("user", "do x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1"),
      _entry("user", "subagent chatter", uuid="s1", isSidechain=True),
      _entry("user", "<command-name>/x</command-name>", uuid="m1", isMeta=True)], "interrupted", "Bash"),
    ([{"type": "summary", "summary": "x"}], "empty", "no user/assistant turns"),
])
def test_turn_state_classifies_the_last_main_chain_entry(tmp_path, rows, state, detail_part):
    path = _write(tmp_path / "t.jsonl", rows)
    result = tickle.turn_state(path)
    assert result["state"] == state, result
    assert detail_part in result["detail"]
    if state != "empty":
        assert result["last_uuid"] == rows[-1]["uuid"] if not rows[-1].get("isSidechain") and not rows[-1].get("isMeta") else True
        # entries are minted at import; a slow full suite ages them, so bound loosely
        assert result["age_s"] is not None and 250 < result["age_s"] < 7200


def test_turn_state_handles_missing_and_huge_transcripts(tmp_path):
    assert tickle.turn_state(None)["state"] == "empty"
    assert tickle.turn_state(tmp_path / "nope.jsonl")["state"] == "empty"
    rows = [_entry("user", "do x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1")]
    path = _write(tmp_path / "big.jsonl", rows)
    with path.open("a") as stream:
        for i in range(400):  # ~2 MB of later sidechain noise, all ignored
            stream.write(json.dumps(_entry("assistant", "x" * 5000, uuid=f"s{i}", isSidechain=True)) + "\n")
    assert tickle.turn_state(path)["state"] == "interrupted"


def test_decide_gates_source_age_dedupe_cooldown_and_switch(tmp_path, monkeypatch):
    path = _write(tmp_path / "t.jsonl", [_entry("user", "x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1")])
    assert tickle.decide(SESSION, path, source="resume")["tickle"] is True
    assert tickle.decide(SESSION, path, source="startup")["tickle"] is True
    for source in ("compact", "clear"):
        verdict = tickle.decide(SESSION, path, source=source)
        assert verdict["tickle"] is False and "not a restart" in verdict["reason"]
    old = _write(tmp_path / "old.jsonl", [_entry("assistant", TOOL_USE, uuid="a9", age_s=9 * 3600)])
    verdict = tickle.decide(SESSION, old, source="resume")
    assert verdict["tickle"] is False and "older than" in verdict["reason"]
    monkeypatch.setenv("SUBFLEET_TICKLE_MAX_AGE_S", "36000")
    assert tickle.decide(SESSION, old, source="resume")["tickle"] is True
    # dedupe on the interruption point, then cooldown
    tickle.save_record(SESSION, {"last_uuid": "a1", "at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
    verdict = tickle.decide(SESSION, path, source="resume")
    assert verdict["tickle"] is False and "already nudged" in verdict["reason"]
    tickle.save_record(SESSION, {"last_uuid": "other", "at": datetime.now(timezone.utc).isoformat()})
    verdict = tickle.decide(SESSION, path, source="resume")
    assert verdict["tickle"] is False and "cooldown" in verdict["reason"]
    assert tickle.decide(SESSION, path, source="resume", force=True)["tickle"] is True
    monkeypatch.setenv("SUBFLEET_TICKLE", "off")
    assert "disabled" in tickle.decide(SESSION, path, source="resume", force=True)["reason"]


def test_deliver_pushes_an_attested_nudge_and_remembers_it(registry, tmp_path):
    claude_dir = registry["claude_dir"]
    projects = claude_dir / "projects" / "-home-user"
    path = _write(projects / f"{SESSION}.jsonl", [
        _entry("user", "x", uuid="u1", permissionMode="bypassPermissions"),
        _entry("assistant", TOOL_USE, uuid="a1"),
    ])
    slept = []
    verdict = tickle.deliver(SESSION, path, delay_s=5.0, sleep=slept.append)
    assert slept == [5.0]
    assert verdict["delivered"] is True, verdict
    lines = _wait_lines(registry["inbox"], 2)
    content = lines[1]["message"]["content"]
    assert content.startswith('<cross-session-message from-name="subfleet" from-mode="bypass">')
    assert tickle.MARKER in content and "Continue where you left off" in content
    assert "subfleet runs --mine" in content
    record = tickle.load_record(SESSION)
    assert record["last_uuid"] == "a1" and record["delivered"] is True and len(record["history"]) == 1
    # the same interruption point is never nudged twice
    again = tickle.deliver(SESSION, path, delay_s=0)
    assert again["delivered"] is False and "already nudged" in again["reason"]
    assert len(registry["inbox"].lines) == 2


def test_deliver_skips_a_session_that_is_still_working(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-home-user"
    path = _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1")])

    def busy_sleep(_seconds):
        # the session appends to its transcript while we wait → it is alive
        with path.open("a") as stream:
            stream.write(json.dumps(_entry("user", TOOL_RESULT, uuid="u2")) + "\n")

    verdict = tickle.deliver(SESSION, path, delay_s=1.0, sleep=busy_sleep)
    assert verdict["delivered"] is False and "active" in verdict["reason"]
    assert registry["inbox"].lines == []
    # manual sweeps also insist on a quiet period
    fresh = _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a2", age_s=10)])
    verdict = tickle.deliver(SESSION, fresh, delay_s=0, min_idle_s=120)
    assert verdict["delivered"] is False and "quiet" in verdict["reason"]


def test_session_start_hook_spawns_the_nudger_only_when_interrupted(tmp_path, monkeypatch, capsys):
    path = _write(tmp_path / "t.jsonl", [_entry("user", "x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1")])
    spawned = []
    monkeypatch.setattr(tickle, "spawn", lambda session_id, transcript, **kw: spawned.append((session_id, str(transcript))) or 4242)

    def run_hook(payload):
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            return cli.main(["_session-hook", "session-start"])
        finally:
            sys.stdin = sys.__stdin__

    assert run_hook({"session_id": SESSION, "transcript_path": str(path), "source": "resume"}) == 0
    assert spawned == [(SESSION, str(path))]
    assert run_hook({"session_id": SESSION, "transcript_path": str(path), "source": "compact"}) == 0
    assert len(spawned) == 1
    done = _write(tmp_path / "done.jsonl", [_entry("assistant", "finished", uuid="a2")])
    assert run_hook({"session_id": SESSION, "transcript_path": str(done), "source": "resume"}) == 0
    assert len(spawned) == 1
    assert capsys.readouterr().out == ""  # no notices → no context


def test_spawn_launches_a_detached_worker(tmp_path):
    fake = tmp_path / "fake-subfleet"
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$SPAWN_LOG"\npython3 -c "import os; print(os.getsid(0))" >> "$SPAWN_LOG"\n')
    fake.chmod(0o755)
    log = tmp_path / "spawn.log"
    os.environ["SPAWN_LOG"] = str(log)
    try:
        pid = tickle.spawn(SESSION, tmp_path / "t.jsonl", delay_s=2.5, executable=str(fake))
        assert isinstance(pid, int) and pid > 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not log.exists() or len(log.read_text().splitlines()) < 2):
            time.sleep(0.05)
        lines = log.read_text().splitlines()
        assert lines[0] == f"_tickle --session {SESSION} --delay 2.5 --transcript {tmp_path / 't.jsonl'}"
        assert int(lines[1]) != os.getsid(0), "the worker must live in its own session"
    finally:
        os.environ.pop("SPAWN_LOG", None)


def test_tickle_command_survey_and_single_session(registry, tmp_path, capsys):
    projects = registry["claude_dir"] / "projects" / "-home-user"
    _write(projects / f"{SESSION}.jsonl", [_entry("user", "x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1", age_s=400)])
    assert cli.main(["tickle", "--dry-run"]) == 0
    table = capsys.readouterr().out
    assert "review-lane" in table and "interrupted" in table and "Bash" in table
    assert cli.main(["tickle", "--session", SESSION, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["tickle"] is True
    assert cli.main(["tickle", "--session", SESSION]) == 0
    assert "nudged review-lane" in capsys.readouterr().out
    assert _wait_lines(registry["inbox"], 2)[1]["type"] == "user"
    assert cli.main(["tickle", "--session", SESSION]) == 1
    assert "already nudged" in capsys.readouterr().out
    assert cli.main(["tickle", "--all", "--dry-run", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["session_id"] == SESSION and rows[0]["state"] == "interrupted"


STUB_USER = {"type": "user", "isMeta": True, "message": {"role": "user", "content": [{"type": "text", "text": "Continue from where you left off."}]}, "uuid": "stub-u1"}


def _stub_assistant(uuid="stub-a1"):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "No response requested."}]}}


def _limit_banner(kind):
    entry = _entry("assistant", [{"type": "text", "text": "You've reached your usage limit. Switch to another model."}], uuid=f"banner-{kind}")
    if kind == "error":
        entry["error"] = "rate_limit"
    elif kind == "quota":
        entry["quotaLimits"] = {"status": "rejected", "rateLimitType": "five_hour"}
    else:
        entry["isApiErrorMessage"] = True
    return entry


@pytest.mark.parametrize("kind", ["error", "quota", "api-error"])
def test_restart_stub_and_limit_banner_are_seen_through(tmp_path, kind):
    """The desktop app's account-switch sequence: work → limit banner → resume
    stub. The turn underneath decides."""
    rows = [_entry("assistant", TOOL_USE, uuid="a1"), _entry("user", TOOL_RESULT, uuid="u2"),
            _limit_banner(kind), STUB_USER, _stub_assistant()]
    state = tickle.turn_state(_write(tmp_path / "t.jsonl", rows))
    assert state["state"] == "interrupted"
    assert state["restart_stubs"] == 1 and state["limit_banner"] is True
    assert "hit a usage limit" in state["detail"] and "resume stub" in state["detail"]
    assert tickle.dedupe_key(state) == "stub-a1"
    text = tickle.message(state)
    assert "hit its usage limit mid-task" in text and "fresh one now" in text
    # trailing assistant text under a limit banner: the session was still
    # making requests when the limit hit — resume it (hedged in the message)
    done = [_entry("assistant", [{"type": "text", "text": "Now let me run the tests."}], uuid="a9"),
            _limit_banner(kind), STUB_USER, _stub_assistant("stub-a2")]
    state = tickle.turn_state(_write(tmp_path / "done.jsonl", done))
    assert state["state"] == "interrupted"
    assert "cut off by a usage limit" in state["detail"]
    # without a banner, trailing assistant text stays completed
    plain = [_entry("assistant", [{"type": "text", "text": "All done."}], uuid="a9"),
             STUB_USER, _stub_assistant("stub-a3")]
    assert tickle.turn_state(_write(tmp_path / "plain.jsonl", plain))["state"] == "completed"


def test_double_hop_restart_renudges_within_minutes(registry, tmp_path):
    """An account switch can restart every session twice within minutes (one
    hop per sign-in); the second hop must not be cooled down."""
    projects = registry["claude_dir"] / "projects" / "-home-user"
    rows = [_entry("assistant", TOOL_USE, uuid="a1"), STUB_USER, _stub_assistant("hop-1")]
    path = _write(projects / f"{SESSION}.jsonl", rows)
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is True
    rows += [STUB_USER, _stub_assistant("hop-2")]
    path = _write(projects / f"{SESSION}.jsonl", rows)
    record = tickle.load_record(SESSION)
    from subfleet.util import iso, now_local
    record["at"] = iso(now_local() - timedelta(seconds=120))  # 2 min ago > 90s cooldown
    tickle.save_record(SESSION, record)
    verdict = tickle.deliver(SESSION, path, delay_s=0)
    assert verdict["delivered"] is True, verdict


def test_each_restart_earns_one_nudge(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-home-user"
    rows = [_entry("assistant", TOOL_USE, uuid="a1"), STUB_USER, _stub_assistant("stub-r1")]
    path = _write(projects / f"{SESSION}.jsonl", rows)
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is True
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is False  # same restart: once
    # the app restarts again around the SAME stuck turn → a new stub uuid → nudge again
    rows2 = rows + [STUB_USER, _stub_assistant("stub-r2")]
    path = _write(projects / f"{SESSION}.jsonl", rows2)
    tickle.save_record(SESSION, {**tickle.load_record(SESSION), "at": "2026-01-01T00:00:00+00:00"})  # clear cooldown
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is True
    assert len([l for l in registry["inbox"].lines if l.get("type") == "user"]) == 2


def test_stub_written_during_the_wait_is_not_activity(registry, tmp_path):
    """The stub lands moments after process start — often inside the nudger's
    wait. Liveness is judged on the real turn, so the stub write must not
    stand down."""
    projects = registry["claude_dir"] / "projects" / "-home-user"
    path = _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1")])

    def stub_lands(_seconds):
        with path.open("a") as stream:
            stream.write(json.dumps(STUB_USER) + "\n")
            stream.write(json.dumps(_stub_assistant()) + "\n")

    verdict = tickle.deliver(SESSION, path, delay_s=1.0, sleep=stub_lands)
    assert verdict["delivered"] is True, verdict


def test_all_sweep_excludes_the_invoking_session(registry, tmp_path, monkeypatch, capsys):
    """A long tool call writes no turns, so the sweeping session can look dead
    to itself. --all must skip self."""
    projects = registry["claude_dir"] / "projects" / "-home-user"
    _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1", age_s=400)])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert cli.main(["tickle", "--all"]) == 0
    out = capsys.readouterr().out
    assert "this session (excluded)" in out
    assert "nudged 0" in out
    assert registry["inbox"].lines == []


def test_muster_calls_completed_recent_sessions_and_dedupes(registry, tmp_path):
    """After a switch where nothing died — turns ended normally in assistant
    text — tickle correctly stays quiet; muster is the roll call."""
    projects = registry["claude_dir"] / "projects" / "-home-user"
    done = _write(projects / f"{SESSION}.jsonl",
                  [_entry("assistant", [{"type": "text", "text": "Shipped."}], uuid="a1", age_s=600)])
    verdict = tickle.muster_eligible(SESSION, done)
    assert verdict["muster"] is True and "completed" in verdict["reason"]
    verdict = tickle.muster_deliver(SESSION, done, sample_s=0)
    assert verdict["delivered"] is True, verdict
    content = _wait_lines(registry["inbox"], 2)[1]["message"]["content"]
    assert tickle.MUSTER_MARKER in content and "standing or pending work" in content
    # dedupe: the same idle point is not called twice
    again = tickle.muster_deliver(SESSION, done, sample_s=0)
    assert again["delivered"] is False and "already called" in again["reason"]
    # a session whose whole recent tail is the roll call itself is "tickled"
    called = _write(projects / f"{SESSION}.jsonl",
                    [_entry("assistant", "x", uuid="a0", age_s=700),
                     _entry("user", tickle.muster_message({}), uuid="u9", age_s=600)])
    assert tickle.turn_state(called)["state"] == "tickled"


def test_muster_windows_and_guards(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-home-user"
    old = _write(projects / f"{SESSION}.jsonl",
                 [_entry("assistant", [{"type": "text", "text": "done"}], uuid="a1", age_s=3 * 3600)])
    verdict = tickle.muster_eligible(SESSION, old)
    assert verdict["muster"] is False and "roll-call window" in verdict["reason"]
    fresh = _write(projects / f"{SESSION}.jsonl",
                   [_entry("assistant", [{"type": "text", "text": "done"}], uuid="a2", age_s=30)])
    verdict = tickle.muster_deliver(SESSION, fresh, sample_s=0)
    assert verdict["delivered"] is False and "quiet" in verdict["reason"]
    esc = _write(projects / f"{SESSION}.jsonl",
                 [_entry("assistant", "x", uuid="a0"),
                  _entry("user", "[Request interrupted by user]", uuid="u1", age_s=600)])
    assert tickle.muster_eligible(SESSION, esc)["muster"] is False
    interrupted = _write(projects / f"{SESSION}.jsonl",
                         [_entry("assistant", TOOL_USE, uuid="a3", age_s=600)])
    verdict = tickle.muster_deliver(SESSION, interrupted, sample_s=0)
    assert verdict["delivered"] is True
    content = registry["inbox"].lines[-1]["message"]["content"]
    assert tickle.MARKER in content  # interrupted sessions get the resume nudge, not the roll call


def test_cold_sessions_lists_interrupted_transcripts_without_a_process(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-home-user"
    cold_id = "cccccccc-dddd-4eee-8fff-000000000001"
    _write(projects / f"{cold_id}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1", age_s=900)])
    _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a2", age_s=900)])  # live → excluded
    done_id = "cccccccc-dddd-4eee-8fff-000000000002"
    _write(projects / f"{done_id}.jsonl", [_entry("assistant", "done", uuid="a3", age_s=900)])  # completed → excluded
    rows = tickle.cold_sessions()
    assert [row["session_id"] for row in rows] == [cold_id]
    assert "Bash" in rows[0]["detail"] and rows[0]["project"] == "-home-user"


def test_hook_defers_dedupe_to_the_worker_on_a_fresh_restart(tmp_path, monkeypatch, capsys):
    """The app writes the new restart's stub AFTER the hook runs, so an
    'already nudged' verdict at hook time can be stale — spawn anyway."""
    path = _write(tmp_path / "t.jsonl", [_entry("assistant", TOOL_USE, uuid="a1"),
                                         STUB_USER, _stub_assistant("old-stub")])
    tickle.save_record(SESSION, {"last_uuid": "old-stub",
                                 "at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
    assert "already nudged" in tickle.decide(SESSION, path, source="resume")["reason"]
    spawned = []
    monkeypatch.setattr(tickle, "spawn", lambda sid, transcript, **kw: spawned.append(sid) or 1)
    sys.stdin = io.StringIO(json.dumps({"session_id": SESSION, "transcript_path": str(path), "source": "resume"}))
    try:
        assert cli.main(["_session-hook", "session-start"]) == 0
    finally:
        sys.stdin = sys.__stdin__
    assert spawned == [SESSION], "the worker re-decides with the fresh transcript"
    record = tickle.load_record(SESSION)
    assert record["history"][-1].get("deferred_to_worker") is True
