"""Resume nudges for sessions restarted mid-turn (subfleet/tickle.py)."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import cli, notify, tickle
from test_notify import SESSION, FakeInbox, _wait_lines, registry  # noqa: F401  (fixture reuse)


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
    projects = claude_dir / "projects" / "-Users-max"
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
    projects = registry["claude_dir"] / "projects" / "-Users-max"
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
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    _write(projects / f"{SESSION}.jsonl", [_entry("user", "x", uuid="u1"), _entry("assistant", TOOL_USE, uuid="a1", age_s=400)])
    assert cli.main(["tickle", "--dry-run"]) == 0
    table = capsys.readouterr().out
    assert "tariff-lane" in table and "interrupted" in table and "Bash" in table
    assert cli.main(["tickle", "--session", SESSION, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["tickle"] is True
    assert cli.main(["tickle", "--session", SESSION]) == 0
    assert "nudged tariff-lane" in capsys.readouterr().out
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
    entry = _entry("assistant", [{"type": "text", "text": "You've reached your Fable 5 limit. Switch to another model."}], uuid=f"banner-{kind}")
    if kind == "error":
        entry["error"] = "rate_limit"
    elif kind == "quota":
        entry["quotaLimits"] = {"status": "rejected", "rateLimitType": "five_hour"}
    else:
        entry["isApiErrorMessage"] = True
    return entry


@pytest.mark.parametrize("kind", ["error", "quota", "api-error"])
def test_restart_stub_and_limit_banner_are_seen_through(tmp_path, kind):
    """The desktop app's account-switch sequence, replayed from a real transcript
    (2026-08-23): work → limit banner → resume stub. The turn underneath decides."""
    rows = [_entry("assistant", TOOL_USE, uuid="a1"), _entry("user", TOOL_RESULT, uuid="u2"),
            _limit_banner(kind), STUB_USER, _stub_assistant()]
    state = tickle.turn_state(_write(tmp_path / "t.jsonl", rows))
    assert state["state"] == "interrupted"
    assert state["restart_stubs"] == 1 and state["limit_banner"] is True
    assert "hit a usage limit" in state["detail"] and "resume stub" in state["detail"]
    assert tickle.dedupe_key(state) == "stub-a1"
    text = tickle.message(state)
    assert "usage-limit banner" in text and "Detected in the transcript" in text
    assert "fresh" not in text and "account switch" not in text
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
    """Team sign-in restarts sessions, then the switch to personal restarts
    them again ~9 minutes later; the second hop must not be cooled down."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    rows = [_entry("assistant", TOOL_USE, uuid="a1"), STUB_USER, _stub_assistant("hop-1")]
    path = _write(projects / f"{SESSION}.jsonl", rows)
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is True
    rows += [STUB_USER, _stub_assistant("hop-2")]
    path = _write(projects / f"{SESSION}.jsonl", rows)
    record = tickle.load_record(SESSION)
    from subfleet.util import iso, now_local
    from datetime import timedelta
    record["at"] = iso(now_local() - timedelta(seconds=120))  # 2 min ago > 90s cooldown
    tickle.save_record(SESSION, record)
    verdict = tickle.deliver(SESSION, path, delay_s=0)
    assert verdict["delivered"] is True, verdict


def test_each_restart_earns_one_nudge(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    rows = [_entry("assistant", TOOL_USE, uuid="a1"), STUB_USER, _stub_assistant("stub-r1")]
    path = _write(projects / f"{SESSION}.jsonl", rows)
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is True
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is False  # same restart: once
    # the app restarts again around the SAME stuck turn → a new stub uuid → nudge again
    rows2 = rows + [STUB_USER, _stub_assistant("stub-r2")]
    path = _write(projects / f"{SESSION}.jsonl", rows2)
    tickle.save_record(SESSION, {**tickle.load_record(SESSION), "at": "2026-08-23T00:00:00+00:00"})  # clear cooldown
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is True
    assert len([l for l in registry["inbox"].lines if l.get("type") == "user"]) == 2


def test_stub_written_during_the_wait_is_not_activity(registry, tmp_path):
    """The stub lands ~0.7s after process start — often inside the nudger's wait.
    Liveness is judged on the real turn, so the stub write must not stand down."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    path = _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1")])

    def stub_lands(_seconds):
        with path.open("a") as stream:
            stream.write(json.dumps(STUB_USER) + "\n")
            stream.write(json.dumps(_stub_assistant()) + "\n")

    verdict = tickle.deliver(SESSION, path, delay_s=1.0, sleep=stub_lands)
    assert verdict["delivered"] is True, verdict


def test_all_sweep_excludes_the_invoking_session(registry, tmp_path, monkeypatch, capsys):
    """A long tool call writes no turns, so the sweeping session can look dead
    to itself (self-nudge observed live 2026-08-23). --all must skip self."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1", age_s=400)])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert cli.main(["tickle", "--all"]) == 0
    out = capsys.readouterr().out
    assert "this session (excluded)" in out
    assert "nudged 0" in out
    assert registry["inbox"].lines == []


def test_muster_calls_completed_recent_sessions_and_dedupes(registry, tmp_path):
    """The usage-credits case (2026-08-23 23:03): nothing died, turns ended in
    assistant text, tickle correctly stays quiet — muster is the roll call."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
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
    projects = registry["claude_dir"] / "projects" / "-Users-max"
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
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    cold_id = "cccccccc-dddd-4eee-8fff-000000000001"
    _write(projects / f"{cold_id}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1", age_s=900)])
    _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a2", age_s=900)])  # live → excluded
    done_id = "cccccccc-dddd-4eee-8fff-000000000002"
    _write(projects / f"{done_id}.jsonl", [_entry("assistant", "done", uuid="a3", age_s=900)])  # completed → excluded
    rows = tickle.cold_sessions()
    assert [row["session_id"] for row in rows] == [cold_id]
    assert "Bash" in rows[0]["detail"] and rows[0]["project"] == "-Users-max"


def test_cold_sessions_and_auto_revive_skip_headless_lane_runs(registry, tmp_path, monkeypatch):
    """2026-09-04: the sweep revived five dead `claude -p` lane runs as untracked
    continuations on lane tokens. A lane run is recognised two ways — its
    session id in runs/*/meta.json (subfleet-launched) or a transcript whose
    first prompt came from the SDK (entrypoint sdk-cli + promptSource sdk) —
    and is neither listed cold nor revived. The tmux orchestrator is sdk-cli
    with typed prompts and stays eligible."""
    projects = registry["claude_dir"] / "projects" / "-Users-max-repo-worktrees-lane"
    state = tmp_path / "state"
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(state))
    ledger = tmp_path / "lane-ledger.jsonl"
    ledger.write_text("")
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    launched = "cccccccc-1111-4000-8000-000000000001"
    sdk_only = "cccccccc-1111-4000-8000-000000000002"
    typed = "cccccccc-1111-4000-8000-000000000003"
    (state / "runs" / "20260904-093524-lane").mkdir(parents=True)
    (state / "runs" / "20260904-093524-lane" / "meta.json").write_text(
        json.dumps({"id": "20260904-093524-lane", "session_id": launched, "family": "claude"}))
    headless = {"entrypoint": "sdk-cli", "promptSource": "sdk", "cwd": "/work/lane"}
    _write(projects / f"{launched}.jsonl", [_entry("user", "# lane brief", uuid="u1", age_s=600, **headless),
                                            _entry("assistant", TOOL_USE, uuid="a1", age_s=580)])
    _write(projects / f"{sdk_only}.jsonl", [_entry("user", "# lane brief", uuid="u2", age_s=600, **headless),
                                            _entry("assistant", TOOL_USE, uuid="a2", age_s=580)])
    _write(projects / f"{typed}.jsonl", [_entry("user", "harvest whatever landed", uuid="u3", age_s=600,
                                                entrypoint="sdk-cli", promptSource="typed", cwd="/work/orch"),
                                         _entry("assistant", TOOL_USE, uuid="a3", age_s=580)])

    assert tickle.lane_session_ids() == {launched}
    assert tickle.headless_transcript(projects / f"{sdk_only}.jsonl") is True
    assert tickle.headless_transcript(projects / f"{typed}.jsonl") is False
    assert [row["session_id"] for row in tickle.cold_sessions()] == [typed]

    # auto-revive: the typed session revives; a lane id that somehow reaches the
    # loop is refused with a named reason
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    for cli in (launched, sdk_only, typed):
        d = store / "acct" / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"local_{cli}.json").write_text(json.dumps(
            {"cliSessionId": cli, "cwd": "/work", "permissionMode": "bypassPermissions"}))
    spawned = []
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@example.com", "tok"))
    monkeypatch.setattr(tickle, "revive_session",
                        lambda cli, cwd, token, *, bypass, model=None, popen=None, **kw:
                        spawned.append(cli) or 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert spawned == [typed]
    assert results[typed].get("revived") is True
    assert launched not in results and sdk_only not in results

    monkeypatch.setattr(tickle, "cold_sessions", lambda **kw: [
        {"session_id": sdk_only, "detail": "Bash", "age_s": 600}])
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert "headless lane run" in results[sdk_only]["skip"]
    assert spawned == [typed]


def test_retired_sessions_are_never_listed_cold_or_revived(registry, tmp_path, monkeypatch):
    """A replaced orchestrator whose tmux was killed looks interrupted; the
    retire marker keeps the sweep from resurrecting it headlessly."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    gone = "cccccccc-2222-4000-8000-000000000001"
    _write(projects / f"{gone}.jsonl", [_entry("user", "harvest", uuid="u1", age_s=600, cwd="/work"),
                                        _entry("assistant", TOOL_USE, uuid="a1", age_s=580)])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    assert [row["session_id"] for row in tickle.cold_sessions()] == [gone]
    tickle.retire_session(gone, "replaced by a fresh orchestrator")
    assert tickle.retired(gone) is True
    assert tickle.cold_sessions() == []
    monkeypatch.setattr(tickle, "cold_sessions", lambda **kw: [{"session_id": gone, "detail": "Bash", "age_s": 600}])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert "retired" in results[gone]["skip"]


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


def test_auto_revive_guards_and_launch(registry, tmp_path, monkeypatch):
    """Cold interrupted bypass sessions revive once; husks, probes, prompting
    sessions, and already-revived points are skipped (2026-08-25 live faults)."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    def index(cli, cwd="/work/repo", mode="bypassPermissions"):
        d = store / "acct" / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"local_{cli}.json").write_text(json.dumps(
            {"cliSessionId": cli, "cwd": cwd, "permissionMode": mode}))
    work = "cccccccc-0000-4000-8000-000000000001"
    husk = "cccccccc-0000-4000-8000-000000000002"
    ask = "cccccccc-0000-4000-8000-000000000003"
    _write(projects / f"{work}.jsonl", [_entry("user", "build it", uuid="u1", age_s=600),
                                        _entry("assistant", TOOL_USE, uuid="a1", age_s=580)])
    _write(projects / f"{husk}.jsonl", [_entry("user", "Reply with exactly: ok", uuid="u2", age_s=600)])
    _write(projects / f"{ask}.jsonl", [_entry("user", "x", uuid="u3", age_s=600),
                                       _entry("assistant", TOOL_USE, uuid="a3", age_s=580)])
    index(work); index(ask, mode="default")

    spawned = []
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@example.com", "tok"))
    monkeypatch.setattr(tickle, "revive_session",
                        lambda cli, cwd, token, *, bypass, model=None, popen=None, **kw:
                        spawned.append((cli, cwd, bypass, model)) or 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert results[work].get("revived") is True and results[work]["lane"] == "lane@example.com"
    assert results[work]["model"] == "claude-fable-5-1"
    assert spawned == [(work, "/work/repo", True, "claude-fable-5-1")]
    assert tickle.load_record(work)["history"][-1]["model"] == "claude-fable-5-1"
    assert "one-shot" in results[husk]["skip"]
    assert "permission mode default" in results[ask]["skip"]
    # once per stuck point
    again = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert "already revived" in again[work]["skip"]
    # off switch
    monkeypatch.setenv("SUBFLEET_REVIVE", "off")
    assert tickle.auto_revive() == [{"skip": "disabled (SUBFLEET_REVIVE=off)"}]


def test_probe_lane_walks_ranking_and_caches(tmp_path, monkeypatch):
    pick_calls = []

    def fake_pick(cmd, **kwargs):
        pick_calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ranked": [
                {"email": "dead@x"}, {"email": "live@x"},
            ]}),
        )

    monkeypatch.setattr(tickle.subprocess, "run", fake_pick)
    monkeypatch.setattr(tickle, "_lane_token", lambda e: f"tok-{e}")
    calls = []
    def fake_run(cmd, **kw):
        calls.append(kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"])
        class R: pass
        r = R()
        r.returncode = 0
        r.stdout = "You've reached your Fable 5 limit" if "dead" in calls[-1] else "ok"
        return r
    clock = [1000.0]
    assert tickle.probe_lane(runner=fake_run, now_fn=lambda: clock[0]) == ("live@x", "tok-live@x")
    assert calls == ["tok-dead@x", "tok-live@x"]
    # cached: no new probes inside the TTL
    assert tickle.probe_lane(runner=fake_run, now_fn=lambda: clock[0] + 60) == ("live@x", "tok-live@x")
    assert len(calls) == 2
    tickle.clear_lane_cache()
    assert tickle.probe_lane(runner=fake_run, now_fn=lambda: clock[0] + 61) == ("live@x", "tok-live@x")
    assert len(calls) == 4
    assert len(pick_calls) == 2
    for command, _kwargs in pick_calls:
        assert command[:3] == [tickle._subfleet_bin(), "pick", "claude"]
        assert command[command.index("--model") + 1] == "claude-fable-5-1"
        assert command.count("--model") == 1
        assert "--json" in command and "--all" in command


@pytest.mark.parametrize("model", [None, "claude-opus-5"])
def test_revive_session_model_flag_is_opt_in(tmp_path, monkeypatch, model):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-leak")
    calls = []

    class Proc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return Proc()

    kwargs = {"bypass": True, "popen": fake_popen}
    if model is not None:
        kwargs["model"] = model
    pid = tickle.revive_session(SESSION, "/work/repo", "oauth-token", **kwargs)
    assert pid == 4242
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    prompt = tickle.message({}, revive={"host": tickle.REVIVE_HOST_PRINT, "lane": None,
                                        "model": model, "detail": None})
    expected = ["claude", "-p", "--resume", SESSION, prompt]
    if model is not None:
        expected += ["--model", model]
    expected.append("--dangerously-skip-permissions")
    assert cmd == expected
    assert ("--model" in cmd) is (model is not None)
    assert kwargs["cwd"] == "/work/repo"
    assert kwargs["stdin"] == tickle.subprocess.DEVNULL
    assert kwargs["stderr"] == tickle.subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]


def test_probe_lane_any_prefers_fable_and_notices_its_recovery(monkeypatch):
    fable = "claude-fable-5-1"
    opus = "claude-opus-5"
    available = {fable: False, opus: True}
    calls = []
    ranked_models = []
    monkeypatch.setattr(
        tickle, "_ranked_lanes",
        lambda model: ranked_models.append(model) or ["first@x", "second@x"],
    )
    monkeypatch.setattr(tickle, "_lane_token", lambda email: f"tok-{email}")

    class Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        model = cmd[cmd.index("--model") + 1]
        calls.append((model, kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"]))
        return Result("ok" if available[model] else "usage limit")

    clock = [1000.0]
    assert tickle.probe_lane_any(
        [fable, opus], runner=fake_run, now_fn=lambda: clock[0],
    ) == ("first@x", "tok-first@x", opus)
    assert calls == [
        (fable, "tok-first@x"), (fable, "tok-second@x"),
        (opus, "tok-first@x"),
    ]
    assert ranked_models == [fable, opus]

    # Opus remains positively cached, but a Fable miss is never negative-cached.
    # Its recovery must therefore win on the very next preference walk.
    available[fable] = True
    clock[0] += 60
    assert tickle.probe_lane_any(
        [fable, opus], runner=fake_run, now_fn=lambda: clock[0],
    ) == ("first@x", "tok-first@x", fable)
    assert calls[-1] == (fable, "tok-first@x")
    assert ranked_models == [fable, opus, fable]


def test_probe_lane_cache_keeps_independent_model_entries(monkeypatch):
    fable = "claude-fable-5-1"
    opus = "claude-opus-5"
    calls = []
    ranked_models = []
    monkeypatch.setattr(
        tickle, "_ranked_lanes",
        lambda model: ranked_models.append(model) or ["lane@x"],
    )
    monkeypatch.setattr(tickle, "_lane_token", lambda email: f"tok-{email}")

    class Result:
        returncode = 0
        stdout = "ok"

    def fake_run(cmd, **kwargs):
        calls.append(cmd[cmd.index("--model") + 1])
        return Result()

    assert tickle.probe_lane(fable, runner=fake_run, now_fn=lambda: 1000.0)
    assert tickle.probe_lane(opus, runner=fake_run, now_fn=lambda: 1001.0)
    assert tickle.probe_lane(fable, runner=fake_run, now_fn=lambda: 1060.0)
    assert tickle.probe_lane(opus, runner=fake_run, now_fn=lambda: 1061.0)
    assert calls == [fable, opus]
    cached = json.loads(tickle._probe_cache_path().read_text())
    assert set(cached["models"]) == {fable, opus}
    # The 10-minute TTL is strict: exactly 600 seconds old re-probes each key.
    assert tickle.probe_lane(fable, runner=fake_run, now_fn=lambda: 1600.0)
    assert tickle.probe_lane(opus, runner=fake_run, now_fn=lambda: 1601.0)
    assert calls == [fable, opus, fable, opus]
    assert ranked_models == [fable, opus, fable, opus]


def test_probe_lane_reads_the_legacy_single_model_cache(monkeypatch):
    tickle.atomic_write_json(tickle._probe_cache_path(), {
        "email": "legacy@x", "model": "claude-fable-5-1", "at": 1000.0,
    })
    monkeypatch.setattr(tickle, "_lane_token", lambda email: f"tok-{email}")
    monkeypatch.setattr(
        tickle, "_ranked_lanes",
        lambda model: pytest.fail("a fresh legacy cache entry must not live-probe"),
    )
    assert tickle.probe_lane(
        "claude-fable-5-1", now_fn=lambda: 1060.0,
    ) == ("legacy@x", "tok-legacy@x")


def test_revive_model_preferences_follow_env_and_explicit_override(monkeypatch):
    assert tickle.revive_models({}) == ["claude-fable-5-1", "claude-opus-5"]
    monkeypatch.setenv(
        "SUBFLEET_REVIVE_MODELS",
        " custom-model, claude-opus-5, custom-model, , claude-fable-5-1 ",
    )
    assert tickle.revive_models() == [
        "custom-model", "claude-opus-5", "claude-fable-5-1",
    ]
    assert tickle.revive_model_preferences() == [
        "custom-model", "claude-opus-5", "claude-fable-5-1",
    ]
    assert tickle.revive_model_preferences(allow_fallback=False) == ["custom-model"]
    assert tickle.revive_model_preferences("pinned-model") == [
        "pinned-model", "custom-model", "claude-opus-5", "claude-fable-5-1",
    ]
    assert tickle.revive_model_preferences(
        "pinned-model", allow_fallback=False,
    ) == ["pinned-model"]


def test_live_revive_sessions_counts_only_detached_marked_resumes():
    detached = "11111111-1111-4111-8111-111111111111"
    app_child = "22222222-2222-4222-8222-222222222222"
    unmarked = "33333333-3333-4333-8333-333333333333"

    class Result:
        returncode = 0
        stdout = "\n".join([
            " PID  PPID COMMAND",
            f"101 1 claude -p --resume {detached} subfleet: continue work",
            f"202 77 claude -p --resume {app_child} subfleet: desktop child",
            f"303 1 claude -p --resume {unmarked} Continue from where you left off.",
            "bad row",
        ])

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Result()

    assert tickle.live_revive_sessions(runner=fake_run) == {detached: [101]}
    assert calls[0][0] == ["ps", "-Ao", "pid=,ppid=,command="]
    assert calls[0][1]["timeout"] == 30


@pytest.mark.parametrize("failure", [OSError("ps denied"), 1])
def test_live_revive_sessions_reports_an_unknown_census(failure):
    def fake_run(cmd, **kwargs):
        if isinstance(failure, BaseException):
            raise failure

        class Result:
            returncode = failure
            stdout = ""

        return Result()

    assert tickle.live_revive_sessions(runner=fake_run) is None


def test_auto_revive_fails_closed_when_lock_or_census_is_unavailable(monkeypatch):
    monkeypatch.setattr(tickle, "_acquire_revive_lock", lambda: None)
    monkeypatch.setattr(
        tickle, "cold_sessions",
        lambda: pytest.fail("lock failure must stop before candidate discovery"),
    )
    assert tickle.auto_revive() == [{
        "skip": "another revive pass is running or the revive lock is unavailable",
    }]

    class Lock:
        pass

    monkeypatch.setattr(tickle, "_acquire_revive_lock", lambda: Lock())
    monkeypatch.setattr(tickle, "_release_revive_lock", lambda stream: None)
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: None)
    assert tickle.auto_revive() == [{"skip": "could not count live detached revives"}]


def test_auto_revive_preserves_min_age_probe_cwd_and_batch_guards(
        tmp_path, monkeypatch):
    projects = Path(os.environ["SUBFLEET_CLAUDE_DIR"]) / "projects" / "-Users-max"
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))

    def index(cli):
        directory = store / "acct" / "org"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"local_{cli}.json").write_text(json.dumps({
            "cliSessionId": cli, "cwd": "/work/repo",
            "permissionMode": "bypassPermissions",
        }))

    young = "aaaaaaaa-0000-4000-8000-000000000001"
    probe = "aaaaaaaa-0000-4000-8000-000000000002"
    no_cwd = "aaaaaaaa-0000-4000-8000-000000000003"
    ready = "aaaaaaaa-0000-4000-8000-000000000004"
    capped = "aaaaaaaa-0000-4000-8000-000000000005"
    _write(projects / f"{young}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1", age_s=30)])
    _write(projects / f"{probe}.jsonl", [
        _entry("assistant", "ok", uuid="a2", age_s=700),
        _entry("user", tickle.PROBE_PROMPT, uuid="u2", age_s=600),
    ])
    _write(projects / f"{no_cwd}.jsonl", [_entry("assistant", TOOL_USE, uuid="a3", age_s=600)])
    _write(projects / f"{ready}.jsonl", [_entry("assistant", TOOL_USE, uuid="a4", age_s=600)])
    _write(projects / f"{capped}.jsonl", [_entry("assistant", TOOL_USE, uuid="a5", age_s=600)])
    for cli in (young, probe, ready, capped):
        index(cli)
    monkeypatch.setattr(tickle, "cold_sessions", lambda: [
        {"session_id": young, "age_s": 30, "detail": "young"},
        {"session_id": probe, "age_s": 600, "detail": "probe"},
        {"session_id": no_cwd, "age_s": 600, "detail": "missing cwd"},
        {"session_id": ready, "age_s": 600, "detail": "ready"},
        {"session_id": capped, "age_s": 600, "detail": "capped"},
    ])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@x", "tok"))
    launched = []
    monkeypatch.setattr(
        tickle, "revive_session",
        lambda cli, cwd, token, *, bypass, model=None, **kw:
        launched.append((cli, model)) or 4242,
    )

    results = {row["session_id"]: row for row in tickle.auto_revive(max_batch=1)}
    assert "app may still restart" in results[young]["skip"]
    assert "own lane probe" in results[probe]["skip"]
    assert "no cwd found" in results[no_cwd]["skip"]
    assert results[ready]["revived"] is True
    assert results[capped]["skip"] == "batch cap reached"
    assert launched == [(ready, "claude-fable-5-1")]


def test_auto_revive_counts_only_live_detached_resumes_against_cap(
        monkeypatch):
    candidate = "aaaaaaaa-0000-4000-8000-000000000006"
    running = "aaaaaaaa-0000-4000-8000-000000000007"
    monkeypatch.setattr(tickle, "cold_sessions", lambda: [
        {"session_id": candidate, "age_s": 600, "detail": "ready"},
    ])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {running: [707]})
    monkeypatch.setattr(
        tickle, "probe_lane",
        lambda **kw: pytest.fail("capacity probe must not run at the live cap"),
    )
    assert tickle.auto_revive(max_batch=1) == [
        {"session_id": candidate, "skip": "batch cap reached"},
    ]


def _prepare_cold_revive_candidate(tmp_path, monkeypatch, cli_id):
    projects = Path(os.environ["SUBFLEET_CLAUDE_DIR"]) / "projects" / "-Users-max"
    _write(projects / f"{cli_id}.jsonl", [
        _entry("user", "build it", uuid=f"u-{cli_id[-4:]}", age_s=600),
        _entry("assistant", TOOL_USE, uuid=f"a-{cli_id[-4:]}", age_s=580),
    ])
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    directory = store / "acct" / "org"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"local_{cli_id}.json").write_text(json.dumps({
        "cliSessionId": cli_id, "cwd": "/work/repo",
        "permissionMode": "bypassPermissions",
    }))


def test_auto_revive_never_crosses_tiers_without_an_explicit_model(tmp_path, monkeypatch):
    """Max 8/26: fable-grade sessions must not silently resume on Opus. The
    fallback chain applies only when a human passes --model; the automatic
    pass parks a session whose own tier has no live lane."""
    cli_id = "aaaaaaaa-0000-4000-8000-000000000008"
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, cli_id)
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    probes = []

    def fake_probe(model, **kwargs):
        probes.append(model)
        return ("opus@x", "tok") if model == "claude-opus-5" else None

    launched = []
    monkeypatch.setattr(tickle, "probe_lane", fake_probe)
    monkeypatch.setattr(
        tickle, "revive_session",
        lambda cli, cwd, token, *, bypass, model=None, **kw:
        launched.append((cli, cwd, token, bypass, model)) or 4242,
    )
    # automatic: no recorded model -> default tier only; opus lane must not be used
    result = tickle.auto_revive()
    assert probes == ["claude-fable-5-1"]
    assert launched == []
    assert result[0]["skip"] == "parked: no lane serves claude-fable-5-1"
    # explicit --model opts into the fallback chain and records what launched
    probes.clear()
    result = tickle.auto_revive(model="claude-fable-5-1")
    assert probes == ["claude-fable-5-1", "claude-opus-5"]
    assert result[0]["revived"] is True and result[0]["model"] == "claude-opus-5"
    assert launched == [(cli_id, "/work/repo", "tok", True, "claude-opus-5")]
    assert tickle.load_record(cli_id)["history"][-1]["model"] == "claude-opus-5"


def test_auto_revive_targets_each_sessions_own_recorded_model(tmp_path, monkeypatch):
    """A session recorded as Opus (e.g. an activated paper reader) revives on
    an Opus lane in the same pass that parks its fable-recorded neighbor."""
    fable_id = "aaaaaaaa-0000-4000-8000-00000000000a"
    opus_id = "aaaaaaaa-0000-4000-8000-00000000000b"
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, fable_id)
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, opus_id)
    store = Path(os.environ["SUBFLEET_SESSION_STORE"])
    for path in store.glob("*/*/local_*.json"):
        data = json.loads(path.read_text())
        if data.get("cliSessionId") == opus_id:
            data["model"] = "claude-opus-5"
            path.write_text(json.dumps(data))
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    probes = []

    def fake_probe(model, **kwargs):
        probes.append(model)
        return ("opus@x", "tok") if model == "claude-opus-5" else None

    launched = []
    monkeypatch.setattr(tickle, "probe_lane", fake_probe)
    monkeypatch.setattr(
        tickle, "revive_session",
        lambda cli, cwd, token, *, bypass, model=None, **kw:
        launched.append((cli, model)) or 4242,
    )
    results = {r["session_id"]: r for r in tickle.auto_revive()}
    assert results[opus_id]["revived"] is True
    assert results[opus_id]["model"] == "claude-opus-5"
    assert results[fable_id]["skip"] == "parked: no lane serves claude-fable-5-1"
    assert launched == [(opus_id, "claude-opus-5")]
    # one probe per distinct target, not per session
    assert sorted(probes) == ["claude-fable-5-1", "claude-opus-5"]


def test_auto_revive_reads_the_serving_model_from_the_transcript(tmp_path, monkeypatch):
    """A session whose store entry records no model revives on the model that
    actually served its last real turn — not on a hardcoded default — and the
    app's synthetic banner entries (model "<synthetic>") are skipped."""
    cli_id = "aaaaaaaa-0000-4000-8000-00000000000c"
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, cli_id)
    projects = Path(os.environ["SUBFLEET_CLAUDE_DIR"]) / "projects" / "-Users-max"
    served = _entry("assistant", TOOL_USE, uuid="a-serv", age_s=580)
    served["message"]["model"] = "claude-opus-5"
    banner = _entry("assistant", "You've hit your usage limit", uuid="a-ban",
                    age_s=560, isApiErrorMessage=True)
    banner["message"]["model"] = "<synthetic>"
    _write(projects / f"{cli_id}.jsonl", [
        _entry("user", "grind it", uuid="u-serv", age_s=600), served, banner,
    ])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    probes = []
    monkeypatch.setattr(
        tickle, "probe_lane",
        lambda model, **kwargs: probes.append(model) or ("opus@x", "tok"),
    )
    launched = []
    monkeypatch.setattr(
        tickle, "revive_session",
        lambda cli, cwd, token, *, bypass, model=None, **kw:
        launched.append((cli, model)) or 4242,
    )
    results = {r["session_id"]: r for r in tickle.auto_revive()}
    assert probes == ["claude-opus-5"]
    assert results[cli_id]["revived"] is True
    assert launched == [(cli_id, "claude-opus-5")]


def test_auto_revive_no_fallback_never_probes_opus(tmp_path, monkeypatch):
    cli_id = "aaaaaaaa-0000-4000-8000-000000000009"
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, cli_id)
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    probes = []
    monkeypatch.setattr(
        tickle, "probe_lane",
        lambda model, **kwargs: probes.append(model) or None,
    )
    monkeypatch.setattr(
        tickle, "revive_session",
        lambda *args, **kwargs: pytest.fail("no-capacity revive must not launch"),
    )
    result = tickle.auto_revive(model="claude-fable-5-1", allow_fallback=False)
    assert probes == ["claude-fable-5-1"]
    assert result[0]["skip"] == "parked: no lane serves claude-fable-5-1"


def test_revive_cli_forwards_model_flags_and_prints_selected_model(
        monkeypatch, capsys):
    seen = {}

    def fake_auto_revive(**kwargs):
        seen.update(kwargs)
        return [{
            "session_id": SESSION, "would_revive": True,
            "models": ["claude-opus-5"], "detail": "cut off",
        }]

    monkeypatch.setattr(tickle, "auto_revive", fake_auto_revive)
    assert cli.main([
        "revive", "--model", "claude-opus-5", "--no-fallback",
        "--dry-run", "--max", "3",
    ]) == 0
    assert seen == {
        "max_batch": 3, "dry_run": True, "model": "claude-opus-5",
        "allow_fallback": False,
    }
    output = capsys.readouterr().out
    assert "would revive models=claude-opus-5" in output


def test_revive_cli_prints_the_selected_model(monkeypatch, capsys):
    monkeypatch.setattr(tickle, "auto_revive", lambda **kwargs: [{
        "session_id": SESSION, "revived": True, "pid": 4242,
        "lane": "lane@x", "model": "claude-opus-5", "detail": "cut off",
    }])
    assert cli.main(["revive"]) == 0
    output = capsys.readouterr().out
    assert "model=claude-opus-5" in output


def test_revive_help_documents_model_fallback(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["revive", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--model" in output
    assert "--no-fallback" in output
    assert "SUBFLEET_REVIVE_MODELS" in output


def test_revive_targets_normalize_the_retired_fable_pin(monkeypatch):
    monkeypatch.setenv("SUBFLEET_REVIVE_MODELS", "claude-fable-5, claude-opus-5, claude-fable-5-1")
    assert tickle.revive_models() == ["claude-fable-5-1", "claude-opus-5"]
    assert tickle.revive_model_preferences("claude-fable-5") == ["claude-fable-5-1", "claude-opus-5"]
    assert tickle.revive_model_preferences("fable", allow_fallback=False) == ["claude-fable-5-1"]
    monkeypatch.delenv("SUBFLEET_REVIVE_MODELS")
    assert tickle.revive_models() == ["claude-fable-5-1", "claude-opus-5"]


def test_probe_lane_normalizes_the_retired_fable_pin(monkeypatch):
    ranked_models = []
    monkeypatch.setattr(tickle, "_ranked_lanes", lambda model: ranked_models.append(model) or ["lane@x"])
    monkeypatch.setattr(tickle, "_lane_token", lambda email: f"tok-{email}")
    probed = []

    class Result:
        returncode = 0
        stdout = "ok"

    def fake_run(cmd, **kwargs):
        probed.append(cmd[cmd.index("--model") + 1])
        return Result()

    assert tickle.probe_lane("claude-fable-5", runner=fake_run, now_fn=lambda: 1000.0) == ("lane@x", "tok-lane@x")
    assert probed == ["claude-fable-5-1"] and ranked_models == ["claude-fable-5-1"]
    # cached under the current id, so the current pin is served from the cache
    assert tickle.probe_lane("claude-fable-5-1", runner=fake_run, now_fn=lambda: 1001.0) == ("lane@x", "tok-lane@x")
    assert probed == ["claude-fable-5-1"]
    assert tickle.probe_lane_any(
        ["claude-fable-5", "claude-fable-5-1"], runner=fake_run, now_fn=lambda: 1002.0,
    ) == ("lane@x", "tok-lane@x", "claude-fable-5-1")


def test_auto_revive_resumes_a_retired_fable_session_on_the_current_pin(registry, tmp_path, monkeypatch):
    """A session whose store entry (or last served turn) names claude-fable-5
    revives on claude-fable-5-1 — never on the retired model."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    work = "dddddddd-0000-4000-8000-000000000001"
    entry_dir = store / "acct" / "org"
    entry_dir.mkdir(parents=True)
    (entry_dir / f"local_{work}.json").write_text(json.dumps({
        "cliSessionId": work, "cwd": "/work/repo",
        "permissionMode": "bypassPermissions", "model": "claude-fable-5",
    }))
    _write(projects / f"{work}.jsonl", [_entry("user", "build it", uuid="u1", age_s=600),
                                        _entry("assistant", TOOL_USE, uuid="a1", age_s=580)])
    spawned = []
    probed = []
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane_any",
                        lambda models, **kw: probed.append(list(models)) or ("lane@example.com", "tok", models[0]))
    monkeypatch.setattr(tickle, "revive_session",
                        lambda cli, cwd, token, *, bypass, model=None, popen=None, **kw:
                        spawned.append((cli, cwd, bypass, model)) or 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert results[work].get("revived") is True
    assert results[work]["model"] == "claude-fable-5-1"
    assert probed == [["claude-fable-5-1"]]
    assert spawned == [(work, "/work/repo", True, "claude-fable-5-1")]


TOO_OLD_400 = (
    "API Error: 400 Claude Code 2.1.87 does not support this model; version "
    "2.1.251 or newer is required. Run 'claude update', or update the Claude "
    "desktop app, then try again."
)


def test_probe_lane_raises_on_a_too_old_cli_instead_of_walking_lanes(monkeypatch):
    """2026-09-02: the launchd revive job ran a Homebrew cask at 2.1.87; every
    Fable 5.1 probe got the version 400 and sessions were parked as "no lane
    serves". A host fault stops the walk on the first lane."""
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/opt/homebrew/bin/claude")
    monkeypatch.setattr(tickle, "_ranked_lanes", lambda model: ["a@x", "b@x", "c@x"])
    monkeypatch.setattr(tickle, "_lane_token", lambda email: f"tok-{email}")
    probes = []

    class Result:
        returncode = 1
        stdout = TOO_OLD_400
        stderr = ""

    def fake_run(cmd, **kwargs):
        probes.append(cmd)
        return Result()

    with pytest.raises(tickle.ClaudeCliTooOld) as excinfo:
        tickle.probe_lane("claude-fable-5-1", runner=fake_run, now_fn=lambda: 1000.0)
    assert len(probes) == 1
    assert probes[0][0] == "/opt/homebrew/bin/claude"
    message = str(excinfo.value)
    assert "Claude Code 2.1.87 does not support this model; version 2.1.251 or newer is required" in message
    assert "/opt/homebrew/bin/claude cannot request claude-fable-5-1" in message
    assert "claude update" in message
    assert not tickle._probe_cache_path().exists()


def test_auto_revive_reports_a_too_old_cli_once_and_never_as_capacity(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    entry_dir = store / "acct" / "org"
    entry_dir.mkdir(parents=True)
    sessions = ["eeeeeeee-0000-4000-8000-000000000001", "eeeeeeee-0000-4000-8000-000000000002"]
    for cli_id, model in zip(sessions, ("claude-fable-5-1", "claude-fable-5[1m]")):
        (entry_dir / f"local_{cli_id}.json").write_text(json.dumps({
            "cliSessionId": cli_id, "cwd": "/work/repo",
            "permissionMode": "bypassPermissions", "model": model,
        }))
        _write(projects / f"{cli_id}.jsonl", [_entry("user", "build it", uuid="u1", age_s=600),
                                              _entry("assistant", TOOL_USE, uuid="a1", age_s=580)])
    probed = []
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})

    def fake_probe_any(models, **kw):
        probed.append(list(models))
        raise tickle.ClaudeCliTooOld(
            "Claude Code 2.1.87 does not support this model; version 2.1.251 or "
            "newer is required — /opt/homebrew/bin/claude cannot request " + models[0]
        )

    monkeypatch.setattr(tickle, "probe_lane_any", fake_probe_any)
    monkeypatch.setattr(tickle, "revive_session",
                        lambda *a, **kw: pytest.fail("nothing may launch on a too-old CLI"))
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    # two distinct target tuples (bare id vs [1m] form) → one probe each, no more
    assert sorted(probed) == [["claude-fable-5-1"], ["claude-fable-5-1[1m]"]]
    for cli_id in sessions:
        skip = results[cli_id]["skip"]
        assert skip.startswith("claude CLI too old for claude-fable-5-1")
        assert "/opt/homebrew/bin/claude" in skip
        assert "no lane serves" not in skip
        assert "revived" not in results[cli_id]


def test_revive_session_runs_the_resolved_claude_binary(monkeypatch):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/explicit/claude")
    calls = []

    class Proc:
        pid = 7

    monkeypatch.setattr(tickle, "revive_session", tickle.revive_session)
    pid = tickle.revive_session(SESSION, "/work/repo", "tok", bypass=False,
                                popen=lambda cmd, **kw: calls.append(list(cmd)) or Proc())
    assert pid == 7 and calls[0][0] == "/explicit/claude"
